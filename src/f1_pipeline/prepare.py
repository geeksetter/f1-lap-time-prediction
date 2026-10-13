"""SQL-sourced, point-in-time features and separately audited retrospective selection."""

import hashlib
import json
import math
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from .config import ROOT
from .db import TABLES, connect, counts, init_db

POLICY_VERSION = "ordinary-dry-v1"
FEATURE_COLUMNS = (
    "compound",
    "tyre_age_before_lap",
    "air_temperature",
    "track_temperature",
    "driver_name",
    "lap_number",
    "previous_lap_duration",
)
OBSERVATION_COLUMNS = [
    "session_key",
    "driver_number",
    "lap_number",
    "year",
    "driver_name",
    "lap_start_at",
    "lap_end_at",
    "lap_duration",
    "stint_number",
    "stint_lap_start",
    "tyre_age_at_stint_start",
    "compound",
    "tyre_age_before_lap",
    "weather_at",
    "weather_age_seconds",
    "air_temperature",
    "track_temperature",
    "rainfall_at_start",
    "previous_lap_duration",
    "previous_lap_number",
    "previous_lap_end_at",
    "exclusion_reasons",
    "warning_codes",
    "race_control_event_ids",
]
TRAINING_COLUMNS = [
    "session_key",
    "driver_number",
    "lap_number",
    "year",
    "driver_name",
    "lap_start_at",
    "compound",
    "tyre_age_before_lap",
    "air_temperature",
    "track_temperature",
    "weather_at",
    "weather_age_seconds",
    "previous_lap_duration",
    "previous_lap_number",
    "previous_lap_end_at",
    "lap_duration",
]
REASONS = {
    "first_lap": "Первый круг: стартовая процедура",
    "pit_out": "Круг выезда из боксов по OpenF1",
    "pit_in": "Круг перед pit-out / сменой отрезка (ретроспективный вывод)",
    "unknown_pit_status": "Нет признака pit-out",
    "missing_target": "Нет времени круга",
    "invalid_target": "Время неположительно или не является конечным числом",
    "missing_start": "Нет времени начала круга",
    "outside_session": "Начало вне границ сессии",
    "missing_driver": "Нет имени пилота",
    "missing_stint": "Нет подходящего отрезка",
    "ambiguous_stint": "Несколько подходящих отрезков",
    "invalid_tyre_age": "Нет возраста шин или возраст отрицательный",
    "wet_compound": "Дождевой состав шин",
    "unknown_compound": "Нет поддерживаемого состава шин",
    "missing_weather": "Нет погодного наблюдения до начала",
    "stale_weather": "Погода старше 120 с на начало круга",
    "invalid_temperature": "Пропуск / неконечная / физически неправдоподобная температура",
    "rain": "Пересечение с интервалом rainfall=1",
    "unknown_rainfall": "Нет достоверного rainfall на начало / в течение круга",
    "safety_car": "Пересечение с Safety Car",
    "virtual_safety_car": "Пересечение с Virtual Safety Car",
    "red_flag": "Пересечение с красным флагом",
    "yellow_flag": "Пересечение с жёлтым флагом (консервативно для всего круга)",
}


