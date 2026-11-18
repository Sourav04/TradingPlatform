from decimal import Decimal
from typing import Protocol

from trading_platform.core.models import Quote, Signal


class Strategy(Protocol):
    def on_quote(self, quote: Quote) -> Signal | None: ...


class EMACrossover:
    name = "ema-crossover"

    def __init__(self, fast: int = 2, slow: int = 4, size: int = 10) -> None:
        if not 0 < fast < slow or size <= 0:
            raise ValueError("Expected 0 < fast < slow and a positive position size")
        self._fast_alpha = Decimal(2) / (fast + 1)
        self._slow_alpha = Decimal(2) / (slow + 1)
        self._warmup = slow
        self._size = size
        self._series: dict[str, tuple[Quote, Decimal, Decimal, int]] = {}

    def on_quote(self, quote: Quote) -> Signal | None:
        previous = self._series.get(quote.symbol)
        if previous is None:
            self._series[quote.symbol] = (quote, quote.price, quote.price, 1)
            return None
        last_quote, fast, slow, count = previous
        if quote.timestamp <= last_quote.timestamp:
            raise ValueError("EMA requires strictly ordered quotes")
        next_fast = fast + (quote.price - fast) * self._fast_alpha
        next_slow = slow + (quote.price - slow) * self._slow_alpha
        self._series[quote.symbol] = (quote, next_fast, next_slow, count + 1)
        if count + 1 < self._warmup:
            return None
        if fast <= slow and next_fast > next_slow:
            return Signal(self.name, quote, self._size)
        if fast >= slow and next_fast < next_slow:
            return Signal(self.name, quote, 0)
        return None
