from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Protocol


def utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Timezone-aware timestamp required")
    return value.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class ManualClock:
    current: datetime

    def now(self) -> datetime:
        return utc(self.current)


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: datetime
    price: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc(self.timestamp))
        if not self.symbol or not self.price.is_finite() or self.price <= 0:
            raise ValueError("Invalid quote")


@dataclass(frozen=True)
class Signal:
    strategy: str
    quote: Quote
    target: int

    @property
    def key(self) -> str:
        identity = (
            self.strategy,
            self.quote.symbol,
            self.quote.timestamp.isoformat(),
            self.target,
        )
        return sha256(repr(identity).encode()).hexdigest()
