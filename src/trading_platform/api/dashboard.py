from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from trading_platform.backtesting.historical import Report, Settings, backtest
from trading_platform.backtesting.replay import Costs
from trading_platform.execution.store import Store
from trading_platform.market_data.candles import Candle
from trading_platform.news_engine.events import FRESHNESS, Event, replay
from trading_platform.news_engine.fixtures import fixture_collectors
from trading_platform.news_engine.journal import events_during
from trading_platform.operations.workspace import (
    DAILY_LOSS,
    Evidence,
    evidence_for,
    parse_csv,
)
from trading_platform.strategies.catalog import CATALOG

ASSETS = Path(__file__).with_name("dashboard_assets")


class DashboardInput(BaseModel):
    csv: str = Field(min_length=1, max_length=2_000_000)
    symbol: str = Field(min_length=5, max_length=64)
    start: date
    end: date
    fast: int = Field(default=2, ge=1, le=500)
    slow: int = Field(default=4, ge=2, le=500)
    units: int = Field(default=10, ge=1, le=100)
    cash: Decimal = Decimal("100000")
    fee_bps: Decimal = Decimal("5")
    slippage_bps: Decimal = Decimal("10")
    stop_loss_percent: Decimal = Decimal("2")
    max_risk_percent: Decimal = Decimal("1")
    mock_news: bool = False
    strategy_id: str = CATALOG[0].id
    strategy_version: str = CATALOG[0].version


@dataclass(frozen=True)
class DashboardReport:
    backtest: Report
    news_as_of: datetime
    news_mode: str
    news: list[Event]
    evidence: Evidence


def dashboard_router(store: Store, latency: timedelta) -> APIRouter:
    router = APIRouter()

    @router.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(ASSETS / "index.html")

    @router.post("/backtests")
    def run_dashboard_backtest(inputs: DashboardInput) -> DashboardReport:
        report, candles = run_csv_backtest(inputs)
        bars = [point.bar for point in report.equity_curve]
        if inputs.mock_news:
            events = [
                event
                for collector in fixture_collectors()
                for event in collector.collect()
            ]
        else:
            events = events_during(store, bars[0], bars[-1])
        # A new observation or an expired event can change the deduplicated set.
        changes = (
            change
            for event in events
            for change in (
                event.available_at(latency),
                event.published_at + FRESHNESS + timedelta(microseconds=1),
            )
        )
        candidates = {bisect_left(bars, change) for change in changes}
        visible: dict[tuple[str, str], Event] = {}
        for index in sorted(candidates):
            if index < len(bars):
                for event in replay(events, bars[index], latency):
                    visible.setdefault((event.source, event.source_id), event)
        return DashboardReport(
            report,
            bars[-1],
            "mock" if inputs.mock_news else "recorded",
            list(visible.values()),
            evidence_for(store, inputs.csv, report, candles, inputs.mock_news),
        )

    return router


def run_csv_backtest(inputs: DashboardInput) -> tuple[Report, list[Candle]]:
    try:
        settings = Settings(
            inputs.symbol,
            inputs.start,
            inputs.end,
            inputs.fast,
            inputs.slow,
            inputs.units,
            inputs.cash,
            Costs(inputs.fee_bps, inputs.slippage_bps),
            stop_loss_percent=inputs.stop_loss_percent,
            max_risk_percent=inputs.max_risk_percent,
            max_daily_loss_percent=DAILY_LOSS,
            strategy_id=inputs.strategy_id,
            strategy_version=inputs.strategy_version,
        )
        candles = parse_csv(inputs.csv)
        report = backtest(candles, settings)
    except (ValueError, DecimalException) as exc:
        raise HTTPException(
            422, str(exc) or "Invalid numeric values in backtest"
        ) from exc
    return report, candles
