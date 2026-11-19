import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("filename", "fills", "units", "pnl", "rejections"),
    [
        ("flat-market.csv", 0, 0, "0", []),
        ("missing-sessions.csv", 0, 0, "0", ["Market data stale or future-dated"]),
        ("open-position.csv", 1, 10, "38.44", []),
    ],
)
def test_mock_csv_cli(
    filename: str, fills: int, units: int, pnl: str, rejections: list[str]
) -> None:
    result = run_backtest(ROOT / "examples" / "mock-candles" / filename)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["trade_count"] == fills
    assert report["equity_curve"][-1]["units"] == units
    assert Decimal(report["net_pnl"]) == Decimal(pnl)
    assert [item["reason"] for item in report["rejections"]] == rejections
    if fills:
        assert Decimal(report["fills"][0]["open_price"]) == Decimal(104)


def test_duplicate_candle_with_different_utc_offset_is_rejected() -> None:
    result = run_backtest(ROOT / "tests" / "fixtures" / "duplicate-candle.csv")
    assert result.returncode == 2
    assert "Candle timestamps must be unique and strictly increasing" in result.stderr
    assert not result.stdout


def run_backtest(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "trading_platform.backtesting.cli",
            str(path),
            "--symbol",
            "NSE:DEMO",
            "--start",
            "2025-01-06",
            "--end",
            "2025-01-20",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
