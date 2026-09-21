/**
 * The ideal fifteen: building the request, calling the service, and reading the
 * answer into the shapes the /ideal page draws.
 *
 * Two modes, one endpoint (contract.py: IdealSquadRequest):
 *
 *   - From scratch: a budget, no squad. Every player costs his list price.
 *   - Wildcard: the fifteen held plus the bank. A kept player costs his
 *     *selling* price, so the money available is bank + what the fifteen sell
 *     for, not £100.0m.
 *
 * Where a wildcard's selling prices come from matters, and the page says so:
 * an FPL team-ID import reconstructs them exactly from the transfer log; a squad
 * saved in the builder stores only element ids, so its selling prices are
 * approximated by today's list prices.
 *
 * The service call mirrors `requestOptimize` in lib/optimizer.ts — same
 * timeout, same failure kinds, same sentences — because that function is
 * hard-wired to /optimize and the two must fail the same way.
 *
 * Relative imports only: the test runner has no path aliases.
 *
 * Money is integer tenths (CLAUDE.md invariant 1). Nothing here divides by ten.
 */

import {
  FAILURE_STATUS,
  optimizerBaseUrl,
  optimizerTimeoutMs,
  type OptimizeFailure,
  type RequestOptimizeOptions,
} from "./optimizer";
import {
  DEFAULT_HORIZON,
  MAX_HORIZON,
  MAX_IDEAL_BUDGET_TENTHS,
  MIN_HORIZON,
  MalformedResponseError,
  SQUAD_SIZE,
  describeDetail,
  detailFromBody,
  parseIdealSquadResponse,
  type IdealPlayer,
  type IdealSquadRequest,
  type IdealSquadResponse,
  type OptimizeSquadPlayer,
} from "./optimizerTypes";
import {
  POSITIONS,
  POSITION_BY_ELEMENT_TYPE,
  SLOT_POSITIONS,
  type ElementType,
  type PlayerIndex,
  type Position,
  type SquadState,
} from "./squad";

export { FAILURE_STATUS };

// ---------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------

/** One player held going into a wildcard, with what he would sell for. */
export interface WildcardHolding {
  elementId: number;
  /** Integer tenths. */
  sellingPriceTenths: number;
  /** Integer tenths, display only. Null when unknown (a builder squad). */
  purchasePriceTenths: number | null;
  /** For naming the players who go out. */
  webName: string | null;
}

export interface WildcardSquad {
  holdings: WildcardHolding[];
  /** Integer tenths. */
  bankTenths: number;
  /**
   * True when selling prices are guesses — a builder squad valued at today's
   * list prices. The page must say so; the team-ID import is exact.
   */
  approximate: boolean;
  source: "team-id" | "saved";
}

export type WildcardResult =
  | { ok: true; squad: WildcardSquad }
  | { ok: false; message: string };

/**
 * A squad saved in the builder, as a wildcard input.
 *
 * The builder stores element ids and a bank — never what was paid — so the
 * selling price is unknowable. Today's list price stands in. That is exact for
 * a player bought at today's price and too generous for one held through a rise
 * (a seller keeps only half of it), which is why the result is flagged.
 */
export function wildcardFromSavedSquad(
  squad: SquadState,
  index: PlayerIndex,
): WildcardResult {
  const ids = squad.picks.filter((id): id is number => id != null);
  if (ids.length < SQUAD_SIZE) {
    return {
      ok: false,
      message:
        ids.length === 0
          ? "There is no squad saved in this browser yet. Build one, or use your FPL team ID."
          : `Your saved squad has ${ids.length} of ${SQUAD_SIZE} players. Finish it in the builder first, or use your FPL team ID.`,
    };
  }
  const holdings: WildcardHolding[] = [];
  for (const id of ids) {
    const player = index.get(id);
    if (!player) {
      return {
        ok: false,
        message:
          "Your saved squad has a player who is not in this season's list. Replace him in the builder first.",
      };
    }
    holdings.push({
      elementId: id,
      sellingPriceTenths: player.priceTenths,
      purchasePriceTenths: null,
      webName: player.webName,
    });
  }
  return {
    ok: true,
    squad: {
      holdings,
      bankTenths: Math.max(0, Math.round(squad.bankTenths)),
      approximate: true,
      source: "saved",
    },
  };
}

