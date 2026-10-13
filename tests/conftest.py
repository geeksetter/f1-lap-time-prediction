"""PostgreSQL tests only touch an explicitly configured disposable _test database."""

import os

import psycopg
import pytest

from f1_pipeline.db import connect, init_db


@pytest.fixture
def test_url():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated database ending in _test")
    if not psycopg.conninfo.conninfo_to_dict(url)["dbname"].endswith("_test"):
        pytest.fail("Safety guard: test database must end in _test")
    init_db(url)
    with connect(url) as conn:
        conn.execute(
            "TRUNCATE f1.ingestion_runs,f1.meetings,analytics.preparation_runs CASCADE"
        )
    return url
