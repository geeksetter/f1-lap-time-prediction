"""Synthetic fixtures are confined to tests, never reported as OpenF1 observations."""
import os
from datetime import datetime,timezone

import psycopg
import pytest

from f1_pipeline.api import Batch,digest
from f1_pipeline.collect import collect_year
from f1_pipeline.db import connect,counts,init_db
from f1_pipeline.quality import inspect_database


class FixtureClient:
    def __init__(self, bad_driver=False, duplicate=False):
        self.rows={
            "sessions":[{"session_key":101,"meeting_key":11,"year":2024,"circuit_short_name":"Catalunya","session_name":"Race","date_start":"2024-06-23T13:00:00Z"}],
            "meetings":[{"meeting_key":11,"year":2024}],
            "drivers":[{"session_key":101,"driver_number":1,"full_name":"TEST DRIVER"}],
            "laps":[{"session_key":101,"driver_number":99 if bad_driver else 1,"lap_number":1,"lap_duration":None,"date_start":None}],
            "stints":[{"session_key":101,"driver_number":1,"stint_number":1,"lap_start":1,"lap_end":1,"compound":"SOFT","tyre_age_at_start":0}],
            "weather":[{"session_key":101,"date":"2024-06-23T12:59:00Z","rainfall":0}],
            "race_control":[{"session_key":101,"date":"2024-06-23T13:00:00Z","driver_number":None,"message":"TEST GREEN"}],
        }
        if duplicate: self.rows["laps"]*=2

    def fetch(self,endpoint,**params):
        rows=self.rows[endpoint]
        return Batch(endpoint,params,"https://example.invalid/test",datetime.now(timezone.utc),rows,digest(rows),1)


@pytest.fixture
def test_url():
    url=os.getenv("TEST_DATABASE_URL")
    if not url: pytest.skip("Set TEST_DATABASE_URL to a dedicated database ending in _test")
    if not psycopg.conninfo.conninfo_to_dict(url)["dbname"].endswith("_test"):
        pytest.fail("Safety guard: test database must end in _test")
    init_db(url)
    with connect(url) as conn:
        conn.execute("TRUNCATE f1.ingestion_runs,f1.meetings CASCADE")
    return url


@pytest.mark.integration
def test_repeat_preserves_counts_and_nulls(test_url):
    collect_year(FixtureClient(),2024,url=test_url)
    with connect(test_url) as conn: first=counts(conn)
    collect_year(FixtureClient(),2024,url=test_url)
    with connect(test_url) as conn:
        assert counts(conn)==first
        assert conn.execute("SELECT lap_duration FROM f1.laps").fetchone()["lap_duration"] is None
        assert conn.execute("SELECT count(*) AS n FROM f1.api_batches").fetchone()["n"]==14
        assert conn.execute("SELECT count(*) AS n FROM f1.ingestion_runs WHERE status='completed'").fetchone()["n"]==2


@pytest.mark.integration
def test_failed_race_is_atomic_and_retains_source_evidence(test_url):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        collect_year(FixtureClient(bad_driver=True),2024,url=test_url)
    with connect(test_url) as conn:
        assert all(n==0 for n in counts(conn).values())
        assert conn.execute("SELECT status FROM f1.ingestion_runs").fetchone()["status"]=="failed"
        assert conn.execute("SELECT count(*) AS n FROM f1.api_batches").fetchone()["n"]==7


@pytest.mark.integration
def test_duplicate_source_key_is_rejected_and_audited(test_url):
    with pytest.raises(ValueError,match="Duplicate"):
        collect_year(FixtureClient(duplicate=True),2024,url=test_url)
    with connect(test_url) as conn:
        assert counts(conn)["laps"]==0
        assert conn.execute("SELECT response FROM f1.api_batches WHERE endpoint='laps'").fetchone()["response"]==FixtureClient(duplicate=True).rows["laps"]


@pytest.mark.integration
def test_quality_reports_missing_timing_without_imputation(test_url):
    collect_year(FixtureClient(),2024,url=test_url)
    r=inspect_database(test_url)
    assert r['counts']['laps']==1
    assert r['sessions'][0]['null_duration']==1
    assert r['numeric_profiles']['laps.lap_duration'][0]['missing']==1
    assert r['join_coverage'][0]['multiple_stints']==0
    assert all(b['hash_matches'] for b in r['batches'])
    assert not any(i['severity']=='critical' for i in r['issues'])
