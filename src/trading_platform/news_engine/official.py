import argparse
import re
from collections.abc import Callable
from datetime import datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from http.client import HTTPMessage
from typing import IO
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

from sqlalchemy.exc import SQLAlchemyError

from trading_platform.core.config import database_url
from trading_platform.core.models import Clock, SystemClock
from trading_platform.execution.store import Store
from trading_platform.news_engine.events import Event, Source, publication_time
from trading_platform.news_engine.journal import record_events

FEEDS = {
    Source.RBI: "https://rbi.org.in/pressreleases_rss.xml",
    Source.FED: "https://www.federalreserve.gov/feeds/press_all.xml",
}


def official_url(url: str, source: Source) -> bool:
    parsed = urlsplit(url)
    domain = "rbi.org.in" if source == Source.RBI else "federalreserve.gov"
    host = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and (host == domain or host.endswith("." + domain))
        and parsed.username is None
        and parsed.password is None
    )


class OfficialRedirect(HTTPRedirectHandler):
    def __init__(self, source: Source) -> None:
        self.source = source

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        if not official_url(newurl, self.source):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(source: Source) -> bytes:
    request = Request(
        FEEDS[source], headers={"User-Agent": "TradingPlatform/0.1 (RSS reader)"}
    )
    with build_opener(OfficialRedirect(source)).open(request, timeout=15) as response:
        if not official_url(response.geturl(), source):
            raise ValueError("RSS response redirected outside the official domain")
        raw: bytes = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("RSS response exceeds the expected size")
    return raw


def parse_feed(
    source: Source, raw: bytes, observed: datetime
) -> tuple[list[Event], int]:
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.IGNORECASE):
        raise ValueError("RSS declarations are unsupported")
    root = ElementTree.fromstring(raw)
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise ValueError("Expected an official RSS channel")
    events = []
    skipped = 0
    for item in channel.findall("item"):
        try:
            url = urljoin(FEEDS[source], (item.findtext("link") or "").strip())
            headline = (item.findtext("title") or "").strip()
            stamp = (item.findtext("pubDate") or "").strip()
            published = parsedate_to_datetime(stamp)
            if not official_url(url, source) or url == FEEDS[source]:
                raise ValueError("Missing official release link")
            if published.utcoffset() is None and source != Source.RBI:
                raise ValueError("Missing publication offset")
            key = sha256(url.encode()).hexdigest()
            events.append(
                Event(
                    source,
                    key,
                    url,
                    headline,
                    publication_time(source, published),
                    observed,
                )
            )
        except (ValueError, TypeError, OverflowError):
            skipped += 1
    return events, skipped


def collect(
    store: Store,
    source: Source,
    clock: Clock,
    fetch: Callable[[Source], bytes] = download,
) -> tuple[int, int]:
    raw = fetch(source)
    events, skipped = parse_feed(source, raw, clock.now())
    record_events(store, events)
    return len(events), skipped


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record official RBI and Federal Reserve RSS events"
    )
    parser.add_argument("--source", choices=["rbi", "fed", "all"], default="all")
    args = parser.parse_args()
    sources = {"rbi": [Source.RBI], "fed": [Source.FED], "all": list(FEEDS)}
    store = Store(database_url())
    failed = False
    try:
        store.initialize()
        for source in sources[args.source]:
            try:
                accepted, skipped = collect(store, source, SystemClock())
                print(f"{source}: {accepted} valid items, {skipped} skipped")
            except (URLError, OSError, ValueError, ElementTree.ParseError) as exc:
                failed = True
                print(f"{source}: collection failed ({exc})")
    except SQLAlchemyError:
        parser.exit(1, "Could not persist news in the configured database\n")
    finally:
        store.engine.dispose()
    if failed:
        parser.exit(1)


if __name__ == "__main__":
    main()
