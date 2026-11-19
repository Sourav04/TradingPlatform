from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from trading_platform.backtesting.historical import (
    Fill,
    Report,
    Settings,
    backtest,
    priced_fill,
)
from trading_platform.execution.store import Base, Control, Store
from trading_platform.market_data.candles import INDIA, Candle, parse_candles

RISK_BUDGET = Decimal("1")
DAILY_LOSS = Decimal("2")
MAX_PAPER_BARS = 1_000
CONFIG = TypeAdapter(Settings)
FILLS = TypeAdapter(list[Fill])


class EvidenceRow(Base):
    __tablename__ = "dashboard_evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(40), index=True)
    csv_sha256: Mapped[str] = mapped_column(String(64))
    csv: Mapped[str] = mapped_column(Text)
    settings: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)


class PaperRow(Base):
    __tablename__ = "dashboard_paper"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("dashboard_evidence.id"))
    approved_at: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20))
    cursor: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    blocked: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[str] = mapped_column(Text)


@dataclass(frozen=True)
class Results:
    bars: int
    fills: int
    pnl: Decimal
    drawdown: Decimal


@dataclass(frozen=True)
class Evidence:
    id: str
    created_at: str
    dataset: str
    csv_sha256: str
    strategy_id: str
    strategy_version: str
    symbol: str
    settings: Settings
    historical: Results
    validation: Results | None
    stress: Results | None
    status: str
    confidence: str
    reasons: list[str]


@dataclass(frozen=True)
class PaperAudit:
    action: str
    at: str
    actor: str = "Local operator"


@dataclass(frozen=True)
class Paper:
    id: str
    evidence_id: str
    approved_at: str
    strategy_id: str
    strategy_version: str
    symbol: str
    status: str
    cursor: int
    revision: int
    total_bars: int
    cash: Decimal
    equity: Decimal
    realized: Decimal
    unrealized: Decimal
    units: int
    stop: Decimal | None
    mark: Decimal | None
    fills: list[Fill]
    rejections: list[str]
    audit: list[PaperAudit]


EVIDENCE = TypeAdapter(Evidence)
PAPER = TypeAdapter(Paper)


class ApprovalError(ValueError):
    pass


def parse_csv(csv: str) -> list[Candle]:
    return list(parse_candles(StringIO(csv.removeprefix("\ufeff"))))


def result(report: Report) -> Results:
    return Results(
        len(report.equity_curve),
        report.trade_count,
        report.net_pnl,
        report.max_drawdown_percent,
    )


