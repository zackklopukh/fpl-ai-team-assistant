/**
 * The only file in the web app that talks to the FPL API. Server-only.
 *
 * CLAUDE.md invariant 3: the web app never calls the FPL API from a request a
 * user is waiting on. Cron writes Postgres, the app reads Postgres. The
 * per-manager endpoints are the single exception, because they are specific to
 * one person and cron cannot know about them in advance:
 *
 *   - `entry/{id}/`                  the manager summary
 *   - `entry/{id}/transfers/`        every price they ever paid
 *   - `entry/{id}/event/{gw}/picks/` the squad itself, public after the deadline
 *
 * The exception comes with conditions, and they are this module's job:
 *
 *   1. **Cache for at least an hour.** A transfer log changes weekly at most and
 *      a squad changes once a gameweek. Two layers, deliberately: Next's Data
 *      Cache (shared across requests on the platform) and an in-process map
 *      (which also covers dev, self-hosting, and caching a 404 so a typo'd team
 *      id is not asked for again and again).
 *   2. **A descriptive User-Agent**, from `FPL_USER_AGENT`, matching what
 *      ingest/ sends. Anonymous scrapers get blocked first.
 *   3. **Rate-limit per team id.** That is lib/rateLimit.ts's job, and the route
 *      handler's to call.
 *
 * Emphatically *not* here: `bootstrap-static/`. Current prices come from our own
 * database (or the checked-in seed) via lib/db.ts. Fetching bootstrap from a
 * request path would violate the invariant outright, and it is a megabyte of
 * data we already have.
 *
 * Nothing here logs a team id. It is the user's, it identifies them, and
 * CLAUDE.md invariant 4 says nothing about an identifiable user is stored. It is
 * used for the duration of a request and then forgotten; a cached response sits
 * in memory until it expires, and no identifier is written anywhere durable.
 */

// Relative, not aliased: this module is imported by tests that run outside the
// Next toolchain, where `@/` is not resolved.
import type { EntryPayload, PicksPayload, TransferRow } from "./reconstruct";

// A client bundle that reached this module would ship the outbound fetch into
// the browser, where none of the conditions above hold. Fail at import.
if (typeof window !== "undefined") {
  throw new Error(
    "lib/fpl.ts is server-only — it must never be imported into client code.",
  );
}

const BASE_URL = "https://fantasy.premierleague.com/api";

/** One hour, the floor set by ARCHITECTURE.md's "Not getting blocked". */
export const CACHE_SECONDS = 3600;
/** A 404 or a refusal is cached too, but briefly — the team may appear later. */
export const NEGATIVE_CACHE_SECONDS = 300;
/** Long enough for a slow upstream, short enough that the user is not stranded. */
export const REQUEST_TIMEOUT_MS = 8000;

function userAgent(): string {
  return (
    process.env.FPL_USER_AGENT?.trim() ||
    "fpl-ai-team-assistant/0.1 (+https://github.com/zackklopukh/fpl_ai_team_assistant)"
  );
}

/** What went wrong, in terms the route handler can turn into a sentence. */
export type FplErrorKind =
  | "not-found" // the team id does not exist
  | "unavailable" // upstream refused, 5xx'd, or is rate-limiting us
  | "timeout" // upstream took too long
  | "malformed"; // 200 with something that is not the payload we expect

export class FplError extends Error {
  readonly kind: FplErrorKind;
  /** The upstream status, for our own logs. Never sent to the client. */
  readonly status: number | null;

  constructor(kind: FplErrorKind, message: string, status: number | null = null) {
    super(message);
    this.name = "FplError";
    this.kind = kind;
    this.status = status;
  }
}

// ---------------------------------------------------------------------------
// In-process cache
//
// Per instance, like the rate limiter, and for the same honest reason: several
// serverless instances mean several copies. It is a hit-rate improvement, not a
// guarantee, and the Next Data Cache below is the layer that survives instances.
// ---------------------------------------------------------------------------

interface CacheEntry {
  expiresAt: number;
  /** A resolved payload, or the error to re-raise for a negatively cached path. */
  value: { ok: true; data: unknown } | { ok: false; error: FplError };
}

const cache = new Map<string, CacheEntry>();

function cacheGet(key: string): CacheEntry["value"] | null {
  const entry = cache.get(key);
  if (!entry) return null;
  if (entry.expiresAt <= Date.now()) {
    cache.delete(key);
    return null;
  }
  return entry.value;
}

