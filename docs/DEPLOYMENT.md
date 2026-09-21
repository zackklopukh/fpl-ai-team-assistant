# Deployment

Everything needed to take this repository from a clone to a running site

Three deployables, deployed independently:

| What | Where | Deploys when |
| --- | --- | --- |
| `web/` | Vercel | push to `main` (and a preview per pull request) |
| `optimizer/` | Modal | you run `modal deploy`, by hand |
| `ingest/` | GitHub Actions cron | on its schedule, once secrets are set |

Nothing here is required to run the tests, and nothing here is required to run
the site locally. `npm run dev` in `web/` works with no database, no optimizer
and no Sentry account — it serves the checked-in seed snapshot and says so in
the console. That is the state a new contributor starts in and it must keep
working.

---

## Read this before you deploy anything

### Vercel Hobby forbids commercial use

The free Hobby plan's terms allow personal, non-commercial projects only. This
project on Hobby is fine **as a portfolio project**. The moment it takes money
in any form — ads, sponsorship, a subscription, affiliate links, a Buy Me A
Coffee button tied to the product — it needs Vercel Pro at **$20/month**, or it
moves to Cloudflare Pages or Netlify.

ARCHITECTURE.md flags this as a decision to make *before* building anything that
depends on revenue, because not building a payments flow is easy and migrating
hosting later is not. Decide now; it is cheaper.

### GitHub Actions minutes: make the repository public, or pay

The cron schedule in `.github/workflows/` fires roughly:

| Workflow | Schedule | Runs/month (30 days) |
| --- | --- | --- |
| `ingest-bootstrap` | every 2 hours, in season | ~360 |
| `ingest-live` | every 15 min inside match windows | ~1,045 |
| `ingest-daily` | 3 crons a day | ~90 |
| | **total** | **~1,495** |

GitHub bills Actions in **whole minutes, rounded up per job**, so ~1,495 runs is
at least ~1,495 minutes even though most of these jobs finish in twenty seconds.
(The nightly xP job, which now fits three models, is the one exception: a few
minutes a night, ~90-150 a month.)
The free allowance on a **private** repository is **2,000 minutes/month**. The
allowance on a **public** repository is **unlimited**.

Bootstrap was every 30 minutes originally (~2,560 runs a month, over the private
allowance); at every 2 hours it fits either way. The options, if the schedule
ever grows again:

1. **Make the repository public.** Free, unlimited, and the recommended path —
   there are no secrets in the source, only in Actions secrets. This is the
   default assumption of everything below.
2. **Keep it private and pay.** Roughly $0.008/minute beyond the free tier, so
   ~560 minutes over ≈ $4.50/month. Small, but it is no longer a free project.
3. **Keep it private and cut the schedule** — which is what every-2-hours
   already is, at the cost of staler prices and injury news. That matters most
   in the 24 hours before a deadline, exactly when people use the tool, so a
   deadline-aware bump (`ingest/season.py` has `should_sync_often`) is the
   better next step than a flat rate.

The off-season guard in `ingest-bootstrap` and `ingest-live` already removes
roughly a quarter of the year, so the annual average is lower than the in-season
month above. Do not plan against the average — the bill arrives monthly.

---

## 1. Supabase

1. Create a project at supabase.com. Note the region; put Vercel's functions
   near it later if you ever start caring about latency.
2. Apply the schema. Either paste `db/schema.sql` into the SQL editor, or:

   ```bash
   psql "$DATABASE_URL" -f db/schema.sql
   ```

   It is safe to re-run — every object is created if-not-exists.
3. Enable `pg_trgm` if the schema has not already (`create extension if not
   exists pg_trgm;`). The screenshot name matching needs it.

### The connection string, and the IPv6 trap

**Project Settings → Database → Connection string.** You are offered more than
one and they are not interchangeable:

| String | Address family | Use it for |
| --- | --- | --- |
| Direct connection (`db.<ref>.supabase.co:5432`) | **IPv6 only** on new projects | local `psql`, local dev on an IPv6 network |
| Session pooler (`aws-0-<region>.pooler.supabase.com:5432`) | IPv4 | GitHub Actions and local ingestion — long-lived jobs |
| Transaction pooler (same host, port `6543`) | IPv4 | **the web app** — serverless, many short-lived clients |

New Supabase projects no longer get a dedicated IPv4 address for the direct
connection. If a network has no IPv6 route — and Vercel's build and serverless
environment, and plenty of home and CI networks, may not — the direct host
simply fails to resolve or times out, with an error that looks like a firewall
problem rather than an address-family problem. **This has bitten people. If
`DATABASE_URL` times out from Vercel but works from your laptop, this is why.**

