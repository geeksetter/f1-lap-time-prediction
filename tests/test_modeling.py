"""Analytic solutions, holdout isolation and SQL-history adversarial checks."""

import json
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from psycopg import sql
from test_preparation import seed

from f1_pipeline.dataset import baseline_predictions, read_laps
from f1_pipeline.db import connect, upsert
from f1_pipeline.experiment import evaluate_experiment, predict_lap, train_experiment
from f1_pipeline.metrics import regression_metrics
from f1_pipeline.prepare import prepare_dataset
from f1_pipeline.preprocessing import FeatureTransformer, LapTimeModel
from f1_pipeline.regression import MultipleLinearRegression


def test_exact_multivariate_solution_and_serialization(tmp_path):
    X = np.array([[0, 0], [1, 0], [0, 1], [2, 1], [1, 3]], dtype=float)
    y = 4 + 2 * X[:, 0] - 3 * X[:, 1]
    model = MultipleLinearRegression().fit(X, y)
    assert model.intercept_ == pytest.approx(4, abs=1e-12)
    np.testing.assert_allclose(model.coef_, [2, -3], atol=1e-12)
    np.testing.assert_allclose(model.predict([[3, 2]]), [4], atol=1e-12)
    model.save(tmp_path / "linear.json")
    np.testing.assert_array_equal(
        model.predict(X),
        MultipleLinearRegression.load(tmp_path / "linear.json").predict(X),
    )


def test_singular_solution_matches_independent_lstsq_and_exposes_rank():
    X = np.array([[1, 2], [2, 4], [3, 6], [4, 8]], dtype=float)
    y = np.array([2, 5, 5, 8], dtype=float)
    model = MultipleLinearRegression().fit(X, y)
    design = np.column_stack([np.ones(len(X)), X])
    beta = np.linalg.lstsq(design, y, rcond=None)[0]
    np.testing.assert_allclose(model.predict(X), design @ beta, atol=1e-12)
    assert model.rank_ == 2 and not model.full_rank_
    assert (
        regression_metrics(y, model.predict(X), in_sample=True, p=2, full_rank=False)[
            "adjusted_r2"
        ]
        is None
    )


@pytest.mark.parametrize(
    "X,y",
    [
        ([[1, float("nan")]], [1]),
        ([[1, 2]], [float("inf")]),
        ([1, 2], [1, 2]),
        ([[1], [2]], [1]),
        ([], []),
    ],
)
def test_regression_rejects_invalid_input(X, y):
    with pytest.raises(ValueError):
        MultipleLinearRegression().fit(X, y)


def test_prediction_requires_fit_and_matching_features():
    with pytest.raises(RuntimeError):
        MultipleLinearRegression().predict([[1]])
    model = MultipleLinearRegression().fit([[1], [2]], [2, 3])
    with pytest.raises(ValueError):
        model.predict([[1, 2]])


def test_transformer_uses_train_stats_unknown_mean_effect_and_roundtrip():
    train = pd.DataFrame(
        {"lap_number": [2, 4, 6, np.nan], "driver_name": ["A", "A", "B", "B"]}
    )
    transformer = FeatureTransformer(["lap_number"], ["driver_name"]).fit(train)
    state = transformer.to_dict()
    test = pd.DataFrame({"lap_number": [100, np.nan], "driver_name": ["NEW", "A"]})
    encoded = transformer.transform(test)
    assert transformer.medians_[0] == 4 and transformer.means_[0] == 4
    assert encoded[0, 1] == 0  # Unknown driver: train weighted average effect.
    assert encoded[1, 0] == 0  # Missing numeric: train median then train scale.
    assert transformer.to_dict() == state
    np.testing.assert_array_equal(
        encoded, FeatureTransformer.from_dict(state).transform(test)
    )
    assert transformer.unknown_counts(test) == {"driver_name": 1}
    assert "NEW" not in transformer.categories_["driver_name"]


def test_constant_numeric_is_scaled_safely_and_forbidden_features_rejected():
    t = FeatureTransformer(["lap_number"], ["driver_name"]).fit(
        pd.DataFrame({"lap_number": [2, 2], "driver_name": ["A", "A"]})
    )
    assert t.scales_[0] == 1
    np.testing.assert_array_equal(
        t.transform(pd.DataFrame({"lap_number": [2], "driver_name": ["NEW"]})), [[0]]
    )
    with pytest.raises(ValueError):
        FeatureTransformer(["lap_duration"], ["driver_name"])


def test_baselines_use_available_history_and_frozen_fallback():
    frame = pd.DataFrame(
        {
            "correct_history": [
                [],
                [{"lap_duration": 80}],
                [{"lap_duration": 82}, {"lap_duration": 80}],
            ]
        }
    )
    predictions = baseline_predictions(frame, 81)
    np.testing.assert_array_equal(predictions["last_correct"], [81, 80, 82])
    np.testing.assert_array_equal(predictions["mean_last3_correct"], [81, 80, 81])


