from asyncio import Event, to_thread, wait_for
from datetime import UTC, datetime
from time import monotonic

from trading_platform.core.config import get_logger
from trading_platform.execution.store import Store
from trading_platform.operations.automation import claim_next, run_claim, schedule

log = get_logger(__name__)


async def run_worker(store: Store, stop: Event) -> None:
    next_schedule = 0.0
    while not stop.is_set():
        try:
            now = datetime.now(UTC)
            if monotonic() >= next_schedule:
                await to_thread(schedule, store, now)
                next_schedule = monotonic() + 15
            claim = await to_thread(claim_next, store, now)
            if claim is not None:
                await to_thread(run_claim, store, claim)
                continue
        except Exception as exc:
            # Database errors may include query parameters, including uploaded data.
            log.error(
                "Backtest worker error (%s); incomplete jobs are recovered by lease",
                type(exc).__name__,
            )
        try:
            await wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            continue
