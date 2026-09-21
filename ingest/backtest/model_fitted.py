"""Backtest adapter for the fitted model (`fitted-0.1`, `ingest/xp/fitted.py`).

Thin on purpose: the model's own `fit(asof)` / `predict(asof)` are the backtest
interface already, and production calls the same two functions on an `AsOf` it
builds for today (`xp.fitted_live`). The only backtest-specific choice is
availability, which is neutral here — no historical injury data exists — and
live in production.
"""

from __future__ import annotations

from typing import Any

from xp.fitted import DEFAULT_CONFIG, FittedConfig, FittedXPModel

from .data import AsOf


class FittedModel:
    """`fitted-0.1` with neutral availability."""

    def __init__(self, config: FittedConfig = DEFAULT_CONFIG, name: str = "fitted"):
        self.name = name
        self.model = FittedXPModel(config)

    def fit(self, asof: AsOf) -> None:
        self.model.fit(asof)

    def predict(self, asof: AsOf) -> dict[int, float]:
        return {r.element_id: r.xp for r in self.model.predict(asof)}

    def describe(self) -> dict[str, Any] | None:
        return self.model.params.describe() if self.model.params else None
