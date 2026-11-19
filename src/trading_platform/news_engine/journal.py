from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import Index, String, Text, select, text
from sqlalchemy.orm import Mapped, mapped_column

from trading_platform.core.models import utc
from trading_platform.execution.store import Base, Store
from trading_platform.news_engine.events import FRESHNESS, Event, Source


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (Index("ix_news_items_published_at", "published_at"),)

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    headline: Mapped[str] = mapped_column(Text)
    published_at: Mapped[str] = mapped_column(String(40))
    ingested_at: Mapped[str] = mapped_column(String(40))


def timestamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds")


def record_events(store: Store, events: Iterable[Event]) -> None:
    rows = [
        {
            "source": event.source.value,
            "source_id": event.source_id,
            "url": event.url,
            "headline": event.headline,
            "published_at": timestamp(event.published_at),
            "ingested_at": timestamp(event.ingested_at),
        }
        for event in events
    ]
    if rows:
        statement = text(
            "INSERT INTO news_items (source, source_id, url, headline, published_at, ingested_at) "
            "VALUES (:source, :source_id, :url, :headline, :published_at, :ingested_at) "
            "ON CONFLICT (source, source_id) DO NOTHING"
        )
        with store.engine.begin() as connection:
            connection.execute(statement, rows)


def events_during(store: Store, start: datetime, end: datetime) -> list[Event]:
    statement = select(
        NewsItem.source,
        NewsItem.source_id,
        NewsItem.url,
        NewsItem.headline,
        NewsItem.published_at,
        NewsItem.ingested_at,
    ).where(
        NewsItem.published_at >= timestamp(start - FRESHNESS),
        NewsItem.published_at <= timestamp(end),
        NewsItem.ingested_at <= timestamp(end),
    )
    with store.engine.connect() as connection:
        return [
            Event(
                Source(source),
                key,
                url,
                headline,
                datetime.fromisoformat(published),
                datetime.fromisoformat(ingested),
            )
            for source, key, url, headline, published, ingested in connection.execute(
                statement
            )
        ]
