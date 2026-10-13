"""Reproducible descriptive EDA; all observations and aggregates read by SQL."""

import json
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .config import ROOT
from .db import connect
from .prepare import FEATURE_COLUMNS, serializable

YEAR_COLORS = {2023: "#276D9B", 2024: "#A5681E", 2025: "#8B527E"}
COMPOUND_COLORS = {"SOFT": "#AF6F82", "MEDIUM": "#BC952C", "HARD": "#85939F"}
COMPOUNDS = ["SOFT", "MEDIUM", "HARD"]


def validate_training(conn):
    checks = conn.execute("""SELECT
      (SELECT count(*) FROM analytics.training_laps) AS rows,
      (SELECT count(*) FROM analytics.training_laps WHERE weather_at>lap_start_at OR previous_lap_end_at>lap_start_at) AS future_timestamps,
      (SELECT count(*) FROM analytics.training_laps WHERE compound IS NULL OR tyre_age_before_lap IS NULL OR air_temperature IS NULL OR track_temperature IS NULL OR driver_name IS NULL OR lap_duration IS NULL) AS missing_required,
      (SELECT count(*) FROM analytics.training_laps t JOIN analytics.lap_observations a USING(session_key,driver_number,lap_number) WHERE cardinality(a.exclusion_reasons)>0 OR a.preparation_run_id<>t.preparation_run_id) AS audit_mismatch,
      (SELECT count(*) FROM analytics.training_laps t LEFT JOIN f1.laps p ON p.session_key=t.session_key AND p.driver_number=t.driver_number AND p.lap_number=t.previous_lap_number WHERE t.previous_lap_number IS NOT NULL AND (p.lap_duration IS DISTINCT FROM t.previous_lap_duration OR p.date_start+p.lap_duration*interval '1 second' IS DISTINCT FROM t.previous_lap_end_at OR p.lap_number>=t.lap_number)) AS history_mismatch,
      (SELECT count(*) FROM analytics.training_laps t WHERE EXISTS(SELECT 1 FROM f1.weather w WHERE w.session_key=t.session_key AND w.date<=t.lap_start_at AND w.date>t.weather_at)) AS weather_not_latest,
      (SELECT count(DISTINCT preparation_run_id) FROM analytics.training_laps) AS run_count
    """).fetchone()
    if (
        checks["rows"] == 0
        or checks["run_count"] != 1
        or any(
            checks[k]
            for k in (
                "future_timestamps",
                "missing_required",
                "audit_mismatch",
                "history_mismatch",
                "weather_not_latest",
            )
        )
    ):
        raise ValueError(f"Training table validation failed: {checks}")
    return checks


