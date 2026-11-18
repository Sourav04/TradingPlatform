from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import Pool

from trading_platform.brokers.contracts import BrokerUnavailable, Order, State
from trading_platform.brokers.fake import FakeBroker, Fault
from trading_platform.core.models import ManualClock, Quote, Signal
from trading_platform.execution.service import ExecutionService
from trading_platform.execution.store import BrokerOrder, Intent, Store
from trading_platform.market_data.feed import SimulatedFeed
from trading_platform.risk.checks import RiskDenied, RiskGate, RiskLimits


@pytest.fixture
def service(store: Store) -> ExecutionService:
    store.initialize()
    clock = ManualClock(datetime(2025, 1, 6, 4, tzinfo=UTC))
    feed = SimulatedFeed()
    return ExecutionService(
        store,
        FakeBroker(clock),
        RiskGate(
            clock,
            feed,
            RiskLimits(max_gross=Decimal(1500)),
            frozenset({"ema-crossover"}),
        ),
    )


def publish(service: ExecutionService, symbol: str = "A", target: int = 10) -> Signal:
    clock, feed = service.risk.clock, service.risk.feed
    assert isinstance(clock, ManualClock)
    assert isinstance(feed, SimulatedFeed)
    clock.current += timedelta(seconds=1)
    quote = Quote(symbol, clock.now(), Decimal(100))
    feed.publish(quote)
    return Signal("ema-crossover", quote, target)


def assert_orders(service: ExecutionService, count: int) -> None:
    with service.store.transaction() as session:
        assert len(session.scalars(select(Intent)).all()) == count
        assert len(session.scalars(select(BrokerOrder)).all()) == count


def test_duplicate_signal_concurrent_and_unchanged_target(
    service: ExecutionService,
) -> None:
    signal = publish(service)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(service.execute, [signal] * 4))
    assert all(result and result.key == signal.key for result in results)
    assert service.execute(publish(service)) is None
    assert service.reconcile()[1] == {"A": 10}
    assert_orders(service, 1)


def test_timeout_after_acceptance_and_new_process(service: ExecutionService) -> None:
    service.broker.fault = Fault.TIMEOUT
    signal = publish(service)
    result = service.execute(signal)
    assert result and result.status == State.SUBMITTING
    assert service.execute(signal) == result
    with pytest.raises(RiskDenied, match="reconciliation"):
        service.execute(publish(service, "B"))
    restarted_store = Store(
        service.store.engine.url.render_as_string(hide_password=False)
    )
    restarted = ExecutionService(
        restarted_store, FakeBroker(service.risk.clock), service.risk
    )
    orders, positions = restarted.reconcile()
    assert orders[0].status == State.FILLED
    assert positions == {"A": 10}
    assert_orders(service, 1)


def test_partial_fills_and_delayed_ack(service: ExecutionService) -> None:
    service.broker.fault = Fault.PARTIAL
    first = service.execute(publish(service))
    assert first and first.status == State.PARTIAL and first.filled == 5
    assert service.reconcile()[1] == {"A": 5}
    with pytest.raises(RiskDenied, match="reconciliation"):
        service.execute(publish(service, "B"))
    with service.store.transaction() as session:
        service.broker.complete(session, first.key)
    assert service.reconcile()[1] == {"A": 10}

    service.broker.fault = Fault.DELAY
    second = service.execute(publish(service, target=0))
    assert second and second.status == State.SUBMITTING
    assert service.reconcile()[0][0].status == State.SUBMITTING
    clock = service.risk.clock
    assert isinstance(clock, ManualClock)
    clock.current += timedelta(seconds=30)
    assert service.reconcile()[0][0].status == State.ACCEPTED
    with service.store.transaction() as session:
        service.broker.complete(session, second.key)
    assert service.reconcile()[1] == {}


@pytest.mark.parametrize(
    ("fault", "expected"),
    [(Fault.RATE_LIMIT, State.RATE_LIMITED), (Fault.REJECT, State.REJECTED)],
)
def test_broker_negative_ack_is_terminal(
    service: ExecutionService, fault: Fault, expected: State
) -> None:
    service.broker.fault = fault
    signal = publish(service)
    result = service.execute(signal)
    assert result and result.status == expected
    assert service.execute(signal) == result
    assert service.execute(publish(service)) is None
    assert service.reconcile()[1] == {}
    assert_orders(service, 1)


def test_no_orders_on_unavailable_or_stale_data(service: ExecutionService) -> None:
    signal = publish(service)
    clock, feed = service.risk.clock, service.risk.feed
    assert isinstance(clock, ManualClock)
    assert isinstance(feed, SimulatedFeed)
    clock.current += timedelta(minutes=3)
    with pytest.raises(RiskDenied, match="stale"):
        service.execute(signal)
    feed.connected = False
    with pytest.raises(RiskDenied, match="unavailable"):
        service.execute(signal)
    assert_orders(service, 0)


def test_unauthorized_and_portfolio_exposure(service: ExecutionService) -> None:
    signal = publish(service)
    with pytest.raises(RiskDenied, match="unauthorized"):
        service.execute(Signal("unknown", signal.quote, 10))
    assert service.execute(signal)
    with pytest.raises(RiskDenied, match="portfolio"):
        service.execute(publish(service, "B"))
    assert_orders(service, 1)


def test_database_failure_prevents_execution(service: ExecutionService) -> None:
    signal = publish(service)

    def disconnect(*_: Pool) -> None:
        raise OperationalError("test checkout failure", None, RuntimeError())

    event.listen(service.store.engine, "checkout", disconnect)
    try:
        with pytest.raises(OperationalError):
            service.execute(signal)
    finally:
        event.remove(service.store.engine, "checkout", disconnect)
    assert_orders(service, 0)


def test_kill_switch_recovery_after_crash_between_claim_and_submit(
    service: ExecutionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    signal = publish(service)
    original = service.broker.submit

    def interrupt(*_: object) -> None:
        raise BrokerUnavailable("simulated process interruption")

    monkeypatch.setattr(service.broker, "submit", interrupt)
    with pytest.raises(BrokerUnavailable, match="interruption"):
        service.execute(signal)
    monkeypatch.setattr(service.broker, "submit", original)
    service.set_kill_switch(True)
    with pytest.raises(RiskDenied, match="Resolve"):
        service.set_kill_switch(False)
    assert service.recover_unsubmitted()[0].status == State.ABANDONED
    with pytest.raises(RiskDenied, match="halted"):
        service.execute(publish(service, target=8))
    service.set_kill_switch(False)
    result = service.execute(publish(service, target=8))
    assert result and result.status == State.FILLED
    previous = service.execute(signal)
    assert previous and previous.status == State.ABANDONED


def test_broker_outage_and_position_disagreement(service: ExecutionService) -> None:
    signal = publish(service)
    service.broker.fault = Fault.OFFLINE
    with pytest.raises(BrokerUnavailable):
        service.execute(signal)
    assert_orders(service, 0)
    service.broker.fault = Fault.NONE
    assert service.execute(signal)
    with service.store.transaction() as session:
        session.get_one(BrokerOrder, signal.key).filled = 9
    with pytest.raises(RiskDenied, match="differ"):
        service.execute(publish(service, "B"))
    with pytest.raises(BrokerUnavailable, match="differ"):
        service.reconcile()


def test_fake_broker_refuses_orders_without_durable_claim(
    service: ExecutionService,
) -> None:
    with service.store.transaction() as session:
        with pytest.raises(BrokerUnavailable, match="authorized"):
            service.broker.submit(session, Order("unknown", "A", 10))
    assert_orders(service, 0)
