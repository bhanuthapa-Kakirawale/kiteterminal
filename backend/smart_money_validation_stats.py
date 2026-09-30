"""PK-25 research statistics: small, transparent numpy implementations.

Research only — imported by backend.smart_money_validation and its tests,
never by the scanner, strategies, risk, execution or any route. There is
deliberately no scipy/sklearn: every estimator here is a few lines that can
be read and checked by hand, and nothing is persisted (no model artifact).

Conventions
-----------
* Missing values are NaN. Paired statistics drop a pair if either side is NaN.
* A statistic that is undefined (too few values, zero variance) is None —
  never 0, which would read as "no relationship".
* Randomness only enters through `cluster_bootstrap`, which takes an explicit
  seed and is therefore deterministic.
"""

import math

import numpy as np


def as_float(values) -> np.ndarray:
    return np.asarray(values, dtype=float)


def rankdata(values) -> np.ndarray:
    """Average ranks (1-based); ties share the mean of their ranks."""
    x = as_float(values)
    if not len(x):
        return x
    sorter = np.argsort(x, kind="mergesort")
    inverse = np.empty(len(x), dtype=np.intp)
    inverse[sorter] = np.arange(len(x))
    ordered = x[sorter]
    first = np.r_[True, ordered[1:] != ordered[:-1]]
    dense = first.cumsum()[inverse]
    bounds = np.r_[np.flatnonzero(first), len(x)]
    return 0.5 * (bounds[dense] + bounds[dense - 1] + 1)


def _paired(x, y) -> tuple[np.ndarray, np.ndarray]:
    x, y = as_float(x), as_float(y)
    keep = ~(np.isnan(x) | np.isnan(y))
    return x[keep], y[keep]


def pearson(x, y) -> float | None:
    x, y = _paired(x, y)
    if len(x) < 3:
        return None
    dx, dy = x - x.mean(), y - y.mean()
    denom = math.sqrt(float((dx * dx).sum()) * float((dy * dy).sum()))
    if denom == 0:
        return None
    return float((dx * dy).sum() / denom)


def spearman(x, y) -> float | None:
    """Spearman rank correlation = Pearson on average ranks (handles ties)."""
    x, y = _paired(x, y)
    if len(x) < 3:
        return None
    return pearson(rankdata(x), rankdata(y))


def paired_n(x, y) -> int:
    return len(_paired(x, y)[0])


def fisher_p_two_sided(rho: float | None, n: int) -> float | None:
    """Naive two-sided p-value for a correlation via Fisher's z. It ASSUMES
    INDEPENDENT observations, which overlapping signal windows are not — it
    is reported for reference only; the bootstrap interval is primary."""
    if rho is None or n < 4 or abs(rho) >= 1:
        return None
    z = math.atanh(rho) * math.sqrt(n - 3)
    return math.erfc(abs(z) / math.sqrt(2))


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float | None, float | None]:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def median(values) -> float | None:
    x = as_float(values)
    x = x[~np.isnan(x)]
    return float(np.median(x)) if len(x) else None


def mean(values) -> float | None:
    x = as_float(values)
    x = x[~np.isnan(x)]
    return float(x.mean()) if len(x) else None


def trimmed_mean(values, proportion: float = 0.1) -> float | None:
    """Mean after dropping floor(proportion * n) values from each end."""
    x = np.sort(as_float(values))
    x = x[~np.isnan(x)]
    if not len(x):
        return None
    cut = int(math.floor(proportion * len(x)))
    kept = x[cut: len(x) - cut] if len(x) - 2 * cut > 0 else x
    return float(kept.mean())


def winsorize(values, lower: float, upper: float) -> np.ndarray:
    """A clipped COPY; the input is never modified."""
    return np.clip(as_float(values).copy(), lower, upper)


def quantile(values, q: float) -> float | None:
    x = as_float(values)
    x = x[~np.isnan(x)]
    return float(np.quantile(x, q)) if len(x) else None


def profit_factor(values) -> float | None:
    x = as_float(values)
    x = x[~np.isnan(x)]
    losses = -x[x <= 0].sum()
    if not len(x) or losses <= 0:
        return None
    return float(x[x > 0].sum() / losses)


def win_rate(values) -> tuple[int, int]:
    """(wins, n) where a win is a value > 0; NaN is not counted."""
    x = as_float(values)
    x = x[~np.isnan(x)]
    return int((x > 0).sum()), int(len(x))


# --------------------------------------------------------------------------
# Cluster bootstrap
# --------------------------------------------------------------------------