def load_analysis(url=None):
    with connect(url) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        checks = validate_training(conn)
        rows = conn.execute((ROOT / "sql/006_training_read.sql").read_text()).fetchall()
        run = conn.execute(
            "SELECT r.* FROM analytics.preparation_runs r JOIN (SELECT DISTINCT preparation_run_id FROM analytics.training_laps) t ON r.run_id=t.preparation_run_id"
        ).fetchone()
        profiles = conn.execute("""SELECT year,count(*) AS n,min(lap_duration) AS min_seconds,
            max(lap_duration) AS max_seconds,avg(lap_duration) AS mean_seconds,
            percentile_cont(0.5) WITHIN GROUP(ORDER BY lap_duration) AS median_seconds,
            stddev_samp(lap_duration) AS std_seconds,
            avg(track_temperature) AS mean_track_c,stddev_samp(track_temperature) AS std_track_c,
            min(track_temperature) AS min_track_c,max(track_temperature) AS max_track_c,
            corr(track_temperature,lap_duration) AS pearson_track_duration,
            corr(air_temperature,lap_duration) AS pearson_air_duration,
            count(DISTINCT driver_name) AS drivers,count(DISTINCT compound) AS compounds,
            count(*) FILTER(WHERE previous_lap_duration IS NULL) AS history_missing
            FROM analytics.training_laps GROUP BY year ORDER BY year""").fetchall()
        tyres = conn.execute("""SELECT year,compound,count(*) AS n,count(DISTINCT driver_name) AS drivers,
          min(lap_number) AS min_lap_number,max(lap_number) AS max_lap_number,
          percentile_cont(0.5) WITHIN GROUP(ORDER BY lap_duration) AS median_seconds,
          min(tyre_age_before_lap) AS min_age_laps,max(tyre_age_before_lap) AS max_age_laps
          FROM analytics.training_laps GROUP BY year,compound ORDER BY year,compound""").fetchall()
        cleaning = conn.execute("""SELECT year,count(*) AS raw,count(*) FILTER(WHERE cardinality(exclusion_reasons)=0) AS accepted,
          count(*) FILTER(WHERE cardinality(exclusion_reasons)>0) AS excluded
          FROM analytics.lap_observations GROUP BY year ORDER BY year""").fetchall()
        # Ensure a later ingestion has not silently made the prepared snapshot stale.
        from .prepare import _raw_fingerprint

        current_hash, _ = _raw_fingerprint(conn)
        if current_hash != run["input_sha256"]:
            raise ValueError(
                "Raw sources changed after preparation; run f1-pipeline prepare"
            )
    frame = pd.DataFrame(rows)
    return frame, serializable(
        {
            "generated_at": datetime.now(UTC),
            "rows": len(rows),
            "checks": checks,
            "preparation_run_id": run["run_id"],
            "input_sha256": run["input_sha256"],
            "output_sha256": run["output_sha256"],
            "by_race": profiles,
            "by_compound": tyres,
            "cleaning": cleaning,
            "feature_columns": FEATURE_COLUMNS,
            "sql_source": "sql/006_training_read.sql",
        }
    )


