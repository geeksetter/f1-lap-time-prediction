"""Error in seconds; classical adjusted R² is restricted to in-sample OLS."""

import numpy as np


def regression_metrics(actual, predicted, *, in_sample=False, p=None, full_rank=True):
    y, estimate = np.asarray(actual, dtype=float), np.asarray(predicted, dtype=float)
    if (
        y.ndim != 1
        or estimate.shape != y.shape
        or len(y) == 0
        or not np.isfinite(y).all()
        or not np.isfinite(estimate).all()
    ):
        raise ValueError("Metrics require equal nonempty finite vectors")
    residual = y - estimate
    n = len(y)
    sse = float(residual @ residual)
    centered = y - y.mean()
    sst = float(centered @ centered)
    r2 = float(1 - sse / sst) if n >= 2 and sst > 0 else None
    adjusted = None
    reason = "holdout/group: classical adjusted R2 is an in-sample OLS statistic"
    if in_sample:
        if p is None or p < 0 or n <= p + 1:
            reason = "not applicable: n must exceed p+1 (p excludes intercept)"
        elif not full_rank:
            reason = "not applicable: rank-deficient design"
        elif r2 is None:
            reason = "not applicable: constant target or fewer than two observations"
        else:
            adjusted = float(1 - (1 - r2) * (n - 1) / (n - p - 1))
            reason = "in-sample full-rank OLS; descriptive only, dependent laps preclude inference"
    return {
        "n": n,
        "mae_seconds": float(np.abs(residual).mean()),
        "rmse_seconds": float(np.sqrt(sse / n)),
        "r2": r2,
        "adjusted_r2": adjusted,
        "adjusted_r2_note": reason,
        "bias_seconds": float((estimate - y).mean()),
        "target_std_seconds": float(y.std(ddof=0)),
    }
