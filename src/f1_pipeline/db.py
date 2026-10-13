import uuid
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .api import digest
from .config import ROOT, database_url

# Only these identifiers can be interpolated; all data values use placeholders.
TABLES = {
    "meetings": (
        "meeting_key",
        "meeting_name year circuit_key circuit_short_name country_name location date_start date_end is_cancelled",
    ),
    "sessions": (
        "session_key",
        "meeting_key session_name session_type year circuit_key circuit_short_name country_name date_start date_end is_cancelled",
    ),
    "drivers": ("session_key driver_number", "full_name name_acronym team_name"),
    "laps": (
        "session_key driver_number lap_number",
        "date_start lap_duration duration_sector_1 duration_sector_2 duration_sector_3 is_pit_out_lap",
    ),
    "stints": (
        "session_key driver_number stint_number",
        "lap_start lap_end compound tyre_age_at_start",
    ),
    "weather": (
        "session_key date",
        "air_temperature track_temperature humidity pressure rainfall wind_speed wind_direction",
    ),
    "race_control": (
        "session_key event_id",
        "date driver_number lap_number category flag scope sector message",
    ),
}


@contextmanager
def connect(url=None):
    with psycopg.connect(url or database_url(), row_factory=dict_row) as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        yield conn


def init_db(url=None):
    with connect(url) as conn:
        conn.execute((ROOT / "sql/001_schema.sql").read_text())


def natural_key(endpoint, row):
    if endpoint == "race_control":
        return (row["session_key"], digest(row))
    return tuple(row[k] for k in TABLES[endpoint][0].split())


def upsert(conn, endpoint, batch_id, rows):
    keys, fields = (s.split() for s in TABLES[endpoint])
    columns = keys + fields + ["source_batch_id", "raw_record"]
    query = sql.SQL(
        "INSERT INTO f1.{} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}"
    ).format(
        sql.Identifier(endpoint),
        sql.SQL(",").join(map(sql.Identifier, columns)),
        sql.SQL(",").join(sql.Placeholder() for _ in columns),
        sql.SQL(",").join(map(sql.Identifier, keys)),
        sql.SQL(",").join(
            sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
            for c in columns
            if c not in keys
        ),
    )
    values = []
    seen = set()
    for raw in rows:
        row = dict(raw)
        if endpoint == "race_control":
            row["event_id"] = digest(
                raw
            )  # Source has no stable event ID; exact-record identity.
        key = natural_key(endpoint, raw)
        if any(v is None for v in key):
            raise ValueError(f"Null natural key in {endpoint}")
        if key in seen:
            raise ValueError(f"Duplicate natural key in source {endpoint}: {key}")
        seen.add(key)
        values.append([row.get(c) for c in keys + fields] + [batch_id, Jsonb(raw)])
    with conn.cursor() as cur:
        cur.executemany(query, values)


def record_batch(conn, run_id, batch):
    batch_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO f1.api_batches
        (batch_id,run_id,endpoint,source_url,query_params,fetched_at,row_count,content_sha256,attempts,response)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            batch_id,
            run_id,
            batch.endpoint,
            batch.url,
            Jsonb(batch.params),
            batch.fetched_at,
            len(batch.rows),
            batch.sha256,
            batch.attempts,
            Jsonb(batch.rows),
        ),
    )
    return batch_id


def counts(conn):
    return {
        table: conn.execute(
            sql.SQL("SELECT count(*) AS n FROM f1.{}").format(sql.Identifier(table))
        ).fetchone()["n"]
        for table in TABLES
    }
