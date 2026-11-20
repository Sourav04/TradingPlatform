from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_platform.backtesting.historical import Fill, Settings, priced_fill
from trading_platform.core.models import ManualClock, Quote, Signal, utc
from trading_platform.execution.store import Control, Store
from trading_platform.market_data.candles import INDIA, Candle
from trading_platform.market_data.feed import SimulatedFeed
from trading_platform.market_data.live_candles import (
    FRESH,
    INTERVAL,
    session_quote,
    update_candle,
)
from trading_platform.operations.session_report import Session as PaperSession
from trading_platform.operations.session_report import record_session
from trading_platform.operations.workspace import (
    CONFIG,
    ApprovalError,
    EvidenceRow,
    Paper,
    PaperAudit,
    StreamPaperRow,
    approval_evidence,
    parse_csv,
)
from trading_platform.risk.checks import RiskDenied, RiskGate
from trading_platform.strategies.catalog import make_strategy
from trading_platform.strategies.ema import EMACrossover, EMAState


@dataclass(frozen=True)
class StreamState:
    paper: Paper
    candle: Candle | None = None
    ema: EMAState | None = None
    last_tick: Quote | None = None
    observed_at: datetime | None = None
    disconnected: bool = True
    complete_candidate: bool = False
    daily_start: Decimal = Decimal(0)
    daily_halted: bool = False
    sessions: tuple[PaperSession, ...] = ()
    history_started_late: bool = False


@dataclass(frozen=True)
class StreamView:
    paper: Paper
    feed_status: str
    last_tick_at: datetime | None
    candle: Candle | None
    interval_minutes: int = 5
    volume_available: bool = False


STATE = TypeAdapter(StreamState)


def fresh(state: StreamState, now: datetime) -> bool:
    return (
        not state.disconnected
        and state.last_tick is not None
        and timedelta(0) <= now - state.last_tick.timestamp <= FRESH
    )


def view(state: StreamState, now: datetime) -> StreamView:
    status = (
        "offline" if state.disconnected else "fresh" if fresh(state, now) else "stale"
    )
    return StreamView(
        state.paper,
        status,
        state.last_tick.timestamp if state.last_tick else None,
        state.candle,
    )


def stream_overview(store: Store, now: datetime) -> list[StreamView]:
    with Session(store.engine) as session:
        records = (
            select(StreamPaperRow.snapshot)
            .order_by(StreamPaperRow.approved_at.desc())
            .limit(25)
        )
        return [
            view(STATE.validate_json(value), now) for value in session.scalars(records)
        ]


def approve_stream(store: Store, evidence_id: str, now: datetime) -> StreamView:
    with store.transaction() as session:
        evidence = approval_evidence(session, evidence_id)
        settings = evidence.settings
        if evidence.dataset == "Synthetic sample":
            raise ApprovalError(
                "Register a real dataset before approving streaming paper"
            )
        require_ema(settings)
        candles = [
            candle
            for candle in parse_csv(session.get_one(EvidenceRow, evidence_id).csv)
            if settings.start
            <= candle.timestamp.astimezone(INDIA).date()
            <= settings.end
        ]
        if not any(
            later.timestamp - earlier.timestamp == INTERVAL
            for earlier, later in zip(candles, candles[1:])
        ) or any(
            candle.timestamp.minute % 5
            or candle.timestamp.second
            or candle.timestamp.microsecond
            or not session_quote(Quote(settings.symbol, candle.timestamp, candle.open))
            for candle in candles
        ):
            raise ApprovalError(
                "Streaming paper requires evidence built from regular-session five-minute candles"
            )
        stamp = now.isoformat()
        paper = Paper(
            uuid4().hex,
            evidence_id,
            stamp,
            settings.strategy_id,
            settings.strategy_version,
            settings.symbol,
            "active",
            0,
            0,
            0,
            settings.initial_cash,
            settings.initial_cash,
            Decimal(0),
            Decimal(0),
            0,
            None,
            None,
            [],
            [],
            [PaperAudit("approved streaming paper", stamp)],
        )
        state = StreamState(paper, daily_start=settings.initial_cash)
        session.add(
            StreamPaperRow(
                id=paper.id,
                evidence_id=evidence_id,
                symbol=paper.symbol,
                status=paper.status,
                snapshot=STATE.dump_json(state).decode(),
                approved_at=stamp,
            )
        )
        return view(state, now)


def require_ema(settings: Settings) -> EMACrossover:
    try:
        strategy = make_strategy(
            settings.strategy_id,
            settings.strategy_version,
            settings.fast,
            settings.slow,
            settings.units,
        )
    except ValueError as exc:
        raise ApprovalError(str(exc)) from exc
    if not isinstance(strategy, EMACrossover):
        raise ApprovalError("Strategy is unavailable for streaming paper")
    return strategy


