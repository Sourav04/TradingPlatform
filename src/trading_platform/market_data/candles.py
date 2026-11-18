import csv
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_platform.core.models import utc

INDIA = ZoneInfo("Asia/Kolkata")
COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc(self.timestamp))
        prices = (self.open, self.high, self.low, self.close)
        if any(not price.is_finite() or price <= 0 for price in prices):
            raise ValueError("OHLC prices must be positive and finite")
        if self.low > min(self.open, self.close) or self.high < max(
            self.open, self.close
        ):
            raise ValueError("Open and close must lie between low and high")
        if self.volume < 0:
            raise ValueError("Volume must be nonnegative")


def read_candles(path: Path) -> Iterator[Candle]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        records = csv.DictReader(source, strict=True)
        fields = records.fieldnames or []
        if not set(COLUMNS).issubset(fields) or len(fields) != len(set(fields)):
            raise ValueError(
                "CSV requires unique timestamp, open, high, low, close, volume headers"
            )
        try:
            for row in records:
                try:
                    if None in row or any(row[field] is None for field in COLUMNS):
                        raise ValueError("Invalid number of columns")
                    candle = Candle(
                        datetime.fromisoformat(row["timestamp"]),
                        Decimal(row["open"]),
                        Decimal(row["high"]),
                        Decimal(row["low"]),
                        Decimal(row["close"]),
                        int(row["volume"]),
                    )
                except (ValueError, InvalidOperation) as exc:
                    raise ValueError(f"CSV line {records.line_num}: {exc}") from exc
                yield candle
        except csv.Error as exc:
            raise ValueError(f"CSV line {records.line_num}: {exc}") from exc
