from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_platform.execution.store import Store
from trading_platform.market_data.candles import INDIA
from trading_platform.operations.session_report import Session as PaperSession
from trading_platform.operations.stream_paper import STATE, StreamState, view
from trading_platform.operations.workspace import (
    EVIDENCE,
    EvidenceRow,
    Results,
    StreamPaperRow,
)


@dataclass(frozen=True)
class SessionMetrics:
    date: date
    as_of: datetime
    pnl: Decimal
    realized: Decimal
    unrealized: Decimal
    equity: Decimal
    fills: int
    drawdown_percent: Decimal


@dataclass(frozen=True)
class Performance:
    deployment_id: str
    symbol: str
    strategy: str
    version: str
    status: str
    feed_status: str
    units: int
    stop: Decimal | None
    remaining_loss_budget: Decimal | None
    current: SessionMetrics | None
    sessions: list[SessionMetrics]
    recorded_pnl: Decimal
    losing_sessions: int
    fills: int
    max_drawdown: Decimal
    historical: Results
    validation: Results | None
    history_complete: bool


def metrics(session: PaperSession) -> SessionMetrics:
    return SessionMetrics(
        session.date,
        session.as_of,
        session.pnl,
        session.realized,
        session.unrealized,
        session.equity,
        session.fills,
        session.drawdown_percent,
    )


def summarize(state: StreamState, evidence_json: str, now: datetime) -> Performance:
    evidence = EVIDENCE.validate_json(evidence_json)
    paper = state.paper
    sessions = [metrics(item) for item in state.sessions]
    today = now.astimezone(INDIA).date()
    current = sessions[-1] if sessions and sessions[-1].date == today else None
    remaining = None
    if current is not None and paper.status != "closed":
        percent = evidence.settings.max_daily_loss_percent
        if percent is not None:
            limit = state.daily_start * percent / 100
            remaining = (
                Decimal(0)
                if state.daily_halted
                else max(Decimal(0), limit + min(current.pnl, Decimal(0)))
            )
    return Performance(
        paper.id,
        paper.symbol,
        paper.strategy_id,
        paper.strategy_version,
        paper.status,
        view(state, now).feed_status,
        paper.units,
        paper.stop,
        remaining,
        current,
        sessions,
        sum((item.pnl for item in sessions), Decimal(0)),
        sum(item.pnl < 0 for item in sessions),
        sum(item.fills for item in sessions),
        state.sessions[-1].deployment_drawdown_percent
        if state.sessions
        else Decimal(0),
        evidence.historical,
        evidence.validation,
        not state.history_started_late and (bool(sessions) or state.last_tick is None),
    )


def performance_router(store: Store) -> APIRouter:
    router = APIRouter()

    @router.get("/operations/paper-performance")
    def performance() -> list[Performance]:
        now = datetime.now(UTC)
        with Session(store.engine) as session:
            statement = (
                select(StreamPaperRow.snapshot, EvidenceRow.summary)
                .join(EvidenceRow, StreamPaperRow.evidence_id == EvidenceRow.id)
                .order_by(StreamPaperRow.approved_at.desc())
                .limit(25)
            )
            return [
                summarize(STATE.validate_json(snapshot), evidence, now)
                for snapshot, evidence in session.execute(statement)
            ]

    return router
