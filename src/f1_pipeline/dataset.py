"""The only experiment input is parameterized SQL against PostgreSQL."""

import itertools
from datetime import datetime

import numpy as np
import pandas as pd

from .config import ROOT
from .prepare import fingerprint, serializable


def prepared_metadata(conn):
    runs = conn.execute(
        "SELECT DISTINCT preparation_run_id FROM analytics.training_laps"
    ).fetchall()
    if len(runs) != 1:
        raise ValueError("Need one complete prepared dataset; run f1-pipeline prepare")
    run = conn.execute(
        "SELECT run_id,input_sha256,output_sha256,policy_version FROM analytics.preparation_runs WHERE run_id=%s",
        (runs[0]["preparation_run_id"],),
    ).fetchone()
    sessions = conn.execute("""SELECT t.session_key,s.year,s.date_start,s.date_end,s.circuit_short_name,
      count(*) AS n,min(t.lap_start_at) AS first_lap_start,max(t.lap_start_at) AS last_lap_start
      FROM analytics.training_laps t JOIN f1.sessions s USING(session_key)
      GROUP BY t.session_key,s.year,s.date_start,s.date_end,s.circuit_short_name ORDER BY s.date_start,t.session_key""").fetchall()
    if len(sessions) < 3 or len({s["circuit_short_name"] for s in sessions}) != 1:
        raise ValueError("Need at least three chronological races on one circuit")
    if any(a["date_end"] >= b["date_start"] for a, b in itertools.pairwise(sessions)):
        raise ValueError("Session time boundaries overlap")
    return serializable(run), sessions


def split_sessions(sessions):
    return {
        "train": [s["session_key"] for s in sessions[:-2]],
        "validation": [sessions[-2]["session_key"]],
        "test": [sessions[-1]["session_key"]],
    }


def read_laps(conn, session_ids):
    if not session_ids:
        raise ValueError("Empty requested cohort")
    rows = conn.execute(
        (ROOT / "sql/007_model_dataset.sql").read_text(), (list(session_ids),)
    ).fetchall()
    if not rows or {r["session_key"] for r in rows} != set(session_ids):
        raise ValueError("Requested cohort not completely covered")
    keys = [(r["session_key"], r["driver_number"], r["lap_number"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate model observation")
    for r in rows:
        if (
            not r["target_matches_source"]
            or r["weather_at"] > r["lap_start_at"]
            or not np.isfinite(
                [
                    r[k]
                    for k in (
                        "lap_duration",
                        "tyre_age_before_lap",
                        "air_temperature",
                        "track_temperature",
                    )
                ]
            ).all()
        ):
            raise ValueError(
                "Invalid point-in-time observation or changed source target"
            )
        history = r["correct_history"]
        numbers = [p["lap_number"] for p in history]
        if len(numbers) > 3 or numbers != sorted(set(numbers), reverse=True):
            raise ValueError("Invalid history ordering/cardinality")
        for p in history:
            if (
                (p["session_key"], p["driver_number"])
                != (r["session_key"], r["driver_number"])
                or p["lap_number"] >= r["lap_number"]
                or datetime.fromisoformat(p["end_at"]) > r["lap_start_at"]
                or not np.isfinite(p["lap_duration"])
                or p["lap_duration"] <= 0
            ):
                raise ValueError("History leakage / mixed driver or session")
    return pd.DataFrame(rows), fingerprint(rows)


def baseline_predictions(frame, fallback):
    if not np.isfinite(fallback) or fallback <= 0:
        raise ValueError("Invalid training-only fallback")
    last, mean = [], []
    for history in frame.correct_history:
        values = [h["lap_duration"] for h in history]
        last.append(values[0] if values else fallback)
        mean.append(float(np.mean(values)) if values else fallback)
    return {"last_correct": np.asarray(last), "mean_last3_correct": np.asarray(mean)}
