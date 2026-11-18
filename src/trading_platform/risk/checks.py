from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from trading_platform.core.models import Clock, Signal
from trading_platform.market_data.feed import FeedUnavailable, MarketData
from trading_platform.portfolio.exposure import gross_exposure


class RiskDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class RiskLimits:
    max_units: int = 100
    max_gross: Decimal = Decimal("100000")
    quote_age: timedelta = timedelta(minutes=2)


class RiskGate:
    def __init__(
        self,
        clock: Clock,
        feed: MarketData,
        limits: RiskLimits,
        strategies: frozenset[str],
    ) -> None:
        self.clock = clock
        self.feed = feed
        self.limits = limits
        self.strategies = strategies

    def check(self, signal: Signal, positions: dict[str, int], killed: bool) -> int:
        if killed or signal.strategy not in self.strategies:
            raise RiskDenied("Trading halted or strategy unauthorized")
        symbol = signal.quote.symbol
        if not 0 <= signal.target <= self.limits.max_units:
            raise RiskDenied("Position size outside limits")
        projected = {**positions, symbol: signal.target}
        try:
            now = self.clock.now()
            latest = self.feed.latest(symbol)
            if latest.timestamp < signal.quote.timestamp or (
                latest.timestamp == signal.quote.timestamp and latest != signal.quote
            ):
                raise RiskDenied("Signal quote disagrees with the current feed")
            quotes = {
                key: self.feed.latest(key)
                for key, units in projected.items()
                if key != symbol and units
            }
            quotes[symbol] = latest
            for observation in [signal.quote, *quotes.values()]:
                if (
                    not timedelta(0)
                    <= now - observation.timestamp
                    <= self.limits.quote_age
                ):
                    raise RiskDenied("Market data stale or future-dated")
            prices = {key: observation.price for key, observation in quotes.items()}
            if gross_exposure(projected, prices) > self.limits.max_gross:
                raise RiskDenied("Gross portfolio exposure limit exceeded")
        except FeedUnavailable as exc:
            raise RiskDenied("Market data unavailable") from exc
        return signal.target - positions.get(symbol, 0)
