from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class State(StrEnum):
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    FILLED = "filled"
    REJECTED = "rejected"
    RATE_LIMITED = "rate_limited"
    ABANDONED = "abandoned"


PENDING = (State.SUBMITTING, State.ACCEPTED, State.PARTIAL)


class BrokerUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Order:
    key: str
    symbol: str
    quantity: int


@dataclass(frozen=True)
class Update:
    order: Order
    status: State
    filled: int


class BrokerAdapter(Protocol):
    def submit(self, order: Order) -> Update | None: ...
    def lookup(self, key: str) -> Update | None: ...
    def positions(self) -> dict[str, int]: ...


class OpenAlgoAdapter(BrokerAdapter):
    """Reserved adapter boundary with no transport or credentials in Phase 1."""

    def submit(self, order: Order) -> Update:
        raise BrokerUnavailable("OpenAlgo execution is disabled")

    def lookup(self, key: str) -> Update:
        raise BrokerUnavailable("OpenAlgo execution is disabled")

    def positions(self) -> dict[str, int]:
        raise BrokerUnavailable("OpenAlgo execution is disabled")