Set `DATABASE_URL` to the **session pooler** string everywhere — Vercel,
GitHub Actions and your `.env`. One value, and the code picks the mode:

- **The web app switches itself to the transaction pooler** (port 6543, same
  host and credentials) in `web/src/lib/pg.ts`. It has to. Session mode pins a
  database connection to each client for as long as the client stays
  connected, and Supabase allows **15** in total. Vercel runs several instances
  that keep their sockets open while frozen between requests, so session mode
  runs out — this took the live site down on 2026-09-22 with
  `EMAXCONNSESSION max clients reached in session mode`. Transaction mode lends
  a connection only for the length of each query.
- **The Python jobs stay on the session pooler.** psycopg auto-prepares named
  statements, which transaction mode does not support; they are few,
  long-lived connections, which is what session mode is for.

**Keep one pool in the web app.** `web/src/lib/pg.ts` is the only place a
`pg.Pool` is created, capped at 2 connections per instance. A second pool per
module was the other half of that outage.

If the database is configured but unreachable, the site now shows an error
rather than silently serving the 159-player seed — the seed is used only when
`DATABASE_URL` is not configured at all. A "missing from our price list" or
"database is busy" message in production means a connection problem, not a
data problem.

**If you paste a string yourself, percent-encode the password.** A `#` in it
starts a URL fragment and silently cuts off everything after it; write it as
`%23`. `python ingest/find_pooler.py --write` does this for you and finds your
region.

Note also: Supabase pauses a free project after about a week of inactivity. The
ingestion cron writes every 2 hours, so in season it never idles — but a
project created in June and left alone until August **will** be paused when you
come back to it. Unpause it from the dashboard.

---

## 2. Modal (the optimizer)

The optimizer is stateless and gets **no database credentials**. It receives a
squad plus a price list and returns recommendations. Do not give it a
`DATABASE_URL`, ever.

```bash
.venv/bin/python ingest/publish_xp.py    # write data/xp_artifact.local.json first
pip install modal
modal token new                          # opens a browser, writes ~/.modal.toml
modal deploy optimizer/modal_app.py
```

**The xP data is baked into the image at deploy time.** `modal_app.py` copies in
only the `optimizer/` package and `data/xp_artifact.local.json` — never the repo
root, which holds `.env` — and refuses to deploy if the artifact is missing, so
it can never silently serve synthetic data. The consequence: production xP does
not refresh on its own. After each gameweek, re-run `publish_xp.py` and
`modal deploy`. Replacing that with a fetch at container start is the open
"how does production receive the artifact" decision (ARCHITECTURE.md, Phase 7).

`modal deploy` prints the deployed URL — something like
`https://<workspace>--fpl-optimizer-fastapi-app.modal.run`. That value is what
goes into `OPTIMIZER_URL` in Vercel. Redeploy the Vercel project after setting
it; environment variables apply only to new deployments.

Verify it before wiring it up:

```bash
curl -s "$OPTIMIZER_URL/health"          # want "synthetic": false and the live model_version
curl -s -X POST "$OPTIMIZER_URL/squad/ideal" \
  -H 'content-type: application/json' \
  -d '{"current_gw":6,"horizon":1,"budget":1000}'
```

The first request after an idle spell cold-starts in ~4s; a solve takes 1-8s
depending on the horizon. The web app allows 25s.

`modal serve optimizer/modal_app.py` gives a hot-reloading ephemeral deployment
for checking the image without publishing. Locally, the same FastAPI app runs
under uvicorn with no Modal involved:

```bash
.venv/bin/python -m uvicorn optimizer.app:app --port 8000
```

which is why `OPTIMIZER_URL` defaults to `http://127.0.0.1:8000` in
`.env.example`.

**`OPTIMIZER_URL` unset is a supported state.** The site runs without it;
optimisation is the only thing unavailable. It is listed as optional in
`web/src/lib/env.ts` for exactly that reason.

---

## 3. Vercel (the web app)

### Project settings

The Next.js app is **not at the repository root** — it lives in `web/`. This is
the single most common way this deploy goes wrong.

| Setting | Value |
| --- | --- |
| Framework preset | Next.js |
| **Root Directory** | **`web`** |
| Build command | *(default)* `next build` |
| Install command | *(default)* `npm install` |
| Output directory | *(default)* |
| Node version | 22 or 24 |

With Root Directory set to `web`, `web/vercel.json` is the config Vercel reads,
and every path in it is relative to `web/`. Setting the root directory also
means Vercel's install and build steps never see `ingest/` or `optimizer/`,
which is correct — those are Python and deploy elsewhere.

