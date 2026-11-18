from datetime import datetime, timedelta

from trading_platform.news_engine.events import (
    Event,
    FixtureCollector,
    Source,
    publication_time,
)

OFFICIAL_SITES = {
    Source.NSE: "https://www.nseindia.com/",
    Source.BSE: "https://www.bseindia.com/",
    Source.RBI: "https://www.rbi.org.in/",
    Source.FED: "https://www.federalreserve.gov/",
    Source.BLS: "https://www.bls.gov/",
    Source.BEA: "https://www.bea.gov/",
}


def fixture_collectors() -> tuple[FixtureCollector, ...]:
    collectors = []
    for source, site in OFFICIAL_SITES.items():
        local = datetime(2025, 1, 6, 16)
        published = publication_time(source, local)
        event = Event(
            source,
            "fixture-1",
            site,
            f"Mock {source} release",
            published,
            published + timedelta(minutes=2),
        )
        collectors.append(FixtureCollector(source, (event,)))
    return tuple(collectors)
