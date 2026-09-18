/**
 * POST /api/optimize — the browser's only route to the solver.
 *
 * Thin by design. It exists for two reasons and does nothing else:
 *
 *   1. `OPTIMIZER_URL` stays server-side. The optimizer is not a public
 *      endpoint and must not be reachable from a page.
 *   2. The recommendation is logged anonymously. ARCHITECTURE.md and CLAUDE.md
 *      both say this is the only way the model is ever evaluated, and it is
 *      unreconstructable after the fact — so it is written from the first
 *      solver on, not once there is time.
 *
 * Privacy (CLAUDE.md invariant 4). The row carries a hash of the squad and
 * nothing else. No team id, no IP, no session, no cookie, no user agent, no
 * referrer. This route deliberately does not read a single request header, so
 * there is nothing identifying in scope to accidentally write. If a future
 * change needs a header here, that is the moment to stop and raise it.
 *
 * The log write degrades to a no-op. The database is not configured in a fresh
 * clone, and a user asking for advice must not be shown an error because our
 * analytics could not be written.
 *
 * Money is integer tenths all the way through.
 */

import { NextResponse } from "next/server";

import { SEASON } from "@/lib/db";
import {
  FAILURE_STATUS,
  buildLogRow,
  optimizerBaseUrl,
  optimizerTimeoutMs,
  requestOptimize,
  validateOptimizeRequest,
  type RecommendationLogRow,
} from "@/lib/optimizer";
import type { OptimizeRequest } from "@/lib/optimizerTypes";

// pg needs Node, and a solve is never prerendered.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// ---------------------------------------------------------------------------
// The log connection
//
// A separate, tiny pool rather than lib/db.ts's: that module is the read layer
// and is owned elsewhere. The unconfigured-connection pattern is deliberately
// the same one — a placeholder connection string counts as unconfigured, so a
// fresh clone gets a working page instead of an auth error.
// ---------------------------------------------------------------------------

const CONNECTION_STRING = process.env.DATABASE_URL?.trim();

function isConfigured(url: string | undefined): url is string {
  if (!url) return false;
  if (/PASSWORD|PROJECT_REF|REPLACE_WITH/i.test(url)) return false;
  return url.startsWith("postgres://") || url.startsWith("postgresql://");
}

// Imported lazily so a build without `pg` reachable, or an unconfigured clone,
// never pays for the driver at module load.
type PgPool = { query: (sql: string, params: unknown[]) => Promise<unknown> };
let poolPromise: Promise<PgPool | null> | null = null;

function getPool(): Promise<PgPool | null> {
  if (!isConfigured(CONNECTION_STRING)) return Promise.resolve(null);
  if (!poolPromise) {
    poolPromise = import("pg")
      .then(
        (pg) =>
          new pg.Pool({
            connectionString: CONNECTION_STRING,
            max: 2,
            idleTimeoutMillis: 10_000,
            connectionTimeoutMillis: 5_000,
            ssl: { rejectUnauthorized: false },
          }) as unknown as PgPool,
      )
      .catch(() => null);
  }
  return poolPromise;
}

let logNoticeLogged = false;

/**
 * Write one row, or quietly do nothing.
 *
 * Never awaited on the response path and never able to fail the request: the
 * recommendation is the user's, the log is ours.
 */
async function logRecommendation(row: RecommendationLogRow): Promise<void> {
  const pool = await getPool();
  if (!pool) {
    if (!logNoticeLogged) {
      logNoticeLogged = true;
      console.warn(
        "[api/optimize] DATABASE_URL is not configured — recommendations are not being logged. " +
          "This log is the only way the model gets evaluated; set it before trusting any evaluation number.",
      );
    }
    return;
  }
  try {
    await pool.query(
      `insert into recommendation_log
         (squad_hash, season, gw, model_version, solver_version, horizon,
          baseline_xp, payload, solve_ms, data_as_of)
       values ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)`,
      [
        row.squad_hash,
        row.season,
        row.gw,
        row.model_version,
        row.solver_version,
        row.horizon,
        row.baseline_xp,
        JSON.stringify(row.payload),
        row.solve_ms,
        row.data_as_of,
      ],
    );
  } catch (err) {
    // Deliberately the message only. An error object from pg can carry the
    // connection string.
    console.error(
      `[api/optimize] recommendation log write failed: ${
        err instanceof Error ? err.message : "unknown error"
      }`,
    );
  }
}

// ---------------------------------------------------------------------------

function fail(status: number, error: string) {
  return NextResponse.json({ error }, { status, headers: { "Cache-Control": "no-store" } });
}

export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return fail(400, "That request body was not JSON.");
  }

  // Season is the app's, not the caller's: players are keyed by
  // (season, element_id) and a caller-supplied season is how you get a squad
  // scored against the wrong year's ids (CLAUDE.md invariant 2).
  const req = { ...(body as object), season: SEASON } as OptimizeRequest;

  const problem = validateOptimizeRequest(req);
  if (problem) return fail(400, problem);

  const result = await requestOptimize(req, {
    baseUrl: optimizerBaseUrl(),
    timeoutMs: optimizerTimeoutMs(),
  });

  if (!result.ok) {
    return fail(FAILURE_STATUS[result.kind], result.message);
  }

  // Fire and forget. Awaiting would make the user wait on our bookkeeping, and
  // a rejection here must not surface as an unhandled rejection either.
  void logRecommendation(buildLogRow(req, result.response, result.raw)).catch(() => {});

  // The service's body, forwarded unchanged, so the browser parses it with the
  // same function this file did.
  return NextResponse.json(result.raw, {
    headers: {
      // The answer depends on a squad and on an xP snapshot that changes
      // nightly. The optimizer caches its own solves on a key that includes
      // data_as_of; a shared CDN cache here would serve stale prices.
      "Cache-Control": "private, no-store",
    },
  });
}