def serializable(value):
    """Stable, credential-free JSON representation of timestamps and UUIDs."""
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def fingerprint(value):
    payload = json.dumps(serializable(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def tyre_age_before_lap(age_at_start, lap_number, lap_start):
    if age_at_start is None or lap_start is None:
        return None
    if age_at_start < 0 or lap_number < lap_start:
        raise ValueError("Invalid tyre age or lap before stint")
    return age_at_start + lap_number - lap_start


@dataclass
class Interval:
    session_key: int
    reason: str
    start: datetime
    end: datetime
    scope: str = "Track"
    driver_number: int | None = None
    sector: int | None = None
    event_ids: list[str] = field(default_factory=list)
    censored: bool = False


def build_control_intervals(events, session_ends):
    """State machine by session/scope; a pit-exit green is not a race restart.

    'SC in this lap' / 'VSC ending' remain active until a subsequent Track CLEAR
    or GREEN. If that signal is absent, retain the interval to session end.
    """
    active, intervals, ending = {}, [], set()

    def close(key, date, event_id):
        interval = active.pop(key)
        interval.end = date
        interval.event_ids.append(event_id)
        intervals.append(interval)

    for e in sorted(events, key=lambda e: (e["session_key"], e["date"], e["event_id"])):
        sid, date, eid = e["session_key"], e["date"], e["event_id"]
        message = (e.get("message") or "").upper()
        flag, scope = (e.get("flag") or "").upper(), e.get("scope") or "Track"
        reason = None
        if "VIRTUAL SAFETY CAR" in message or "VSC" in message:
            reason = "virtual_safety_car"
        elif "SAFETY CAR" in message:
            reason = "safety_car"
        if reason:
            key = (sid, reason, "Track", None, None)
            if "DEPLOYED" in message:
                if key not in active:
                    active[key] = Interval(
                        sid, reason, date, session_ends[sid], event_ids=[eid]
                    )
                ending.discard(key)
            elif "IN THIS LAP" in message or "ENDING" in message:
                if key in active:
                    active[key].event_ids.append(eid)
                    ending.add(key)
            elif "ENDED" in message and key in active:
                close(key, date, eid)
                ending.discard(key)
        if flag in {"YELLOW", "DOUBLE YELLOW", "RED"}:
            reason = "red_flag" if flag == "RED" else "yellow_flag"
            driver = e.get("driver_number") if scope == "Driver" else None
            sector = e.get("sector") if scope == "Sector" else None
            key = (sid, reason, scope, driver, sector)
            if key not in active:
                active[key] = Interval(
                    sid, reason, date, session_ends[sid], scope, driver, sector, [eid]
                )
            else:
                active[key].event_ids.append(eid)
        if flag in {"GREEN", "CLEAR"} and "PIT EXIT" not in message:
            for key in list(active):
                if key[0] != sid:
                    continue
                reason, key_scope, driver, sector = key[1:]
                matches = scope == "Track" or (
                    scope == key_scope
                    and (scope != "Sector" or sector == e.get("sector"))
                    and (scope != "Driver" or driver == e.get("driver_number"))
                )
                if not matches:
                    continue
                if reason in {"safety_car", "virtual_safety_car"} and (
                    scope != "Track" or key not in ending
                ):
                    continue
                if reason == "red_flag" and (scope != "Track" or flag != "GREEN"):
                    continue
                close(key, date, eid)
                ending.discard(key)
    for interval in active.values():
        interval.censored = True
        intervals.append(interval)
    return sorted(intervals, key=lambda i: (i.session_key, i.start, i.reason))


def build_rain_intervals(weather, session_ends):
    grouped = defaultdict(list)
    for row in weather:
        grouped[row["session_key"]].append(row)
    intervals = []
    for sid, rows in grouped.items():
        rows.sort(key=lambda r: r["date"])
        for i, row in enumerate(rows):
            if row["rainfall"] not in (0, 1) or row["rainfall"] == 1:
                end = rows[i + 1]["date"] if i + 1 < len(rows) else session_ends[sid]
                intervals.append(
                    Interval(
                        sid,
                        "rain" if row["rainfall"] == 1 else "unknown_rainfall",
                        row["date"],
                        end,
                    )
                )
    return intervals


def finite(value):
    return value is not None and math.isfinite(value)


def classify_lap(row, intervals):
    """Reasons are audit-only; no target-derived quantile trimming or imputation."""
    reasons, warnings, evidence = set(), set(), set()
    if row["lap_number"] == 1:
        reasons.add("first_lap")
    if row["is_pit_out_lap"] is True:
        reasons.add("pit_out")
    if row["is_pit_out_lap"] is None:
        reasons.add("unknown_pit_status")
    if row["inferred_pit_in"] or row["before_stint_change"]:
        reasons.add("pit_in")
    duration, start, end = row["lap_duration"], row["lap_start_at"], row["lap_end_at"]
    if duration is None:
        reasons.add("missing_target")
    elif not finite(duration) or duration <= 0:
        reasons.add("invalid_target")
    elif duration < 60 or duration > 120:
        warnings.add("unusual_duration_review_only")  # Retain slow laps.
    if start is None:
        reasons.add("missing_start")
    elif start < row["session_start_at"] or start > row["session_end_at"]:
        reasons.add("outside_session")
    if not row["driver_name"]:
        reasons.add("missing_driver")
    if row["stint_matches"] == 0:
        reasons.add("missing_stint")
    elif row["stint_matches"] > 1:
        reasons.add("ambiguous_stint")
    age = row["tyre_age_before_lap"]
    if age is None or age < 0:
        reasons.add("invalid_tyre_age")
    compound = row["compound"]
    if compound in {"INTERMEDIATE", "WET"}:
        reasons.add("wet_compound")
    elif compound not in {"SOFT", "MEDIUM", "HARD"}:
        reasons.add("unknown_compound")
    if row["weather_at"] is None:
        reasons.add("missing_weather")
    elif row["weather_age_seconds"] > 120:
        reasons.add("stale_weather")
    if not (
        finite(row["air_temperature"])
        and -20 <= row["air_temperature"] <= 60
        and finite(row["track_temperature"])
        and -20 <= row["track_temperature"] <= 80
    ):
        reasons.add("invalid_temperature")
    if row["rainfall_at_start"] not in (0, 1):
        reasons.add("unknown_rainfall")
    if row["rainfall_at_start"] == 1:
        reasons.add("rain")
    if start is not None and end is not None:
        for interval in intervals:
            if interval.session_key != row["session_key"]:
                continue
            if (
                interval.scope == "Driver"
                and interval.driver_number != row["driver_number"]
            ):
                continue
            if start < interval.end and end > interval.start:
                reasons.add(interval.reason)
                evidence.update(interval.event_ids)
    if (
        row["previous_lap_number"] is not None
        and row["previous_lap_number"] != row["lap_number"] - 1
    ):
        warnings.add("previous_completed_lap_not_adjacent")
    return dict(
        row,
        exclusion_reasons=sorted(reasons),
        warning_codes=sorted(warnings),
        race_control_event_ids=sorted(evidence),
    )


def validate_candidates(rows, raw_count):
    keys = [(r["session_key"], r["driver_number"], r["lap_number"]) for r in rows]
    checks = {
        "one_candidate_per_raw_lap": len(rows) == raw_count,
        "unique_keys": len(keys) == len(set(keys)),
        "no_ambiguous_stints": all(r["stint_matches"] <= 1 for r in rows),
        "weather_not_future": all(
            r["weather_at"] is None
            or (r["lap_start_at"] is not None and r["weather_at"] <= r["lap_start_at"])
            for r in rows
        ),
        "history_not_future": all(
            r["previous_lap_end_at"] is None
            or r["previous_lap_end_at"] <= r["lap_start_at"]
            for r in rows
        ),
        "history_lap_precedes_target": all(
            r["previous_lap_number"] is None
            or r["previous_lap_number"] < r["lap_number"]
            for r in rows
        ),
        "tyre_age_formula": all(
            r["tyre_age_before_lap"]
            == r["tyre_age_at_stint_start"] + r["lap_number"] - r["stint_lap_start"]
            for r in rows
            if r["tyre_age_at_stint_start"] is not None
        ),
        "feature_whitelist": not set(FEATURE_COLUMNS)
        & {
            "lap_duration",
            "lap_end_at",
            "exclusion_reasons",
            "race_control_event_ids",
            "stint_lap_end",
            "duration_sector_1",
        },
    }
    if not all(checks.values()):
        raise ValueError(f"Preparation validation failed: {checks}")
    return checks


def _raw_fingerprint(conn):
    hashes = {}
    batches = set()
    for table, (keys, _) in TABLES.items():
        query = sql.SQL("SELECT * FROM f1.{} ORDER BY {}").format(
            sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, keys.split()))
        )
        rows = conn.execute(query).fetchall()
        hashes[table] = fingerprint(rows)
        batches.update(r["source_batch_id"] for r in rows)
    provenance = conn.execute(
        "SELECT batch_id,endpoint,source_url,query_params,fetched_at,content_sha256 FROM f1.api_batches WHERE batch_id=ANY(%s::uuid[]) ORDER BY endpoint,batch_id",
        (list(batches),),
    ).fetchall()
    return fingerprint(hashes), serializable(provenance)


