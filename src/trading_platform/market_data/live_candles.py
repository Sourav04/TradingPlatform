from dataclasses import replace
from datetime import time, timedelta

from trading_platform.core.models import Quote
from trading_platform.market_data.candles import INDIA, Candle

INTERVAL = timedelta(minutes=5)
FRESH = timedelta(seconds=30)


def session_quote(quote: Quote) -> bool:
    local = quote.timestamp.astimezone(INDIA)
    return local.weekday() < 5 and time(9, 15) <= local.time() < time(15, 30)


def update_candle(current: Candle | None, quote: Quote) -> Candle:
    local = quote.timestamp.astimezone(INDIA)
    start = local.replace(minute=local.minute // 5 * 5, second=0, microsecond=0)
    price = quote.price
    if current is None or current.timestamp != start:
        # LTP updates do not provide traded volume.
        return Candle(start, price, price, price, price, 0)
    return replace(
        current, high=max(current.high, price), low=min(current.low, price), close=price
    )
