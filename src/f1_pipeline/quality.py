import json
from datetime import datetime, timezone
from pathlib import Path

from psycopg import sql

from .api import canonical, digest
from .config import ROOT
from .db import TABLES, connect, counts, natural_key

NUMERIC = {
    "laps": ["lap_duration", "duration_sector_1", "duration_sector_2", "duration_sector_3"],
    "stints": ["lap_start", "lap_end", "tyre_age_at_start"],
    "weather": ["air_temperature", "track_temperature", "humidity", "pressure", "wind_speed", "wind_direction"],
}


def inspect_database(url=None):
    with connect(url) as conn:
        result = {"generated_at": datetime.now(timezone.utc).isoformat(),
                  "schema": "f1", "counts": counts(conn), "issues": []}
        result["sessions"] = conn.execute((ROOT/"sql/002_quality.sql").read_text()).fetchall()
        result["numeric_profiles"] = {}
        for table, columns in NUMERIC.items():
            for col in columns:
                query = sql.SQL("""SELECT s.year,count(*) AS n,count(*)-count(t.{c}) AS missing,
                    min(t.{c}) AS minimum,max(t.{c}) AS maximum,avg(t.{c}) AS mean,
                    percentile_cont(.5) WITHIN GROUP(ORDER BY t.{c}) AS median,
                    mode() WITHIN GROUP(ORDER BY t.{c}) AS mode,
                    var_samp(t.{c}) AS variance,stddev_samp(t.{c}) AS stddev
                    FROM f1.{t} t JOIN f1.sessions s USING(session_key)
                    GROUP BY s.year ORDER BY s.year""").format(c=sql.Identifier(col),t=sql.Identifier(table))
                result["numeric_profiles"][f"{table}.{col}"] = conn.execute(query).fetchall()
        result["driver_coverage"] = conn.execute("""SELECT d.session_key,d.driver_number,d.full_name,
            count(l.lap_number) AS laps,min(l.lap_number) AS first_lap,max(l.lap_number) AS last_lap
            FROM f1.drivers d LEFT JOIN f1.laps l USING(session_key,driver_number)
            GROUP BY d.session_key,d.driver_number,d.full_name ORDER BY d.session_key,d.driver_number""").fetchall()
        result["compound_frequency"] = conn.execute("""SELECT s.year,t.compound,count(*) AS stints
            FROM f1.stints t JOIN f1.sessions s USING(session_key) GROUP BY s.year,t.compound
            ORDER BY s.year,t.compound""").fetchall()
        result["time_coverage"] = conn.execute("""SELECT s.year,
            (SELECT min(date_start) FROM f1.laps l WHERE l.session_key=s.session_key) AS first_lap_start,
            (SELECT max(date_start) FROM f1.laps l WHERE l.session_key=s.session_key) AS last_lap_start,
            (SELECT min(date) FROM f1.weather w WHERE w.session_key=s.session_key) AS weather_start,
            (SELECT max(date) FROM f1.weather w WHERE w.session_key=s.session_key) AS weather_end
            FROM f1.sessions s ORDER BY s.year""").fetchall()
        result["join_coverage"] = conn.execute("""WITH coverage AS (
            SELECT l.session_key,l.driver_number,l.lap_number,l.date_start,
              (SELECT count(*) FROM f1.stints t WHERE t.session_key=l.session_key
               AND t.driver_number=l.driver_number AND l.lap_number BETWEEN t.lap_start AND t.lap_end) AS matches,
              (SELECT max(w.date) FROM f1.weather w WHERE w.session_key=l.session_key AND w.date<=l.date_start) AS weather_date
            FROM f1.laps l)
            SELECT s.year,count(*) AS laps,
              count(*) FILTER(WHERE matches=0) AS unmatched_stint,
              count(*) FILTER(WHERE matches>1) AS multiple_stints,
              count(*) FILTER(WHERE c.date_start IS NOT NULL AND weather_date IS NULL) AS missing_prior_weather,
              max(extract(epoch FROM c.date_start-weather_date)) AS max_weather_age_seconds
            FROM coverage c JOIN f1.sessions s USING(session_key) GROUP BY s.year ORDER BY s.year""").fetchall()
        result["domain_checks"] = conn.execute("""SELECT
            (SELECT count(*) FROM f1.laps WHERE lap_duration<=0) AS nonpositive_duration,
            (SELECT count(*) FROM f1.stints WHERE lap_start>lap_end OR tyre_age_at_start<0) AS invalid_stints,
            (SELECT count(*) FROM f1.weather WHERE rainfall NOT IN (0,1) OR humidity NOT BETWEEN 0 AND 100) AS invalid_weather,
            (SELECT count(*) FROM f1.drivers d WHERE NOT EXISTS
              (SELECT 1 FROM f1.laps l WHERE l.session_key=d.session_key AND l.driver_number=d.driver_number)) AS drivers_without_laps
        """).fetchone()
        result["duplicates"] = {}
        for table, (keys,_) in TABLES.items():
            q=sql.SQL("SELECT count(*) AS n FROM (SELECT {} FROM f1.{} GROUP BY {} HAVING count(*)>1) d").format(
                sql.SQL(",").join(map(sql.Identifier,keys.split())),sql.Identifier(table),
                sql.SQL(",").join(map(sql.Identifier,keys.split())))
            result["duplicates"][table]=conn.execute(q).fetchone()["n"]
        result["batches"] = []
        # Latest completed run for each year; failed runs are separately visible below.
        batches=conn.execute("""SELECT b.* FROM f1.api_batches b JOIN (
            SELECT DISTINCT ON (year,country_name) run_id FROM f1.ingestion_runs
            WHERE status='completed' ORDER BY year,country_name,finished_at DESC
            ) r USING(run_id) ORDER BY b.fetched_at""").fetchall()
        for b in batches:
            rows=b.pop("response")
            b["hash_matches"] = digest(rows)==b["content_sha256"].strip()
            keys=[natural_key(b["endpoint"],row) for row in rows]
            b["duplicate_natural_keys"] = len(keys)-len(set(keys))
            b["exact_duplicates"] = len(rows)-len(set(map(canonical,rows)))
            fields=sorted({k for r in rows for k in r})
            b["fields"] = fields
            b["missing_fields"] = {k:sum(r.get(k) is None for r in rows) for k in fields}
            result["batches"].append(b)
        result["runs"] = conn.execute("""SELECT run_id,year,country_name,status,started_at,finished_at,error
            FROM f1.ingestion_runs ORDER BY started_at""").fetchall()
        for table,n in result["duplicates"].items():
            if n: result["issues"].append({"severity":"critical","issue":f"Duplicate keys: {table}","count":n})
        for name,n in result["domain_checks"].items():
            if n: result["issues"].append({"severity":"high","issue":name,"count":n})
        for row in result["sessions"]:
            for field in ("drivers","laps","stints","weather","race_control"):
                if not row[field]: result["issues"].append({"severity":"critical","issue":f"Empty {field}","year":row["year"]})
            if row["null_duration"] or row["null_start"]:
                result["issues"].append({"severity":"medium","year":row["year"],"issue":"Null lap timing; preserve now, exclude or resolve in stage 2",
                                         "null_duration":row["null_duration"],"null_start":row["null_start"]})
            if row["rain_samples"] or row["rain_unknown"]:
                result["issues"].append({"severity":"medium","year":row["year"],"issue":"Dry scope needs temporal rainfall filtering",
                                         "rain_samples":row["rain_samples"],"rain_unknown":row["rain_unknown"]})
        for row in result["join_coverage"]:
            if row["multiple_stints"] or row["unmatched_stint"] or row["missing_prior_weather"]:
                result["issues"].append({"severity":"high","issue":"Join coverage requires stage-2 treatment",**row})
        for b in result["batches"]:
            if not b["hash_matches"] or b["duplicate_natural_keys"]:
                result["issues"].append({"severity":"critical","issue":"Batch hash or uniqueness failed","endpoint":b["endpoint"]})
    return result


