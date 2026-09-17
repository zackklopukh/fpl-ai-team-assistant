# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

A Fantasy Premier League squad rater and optimizer. Two deployables:

- `web/` — Next.js (App Router) on Vercel. Public, read-only, **no user accounts**.
- `optimizer/` — FastAPI on Modal. Stateless solver. Never touches the database.
- `ingest/` — Python scripts run by GitHub Actions cron. The only writer to Postgres.

Full reasoning lives in `ARCHITECTURE.md`. Read it before proposing structural changes.

## Non-negotiable invariants

Violating any of these breaks correctness in ways that are hard to detect later.

1. **Money is integer tenths.** `55` means £5.5m. Never floats, never pounds. The FPL
   selling rule floors a halved difference; float arithmetic produces off-by-one squad
   values that look almost right.
2. **Players are keyed by `(season, element_id)`.** FPL reassigns element ids between
   seasons. A bare `element_id` key silently corrupts historical data.
3. **Never call the FPL API from a user-facing request path.** Cron writes to Postgres,
   the app reads Postgres. The two per-manager endpoints (`entry/{id}/`,
   `entry/{id}/transfers/`) are the only exception, and they must be cached and
   rate-limited.
4. **No accounts, no user data.** No auth, no sessions, no PII in the database. Squad
   state lives in `localStorage` and the URL. If a change would require storing something
   about an identifiable user, stop and raise it rather than implementing it.
5. **The optimizer is stateless.** It receives a squad plus a price list and returns
   recommendations. It gets no database credentials.
6. **Fixtures join one-to-many.** Double gameweeks exist (a team plays twice) and blanks
   exist (a team doesn't play). Never write "the fixture this gameweek".
7. **Screenshot parsing always ends at a human confirmation screen.** No auto-accept
   branch, regardless of match confidence.

## Conventions

- Prices, costs and bank values: integer tenths, suffixed `_tenths` where ambiguous.
- Ingestion writes are upserts on the natural key, so re-runs are safe.
- Model outputs carry a `model_version`; never overwrite a previous version's rows.
- Recommendations are logged anonymously (`squad_hash`, gameweek, version, payload).
  This is the only way the model ever gets evaluated — don't remove it.
- Timestamps in UTC. The gameweek deadline is the clock everything derives from.

## Stack, for reference

Next.js + Vercel · Supabase Postgres (with `pg_trgm`) · FastAPI on Modal · GitHub Actions
cron · Tesseract.js client-side with a vision-model fallback · Sentry.

All on free tiers. Before adding a dependency or service, check whether it has one.

## When working on the optimizer

The xPoints model and the squad solver are separate concerns with separate schedules.
Keep them that way — the model is statistics and always improvable, the solver is
optimization and has a right answer. A bug in one should never require touching the other.

Solver performance: pre-filter the player pool to the top ~150 by xP per position plus
everyone currently in the squad. Always set a solver time limit and return the best
incumbent solution rather than proving optimality.

## Things not to do

- Don't add an ORM. Plain SQL plus Supabase's generated types is the choice here.
- Don't add a queue or Redis without a measured reason.
- Don't store uploaded screenshots.
- Don't use Premier League or club trademarks — crests, kits, official logos.
- Don't train on current-value fields (price, ownership, form, season-to-date xG) when
  predicting a past gameweek. That is lookahead leakage and it invalidates every
  evaluation number downstream.

## Working style

- Prefer small, reviewable changes.
- Ask before introducing a new external service or a new top-level directory.
- If a change contradicts `ARCHITECTURE.md`, say so and propose updating that file too.