def plot_analysis(frame):
    """Four complementary families. Full plotted target range, all boxplot fliers."""
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.axisbelow": True,
            "grid.alpha": 0.20,
            "figure.facecolor": "white",
        }
    )
    years = sorted(frame.year.unique())
    figures = {}
    lo, hi = frame.lap_duration.min(), frame.lap_duration.max()
    bins = np.linspace(lo - 0.3, hi + 0.3, 31)
    fig, axes = plt.subplots(
        len(years),
        1,
        figsize=(9, 3 * len(years)),
        sharex=True,
        sharey=True,
        layout="constrained",
    )
    for ax, year in zip(np.atleast_1d(axes), years):
        part = frame[frame.year == year]
        ax.hist(
            part.lap_duration,
            bins=bins,
            color=YEAR_COLORS.get(year, "#276D9B"),
            edgecolor="white",
            density=True,
        )
        ax.axvline(
            part.lap_duration.median(),
            color="#333333",
            linestyle="--",
            linewidth=1.3,
            label=f"Медиана {part.lap_duration.median():.2f} с",
        )
        ax.set_title(f"{year}: обычные сухие круги, n={len(part)}", loc="left")
        ax.set_ylabel("Плотность, 1/с")
        ax.legend(frameon=False)
        ax.grid(axis="y")
    np.atleast_1d(axes)[-1].set_xlabel("Время круга, с")
    fig.suptitle("Распределение времени круга • Catalunya 2023–2025", fontsize=14)
    figures["01_distribution"] = fig

    # Driver 1 occurs in every race; NaN gaps avoid drawing lines across removed laps.
    fig, axes = plt.subplots(
        len(years),
        1,
        figsize=(9, 3 * len(years)),
        sharex=True,
        sharey=True,
        layout="constrained",
    )
    for ax, year in zip(np.atleast_1d(axes), years):
        part = frame[(frame.year == year) & (frame.driver_number == 1)].sort_values(
            "lap_number"
        )
        curve = part.set_index("lap_number").lap_duration.reindex(
            range(1, int(frame.lap_number.max()) + 1)
        )
        ax.plot(curve.index, curve.values, color="#707780", linewidth=1)
        for compound in COMPOUNDS:
            sub = part[part.compound == compound]
            if len(sub):
                ax.scatter(
                    sub.lap_number,
                    sub.lap_duration,
                    color=COMPOUND_COLORS[compound],
                    edgecolors="#333333",
                    linewidths=0.4,
                    s=25,
                    label=compound,
                )
        ax.set_title(f"{year}: {part.driver_name.iloc[0]}, n={len(part)}", loc="left")
        ax.set_ylabel("Время круга, с")
        ax.grid(axis="y")
        ax.legend(frameon=False, ncols=3, loc="upper right", fontsize=10)
    np.atleast_1d(axes)[-1].set_xlabel("Номер круга (разрывы — исключённые круги)")
    fig.suptitle("Темп по ходу гонки • один пилот, три сессии", fontsize=14)
    figures["02_lap_dynamics"] = fig

    fig, axes = plt.subplots(
        len(years), 1, figsize=(9, 3 * len(years)), sharey=True, layout="constrained"
    )
    for ax, year in zip(np.atleast_1d(axes), years):
        part = frame[frame.year == year]
        present = [c for c in COMPOUNDS if (part.compound == c).any()]
        values = [part[part.compound == c].lap_duration for c in present]
        box = ax.boxplot(
            values,
            tick_labels=[f"{c}\nn={len(v)}" for c, v in zip(present, values)],
            patch_artist=True,
            medianprops={"color": "#222222"},
            flierprops={"marker": "o", "markersize": 3, "markeredgecolor": "#555555"},
        )
        for patch, compound in zip(box["boxes"], present):
            patch.set_facecolor(COMPOUND_COLORS[compound])
            patch.set_alpha(0.65)
        ax.set_title(str(year))
        ax.set_xlabel("Состав шин")
        ax.set_ylabel("Время круга, с")
        ax.grid(axis="y")
    np.atleast_1d(axes)[0].set_ylabel("Время круга, с")
    fig.suptitle("Сравнение шин • все пилоты, все сохранённые выбросы", fontsize=14)
    figures["03_compounds"] = fig

    fig, axes = plt.subplots(
        len(years),
        1,
        figsize=(9, 3 * len(years)),
        sharex=True,
        sharey=True,
        layout="constrained",
    )
    for ax, year in zip(np.atleast_1d(axes), years):
        part = frame[frame.year == year]
        ax.scatter(
            part.track_temperature,
            part.lap_duration,
            color=YEAR_COLORS.get(year, "#276D9B"),
            s=10,
            alpha=0.30,
            edgecolors="none",
        )
        ax.set_title(f"{year} • n={len(part)}")
        ax.set_ylabel("Время круга, с")
        ax.grid()
    np.atleast_1d(axes)[0].set_ylabel("Время круга, с")
    np.atleast_1d(axes)[-1].set_xlabel("Температура трассы, °C")
    fig.suptitle("Температура и время круга • разделение по гонкам", fontsize=14)
    figures["04_temperature"] = fig
    return figures


