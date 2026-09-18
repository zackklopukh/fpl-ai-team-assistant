/**
 * The client for the optimizer service, and the shapes either side of the proxy.
 *
 * Runs server-side only (app/api/optimize/route.ts). `OPTIMIZER_URL` is read
 * here and never reaches the browser — the optimizer is not a public endpoint,
 * and the route in front of it is what keeps it that way.
 *
 * Failure is a first-class outcome, not an exception. The service scales to zero
 * and cold-starts, so "slow" is a normal condition and the timeout is generous:
 * the solver's own limit is around twenty seconds and a container may have to
 * boot before it even starts. A few seconds is not a failure and must never be
 * reported as one.
 *
 * Relative imports only. The test runner has no path aliases.
 *
 * Money is integer tenths (CLAUDE.md invariant 1).
 */

import { SQUAD_SIZE as SQUAD_SLOTS, squadHash, type PlayerIndex, type SquadState } from "./squad";
import {
  DEFAULT_HORIZON,
  MAX_FREE_TRANSFERS,
  MAX_HORIZON,
  MIN_HORIZON,
  SQUAD_SIZE,
  describeDetail,
  detailFromBody,
  MalformedResponseError,
  parseOptimizeResponse,
  type Chip,
  type OptimizeRequest,
  type OptimizeResponse,
  type OptimizeSquadPlayer,
} from "./optimizerTypes";

export const DEFAULT_OPTIMIZER_URL = "http://127.0.0.1:8000";

/**
 * Long enough for a cold start plus a full solve, short enough that a hung
 * service does not hold a serverless invocation open to its own limit.
 */
export const DEFAULT_TIMEOUT_MS = 25_000;

export function optimizerBaseUrl(
  env: Record<string, string | undefined> = process.env,
): string {
  const raw = env.OPTIMIZER_URL?.trim();
  if (!raw) return DEFAULT_OPTIMIZER_URL;
  return raw.replace(/\/+$/, "");
}

