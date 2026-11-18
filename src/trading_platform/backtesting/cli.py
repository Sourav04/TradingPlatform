import argparse
import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_platform.backtesting.historical import Settings, backtest
from trading_platform.backtesting.replay import Costs
from trading_platform.market_data.candles import read_candles


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run an offline EMA backtest on NSE candle data"
    )
    parser.add_argument("csv", type=Path)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--fast", type=int, default=2)
    parser.add_argument("--slow", type=int, default=4)
    parser.add_argument("--units", type=int, default=10)
    parser.add_argument("--cash", default="100000")
    parser.add_argument("--fee-bps", default="5")
    parser.add_argument("--slippage-bps", default="10")
    args = parser.parse_args()
    try:
        settings = Settings(
            args.symbol,
            args.start,
            args.end,
            args.fast,
            args.slow,
            args.units,
            Decimal(args.cash),
            Costs(Decimal(args.fee_bps), Decimal(args.slippage_bps)),
        )
        report = backtest(read_candles(args.csv), settings)
    except (OSError, ValueError, InvalidOperation) as exc:
        parser.error(str(exc))
    print(json.dumps(asdict(report), default=str, indent=2))


if __name__ == "__main__":
    main()