def build_evidence(
    csv: str, report: Report, candles: list[Candle], sample: bool
) -> Evidence:
    settings = report.settings
    dates = sorted({row.bar.astimezone(INDIA).date() for row in report.equity_curve})
    validation = None
    stress = None
    reasons = ["Historical results do not establish live readiness."]
    if len(dates) >= 6:
        cutoff = dates[len(dates) * 2 // 3]
        validation = result(backtest(candles, replace(settings, start=cutoff)))
        if not validation.fills:
            reasons.append("The later validation window produced no fills.")
        elif validation.pnl <= 0:
            reasons.append(
                "The later validation window was not profitable after costs."
            )
    else:
        reasons.append(
            "At least six distinct session dates are needed for a later validation window."
        )
    if settings.costs.fee_bps + 2 * settings.costs.slippage_bps < 10_000:
        stress = result(
            backtest(
                candles,
                replace(
                    settings,
                    costs=replace(
                        settings.costs, slippage_bps=settings.costs.slippage_bps * 2
                    ),
                ),
            )
        )
        if stress.pnl <= 0:
            reasons.append("Historical P&L was not positive with double slippage.")
    else:
        reasons.append("The cost settings do not permit a double-slippage replay.")
    if sample:
        reasons.append("The built-in dataset is synthetic.")
    confidence = "Needs forward validation"
    if sample or validation is None or validation.fills == 0:
        confidence = "Insufficient evidence"
    elif validation.pnl <= 0 or stress is None or stress.pnl <= 0:
        confidence = "Mixed or weak evidence"
    return Evidence(
        uuid4().hex,
        datetime.now(UTC).isoformat(),
        "Synthetic sample" if sample else "Uploaded CSV",
        sha256(csv.encode()).hexdigest(),
        settings.strategy_id,
        settings.strategy_version,
        settings.symbol,
        settings,
        result(report),
        validation,
        stress,
        "Paper only"
        if 0 < len(report.equity_curve) <= MAX_PAPER_BARS
        else "Research only",
        confidence,
        reasons,
    )


def save_evidence(session: Session, csv: str, evidence: Evidence) -> None:
    session.add(
        EvidenceRow(
            id=evidence.id,
            created_at=evidence.created_at,
            csv_sha256=evidence.csv_sha256,
            csv=csv,
            settings=CONFIG.dump_json(evidence.settings).decode(),
            summary=EVIDENCE.dump_json(evidence).decode(),
        )
    )


def evidence_for(
    store: Store, csv: str, report: Report, candles: list[Candle], sample: bool
) -> Evidence:
    evidence = build_evidence(csv, report, candles, sample)
    with store.transaction() as session:
        save_evidence(session, csv, evidence)
    return evidence


def overview(store: Store) -> tuple[list[Evidence], list[Paper]]:
    with store.engine.connect() as conn:
        evidence = conn.scalars(
            select(EvidenceRow.summary)
            .order_by(EvidenceRow.created_at.desc())
            .limit(30)
        )
        paper = conn.scalars(
            select(PaperRow.snapshot).order_by(PaperRow.approved_at.desc()).limit(25)
        )
        return [EVIDENCE.validate_json(row) for row in evidence], [
            PAPER.validate_json(row) for row in paper
        ]


def approve_paper(store: Store, evidence_id: str) -> Paper:
    with store.transaction() as session:
        if session.get_one(Control, 1).killed:
            raise ApprovalError("The simulation kill switch is active")
        active = session.scalar(
            select(PaperRow.id).where(PaperRow.status != "closed").limit(1)
        )
        if active:
            raise ApprovalError(
                "Close the existing paper deployment before approving another"
            )
        row = session.get(EvidenceRow, evidence_id)
        if row is None:
            raise ApprovalError("Backtest evidence not found")
        evidence = EVIDENCE.validate_json(row.summary)
        settings = evidence.settings
        if evidence.status != "Paper only":
            raise ApprovalError(
                f"Paper replay supports up to {MAX_PAPER_BARS} candles per deployment"
            )
        if (
            settings.max_risk_percent != RISK_BUDGET
            or settings.max_daily_loss_percent != DAILY_LOSS
            or settings.stop_loss_percent is None
        ):
            raise ApprovalError(
                "Backtest must use the shared paper risk policy before approval"
            )
        approved_at = datetime.now(UTC).isoformat()
        paper = Paper(
            uuid4().hex,
            row.id,
            approved_at,
            settings.strategy_id,
            settings.strategy_version,
            settings.symbol,
            "active",
            0,
            0,
            evidence.historical.bars,
            settings.initial_cash,
            settings.initial_cash,
            Decimal(0),
            Decimal(0),
            0,
            None,
            None,
            [],
            [],
            [PaperAudit("approved", approved_at)],
        )
        session.add(
            PaperRow(
                id=paper.id,
                evidence_id=row.id,
                approved_at=paper.approved_at,
                status=paper.status,
                cursor=0,
                revision=0,
                blocked="",
                snapshot=PAPER.dump_json(paper).decode(),
            )
        )
        return paper


def paper_report(paper: Paper, report: Report) -> Paper:
    last = report.equity_curve[-1]
    entry = report.fills[-1] if last.units else None
    entry_cost = -entry.cash_change if entry else Decimal(0)
    unrealized = last.value - last.cash - entry_cost
    realized = last.value - report.settings.initial_cash - unrealized
    mark = (last.value - last.cash) / last.units if last.units else None
    return replace(
        paper,
        cash=last.cash,
        equity=last.value,
        realized=realized,
        unrealized=unrealized,
        units=last.units,
        stop=entry.stop_price if entry else None,
        mark=mark,
        fills=report.fills,
        rejections=[item.reason for item in report.rejections],
    )


def change_paper(store: Store, paper_id: str, revision: int, action: str) -> Paper:
    with store.transaction() as session:
        row = session.get(PaperRow, paper_id)
        if row is None or row.revision != revision:
            raise ApprovalError(
                "Paper deployment changed. Refresh before trying again."
            )
        paper = PAPER.validate_json(row.snapshot)
        if action == "pause" and paper.status == "active":
            paper = replace(paper, status="paused")
        elif action == "resume" and paper.status == "paused":
            if session.get_one(Control, 1).killed:
                raise ApprovalError("The simulation kill switch is active")
            paper = replace(paper, status="active")
        elif action == "advance" and paper.status in ("active", "paused"):
            source = session.get_one(EvidenceRow, row.evidence_id)
            settings = CONFIG.validate_json(source.settings)
            candles = parse_csv(source.csv)
            eligible = [
                i
                for i, candle in enumerate(candles)
                if settings.start
                <= candle.timestamp.astimezone(INDIA).date()
                <= settings.end
            ]
            cursor = paper.cursor + 1
            index = eligible[cursor - 1]
            stamp = candles[index].timestamp
            if paper.status == "paused" or session.get_one(Control, 1).killed:
                row.blocked += stamp.isoformat() + "\n"
            blocks = frozenset(
                datetime.fromisoformat(value) for value in row.blocked.splitlines()
            )
            report = backtest(candles[: index + 1], settings, blocks)
            paper = paper_report(paper, report)
            paper = replace(
                paper,
                cursor=cursor,
                status="finished" if cursor == paper.total_bars else paper.status,
            )
        elif action == "close" and paper.status != "closed":
            if paper.units:
                source = session.get_one(EvidenceRow, row.evidence_id)
                settings = CONFIG.validate_json(source.settings)
                candles = [
                    candle
                    for candle in parse_csv(source.csv)
                    if settings.start
                    <= candle.timestamp.astimezone(INDIA).date()
                    <= settings.end
                ]
                candle = candles[paper.cursor - 1]
                last_entry = paper.fills[-1]
                close = priced_fill(
                    candle,
                    last_entry.signal_bar,
                    -paper.units,
                    settings.costs,
                    candle.close,
                    "paper_close",
                    last_entry.stop_price,
                )
                cash = paper.cash + close.cash_change
                paper = replace(
                    paper,
                    cash=cash,
                    equity=cash,
                    units=0,
                    stop=None,
                    mark=None,
                    realized=cash - settings.initial_cash,
                    unrealized=Decimal(0),
                    fills=[*paper.fills, close],
                )
            paper = replace(paper, status="closed")
        else:
            raise ApprovalError(
                "That action is unavailable in the current deployment state"
            )
        audit = paper.audit
        if action != "advance":
            audit = [*audit, PaperAudit(action, datetime.now(UTC).isoformat())]
        paper = replace(paper, revision=paper.revision + 1, audit=audit)
        row.revision, row.cursor, row.status = (
            paper.revision,
            paper.cursor,
            paper.status,
        )
        row.snapshot = PAPER.dump_json(paper).decode()
        return paper