def render_report(report):
    lines=["# Проверка исходных данных OpenF1", "",f"Сформировано: {report['generated_at']}","",
           "Исходные наблюдения сохранены в PostgreSQL без заполнения пропусков и фильтрации кругов.","",
           "| Год | Сессия | Пилоты | Круги | Отрезки | Погода | События | Нет времени круга | Нет начала круга | Дождевые замеры |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for s in report["sessions"]:
        lines.append("| "+" | ".join(str(s[k]) for k in ("year","session_key","drivers","laps","stints","weather","race_control","null_duration","null_start","rain_samples"))+" |")
    lines += ["","## Связи", "", "```json",json.dumps(report["join_coverage"],ensure_ascii=False,indent=2,default=str),"```",
              "","## Замечания", ""]
    for issue in report["issues"]:
        lines.append("- "+json.dumps(issue,ensure_ascii=False,default=str))
    if not report["issues"]: lines.append("Критических и существенных замечаний по выполненным проверкам не обнаружено.")
    lines += ["", "## Границы интерпретации", "",
              "- Пропуски не означают нулевое время круга; источник сохранён без подстановок.",
              "- Номер пилота является ключом внутри сессии, а не постоянным идентификатором человека.",
              "- Отсутствие дождевых замеров не доказывает сухость каждого участка трассы.",
              "- Круги с флагами, ограничениями и въездами в боксы будут отфильтрованы на следующем этапе.",
              "- Лаповые строки зависимы; три гонки дают только три независимых гоночных условия.",
              "- Составы SOFT/MEDIUM/HARD относительны выбранному гоночному уикенду.",
              "- OpenF1 date_start круга приблизителен; точность временных объединений ограничена источником.",
              "- Полные профили, частоты, происхождение и SHA-256 находятся в quality.json."]
    return "\n".join(lines)+"\n"


def write_report(output=ROOT/"reports",url=None):
    report=inspect_database(url)
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    (output/"quality.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+"\n")
    (output/"quality.md").write_text(render_report(report))
    return report