function cacheSet(key: string, value: CacheEntry["value"], seconds: number): void {
  // Sweep opportunistically so a long-lived instance does not accumulate
  // payloads for managers who visited once. Cheap: this map is small.
  const now = Date.now();
  for (const [k, entry] of cache) {
    if (entry.expiresAt <= now) cache.delete(k);
  }
  cache.set(key, { expiresAt: now + seconds * 1000, value });
}

/** Test seam. Never called on a request path. */
export function clearFplCache(): void {
  cache.clear();
}

// ---------------------------------------------------------------------------
// Fetching
// ---------------------------------------------------------------------------

async function getJson<T>(path: string): Promise<T> {
  const cached = cacheGet(path);
  if (cached) {
    if (cached.ok) return cached.data as T;
    throw cached.error;
  }

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      headers: {
        "User-Agent": userAgent(),
        Accept: "application/json",
      },
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      // The platform-level cache. Survives instances; the map above does not.
      next: { revalidate: CACHE_SECONDS },
    });
  } catch (err) {
    const aborted =
      err instanceof Error && (err.name === "TimeoutError" || err.name === "AbortError");
    throw new FplError(
      aborted ? "timeout" : "unavailable",
      aborted
        ? "the FPL API did not respond in time"
        : `could not reach the FPL API: ${err instanceof Error ? err.name : "unknown"}`,
    );
  }

  if (response.status === 404) {
    const error = new FplError("not-found", "no such entry", 404);
    cacheSet(path, { ok: false, error }, NEGATIVE_CACHE_SECONDS);
    throw error;
  }

  if (!response.ok) {
    // The body may be an HTML challenge page or a Cloudflare block. It is never
    // shown to the user and never logged — the status is the useful part.
    const error = new FplError(
      "unavailable",
      `FPL API returned ${response.status}`,
      response.status,
    );
    cacheSet(path, { ok: false, error }, NEGATIVE_CACHE_SECONDS);
    throw error;
  }

  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new FplError("malformed", "the FPL API returned something that is not JSON");
  }

  cacheSet(path, { ok: true, data }, CACHE_SECONDS);
  return data as T;
}

/** A manager's summary: name, current gameweek, last deadline bank and value. */
export async function getEntry(teamId: number): Promise<EntryPayload> {
  const data = await getJson<EntryPayload>(`/entry/${teamId}/`);
  if (!data || typeof data !== "object") {
    throw new FplError("malformed", "entry payload was not an object");
  }
  return data;
}

/** Full transfer log, each row carrying the price actually paid, in tenths. */
export async function getEntryTransfers(teamId: number): Promise<TransferRow[]> {
  const data = await getJson<TransferRow[]>(`/entry/${teamId}/transfers/`);
  if (!Array.isArray(data)) {
    throw new FplError("malformed", "transfer log was not an array");
  }
  return data;
}

/**
 * A manager's squad for a gameweek, or null when it is not public yet.
 *
 * Picks are private until that gameweek's deadline passes, and the endpoint
 * 404s until then. That is an ordinary outcome, not an error, so it comes back
 * as null and the caller says so plainly.
 */
export async function getEntryPicks(
  teamId: number,
  gw: number,
): Promise<PicksPayload | null> {
  try {
    const data = await getJson<PicksPayload>(`/entry/${teamId}/event/${gw}/picks/`);
    if (!data || typeof data !== "object" || !Array.isArray(data.picks)) {
      throw new FplError("malformed", "picks payload had no picks array");
    }
    return data;
  } catch (err) {
    if (err instanceof FplError && err.kind === "not-found") return null;
    throw err;
  }
}

export interface ManagerPayloads {
  entry: EntryPayload;
  transfers: TransferRow[];
  /** Null when no gameweek's picks are public yet. */
  picks: PicksPayload | null;
  /** The gameweek the picks came from, or the entry's current one. */
  gw: number | null;
}

/**
 * The three payloads a reconstruction needs, fetched together.
 *
 * `entry.current_event` is the gameweek whose deadline has most recently passed,
 * which is exactly the one whose picks are public. Before the season starts it
 * is null and there is nothing to show.
 */
export async function fetchManagerPayloads(teamId: number): Promise<ManagerPayloads> {
  const [entry, transfers] = await Promise.all([
    getEntry(teamId),
    getEntryTransfers(teamId),
  ]);

  const currentEvent = entry.current_event;
  const gw =
    typeof currentEvent === "number" && currentEvent > 0 ? Math.trunc(currentEvent) : null;

  const picks = gw === null ? null : await getEntryPicks(teamId, gw);

  return { entry, transfers, picks, gw };
}
