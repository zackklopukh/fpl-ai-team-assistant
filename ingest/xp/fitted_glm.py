"""The two small regressions the fitted model is made of, in plain numpy.

Every fitted relationship in `fitted.py` is either a logistic regression (a
probability: starting, coming off the bench, reaching the defensive-contribution
threshold) or a Poisson regression (a rate: xG per 90 given price, bonus given
expected returns). Both are a few lines of iteratively reweighted least squares.
Writing them out keeps the model to numpy and pandas — the ingest jobs do not
otherwise need scikit-learn or scipy — and keeps every coefficient a plain,
printable number with a name attached.

A small ridge penalty (never on the intercept) keeps the fit stable when a
feature is nearly constant, as some are early in a season.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Glm:
    """A fitted GLM: named coefficients over standardised features.

    Features are centred and scaled at fit time and the same transform is applied
    at prediction, so the ridge penalty treats every feature alike. `coef` is on
    the standardised scale; `raw_coefficients` converts back for reading.
    """

    names: tuple[str, ...]
    intercept: float
    coef: np.ndarray
    mean: np.ndarray
    scale: np.ndarray
    link: str  # "logit" or "log"

    def linear(self, x: np.ndarray) -> np.ndarray:
        z = (np.asarray(x, dtype=float) - self.mean) / self.scale
        return self.intercept + z @ self.coef

    def predict(self, x: np.ndarray) -> np.ndarray:
        eta = self.linear(x)
        if self.link == "logit":
            return 1.0 / (1.0 + np.exp(-np.clip(eta, -30, 30)))
        return np.exp(np.clip(eta, -30, 30))

    def raw_coefficients(self) -> dict[str, float]:
        """Coefficients per unit of each raw feature, plus the intercept."""
        per_unit = self.coef / self.scale
        out = {"intercept": float(self.intercept - (per_unit * self.mean).sum())}
        out.update({n: float(c) for n, c in zip(self.names, per_unit, strict=True)})
        return out


def fit_glm(
    x: np.ndarray,
    y: np.ndarray,
    names: tuple[str, ...],
    link: str,
    weight: np.ndarray | None = None,
    offset: np.ndarray | None = None,
    ridge: float = 1.0,
    iterations: int = 25,
) -> Glm:
    """Fit a logistic (`link="logit"`) or Poisson (`link="log"`) regression by IRLS.

    For the Poisson case `y` may be a non-integer rate and `offset` the log of the
    exposure; the fit is the quasi-likelihood solution, which is what an xG rate
    (not a count) needs. `ridge` is in units of observations' worth of penalty.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n, p = x.shape
    w0 = np.ones(n) if weight is None else np.asarray(weight, dtype=float)
    off = np.zeros(n) if offset is None else np.asarray(offset, dtype=float)
    mean = (w0[:, None] * x).sum(0) / w0.sum()
    scale = np.sqrt((w0[:, None] * (x - mean) ** 2).sum(0) / w0.sum())
    scale[scale < 1e-9] = 1.0
    z = np.column_stack([np.ones(n), (x - mean) / scale])
    beta = np.zeros(p + 1)
    if link == "log":
        rate = (w0 * y).sum() / max((w0 * np.exp(off)).sum(), 1e-12)
        beta[0] = np.log(max(rate, 1e-9))
    else:
        prior = np.clip((w0 * y).sum() / w0.sum(), 1e-4, 1 - 1e-4)
        beta[0] = np.log(prior / (1 - prior))
    penalty = np.eye(p + 1) * ridge
    penalty[0, 0] = 0.0
    for _ in range(iterations):
        eta = np.clip(z @ beta + off, -30, 30)
        if link == "logit":
            mu = 1.0 / (1.0 + np.exp(-eta))
            var = np.maximum(mu * (1 - mu), 1e-9)
        else:
            mu = np.exp(eta)
            var = np.maximum(mu, 1e-12)
        # Working response and weights; the step is a penalised weighted least squares.
        ww = w0 * var
        resid = (y - mu) / var
        grad = z.T @ (w0 * var * resid) - penalty @ beta
        hess = (z * ww[:, None]).T @ z + penalty
        step = np.linalg.solve(hess, grad)
        beta = beta + step
        if np.abs(step).max() < 1e-8:
            break
    return Glm(
        names=tuple(names),
        intercept=float(beta[0]),
        coef=beta[1:],
        mean=mean,
        scale=scale,
        link=link,
    )


def poisson_floor_mean(lam: np.ndarray, per: int, terms: int = 12) -> np.ndarray:
    """E[floor(X / per)] for X ~ Poisson(lam), elementwise.

    Saves score one point per three and goals conceded cost one per two, so the
    expected points are a sum of tail probabilities: `sum_j P(X >= j * per)`.
    Written without scipy; `terms` covers far more saves or goals than a match has.
    """
    lam = np.maximum(np.asarray(lam, dtype=float), 0.0)
    top = per * terms
    # pmf by recurrence, up to top; P(X >= m) = 1 - cdf(m - 1).
    pmf = np.exp(-lam)[:, None] * np.ones((1, top + 1))
    for i in range(1, top + 1):
        pmf[:, i] = pmf[:, i - 1] * lam / i
    cdf = np.cumsum(pmf, axis=1)
    out = np.zeros_like(lam)
    for j in range(1, terms + 1):
        out += np.clip(1.0 - cdf[:, j * per - 1], 0.0, 1.0)
    return out