export function optimizerTimeoutMs(
  env: Record<string, string | undefined> = process.env,
): number {
  const raw = Number(env.OPTIMIZER_TIMEOUT_MS);
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_TIMEOUT_MS;
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

export type OptimizeFailureKind =
  /** Nothing answered: wrong URL, service down, DNS, connection refused. */
  | "unreachable"
  /** It answered too slowly, or not at all before the deadline. */
  | "timeout"
  /** 422: the request was understood and refused. The message names the problem. */
  | "rejected"
  /** 5xx: the service broke on its own side. */
  | "server"
  /** 2xx with a body we cannot read as a result. */
  | "malformed";

export interface OptimizeFailure {
  ok: false;
  kind: OptimizeFailureKind;
  /** HTTP status, when there was one. */
  status?: number;
  /** A sentence for a human. Always populated. */
  message: string;
}

export interface OptimizeSuccess {
  ok: true;
  response: OptimizeResponse;
  /**
   * The body exactly as the service sent it.
   *
   * Kept because two things want the original rather than our reading of it:
   * the recommendation log, so a past answer stays explainable as it was given,
   * and the proxy, which forwards it unchanged so the browser parses with the
   * same function the server did.
   */
  raw: unknown;
}

export type OptimizeResult = OptimizeSuccess | OptimizeFailure;

/** HTTP status the proxy should answer with for each failure kind. */
export const FAILURE_STATUS: Record<OptimizeFailureKind, number> = {
  unreachable: 503,
  timeout: 504,
  rejected: 422,
  server: 502,
  malformed: 502,
};

// ---------------------------------------------------------------------------
// Building a request
// ---------------------------------------------------------------------------

export interface OptimizeOptions {
  freeTransfers: number;
  currentGw: number;
  horizon: number;
  chipsAvailable?: Chip[];
  season: string;
  maxOwnership?: number | null;
}

function clampInt(value: number, min: number, max: number, fallback: number): number {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

/**
 * A browser squad to an `OptimizeRequest`.
 *
 * Selling price is the player's current price. That is exactly right for a squad
 * built from scratch in the builder, where nothing has been held through a price
 * change. A squad imported from a real team arrives with reconstructed selling
 * prices from lib/reconstruct.ts and should be passed through instead — the
 * optimizer never computes selling price itself (contract.py).
 */
export function buildOptimizeRequest(
  squad: SquadState,
  index: PlayerIndex,
  options: OptimizeOptions,
): OptimizeRequest {
  const players: OptimizeSquadPlayer[] = [];
  for (const id of squad.picks) {
    if (id == null) continue;
    const player = index.get(id);
    if (!player) continue;
    players.push({
      element_id: id,
      selling_price: player.priceTenths,
      purchase_price: player.priceTenths,
    });
  }

  return {
    squad: players,
    bank: Math.max(0, Math.round(squad.bankTenths)),
    free_transfers: clampInt(options.freeTransfers, 0, MAX_FREE_TRANSFERS, 1),
    current_gw: clampInt(options.currentGw, 1, 38, 1),
    horizon: clampInt(options.horizon, MIN_HORIZON, MAX_HORIZON, DEFAULT_HORIZON),
    chips_available: options.chipsAvailable ?? [],
    season: options.season,
    max_ownership:
      options.maxOwnership === null || options.maxOwnership === undefined
        ? null
        : Math.min(100, Math.max(0, options.maxOwnership)),
  };
}

/**
 * Shape-check a request that arrived from the browser, before it costs a solve.
 *
 * Not a re-run of lib/squad.ts's rules — the optimizer enforces those and says
 * something specific when they fail. This only rejects what is not an
 * `OptimizeRequest` at all.
 */
export function validateOptimizeRequest(body: unknown): string | null {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return "Send an optimize request object.";
  }
  const req = body as Partial<OptimizeRequest>;

  if (!Array.isArray(req.squad) || req.squad.length !== SQUAD_SIZE) {
    return `A squad is ${SQUAD_SIZE} players — this one has ${
      Array.isArray(req.squad) ? req.squad.length : 0
    }.`;
  }
  for (const player of req.squad) {
    if (
      !player ||
      typeof player !== "object" ||
      !Number.isInteger((player as OptimizeSquadPlayer).element_id) ||
      !Number.isInteger((player as OptimizeSquadPlayer).selling_price) ||
      (player as OptimizeSquadPlayer).selling_price < 0
    ) {
      return "Every squad player needs an element id and a selling price in integer tenths.";
    }
  }
  if (!Number.isInteger(req.bank) || (req.bank as number) < 0) {
    return "Bank must be a whole number of tenths, and not negative.";
  }
  if (!Number.isInteger(req.current_gw) || (req.current_gw as number) < 1 || (req.current_gw as number) > 38) {
    return "Gameweek must be between 1 and 38.";
  }
  if (
    !Number.isInteger(req.horizon) ||
    (req.horizon as number) < MIN_HORIZON ||
    (req.horizon as number) > MAX_HORIZON
  ) {
    return `Horizon must be between ${MIN_HORIZON} and ${MAX_HORIZON} gameweeks.`;
  }
  if (
    !Number.isInteger(req.free_transfers) ||
    (req.free_transfers as number) < 0 ||
    (req.free_transfers as number) > MAX_FREE_TRANSFERS
  ) {
    return `Free transfers must be between 0 and ${MAX_FREE_TRANSFERS}.`;
  }
  return null;
}

// ---------------------------------------------------------------------------
// The call
// ---------------------------------------------------------------------------

export interface RequestOptimizeOptions {
  baseUrl?: string;
  timeoutMs?: number;
  /** Injected in tests. Defaults to the global fetch. */
  fetchImpl?: typeof fetch;
  signal?: AbortSignal;
}

async function readBody(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

/**
 * POST /optimize, and turn everything that can happen into an `OptimizeResult`.
 *
 * Nothing thrown from here reaches the caller: the four honest failures — down,
 * slow, refused, broken — are values, because each one wants different words on
 * screen and a try/catch loses the distinction.
 */
export async function requestOptimize(
  request: OptimizeRequest,
  options: RequestOptimizeOptions = {},
): Promise<OptimizeResult> {
  const baseUrl = options.baseUrl ?? optimizerBaseUrl();
  const timeoutMs = options.timeoutMs ?? optimizerTimeoutMs();
  const doFetch = options.fetchImpl ?? fetch;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const onOuterAbort = () => controller.abort();
  options.signal?.addEventListener("abort", onOuterAbort);

  let response: Response;
  try {
    response = await doFetch(`${baseUrl}/optimize`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(request),
      signal: controller.signal,
      // Never a CDN or a Next data cache: the answer depends on the squad and
      // on an xP snapshot that changes nightly. The service caches its own
      // solves on a key that includes data_as_of.
      cache: "no-store",
    });
  } catch (err) {
    const aborted =
      (err instanceof Error && (err.name === "AbortError" || err.name === "TimeoutError")) ||
      controller.signal.aborted;
    if (aborted) {
      return {
        ok: false,
        kind: "timeout",
        message:
          "The optimizer did not answer in time. It sleeps when idle and can take a few seconds to wake — try again.",
      };
    }
    return {
      ok: false,
      kind: "unreachable",
      message: "The optimizer is not reachable right now. Your squad is safe — try again in a moment.",
    };
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", onOuterAbort);
  }

  if (response.status === 422) {
    const detail = detailFromBody(await readBody(response));
    return {
      ok: false,
      kind: "rejected",
      status: 422,
      message:
        describeDetail(detail) ??
        "The optimizer could not use that squad, and did not say why.",
    };
  }

  if (!response.ok) {
    const detail = detailFromBody(await readBody(response));
    const described = describeDetail(detail);
    if (response.status >= 500) {
      return {
        ok: false,
        kind: "server",
        status: response.status,
        message: described
          ? `The optimizer failed: ${described}`
          : "The optimizer hit an error working that out. Try again shortly.",
      };
    }
    return {
      ok: false,
      kind: "rejected",
      status: response.status,
      message: described ?? `The optimizer refused that request (HTTP ${response.status}).`,
    };
  }

  const body = await readBody(response);
  try {
    return { ok: true, response: parseOptimizeResponse(body), raw: body };
  } catch (err) {
    return {
      ok: false,
      kind: "malformed",
      status: response.status,
      message:
        err instanceof MalformedResponseError
          ? err.message
          : "The optimizer's answer could not be read.",
    };
  }
}

// ---------------------------------------------------------------------------
// The anonymous recommendation log
//
// ARCHITECTURE.md and CLAUDE.md: this is the only way the model is ever
// evaluated, and it is unreconstructable after the fact. It is also the place a
// privacy leak would be easiest to introduce, so the row is built by one
// function with an explicit field list and nothing else may be added to it.
//
// What must never appear here: team id, IP, user agent, session, cookie,
// referrer, share code, or a timestamp of the *request* precise enough to
// correlate with one. `created_at` defaults to now() in the database, which is
// coarse enough to be a fact about the log rather than about a person.
// ---------------------------------------------------------------------------

export interface RecommendationLogRow {
  /** Hash of sorted element ids plus bank. Identifies the squad, never a person. */
  squad_hash: string;
  season: string;
  gw: number;
  model_version: string;
  solver_version: string;
  horizon: number;
  baseline_xp: number;
  /** The plans returned, as sent. */
  payload: unknown;
  solve_ms: number;
  data_as_of: string | null;
}

/** The fields this row is allowed to have. Anything else is a bug. */
export const LOG_ROW_FIELDS: readonly (keyof RecommendationLogRow)[] = [
  "squad_hash",
  "season",
  "gw",
  "model_version",
  "solver_version",
  "horizon",
  "baseline_xp",
  "payload",
  "solve_ms",
  "data_as_of",
];

/**
 * The squad state a hash is taken over, rebuilt from the request.
 *
 * `squadHash` sorts the ids, so the slot order the builder used does not matter
 * and a squad hashed here matches the hash the builder shows on screen.
 */
export function squadStateFromRequest(request: OptimizeRequest): SquadState {
  const picks: (number | null)[] = request.squad.map((player) => player.element_id);
  while (picks.length < SQUAD_SLOTS) picks.push(null);
  return { picks, bankTenths: request.bank };
}

/**
 * Build the log row. Pure — the write is the route's job, and it is allowed to
 * fail silently; building the row is not allowed to be wrong.
 */
export function buildLogRow(
  request: OptimizeRequest,
  response: OptimizeResponse,
  wirePayload: unknown,
): RecommendationLogRow {
  return {
    squad_hash: squadHash(squadStateFromRequest(request)),
    season: request.season,
    gw: request.current_gw,
    model_version: response.modelVersion,
    solver_version: response.solverVersion,
    horizon: request.horizon,
    baseline_xp: response.baselineXp,
    // The plans as the service sent them, so a past answer stays explainable
    // exactly as it was given. Nothing about the caller is in here.
    payload: {
      plans: (wirePayload as { plans?: unknown } | undefined)?.plans ?? [],
      truncated: response.truncated,
      max_ownership: request.max_ownership,
      free_transfers: request.free_transfers,
      chips_available: request.chips_available,
    },
    solve_ms: response.solveMs,
    data_as_of: response.dataAsOf || null,
  };
}
