"""The baseline expected-points model.

`features` decides what the model is allowed to know (and, as of which gameweek);
`model` turns that into points. Keeping them apart is what makes the lookahead
rule enforceable rather than aspirational.
"""

from .features import (
    MINUTES_WINDOW,
    POSITION_PRIORS,
    PlayerFeatures,
    build_features,
    group_stat_rows,
)
from .model import (
    COMPONENT_KEYS,
    MODEL_VERSION,
    FixtureContext,
    GameweekXP,
    expected_points,
    minutes_model,
)

__all__ = [
    "COMPONENT_KEYS",
    "MINUTES_WINDOW",
    "MODEL_VERSION",
    "POSITION_PRIORS",
    "FixtureContext",
    "GameweekXP",
    "PlayerFeatures",
    "build_features",
    "expected_points",
    "group_stat_rows",
    "minutes_model",
]
