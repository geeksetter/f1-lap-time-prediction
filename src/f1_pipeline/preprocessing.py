"""Train-only fitted transformations; explicit feature whitelist and JSON state."""

import numpy as np
import pandas as pd

from .regression import MultipleLinearRegression

FEATURE_SETS = {
    "base": {"numeric": ["lap_number"], "categorical": ["driver_name"]},
    "tyres": {
        "numeric": ["lap_number", "tyre_age_before_lap"],
        "categorical": ["driver_name", "compound"],
    },
    "weather": {
        "numeric": [
            "lap_number",
            "tyre_age_before_lap",
            "air_temperature",
            "track_temperature",
        ],
        "categorical": ["driver_name", "compound"],
    },
}
ALLOWED = {
    "lap_number",
    "tyre_age_before_lap",
    "air_temperature",
    "track_temperature",
    "driver_name",
    "compound",
}


class FeatureTransformer:
    """Median imputation and z-scales; centered K-1 one-hot contrasts.

    Unknown category -> zero contrasts: the train-frequency-weighted mean effect.
    Known category j -> I(category=j)-train_frequency(j), for all but last category.
    The last known category has all contrasts -frequency(j). No test category fit.
    """

    def __init__(self, numeric, categorical):
        self.numeric, self.categorical = list(numeric), list(categorical)
        features = self.numeric + self.categorical
        if (
            not features
            or len(features) != len(set(features))
            or not set(features) <= ALLOWED
        ):
            raise ValueError("Invalid or forbidden model feature list")
        self.fitted = False

    def _check(self, frame):
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise ValueError("Features must be a nonempty DataFrame")
        missing = set(self.numeric + self.categorical) - set(frame.columns)
        if missing:
            raise ValueError(f"Missing feature columns: {sorted(missing)}")
        values = frame[self.numeric].to_numpy(dtype=float)
        if np.isinf(values).any():
            raise ValueError("Infinite numeric feature")
        if frame[self.categorical].isna().any().any():
            raise ValueError("Missing category; supply an explicit category value")
        if any(
            not isinstance(x, str) or not x
            for name in self.categorical
            for x in frame[name]
        ):
            raise ValueError("Categories must be nonempty strings")
        return values

    def fit(self, frame):
        values = self._check(frame)
        if np.isnan(values).all(axis=0).any():
            raise ValueError("Cannot fit an entirely missing numeric feature")
        self.medians_ = np.nanmedian(values, axis=0)
        filled = np.where(np.isnan(values), self.medians_, values)
        self.means_ = filled.mean(axis=0)
        scales = filled.std(axis=0, ddof=0)
        self.scales_ = np.where(scales == 0, 1.0, scales)
        self.minima_, self.maxima_ = filled.min(axis=0), filled.max(axis=0)
        self.categories_ = {
            name: sorted(frame[name].unique().tolist()) for name in self.categorical
        }
        self.frequencies_ = {
            name: [float((frame[name] == level).mean()) for level in levels]
            for name, levels in self.categories_.items()
        }
        self.output_names_ = self.numeric + [
            f"{name}={level} (centered)"
            for name in self.categorical
            for level in self.categories_[name][:-1]
        ]
        self.fitted = True
        return self

    def transform(self, frame):
        if not self.fitted:
            raise RuntimeError("Call fit before transform")
        values = self._check(frame)
        filled = np.where(np.isnan(values), self.medians_, values)
        blocks = [(filled - self.means_) / self.scales_]
        for name in self.categorical:
            known = frame[name].isin(self.categories_[name]).to_numpy()
            levels = self.categories_[name][:-1]
            if levels:
                columns = np.column_stack(
                    [
                        (frame[name] == level).to_numpy(dtype=float) - frequency
                        for level, frequency in zip(
                            levels, self.frequencies_[name][:-1]
                        )
                    ]
                )
                columns[~known] = 0.0
                blocks.append(columns)
        result = np.column_stack(blocks)
        if not np.isfinite(result).all():
            raise ValueError("Nonfinite transformed input")
        return result

    def unknown_counts(self, frame):
        self._check(frame)
        return {
            name: int((~frame[name].isin(self.categories_[name])).sum())
            for name in self.categorical
        }

    def to_dict(self):
        if not self.fitted:
            raise RuntimeError("Unfitted transformer")
        return {
            "version": 1,
            "numeric": self.numeric,
            "categorical": self.categorical,
            "medians": self.medians_.tolist(),
            "means": self.means_.tolist(),
            "scales": self.scales_.tolist(),
            "minima": self.minima_.tolist(),
            "maxima": self.maxima_.tolist(),
            "categories": self.categories_,
            "frequencies": self.frequencies_,
            "output_names": self.output_names_,
            "encoding": "centered_train_frequency_k_minus_one",
            "unknown_category": "zero_contrasts_train_weighted_average_effect",
        }

    @classmethod
    def from_dict(cls, state):
        if (
            state.get("version") != 1
            or state.get("encoding") != "centered_train_frequency_k_minus_one"
        ):
            raise ValueError("Unsupported transformer format")
        obj = cls(state["numeric"], state["categorical"])
        obj.medians_ = np.asarray(state["medians"], dtype=float)
        obj.means_ = np.asarray(state["means"], dtype=float)
        obj.scales_ = np.asarray(state["scales"], dtype=float)
        obj.minima_, obj.maxima_ = (
            np.asarray(state["minima"], dtype=float),
            np.asarray(state["maxima"], dtype=float),
        )
        if (
            any(
                a.shape != (len(obj.numeric),) or not np.isfinite(a).all()
                for a in (
                    obj.medians_,
                    obj.means_,
                    obj.scales_,
                    obj.minima_,
                    obj.maxima_,
                )
            )
            or (obj.scales_ <= 0).any()
            or (obj.minima_ > obj.maxima_).any()
        ):
            raise ValueError("Corrupt numeric transformation state")
        obj.categories_, obj.frequencies_ = state["categories"], state["frequencies"]
        for name in obj.categorical:
            levels, freq = obj.categories_[name], obj.frequencies_[name]
            if (
                not levels
                or len(levels) != len(set(levels))
                or len(levels) != len(freq)
                or not np.isfinite(freq).all()
                or min(freq) <= 0
                or not np.isclose(sum(freq), 1)
            ):
                raise ValueError("Corrupt category transformation state")
        obj.output_names_ = state["output_names"]
        expected = obj.numeric + [
            f"{name}={level} (centered)"
            for name in obj.categorical
            for level in obj.categories_[name][:-1]
        ]
        if obj.output_names_ != expected:
            raise ValueError("Corrupt output feature names")
        obj.fitted = True
        return obj