`web/vercel.json` sets security headers and marks `/api/*` `noindex`. It
deliberately does **not** set `functions.maxDuration`: a route that needs longer
than the default (the optimizer call can take ~20s) should say so in its own
route segment config, next to the code that needs it, rather than in a glob here
that silently stops matching when a file moves.

It also does not define any Vercel Cron. Ingestion is GitHub Actions, by
decision — Vercel's Hobby cron is capped near daily, and the price snapshot has
to run at 01:45 UTC on the nose.

### Environment variables

Set these under **Settings → Environment Variables**, choosing the environments
each applies to.

| Variable | Production | Preview | Development | Required? |
| --- | --- | --- | --- | --- |
| `DATABASE_URL` | pooler string | pooler string | your local `.env` | **Required in production** |
| `NEXT_PUBLIC_SITE_URL` | `https://your-domain` | *(leave unset)* | *(unset)* | optional |
| `OPTIMIZER_URL` | Modal URL | Modal URL | `http://127.0.0.1:8000` | optional |
| `FPL_SEASON` | e.g. `2026-27` | same | same | optional |
| `FPL_USER_AGENT` | UA with a contact address | same | same | optional |
| `SENTRY_DSN` | your DSN | optional | unset | optional |
| `NEXT_PUBLIC_SENTRY_DSN` | your DSN | optional | unset | optional |
| `NEXT_PUBLIC_SUPABASE_URL` | `https://<ref>.supabase.co` | same | same | optional |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | anon key | same | same | optional |
| `SENTRY_TRACES_SAMPLE_RATE` | — | — | — | optional override, `0`–`1` |
| `SENTRY_ERROR_SAMPLE_RATE` | — | — | — | optional override, `0`–`1` |
| `ALLOW_SEED_IN_PRODUCTION` | — | — | — | emergency escape hatch, see below |

Leave `NEXT_PUBLIC_SITE_URL` unset on Preview on purpose. Previews then use
their own generated hostname for canonical URLs and share images, and
`robots.ts` serves `Disallow: /` for anything that is not a production
deployment — a preview must never be indexed.

Anything still containing `PASSWORD`, `PROJECT_REF`, `REPLACE_WITH`, `YOURNAME`
or `placeholder` **counts as unset**. `web/src/lib/env.ts` treats a placeholder
and an absent value identically, matching what `web/src/lib/db.ts` already did,
because a half-filled `.env.example` value produces a far more confusing failure
than a missing one.

### What happens when something is missing

- **Optional variable missing** → the app starts, logs one `[env]` line saying
  what is degraded, and carries on. Missing Sentry means errors go to the
  console. Missing `OPTIMIZER_URL` means no optimisation. Missing
  `DATABASE_URL` outside production means the checked-in seed.
- **`DATABASE_URL` missing on a production deployment** → the server refuses to
  boot, with a message naming the variable and pointing here. This is
  deliberate: without it the site does not crash, it quietly serves a seed
  snapshot with stale prices, which looks like a working site and is worse.
- **Escape hatch:** set `ALLOW_SEED_IN_PRODUCTION=1` to turn that refusal into a
  loud warning and keep serving the seed. Use it when a rotated password would
  otherwise take the site down at the worst moment, and remove it the same day.

---

## 4. Sentry (optional, and it should stay optional)

Nobody needs a Sentry account to run or deploy this. With no DSN,
`web/src/instrumentation.ts` and `web/src/instrumentation-client.ts` do
nothing — the SDK is behind a dynamic import, so it is never even loaded.

To turn it on: create a project at sentry.io (Next.js platform), copy the DSN,
and set both `SENTRY_DSN` and `NEXT_PUBLIC_SENTRY_DSN`. A DSN is a write-only
ingest key and is public by design; it is not a secret.

What the wiring guarantees:

- **Nothing identifying is sent.** `sendDefaultPii` is off, `user` and
  `server_name` are deleted, cookies, query strings and request bodies are
  dropped, and every URL, message, exception value and breadcrumb passes through
  a scrubber that redacts long opaque tokens (the encoded squad string) and
  5-or-more-digit numbers (an FPL team ID). Session Replay and profiling
  integrations are filtered out of the client defaults — a replay would record
  the squad on screen.
- **The free tier is respected.** ~5k events/month, and this project's traffic
  all arrives in the two hours before a deadline. Defaults: server errors 1.0,
  browser errors 0.25, traces 0.02, breadcrumbs capped at 20, plus a deny-list
  for extension noise. Override per environment with the two sample-rate
  variables when you are actively chasing something.

If you later want source maps and readable stack traces, wrap `next.config.ts`
in `withSentryConfig` and add `SENTRY_AUTH_TOKEN`, `SENTRY_ORG` and
`SENTRY_PROJECT`. That is a separate, optional step; error reporting works
without it.

