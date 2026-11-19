from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.backtesting.historical import Settings, backtest
from trading_platform.execution.store import Store
from trading_platform.market_data.candles import read_candles
from trading_platform.news_engine.events import Event, Source
from trading_platform.news_engine.journal import events_during, record_events
from trading_platform.news_engine.official import collect, parse_feed

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ema-candles.csv"
SETTINGS = Settings(
    "NSE:DEMO",
    date(2025, 1, 6),
    date(2025, 1, 17),
    stop_loss_percent=Decimal(2),
    max_risk_percent=Decimal(1),
)


def test_intrabar_stop_and_gapped_stop() -> None:
    candles = list(read_candles(SAMPLE))
    entry = candles[6]
    stop = entry.open * Decimal(".98")
    candles[6] = replace(entry, low=stop)
    immediate = backtest(candles, SETTINGS)
    assert [(fill.reason, fill.units) for fill in immediate.fills] == [
        ("ema", 10),
        ("stop_loss", -10),
    ]
    assert immediate.fills[1].execution_price == stop
    assert immediate.fills[0].fill_bar == immediate.fills[1].fill_bar
    assert immediate.equity_curve[-1].units == 0

    candles[6] = entry
    candles[7] = replace(candles[7], open=Decimal(97), low=Decimal(96))
    gapped = backtest(candles, SETTINGS)
    assert len(gapped.fills) == 2
    assert gapped.fills[1].execution_price == Decimal(97) < stop
    assert gapped.fills[1].stop_price == stop
    assert gapped.net_pnl < immediate.net_pnl


def test_risk_budget_counts_estimated_costs() -> None:
    settings = replace(SETTINGS, max_risk_percent=Decimal(".021"))
    report = backtest(read_candles(SAMPLE), settings)
    assert report.fills == []
    assert "risk budget" in report.rejections[0].reason


def test_dashboard_uses_same_backtester_and_labels_mock_news(store: Store) -> None:
    payload = {
        "csv": SAMPLE.read_text(),
        "symbol": "NSE:DEMO",
        "start": "2025-01-06",
        "end": "2025-01-17",
        "mock_news": True,
    }
    with TestClient(create_app(store)) as client:
        assert "Strategy backtest" in client.get("/dashboard").text
        assert client.get("/dashboard/assets/dashboard.js").status_code == 200
        assert client.get("/dashboard/assets/sample.csv").text == SAMPLE.read_text()
        result = client.post("/backtests", json=payload)
        assert result.status_code == 200, result.text
        response = result.json()
        assert (
            client.post("/backtests", json={**payload, "csv": '"unclosed'}).status_code
            == 422
        )
        assert response["news_mode"] == "mock"
        assert any(event["source"] == "Federal Reserve" for event in response["news"])
        assert response["backtest"]["fills"][0]["stop_price"] == "100.94"
        assert (
            client.post(
                "/backtests", json={**payload, "stop_loss_percent": 0}
            ).status_code
            == 422
        )
        assert (
            client.post("/backtests", json={**payload, "csv": "bad CSV"}).status_code
            == 422
        )


def test_dashboard_replay_excludes_news_observed_after_last_bar(store: Store) -> None:
    with TestClient(create_app(store)) as client:
        before = datetime(2025, 1, 16, 21, tzinfo=UTC)
        first = Event(
            Source.FED,
            "known",
            "https://www.federalreserve.gov/a",
            "Known",
            before,
            before,
        )
        late = Event(
            Source.FED,
            "late",
            "https://www.federalreserve.gov/b",
            "Late",
            before,
            datetime(2025, 1, 17, 4, tzinfo=UTC),
        )
        record_events(store, [first, late])
        response = client.post(
            "/backtests",
            json={
                "csv": SAMPLE.read_text(),
                "symbol": "NSE:DEMO",
                "start": "2025-01-06",
                "end": "2025-01-17",
            },
        ).json()
        assert response["news_mode"] == "recorded"
        assert [event["source_id"] for event in response["news"]] == ["known"]


def test_rss_ingest_preserves_original_observation(store: Store) -> None:
    from trading_platform.core.models import ManualClock

    raw = b"""<rss version="2.0"><channel>
      <item><title>Policy statement</title><link>https://www.federalreserve.gov/news.htm</link>
      <pubDate>Thu, 16 Jan 2025 16:00:00 -0500</pubDate></item>
      <item><title>Invalid</title><link>https://example.com/news</link>
      <pubDate>Thu, 16 Jan 2025 16:00:00 -0500</pubDate></item>
      </channel></rss>"""
    store.initialize()
    observed = datetime(2025, 1, 16, 22, tzinfo=UTC)
    clock = ManualClock(observed)
    assert collect(store, Source.FED, clock, lambda _: raw) == (1, 1)
    clock.current += timedelta(hours=1)
    assert collect(store, Source.FED, clock, lambda _: raw) == (1, 1)
    events = events_during(store, observed, observed)
    assert len(events) == 1
    assert events[0].ingested_at == observed
    assert events[0].india_date == "2025-01-17"


def test_rbi_publishes_local_timestamps_without_offsets() -> None:
    raw = b"""<rss><channel><item><title>Policy release</title>
      <link>https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid=1</link>
      <pubDate>Thu, 16 Jan 2025 19:00:00</pubDate></item></channel></rss>"""
    events, skipped = parse_feed(Source.RBI, raw, datetime(2025, 1, 16, 14, tzinfo=UTC))
    assert skipped == 0
    assert events[0].published_at == datetime(2025, 1, 16, 13, 30, tzinfo=UTC)
