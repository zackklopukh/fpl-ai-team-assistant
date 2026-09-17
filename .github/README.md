# GitHub Actions

Cron is the only writer to Postgres. Everything here either runs an ingestion
script on a schedule or runs the tests.

## Workflows

| File | Trigger | What it runs |
| --- | --- | --- |
| `ingest-bootstrap.yml` | every 30 min, in season | `sync_bootstrap.py` — players, teams, gameweeks: prices, status, injury flags |
| `ingest-daily.yml` | 01:45, 04:00, 05:00 UTC | `snapshot_prices.py`, then `sync_fixtures.py`, then `compute_xp.py` |
| `ingest-live.yml` | every 15 min inside match windows | `sync_live.py` — `player_gw_stats` from `event/{gw}/live/` |
| `ingest-backfill.yml` | manual only | `backfill_history.py` — historical gameweek stats |
| `tests.yml` | push, pull request | pytest for `ingest/` and `optimizer/`, typecheck and build for `web/` |

`ingest-daily.yml` holds three crons in one file because the jobs share setup
and have a real ordering — prices, then fixtures, then the model that reads
both. On a schedule only the job whose cron fired runs; a manual run defaults
to all three and runs them in order, and each job declines to start on top of a
failed predecessor.

All five have `workflow_dispatch`, so any of them can be run by hand.

### The off-season guard

`ingest-bootstrap` and `ingest-live` are the high-frequency jobs, and between
late May and mid-August there is nothing for them to read: element ids are
reassigned, three clubs are replaced, fixtures do not exist and most of the API
returns nulls or last season's data. Both start with a date check that exits
early in that window rather than polling a dead API every 15 minutes for three
months.

It is a calendar heuristic, not a fact about the season. The real first and last
fixtures move by a week or two each year and a summer tournament moves them
further, so the window is deliberately loose at both ends and it will sometimes
skip a day it did not need to. A manual run can override it with the
`ignore_season_guard` input. Once `gameweeks` is populated the honest version of
this check is "is there a deadline within the next N days", which is a database
read rather than a guess — worth replacing it with that.

The daily jobs are not guarded: three requests a day cost nothing, and fixtures
for the new season are published in June, which is exactly when you want them.

## Secrets

Set both in **Settings → Secrets and variables → Actions → Repository
secrets**. They are referenced as `secrets.*` and never inlined.

| Secret | Value |
| --- | --- |
| `DATABASE_URL` | The Supabase Postgres connection string. Same value as in your local `.env`. |
| `FPL_USER_AGENT` | A descriptive User-Agent with a contact address, e.g. `fpl-ai-team-assistant/0.1 (+https://github.com/YOURNAME/fpl_ai_team_assistant)`. Anonymous scrapers get blocked first. |

`tests.yml` needs neither — the tests must not touch the database or the
network. It optionally reads `NEXT_PUBLIC_SUPABASE_URL`,
`NEXT_PUBLIC_SUPABASE_ANON_KEY` and `OPTIMIZER_URL` for the `web` build and
falls back to placeholders when they are not set.

## Running a job by hand

Actions tab → pick the workflow in the left sidebar → **Run workflow** → choose
the branch and any inputs → **Run workflow**. Or with the CLI:

```bash
gh workflow run ingest-bootstrap.yml
gh workflow run ingest-daily.yml -f job=prices
gh workflow run ingest-live.yml -f gameweek=7
gh workflow run ingest-backfill.yml -f from_element=412
gh run watch
```

`ingest-backfill` is manual by design. It is slow, serialised and resumable: if
a run hits the timeout, read the last element id from the log and start it again
with `from_element` set to that.

## Scheduling caveats

- **Scheduled runs are queued, not punctual.** GitHub delays them under load,
  sometimes by several minutes, and drops them entirely on a repository with no
  recent activity. Nothing here should assume it ran at the exact minute.
- **The 01:45 price snapshot must be late, never early.** FPL changes prices at
  roughly 01:30 UTC. Fifteen minutes plus GitHub's own lateness is the margin,
  and the lateness only ever helps. Do not move this job earlier.
- Match windows in `ingest-live` are a guess at when football is on
  (weekends 11:00–23:45 UTC, weekdays 17:00–23:45 UTC). Kickoffs outside that —
  an unusual early or overseas fixture — are picked up by the next in-window
  run, not in real time.

## If you start seeing 403s

GitHub Actions runs from shared IP ranges that many FPL projects already
hammer. That is the reason, and **the fix is to move ingestion somewhere else —
Modal, or a cheap VPS — not to retry harder.** Adding retries or shortening the
interval makes a block more likely, not less. Check that `FPL_USER_AGENT` is
set first, since anonymous traffic is blocked first, and then move the jobs.
