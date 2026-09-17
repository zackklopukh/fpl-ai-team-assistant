# FPL AI Team Assistant

A Fantasy Premier League squad rater and optimizer. Public, read-only, no accounts.

- `ingest/` — Python scripts on GitHub Actions cron. The only writer to Postgres.
- `optimizer/` — FastAPI on Modal. Stateless solver, no database credentials.
- `web/` — Next.js on Vercel. Reads Postgres, calls the optimizer.
- `db/schema.sql` — the reference schema. Cron owns every table in it.

`ARCHITECTURE.md` holds the reasoning. `CLAUDE.md` holds the invariants that must
not be broken — money in integer tenths, `(season, element_id)` keys, no user data.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r ingest/requirements.txt
cp .env.example .env     # then fill it in
```

Run the tests:

```bash
.venv/bin/python -m pytest ingest/tests -q
```

### Database

The schema is plain SQL with no migration tool. Apply it to a fresh Supabase
project by pasting `db/schema.sql` into the SQL editor, or:

```bash
psql "$DATABASE_URL" -f db/schema.sql
```

It is safe to re-run — every object is created if-not-exists.

### What you need to supply

| Thing | Where it goes | Needed for |
| --- | --- | --- |
| Supabase project | `DATABASE_URL`, `NEXT_PUBLIC_SUPABASE_*` | Any ingestion or page that reads data |
| Your FPL team ID | Not stored — used once, by hand | Closing the last Phase 0 check |
| Vercel project | Linked from `web/` | Deploying the site |
| Modal account | `modal token new` | Deploying the optimizer |

None of them are needed to run the tests.

## Conventions

- Money is integer tenths: `55` means £5.5m. Never floats, never pounds.
- Players are keyed by `(season, element_id)`. FPL reassigns element ids yearly.
- Ingestion writes are upserts on the natural key, so re-runs are always safe.
- Timestamps are UTC. The gameweek deadline is the clock everything derives from.

Not affiliated with the Premier League or Fantasy Premier League.
