import logging
import os
from datetime import timedelta


def database_url() -> str:
    return os.environ.get("TRADING_DATABASE_URL", "sqlite:///simulation.db")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"trading_platform.{name}")


def news_latency() -> timedelta:
    seconds = int(os.environ.get("TRADING_NEWS_LATENCY_SECONDS", "0"))
    if seconds < 0:
        raise ValueError("TRADING_NEWS_LATENCY_SECONDS must be nonnegative")
    return timedelta(seconds=seconds)
