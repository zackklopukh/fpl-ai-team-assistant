"""Walk-forward backtest for expected-points models.

This package decides which xP model ships, so the one property it must have is
that no model can see the gameweek it is predicting. That guarantee lives in
`data`: the harness holds the full history, and a model only ever receives an
`AsOf` — a set of freshly filtered copies built for one target gameweek, with no
reference back to the frames or the database they were cut from.

Modules:

* `data` — loads the tables once and builds `AsOf` views and actuals.
* `harness` — walks forward through target gameweeks, fitting and predicting.
* `metrics` — error, rank, top-k, decision value and calibration.
* `baselines` — FPL's own projection, a naive recent-form mean, and baseline-0.1.
* `report` — the terminal table and the JSON results file.
* `run_backtest` — the CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The ingest scripts are run as scripts, not installed as a package, and import
# each other by bare module name (`import db`, `from xp.model import ...`). Make
# that work however this package was reached.
_INGEST_DIR = str(Path(__file__).resolve().parent.parent)
if _INGEST_DIR not in sys.path:
    sys.path.insert(0, _INGEST_DIR)
