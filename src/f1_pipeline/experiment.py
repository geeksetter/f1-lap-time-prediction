"""Chronological experiment: validation selection is persisted before test access."""

import hashlib
import json
import platform
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from psycopg.types.json import Jsonb

from .config import ROOT
from .dataset import baseline_predictions, prepared_metadata, read_laps, split_sessions
from .db import connect
from .metrics import regression_metrics
from .prepare import fingerprint, serializable
from .preprocessing import FEATURE_SETS, LapTimeModel


def write_json(path, value):
    Path(path).write_text(
        json.dumps(serializable(value), ensure_ascii=False, indent=2, allow_nan=False)
        + "\n"
    )


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_hash():
    return fingerprint(
        [
            (name, (ROOT / name).read_text())
            for name in (
                "src/f1_pipeline/regression.py",
                "src/f1_pipeline/preprocessing.py",
                "src/f1_pipeline/metrics.py",
                "src/f1_pipeline/dataset.py",
                "src/f1_pipeline/experiment.py",
                "sql/007_model_dataset.sql",
            )
        ]
    )


def _read_manifest(path):
    manifest = json.loads((path / "manifest.json").read_text())
    if (
        manifest["format_version"] != 1
        or manifest["selected_model"] not in FEATURE_SETS
    ):
        raise ValueError("Unsupported experiment manifest")
    for filename, sha in manifest["model_files_sha256"].items():
        if file_hash(path / filename) != sha:
            raise ValueError(f"Frozen model changed: {filename}")
    return manifest


def score_predictions(frame, predictions):
    return {
        name: regression_metrics(frame.lap_duration.to_numpy(), prediction)
        for name, prediction in predictions.items()
    }


def prediction_table(frame, predictions, phase):
    columns = [
        "session_key",
        "year",
        "driver_number",
        "driver_name",
        "lap_number",
        "compound",
        "lap_start_at",
    ]
    blocks = []
    for name, values in predictions.items():
        part = frame[columns].copy()
        part["phase"], part["model_name"] = phase, name
        part["actual_seconds"] = frame.lap_duration.to_numpy()
        part["predicted_seconds"] = values
        part["residual_seconds"] = part.actual_seconds - part.predicted_seconds
        part["absolute_error_seconds"] = part.residual_seconds.abs()
        part["history_count"] = [len(h) for h in frame.correct_history]
        blocks.append(part)
    return pd.concat(blocks, ignore_index=True)


def save_inputs(frame, path):
    output = frame.drop(columns=["target_matches_source", "correct_history"]).copy()
    output["history_lap_numbers"] = [
        json.dumps([h["lap_number"] for h in rows]) for rows in frame.correct_history
    ]
    output["history_end_times"] = [
        json.dumps([h["end_at"] for h in rows]) for rows in frame.correct_history
    ]
    output["history_durations_seconds"] = [
        json.dumps([h["lap_duration"] for h in rows]) for rows in frame.correct_history
    ]
    output.to_csv(path, index=False)


def grouped_metrics(table):
    result = {}
    for label, columns in {
        "by_race": ["phase", "year", "session_key", "model_name"],
        "by_driver": ["phase", "year", "driver_name", "model_name"],
        "by_compound": ["phase", "year", "compound", "model_name"],
    }.items():
        groups = []
        for key, part in table.groupby(columns, sort=True):
            row = dict(zip(columns, key))
            row.update(
                regression_metrics(
                    part.actual_seconds.to_numpy(), part.predicted_seconds.to_numpy()
                )
            )
            groups.append(row)
        result[label] = groups
    return serializable(result)


def _sql_crosscheck(conn, frame, predictions):
    query = """WITH joined AS (
      SELECT t.lap_duration AS actual,p.estimate,avg(t.lap_duration) OVER() AS target_mean
      FROM jsonb_to_recordset(%s::jsonb) AS p(session_key int,driver_number int,lap_number int,estimate float8)
      JOIN analytics.training_laps t USING(session_key,driver_number,lap_number)
    ) SELECT count(*) AS n,avg(abs(actual-estimate)) AS mae_seconds,
       sqrt(avg(power(actual-estimate,2))) AS rmse_seconds,
       1-sum(power(actual-estimate,2))/nullif(sum(power(actual-target_mean,2)),0) AS r2
       FROM joined"""
    result = {}
    for name, values in predictions.items():
        rows = [
            {
                "session_key": int(r.session_key),
                "driver_number": int(r.driver_number),
                "lap_number": int(r.lap_number),
                "estimate": float(v),
            }
            for r, v in zip(frame.itertuples(), values)
        ]
        actual = conn.execute(query, (Jsonb(rows),)).fetchone()
        expected = regression_metrics(frame.lap_duration.to_numpy(), values)
        if actual["n"] != expected["n"] or any(
            not np.isclose(actual[k], expected[k], atol=1e-10)
            for k in ("mae_seconds", "rmse_seconds", "r2")
            if expected[k] is not None
        ):
            raise ValueError("Independent SQL metric cross-check failed")
        result[name] = actual
    return {"sql": query, "results": result}


