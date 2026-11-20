from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.backtesting.historical import Settings
from trading_platform.execution.store import Store
from trading_platform.operations import automation
from trading_platform.operations.automation import (
    claim_next,
    jobs,
    register,
    run_claim,
    schedule,
)
from trading_platform.operations.workspace import overview
from trading_platform.strategies.catalog import CATALOG

CSV = (Path(__file__).resolve().parents[1] / "examples/ema-candles.csv").read_text()
SETTINGS = Settings("NSE:DEMO", date(2025, 1, 6), date(2025, 1, 17))


def test_worker_completes_registered_plan_without_manual_run(store: Store) -> None:
    with TestClient(create_app(store, start_worker=True)) as client:
        payload = {
            "csv": CSV,
            "symbol": SETTINGS.symbol,
            "start": str(SETTINGS.start),
            "end": str(SETTINGS.end),
            "mock_news": True,
        }
        response = client.post("/backtests/automations", json=payload)
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        assert response.json()["status"] == "queued"
        assert (
            client.post("/backtests/automations", json=payload).json()["id"] == job_id
        )
        deadline = monotonic() + 20
        records = client.get("/backtests/jobs").json()
        while records[0]["status"] in ("queued", "running") and monotonic() < deadline:
            sleep(0.1)
            records = client.get("/backtests/jobs").json()
        assert len(records) == 1
        assert records[0]["status"] == "completed"
        assert records[0]["suite_version"] == "1"
        result = client.get("/operations").json()
        assert result["evidence"][0]["id"] == records[0]["evidence_id"]
        assert result["evidence"][0]["dataset"] == "Synthetic sample"
        assert result["paper"] == [] and result["live_orders"] == []
    with TestClient(create_app(store)) as client:
        assert client.get("/backtests/jobs").json()[0]["status"] == "completed"


def test_registration_validation_updates_and_catalog_upgrade(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.initialize()
    baseline = register(store, CSV, SETTINGS, False)
    changed = register(store, CSV, replace(SETTINGS, fast=3), False)
    refreshed = register(store, CSV.replace(",1000\n", ",1001\n", 1), SETTINGS, False)
    assert len({baseline.id, changed.id, refreshed.id}) == 3
    with pytest.raises(ValueError, match="No candles"):
        register(
            store,
            CSV,
            replace(SETTINGS, start=date(2024, 1, 1), end=date(2024, 2, 1)),
            False,
        )
    assert len(jobs(store)) == 3
    monkeypatch.setattr(automation, "CATALOG", (replace(CATALOG[0], version="2"),))
    now = datetime.now(UTC)
    schedule(store, now)
    schedule(store, now)
    assert [job.strategy_version for job in jobs(store)].count("2") == 3


def test_concurrent_claim_and_expired_worker_cannot_duplicate_evidence(
    store: Store,
) -> None:
    store.initialize()
    expected = register(store, CSV, SETTINGS, False)
    now = datetime.now(UTC)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: claim_next(store, now), range(3)))
    active = [claim for claim in results if claim is not None]
    assert len(active) == 1
    assert jobs(store)[0].status == "running"
    recovered = claim_next(store, now + timedelta(minutes=6))
    assert (
        recovered
        and recovered.id == expected.id
        and recovered.lease_token != active[0].lease_token
    )
    run_claim(store, active[0])
    assert overview(store)[0] == []
    run_claim(store, recovered)
    run_claim(store, recovered)
    assert len(overview(store)[0]) == 1
    assert jobs(store)[0].attempts == 2


def test_unavailable_strategy_fails_without_evidence(store: Store) -> None:
    store.initialize()
    register(store, CSV, SETTINGS, False)
    claim = claim_next(store, datetime.now(UTC))
    assert claim
    run_claim(
        store,
        replace(claim, settings=replace(claim.settings, strategy_version="removed")),
    )
    failed = jobs(store)[0]
    assert failed.status == "failed"
    assert failed.error == "Strategy version is unavailable"
    assert failed.evidence_id is None
