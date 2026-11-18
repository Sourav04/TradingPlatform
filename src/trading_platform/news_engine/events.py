import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from trading_platform.core.models import utc

FRESHNESS = timedelta(hours=24)


class Source(StrEnum):
    NSE = "NSE"
    BSE = "BSE"
    RBI = "RBI"
    FED = "Federal Reserve"
    BLS = "BLS"
    BEA = "BEA"

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(
            "Asia/Kolkata"
            if self in (Source.NSE, Source.BSE, Source.RBI)
            else "America/New_York"
        )


def publication_time(source: Source, local: datetime) -> datetime:
    if local.tzinfo is not None:
        return utc(local)
    first = local.replace(tzinfo=source.zone, fold=0)
    second = local.replace(tzinfo=source.zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError(
            "Ambiguous or nonexistent local time requires an explicit offset"
        )
    return utc(first)


@dataclass(frozen=True)
class Event:
    source: Source
    source_id: str
    url: str
    headline: str
    published_at: datetime
    ingested_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "published_at", utc(self.published_at))
        object.__setattr__(self, "ingested_at", utc(self.ingested_at))
        if (
            not self.source_id
            or not self.headline.strip()
            or not self.url.startswith("https://")
        ):
            raise ValueError(
                "Event requires an identifier, headline and HTTPS provenance"
            )
        if self.ingested_at < self.published_at:
            raise ValueError("Ingestion precedes publication")

    def available_at(self, latency: timedelta) -> datetime:
        if latency < timedelta(0):
            raise ValueError("Publication latency must be nonnegative")
        return max(self.ingested_at, self.published_at + latency)

    @property
    def india_date(self) -> str:
        return self.published_at.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()


class Collector(Protocol):
    source: Source

    def collect(self) -> Iterable[Event]: ...


@dataclass(frozen=True)
class FixtureCollector:
    source: Source
    events: tuple[Event, ...]

    def collect(self) -> Iterable[Event]:
        for event in self.events:
            if event.source != self.source:
                raise ValueError("Collector provenance mismatch")
            yield event


def replay(
    events: Iterable[Event], as_of: datetime, latency: timedelta = timedelta(0)
) -> Iterator[Event]:
    cutoff = utc(as_of)
    if latency < timedelta(0):
        raise ValueError("Publication latency must be nonnegative")
    ordered = sorted(
        events,
        key=lambda event: (
            event.available_at(latency),
            event.published_at,
            event.source,
            event.source_id,
        ),
    )
    identifiers: set[tuple[Source, str]] = set()
    headlines: dict[str, list[datetime]] = defaultdict(list)
    for event in ordered:
        if (
            event.available_at(latency) > cutoff
            or cutoff - event.published_at > FRESHNESS
        ):
            continue
        identity = (event.source, event.source_id)
        normalized = unicodedata.normalize("NFKC", event.headline).casefold()
        headline = " ".join(re.findall(r"\w+", normalized))
        related = headlines[headline]
        duplicate = identity in identifiers or any(
            abs(event.published_at - prior) <= FRESHNESS for prior in related
        )
        identifiers.add(identity)
        if not duplicate:
            related.append(event.published_at)
            yield event
