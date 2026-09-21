"""Backtest adapter for the gradient-boosting model (`gbm-0.1`).

Thin on purpose: the backtest must evaluate the code production runs, so this
only decides *when* to refit and passes neutral availability. Everything else is
`xp.gbm.GbmXpModel`, called exactly as `xp.gbm.compute_live` calls it.

Refit cadence. The harness calls `fit` before every target by default (the
nightly job's cadence). A full refit takes seconds, which over ~120 targets
makes a run slow for no measurable gain, so this refits every `refit_every`
targets and always at the first target of a season (new element ids, and a
whole summer of transfers). Between refits `fit` is a no-op and `predict`
rebuilds features from the new view, so predictions always use the latest
results — only the trees are a few weeks old.
"""

from __future__ import annotations

from typing import Any

from xp.gbm import GbmConfig, GbmXpModel

from .data import AsOf

DEFAULT_REFIT_EVERY = 4


class GbmModel:
    name = "gbm"

    def __init__(
        self,
        config: GbmConfig | None = None,
        refit_every: int = DEFAULT_REFIT_EVERY,
        name: str | None = None,
    ):
        self.model = GbmXpModel(config)
        self.refit_every = refit_every
        if name:
            self.name = name
        self._last_fit: tuple[str, int] | None = None
        self.fit_log: list[dict[str, Any]] = []

    def _due(self, asof: AsOf) -> bool:
        if self._last_fit is None or self._last_fit[0] != asof.season:
            return True
        return asof.gw - self._last_fit[1] >= self.refit_every

    def fit(self, asof: AsOf) -> None:
        if not self._due(asof):
            return
        self.model.fit(asof)
        self._last_fit = (asof.season, asof.gw)
        self.fit_log.append(
            {"season": asof.season, "gw": asof.gw, "rows": self.model.info.rows,
             "seconds": round(self.model.info.seconds, 2)}
        )

    def predict(self, asof: AsOf) -> dict[int, float]:
        # Backtest availability is neutral: no historical injury data exists.
        return self.model.predict(asof, availability=None)