def train_experiment(output, url=None):
    path = Path(output)
    with connect(url) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        prepared, sessions = prepared_metadata(conn)
        split = split_sessions(sessions)
        if (path / "evaluation.json").exists():
            manifest = _read_manifest(path)
            if (
                manifest["prepared"]["output_sha256"] != prepared["output_sha256"]
                or manifest["code_sha256"] != code_hash()
            ):
                raise ValueError(
                    "Evaluated experiment is locked: cannot change data/code and tune against its test"
                )
            return manifest
        train, train_hash = read_laps(conn, split["train"])
        validation, validation_hash = read_laps(conn, split["validation"])
        fallback_validation = float(train.lap_duration.mean())
        val_predictions = baseline_predictions(validation, fallback_validation)
        training_diagnostics = {}
        models = {}
        for name in FEATURE_SETS:
            model = LapTimeModel(name).fit(train)
            models[name] = model
            val_predictions[name] = model.predict(validation)
            training_diagnostics[name] = {
                "scope": "in-sample, validation candidate fitted on train races only",
                "metrics": regression_metrics(
                    train.lap_duration.to_numpy(),
                    model.predict(train),
                    in_sample=True,
                    p=model.regressor.n_features_,
                    full_rank=model.regressor.full_rank_,
                ),
                "rank": model.regressor.rank_,
                "design_columns": model.regressor.n_features_ + 1,
                "condition_number": model.regressor.condition_number_,
                "unknown_validation_categories": model.transformer.unknown_counts(
                    validation
                ),
            }
        validation_metrics = score_predictions(validation, val_predictions)
        selected = min(
            FEATURE_SETS,
            key=lambda name: (
                validation_metrics[name]["mae_seconds"],
                list(FEATURE_SETS).index(name),
            ),
        )
        reference = min(
            ("last_correct", "mean_last3_correct"),
            key=lambda name: validation_metrics[name]["mae_seconds"],
        )
        validation_sql = _sql_crosscheck(conn, validation, val_predictions)
        # Selection is fixed. Refit all predeclared variants on train+validation.
        development = pd.concat([train, validation], ignore_index=True)
        path.mkdir(parents=True, exist_ok=True)
        (path / "models").mkdir(exist_ok=True)
        roundtrip, final_diagnostics = {}, {}
        for name in FEATURE_SETS:
            models[name].save(path / "models" / f"{name}_validation.json")
            final = LapTimeModel(name).fit(development)
            filename = path / "models" / f"{name}.json"
            final.save(filename)
            before = final.predict(development)
            after = LapTimeModel.load(filename).predict(development)
            difference = float(np.max(np.abs(before - after)))
            if not np.allclose(before, after, rtol=0, atol=1e-12):
                raise ValueError("Model save/load changed predictions")
            roundtrip[name] = difference
            final_diagnostics[name] = {
                "scope": "in-sample refit on train+validation",
                "metrics": regression_metrics(
                    development.lap_duration.to_numpy(),
                    before,
                    in_sample=True,
                    p=final.regressor.n_features_,
                    full_rank=final.regressor.full_rank_,
                ),
                "rank": final.regressor.rank_,
                "design_columns": final.regressor.n_features_ + 1,
                "condition_number": final.regressor.condition_number_,
            }
        selected_path = path / "selected_model.json"
        selected_path.write_bytes((path / "models" / f"{selected}.json").read_bytes())
        prediction_table(validation, val_predictions, "validation").to_csv(
            path / "validation_predictions.csv", index=False
        )
        save_inputs(validation, path / "validation_inputs.csv")
        manifest = {
            "format_version": 1,
            "created_at": datetime.now(UTC),
            "code_sha256": code_hash(),
            "environment": {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "pandas": pd.__version__,
            },
            "prepared": prepared,
            "sessions": sessions,
            "split": split,
            "read_sessions_during_selection": split["train"] + split["validation"],
            "test_targets_read_during_training": False,
            "train_input_sha256": train_hash,
            "validation_input_sha256": validation_hash,
            "feature_sets": FEATURE_SETS,
            "selected_model": selected,
            "baseline_reference": reference,
            "selection_rule": "minimum validation MAE among three fixed OLS variants; no test access",
            "baseline_policy": {
                "window": 3,
                "correct": "previous completed accepted ordinary dry lap, same session/driver",
                "insufficient_history": "use available 1 or 2 laps; zero -> frozen mean of fitting targets",
                "validation_fallback_seconds": fallback_validation,
                "test_fallback_seconds": float(development.lap_duration.mean()),
            },
            "validation_metrics": validation_metrics,
            "training_diagnostics": training_diagnostics,
            "final_fit_diagnostics": final_diagnostics,
            "serialization_max_abs_difference": roundtrip,
            "validation_sql_crosscheck": validation_sql,
            "validation_group_metrics": grouped_metrics(
                prediction_table(validation, val_predictions, "validation")
            ),
            "model_files_sha256": {
                str(p.relative_to(path)): file_hash(p)
                for p in sorted((path / "models").glob("*.json"))
            },
        }
        manifest["model_files_sha256"]["selected_model.json"] = file_hash(selected_path)
        write_json(path / "manifest.json", manifest)
    return serializable(manifest)


