import argparse
import csv
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException
from importlib import import_module
from pathlib import Path
from time import monotonic, sleep

from trading_platform.core.config import database_url
from trading_platform.core.models import Quote
from trading_platform.execution.store import Store
from trading_platform.operations.stream_paper import disconnect, ingest

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def exchange_token(instruments: Path, symbol: str) -> str:
    prefix, sep, ticker = symbol.partition(":")
    if prefix != "NSE" or not sep or not ticker:
        raise ValueError("Use a symbol such as NSE:WIPRO")
    with instruments.open(encoding="utf-8-sig", newline="") as source:
        rows = csv.DictReader(source)
        if not {"exchange", "segment", "trading_symbol", "exchange_token"}.issubset(
            rows.fieldnames or []
        ):
            raise ValueError("Groww instruments CSV is missing required fields")
        tokens = {
            row["exchange_token"]
            for row in rows
            if row["exchange"] == "NSE"
            and row["segment"] == "CASH"
            and row["trading_symbol"] == ticker
        }
    if len(tokens) != 1:
        raise ValueError("Expected exactly one NSE CASH instrument for the symbol")
    token = tokens.pop()
    if not token or not token.isascii() or not token.isdecimal():
        raise ValueError("Invalid NSE exchange token in the instruments CSV")
    return token


def decode_ltp(payload: object, symbol: str, instrument: str) -> Quote | None:
    # The published example has an ltp envelope; SDK 1.5.0 get_ltp unwraps it.
    node = payload.get("ltp", payload) if isinstance(payload, dict) else payload
    for key in ("NSE", "CASH", instrument):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if not isinstance(node, dict):
        return None
    millis, price = node.get("tsInMillis"), node.get("ltp")
    if type(millis) not in (int, float, Decimal) or type(price) not in (
        int,
        float,
        Decimal,
    ):
        return None
    try:
        epoch_millis = Decimal(str(millis))
        if (
            not epoch_millis.is_finite()
            or epoch_millis != epoch_millis.to_integral_value()
        ):
            return None
        stamp = EPOCH + timedelta(milliseconds=int(epoch_millis))
        return Quote(symbol, stamp, Decimal(str(price)))
    except (OverflowError, ValueError, DecimalException):
        return None


def consume_sdk(
    sdk: object,
    access_token: str,
    symbol: str,
    instrument: str,
    on_quote: Callable[[Quote], None],
) -> None:
    api = getattr(sdk, "GrowwAPI")(access_token)
    feed = getattr(sdk, "GrowwFeed")(api)
    active = True

    def receive(_: object) -> None:
        if active:
            tick = decode_ltp(feed.get_ltp(), symbol, instrument)
            if tick is not None:
                on_quote(tick)

    try:
        feed.subscribe_ltp(
            [{"exchange": "NSE", "segment": "CASH", "exchange_token": instrument}],
            on_data_received=receive,
        )
        feed.consume()
    finally:
        active = False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send Groww NSE prices to the simulated paper account"
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument(
        "--instruments", type=Path, required=True, help="Official Groww instruments CSV"
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Display quotes without touching paper state",
    )
    args = parser.parse_args()
    access = os.environ.get("GROWW_ACCESS_TOKEN", "")
    if not access or any(ord(char) < 33 or ord(char) > 126 for char in access):
        parser.error("Provide a valid GROWW_ACCESS_TOKEN environment variable")
    try:
        instrument = exchange_token(args.instruments, args.symbol)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    try:
        sdk = import_module("growwapi")
    except ImportError:
        parser.error(
            "Install the provider SDK with uv run --locked --with growwapi==1.5.0"
        )
    if args.probe:
        last_print = float("-inf")

        def show(quote: Quote) -> None:
            nonlocal last_print
            if monotonic() - last_print >= 3:
                last_print = monotonic()
                print(
                    f"{quote.symbol} {quote.timestamp.isoformat()} {quote.price}",
                    flush=True,
                )

        try:
            consume_sdk(sdk, access, args.symbol, instrument, show)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            parser.exit(1, f"Groww quote probe stopped ({type(exc).__name__})\n")
        return

    store = Store(database_url())

    def publish(quote: Quote) -> None:
        ingest(store, quote, datetime.now(UTC))

    try:
        store.initialize()
        for attempt in range(3):
            disconnect(store, args.symbol)
            print(f"Subscribing to {args.symbol} for paper simulation", flush=True)
            try:
                consume_sdk(sdk, access, args.symbol, instrument, publish)
            except Exception as exc:
                print(f"Groww feed stopped ({type(exc).__name__})", file=sys.stderr)
            finally:
                disconnect(store, args.symbol)
            if attempt < 2:
                sleep(2 ** (attempt + 1))
        parser.exit(1, "Groww stream stopped after three connection attempts\n")
    except KeyboardInterrupt:
        return
    except Exception as exc:
        parser.exit(1, f"Streaming paper unavailable ({type(exc).__name__})\n")
    finally:
        store.engine.dispose()


if __name__ == "__main__":
    main()