def _store_rows(conn, table, columns, rows, run_id):
    columns = columns + ["preparation_run_id"]
    query = sql.SQL("INSERT INTO analytics.{} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, columns)),
        sql.SQL(",").join(sql.Placeholder() for _ in columns),
    )
    with conn.cursor() as cur:
        cur.executemany(
            query, [[r.get(c) for c in columns[:-1]] + [run_id] for r in rows]
        )


def prepare_dataset(
    url=None, output=None, years=(2023, 2024, 2025), circuit="Catalunya"
):
    init_db(url)
    config = {
        "years": list(years),
        "circuit": circuit,
        "weather_max_age_seconds": 120,
        "air_temperature_bounds_c": [-20, 60],
        "track_temperature_bounds_c": [-20, 80],
        "history": "latest strictly completed earlier raw lap within same driver/session",
        "target_trimming": False,
    }
    with connect(url) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute("SELECT pg_advisory_xact_lock(4912027)")
        raw_before, batches = _raw_fingerprint(conn)
        raw_counts = counts(conn)
        sessions = conn.execute(
            "SELECT session_key,year,date_start,date_end FROM f1.sessions WHERE session_name='Race' AND circuit_short_name=%s AND year=ANY(%s) AND is_cancelled IS DISTINCT FROM true ORDER BY year",
            (circuit, list(years)),
        ).fetchall()
        if not sessions or {s["year"] for s in sessions} != set(years):
            raise ValueError(
                "Requested years are not completely covered by source races"
            )
        ids = [s["session_key"] for s in sessions]
        if any(s["date_start"] is None or s["date_end"] is None for s in sessions):
            raise ValueError("Missing session boundaries")
        coverage = {}
        for table in ("drivers", "laps", "stints", "weather", "race_control"):
            q = sql.SQL(
                "SELECT session_key,count(*) AS n FROM f1.{} WHERE session_key=ANY(%s) GROUP BY session_key"
            ).format(sql.Identifier(table))
            result = conn.execute(q, (ids,)).fetchall()
            if {r["session_key"] for r in result} != set(ids):
                raise ValueError(f"Required source {table} is missing for a race")
            coverage[table] = result
        raw_n = sum(r["n"] for r in coverage["laps"])
        candidates = conn.execute(
            (ROOT / "sql/005_prepare_candidates.sql").read_text(),
            (circuit, list(years)),
        ).fetchall()
        checks = validate_candidates(candidates, raw_n)
        ends = {s["session_key"]: s["date_end"] for s in sessions}
        events = conn.execute(
            "SELECT * FROM f1.race_control WHERE session_key=ANY(%s) ORDER BY session_key,date,event_id",
            (ids,),
        ).fetchall()
        weather = conn.execute(
            "SELECT session_key,date,rainfall FROM f1.weather WHERE session_key=ANY(%s) ORDER BY session_key,date",
            (ids,),
        ).fetchall()
        control = build_control_intervals(events, ends)
        rain = build_rain_intervals(weather, ends)
        rows = [classify_lap(r, control + rain) for r in candidates]
        accepted = [r for r in rows if not r["exclusion_reasons"]]
        if not accepted:
            raise ValueError(
                "No ordinary dry laps remain; inspect source quality/policy"
            )
        output_hash = fingerprint(
            [{k: r[k] for k in OBSERVATION_COLUMNS} for r in rows]
        )
        run_id = uuid.uuid4()
        code_hash = fingerprint(
            [
                (name, (ROOT / name).read_text())
                for name in (
                    "src/f1_pipeline/prepare.py",
                    "sql/005_prepare_candidates.sql",
                    "sql/004_preparation_schema.sql",
                )
            ]
        )
        raw_after, _ = _raw_fingerprint(conn)
        checks["raw_sources_unchanged"] = raw_before == raw_after
        conn.execute(
            """INSERT INTO analytics.preparation_runs(run_id,policy_version,input_sha256,output_sha256,code_sha256,configuration,source_batches,raw_count,accepted_count,validation)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                run_id,
                POLICY_VERSION,
                raw_before,
                output_hash,
                code_hash,
                Jsonb(config),
                Jsonb(batches),
                raw_n,
                len(accepted),
                Jsonb(checks),
            ),
        )
        # Atomic replacement of CURRENT derived tables; run log/provenance are retained.
        conn.execute("DELETE FROM analytics.training_laps")
        conn.execute("DELETE FROM analytics.lap_observations")
        _store_rows(conn, "lap_observations", OBSERVATION_COLUMNS, rows, run_id)
        _store_rows(conn, "training_laps", TRAINING_COLUMNS, accepted, run_id)
        final_raw_hash, _ = _raw_fingerprint(conn)
        if final_raw_hash != raw_before:
            raise ValueError("Preparation unexpectedly modified raw sources")
        report = {
            "generated_at": datetime.now(UTC),
            "run_id": run_id,
            "policy_version": POLICY_VERSION,
            "input_sha256": raw_before,
            "output_sha256": output_hash,
            "configuration": config,
            "raw_table_counts": raw_counts,
            "coverage": coverage,
            "checks": checks,
            "total": {
                "raw": raw_n,
                "accepted": len(accepted),
                "excluded": raw_n - len(accepted),
                "excluded_share": (raw_n - len(accepted)) / raw_n,
            },
            "by_race": [],
            "reason_counts": [],
            "control_intervals": [vars(i) for i in control],
            "rain_intervals": [vars(i) for i in rain],
            "warnings": dict(Counter(w for r in rows for w in r["warning_codes"])),
            "tyre_examples": conn.execute(
                """SELECT session_key,driver_number,lap_number,stint_number,stint_lap_start,tyre_age_at_stint_start,tyre_age_before_lap,compound FROM analytics.lap_observations WHERE session_key=9539 AND driver_number=63 AND lap_number IN (1,15,16,17,37,38) ORDER BY lap_number"""
            ).fetchall(),
        }
        for s in sessions:
            group = [r for r in rows if r["session_key"] == s["session_key"]]
            n = sum(bool(r["exclusion_reasons"]) for r in group)
            report["by_race"].append(
                {
                    "year": s["year"],
                    "session_key": s["session_key"],
                    "raw": len(group),
                    "accepted": len(group) - n,
                    "excluded": n,
                    "excluded_share": n / len(group),
                }
            )
        for reason, n in sorted(
            Counter(code for r in rows for code in r["exclusion_reasons"]).items()
        ):
            report["reason_counts"].append(
                {
                    "reason": reason,
                    "description": REASONS[reason],
                    "count": n,
                    "share_of_all": n / raw_n,
                    "by_year": {
                        s["year"]: sum(
                            reason in r["exclusion_reasons"]
                            for r in rows
                            if r["session_key"] == s["session_key"]
                        )
                        for s in sessions
                    },
                }
            )
        report["accepted_profiles"] = conn.execute(
            """SELECT year,count(*) AS n,count(DISTINCT driver_name) AS drivers,count(DISTINCT compound) AS compounds,min(lap_duration) AS min_seconds,max(lap_duration) AS max_seconds,min(tyre_age_before_lap) AS tyre_age_min,max(tyre_age_before_lap) AS tyre_age_max,min(air_temperature) AS air_min,max(air_temperature) AS air_max,min(track_temperature) AS track_min,max(track_temperature) AS track_max,max(weather_age_seconds) AS weather_age_max,count(*) FILTER(WHERE previous_lap_duration IS NULL) AS missing_history FROM analytics.training_laps GROUP BY year ORDER BY year"""
        ).fetchall()
        report["excluded_examples"] = conn.execute(
            """SELECT year,driver_number,lap_number,lap_duration,exclusion_reasons FROM analytics.lap_observations WHERE cardinality(exclusion_reasons)>0 ORDER BY cardinality(exclusion_reasons) DESC,session_key,driver_number,lap_number LIMIT 8"""
        ).fetchall()
        for p in report["accepted_profiles"]:
            if (
                p["compounds"] < 2
                or p["air_min"] == p["air_max"]
                or p["track_min"] == p["track_max"]
            ):
                raise ValueError("Insufficient tyre/weather variation in a race")
        # Reconcile persisted tables, not just Python lists.
        reconciliation = conn.execute("""SELECT
          (SELECT count(*) FROM analytics.lap_observations) AS audited,
          (SELECT count(*) FROM analytics.training_laps) AS training,
          (SELECT count(*) FROM analytics.lap_observations WHERE cardinality(exclusion_reasons)=0) AS eligible,
          (SELECT count(*) FROM analytics.training_laps t JOIN analytics.lap_observations a USING(session_key,driver_number,lap_number) WHERE cardinality(a.exclusion_reasons)>0) AS rejected_in_training""").fetchone()
        if reconciliation != {
            "audited": raw_n,
            "training": len(accepted),
            "eligible": len(accepted),
            "rejected_in_training": 0,
        }:
            raise ValueError(f"Stored table reconciliation failed: {reconciliation}")
        report["reconciliation"] = reconciliation
    report = serializable(report)
    if output:
        write_cleaning_report(report, output)
    return report


def write_cleaning_report(report, output):
    path = Path(output)
    path.mkdir(parents=True, exist_ok=True)
    (path / "cleaning.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    t = report["total"]
    lines = [
        "# Подготовка обычных сухих кругов",
        "",
        f"Сформировано UTC: {report['generated_at']}. Политика: `{POLICY_VERSION}`.",
        "",
        f"Из {t['raw']} кругов сохранено {t['accepted']}; исключено {t['excluded']} ({t['excluded_share']:.2%}).",
        "",
        "Источник — PostgreSQL `f1.*`; все исходные записи сохранены. Итоговая оценка модели будет относиться к ретроспективно отобранным обычным сухим кругам, а не ко всем кругам гонки.",
        "",
        "| Год | Исходные | Сохранены | Исключены | Доля |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in report["by_race"]:
        lines.append(
            f"| {r['year']} | {r['raw']} | {r['accepted']} | {r['excluded']} | {r['excluded_share']:.2%} |"
        )
    lines += [
        "",
        "## Причины (могут пересекаться)",
        "",
        "Доля каждой причины рассчитана от всех исходных кругов. Сумма причин больше числа исключённых кругов: одна запись может иметь несколько причин.",
        "",
        "| Код | Объяснение | Кругов | Доля | 2023 | 2024 | 2025 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in report["reason_counts"]:
        by = r["by_year"]
        lines.append(
            f"| `{r['reason']}` | {r['description']} | {r['count']} | {r['share_of_all']:.2%} | {by.get('2023', 0)} | {by.get('2024', 0)} | {by.get('2025', 0)} |"
        )
    lines += ["", "## Проверки", ""]
    lines += [f"- `{k}`: **{v}**" for k, v in report["checks"].items()]
    lines += [
        "",
        "## Проверка возраста шин на реальных отрезках",
        "",
        "`age_before = tyre_age_at_start + lap_number − lap_start`. Текущий круг ещё не пройден; `+1` не добавляется.",
        "",
        "| Сессия | Пилот | Круг | Отрезок | Начало отрезка | Начальный возраст | Возраст до круга | Состав |",
        "|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in report["tyre_examples"]:
        lines.append(
            "| "
            + " | ".join(
                str(r[k])
                for k in (
                    "session_key",
                    "driver_number",
                    "lap_number",
                    "stint_number",
                    "stint_lap_start",
                    "tyre_age_at_stint_start",
                    "tyre_age_before_lap",
                    "compound",
                )
            )
            + " |"
        )
    lines += [
        "",
        "## Ограничения",
        "",
        "- Жёлтый флаг в мини-секторе исключает все пересекающие его интервал круги: точное время проезда сектора не используется. Это консервативный отбор, часть чистых кругов теряется.",
        "- SC IN THIS LAP не завершает ограничение. В данных 2025 года оно заканчивается последующим TRACK CLEAR. Незакрытое ограничение продолжалось бы до конца сессии и помечалось censored.",
        "- Pit-in выводится из следующего pit-out или следующего отрезка. Это причина ретроспективного исключения; она не является прогнозным признаком. Пит-проезды без отметки pit-out и без смены отрезка могут остаться незамеченными.",
        "- Дождь переносится от замера rainfall=1 до следующего замера. В 2024 году есть один такой замер; он сохранён как факт источника. Отсутствие дождя не гарантирует сухой асфальт.",
        "- Время начала круга приблизительное; погода примерно раз в минуту. Доступность означает timestamp источника, а не доказанную задержку доставки в живом API.",
        "- Предыдущий круг выбирается только если его расчётный конец уже наступил. При несовпадении приблизительных времён возможен пропуск ближайшего круга; предупреждение сохраняется. История включает предыдущие pit/SC круги и не использует будущую очистку.",
        "- Медленные круги не отсекаются по квантилям. Порог 60–120 с создаёт только предупреждение для просмотра.",
        "- Три гонки — три независимых погодных сценария. Топливо, трафик, изменения машины и абсолютный состав C1/C2/C3 не учтены; SOFT/MEDIUM/HARD — относительные обозначения внутри уик-энда.",
        "- Исходные stints — исторические итоговые записи без timestamp объявления. Состав и начальный возраст считаются известными команде до круга; этот проект не воспроизводит задержки публикации API.",
        "",
        "Полные интервалы, предупреждения, примеры и диапазоны: [cleaning.json](cleaning.json). Все причины каждого круга: `analytics.lap_observations`. Правила: [preparation.md](../docs/preparation.md).",
    ]
    (path / "cleaning.md").write_text("\n".join(lines) + "\n")
