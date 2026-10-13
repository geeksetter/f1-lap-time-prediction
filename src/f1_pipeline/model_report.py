"""Static experiment evidence: frozen test predictions, no tuning here."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def write_model_report(manifest, evaluation, table, output):
    path = Path(output)
    (path / "figures").mkdir(parents=True, exist_ok=True)
    chosen, reference = manifest["selected_model"], manifest["baseline_reference"]
    colors = {chosen: "#276D9B", reference: "#A5681E"}
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
    selected = table[table.model_name == chosen]
    baseline = table[table.model_name == reference]
    n = len(selected)
    figures = {}
    fig, axes = plt.subplots(
        2, 1, figsize=(9, 8), sharex=True, sharey=True, layout="constrained"
    )
    lo = (
        min(
            selected.actual_seconds.min(),
            selected.predicted_seconds.min(),
            baseline.predicted_seconds.min(),
        )
        - 0.3
    )
    hi = (
        max(
            selected.actual_seconds.max(),
            selected.predicted_seconds.max(),
            baseline.predicted_seconds.max(),
        )
        + 0.3
    )
    for ax, part, name in zip(axes, [selected, baseline], [chosen, reference]):
        ax.scatter(
            part.actual_seconds,
            part.predicted_seconds,
            s=12,
            alpha=0.35,
            color=colors[name],
            edgecolors="none",
        )
        ax.plot([lo, hi], [lo, hi], linestyle="--", color="#333333", linewidth=1.2)
        ax.set_title(f"{name} • n={len(part)}", loc="left")
        ax.set_ylabel("Прогноз, с")
        ax.grid()
    axes[-1].set_xlabel("Фактическое время, с")
    fig.suptitle("Факт и прогноз • итоговая гонка 2025", fontsize=14)
    figures["01_actual_predicted"] = fig

    fig, ax = plt.subplots(figsize=(9, 5), layout="constrained")
    lo = min(selected.residual_seconds.min(), baseline.residual_seconds.min())
    hi = max(selected.residual_seconds.max(), baseline.residual_seconds.max())
    bins = np.linspace(lo - 0.1, hi + 0.1, 35)
    for part, name in [(selected, chosen), (baseline, reference)]:
        ax.hist(
            part.residual_seconds,
            bins=bins,
            histtype="step",
            linewidth=1.8,
            density=True,
            color=colors[name],
            label=name,
        )
    ax.axvline(0, color="#333333", linestyle="--", linewidth=1.2)
    ax.set(
        xlabel="Остаток: факт − прогноз, с",
        ylabel="Плотность, 1/с",
        title=f"Распределение остатков • одинаковые {n} кругов",
    )
    ax.legend(frameon=False)
    ax.grid(axis="y")
    figures["02_residuals"] = fig

    fig, axes = plt.subplots(
        2, 1, figsize=(9, 8), sharex=True, sharey=True, layout="constrained"
    )
    for ax, part, name in zip(axes, [selected, baseline], [chosen, reference]):
        ax.scatter(
            part.predicted_seconds,
            part.residual_seconds,
            s=12,
            alpha=0.35,
            color=colors[name],
            edgecolors="none",
        )
        ax.axhline(0, color="#333333", linestyle="--", linewidth=1.2)
        ax.set(
            title=f"{name} • n={len(part)}", xlabel="Прогноз, с", ylabel="Остаток, с"
        )
        ax.grid()
    fig.suptitle(
        "Структура остатков • систематическая ошибка видна относительно нуля",
        fontsize=13,
    )
    figures["03_residual_structure"] = fig

    fig, ax = plt.subplots(figsize=(9, 8), layout="constrained")
    names = sorted(selected.driver_name.unique())
    labels = []
    positions = np.arange(len(names))
    for i, (part, name) in enumerate([(selected, chosen), (baseline, reference)]):
        means = part.groupby("driver_name").absolute_error_seconds.mean().reindex(names)
        ax.barh(
            positions + (i - 0.5) * 0.32,
            means,
            height=0.30,
            color=colors[name],
            label=name,
        )
    for name in names:
        labels.append(f"{name} • n={(selected.driver_name == name).sum()}")
    ax.set_yticks(positions, labels)
    ax.invert_yaxis()
    ax.set(xlabel="MAE, с", title="Ошибка по пилотам • итоговый тест 2025")
    ax.legend(frameon=False)
    ax.grid(axis="x")
    figures["04_driver_errors"] = fig

    fig, ax = plt.subplots(figsize=(9, 5), layout="constrained")
    compounds = [
        c for c in ["SOFT", "MEDIUM", "HARD"] if (selected.compound == c).any()
    ]
    positions = np.arange(len(compounds))
    for i, (part, name) in enumerate([(selected, chosen), (baseline, reference)]):
        means = (
            part.groupby("compound").absolute_error_seconds.mean().reindex(compounds)
        )
        ax.bar(
            positions + (i - 0.5) * 0.32,
            means,
            width=0.30,
            color=colors[name],
            label=name,
        )
    ax.set_xticks(
        positions, [f"{c}\nn={(selected.compound == c).sum()}" for c in compounds]
    )
    ax.set(
        ylabel="MAE, с",
        xlabel="Состав шин",
        title="Ошибка по составам • HARD имеет малое покрытие",
    )
    ax.legend(frameon=False)
    ax.grid(axis="y")
    figures["05_compound_errors"] = fig

    fig, ax = plt.subplots(figsize=(9, 5), layout="constrained")
    pilot = selected[selected.driver_number == 1].sort_values("lap_number")
    if len(pilot):
        axis = range(1, int(selected.lap_number.max()) + 1)
        actual = pilot.set_index("lap_number").actual_seconds.reindex(axis)
        ax.plot(
            actual.index, actual.values, color="#333333", label="Факт", linewidth=1.4
        )
        for part, name, style in [(selected, chosen, "--"), (baseline, reference, ":")]:
            curve = (
                part[part.driver_number == 1]
                .set_index("lap_number")
                .predicted_seconds.reindex(axis)
            )
            ax.plot(
                curve.index,
                curve.values,
                color=colors[name],
                linestyle=style,
                linewidth=1.3,
                label=name,
            )
        ax.set(
            title=f"Факт и прогноз по кругам • {pilot.driver_name.iloc[0]}, 2025",
            xlabel="Номер круга (разрывы — исключённые круги)",
            ylabel="Время круга, с",
        )
        ax.legend(frameon=False)
        ax.grid(axis="y")
        figures["06_lap_predictions"] = fig
    else:
        plt.close(fig)
    for name, fig in figures.items():
        fig.savefig(path / "figures" / f"{name}.png", dpi=160)
        plt.close(fig)
    rows = []
    for phase, metrics in [
        ("validation", manifest["validation_metrics"]),
        ("test", evaluation["test_metrics"]),
    ]:
        for name, values in metrics.items():
            rows.append({"phase": phase, "model": name, **values})
    report = {
        "selected_model": chosen,
        "baseline_reference": reference,
        "comparison": rows,
        "group_metrics": evaluation["group_metrics"],
        "validation_group_metrics": manifest["validation_group_metrics"],
        "training_diagnostics": manifest["training_diagnostics"],
        "final_fit_diagnostics": manifest["final_fit_diagnostics"],
        "unknown_categories": evaluation["unknown_categories"],
        "numeric_extrapolation": evaluation["numeric_extrapolation"],
        "figures": [f"figures/{name}.png" for name in figures],
        "largest_selected_errors": selected.nlargest(5, "absolute_error_seconds")[
            [
                "driver_name",
                "lap_number",
                "actual_seconds",
                "predicted_seconds",
                "residual_seconds",
            ]
        ].to_dict(orient="records"),
        "history_strata": [
            {
                "model": name,
                "history_available": has_history,
                "n": len(part),
                "mae_seconds": float(part.absolute_error_seconds.mean()),
            }
            for name in (chosen, reference)
            for has_history in (False, True)
            if len(
                part := table[
                    (table.model_name == name)
                    & ((table.history_count > 0) == has_history)
                ]
            )
        ],
    }
    (path / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    chosen_score = evaluation["test_metrics"][chosen]
    ref_score = evaluation["test_metrics"][reference]
    conclusion = (
        "уступает"
        if chosen_score["mae_seconds"] > ref_score["mae_seconds"]
        else "превосходит"
    )
    lines = [
        "# Множественная линейная регрессия: честная проверка",
        "",
        f"Выбор сделан на валидации 2024: **{chosen}**. Последняя гонка 2025 оценена после сохранения моделей. На итоговом тесте выбранная регрессия **{conclusion}** заранее выбранному базовому прогнозу `{reference}` по MAE.",
        "",
        "| Выборка | Модель | n | MAE, с | RMSE, с | R² | Bias прогноз−факт, с |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['phase']} | {r['model']} | {r['n']} | {r['mae_seconds']:.4f} | {r['rmse_seconds']:.4f} | {r['r2']:.4f} | {r['bias_seconds']:.4f} |"
        )
    lines += [
        "",
        "Все пять методов сравниваются на одинаковых наблюдениях. `mean_last3_correct` использует 1–3 уже завершённых обычных сухих круга. При нуле истории оба базовых метода используют замороженное среднее обучающей выборки. Модели не используют историю как признак: сравнение показывает отдельную ценность пилота, номера круга, шин и погоды.",
        "",
        "Предсказания, входы и временные следы: [artifacts/stage3](../../artifacts/stage3). Точное разделение, параметры преобразований и хеши: [manifest.json](../../artifacts/stage3/manifest.json). Проверка SQL: [evaluation.json](../../artifacts/stage3/evaluation.json). Методика: [modeling.md](../../docs/modeling.md).",
        "",
        "## Обучение и скорректированный R²",
        "",
        "Скорректированный R² приведён только для полного тренировочного набора OLS: n>p+1 и полный ранг, p — число кодированных признаков без свободного члена. Для holdout и групповых срезов он не применяется. Зависимость соседних кругов не позволяет использовать его для статистического вывода о значимости.",
        "",
        "| Финальная модель | Fit n | p | Ранг / столбцы | Train R² | Train adjusted R² |",
        "|---|---:|---:|---|---:|---|",
    ]
    for name, d in manifest["final_fit_diagnostics"].items():
        m = d["metrics"]
        adjusted = (
            f"{m['adjusted_r2']:.4f}"
            if m["adjusted_r2"] is not None
            else m["adjusted_r2_note"]
        )
        lines.append(
            f"| {name} | {m['n']} | {d['design_columns'] - 1} | {d['rank']} / {d['design_columns']} | {m['r2']:.4f} | {adjusted} |"
        )
    captions = {
        "01_actual_predicted": "Пунктир — идеальный прогноз. Отклонения сохраняются целиком, без обрезки плохих прогнозов.",
        "02_residuals": "Остаток = факт − прогноз. Положительный остаток означает недооценку времени. Плотность и общие интервалы позволяют сравнить методы.",
        "03_residual_structure": "Структура относительно нуля показывает смещение и неодинаковый разброс; причина сама по себе по этому графику не устанавливается.",
        "04_driver_errors": "Показаны все пилоты и n. Новые для fit пилоты кодируются нулевыми контрастами, то есть средним эффектом тренировочных категорий.",
        "05_compound_errors": "MAE считается по тем же кругам для обоих методов. HARD: всего шесть кругов одного пилота; этот срез нестабилен.",
        "06_lap_predictions": "Пилот 1 выбран заранее. Разрывы не соединяются. Базовый метод видит только завершённые прошлые круги и может запаздывать при изменении темпа.",
    }
    for name in figures:
        lines += ["", f"![{name}](figures/{name}.png)", "", captions[name]]
    lines += [
        "",
        "## Ошибки и ограничения переноса",
        "",
        f"Среднее смещение выбранной модели: {chosen_score['bias_seconds']:.4f} с. Ошибка и её направление показаны без искусственного ограничения прогнозов.",
        "",
        "- Три гонки — мало независимых сценариев. Результаты по кругам коррелированы; тысячи строк не означают тысячи независимых экспериментов.",
        "- Топливо, трафик и изменения машины не включены. Они могут объяснять ошибки, но этот набор не позволяет отделить их причинный вклад от шин и температуры.",
        "- Погодный набор может требовать экстраполяции: диапазоны fit и число вышедших за них наблюдений приведены ниже. Линейность за границами fit не проверена.",
        "- Начало круга приблизительное; из-за строгого требования завершения ближайший предыдущий круг иногда пропускается. История не переносится между пилотами/гонками.",
        "- Условие обычного сухого круга определяется ретроспективно. Будущие pit/SC/дождь не прогнозируются. Погодные и шинные timestamp не воспроизводят задержки публикации исторического API.",
        "- EDA этапа 2 уже показывал данные 2025; этот тест отделён от обучения и выбора по метрикам, но не является полностью слепым для человека. Улучшенную версию нужно проверять на новой заранее выделенной гонке.",
        "",
        "| Набор | Признак | Fit min | Fit max | Вне диапазона на тесте |",
        "|---|---|---:|---:|---:|",
    ]
    for name, features in evaluation["numeric_extrapolation"].items():
        for feature, d in features.items():
            lines.append(
                f"| {name} | {feature} | {d['train_min']:.3f} | {d['train_max']:.3f} | {d['outside_range']} |"
            )
    lines += [
        "",
        f"Новые для fit пилоты: {evaluation['unknown_categories'][chosen]['driver_name']} кругов из {n}. Их средний тренировочный эффект — явное ограничение переноса, а не известная скорость конкретного нового пилота.",
        "",
        "### Начало истории и крупнейшие ошибки",
        "",
        "| Метод | Есть корректная история | n | MAE, с |",
        "|---|---|---:|---:|",
    ]
    for r in report["history_strata"]:
        lines.append(
            f"| {r['model']} | {r['history_available']} | {r['n']} | {r['mae_seconds']:.4f} |"
        )
    lines += [
        "",
        "| Пилот | Круг | Факт, с | Прогноз, с | Остаток, с |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in report["largest_selected_errors"]:
        lines.append(
            f"| {r['driver_name']} | {r['lap_number']} | {r['actual_seconds']:.3f} | {r['predicted_seconds']:.3f} | {r['residual_seconds']:.3f} |"
        )
    lines += [
        "",
        "Отдельные большие замедления остаются в тесте, если не имеют причины исключения по заранее заданной политике. Трафик или инцидент — возможные объяснения; без дополнительных наблюдений их причину установить нельзя. Эти срезы являются диагностикой уже замороженного теста и не использовались для изменения модели.",
    ]
    lines += [
        "",
        "Все срезы по гонкам, пилотам и составам с n/MAE/RMSE/R²: [metrics.json](metrics.json). Ни выбранный набор, ни окно истории, ни фильтры не менялись после анализа итогового теста.",
    ]
    (path / "results.md").write_text("\n".join(lines) + "\n")
    return report