---

## 5. GitHub Actions (ingestion)

Cron is the only writer to Postgres. `.github/README.md` is the reference for
the workflows, the schedules, the off-season guard, how to run a job by hand and
what to do about 403s. Do not duplicate it here — read it.

For deployment purposes you need exactly two repository secrets, under
**Settings → Secrets and variables → Actions → Repository secrets**:

- `DATABASE_URL` — the **pooler** string (see the IPv6 note above; Actions
  runners are not guaranteed IPv6).
- `FPL_USER_AGENT` — descriptive, with a contact address.

`tests.yml` needs neither, by design.

---

## First deploy, in order

Do these in sequence. Each step has a check; do not move on until it passes.

1. **Supabase project created, `db/schema.sql` applied.**
   *Check:* `.venv/bin/python ingest/verify_db.py` — it parses `db/schema.sql`
   and reports every missing table or column, plus whether `pg_trgm` is
   installed. It is a better check than `\dt` because it catches a schema that
   was applied from an older copy of the file.

2. **Ingestion run once, locally.**
   ```bash
   .venv/bin/python ingest/sync_bootstrap.py
   ```
   *Check:* `psql "$DATABASE_URL" -c "select count(*) from players"` returns
   several hundred, not zero. Until this passes, a deployed site has nothing to
   show.

3. **Actions secrets set, one workflow run by hand.**
   ```bash
   gh workflow run ingest-bootstrap.yml && gh run watch
   ```
   *Check:* the run is green, and `select max(updated_at) from players` moves.

4. **Optimizer deployed.**
   ```bash
   modal deploy optimizer/modal_app.py
   ```
   *Check:* `curl -s "$OPTIMIZER_URL/health"` returns 200. Keep the URL.

5. **Vercel project created, Root Directory set to `web`.**
   *Check:* the first build log's install step runs in `web/`, and the build
   output lists routes including `/players` and `/sitemap.xml`. A build that
   fails with "no Next.js version detected" means the root directory is still
   the repository root.

6. **Environment variables set for Production, then redeploy.**
   *Check:* the production site's player page shows prices matching the
   database, and the build/runtime log contains **no** `[db] Using seed data`
   line. That line in production means `DATABASE_URL` did not take effect.

7. **Domain attached (optional), `NEXT_PUBLIC_SITE_URL` set to match.**
   *Check:* `curl -s https://your-domain/robots.txt` shows `Allow: /` and a
   `Sitemap:` line pointing at your domain — not `Disallow: /`, which means the
   deployment is not being seen as production, and not `localhost`, which means
   `NEXT_PUBLIC_SITE_URL` is unset.

8. **Sharing check.**
   *Check:* `curl -s -o /dev/null -w '%{http_code} %{content_type}\n'
   https://your-domain/opengraph-image` returns `200 image/png`, and pasting the
   root URL into Slack, WhatsApp or iMessage unfurls with the card and the
   "not affiliated" line.

9. **Failure check.** Visit a path that does not exist and a player id that does
   not exist.
   *Check:* both render the 404 page with working links, and return HTTP 404 —
   not 200, which would let search engines index them.

10. **Sentry check (only if you enabled it).** Trigger an error on a preview
    deployment.
    *Check:* the event arrives, and the event's URL, breadcrumbs and message
    contain no team ID and no squad string. If you can read a squad off a Sentry
    event, the scrubber has regressed and that is a bug to fix before
    production.

---

## Hazards worth knowing before they happen

- **The IPv6 direct connection** (above). The first thing to suspect when the
  database works locally and not on Vercel.
- **A paused Supabase project.** Free projects pause after ~1 week idle. In
  season the cron prevents it; out of season it will happen.
- **The summer.** Between late May and mid-August element ids are reassigned,
  three clubs change and most of the API returns nulls. The site is expected to
  look wrong; the Actions guard stops it polling a dead API for three months.
- **Deadline concentration.** Essentially all traffic arrives in the two hours
  before a Friday or Saturday deadline. Average load means nothing here. It is
  also why the Sentry sample rates are what they are — one bad Friday deploy at
  1.0 can spend a month's event quota in twenty minutes.
- **`ALLOW_SEED_IN_PRODUCTION` left on.** It makes a misconfigured production
  site look healthy. Grep for it before every release.
- **Trademarks.** Player names and statistics are fine. Club crests, kit
  imagery, and Premier League or FPL logos are not, and neither is anything
  implying official affiliation. The Open Graph card is typographic for this
  reason and carries the disclaimer itself, because it is the part of the site
  that travels furthest on its own.
