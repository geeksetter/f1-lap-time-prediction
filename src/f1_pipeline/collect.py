import logging
import uuid

from .db import connect, record_batch, upsert

LOG = logging.getLogger(__name__)
CHILDREN = ["drivers", "laps", "stints", "weather", "race_control"]


def collect_year(client, year, country="Spain", circuit="Catalunya", url=None):
    run_id = uuid.uuid4()
    with connect(url) as conn:
        conn.execute("INSERT INTO f1.ingestion_runs(run_id,year,country_name,status) VALUES (%s,%s,%s,'running')",
                     (run_id,year,country))
    try:
        batches = [client.fetch("sessions", year=year,session_name="Race",country_name=country)]
        selected = [s for s in batches[0].rows if s.get("circuit_short_name") == circuit
                    and not s.get("is_cancelled",False)]
        if len(selected) != 1:
            raise ValueError(f"Expected one {circuit} Race in {year}, got {len(selected)}")
        session = selected[0]
        # Persist complete response; normalize only selected circuit. No invented source rows.
        batches.append(client.fetch("meetings",meeting_key=session["meeting_key"]))
        if len(batches[-1].rows) != 1:
            raise ValueError("Meeting response must contain exactly one row")
        for endpoint in CHILDREN:
            batch = client.fetch(endpoint,session_key=session["session_key"])
            batches.append(batch)
            if not batch.rows:
                raise ValueError(f"Required source {endpoint} is empty")
        # A race is normalized atomically: children never survive a failed parent load.
        with connect(url) as conn:
            ids = {b.endpoint: record_batch(conn,run_id,b) for b in batches}
            meeting_batch = next(b for b in batches if b.endpoint == "meetings")
            upsert(conn,"meetings",ids["meetings"],meeting_batch.rows)
            upsert(conn,"sessions",ids["sessions"],selected)
            for b in batches:
                if b.endpoint in CHILDREN:
                    upsert(conn,b.endpoint,ids[b.endpoint],b.rows)
            conn.execute("UPDATE f1.ingestion_runs SET status='completed',finished_at=now() WHERE run_id=%s",(run_id,))
        LOG.info("Completed year=%s session=%s run=%s",year,session["session_key"],run_id)
        return str(run_id)
    except Exception as exc:
        # Store acquired responses even when normalization fails, so anomalies remain inspectable.
        with connect(url) as conn:
            for b in locals().get("batches",[]):
                record_batch(conn,run_id,b)
            conn.execute("UPDATE f1.ingestion_runs SET status='failed',finished_at=now(),error=%s WHERE run_id=%s",
                         (f"{type(exc).__name__}: {exc}",run_id))
        raise