def fill_at(
    tick: Quote,
    when: datetime,
    quantity: int,
    settings: Settings,
    reason: str,
    stop: Decimal | None,
) -> Fill:
    price = tick.price
    bar = Candle(tick.timestamp, price, price, price, price, 0)
    return priced_fill(bar, when, quantity, settings.costs, price, reason, stop)


def apply_fill(paper: Paper, fill: Fill, settings: Settings) -> Paper:
    cash = paper.cash + fill.cash_change
    units = paper.units + fill.units
    realized = paper.realized if units else cash - settings.initial_cash
    return replace(
        paper,
        cash=cash,
        units=units,
        stop=fill.stop_price if units else None,
        realized=realized,
        unrealized=Decimal(0),
        fills=[*paper.fills, fill],
    )


def execute_signal(
    paper: Paper,
    tick: Quote,
    settings: Settings,
    signal: Signal,
    blocked: bool,
    daily_halted: bool,
) -> Paper:
    if signal.target == paper.units:
        return paper
    if not signal.target and paper.units:
        return apply_fill(
            paper,
            fill_at(
                tick, signal.quote.timestamp, -paper.units, settings, "ema", paper.stop
            ),
            settings,
        )
    stop_percent, budget_percent = settings.stop_loss_percent, settings.max_risk_percent
    if stop_percent is None or budget_percent is None:
        return replace(
            paper, rejections=[*paper.rejections, "Missing protective risk settings"]
        )
    feed = SimulatedFeed()
    feed.publish(tick)
    gate = RiskGate(
        ManualClock(tick.timestamp),
        feed,
        replace(settings.limits, quote_age=FRESH),
        frozenset({settings.strategy_id}),
    )
    try:
        if daily_halted:
            raise RiskDenied("Daily loss limit reached")
        quantity = gate.check(signal, {tick.symbol: paper.units}, killed=blocked)
        trigger = tick.price * (1 - stop_percent / 100)
        entry = fill_at(
            tick, signal.quote.timestamp, quantity, settings, "ema", trigger
        )
        exit_quote = replace(tick, price=trigger)
        estimate = fill_at(
            exit_quote,
            signal.quote.timestamp,
            -quantity,
            settings,
            "stop_loss",
            trigger,
        )
        if (
            -(entry.cash_change + estimate.cash_change)
            > paper.cash * budget_percent / 100
        ):
            raise RiskDenied("Estimated loss at the stop exceeds the risk budget")
        if paper.cash + entry.cash_change < 0:
            raise RiskDenied("Insufficient cash including costs")
        return apply_fill(paper, entry, settings)
    except RiskDenied as exc:
        return replace(paper, rejections=[*paper.rejections, str(exc)])


def process_tick(
    state: StreamState, tick: Quote, now: datetime, settings: Settings, killed: bool
) -> StreamState:
    now = utc(now)
    previous = state.last_tick
    if (
        tick.symbol != state.paper.symbol
        or tick.timestamp < datetime.fromisoformat(state.paper.approved_at)
        or not session_quote(tick)
        or not timedelta(0) <= now - tick.timestamp <= FRESH
        or (previous is not None and tick.timestamp <= previous.timestamp)
    ):
        return state

    paper = state.paper
    local = tick.timestamp.astimezone(INDIA)
    first_today = (
        previous is None or previous.timestamp.astimezone(INDIA).date() != local.date()
    )
    daily_start = paper.equity if first_today else state.daily_start
    daily_halted = False if first_today else state.daily_halted
    continuous = (
        previous is not None
        and not state.disconnected
        and tick.timestamp - previous.timestamp <= FRESH
        and not first_today
    )
    candle = update_candle(state.candle if continuous else None, tick)
    rollover = state.candle is not None and state.candle.timestamp != candle.timestamp
    complete = (
        continuous
        and rollover
        and state.complete_candidate
        and state.candle is not None
        and candle.timestamp - state.candle.timestamp == INTERVAL
    )
    ema = state.ema if continuous else None
    signal = None
    if complete and state.candle is not None and previous is not None:
        strategy = require_ema(settings)
        if ema is not None:
            strategy.restore(ema)
        signal = strategy.on_quote(
            Quote(tick.symbol, previous.timestamp, state.candle.close)
        )
        ema = strategy.checkpoint(tick.symbol)
        paper = replace(paper, cursor=paper.cursor + 1)

    closing_window = local.time() >= time(15, 25)
    exited = False
    if paper.units and paper.stop is not None and tick.price <= paper.stop:
        when = paper.fills[-1].signal_bar
        paper = apply_fill(
            paper,
            fill_at(tick, when, -paper.units, settings, "stop_loss", paper.stop),
            settings,
        )
        exited = True
    elif paper.units and (closing_window or first_today):
        paper = apply_fill(
            paper,
            fill_at(
                tick,
                tick.timestamp,
                -paper.units,
                settings,
                "session_close",
                paper.stop,
            ),
            settings,
        )
        exited = True
    daily_percent = settings.max_daily_loss_percent
    if daily_percent is not None:
        daily_halted = (
            daily_halted
            or paper.cash + paper.units * tick.price
            <= daily_start * (1 - daily_percent / 100)
        )
    if signal is not None and not exited and not closing_window:
        paper = execute_signal(
            paper,
            tick,
            settings,
            signal,
            killed or paper.status == "paused",
            daily_halted,
        )
    value = paper.cash + paper.units * tick.price
    unrealized = (
        value - paper.cash + paper.fills[-1].cash_change if paper.units else Decimal(0)
    )
    paper = replace(paper, equity=value, unrealized=unrealized, mark=tick.price)
    candidate = continuous and (rollover or state.complete_candidate)
    return StreamState(
        paper,
        candle,
        ema,
        tick,
        now,
        False,
        candidate,
        daily_start,
        daily_halted,
        record_session(state.sessions, state.paper, paper, tick.timestamp),
        state.history_started_late
        or (not state.sessions and state.last_tick is not None),
    )


