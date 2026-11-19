import json
from datetime import UTC, date, datetime
from decimal import Decimal
from email.message import Message
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from trading_platform.backtesting.historical import Settings, backtest
from trading_platform.market_data.candles import read_candles
from trading_platform.market_data.groww import (
    ENDPOINT,
    GrowwError,
    HistoryRequest,
    decode_candles,
    fetch_history,
    get_response,
    save_dataset,
)
from trading_platform.market_data.groww_cli import main

ROOT = Path(__file__).resolve().parents[1]
RAW = (ROOT / "tests/fixtures/groww-daily.json").read_bytes()
QUERY = HistoryRequest("NSE:WIPRO", date(2025, 1, 6), date(2025, 1, 20))
NOW = datetime(2025, 1, 20, 19, tzinfo=UTC)


def test_mock_download_and_offline_replay(tmp_path: Path) -> None:
    def send(request: Request) -> bytes:
        url = urlsplit(request.full_url)
        assert request.get_method() == "GET"
        assert f"{url.scheme}://{url.netloc}{url.path}" == ENDPOINT
        assert request.get_header("Authorization") == "Bearer mock"
        assert request.get_header("X-api-version") == "1.0"
        assert parse_qs(url.query) == {
            "exchange": ["NSE"],
            "segment": ["CASH"],
            "groww_symbol": ["NSE-WIPRO"],
            "start_time": ["2025-01-06 00:00:00"],
            "end_time": ["2025-01-20 23:59:59"],
            "candle_interval": ["1day"],
        }
        return RAW

    candles = fetch_history(QUERY, "mock", NOW, send)
    assert candles[0].timestamp == datetime(2025, 1, 6, 3, 45, tzinfo=UTC)
    output = tmp_path / "dataset"
    save_dataset(output, QUERY, candles)
    csv_file = output / "candles.csv"
    metadata = json.loads((output / "metadata.json").read_text())
    assert metadata["provider"] == "Groww"
    assert metadata["symbol"] == QUERY.symbol
    assert metadata["candle_count"] == 10
    assert metadata["possible_missing_weekdays_india"] == ["2025-01-20"]
    assert metadata["csv_sha256"] == sha256(csv_file.read_bytes()).hexdigest()
    assert list(read_candles(csv_file)) == candles
    settings = Settings(QUERY.symbol, QUERY.start, QUERY.end)
    saved = backtest(read_candles(csv_file), settings)
    assert saved == backtest(read_candles(ROOT / "examples/ema-candles.csv"), settings)
    assert saved.trade_count == 2 and saved.net_pnl == Decimal("-102.94")
    with pytest.raises(FileExistsError):
        save_dataset(output, QUERY, candles)
    assert sha256(csv_file.read_bytes()).hexdigest() == metadata["csv_sha256"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"<html>", "invalid JSON"),
        (b'{"status":"FAILURE"}', "failed historical"),
        (b'{"status":"SUCCESS","payload":{}}', "missing"),
        (
            b'{"status":"SUCCESS","payload":{"candles":[],"interval_in_minutes":5}}',
            "interval",
        ),
        (
            b'{"status":"SUCCESS","payload":{"candles":[],"interval_in_minutes":1440}}',
            "no candles",
        ),
    ],
)
def test_bad_responses(raw: bytes, message: str) -> None:
    with pytest.raises(GrowwError, match=message):
        decode_candles(raw, QUERY)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (["2025-01-17T03:45:00Z", 100, 101, 99, 100, 5], "Duplicate"),
        (["2025-01-21T09:15:00", 100, 101, 99, 100, 5], "outside"),
        (["2025-01-20T09:15:00", 100, 99, 99, 100, 5], "Invalid"),
        (["2025-01-20T09:15:00", 100, 101, 99, 100, True], "Invalid"),
    ],
)
def test_invalid_candles(row: list[object], message: str) -> None:
    data = json.loads(RAW)
    data["payload"]["candles"].append(row)
    with pytest.raises(GrowwError, match=message):
        decode_candles(json.dumps(data).encode(), QUERY)


def test_today_and_missing_token_rejected_before_transport() -> None:
    def send(_: Request) -> bytes:
        pytest.fail("Transport must not be called")

    with pytest.raises(ValueError, match="before today"):
        fetch_history(QUERY, "mock", datetime(2025, 1, 20, 18, 29, tzinfo=UTC), send)
    with pytest.raises(ValueError, match="GROWW_ACCESS_TOKEN"):
        fetch_history(QUERY, "", NOW, send)


@pytest.mark.parametrize(
    ("status", "message"), [(401, "denied"), (429, "rate limit"), (302, "302")]
)
def test_http_error_without_leaking_response(
    monkeypatch: pytest.MonkeyPatch, status: int, message: str
) -> None:
    class Opener:
        def open(self, request: Request, timeout: int) -> None:
            assert timeout == 15
            raise HTTPError(
                request.full_url, status, "mock-sensitive-body", Message(), BytesIO(b"")
            )

    monkeypatch.setattr(
        "trading_platform.market_data.groww.build_opener", lambda _: Opener()
    )
    with pytest.raises(GrowwError, match=message) as raised:
        get_response(Request(ENDPOINT))
    assert "mock-sensitive-body" not in str(raised.value)


def test_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Opener:
        def open(self, request: Request, timeout: int) -> None:
            raise URLError("mock network error")

    monkeypatch.setattr(
        "trading_platform.market_data.groww.build_opener", lambda _: Opener()
    )
    with pytest.raises(GrowwError, match="Could not reach"):
        get_response(Request(ENDPOINT))


def test_cli_exports_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "sample"
    args = [
        "groww_cli",
        "--symbol",
        QUERY.symbol,
        "--start",
        str(QUERY.start),
        "--end",
        str(QUERY.end),
        "--output",
        str(output),
    ]
    monkeypatch.setattr("sys.argv", args)
    monkeypatch.setenv("GROWW_ACCESS_TOKEN", "mock")
    monkeypatch.setattr(
        "trading_platform.market_data.groww_cli.fetch_history",
        lambda query, token, now: decode_candles(RAW, query),
    )
    main()
    assert "Saved 10 daily candles" in capsys.readouterr().out
    assert (output / "metadata.json").is_file()
