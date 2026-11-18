from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_platform.core.models import ManualClock, Quote, Signal
from trading_platform.execution.service import ExecutionService, Result
from trading_platform.market_data.feed import SimulatedFeed
from trading_platform.strategies.ema import Strategy


@dataclass(frozen=True)
class Costs:
    fee_bps: Decimal = Decimal(5)
    slippage_bps: Decimal = Decimal(10)

    def estimate(self, quantity: int, price: Decimal) -> Decimal:
        return abs(quantity) * price * (self.fee_bps + self.slippage_bps) / 10_000


def ordered_replay(quotes: Iterable[Quote]) -> Iterable[Quote]:
    previous = None
    for quote in quotes:
        if previous is not None and quote.timestamp < previous:
            raise ValueError("Historical quotes must arrive in chronological order")
        previous = quote.timestamp
        yield quote


@dataclass(frozen=True)
class SimulatedFill:
    order: Result
    quote_time: datetime
    estimated_cost_inr: str


def run_replay(
    quotes: Iterable[Quote],
    strategy: Strategy,
    feed: SimulatedFeed,
    clock: ManualClock,
    execution: ExecutionService,
    costs: Costs = Costs(),
) -> tuple[list[Signal], list[SimulatedFill]]:
    signals: list[Signal] = []
    orders: list[SimulatedFill] = []
    pending: dict[str, Signal] = {}
    for quote in ordered_replay(quotes):
        clock.current = quote.timestamp
        feed.publish(quote)
        previous = pending.pop(quote.symbol, None)
        if previous is not None:
            result = execution.execute(previous)
            if result is not None:
                cost = costs.estimate(result.filled, quote.price)
                orders.append(SimulatedFill(result, quote.timestamp, str(cost)))
        signal = strategy.on_quote(quote)
        if signal is not None:
            signals.append(signal)
            pending[quote.symbol] = signal
    return signals, orders
