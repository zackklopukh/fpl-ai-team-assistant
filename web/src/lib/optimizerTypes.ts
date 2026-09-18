/**
 * TypeScript mirrors of optimizer/contract.py.
 *
 * That file is the specification; this one is the translation. Two shapes live
 * here on purpose:
 *
 *   - `Wire*` types are exactly what comes off the socket — snake_case, and
 *     `n_fixtures` keyed by *strings*, because a Python `dict[int, int]` has no
 *     integer keys once it is JSON.
 *   - The unprefixed types are what the UI works with: camelCase, with
 *     `nFixtures` a real `Map<number, number>` keyed by element id.
 *
 * Parsing between them is this file's whole job, and it is deliberately
 * defensive: a field the service stops sending should degrade to a sensible
 * default rather than throw somewhere in a render.
 *
 * No `@/` imports and no React here — the test runner has no path aliases
 * configured, and these functions have to be testable on their own.
 *
 * Money is integer tenths (CLAUDE.md invariant 1). Nothing in this file divides
 * by ten; lib/format.ts owns that.
 */

// ---------------------------------------------------------------------------
// Request
// ---------------------------------------------------------------------------

export type Chip = "wildcard" | "freehit" | "bboost" | "3xc";

export const CHIPS: readonly Chip[] = ["wildcard", "freehit", "bboost", "3xc"];

/** contract.py: MAX_HORIZON. Beyond five the fixture information is too noisy. */
export const MAX_HORIZON = 5;
export const MIN_HORIZON = 1;
export const DEFAULT_HORIZON = 3;
/** contract.py: `free_transfers: int = Field(ge=0, le=5)`. */
export const MAX_FREE_TRANSFERS = 5;
export const SQUAD_SIZE = 15;

/**
 * One held player, with what they would fetch if sold.
 *
 * The optimizer does not compute selling price — the caller reconstructs it
 * from the transfer log and passes it in (contract.py). For a squad built from
 * scratch in the browser, purchase price and selling price coincide.
 */
export interface OptimizeSquadPlayer {
  element_id: number;
  /** Integer tenths. */
  selling_price: number;
  /** Integer tenths, display only. */
  purchase_price?: number | null;
}

export interface OptimizeRequest {
  squad: OptimizeSquadPlayer[];
  /** Integer tenths. */
  bank: number;
  free_transfers: number;
  current_gw: number;
  horizon: number;
  chips_available: Chip[];
  season: string;
  /** Differential mode: exclude players owned by more than this %. */
  max_ownership: number | null;
}

// ---------------------------------------------------------------------------
// Response — wire shapes
// ---------------------------------------------------------------------------

export interface WireTransfer {
  element_id: number;
  web_name: string;
  price: number;
}

export interface WireGameweekBreakdown {
  gw: number;
  xp: number;
  captain_element_id: number;
  /**
   * `dict[int, int]` in Python, **string keys** over JSON. Typing this
   * `Record<number, number>` would be a lie that happens to work at runtime and
   * breaks the moment anyone calls `Object.keys()` and compares to an id.
   */
  n_fixtures: Record<string, number>;
}

export interface WirePlan {
  label: string;
  transfers_in: WireTransfer[];
  transfers_out: WireTransfer[];
  hit_cost: number;
  xi: number[];
  bench_order: number[];
  captain: number;
  vice_captain: number;
  delta_xp: number;
  bank_after: number | null;
  per_gw_breakdown: WireGameweekBreakdown[];
  reasoning: string;
}

export interface WireOptimizeResponse {
  baseline_xp: number;
  plans: WirePlan[];
  model_version: string;
  solver_version: string;
  data_as_of: string;
  solve_ms: number;
  truncated: boolean;
}

// ---------------------------------------------------------------------------
// Response — parsed shapes
// ---------------------------------------------------------------------------

export interface Transfer {
  elementId: number;
  webName: string;
  /** Integer tenths. Banked on a sale, paid on a buy — see contract.py. */
  priceTenths: number;
}

export interface GameweekBreakdown {
  gw: number;
  xp: number;
  captainElementId: number;
  /** element id -> fixtures that gameweek. 0 is a blank, 2 a double. */
  nFixtures: Map<number, number>;
}

export interface Plan {
  label: string;
  transfersIn: Transfer[];
  transfersOut: Transfer[];
  /** Points, 4 per transfer beyond free. */
  hitCost: number;
  xi: number[];
  benchOrder: number[];
  captain: number;
  viceCaptain: number;
  /** Expected points gained over holding, after hits. */
  deltaXp: number;
  /** Integer tenths left in the bank, when the service supplies it. */
  bankAfterTenths: number | null;
  perGwBreakdown: GameweekBreakdown[];
  reasoning: string;
}

export interface OptimizeResponse {
  baselineXp: number;
  plans: Plan[];
  modelVersion: string;
  solverVersion: string;
  /** ISO timestamp of the price and xP data used. Never `now()`. */
  dataAsOf: string;
  solveMs: number;
  truncated: boolean;
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

function num(value: unknown, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const n = Number(value);
    if (Number.isFinite(n)) return n;
  }
  return fallback;
}

function intList(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => num(item, Number.NaN))
    .filter((item) => Number.isInteger(item));
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

/**
 * `{"328": 2}` -> `Map(328 => 2)`.
 *
 * The keys arrive as strings and the element ids they are compared against are
 * numbers. Doing this once, here, is the difference between a lookup that works
 * and one that silently misses every time.
 */
export function parseNFixtures(raw: unknown): Map<number, number> {
  const out = new Map<number, number>();
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    const elementId = Number(key);
    if (!Number.isInteger(elementId)) continue;
    out.set(elementId, num(value, 0));
  }
  return out;
}

