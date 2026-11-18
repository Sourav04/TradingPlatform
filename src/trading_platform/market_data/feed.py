from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from trading_platform.core.models import Quote


class FeedUnavailable(RuntimeError):
    pass


class MarketData(Protocol):
    def latest(self, symbol: str) -> Quote: ...


class SimulatedFeed:
    def __init__(self) -> None:
        self.connected = True
        self._quotes: dict[str, Quote] = {}

    def publish(self, quote: Quote) -> None:
        previous = self._quotes.get(quote.symbol)
        if previous and quote.timestamp <= previous.timestamp:
            raise ValueError("Quotes must arrive in strictly increasing time order")
        self._quotes[quote.symbol] = quote

    def latest(self, symbol: str) -> Quote:
        if not self.connected or symbol not in self._quotes:
            raise FeedUnavailable(f"No connected feed for {symbol}")
        return self._quotes[symbol]


def fixture_quotes(
    prices: Iterable[int] = (100, 98, 96, 95, 97, 101, 104, 100, 95, 94),
) -> Iterator[Quote]:
    start = datetime(2025, 1, 6, 4, tzinfo=UTC)
    for index, price in enumerate(prices):
        yield Quote("NSE:DEMO", start + timedelta(minutes=index), Decimal(price))