def ingest(store: Store, quote: Quote, observed_at: datetime) -> None:
    with store.transaction() as session:
        row = session.scalar(
            select(StreamPaperRow).where(
                StreamPaperRow.symbol == quote.symbol,
                StreamPaperRow.status != "closed",
            )
        )
        if row is None:
            return
        settings = CONFIG.validate_json(
            session.get_one(EvidenceRow, row.evidence_id).settings
        )
        state = STATE.validate_json(row.snapshot)
        updated = process_tick(
            state, quote, observed_at, settings, session.get_one(Control, 1).killed
        )
        if updated != state:
            row.snapshot = STATE.dump_json(updated).decode()


def disconnect(store: Store, symbol: str) -> None:
    with store.transaction() as session:
        row = session.scalar(
            select(StreamPaperRow).where(
                StreamPaperRow.symbol == symbol,
                StreamPaperRow.status != "closed",
            )
        )
        if row is not None:
            state = STATE.validate_json(row.snapshot)
            row.snapshot = STATE.dump_json(replace(state, disconnected=True)).decode()


def stream_control(
    store: Store, paper_id: str, revision: int, action: str, now: datetime
) -> StreamView:
    with store.transaction() as session:
        row = session.get(StreamPaperRow, paper_id)
        if row is None:
            raise ApprovalError("Streaming paper deployment not found")
        state = STATE.validate_json(row.snapshot)
        paper = state.paper
        if paper.revision != revision or paper.status == "closed":
            raise ApprovalError("Streaming paper changed. Refresh and try again.")
        if action == "pause" and paper.status == "active":
            status = "paused"
        elif (
            action == "resume"
            and paper.status == "paused"
            and not session.get_one(Control, 1).killed
        ):
            status = "active"
        elif action == "close":
            if paper.units:
                if not fresh(state, now) or state.last_tick is None:
                    raise ApprovalError(
                        "Wait for a fresh quote before closing an open paper position"
                    )
                settings = CONFIG.validate_json(
                    session.get_one(EvidenceRow, row.evidence_id).settings
                )
                fill = fill_at(
                    state.last_tick,
                    now,
                    -paper.units,
                    settings,
                    "paper_close",
                    paper.stop,
                )
                paper = apply_fill(paper, fill, settings)
                paper = replace(paper, equity=paper.cash)
            status = "closed"
        else:
            raise ApprovalError(
                "That action is unavailable in the current deployment state"
            )
        audit = [*paper.audit, PaperAudit(action, now.isoformat())]
        paper = replace(paper, status=status, revision=paper.revision + 1, audit=audit)
        sessions = state.sessions
        if len(paper.fills) > len(state.paper.fills):
            sessions = record_session(
                sessions, state.paper, paper, paper.fills[-1].fill_bar
            )
        state = replace(
            state,
            paper=paper,
            sessions=sessions,
            history_started_late=state.history_started_late
            or (not state.sessions and state.last_tick is not None),
        )
        row.status, row.snapshot = status, STATE.dump_json(state).decode()
        return view(state, now)