def write_eda(output, url=None):
    path = Path(output)
    (path / "figures").mkdir(parents=True, exist_ok=True)
    frame, report = load_analysis(url)
    figures = plot_analysis(frame)
    report["figures"] = []
    for name, fig in figures.items():
        fig.savefig(path / "figures" / f"{name}.png", dpi=160)
        fig.savefig(path / "figures" / f"{name}.svg")
        plt.close(fig)
        report["figures"].append(f"figures/{name}.png")
    (path / "eda.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    lines = [
        "# Исследовательский анализ",
        "",
        f"Источник: SQL → PostgreSQL `analytics.training_laps`; {report['rows']} обычных сухих кругов Catalunya, 2023–2025. UTC: {report['generated_at']}.",
        "",
        "Чтение: [006_training_read.sql](../sql/006_training_read.sql). Проверки: [eda.json](eda.json). Очистка: [cleaning.md](cleaning.md).",
        "",
        "| Год | n | Медиана, с | Среднее, с | SD, с | Трасса min–max, °C | SD трассы, °C | Pearson температура/время |",
        "|---|---:|---:|---:|---:|---|---:|---:|",
    ]
    for r in report["by_race"]:
        lines.append(
            f"| {r['year']} | {r['n']} | {r['median_seconds']:.3f} | {r['mean_seconds']:.3f} | {r['std_seconds']:.3f} | {r['min_track_c']:.1f}–{r['max_track_c']:.1f} | {r['std_track_c']:.3f} | {r['pearson_track_duration']:.3f} |"
        )
    descriptions = [
        (
            "Распределение",
            "Гистограммы используют общие интервалы и плотность для сравнения гонок с разным числом кругов. Правый хвост остаётся в выборке: замедления без формальной причины исключения не удаляются. Пунктир — медиана, не прогноз.",
        ),
        (
            "Динамика",
            "Один и тот же пилот позволяет видеть последовательность темпа, составов и разрывов. Разрывы не соединены линиями. Темп зависит одновременно от топлива, трафика и шин; наклон не является оценкой деградации шин.",
        ),
        (
            "Составы шин",
            "Boxplot показывает медиану, квартильный диапазон, усы 1.5 IQR и все точки за усами. Метки n показывают неодинаковое покрытие. Шины применялись на разных этапах гонки и разными пилотами, поэтому сравнение медиан не доказывает преимущество состава.",
        ),
        (
            "Температура",
            "Каждая точка — круг пилота. Отдельные панели и общие оси показывают, насколько погодные сценарии отличаются между годами. Корреляция описательная: время гонки, топливо и выбор шин связаны с температурой. Повторяющиеся замеры погоды используются многими кругами, число кругов не равно числу независимых погодных наблюдений.",
        ),
    ]
    for (title, text), figure in zip(descriptions, report["figures"]):
        lines += ["", f"## {title}", "", f"![{title}]({figure})", "", text]
        if title == "Составы шин":
            lines += [
                "",
                "| Год | Состав | Кругов | Пилотов | Номера кругов | Медиана, с |",
                "|---|---|---:|---:|---|---:|",
            ]
            for r in report["by_compound"]:
                lines.append(
                    f"| {r['year']} | {r['compound']} | {r['n']} | {r['drivers']} | {r['min_lap_number']}–{r['max_lap_number']} | {r['median_seconds']:.3f} |"
                )
            for r in report["by_compound"]:
                if r["n"] < 20:
                    lines += [
                        "",
                        f"В {r['year']} состав {r['compound']} представлен всего {r['n']} кругами {r['drivers']} пилота(ов), на кругах {r['min_lap_number']}–{r['max_lap_number']}. Этот малый и смещённый поднабор не позволяет оценить преимущество состава.",
                    ]
        if title == "Температура":
            values = "; ".join(
                f"{r['year']}: r={r['pearson_track_duration']:.3f}"
                for r in report["by_race"]
            )
            lines += [
                "",
                f"Описательная корреляция температуры трассы и длительности: {values}. Связь неодинакова по гонкам; объединение всех лет скроет это различие.",
            ]
    lines += [
        "",
        "## Выводы для следующего этапа",
        "",
        "- У шин и температур есть вариативность, но только три гонки дают слабую основу для обобщения погоды. Нужны дополнительные сухие гонки той же трассы для более устойчивых выводов.",
        "- Оценивать модель следует по целым гонкам в хронологическом порядке: например, 2023 — обучение, 2024 — проверка выбора модели, 2025 — финальный тест. Случайное перемешивание кругов смешает почти одинаковые условия и завысит качество.",
        "- SOFT/MEDIUM/HARD и имя пилота остаются категориями; никакое кодирование, масштабирование или заполнение пропусков на полном наборе не обучалось.",
        "- Признак предыдущего времени может улучшить прогноз, но отражает также предыдущий pit/SC круг. При финальной гонке он допустим только в последовательном режиме, когда прошлый результат уже известен; для прогноза всей гонки заранее он недоступен.",
        "- Итоговая задача условная: время обычного сухого круга. Классификацию будущих аварий, дождя и пит-стопов модель этого проекта не решает.",
    ]
    (path / "eda.md").write_text("\n".join(lines) + "\n")
    return report
