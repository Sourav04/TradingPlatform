from collections.abc import Mapping
from decimal import Decimal


def gross_exposure(
    positions: Mapping[str, int], prices: Mapping[str, Decimal]
) -> Decimal:
    return sum(
        (abs(units) * prices[symbol] for symbol, units in positions.items() if units),
        Decimal(0),
    )
