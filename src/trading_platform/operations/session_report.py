from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from trading_platform.market_data.candles import INDIA
from trading_platform.operations.workspace import Paper

ZERO = Decimal(0)


@dataclass(frozen=True)
class Session:
    date: date
    as_of: datetime
    opening_equity: Decimal
    equity: Decimal
    opening_realized: Decimal
    opening_unrealized: Decimal
    realized: Decimal
    unrealized: Decimal
    fills: int
    peak: Decimal
    drawdown_percent: Decimal
    deployment_peak: Decimal
    deployment_drawdown_percent: Decimal

    @property
    def pnl(self) -> Decimal:
        return self.equity - self.opening_equity


def record_session(
    sessions: tuple[Session, ...], before: Paper, after: Paper, stamp: datetime
) -> tuple[Session, ...]:
    day = stamp.astimezone(INDIA).date()
    previous = sessions[-1] if sessions else None
    same = previous is not None and previous.date == day
    if previous is not None and same:
        baseline = previous
    else:
        baseline = Session(
            day,
            stamp,
            before.equity,
            before.equity,
            before.realized,
            before.unrealized,
            ZERO,
            ZERO,
            0,
            before.equity,
            ZERO,
            previous.deployment_peak if previous else before.equity,
            previous.deployment_drawdown_percent if previous else ZERO,
        )
    peak = max(baseline.peak, after.equity)
    drawdown = (peak - after.equity) / peak * 100 if peak else ZERO
    deployment_peak = max(baseline.deployment_peak, after.equity)
    deployment_drawdown = (
        (deployment_peak - after.equity) / deployment_peak * 100
        if deployment_peak
        else ZERO
    )
    updated = replace(
        baseline,
        as_of=stamp,
        equity=after.equity,
        realized=after.realized - baseline.opening_realized,
        unrealized=after.unrealized - baseline.opening_unrealized,
        fills=baseline.fills + len(after.fills) - len(before.fills),
        peak=peak,
        drawdown_percent=max(baseline.drawdown_percent, drawdown),
        deployment_peak=deployment_peak,
        deployment_drawdown_percent=max(
            baseline.deployment_drawdown_percent, deployment_drawdown
        ),
    )
    return (*sessions[:-1], updated) if same else (*sessions, updated)
