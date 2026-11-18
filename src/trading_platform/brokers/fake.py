from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy.orm import Session

from trading_platform.brokers.contracts import BrokerUnavailable, Order, State, Update
from trading_platform.core.models import Clock
from trading_platform.execution.store import BrokerOrder, Control, Intent, net_positions


class Fault(StrEnum):
    NONE = "none"
    REJECT = "reject"
    TIMEOUT = "timeout"
    PARTIAL = "partial"
    DELAY = "delay"
    RATE_LIMIT = "rate_limit"
    OFFLINE = "offline"


class FakeBroker:
    def __init__(self, clock: Clock, fault: Fault = Fault.NONE) -> None:
        self.clock = clock
        self.fault = fault

    def _available(self) -> None:
        if self.fault == Fault.OFFLINE:
            raise BrokerUnavailable("Fake broker offline")

    @staticmethod
    def _update(row: BrokerOrder) -> Update:
        return Update(
            Order(row.key, row.symbol, row.quantity), State(row.status), row.filled
        )

    def submit(self, session: Session, order: Order) -> Update | None:
        self._available()
        claim = session.get(Intent, order.key)
        if (
            claim is None
            or claim.status != State.SUBMITTING
            or claim.symbol != order.symbol
            or claim.quantity != order.quantity
            or session.get_one(Control, 1).killed
        ):
            raise BrokerUnavailable("No authorized simulated order claim")
        row = session.get(BrokerOrder, order.key)
        if row is not None:
            if self._update(row).order != order:
                raise BrokerUnavailable("Broker idempotency key collision")
            return self.lookup(session, order.key)
        status = State.FILLED
        filled = order.quantity
        visible = self.clock.now()
        if self.fault == Fault.REJECT:
            status, filled = State.REJECTED, 0
        elif self.fault == Fault.RATE_LIMIT:
            status, filled = State.RATE_LIMITED, 0
        elif self.fault == Fault.PARTIAL:
            filled = (abs(order.quantity) // 2) * (1 if order.quantity > 0 else -1)
            status = State.PARTIAL if filled else State.ACCEPTED
        elif self.fault == Fault.DELAY:
            status, filled = State.ACCEPTED, 0
            visible += timedelta(seconds=30)
        row = BrokerOrder(
            key=order.key,
            symbol=order.symbol,
            quantity=order.quantity,
            filled=filled,
            status=status,
            visible_at=visible.isoformat(),
        )
        session.add(row)
        if self.fault in (Fault.TIMEOUT, Fault.DELAY):
            return None
        return self._update(row)

    def lookup(self, session: Session, key: str) -> Update | None:
        self._available()
        row = session.get(BrokerOrder, key)
        if row is None or self.clock.now() < datetime.fromisoformat(row.visible_at):
            return None
        return self._update(row)

    def has_record(self, session: Session, key: str) -> bool:
        self._available()
        return session.get(BrokerOrder, key) is not None

    def positions(self, session: Session) -> dict[str, int]:
        self._available()
        return net_positions(session, BrokerOrder)

    def complete(self, session: Session, key: str) -> None:
        update = self.lookup(session, key)
        if update is None or update.status not in (State.ACCEPTED, State.PARTIAL):
            raise ValueError("Order is not acknowledged and open")
        row = session.get_one(BrokerOrder, key)
        row.status, row.filled = State.FILLED, row.quantity
