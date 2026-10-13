"""Adversarial synthetic cases, never substituted for real OpenF1 data."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from f1_pipeline.db import connect, counts, upsert
from f1_pipeline.eda import validate_training
from f1_pipeline.prepare import (
    FEATURE_COLUMNS,
    Interval,
    build_control_intervals,
    build_rain_intervals,
    classify_lap,
    prepare_dataset,
    tyre_age_before_lap,
)

BASE = datetime(2024, 6, 23, 13, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "age,lap,start,expected",
    [(0, 16, 16, 0), (0, 17, 16, 1), (3, 1, 1, 3), (3, 15, 1, 17), (3, 44, 44, 3)],
)
def test_tyre_age_counts_completed_laps_only(age, lap, start, expected):
    assert tyre_age_before_lap(age, lap, start) == expected


def test_invalid_age_is_not_repaired():
    assert tyre_age_before_lap(None, 17, 16) is None
    with pytest.raises(ValueError):
        tyre_age_before_lap(-1, 17, 16)
    with pytest.raises(ValueError):
        tyre_age_before_lap(0, 15, 16)


def event(second, message, flag=None, scope=None, sector=None, driver=None, sid=101):
    return {
        "session_key": sid,
        "date": BASE + timedelta(seconds=second),
        "event_id": f"event-{sid}-{second}",
        "message": message,
        "flag": flag,
        "scope": scope,
        "sector": sector,
        "driver_number": driver,
    }


def test_safety_car_ending_notice_is_not_restart_and_pit_green_is_ignored():
    events = [
        event(10, "SAFETY CAR DEPLOYED"),
        event(20, "SAFETY CAR IN THIS LAP"),
        event(25, "GREEN LIGHT - PIT EXIT OPEN", "GREEN", "Track"),
        event(30, "TRACK CLEAR", "CLEAR", "Track"),
    ]
    (interval,) = build_control_intervals(events, {101: BASE + timedelta(hours=2)})
    assert interval.start == BASE + timedelta(seconds=10)
    assert interval.end == BASE + timedelta(seconds=30)
    assert not interval.censored


def test_vsc_red_and_sector_states_do_not_clear_each_other():
    events = [
        event(10, "VIRTUAL SAFETY CAR DEPLOYED"),
        event(11, "YELLOW", "YELLOW", "Sector", 12),
        event(12, "RED", "RED", "Track"),
        event(20, "CLEAR", "CLEAR", "Sector", 11),
        event(21, "CLEAR", "CLEAR", "Sector", 12),
        event(25, "VIRTUAL SAFETY CAR ENDING"),
        event(30, "TRACK CLEAR", "CLEAR", "Track"),
        event(40, "GREEN FLAG", "GREEN", "Track"),
    ]
    intervals = {
        i.reason: i
        for i in build_control_intervals(events, {101: BASE + timedelta(hours=2)})
    }
    assert intervals["yellow_flag"].end == BASE + timedelta(seconds=21)
    assert intervals["virtual_safety_car"].end == BASE + timedelta(seconds=30)
    assert intervals["red_flag"].end == BASE + timedelta(seconds=40)


def test_unclosed_control_state_is_censored_to_session_end():
    end = BASE + timedelta(hours=2)
    intervals = build_control_intervals([event(10, "SAFETY CAR DEPLOYED")], {101: end})
    assert intervals[0].censored and intervals[0].end == end


def ordinary_row(duration=80):
    return {
        "session_key": 101,
        "driver_number": 1,
        "lap_number": 5,
        "driver_name": "TEST",
        "lap_start_at": BASE + timedelta(seconds=400),
        "lap_end_at": BASE + timedelta(seconds=400 + duration),
        "session_start_at": BASE,
        "session_end_at": BASE + timedelta(hours=2),
        "lap_duration": duration,
        "is_pit_out_lap": False,
        "inferred_pit_in": False,
        "before_stint_change": False,
        "stint_matches": 1,
        "compound": "SOFT",
        "tyre_age_before_lap": 4,
        "weather_at": BASE + timedelta(seconds=390),
        "weather_age_seconds": 10,
        "air_temperature": 25,
        "track_temperature": 40,
        "rainfall_at_start": 0,
        "previous_lap_number": 4,
    }


def test_slow_lap_is_retained_and_driver_yellow_does_not_affect_other_driver():
    row = ordinary_row(150)
    other = Interval(
        101,
        "yellow_flag",
        BASE,
        BASE + timedelta(hours=2),
        scope="Driver",
        driver_number=2,
    )
    result = classify_lap(row, [other])
    assert result["exclusion_reasons"] == []
    assert "unusual_duration_review_only" in result["warning_codes"]


def test_half_open_flag_overlap_and_multiple_reasons():
    row = ordinary_row()
    interval = Interval(
        101, "yellow_flag", row["lap_end_at"], row["lap_end_at"] + timedelta(seconds=10)
    )
    assert classify_lap(row, [interval])["exclusion_reasons"] == []
    interval.start = row["lap_end_at"] - timedelta(seconds=1)
    row.update(is_pit_out_lap=True, inferred_pit_in=True)
    assert set(classify_lap(row, [interval])["exclusion_reasons"]) == {
        "yellow_flag",
        "pit_out",
        "pit_in",
    }


def test_rain_is_held_only_until_next_weather_observation():
    weather = [
        {"session_key": 101, "date": BASE + timedelta(seconds=s), "rainfall": rain}
        for s, rain in [(0, 0), (420, 1), (480, 0)]
    ]
    intervals = build_rain_intervals(weather, {101: BASE + timedelta(hours=2)})
    (interval,) = intervals
    assert interval.end == BASE + timedelta(seconds=480)
    assert classify_lap(ordinary_row(), intervals)["exclusion_reasons"] == ["rain"]


def test_no_target_or_retrospective_field_in_features():
    assert "previous_lap_duration" in FEATURE_COLUMNS
    assert not set(FEATURE_COLUMNS) & {
        "lap_duration",
        "lap_end_at",
        "duration_sector_1",
        "is_pit_out_lap",
        "exclusion_reasons",
        "rainfall_during_lap",
        "race_control_event_ids",
    }


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"lap_duration": None, "lap_end_at": None}, "missing_target"),
        ({"lap_duration": float("inf"), "lap_end_at": None}, "invalid_target"),
        ({"lap_start_at": None, "lap_end_at": None}, "missing_start"),
        ({"compound": "INTERMEDIATE"}, "wet_compound"),
        ({"tyre_age_before_lap": -1}, "invalid_tyre_age"),
        ({"weather_age_seconds": 121}, "stale_weather"),
        ({"air_temperature": None}, "invalid_temperature"),
        ({"track_temperature": 150}, "invalid_temperature"),
        ({"rainfall_at_start": None}, "unknown_rainfall"),
    ],
)
def test_missing_and_invalid_data_have_explicit_reasons(changes, reason):
    row = ordinary_row()
    row.update(changes)
    assert reason in classify_lap(row, [])["exclusion_reasons"]


def seed(conn):
    run, batch = uuid.uuid4(), uuid.uuid4()

    def store(endpoint, rows):
        upsert(conn, endpoint, batch, json.loads(json.dumps(rows, default=str)))

    conn.execute(
        "INSERT INTO f1.ingestion_runs(run_id,year,country_name,status) VALUES (%s,2024,'Spain','completed')",
        (run,),
    )
    conn.execute(
        """INSERT INTO f1.api_batches(batch_id,run_id,endpoint,source_url,query_params,fetched_at,row_count,content_sha256,attempts,response)
      VALUES (%s,%s,'test','https://example.invalid',%s,now(),0,%s,1,%s)""",
        (batch, run, Jsonb({}), "0" * 64, Jsonb([])),
    )
    store("meetings", [{"meeting_key": 11, "year": 2024}])
    for sid in [101, 102]:
        store(
            "sessions",
            [
                {
                    "session_key": sid,
                    "meeting_key": 11,
                    "year": 2024,
                    "session_name": "Race",
                    "circuit_short_name": "Catalunya",
                    "date_start": BASE,
                    "date_end": BASE + timedelta(hours=2),
                }
            ],
        )
        store(
            "drivers",
            [
                {"session_key": sid, "driver_number": d, "full_name": f"TEST {d}"}
                for d in [1, 2]
            ],
        )
        for driver in [1, 2]:
            store(
                "laps",
                [
                    {
                        "session_key": sid,
                        "driver_number": driver,
                        "lap_number": lap,
                        "date_start": BASE + timedelta(seconds=(lap - 1) * 80),
                        "lap_duration": 80 + driver,
                        "is_pit_out_lap": lap == 4,
                        "duration_sector_1": 20,
                    }
                    for lap in range(1, 8)
                ],
            )
            store(
                "stints",
                [
                    {
                        "session_key": sid,
                        "driver_number": driver,
                        "stint_number": n,
                        "lap_start": start,
                        "lap_end": end,
                        "compound": compound,
                        "tyre_age_at_start": age,
                    }
                    for n, start, end, compound, age in [
                        (1, 1, 3, "SOFT", 3),
                        (2, 4, 7, "MEDIUM", 0),
                    ]
                ],
            )
        store(
            "weather",
            [
                {
                    "session_key": sid,
                    "date": BASE + timedelta(seconds=second),
                    "air_temperature": 25 + second / 1000 + sid - 101,
                    "track_temperature": 40 + second / 1000,
                    "rainfall": 0,
                }
                for second in range(-1, 601, 60)
            ],
        )
        store("race_control", [dict(event(0, "GREEN FLAG", "GREEN", "Track", sid=sid))])


@pytest.mark.integration
def test_sql_asof_stint_pit_history_isolation_and_repeat(test_url):
    with connect(test_url) as conn:
        seed(conn)
        before = counts(conn)
    first = prepare_dataset(test_url, years=(2024,))
    second = prepare_dataset(test_url, years=(2024,))
    assert first["output_sha256"] == second["output_sha256"]
    assert first["total"] == second["total"]
    with connect(test_url) as conn:
        assert counts(conn) == before
        assert validate_training(conn)["history_mismatch"] == 0
        rows = conn.execute(
            "SELECT * FROM analytics.lap_observations WHERE session_key=101 AND driver_number=1 ORDER BY lap_number"
        ).fetchall()
        assert "pit_in" in rows[2]["exclusion_reasons"]
        assert "pit_out" in rows[3]["exclusion_reasons"]
        assert rows[3]["tyre_age_before_lap"] == 0
        assert rows[4]["tyre_age_before_lap"] == 1
        # 81s duration but next start at +80s: nearest lap isn't yet completed.
        assert rows[4]["previous_lap_number"] == 3
        assert rows[4]["previous_lap_duration"] == 81
        assert rows[4]["weather_at"] == BASE + timedelta(seconds=299)
        assert rows[4]["weather_at"] <= rows[4]["lap_start_at"]
        other = conn.execute(
            "SELECT * FROM analytics.training_laps WHERE session_key=102 AND driver_number=2 AND lap_number=5"
        ).fetchone()
        assert other["previous_lap_duration"] == 82
        assert other["air_temperature"] != rows[4]["air_temperature"]


@pytest.mark.integration
def test_future_weather_and_same_lap_sector_target_cannot_change_features(test_url):
    with connect(test_url) as conn:
        seed(conn)
    prepare_dataset(test_url, years=(2024,))
    with connect(test_url) as conn:
        before = conn.execute(
            "SELECT * FROM analytics.training_laps WHERE session_key=101 AND driver_number=1 AND lap_number=5"
        ).fetchone()
        conn.execute(
            "UPDATE f1.weather SET air_temperature=50,track_temperature=70 WHERE session_key=101 AND date>%s",
            (before["lap_start_at"],),
        )
        conn.execute(
            "UPDATE f1.laps SET duration_sector_1=999,lap_duration=90 WHERE session_key=101 AND driver_number=1 AND lap_number=5"
        )
    prepare_dataset(test_url, years=(2024,))
    with connect(test_url) as conn:
        after = conn.execute(
            "SELECT * FROM analytics.training_laps WHERE session_key=101 AND driver_number=1 AND lap_number=5"
        ).fetchone()
    assert [before[c] for c in FEATURE_COLUMNS] == [after[c] for c in FEATURE_COLUMNS]
    assert after["lap_duration"] == 90 and before["lap_duration"] == 81


@pytest.mark.integration
def test_ambiguous_stint_fails_without_replacing_previous_prepared_dataset(test_url):
    with connect(test_url) as conn:
        seed(conn)
    first = prepare_dataset(test_url, years=(2024,))
    with connect(test_url) as conn:
        conn.execute(
            "INSERT INTO f1.stints SELECT session_key,driver_number,3,lap_start,lap_end,compound,tyre_age_at_start,source_batch_id,raw_record FROM f1.stints WHERE session_key=101 AND driver_number=1 AND stint_number=1"
        )
    with pytest.raises(ValueError, match="validation failed"):
        prepare_dataset(test_url, years=(2024,))
    with connect(test_url) as conn:
        stored = conn.execute(
            "SELECT DISTINCT preparation_run_id FROM analytics.training_laps"
        ).fetchone()
        assert str(stored["preparation_run_id"]) == first["run_id"]