function parseTransfer(raw: unknown): Transfer {
  const t = (raw ?? {}) as Partial<WireTransfer>;
  return {
    elementId: num(t.element_id),
    webName: str(t.web_name, `Player ${num(t.element_id)}`),
    priceTenths: num(t.price),
  };
}

function parseTransfers(raw: unknown): Transfer[] {
  return Array.isArray(raw) ? raw.map(parseTransfer) : [];
}

export function parseGameweekBreakdown(raw: unknown): GameweekBreakdown {
  const g = (raw ?? {}) as Partial<WireGameweekBreakdown>;
  return {
    gw: num(g.gw),
    xp: num(g.xp),
    captainElementId: num(g.captain_element_id),
    nFixtures: parseNFixtures(g.n_fixtures),
  };
}

export function parsePlan(raw: unknown): Plan {
  const p = (raw ?? {}) as Partial<WirePlan>;
  return {
    label: str(p.label, "Plan"),
    transfersIn: parseTransfers(p.transfers_in),
    transfersOut: parseTransfers(p.transfers_out),
    hitCost: num(p.hit_cost),
    xi: intList(p.xi),
    benchOrder: intList(p.bench_order),
    captain: num(p.captain),
    viceCaptain: num(p.vice_captain),
    deltaXp: num(p.delta_xp),
    // `null` and "absent" both mean the service did not say. Zero would be a
    // claim about the bank, so it must not be the fallback.
    bankAfterTenths:
      p.bank_after === null || p.bank_after === undefined
        ? null
        : num(p.bank_after),
    perGwBreakdown: Array.isArray(p.per_gw_breakdown)
      ? p.per_gw_breakdown.map(parseGameweekBreakdown)
      : [],
    reasoning: str(p.reasoning),
  };
}

export class MalformedResponseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MalformedResponseError";
  }
}

/**
 * A wire body to the shape the UI renders.
 *
 * `plans` missing is the one thing worth refusing: a response with no plans is
 * not a degraded answer, it is a different service.
 */
export function parseOptimizeResponse(raw: unknown): OptimizeResponse {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new MalformedResponseError("The optimizer returned something that is not a result.");
  }
  const r = raw as Partial<WireOptimizeResponse>;
  if (!Array.isArray(r.plans)) {
    throw new MalformedResponseError("The optimizer returned a result with no plans in it.");
  }
  return {
    baselineXp: num(r.baseline_xp),
    plans: r.plans.map(parsePlan),
    modelVersion: str(r.model_version, "unknown"),
    solverVersion: str(r.solver_version, "unknown"),
    dataAsOf: str(r.data_as_of),
    solveMs: num(r.solve_ms),
    truncated: r.truncated === true,
  };
}

/** A plan that moves nobody. "Hold" is a first-class result, not a null one. */
export function isHoldPlan(plan: Plan): boolean {
  return plan.transfersIn.length === 0 && plan.transfersOut.length === 0;
}

// ---------------------------------------------------------------------------
// 422 bodies
//
// FastAPI answers a 422 in two different shapes and the web app gets both:
//
//   pydantic validation      {"detail": [{"loc": [...], "msg": "...", ...}]}
//   the service's own checks {"detail": "squad has 6 defenders, expected 5"}
//
// The second is the useful one — app.py raises HTTPException with a sentence
// written for a human. The first is a list of objects addressed to a developer.
// Both have to turn into something a user can read. See the report note: this
// normalisation belongs on the service side, not here.
// ---------------------------------------------------------------------------

export interface PydanticErrorItem {
  loc?: (string | number)[];
  msg?: string;
  type?: string;
}

function isPydanticErrorItem(value: unknown): value is PydanticErrorItem {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const item = value as PydanticErrorItem;
  return typeof item.msg === "string" || Array.isArray(item.loc);
}

/** `["body", "squad", 3, "selling_price"]` -> "squad → player 4 → selling price". */
function describeLocation(loc: (string | number)[] | undefined): string | null {
  if (!Array.isArray(loc) || loc.length === 0) return null;
  const parts = loc
    // "body" is where every request field lives; saying so tells nobody anything.
    .filter((part) => part !== "body")
    .map((part) =>
      typeof part === "number" ? `player ${part + 1}` : String(part).replace(/_/g, " "),
    );
  return parts.length > 0 ? parts.join(" → ") : null;
}

/**
 * Either 422 shape to one sentence a person can act on.
 *
 * Returns null when the body carries nothing usable, so the caller can fall
 * back to its own wording rather than printing "[object Object]".
 */
export function describeDetail(detail: unknown): string | null {
  if (typeof detail === "string") {
    const trimmed = detail.trim();
    return trimmed === "" ? null : trimmed;
  }

  if (Array.isArray(detail)) {
    const lines = detail
      .filter(isPydanticErrorItem)
      .map((item) => {
        const where = describeLocation(item.loc);
        const what = (item.msg ?? "is not valid").trim();
        return where ? `${where}: ${what}` : what;
      })
      .filter((line) => line !== "");
    if (lines.length === 0) return null;
    // A handful is informative; forty is a wall. The first few name the problem.
    const shown = lines.slice(0, 4);
    const rest = lines.length - shown.length;
    return shown.join("; ") + (rest > 0 ? `; and ${rest} more` : "");
  }

  // A bare object, or anything else. Pull a message out if one is in there.
  if (detail && typeof detail === "object") {
    const msg = (detail as { msg?: unknown; message?: unknown });
    if (typeof msg.msg === "string") return msg.msg;
    if (typeof msg.message === "string") return msg.message;
  }

  return null;
}

/** The `detail` field of an error body, whichever shape it arrived in. */
export function detailFromBody(body: unknown): unknown {
  if (!body || typeof body !== "object" || Array.isArray(body)) return undefined;
  return (body as { detail?: unknown }).detail;
}
