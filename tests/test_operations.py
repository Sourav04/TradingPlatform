from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.execution.store import Store
from trading_platform.news_engine.events import Event, Source
from trading_platform.news_engine.journal import record_events

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ema-candles.csv"
PAYLOAD = {
    "symbol": "NSE:DEMO",
    "start": "2025-01-06",
    "end": "2025-01-17",
}


def command(client: TestClient, paper: dict[str, Any], action: str) -> dict[str, Any]:
    response = client.post(
        f"/operations/paper/{paper['id']}",
        json={"action": action, "revision": paper["revision"]},
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def test_approval_records_configuration_and_simulates_protective_exit(
    store: Store,
) -> None:
    with TestClient(create_app(store)) as client:
        catalog = client.get("/operations").json()
        assert catalog["strategies"][0]["id"] == "ema-crossover"
        assert not catalog["live_enabled"] and not catalog["live_orders"]
        assert client.post("/operations/live", json={}).status_code == 404
        backtest = client.post(
            "/backtests", json={**PAYLOAD, "csv": SAMPLE.read_text()}
        )
        assert backtest.status_code == 200, backtest.text
        evidence = backtest.json()["evidence"]
        assert len(evidence["csv_sha256"]) == 64
        assert evidence["confidence"] in (
            "Mixed or weak evidence",
            "Insufficient evidence",
        )
        assert evidence["validation"]["bars"] == 4
        assert evidence["stress"]["pnl"] != evidence["historical"]["pnl"]
        request = {"evidence_id": evidence["id"]}
        approved = client.post("/operations/paper", json=request)
        assert approved.status_code == 201, approved.text
        paper = approved.json()
        assert client.post("/operations/paper", json=request).status_code == 409
        first = command(client, paper, "advance")
        assert first["cursor"] == 1
        repeat = client.post(
            f"/operations/paper/{paper['id']}",
            json={"action": "advance", "revision": 0},
        )
        assert repeat.status_code == 409
        paper = first
        for _ in range(6):
            paper = command(client, paper, "advance")
        assert (
            paper["cursor"] == 7 and paper["units"] == 10 and paper["stop"] == "100.94"
        )
        assert paper["unrealized"] != "0"
        paper = command(client, paper, "pause")
        paper = command(client, paper, "advance")
        assert paper["status"] == "paused" and paper["units"] == 0
        assert paper["fills"][-1]["reason"] == "stop_loss"
        assert paper["realized"] != "0"
        paper = command(client, paper, "close")
        assert paper["status"] == "closed"
        assert [entry["action"] for entry in paper["audit"]] == [
            "approved",
            "pause",
            "close",
        ]
        assert client.get("/operations").json()["paper"][0] == paper

    with TestClient(create_app(store)) as client:
        snapshot = client.get("/operations").json()
        assert snapshot["paper"][0]["cursor"] == 8
        assert snapshot["evidence"][0]["id"] == evidence["id"]


def test_incompatible_policy_and_unavailable_strategy_are_ineligible(
    store: Store,
) -> None:
    with TestClient(create_app(store)) as client:
        inputs = {**PAYLOAD, "csv": SAMPLE.read_text()}
        result = client.post(
            "/backtests", json={**inputs, "strategy_version": "unknown"}
        )
        assert result.status_code == 422
        result = client.post("/backtests", json={**inputs, "max_risk_percent": "5"})
        assert result.status_code == 200
        approval = {"evidence_id": result.json()["evidence"]["id"]}
        assert client.post("/operations/paper", json=approval).status_code == 409


def test_news_recommends_review_without_placing_orders(store: Store) -> None:
    with TestClient(create_app(store)) as client:
        now = datetime.now(UTC)
        item = Event(
            Source.RBI,
            "rbi",
            "https://www.rbi.org.in/release",
            "Monetary policy decision",
            now - timedelta(minutes=3),
            now - timedelta(minutes=2),
        )
        record_events(store, [item])
        data = client.get("/operations").json()
        assert data["news"][0]["recommendation"] == "Review exposure"
        assert data["news"][0]["impact"] == "Direction uncertain"
        assert data["paper"] == [] and data["live_orders"] == []


def test_daily_loss_latches_for_entries_after_protective_exit() -> None:
    from dataclasses import replace
    from datetime import date
    from decimal import Decimal

    from trading_platform.backtesting.historical import Settings, backtest
    from trading_platform.market_data.candles import Candle, read_candles

    start = datetime(2025, 1, 6, 4, tzinfo=UTC)
    candles = [
        replace(candle, timestamp=start + timedelta(minutes=index))
        for index, candle in enumerate(read_candles(SAMPLE))
    ]
    for index, price in enumerate(
        map(Decimal, [96, 102, 112, 116]), start=len(candles)
    ):
        candles.append(
            Candle(start + timedelta(minutes=index), price, price, price, price, 100)
        )
    settings = Settings(
        "NSE:DEMO",
        date(2025, 1, 6),
        date(2025, 1, 6),
        stop_loss_percent=Decimal(2),
        max_risk_percent=Decimal(1),
        max_daily_loss_percent=Decimal(".01"),
    )
    report = backtest(candles, settings)
    assert [fill.reason for fill in report.fills] == ["ema", "stop_loss"]
    assert any(item.reason == "Daily loss limit reached" for item in report.rejections)
