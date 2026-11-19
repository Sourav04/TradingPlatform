from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from trading_platform.api.dashboard import ASSETS, dashboard_router
from trading_platform.backtesting.replay import SimulatedFill, run_replay
from trading_platform.brokers.contracts import BrokerUnavailable
from trading_platform.brokers.fake import FakeBroker
from trading_platform.certification.readiness import readiness
from trading_platform.core.config import database_url, get_logger, news_latency
from trading_platform.core.models import ManualClock, Signal
from trading_platform.execution.service import ExecutionService
from trading_platform.execution.store import Store
from trading_platform.market_data.feed import SimulatedFeed, fixture_quotes
from trading_platform.news_engine.events import Event, replay
from trading_platform.news_engine.fixtures import fixture_collectors
from trading_platform.risk.checks import RiskDenied, RiskGate, RiskLimits
from trading_platform.strategies.ema import EMACrossover

log = get_logger(__name__)


@dataclass(frozen=True)
class SimulationReport:
    mode: str
    signals: list[Signal]
    orders: list[SimulatedFill]
    positions: dict[str, int]
    news: list[Event]


def create_app(store: Store | None = None) -> FastAPI:
    database = store if store is not None else Store(database_url())
    latency = news_latency()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            database.initialize()
            yield
        finally:
            database.engine.dispose()

    api = FastAPI(title="TradingPlatform Simulation", lifespan=lifespan)

    api.include_router(dashboard_router(database, latency))
    api.mount(
        "/dashboard/assets", StaticFiles(directory=ASSETS), name="dashboard-assets"
    )

    @api.exception_handler(SQLAlchemyError)
    @api.exception_handler(BrokerUnavailable)
    async def dependencies_unavailable(_: Request, exc: Exception) -> JSONResponse:
        log.error("Simulation dependency unavailable (%s)", type(exc).__name__)
        return JSONResponse(
            {"detail": "Simulation dependency unavailable"}, status_code=503
        )

    @api.get("/health")
    def health() -> dict[str, str | bool]:
        return readiness(database)

    @api.get("/strategies")
    def strategies() -> list[str]:
        return [EMACrossover.name]

    @api.post("/simulations/ema")
    def simulate() -> SimulationReport:
        quotes = list(fixture_quotes())
        clock = ManualClock(quotes[0].timestamp)
        feed = SimulatedFeed()
        strategy = EMACrossover()
        service = ExecutionService(
            database,
            FakeBroker(clock),
            RiskGate(clock, feed, RiskLimits(), frozenset({strategy.name})),
        )
        try:
            signals, orders = run_replay(quotes, strategy, feed, clock, service)
            _, positions = service.reconcile()
        except RiskDenied as exc:
            raise HTTPException(409, str(exc)) from exc
        events = (
            event for collector in fixture_collectors() for event in collector.collect()
        )
        return SimulationReport(
            "simulation",
            signals,
            orders,
            positions,
            list(replay(events, datetime(2025, 1, 7, 10, tzinfo=UTC), latency)),
        )

    return api


app = create_app()
