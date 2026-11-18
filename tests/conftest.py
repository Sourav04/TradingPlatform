import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from trading_platform.execution.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        database = Store(f"sqlite:///{tmp_path / 'test.db'}")
        try:
            yield database
        finally:
            database.engine.dispose()
        return

    schema = f"phase1_test_{uuid4().hex}"
    admin = create_engine(url)
    scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    database = Store(scoped_url.render_as_string(hide_password=False))
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
        yield database
    finally:
        database.engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.dispose()