/** The slice of GET /api/squad/{teamId} a wildcard needs. */
export interface ImportedSquadForWildcard {
  picksAvailable: boolean;
  bankTenths: number | null;
  note?: string | null;
  players: {
    elementId: number;
    webName: string | null;
    sellingPriceTenths: number;
    purchasePriceTenths: number;
  }[];
}

/** An FPL team-ID import, as a wildcard input. Selling prices are exact. */
export function wildcardFromImport(imported: ImportedSquadForWildcard): WildcardResult {
  if (!imported.picksAvailable) {
    return {
      ok: false,
      message:
        imported.note ??
        "That team's picks are not public yet. FPL keeps a squad private until the deadline passes.",
    };
  }
  if (imported.players.length !== SQUAD_SIZE) {
    return {
      ok: false,
      message: `That team came back with ${imported.players.length} players, not ${SQUAD_SIZE}, so it cannot be rebuilt.`,
    };
  }
  return {
    ok: true,
    squad: {
      holdings: imported.players.map((p) => ({
        elementId: p.elementId,
        sellingPriceTenths: p.sellingPriceTenths,
        purchasePriceTenths: p.purchasePriceTenths,
        webName: p.webName,
      })),
      bankTenths: Math.max(0, imported.bankTenths ?? 0),
      approximate: false,
      source: "team-id",
    },
  };
}

/** What the wildcard's money is: bank plus what the fifteen sell for. */
export function wildcardBudgetTenths(squad: WildcardSquad): number {
  return squad.holdings.reduce((sum, h) => sum + h.sellingPriceTenths, squad.bankTenths);
}

/**
 * "85", "85.0", "£85.5m" -> 850, 855. Null for anything that is not a sum of
 * money with at most one decimal place, or is outside what the service takes.
 *
 * Parsed as text, not multiplied as a float: `85.5 * 10` happens to be exact,
 * but the rule in this codebase is that money never passes through a float.
 */
export function parseBudgetInput(raw: string): number | null {
  const cleaned = raw.trim().replace(/^£/, "").replace(/m$/i, "").trim();
  const match = /^(\d{1,3})(?:\.(\d))?$/.exec(cleaned);
  if (!match) return null;
  const tenths = Number(match[1]) * 10 + (match[2] ? Number(match[2]) : 0);
  if (tenths < 0 || tenths > MAX_IDEAL_BUDGET_TENTHS) return null;
  return tenths;
}

// ---------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------

export interface IdealOptions {
  currentGw: number;
  horizon: number;
  /** The route overrides this with the app's own season. */
  season: string;
  maxOwnership?: number | null;
}

