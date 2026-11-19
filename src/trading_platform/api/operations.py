import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from trading_platform.execution.store import Store
from trading_platform.news_engine.events import Event, Source, replay
from trading_platform.news_engine.journal import events_during
from trading_platform.operations.stream_paper import (
    StreamView,
    approve_stream,
    stream_control,
    stream_overview,
)
from trading_platform.operations.workspace import (
    DAILY_LOSS,
    RISK_BUDGET,
    ApprovalError,
    Evidence,
    Paper,
    approve_paper,
    change_paper,
    overview,
)
from trading_platform.risk.checks import RiskLimits
from trading_platform.strategies.catalog import CATALOG, StrategyInfo


@dataclass(frozen=True)
class NewsSignal:
    event: Event
    recommendation: str
    impact: str
    rationale: str
    scope: str


def suggest(event: Event) -> NewsSignal:
    title = event.headline.casefold()
    if re.search(
        r"monetary policy|policy rate|fomc|federal funds|repo rate|policy decision",
        title,
    ):
        return NewsSignal(
            event,
            "Review exposure",
            "Direction uncertain",
            "An official policy release may affect rates and broad market exposure. Check the release.",
            "Broad market",
        )
    return NewsSignal(
        event,
        "Informational",
        "Impact unassessed",
        "The official release is available for review. No automated action is attached to it.",
        "India market" if event.source == Source.RBI else "US macro",
    )


@dataclass(frozen=True)
class OperationsOverview:
    strategies: tuple[StrategyInfo, ...]
    evidence: list[Evidence]
    paper: list[Paper]
    news: list[NewsSignal]
    latest_news_at: datetime | None
    risk_per_trade_percent: Decimal
    daily_loss_percent: Decimal
    max_gross_inr: Decimal
    live_enabled: bool
    live_orders: tuple[()]
    streaming_paper: list[StreamView]


class PaperApproval(BaseModel):
    evidence_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class PaperAction(BaseModel):
    action: Literal["advance", "pause", "resume", "close"]
    revision: int = Field(ge=0)


def operations_router(store: Store, latency: timedelta) -> APIRouter:
    router = APIRouter(prefix="/operations")

    @router.get("")
    def workspace() -> OperationsOverview:
        evidence, paper = overview(store)
        now = datetime.now(UTC)
        events = list(replay(events_during(store, now, now), now, latency))
        return OperationsOverview(
            CATALOG,
            evidence,
            paper,
            [suggest(event) for event in reversed(events)],
            max((event.ingested_at for event in events), default=None),
            RISK_BUDGET,
            DAILY_LOSS,
            RiskLimits().max_gross,
            False,
            (),
            stream_overview(store, now),
        )

    @router.post("/paper", status_code=201)
    def approve(approval: PaperApproval) -> Paper:
        try:
            return approve_paper(store, approval.evidence_id)
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/paper/{paper_id}")
    def change(paper_id: str, command: PaperAction) -> Paper:
        try:
            return change_paper(store, paper_id, command.revision, command.action)
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/stream-paper")
    def stream_status() -> list[StreamView]:
        return stream_overview(store, datetime.now(UTC))

    @router.post("/stream-paper", status_code=201)
    def start_stream(approval: PaperApproval) -> StreamView:
        try:
            return approve_stream(store, approval.evidence_id, datetime.now(UTC))
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/stream-paper/{paper_id}")
    def change_stream(paper_id: str, command: PaperAction) -> StreamView:
        try:
            return stream_control(
                store, paper_id, command.revision, command.action, datetime.now(UTC)
            )
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
