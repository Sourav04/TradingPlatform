from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import DecimalException
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, or_, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from trading_platform.backtesting.historical import Settings, backtest
from trading_platform.execution.store import Base, Store
from trading_platform.market_data.candles import INDIA
from trading_platform.operations.workspace import (
    CONFIG,
    EVIDENCE,
    EvidenceRow,
    Results,
    build_evidence,
    parse_csv,
    save_evidence,
)
from trading_platform.strategies.catalog import CATALOG, make_strategy

LEASE = timedelta(minutes=5)
MAX_ATTEMPTS = 3
# Increase when evidence calculation or fill semantics change.
SUITE_VERSION = b"1"


class PlanRow(Base):
    __tablename__ = "backtest_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    csv_hash: Mapped[str] = mapped_column(String(64))
    csv: Mapped[str] = mapped_column(Text)
    settings: Mapped[str] = mapped_column(Text)
    sample: Mapped[bool] = mapped_column(Boolean)


class RunRow(Base):
    __tablename__ = "backtest_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("backtest_plans.id"))
    strategy_version: Mapped[str] = mapped_column(String(40))
    suite_version: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(12), index=True)
    queued_at: Mapped[str] = mapped_column(String(40))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[str | None] = mapped_column(String(40))
    lease_token: Mapped[str | None] = mapped_column(String(32))
    evidence_id: Mapped[str | None] = mapped_column(ForeignKey("dashboard_evidence.id"))
    error: Mapped[str | None] = mapped_column(Text)


@dataclass(frozen=True)
class Job:
    id: str
    status: str
    queued_at: str
    strategy_id: str
    strategy_version: str
    suite_version: str
    symbol: str
    dataset_hash: str
    sample: bool
    attempts: int
    evidence_id: str | None
    error: str | None
    settings: Settings
    historical: Results | None


@dataclass(frozen=True)
class Claim:
    id: str
    lease_token: str
    csv: str
    settings: Settings
    sample: bool


def describe(row: RunRow, plan: PlanRow, summary: str | None = None) -> Job:
    settings = CONFIG.validate_json(plan.settings)
    return Job(
        row.id,
        row.status,
        row.queued_at,
        settings.strategy_id,
        row.strategy_version,
        row.suite_version,
        settings.symbol,
        plan.csv_hash,
        plan.sample,
        row.attempts,
        row.evidence_id,
        row.error,
        replace(settings, strategy_version=row.strategy_version),
        EVIDENCE.validate_json(summary).historical if summary is not None else None,
    )


def enqueue(session: Session, plan: PlanRow, version: str, now: datetime) -> RunRow:
    key = sha256(
        b"\0".join((plan.id.encode(), version.encode(), SUITE_VERSION))
    ).hexdigest()
    existing = session.get(RunRow, key)
    if existing is not None:
        return existing
    job = RunRow(
        id=key,
        plan_id=plan.id,
        strategy_version=version,
        suite_version=SUITE_VERSION.decode(),
        status="queued",
        queued_at=now.isoformat(),
        attempts=0,
    )
    session.add(job)
    return job


def register(store: Store, csv: str, settings: Settings, sample: bool) -> Job:
    strategy = next((item for item in CATALOG if item.id == settings.strategy_id), None)
    if strategy is None or settings.strategy_version != strategy.version:
        raise ValueError("Choose the current version of a catalog strategy")
    make_strategy(
        strategy.id, strategy.version, settings.fast, settings.slow, settings.units
    )
    candles = parse_csv(csv)
    if any(
        current.timestamp <= prior.timestamp
        for prior, current in zip(candles, candles[1:])
    ):
        raise ValueError("Candle timestamps must be unique and strictly increasing")
    if not any(
        settings.start <= candle.timestamp.astimezone(INDIA).date() <= settings.end
        for candle in candles
    ):
        raise ValueError("No candles in the selected India date range")

    # A stable plan follows the registered strategy ID. Jobs pin the actual version.
    configuration = CONFIG.dump_json(replace(settings, strategy_version=""))
    csv_hash = sha256(csv.encode()).hexdigest()
    key = sha256(
        b"\0".join((csv_hash.encode(), str(sample).encode(), configuration))
    ).hexdigest()
    with store.transaction() as session:
        plan = session.get(PlanRow, key)
        if plan is None:
            plan = PlanRow(
                id=key,
                csv_hash=csv_hash,
                csv=csv,
                settings=configuration.decode(),
                sample=sample,
            )
            session.add(plan)
            session.flush()
        return describe(
            enqueue(session, plan, strategy.version, datetime.now(UTC)), plan
        )


def schedule(store: Store, now: datetime) -> None:
    versions = {item.id: item.version for item in CATALOG}
    with store.transaction() as session:
        for plan in session.scalars(select(PlanRow)).all():
            strategy_id = CONFIG.validate_json(plan.settings).strategy_id
            if strategy_id in versions:
                enqueue(session, plan, versions[strategy_id], now)


def claim_next(store: Store, now: datetime) -> Claim | None:
    stamp = now.isoformat()
    with store.transaction() as session:
        statement = (
            select(RunRow)
            .where(
                or_(
                    RunRow.status == "queued",
                    (RunRow.status == "running") & (RunRow.lease_until <= stamp),
                )
            )
            .order_by(RunRow.queued_at, RunRow.id)
        )
        for row in session.scalars(statement):
            if row.suite_version != SUITE_VERSION.decode():
                row.status, row.error = (
                    "failed",
                    "This evidence suite version is no longer available",
                )
                row.lease_token, row.lease_until = None, None
                continue
            if row.attempts >= MAX_ATTEMPTS:
                row.status, row.error = (
                    "failed",
                    "The worker lease expired after repeated attempts",
                )
                row.lease_token, row.lease_until = None, None
                continue
            plan = session.get_one(PlanRow, row.plan_id)
            token = uuid4().hex
            row.status, row.lease_token = "running", token
            row.attempts += 1
            row.lease_until = (now + LEASE).isoformat()
            settings = replace(
                CONFIG.validate_json(plan.settings),
                strategy_version=row.strategy_version,
            )
            return Claim(row.id, token, plan.csv, settings, plan.sample)
    return None


def run_claim(store: Store, claim: Claim) -> None:
    evidence = None
    error = None
    try:
        candles = parse_csv(claim.csv)
        report = backtest(candles, claim.settings)
        evidence = build_evidence(claim.csv, report, candles, claim.sample)
    except (ValueError, DecimalException) as exc:
        error = str(exc)[:240] or "Invalid backtest input"

    with store.transaction() as session:
        row = session.get_one(RunRow, claim.id)
        if row.status != "running" or row.lease_token != claim.lease_token:
            return
        if evidence is not None:
            save_evidence(session, claim.csv, evidence)
            session.flush()
            row.status, row.evidence_id = "completed", evidence.id
        else:
            row.status, row.error = "failed", error
        row.lease_token, row.lease_until = None, None


def jobs(store: Store) -> list[Job]:
    with Session(store.engine) as session:
        statement = (
            select(RunRow, PlanRow, EvidenceRow.summary)
            .select_from(RunRow)
            .join(PlanRow, RunRow.plan_id == PlanRow.id)
            .outerjoin(EvidenceRow, RunRow.evidence_id == EvidenceRow.id)
            .order_by(RunRow.queued_at.desc(), RunRow.id)
            .limit(50)
        )
        return [
            describe(row, plan, summary)
            for row, plan, summary in session.execute(statement)
        ]
