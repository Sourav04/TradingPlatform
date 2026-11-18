import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.backtesting.historical import Settings, backtest
from trading_platform.market_data.candles import read_candles

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "ema-candles.csv"
SETTINGS = Settings("NSE:DEMO", date(2025, 1, 6), date(2025, 1, 17))


def test_cli_and_accounting() -> None:
    invocation = [
        sys.executable,
        "-m",
        "trading_platform.backtesting.cli",
        str(FIXTURE),
        "--symbol",
        SETTINGS.symbol,
        "--start",
        str(SETTINGS.start),
        "--end",
        str(SETTINGS.end),
    ]
    output = subprocess.check_output(invocation, text=True)
    assert subprocess.check_output(invocation, text=True) == output
    result = json.loads(output)
    assert [(f["units"], f["open_price"]) for f in result["fills"]] == [
        (10, "103"),
        (-10, "93"),
    ]
    assert result["trade_count"] == 2
    assert Decimal(result["total_cost"]) == Decimal("2.94")
    assert Decimal(result["net_pnl"]) == Decimal("-102.94")
    assert Decimal(result["final_equity"]) == Decimal("99897.06")
    assert result["equity_curve"][-1]["units"] == 0
    assert Decimal(result["max_drawdown_percent"]).quantize(
        Decimal(".0001")
    ) == Decimal(".1114")


def test_no_future_data_and_no_fill_after_selected_range() -> None:
    candles = list(read_candles(FIXTURE))
    result = backtest(
        candles, replace(SETTINGS, start=date(2025, 1, 13), end=date(2025, 1, 16))
    )
    assert result.fills[0].signal_bar == candles[5].timestamp
    assert result.fills[0].fill_bar == candles[6].timestamp
    assert result.unfilled_signal and result.unfilled_signal.target == 0
    assert len(result.fills) == 1
    assert result.equity_curve[-1].units == 10
    perturbed = [*candles[:6], replace(candles[6], close=Decimal("102")), *candles[7:]]
    other = backtest(perturbed, SETTINGS)
    assert result.signals[0] == other.signals[0]
    assert result.fills[0] == other.fills[0]


def test_cash_risk_and_market_gap_fail_closed() -> None:
    candles = list(read_candles(FIXTURE))
    no_cash = backtest(candles, replace(SETTINGS, initial_cash=Decimal("100")))
    assert not no_cash.fills
    assert "Insufficient cash" in no_cash.rejections[0].reason
    max_gross = replace(SETTINGS.limits, max_gross=Decimal(1000))
    exposure = backtest(candles, replace(SETTINGS, limits=max_gross))
    assert not exposure.fills
    assert "exposure" in exposure.rejections[0].reason
    short_age = replace(SETTINGS.limits, quote_age=SETTINGS.limits.quote_age / 14)
    stale = backtest(candles, replace(SETTINGS, limits=short_age))
    assert not stale.fills
    assert "stale" in stale.rejections[0].reason


def test_india_dates_and_chronological_validation() -> None:
    candle = next(read_candles(FIXTURE))
    candle = replace(candle, timestamp=datetime(2025, 1, 5, 19, tzinfo=UTC))
    assert len(backtest([candle], SETTINGS).equity_curve) == 1
    with pytest.raises(ValueError, match="strictly increasing"):
        backtest([candle, candle], SETTINGS)
    with pytest.raises(ValueError, match="No candles"):
        backtest([], SETTINGS)


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ("2025-01-06,100,101,99,100,1", "Timezone-aware"),
        ("2025-01-06T09:15:00+05:30,100,98,90,99,1", "low and high"),
        ("2025-01-06T09:15:00+05:30,NaN,101,99,100,1", "finite"),
        ("2025-01-06T09:15:00+05:30,100,101,99,100,-1", "nonnegative"),
    ],
)
def test_csv_rejects_invalid_rows(tmp_path: Path, row: str, reason: str) -> None:
    path = tmp_path / "data.csv"
    path.write_text(f"timestamp,open,high,low,close,volume\n{row}\n")
    with pytest.raises(ValueError, match=f"CSV line 2: .*{reason}"):
        list(read_candles(path))
