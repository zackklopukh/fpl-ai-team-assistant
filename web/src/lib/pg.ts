/**
 * The web app's one Postgres pool.
 *
 * There used to be three — players (db.ts), fixtures (fixtures.ts) and the
 * recommendation log (api/optimize) — each opening up to 2-3 connections per
 * server instance. On Vercel every instance is a separate process that keeps its
 * sockets while frozen between requests, and Supabase's session pooler allows
 * 15 clients in total. Two warm instances filled it; every query after that
 * failed with EMAXCONNSESSION, and the pages fell back to the 159-player seed,
 * which surfaced to users as "a player is missing from our price list".
 *
 * Two fixes live here:
 *
 * 1. **One small pool per process**, shared by every module and cached on
 *    `globalThis` so development's hot reload, which re-evaluates modules, does
 *    not leak a fresh pool per edit.
 * 2. **The transaction pooler, not the session pooler.** On Supabase the same
 *    host serves session mode on 5432 and transaction mode on 6543. Session mode
 *    pins a database connection to each client for as long as the client stays
 *    connected; transaction mode lends one only for the length of a query, which
 *    is what a serverless app needs. node-postgres's parameterised queries use
 *    unnamed statements, which transaction mode supports. (The Python ingest jobs
 *    stay on 5432: psycopg auto-prepares named statements, which it does not.)
 *
 * Relative imports only: the test runner has no path aliases.
 */

import { Pool } from "pg";

const PLACEHOLDER = /PASSWORD|PROJECT_REF|REPLACE_WITH/i;

/** A placeholder from .env.example counts as unconfigured, not as a bad password. */
export function isConfigured(url: string | undefined): url is string {
  if (!url) return false;
  if (PLACEHOLDER.test(url)) return false;
  return url.startsWith("postgres://") || url.startsWith("postgresql://");
}

/**
 * The connection string the web app should use.
 *
 * - `sslmode` is removed: node-postgres builds its own TLS config from it and
 *   that overrides the `ssl` option below, failing against Supabase with
 *   "self-signed certificate in certificate chain".
 * - A Supabase pooler address on the session port (5432) is moved to the
 *   transaction port (6543). Same host, same credentials. Anything else is left
 *   exactly as given, so a local Postgres or a direct connection still works.
 */
export function webConnectionString(url: string): string {
  try {
    const parsed = new URL(url);
    parsed.searchParams.delete("sslmode");
    if (parsed.hostname.endsWith(".pooler.supabase.com") && parsed.port === "5432") {
      parsed.port = "6543";
    }
    return parsed.toString();
  } catch {
    return url;
  }
}

/** Raised when a configured database cannot be reached — never papered over. */
export class DatabaseUnavailableError extends Error {
  constructor(cause: unknown) {
    super(
      `The database is unavailable right now (${
        cause instanceof Error ? cause.message : String(cause)
      }).`,
    );
    this.name = "DatabaseUnavailableError";
  }
}

const GLOBAL_KEY = "__fplWebPgPool" as const;
type WithPool = typeof globalThis & { [GLOBAL_KEY]?: Pool };

/** The shared pool, or null when DATABASE_URL is not configured. */
export function getPool(): Pool | null {
  const url = process.env.DATABASE_URL?.trim();
  if (!isConfigured(url)) return null;

  const g = globalThis as WithPool;
  if (!g[GLOBAL_KEY]) {
    const pool = new Pool({
      connectionString: webConnectionString(url),
      // A page makes a few queries at once; two connections serve them without
      // letting one instance hog the pooler. The rest queue for milliseconds.
      max: 2,
      idleTimeoutMillis: 5_000,
      connectionTimeoutMillis: 5_000,
      // Let a serverless process exit while connections sit idle.
      allowExitOnIdle: true,
      // Supabase terminates TLS with its own CA; the data is public reference
      // data, so verification is not the security boundary here.
      ssl: { rejectUnauthorized: false },
    });
    pool.on("error", (err) => {
      console.error("[pg] idle client error:", err.message);
    });
    g[GLOBAL_KEY] = pool;
  }
  return g[GLOBAL_KEY] ?? null;
}