def evaluate_experiment(output, report_output, url=None):
    path = Path(output)
    manifest = _read_manifest(path)
    with connect(url) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        prepared, sessions = prepared_metadata(conn)
        if (
            prepared["output_sha256"] != manifest["prepared"]["output_sha256"]
            or split_sessions(sessions) != manifest["split"]
        ):
            raise ValueError("Prepared data or race boundaries changed since training")
        test, test_hash = read_laps(conn, manifest["split"]["test"])
        predictions = baseline_predictions(
            test, manifest["baseline_policy"]["test_fallback_seconds"]
        )
        unknown, extrapolation = {}, {}
        for name in FEATURE_SETS:
            model = LapTimeModel.load(path / "models" / f"{name}.json")
            predictions[name] = model.predict(test)
            unknown[name] = model.transformer.unknown_counts(test)
            extrapolation[name] = {
                column: {
                    "train_min": float(model.transformer.minima_[i]),
                    "train_max": float(model.transformer.maxima_[i]),
                    "outside_range": int(
                        (
                            (test[column] < model.transformer.minima_[i])
                            | (test[column] > model.transformer.maxima_[i])
                        ).sum()
                    ),
                }
                for i, column in enumerate(model.transformer.numeric)
            }
        table = prediction_table(test, predictions, "test")
        scores = score_predictions(test, predictions)
        result = {
            "created_at": datetime.now(UTC),
            "test_input_sha256": test_hash,
            "selected_model": manifest["selected_model"],
            "baseline_reference": manifest["baseline_reference"],
            "test_metrics": scores,
            "unknown_categories": unknown,
            "numeric_extrapolation": extrapolation,
            "history_counts": test.correct_history.map(len)
            .value_counts()
            .sort_index()
            .to_dict(),
            "group_metrics": grouped_metrics(table),
            "test_sql_crosscheck": _sql_crosscheck(conn, test, predictions),
            "model_files_sha256": manifest["model_files_sha256"],
            "evaluation_policy": "Frozen models; sequential pre-lap predictions; no tuning/refit after test",
        }
        if (path / "evaluation.json").exists():
            prior = json.loads((path / "evaluation.json").read_text())
            if prior["test_input_sha256"] != test_hash or fingerprint(
                prior["test_metrics"]
            ) != fingerprint(scores):
                raise ValueError(
                    "Previously evaluated test changed; cannot silently replace the result"
                )
            result = prior
        else:
            write_json(path / "evaluation.json", result)
        table.to_csv(path / "test_predictions.csv", index=False)
        save_inputs(test, path / "test_inputs.csv")
    from .model_report import write_model_report

    write_model_report(manifest, serializable(result), table, report_output)
    return serializable(result)


def predict_lap(output, session_key, driver_number, lap_number, url=None):
    path = Path(output)
    manifest = _read_manifest(path)
    model = LapTimeModel.load(path / "selected_model.json")
    with connect(url) as conn:
        row = conn.execute(
            """SELECT t.session_key,t.driver_number,t.lap_number,t.driver_name,t.compound,
          t.tyre_age_before_lap,t.air_temperature,t.track_temperature,t.lap_start_at,t.weather_at,s.circuit_short_name
          FROM analytics.training_laps t JOIN f1.sessions s USING(session_key)
          WHERE session_key=%s AND driver_number=%s AND lap_number=%s""",
            (session_key, driver_number, lap_number),
        ).fetchone()
        if row is None:
            raise ValueError("Lap is absent from the accepted historical dataset")
        if (
            row["circuit_short_name"] != manifest["sessions"][0]["circuit_short_name"]
            or row["weather_at"] > row["lap_start_at"]
        ):
            raise ValueError("Unsupported circuit or future weather")
    frame = pd.DataFrame([row])
    return serializable(
        {
            "session_key": session_key,
            "driver_number": driver_number,
            "lap_number": lap_number,
            "lap_start_at": row["lap_start_at"],
            "weather_at": row["weather_at"],
            "model": manifest["selected_model"],
            "prediction_seconds": float(model.predict(frame)[0]),
            "unknown_categories": model.transformer.unknown_counts(frame),
            "mode": "historical pre-start feature replay; current lap target is not read",
        }
    )
