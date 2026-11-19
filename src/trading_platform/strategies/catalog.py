from dataclasses import dataclass

from trading_platform.strategies.ema import EMACrossover, Strategy


@dataclass(frozen=True)
class StrategyInfo:
    id: str
    version: str
    name: str
    description: str
    source: str


CATALOG = (
    StrategyInfo(
        EMACrossover.name,
        "1",
        "EMA crossover",
        "Long-only fast and slow EMA crossover with protective exits.",
        "trading_platform.strategies.ema",
    ),
)


def make_strategy(
    strategy_id: str, version: str, fast: int, slow: int, units: int
) -> Strategy:
    if (strategy_id, version) != (CATALOG[0].id, CATALOG[0].version):
        raise ValueError("Strategy version is unavailable")
    return EMACrossover(fast, slow, units)
