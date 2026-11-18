from dataclasses import dataclass

from sqlalchemy import select

from trading_platform.brokers.contracts import (
    PENDING,
    BrokerUnavailable,
    Order,
    State,
    Update,
)
from trading_platform.brokers.fake import FakeBroker
from trading_platform.core.config import get_logger
from trading_platform.core.models import Signal
from trading_platform.execution.store import (
    Control,
    DesiredPosition,
    Intent,
    Store,
    net_positions,
)
from trading_platform.risk.checks import RiskDenied, RiskGate

log = get_logger(__name__)


@dataclass(frozen=True)
class Result:
    key: str
    symbol: str
    quantity: int
    target: int
    status: State
    filled: int


def result(intent: Intent) -> Result:
    return Result(
        intent.key,
        intent.symbol,
        intent.quantity,
        intent.target,
        State(intent.status),
        intent.filled,
    )


def apply_update(intent: Intent, update: Update) -> None:
    quantity, filled = intent.quantity, update.filled
    no_fill = (State.ACCEPTED, State.REJECTED, State.RATE_LIMITED)
    invalid_fill = (
        (update.status in no_fill and filled != 0)
        or (update.status == State.FILLED and filled != quantity)
        or (update.status == State.PARTIAL and not 0 < abs(filled) < abs(quantity))
    )
    if (
        update.order != Order(intent.key, intent.symbol, quantity)
        or update.status in (State.SUBMITTING, State.ABANDONED)
        or invalid_fill
        or filled * quantity < 0
        or abs(filled) < abs(intent.filled)
        or (
            intent.status == State.PARTIAL
            and update.status not in (State.PARTIAL, State.FILLED)
        )
    ):
        raise BrokerUnavailable("Inconsistent broker update")
    intent.status, intent.filled = update.status, filled


class ExecutionService:
    def __init__(self, store: Store, broker: FakeBroker, risk: RiskGate) -> None:
        self.store = store
        self.broker = broker
        self.risk = risk

    def execute(self, signal: Signal) -> Result | None:
        with self.store.transaction() as session:
            existing = session.get(Intent, signal.key)
            if existing is not None:
                return result(existing)
            control = session.get_one(Control, 1)
            if session.scalar(
                select(Intent.key).where(Intent.status.in_(PENDING)).limit(1)
            ):
                raise RiskDenied("An order requires reconciliation")
            positions = net_positions(session, Intent)
            if positions != self.broker.positions(session):
                raise RiskDenied("Broker positions differ from recorded fills")
            quantity = self.risk.check(signal, positions, control.killed)
            symbol = signal.quote.symbol
            timestamp = signal.quote.timestamp.isoformat(timespec="microseconds")
            desired = session.get(DesiredPosition, symbol)
            if desired is not None:
                if timestamp <= desired.timestamp:
                    raise RiskDenied(
                        "A newer or simultaneous target is already recorded"
                    )
                if signal.target == desired.target:
                    desired.timestamp = timestamp
                    return None
            session.merge(
                DesiredPosition(
                    symbol=symbol, timestamp=timestamp, target=signal.target
                )
            )
            if quantity == 0:
                return None
            session.add(
                Intent(
                    key=signal.key,
                    strategy=signal.strategy,
                    symbol=symbol,
                    target=signal.target,
                    quantity=quantity,
                    filled=0,
                    status=State.SUBMITTING,
                )
            )

        # Claim is durable. Only the caller that created it may dispatch it.
        # The fake broker shares the database so the final kill/risk checks and
        # broker acceptance are serialized with control changes and other orders.
        with self.store.transaction() as session:
            intent = session.get_one(Intent, signal.key)
            if intent.status != State.SUBMITTING:
                return result(intent)
            control = session.get_one(Control, 1)
            if control.killed:
                intent.status = State.ABANDONED
                return result(intent)
            positions = net_positions(session, Intent)
            if positions != self.broker.positions(session):
                raise RiskDenied("Broker positions changed before dispatch")
            self.risk.check(signal, positions, control.killed)
            update = self.broker.submit(
                session, Order(intent.key, intent.symbol, quantity)
            )
            if update is not None:
                apply_update(intent, update)
            else:
                log.warning("Awaiting broker reconciliation for intent %s", intent.key)
            return result(intent)

    def reconcile(self) -> tuple[list[Result], dict[str, int]]:
        with self.store.transaction() as session:
            orders = session.scalars(
                select(Intent).where(Intent.status.in_(PENDING))
            ).all()
            for intent in orders:
                update = self.broker.lookup(session, intent.key)
                if update is not None:
                    apply_update(intent, update)
            session.flush()
            positions = net_positions(session, Intent)
            if positions != self.broker.positions(session):
                raise BrokerUnavailable("Broker positions differ from recorded fills")
            return [result(intent) for intent in orders], positions

    def recover_unsubmitted(self) -> list[Result]:
        with self.store.transaction() as session:
            if not session.get_one(Control, 1).killed:
                raise RiskDenied(
                    "Enable kill switch before recovering unsubmitted intents"
                )
            self.broker.positions(session)
            intents = session.scalars(
                select(Intent).where(Intent.status == State.SUBMITTING)
            )
            recovered = []
            for intent in intents:
                if not self.broker.has_record(session, intent.key):
                    intent.status = State.ABANDONED
                    recovered.append(result(intent))
            return recovered

    def set_kill_switch(self, enabled: bool) -> None:
        with self.store.transaction() as session:
            control = session.get_one(Control, 1)
            if not enabled:
                pending = session.scalar(
                    select(Intent.key).where(Intent.status.in_(PENDING)).limit(1)
                )
                if pending or net_positions(session, Intent) != self.broker.positions(
                    session
                ):
                    raise RiskDenied("Resolve broker state before clearing kill switch")
            control.killed = enabled
            log.info("Simulation kill switch set to %s", enabled)
