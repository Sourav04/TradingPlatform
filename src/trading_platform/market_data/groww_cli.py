import argparse
import os
from datetime import UTC, date, datetime
from pathlib import Path

from trading_platform.market_data.groww import (
    GrowwError,
    HistoryRequest,
    fetch_history,
    save_dataset,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download completed daily NSE candles from Groww"
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--output", type=Path, required=True, help="New directory for the dataset"
    )
    args = parser.parse_args()

    try:
        if args.output.exists():
            raise ValueError("Output directory already exists")
        query = HistoryRequest(args.symbol, args.start, args.end)
        candles = fetch_history(
            query, os.environ.get("GROWW_ACCESS_TOKEN", ""), datetime.now(UTC)
        )
        save_dataset(args.output, query, candles)
    except (GrowwError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Saved {len(candles)} daily candles to {args.output / 'candles.csv'}")


if __name__ == "__main__":
    main()
