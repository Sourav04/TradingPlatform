from sqlalchemy import select

from trading_platform.brokers.contracts import PENDING
from trading_platform.execution.store import (
    BrokerOrder,
    Control,
    Intent,
    Store,
    net_positions,
)


def readiness(store: Store) -> dict[str, str | bool]:
    with store.transaction() as session:
        killed = session.get_one(Control, 1).killed
        pending = session.scalar(
            select(Intent.key).where(Intent.status.in_(PENDING)).limit(1)
        )
        mismatch = net_positions(session, Intent) != net_positions(session, BrokerOrder)
    return {
        "status": "blocked" if killed or pending or mismatch else "ready",
        "mode": "simulation",
        "live_trading": False,
        "kill_switch": killed,
        "reconciliation_required": bool(pending or mismatch),
    }
