"""Environment and constants shared by every ingestion script."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")

FPL_BASE_URL = "https://fantasy.premierleague.com/api"

# Anonymous scrapers get blocked first, so identify the project and leave a
# contact address.
USER_AGENT = os.getenv(
    "FPL_USER_AGENT",
    "fpl-ai-team-assistant/0.1 (+https://github.com/zackklopukh/fpl_ai_team_assistant)",
)

SEASON = os.getenv("FPL_SEASON", "2026-27")

DATABASE_URL = os.getenv("DATABASE_URL")

# The xP model the site and the optimizer actually use. Every model in
# compute_xp.MODELS is computed nightly and scored weekly (score_models.py), but
# only this one is published. Chosen 2026-09-21: on a one-season holdout the
# fitted, boosted and baseline models were statistically indistinguishable, and
# the pre-committed tie-break favours the explainable fitted model. Revisit with
# this season's scores around GW15.
LIVE_MODEL_VERSION = os.getenv("FPL_LIVE_MODEL", "fitted-0.1")

# Positions, as FPL numbers them. Squad composition is fixed by the rules.
POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_COMPOSITION = {1: 2, 2: 5, 3: 5, 4: 3}
SQUAD_SIZE = 15
MAX_PER_CLUB = 3


def require_database_url() -> str:
    """Fail early and legibly rather than deep inside psycopg."""
    if not DATABASE_URL:
        raise SystemExit(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in."
        )
    return DATABASE_URL
