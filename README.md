# FPL AI Team Assistant

A Fantasy Premier League squad rater and optimizer. Public, read-only, no accounts.

- **Import your team** by FPL team ID — exact selling prices, no login, no screenshot.
- **Build a squad** on a pitch of club-colour shirts, shareable by link.
- **Get advice** — ranked transfer plans over the next few gameweeks, with the
  working shown, from a mixed-integer solver.
- **Ideal team / wildcard** — the best fifteen a budget buys, or the best rebuild
  of your own squad.
- **Players and fixtures** — a sortable player table and a fixture ticker that
  shows double and blank gameweeks honestly.

## How it fits together

- `ingest/` — Python jobs on GitHub Actions cron. The only writer to Postgres.
  Syncs FPL data, fits the expected-points models nightly, grades them weekly.
- `optimizer/` — FastAPI on Modal. A stateless MIP solver (PuLP + CBC); no
  database credentials, reads a published xP artifact.
- `web/` — Next.js on Vercel. Reads Postgres, calls the optimizer.
- `db/schema.sql` — the schema. Cron owns every table in it.

`ARCHITECTURE.md` holds the reasoning and the current status of each phase.
`CLAUDE.md` holds the invariants that must not be broken — money in integer
tenths, `(season, element_id)` keys, no user data. `docs/DEPLOYMENT.md` takes it
from a clone to a running site.

### The model, briefly

Expected points are predicted per player per fixture, decomposed into scoring
events (minutes, goals, assists, clean sheets, bonus, saves, defensive
contribution, cards). Three models run nightly side by side — a hand-tuned
baseline, a fitted statistical model built from small GLMs and a Poisson
team-strength model (`ingest/xp/fitted.py`), and a gradient-boosting challenger
(`ingest/xp/gbm.py`). A leak-proof walk-forward backtest (`ingest/backtest/`)
could not separate them on a held-out season, so the explainable fitted model
is live and all three are graded weekly on this season's gameweeks
(`ingest/score_models.py`). `config.LIVE_MODEL_VERSION` chooses which is
published.

## Local setup

Python 3.14 and Node 24.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r ingest/requirements-model.txt -r optimizer/requirements.txt
cp .env.example .env     # then fill it in — see docs/DEPLOYMENT.md for the connection string
cd web && npm install
```

`requirements-model.txt` includes `requirements.txt` and adds numpy, pandas and
scikit-learn for the models, the backtest and the tests.

Run the tests (no database or network needed):

```bash
.venv/bin/python -m pytest ingest/tests optimizer/tests -q
cd web && npx vitest run && npm run typecheck
```

Run it locally — the optimizer in one terminal, the site in another:

```bash
.venv/bin/python ingest/publish_xp.py          # the optimizer's xP data, from Postgres
.venv/bin/python -m uvicorn optimizer.app:app --port 8000
```

```bash
cd web && npm run dev                           # http://localhost:3000
```

With no `DATABASE_URL` the site serves a checked-in sample of players and says
so; with one configured it reads Postgres and fails loudly rather than falling
back if the database is unreachable.

### Database

The schema is plain SQL with no migration tool. Apply it by pasting
`db/schema.sql` into the Supabase SQL editor, or:

```bash
psql "$DATABASE_URL" -f db/schema.sql
```

It is safe to re-run — tables are created if-not-exists and later columns are
added as idempotent `alter table ... add column if not exists`. Then follow the
bring-up sequence in `ingest/README.md`, and check it with
`python ingest/run.py verify`.

### What you need to supply

| Thing | Where it goes | Needed for |
| --- | --- | --- |
| Supabase project | `DATABASE_URL` (session pooler string) in `.env`, Vercel and Actions secrets | Ingestion, and any page that reads live data |
| Vercel project | Root Directory `web` | Deploying the site |
| Modal account | `modal token new`, then `modal deploy optimizer/modal_app.py` | Deploying the optimizer; `OPTIMIZER_URL` in Vercel |
| `FPL_USER_AGENT` | `.env` and Actions secrets | Identifying the scraper to FPL |

None of them are needed to run the tests.

## Conventions

- Money is integer tenths: `55` means £5.5m. Never floats, never pounds.
- Players are keyed by `(season, element_id)`. FPL reassigns element ids yearly;
  `players.code` is the only stable cross-season identity.
- Ingestion writes are upserts on the natural key, so re-runs are always safe.
- Timestamps are UTC. The gameweek deadline is the clock everything derives from.

Not affiliated with the Premier League or Fantasy Premier League.
