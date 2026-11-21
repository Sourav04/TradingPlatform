from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.api.performance import summarize
from trading_platform.backtesting.historical import Settings, backtest
from trading_platform.core.models import Quote
from trading_platform.execution.store import Store
from trading_platform.operations.session_report import record_session
from trading_platform.operations.stream_paper import (
    STATE,
    StreamState,
    approve_stream,
    ingest,
)
from trading_platform.operations.workspace import (
    EVIDENCE,
    Paper,
    evidence_for,
    parse_csv,
)

START = datetime(2025, 1, 6, 3, 45, tzinfo=UTC)
SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ema-candles.csv"


def create_evidence(store: Store) -> tuple[str, str]:
    header, *rows = SAMPLE.read_text().splitlines()
    csv = (
        header
        + "\n"
        + "\n".join(
            (START + timedelta(minutes=5 * index)).isoformat()
            + ","
            + row.split(",", 1)[1]
            for index, row in enumerate(rows)
        )
    )
    settings = Settings(
        "NSE:DEMO",
        START.date(),
        START.date(),
        stop_loss_percent=Decimal(2),
        max_risk_percent=Decimal(1),
        max_daily_loss_percent=Decimal(2),
    )
    candles = parse_csv(csv)
    evidence = evidence_for(store, csv, backtest(candles, settings), candles, False)
    return evidence.id, EVIDENCE.dump_json(evidence).decode()


def test_session_and_deployment_drawdown_and_daily_latch(store: Store) -> None:
    store.initialize()
    key, evidence = create_evidence(store)
    initial: Paper = approve_stream(store, key, START).paper
    gain = replace(initial, equity=Decimal(101000), unrealized=Decimal(1000))
    loss = replace(
        gain, equity=Decimal(99000), realized=Decimal(-1000), unrealized=Decimal(0)
    )
    second = START + timedelta(days=1)
    sessions = record_session((), initial, gain, START)
    sessions = record_session(sessions, gain, loss, second)
    recovery = replace(loss, equity=Decimal(100000), unrealized=Decimal(1000))
    sessions = record_session(sessions, loss, recovery, second + timedelta(minutes=5))
    status = summarize(
        StreamState(
            recovery, daily_start=Decimal(101000), daily_halted=True, sessions=sessions
        ),
        evidence,
        second,
    )
    assert status.recorded_pnl == 0 and status.losing_sessions == 1
    assert status.current and status.current.pnl == -1000
    assert status.current.realized == -1000 and status.current.unrealized == 0
    assert status.max_drawdown.quantize(Decimal(".01")) == Decimal("1.98")
    assert status.remaining_loss_budget == 0
    assert (
        summarize(
            StreamState(recovery, sessions=sessions),
            evidence,
            second + timedelta(days=1),
        ).current
        is None
    )


def test_accepted_quotes_record_one_row_per_session_and_persist(store: Store) -> None:
    with TestClient(create_app(store)) as client:
        assert "Session overview" in client.get("/dashboard").text
        key, _ = create_evidence(store)
        approved = approve_stream(store, key, START)
        for stamp in (START, START + timedelta(seconds=15), START + timedelta(days=1)):
            tick = Quote(approved.paper.symbol, stamp, Decimal(100))
            ingest(store, tick, stamp)
        before = client.get("/operations/paper-performance").json()
        assert len(before[0]["sessions"]) == 2
        assert before[0]["history_complete"]
    with TestClient(create_app(store)) as client:
        after = client.get("/operations/paper-performance").json()
        assert after[0]["sessions"] == before[0]["sessions"]
        assert after[0]["historical"]["bars"] == 10


def test_prior_stream_snapshot_remains_readable(store: Store) -> None:
    store.initialize()
    key, evidence = create_evidence(store)
    paper = approve_stream(store, key, START).paper
    state = StreamState(paper, last_tick=Quote(paper.symbol, START, Decimal(100)))
    raw = STATE.dump_python(
        state, mode="json", exclude={"sessions", "history_started_late"}
    )
    assert not summarize(STATE.validate_python(raw), evidence, START).history_complete