function clampInt(value: number, min: number, max: number, fallback: number): number {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function commonFields(options: IdealOptions) {
  return {
    current_gw: clampInt(options.currentGw, 1, 38, 1),
    horizon: clampInt(options.horizon, MIN_HORIZON, MAX_HORIZON, DEFAULT_HORIZON),
    season: options.season,
    max_ownership:
      options.maxOwnership === null || options.maxOwnership === undefined
        ? null
        : Math.min(100, Math.max(0, options.maxOwnership)),
  };
}

/** From scratch: a budget and no squad. */
export function buildScratchRequest(
  budgetTenths: number,
  options: IdealOptions,
): IdealSquadRequest {
  return {
    ...commonFields(options),
    squad: null,
    bank: 0,
    budget: clampInt(budgetTenths, 0, MAX_IDEAL_BUDGET_TENTHS, 1000),
  };
}

/** Wildcard: the fifteen held, with selling prices, plus the bank. */
export function buildWildcardRequest(
  squad: WildcardSquad,
  options: IdealOptions,
): IdealSquadRequest {
  const players: OptimizeSquadPlayer[] = squad.holdings.map((h) => ({
    element_id: h.elementId,
    selling_price: Math.max(0, Math.round(h.sellingPriceTenths)),
    purchase_price:
      h.purchasePriceTenths === null ? null : Math.max(0, Math.round(h.purchasePriceTenths)),
  }));
  return {
    ...commonFields(options),
    squad: players,
    bank: Math.max(0, Math.round(squad.bankTenths)),
    // Ignored by the service in wildcard mode; the budget is bank + sales.
    budget: 1000,
  };
}

/**
 * Shape-check a request from the browser before it costs a solve. The service
 * enforces the rules and words its own refusals; this only rejects what is not
 * an ideal-squad request at all.
 */
export function validateIdealRequest(body: unknown): string | null {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return "Send an ideal-squad request object.";
  }
  const req = body as Partial<IdealSquadRequest>;
  if (req.squad !== undefined && req.squad !== null) {
    if (!Array.isArray(req.squad) || req.squad.length !== SQUAD_SIZE) {
      return `A wildcard starts from ${SQUAD_SIZE} players — this has ${
        Array.isArray(req.squad) ? req.squad.length : 0
      }.`;
    }
    for (const player of req.squad) {
      if (
        !player ||
        typeof player !== "object" ||
        !Number.isInteger(player.element_id) ||
        !Number.isInteger(player.selling_price) ||
        player.selling_price < 0
      ) {
        return "Every squad player needs an element id and a selling price in integer tenths.";
      }
    }
  }
  if (req.bank !== undefined && (!Number.isInteger(req.bank) || (req.bank as number) < 0)) {
    return "Bank must be a whole number of tenths, and not negative.";
  }
  if (
    req.budget !== undefined &&
    (!Number.isInteger(req.budget) ||
      (req.budget as number) < 0 ||
      (req.budget as number) > MAX_IDEAL_BUDGET_TENTHS)
  ) {
    return `Budget must be a whole number of tenths between 0 and ${MAX_IDEAL_BUDGET_TENTHS}.`;
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
  return null;
}

// ---------------------------------------------------------------------------
// The call — mirrors requestOptimize in lib/optimizer.ts
// ---------------------------------------------------------------------------

export interface IdealSuccess {
  ok: true;
  response: IdealSquadResponse;
  /** Forwarded unchanged by the proxy so the browser parses it the same way. */
  raw: unknown;
}

export type IdealResult = IdealSuccess | OptimizeFailure;

async function readBody(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

/** POST /squad/ideal. Nothing thrown reaches the caller; failures are values. */
export async function requestIdeal(
  request: IdealSquadRequest,
  options: RequestOptimizeOptions = {},
): Promise<IdealResult> {
  const baseUrl = options.baseUrl ?? optimizerBaseUrl();
  const timeoutMs = options.timeoutMs ?? optimizerTimeoutMs();
  const doFetch = options.fetchImpl ?? fetch;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const onOuterAbort = () => controller.abort();
  options.signal?.addEventListener("abort", onOuterAbort);

  // A from-scratch request carries no squad key at all, as the contract says.
  const { squad, ...rest } = request;
  const body = squad ? request : rest;

  let response: Response;
  try {
    response = await doFetch(`${baseUrl}/squad/ideal`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
      // The service caches identical solves on a key that includes data_as_of.
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
      message: describeDetail(detail) ?? "The optimizer could not build that team, and did not say why.",
    };
  }

  if (!response.ok) {
    const described = describeDetail(detailFromBody(await readBody(response)));
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

  const raw = await readBody(response);
  try {
    return { ok: true, response: parseIdealSquadResponse(raw), raw };
  } catch (err) {
    return {
      ok: false,
      kind: "malformed",
      status: response.status,
      message:
        err instanceof MalformedResponseError ? err.message : "The optimizer's answer could not be read.",
    };
  }
}

// ---------------------------------------------------------------------------
// Reading the answer
// ---------------------------------------------------------------------------

export function positionOfElementType(elementType: number): Position | null {
  return elementType === 1 || elementType === 2 || elementType === 3 || elementType === 4
    ? POSITION_BY_ELEMENT_TYPE[elementType as ElementType]
    : null;
}

/** "3-4-3" -> { GKP: 1, DEF: 3, MID: 4, FWD: 3 }, or null if it is not one. */
export function parseFormation(formation: string): Record<Position, number> | null {
  const match = /^(\d)-(\d)-(\d)$/.exec(formation.trim());
  if (!match) return null;
  return { GKP: 1, DEF: Number(match[1]), MID: Number(match[2]), FWD: Number(match[3]) };
}

export interface Lineup {
  /** The XI in pitch rows, goalkeeper first, each row in the order `xi` gave. */
  rows: Record<Position, IdealPlayer[]>;
  /** The four substitutes, in `bench_order`. */
  bench: IdealPlayer[];
  /** Row sizes agree with the `formation` string. False would be a service bug. */
  formationMatches: boolean;
}

/**
 * Split the answer into what FPL draws: the XI by position, then the bench.
 *
 * Rows are built from each player's own position, not from the formation
 * string, so a disagreement between the two cannot put a defender in midfield;
 * it is reported instead.
 */
export function lineupFromResponse(response: IdealSquadResponse): Lineup {
  const byId = new Map(response.players.map((p) => [p.elementId, p]));
  const rows: Record<Position, IdealPlayer[]> = { GKP: [], DEF: [], MID: [], FWD: [] };
  for (const id of response.xi) {
    const player = byId.get(id);
    const position = player ? positionOfElementType(player.elementType) : null;
    if (player && position) rows[position].push(player);
  }
  const bench = response.benchOrder
    .map((id) => byId.get(id))
    .filter((p): p is IdealPlayer => p !== undefined);

  const expected = parseFormation(response.formation);
  const formationMatches =
    expected !== null && POSITIONS.every((position) => rows[position].length === expected[position]);

  return { rows, bench, formationMatches };
}

export interface WildcardChanges {
  /** New signings, at list price. */
  incoming: IdealPlayer[];
  /** Players sold, at their selling price. */
  outgoing: WildcardHolding[];
  kept: IdealPlayer[];
  /** Kept players whose selling price is not today's list price. */
  discounted: IdealPlayer[];
}

/** Who comes in, who goes out, who stays. */
export function wildcardChanges(
  response: IdealSquadResponse,
  squad: WildcardSquad,
): WildcardChanges {
  const inResult = new Set(response.players.map((p) => p.elementId));
  const kept = response.players.filter((p) => p.kept);
  return {
    incoming: response.players.filter((p) => !p.kept),
    outgoing: squad.holdings.filter((h) => !inResult.has(h.elementId)),
    kept,
    discounted: kept.filter((p) => p.costTenths !== p.priceTenths),
  };
}

/**
 * The fifteen as a builder squad, for `useSquad().replaceSquad`.
 *
 * Laid into the builder's fixed 2-5-5-3 slot order by position. The bank is the
 * service's `bank_after`: what this manager has left once the fifteen are paid
 * for at *their* costs.
 */
export function toBuilderSquad(response: IdealSquadResponse): SquadState {
  const queues = new Map<Position, number[]>(POSITIONS.map((p) => [p, []]));
  for (const player of response.players) {
    const position = positionOfElementType(player.elementType);
    if (position) queues.get(position)!.push(player.elementId);
  }
  const picks = SLOT_POSITIONS.map((position) => queues.get(position)!.shift() ?? null);
  return { picks, bankTenths: response.bankAfterTenths };
}

/** "GW6" or "GW6-8", from the breakdown, falling back to the request. */
export function horizonLabel(
  response: IdealSquadResponse | null,
  fallbackGw: number,
  fallbackHorizon: number,
): string {
  const gws = response?.perGwBreakdown.map((g) => g.gw) ?? [];
  const first = gws.length > 0 ? Math.min(...gws) : fallbackGw;
  const last = gws.length > 0 ? Math.max(...gws) : fallbackGw + fallbackHorizon - 1;
  return first === last ? `GW${first}` : `GW${first}-${last}`;
}
