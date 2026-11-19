import csv
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from trading_platform.market_data.candles import COLUMNS, INDIA, Candle

ENDPOINT = "https://api.groww.in/v1/historical/candles"
INTERVAL = "1day"
MAX_DAYS = 180
MAX_RESPONSE_BYTES = 1_000_000


class GrowwError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def get_response(request: Request) -> bytes:
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            body: bytes = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        try:
            if exc.code in (401, 403):
                raise GrowwError(
                    f"Groww denied historical data access (HTTP {exc.code})"
                ) from None
            if exc.code == 429:
                raise GrowwError(
                    "Groww rate limit reached (HTTP 429). Try again later."
                ) from None
            raise GrowwError(
                f"Groww historical request failed (HTTP {exc.code})"
            ) from None
        finally:
            exc.close()
    except (URLError, OSError):
        raise GrowwError("Could not reach Groww historical data") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise GrowwError("Groww response exceeded the expected size")
    return body


@dataclass(frozen=True)
class HistoryRequest:
    symbol: str
    start: date
    end: date

    def __post_init__(self) -> None:
        if re.fullmatch(r"NSE:[A-Z0-9][A-Z0-9&._-]{0,29}", self.symbol) is None:
            raise ValueError("Use an NSE cash symbol such as NSE:WIPRO")
        if not 0 <= (self.end - self.start).days < MAX_DAYS:
            raise ValueError("Select an inclusive date range of at most 180 days")

    def params(self) -> dict[str, str]:
        return {
            "exchange": "NSE",
            "segment": "CASH",
            "groww_symbol": self.symbol.replace(":", "-", 1),
            "start_time": datetime.combine(self.start, time.min, INDIA).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "end_time": datetime.combine(self.end, time(23, 59, 59), INDIA).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "candle_interval": INTERVAL,
        }


def fetch_history(
    query: HistoryRequest,
    token: str,
    now: datetime,
    transport: Callable[[Request], bytes] = get_response,
) -> list[Candle]:
    if now.utcoffset() is None or query.end >= now.astimezone(INDIA).date():
        raise ValueError(
            "End date must be before today in India for completed daily candles"
        )
    if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ValueError("Provide a valid GROWW_ACCESS_TOKEN environment variable")
    request = Request(
        f"{ENDPOINT}?{urlencode(query.params())}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "X-API-VERSION": "1.0",
        },
        method="GET",
    )
    return decode_candles(transport(request), query)


def decode_candles(raw: bytes, query: HistoryRequest) -> list[Candle]:
    try:
        response = json.loads(raw, parse_float=Decimal, parse_constant=Decimal)
    except (ValueError, UnicodeError):
        raise GrowwError("Groww returned invalid JSON") from None
    if not isinstance(response, dict) or response.get("status") != "SUCCESS":
        raise GrowwError("Groww returned a failed historical data response")
    payload = response.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise GrowwError("Groww response is missing the historical candle list")
    if payload.get("interval_in_minutes") != 1440:
        raise GrowwError("Groww did not confirm the requested daily interval")
    candles = []
    session_dates = set()
    for index, row in enumerate(payload["candles"], start=1):
        try:
            if not isinstance(row, list) or len(row) not in (6, 7):
                raise ValueError
            stamp, open_, high, low, close, volume = row[:6]
            if not isinstance(stamp, str) or "T" not in stamp and " " not in stamp:
                raise ValueError
            if type(volume) is not int or any(
                type(price) not in (int, Decimal) for price in (open_, high, low, close)
            ):
                raise ValueError
            local = datetime.fromisoformat(stamp)
            if local.utcoffset() is None:
                local = local.replace(tzinfo=INDIA)
            day = local.astimezone(INDIA).date()
            if not query.start <= day <= query.end:
                raise GrowwError(
                    f"Groww candle {index} is outside the requested date range"
                )
            if day in session_dates:
                raise GrowwError(f"Duplicate Groww daily candle for {day}")
            candle = Candle(
                local,
                Decimal(open_),
                Decimal(high),
                Decimal(low),
                Decimal(close),
                volume,
            )
        except (ValueError, OverflowError):
            raise GrowwError(f"Invalid Groww candle at row {index}") from None
        session_dates.add(day)
        candles.append(candle)
    if not candles:
        raise GrowwError("Groww returned no candles for the requested date range")
    return sorted(candles, key=lambda candle: candle.timestamp)


def save_dataset(output: Path, query: HistoryRequest, candles: list[Candle]) -> None:
    present = {candle.timestamp.astimezone(INDIA).date() for candle in candles}
    days = (
        query.start + timedelta(days=index)
        for index in range((query.end - query.start).days + 1)
    )
    missing = [
        day.isoformat() for day in days if day.weekday() < 5 and day not in present
    ]
    output.mkdir()
    table, manifest = output / "candles.csv", output / "metadata.json"
    try:
        with table.open("x", encoding="utf-8", newline="") as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(COLUMNS)
            writer.writerows(
                (c.timestamp.isoformat(), c.open, c.high, c.low, c.close, c.volume)
                for c in candles
            )
        metadata = {
            "provider": "Groww",
            "endpoint": ENDPOINT,
            "symbol": query.symbol,
            "interval": INTERVAL,
            "start_india": query.start.isoformat(),
            "end_india": query.end.isoformat(),
            "candle_count": len(candles),
            "observed_start_india": min(present).isoformat(),
            "observed_end_india": max(present).isoformat(),
            "possible_missing_weekdays_india": missing,
            "csv_sha256": sha256(table.read_bytes()).hexdigest(),
        }
        manifest.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        manifest.unlink(missing_ok=True)
        table.unlink(missing_ok=True)
        output.rmdir()
        raise