def cluster_bootstrap(clusters, statistic, reps: int, seed: int) -> np.ndarray:
    """Resample whole clusters with replacement `reps` times and return the
    statistic of each replicate (NaN where it is undefined).

    `clusters` labels each row (here: the ISO week of the signal), so rows
    that share market days — overlapping forward windows, consecutive signals
    of one symbol — are resampled together. `statistic(indices)` receives
    the row indices of one replicate. Deterministic for a given seed."""
    labels = np.asarray(clusters)
    unique = sorted(set(labels.tolist()))
    members = [np.flatnonzero(labels == u) for u in unique]
    k = len(members)
    out = np.full(reps, np.nan)
    if k == 0:
        return out
    rng = np.random.default_rng(seed)
    for r in range(reps):
        chosen = rng.integers(0, k, k)
        idx = np.concatenate([members[c] for c in chosen])
        value = statistic(idx)
        out[r] = np.nan if value is None else value
    return out


def interval(replicates: np.ndarray, level: float = 0.95) -> tuple[float | None, float | None]:
    x = replicates[~np.isnan(replicates)]
    if len(x) < 20:
        return None, None
    tail = (1 - level) / 2
    return float(np.quantile(x, tail)), float(np.quantile(x, 1 - tail))


def one_sided_p(replicates: np.ndarray, greater: bool = True) -> float | None:
    """Bootstrap one-sided p for H: statistic > 0 (or < 0): the share of
    replicates on the wrong side of zero, with the +1 correction."""
    x = replicates[~np.isnan(replicates)]
    if len(x) < 20:
        return None
    wrong = (x <= 0).sum() if greater else (x >= 0).sum()
    return float((wrong + 1) / (len(x) + 1))


def holm(pvalues: dict) -> dict:
    """Holm-Bonferroni adjusted p-values for a dict {name: p or None}."""
    known = sorted(((p, name) for name, p in pvalues.items() if p is not None))
    m = len(known)
    adjusted, running = {}, 0.0
    for rank, (p, name) in enumerate(known):
        running = max(running, min(1.0, (m - rank) * p))
        adjusted[name] = running
    return {name: adjusted.get(name) for name in pvalues}


# --------------------------------------------------------------------------
# Research regressions (diagnostic only; nothing is persisted)
# --------------------------------------------------------------------------

def add_intercept(x: np.ndarray) -> np.ndarray:
    x = as_float(x)
    if x.ndim == 1:
        x = x.reshape(-1, 1)
    return np.hstack([np.ones((x.shape[0], 1)), x])


def ols_fit(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Least-squares coefficients; `x` already carries the intercept column."""
    coef, *_ = np.linalg.lstsq(as_float(x), as_float(y), rcond=None)
    return coef


def logistic_fit(x: np.ndarray, y: np.ndarray, ridge: float = 1e-3, max_iter: int = 100,
                 tol: float = 1e-10) -> np.ndarray:
    """Logistic regression by IRLS (Newton), with a small FIXED ridge on the
    slopes (not the intercept, column 0) so separable folds stay finite. The
    ridge is a numerical safeguard, not a tuned hyper-parameter."""
    x, y = as_float(x), as_float(y)
    beta = np.zeros(x.shape[1])
    penalty = np.full(x.shape[1], ridge)
    penalty[0] = 0.0
    for _ in range(max_iter):
        p = 1 / (1 + np.exp(-np.clip(x @ beta, -35, 35)))
        w = p * (1 - p)
        grad = x.T @ (y - p) - penalty * beta
        hess = (x.T * w) @ x + np.diag(penalty)
        step = np.linalg.solve(hess + 1e-12 * np.eye(len(beta)), grad)
        beta = beta + step
        if float(np.abs(step).max()) < tol:
            break
    return beta


def logistic_predict(x: np.ndarray, beta: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(as_float(x) @ beta, -35, 35)))


def auc(scores, labels) -> float | None:
    """Area under the ROC curve via the Mann-Whitney U statistic (ties 0.5)."""
    s, y = _paired(scores, labels)
    pos, neg = int((y == 1).sum()), int((y == 0).sum())
    if pos == 0 or neg == 0:
        return None
    ranks = rankdata(s)
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))


def log_loss(p, y) -> float | None:
    p, y = _paired(p, y)
    if not len(p):
        return None
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def brier(p, y) -> float | None:
    p, y = _paired(p, y)
    return float(((p - y) ** 2).mean()) if len(p) else None