def test_metrics_known_solution_negative_r2_and_adjusted_applicability():
    metrics = regression_metrics([1, 2, 3], [2, 2, 2], in_sample=True, p=0)
    assert metrics["mae_seconds"] == pytest.approx(2 / 3)
    assert metrics["rmse_seconds"] == pytest.approx(np.sqrt(2 / 3))
    assert metrics["r2"] == 0 and metrics["adjusted_r2"] == 0
    assert regression_metrics([1, 2, 3], [10, 10, 10])["r2"] < 0
    assert regression_metrics([1, 1], [1, 1])["r2"] is None
    assert (
        regression_metrics([1, 2], [1, 2], in_sample=True, p=1)["adjusted_r2"] is None
    )
    assert regression_metrics([1, 2, 3], [1, 2, 3])["adjusted_r2"] is None


@pytest.fixture
def model_database(test_url):
    with connect(test_url) as conn:
        seed(conn)
        conn.execute(
            "UPDATE f1.sessions SET year=2023,date_start=date_start-interval '1 year',date_end=date_end-interval '1 year' WHERE session_key=101"
        )
        conn.execute(
            "UPDATE f1.laps SET date_start=date_start-interval '1 year' WHERE session_key=101"
        )
        conn.execute(
            "UPDATE f1.weather SET date=date-interval '1 year' WHERE session_key=101"
        )
        conn.execute(
            "UPDATE f1.race_control SET date=date-interval '1 year' WHERE session_key=101"
        )
        batch = conn.execute(
            "SELECT source_batch_id FROM f1.sessions WHERE session_key=102"
        ).fetchone()["source_batch_id"]
        session = conn.execute(
            "SELECT * FROM f1.sessions WHERE session_key=102"
        ).fetchone()
        session = {
            k: v
            for k, v in session.items()
            if k not in ("source_batch_id", "raw_record")
        }
        session.update(
            session_key=103,
            year=2025,
            date_start=session["date_start"] + timedelta(days=365),
            date_end=session["date_end"] + timedelta(days=365),
        )
        upsert(conn, "sessions", batch, json.loads(json.dumps([session], default=str)))
        for table in ["drivers", "laps", "stints", "weather", "race_control"]:
            rows = conn.execute(
                sql.SQL("SELECT * FROM f1.{} WHERE session_key=102").format(
                    sql.Identifier(table)
                )
            ).fetchall()
            for row in rows:
                for key in ["source_batch_id", "raw_record", "event_id"]:
                    row.pop(key, None)
                row["session_key"] = 103
                for key in ["date", "date_start"]:
                    if row.get(key) is not None:
                        row[key] += timedelta(days=365)
            upsert(conn, table, batch, json.loads(json.dumps(rows, default=str)))
    prepare_dataset(test_url, years=(2023, 2024, 2025))
    return test_url


@pytest.mark.integration
def test_sql_baseline_is_correct_completed_history_not_raw_pit_or_future(
    model_database,
):
    with connect(model_database) as conn:
        frame, _ = read_laps(conn, [102])
    row = frame[(frame.driver_number == 1) & (frame.lap_number == 5)].iloc[0]
    # Lap 3 is pit-in, 4 pit-out. Both are forbidden baseline history.
    # Latest remaining completed correct lap is 2, not the stage-2 raw lag 3.
    assert [h["lap_number"] for h in row.correct_history] == [2]
    assert row.correct_history[0]["session_key"] == 102
    assert row.correct_history[0]["driver_number"] == 1
    first = frame[(frame.driver_number == 1) & (frame.lap_number == 2)].iloc[0]
    assert first.correct_history == []


@pytest.mark.integration
def test_training_does_not_read_test_and_evaluation_never_fits(
    model_database, tmp_path, monkeypatch
):
    import f1_pipeline.experiment as exp

    actual_read = exp.read_laps
    called = []

    def tracked_read(conn, ids):
        called.append(tuple(ids))
        return actual_read(conn, ids)

    monkeypatch.setattr(exp, "read_laps", tracked_read)
    manifest = train_experiment(tmp_path / "experiment", model_database)
    assert called == [(101,), (102,)]
    assert manifest["test_targets_read_during_training"] is False
    assert manifest["split"] == {"train": [101], "validation": [102], "test": [103]}
    assert all(x == 0 for x in manifest["serialization_max_abs_difference"].values())

    def forbidden_fit(*args, **kwargs):
        raise AssertionError("Fit called after opening final test")

    monkeypatch.setattr(LapTimeModel, "fit", forbidden_fit)
    result = evaluate_experiment(
        tmp_path / "experiment", tmp_path / "report", model_database
    )
    assert called[-1] == (103,)
    assert result["selected_model"] == manifest["selected_model"]
    counts = {m["n"] for m in result["test_metrics"].values()}
    assert len(counts) == 1
    second = evaluate_experiment(
        tmp_path / "experiment", tmp_path / "report", model_database
    )
    assert second["test_metrics"] == result["test_metrics"]
    prediction = predict_lap(tmp_path / "experiment", 103, 1, 5, model_database)
    assert np.isfinite(prediction["prediction_seconds"])
    # Existing evaluated experiment returns its frozen selection, not a new fit.
    assert (
        train_experiment(tmp_path / "experiment", model_database)["selected_model"]
        == manifest["selected_model"]
    )
