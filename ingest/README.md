# Ingestion

Cron is the only writer to Postgres. The web app reads Postgres and never calls
the FPL API; the optimizer gets no database credentials at all. Everything in
this folder is a scheduled job, a shared helper, or a check.

Every write is an upsert on the natural key, so **re-running a job after a
failure is always safe**. You never have to reason about partial state.

## The jobs

| Job | `run.py` name | Schedule (UTC) | Writes |
| --- | --- | --- | --- |
| `sync_bootstrap.py` | `bootstrap` | every 2 hours in season | `players`, `teams`, `gameweeks` — prices, status, injury news |
| `snapshot_prices.py` | `prices` | daily 01:45 | `price_history` — one row per player per day |
| `sync_fixtures.py` | `fixtures` | daily 04:00 | `fixtures` — FDR, kickoffs, results |
| `compute_xp.py` | `xp` | nightly 05:00 | `xpoints` for the next 5 gameweeks, **for every model** (`baseline-0.1`, `fitted-0.1`, `gbm-0.1`) side by side |
| `score_models.py` | `score` | nightly, after `xp` | nothing (read only) — grades each model on finished gameweeks using the prediction it froze at the deadline |
| `publish_xp.py` | `publish-xp` | before each optimizer deploy | a JSON artifact of the **live** model's xP for the optimizer — never the database |
| `import_history.py` | `import-history` | once, and when a season ends | past seasons from the public vaastav dataset, `source='history'` |
| `sync_live.py` | `live` | every 15 min in match windows | `player_gw_stats` — bonus provisional |
| `settle_bonus.py` | `settle-bonus` | ~2h after the last match | `player_gw_stats` — final bonus, `bonus_settled = true` |
| `backfill_history.py` | `backfill` | manual, and once on a fresh database | `player_gw_stats` — historical rows |
| `verify_db.py` | `verify` | by hand | nothing (read only) |
| `season.py` | `season` | by hand | nothing (read only) |

`squad_reconstruct.py` is not a scheduled job — it is the library the web app's
team-id import path uses, and it writes nothing.

## Running one

`run.py` is the front door. It runs exactly the code the workflows run:

```bash
python ingest/run.py --help            # every job, with its schedule
python ingest/run.py verify
python ingest/run.py prices --dry-run
python ingest/run.py live --gameweek 7
python ingest/run.py backfill --from-element 412
python ingest/run.py settle-bonus --dry-run
```

The per-script entry points still work and the GitHub Actions workflows still
call them by path:

```bash
python ingest/sync_bootstrap.py
python ingest/snapshot_prices.py --dry-run
```

`--dry-run` exists on `prices`, `backfill` and `settle-bonus` — the three jobs
where there is a meaningful "what would this do" to report. `bootstrap`,
`fixtures` and `live` write whatever the API currently says; there is no halfway
version of that.

## Bring-up, on a fresh database

Do these in order. Each one depends on the last: `players` has a foreign key onto
`teams`, `player_gw_stats` onto `players`, and the model reads both.

```bash
# 0. Dependencies. requirements-model.txt includes requirements.txt and adds
#    numpy, pandas and scikit-learn, which the xP models need.
python -m pip install -r ingest/requirements-model.txt

# 0b. The connection string. Finds your pooler region, percent-encodes the
#     password (a `#` silently truncates the URL), writes it to .env.
python ingest/find_pooler.py --write

# 1. Apply the schema. Supabase SQL editor, or psql:
psql "$DATABASE_URL" -f db/schema.sql

# 2. Check it took. Tables, columns, pg_trgm, row counts.
python ingest/run.py verify

# 3. Teams, players and the gameweek calendar. Everything else needs these.
python ingest/run.py bootstrap

# 4. Fixtures for the whole season.
python ingest/run.py fixtures

# 5. This season's per-gameweek stats so far. Slow — see below.
python ingest/run.py backfill

# 6. Past seasons, for the models to learn from (~14s; 2023-24 to 2025-26).
python ingest/run.py import-history

# 7. Expected points for the next five gameweeks, every model side by side.
#    Without steps 5 and 6 the minutes model has almost nothing to go on and
#    projects nonsense — publish-xp refuses to ship that, but xp will write it.
python ingest/run.py xp

# 8. The live model's artifact for the optimizer.
python ingest/run.py publish-xp