class LapTimeModel:
    """Own regression plus its persisted, train-fitted feature transformations."""

    def __init__(self, feature_set):
        if feature_set not in FEATURE_SETS:
            raise ValueError("Unknown feature set")
        self.feature_set = feature_set
        self.transformer = FeatureTransformer(**FEATURE_SETS[feature_set])
        self.regressor = MultipleLinearRegression()

    def fit(self, frame):
        self.transformer.fit(frame)
        self.regressor.fit(
            self.transformer.transform(frame), frame.lap_duration.to_numpy()
        )
        return self

    def predict(self, frame):
        return self.regressor.predict(self.transformer.transform(frame))

    def to_dict(self):
        return {
            "format_version": 1,
            "feature_set": self.feature_set,
            "transformer": self.transformer.to_dict(),
            "regressor": self.regressor.to_dict(),
        }

    def save(self, path):
        import json
        from pathlib import Path

        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)
            + "\n"
        )

    @classmethod
    def load(cls, path):
        import json
        from pathlib import Path

        state = json.loads(Path(path).read_text())
        if state.get("format_version") != 1:
            raise ValueError("Unsupported model bundle")
        obj = cls(state["feature_set"])
        obj.transformer = FeatureTransformer.from_dict(state["transformer"])
        obj.regressor = MultipleLinearRegression.from_dict(state["regressor"])
        if len(obj.transformer.output_names_) != obj.regressor.n_features_:
            raise ValueError("Transformer/regression size mismatch")
        if (
            obj.transformer.numeric != FEATURE_SETS[obj.feature_set]["numeric"]
            or obj.transformer.categorical
            != FEATURE_SETS[obj.feature_set]["categorical"]
        ):
            raise ValueError("Bundle feature-set mismatch")
        return obj
