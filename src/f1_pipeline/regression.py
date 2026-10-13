"""Own ordinary least-squares regression. NumPy supplies linear algebra only."""

import json
from pathlib import Path

import numpy as np


class MultipleLinearRegression:
    """Minimize ||[1, X] beta - y||² using an explicitly implemented SVD solve.

    No inversion of X.T @ X, no sklearn model, no iterative stopping/hyperparameters.
    Rank-deficient designs use the minimum-norm solution and expose diagnostics.
    """

    def __init__(self):
        self.fitted = False

    @staticmethod
    def _matrix(X):
        matrix = np.asarray(X, dtype=float)
        if matrix.ndim != 2 or not len(matrix) or not np.isfinite(matrix).all():
            raise ValueError("X must be a nonempty finite 2D numeric matrix")
        return matrix

    def fit(self, X, y):
        X = self._matrix(X)
        y = np.asarray(y, dtype=float)
        if y.ndim != 1 or len(y) != len(X) or not np.isfinite(y).all():
            raise ValueError("y must be a finite vector with one value per row")
        design = np.column_stack([np.ones(len(X)), X])
        left, singular, right = np.linalg.svd(design, full_matrices=False)
        threshold = np.finfo(float).eps * max(design.shape) * singular[0]
        retained = singular > threshold
        inverse = np.divide(1.0, singular, out=np.zeros_like(singular), where=retained)
        beta = (right.T * inverse) @ (left.T @ y)
        rank = int(retained.sum())
        self.intercept_ = float(beta[0])
        self.coef_ = beta[1:]
        self.n_features_ = X.shape[1]
        self.n_samples_ = len(X)
        self.rank_ = int(rank)
        self.singular_values_ = singular.tolist()
        self.full_rank_ = rank == design.shape[1]
        self.condition_number_ = (
            float(singular[0] / singular[-1])
            if self.full_rank_ and singular[-1] > 0
            else None
        )
        self.fitted = True
        return self

    def predict(self, X):
        if not self.fitted:
            raise RuntimeError("Call fit before predict")
        X = self._matrix(X)
        if X.shape[1] != self.n_features_:
            raise ValueError("Predict matrix has a different number of features")
        result = self.intercept_ + X @ self.coef_
        if not np.isfinite(result).all():
            raise ValueError("Predictions are not finite")
        return result

    def to_dict(self):
        if not self.fitted:
            raise RuntimeError("Cannot save an unfitted model")
        return {
            "type": "MultipleLinearRegression",
            "version": 1,
            "algorithm": "ordinary_least_squares_explicit_svd_pseudoinverse_eps_max_shape",
            "intercept": self.intercept_,
            "coefficients": self.coef_.tolist(),
            "n_features": self.n_features_,
            "n_samples": self.n_samples_,
            "rank": self.rank_,
            "full_rank": self.full_rank_,
            "singular_values": self.singular_values_,
            "condition_number": self.condition_number_,
        }

    @classmethod
    def from_dict(cls, state):
        if state.get("type") != "MultipleLinearRegression" or state.get("version") != 1:
            raise ValueError("Unsupported model format")
        model = cls()
        coefficients = np.asarray(state["coefficients"], dtype=float)
        if (
            coefficients.ndim != 1
            or len(coefficients) != state["n_features"]
            or not np.isfinite(coefficients).all()
            or not np.isfinite(state["intercept"])
        ):
            raise ValueError("Corrupt model coefficients")
        model.coef_, model.intercept_ = coefficients, float(state["intercept"])
        model.n_features_, model.n_samples_ = state["n_features"], state["n_samples"]
        model.rank_, model.full_rank_ = state["rank"], state["full_rank"]
        model.singular_values_ = state["singular_values"]
        model.condition_number_ = state["condition_number"]
        model.fitted = True
        return model

    def save(self, path):
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, allow_nan=False) + "\n"
        )

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text()))