# 9. Confirm. Every table should now have rows and ingest_runs a clean history.
python ingest/run.py verify
```

Then push the `DATABASE_URL` and `FPL_USER_AGENT` secrets to GitHub Actions (see
`.github/README.md`) and the schedule takes over.

`python ingest/run.py season` at any point tells you whether there is a season
on, when the next deadline is, and whether the frequent jobs should be running
at a raised cadence.

### About the backfill

`backfill_history.py` walks every player's `element-summary/{id}/` — roughly 659
requests, serialised with a sleep between them, about seven minutes of wall
clock. That is deliberate, not a bug to optimise: the FPL API has no documented
rate limit, which means the limit is whatever Cloudflare decides today, and this
is the job most likely to draw attention.

It is resumable. Rows already stored are skipped without a request, and a run
that dies part way through is restarted from where it stopped:

```bash
python ingest/run.py backfill --from-element 412
```

The element id to resume from is in the last progress line of the log. On GitHub
Actions it is `gh workflow run ingest-backfill.yml -f from_element=412`.

Run it now and keep running it. `element-summary` only ever exposes the *current*
season, so a week this job does not run is a week of training data that cannot be
recovered later at any price.

## Bonus settlement

`sync_live.py` writes what FPL is reporting while matches are on, and bonus at
that point is a live projection off BPS. A correction after full time moves the
three bonus points to a different player. `player_gw_stats.bonus_settled` records
which kind of row you are looking at, and `gameweeks.data_checked` is FPL saying
the gameweek is now final.

`settle_bonus.py` closes that gap. It queries for gameweeks holding unsettled
rows first, so the usual case — nothing to settle — costs one query and no HTTP
request at all. When there is something to settle it re-pulls that gameweek's
live payload through the *same* transform `sync_live` uses and rewrites the rows
with `bonus_settled = true`.

One thing it deliberately does not touch: `value_tenths` and `selected_by` are
the player's price and ownership *during that gameweek*. The payload it reads
carries today's. They are written when a row is first inserted and left alone on
every settle, so a gameweek settled late can never backdate today's price into a
past row.

## Recovering from a failed run

1. **Find out what failed.** `python ingest/run.py verify` prints the last eight
   `ingest_runs` with their errors. A row with `ok = null` is a job that was
   killed rather than one that failed — a GitHub Actions timeout, usually.
2. **Just run it again.** Every write is an upsert on the natural key. There is
   no cleanup step and no partial state to undo. The one job worth resuming
   rather than restarting is the backfill, and only because it is slow.
3. **If it is the database.** `verify` distinguishes "cannot connect" (exit 2)
   from "connected, and the schema is wrong" (exit 1). Supabase pauses free
   projects after about a week of inactivity; the 30-minute bootstrap sync
   normally prevents that, so a paused project usually means the cron has been
   failing for a while and the pause is a symptom.
4. **If it is 403s from the FPL API.** GitHub Actions runs from shared IP ranges
   that many FPL projects already hammer. Check `FPL_USER_AGENT` is set first,
   since anonymous traffic is blocked first. Then move ingestion somewhere else —
   Modal, or a cheap VPS. Do not retry harder; that makes a block more likely.
5. **If a gameweek's points look wrong.** Check `bonus_settled` for that
   gameweek. If it is false, `python ingest/run.py settle-bonus` and see whether
   FPL has checked the data yet; if it has not, the numbers are provisional and
   correct to be provisional.

## The off-season

Between late May and mid-August element ids are reassigned, three clubs are
replaced, fixtures do not exist, and most of the API returns nulls or last
season's data. `ingest-bootstrap` and `ingest-live` guard against polling a dead
API with a hard-coded calendar window, which their own comments correctly call a
heuristic.

`season.py` is the honest version: it reads `gameweeks.deadline_time` and asks
whether there is a deadline near enough to matter, and falls back to the same
calendar window only when the table is empty or the database is unreachable. It
names which of the two it used, so an answer and a guess never look alike.

```bash
python ingest/run.py season              # report
python ingest/run.py season --exit-code  # exit 1 when off-season, for a guard
```

It also exposes the deadline predicates: the next deadline, time until it, and
`should_sync_often` — true in the 24 hours before a deadline, which is when press
conferences land and when essentially all of the traffic arrives. Nothing branches
on that yet; the workflows still run one cadence all week.

## Conventions

- **Money is integer tenths.** `55` is £5.5m. Never floats, never pounds.
- **Players are keyed by `(season, element_id)`.** FPL reassigns element ids
  between seasons.
- **Timestamps are UTC.** The gameweek deadline is the clock everything derives
  from.
- **No lookahead.** `player_gw_stats.value_tenths` and `selected_by` are as of
  that gameweek. Never fill them from `players`, which describes today.
- **Pure transforms are separated from I/O.** The payload-to-row functions take
  a payload and return rows; the network and the database live in `main`. That
  split is what makes the tests offline.

## Tests

Offline by design — no database, no network. Saved payloads live in
`tests/fixtures/`.

```bash
python -m pytest ingest/tests -q
python -m pytest ingest/tests optimizer/tests -q
```
