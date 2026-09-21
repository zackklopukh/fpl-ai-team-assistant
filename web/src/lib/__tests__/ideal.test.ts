/**
 * The ideal-fifteen page's logic: requests in both modes, the saved-squad
 * approximation, parsing, the pitch layout and the builder hand-off.
 *
 * The network is mocked. Relative imports: the runner has no path aliases.
 */

import { describe, expect, it } from "vitest";

import {
  buildScratchRequest,
  buildWildcardRequest,
  lineupFromResponse,
  parseBudgetInput,
  parseFormation,
  requestIdeal,
  toBuilderSquad,
  validateIdealRequest,
  wildcardBudgetTenths,
  wildcardChanges,
  wildcardFromImport,
  wildcardFromSavedSquad,
} from "../ideal";
import {
  parseIdealSquadResponse,
  type WireIdealPlayer,
  type WireIdealSquadResponse,
} from "../optimizerTypes";
import {
  SLOT_POSITIONS,
  buildPlayerIndex,
  type Position,
  type SquadPlayer,
  type SquadState,
} from "../squad";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const TYPE: Record<Position, number> = { GKP: 1, DEF: 2, MID: 3, FWD: 4 };

/** Fifteen builder players, ids 101-115 in slot order, all £5.0m-£6.4m. */
const PLAYERS: SquadPlayer[] = SLOT_POSITIONS.map((position, i) => ({
  elementId: 101 + i,
  webName: `P${101 + i}`,
  position,
  teamFplId: (i % 10) + 1,
  teamName: `Club ${(i % 10) + 1}`,
  teamShortName: `C${(i % 10) + 1}`,
  teamCode: 3,
  priceTenths: 50 + i,
}));
const INDEX = buildPlayerIndex(PLAYERS);

const SAVED: SquadState = { picks: PLAYERS.map((p) => p.elementId), bankTenths: 23 };

function wirePlayer(id: number, position: Position, extra: Partial<WireIdealPlayer> = {}): WireIdealPlayer {
  return {
    element_id: id,
    web_name: `W${id}`,
    element_type: TYPE[position],
    team_fpl_id: 1,
    price: 60,
    cost: 60,
    kept: false,
    xp: 10,
    ...extra,
  };
}

/** A 3-4-3: GK 1, DEF 2-6, MID 7-11, FWD 12-14 ... numbered 1-15. */
function scratchWire(): WireIdealSquadResponse {
  const players = [
    wirePlayer(1, "GKP"),
    wirePlayer(2, "GKP"),
    wirePlayer(3, "DEF"),
    wirePlayer(4, "DEF"),
    wirePlayer(5, "DEF"),
    wirePlayer(6, "DEF"),
    wirePlayer(7, "DEF"),
    wirePlayer(8, "MID"),
    wirePlayer(9, "MID"),
    wirePlayer(10, "MID"),
    wirePlayer(11, "MID"),
    wirePlayer(12, "MID"),
    wirePlayer(13, "FWD"),
    wirePlayer(14, "FWD"),
    wirePlayer(15, "FWD"),
  ];
  return {
    players,
    // Deliberately interleaved: rows must come from positions, not order.
    xi: [13, 3, 1, 8, 4, 9, 14, 5, 10, 11, 15],
    bench_order: [2, 6, 12, 7],
    captain: 13,
    vice_captain: 8,
    formation: "3-4-3",
    cost: 900,
    budget: 1000,
    bank_after: 100,
    total_xp: 172.9,
    per_gw_breakdown: [
      { gw: 6, xp: 57, captain_element_id: 13, n_fixtures: { "13": 2, "3": 0 } },
      { gw: 7, xp: 59, captain_element_id: 8, n_fixtures: {} },
    ],
    kept_count: null,
    gain_vs_hold: null,
    reasoning: "The highest-scoring fifteen.",
    model_version: "fitted-0.1",
    solver_version: "mip-0.1",
    data_as_of: "2026-09-21T20:46:45Z",
    season: "2026-27",
    solve_ms: 2302,
    truncated: false,
  };
}

const OPTIONS = { currentGw: 6, horizon: 3, season: "2026-27" };

// ---------------------------------------------------------------------------

describe("request building", () => {
  it("from scratch sends a budget in integer tenths and no squad", () => {
    const req = buildScratchRequest(850, OPTIONS);
    expect(req.budget).toBe(850);
    expect(Number.isInteger(req.budget)).toBe(true);
    expect(req.squad).toBeNull();
    expect(req.bank).toBe(0);
    expect(req.current_gw).toBe(6);
    expect(req.horizon).toBe(3);
    expect(req.max_ownership).toBeNull();
    expect(validateIdealRequest(req)).toBeNull();
  });

  it("clamps the horizon and passes the ownership cap", () => {
    const req = buildScratchRequest(1000, { ...OPTIONS, horizon: 9, maxOwnership: 15 });
    expect(req.horizon).toBe(5);
    expect(req.max_ownership).toBe(15);
  });

  it("parses a budget as text, never through a float", () => {
    expect(parseBudgetInput("100.0")).toBe(1000);
    expect(parseBudgetInput("85")).toBe(850);
    expect(parseBudgetInput("£85.5m")).toBe(855);
    expect(parseBudgetInput("0.3")).toBe(3);
    expect(parseBudgetInput("85.55")).toBeNull();
    expect(parseBudgetInput("-5")).toBeNull();
    expect(parseBudgetInput("abc")).toBeNull();
    expect(parseBudgetInput("200.1")).toBeNull();
  });

  it("a wildcard from a team-ID import carries exact selling prices and the bank", () => {
    const result = wildcardFromImport({
      picksAvailable: true,
      bankTenths: 7,
      players: PLAYERS.map((p) => ({
        elementId: p.elementId,
        webName: p.webName,
        sellingPriceTenths: p.priceTenths - 1,
        purchasePriceTenths: p.priceTenths - 3,
      })),
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.squad.approximate).toBe(false);

    const req = buildWildcardRequest(result.squad, OPTIONS);
    expect(req.squad).toHaveLength(15);
    expect(req.squad![0]).toEqual({ element_id: 101, selling_price: 49, purchase_price: 47 });
    expect(req.bank).toBe(7);
    for (const p of req.squad!) expect(Number.isInteger(p.selling_price)).toBe(true);
    expect(validateIdealRequest(req)).toBeNull();
  });

  it("refuses an import whose picks are not public yet, with its reason", () => {
    const result = wildcardFromImport({
      picksAvailable: false,
      bankTenths: null,
      note: "This team's picks are not public yet.",
      players: [],
    });
    expect(result).toEqual({ ok: false, message: "This team's picks are not public yet." });
  });

  it("validation rejects a short squad and a float bank", () => {
    const req = buildScratchRequest(1000, OPTIONS);
    expect(validateIdealRequest({ ...req, squad: [{ element_id: 1, selling_price: 50 }] })).toMatch(/15/);
    expect(validateIdealRequest({ ...req, bank: 1.5 })).toMatch(/tenths/);
    expect(validateIdealRequest({ ...req, budget: 2500 })).toMatch(/Budget/);
  });
});

describe("the saved-squad approximation", () => {
  it("values each player at today's price, flags it approximate, and keeps the bank", () => {
    const result = wildcardFromSavedSquad(SAVED, INDEX);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.squad.approximate).toBe(true);
    expect(result.squad.source).toBe("saved");
    expect(result.squad.bankTenths).toBe(23);
    for (const h of result.squad.holdings) {
      expect(h.sellingPriceTenths).toBe(INDEX.get(h.elementId)!.priceTenths);
      expect(h.purchasePriceTenths).toBeNull();
    }
    // 50+51+...+64 = 855, plus 23 in the bank.
    expect(wildcardBudgetTenths(result.squad)).toBe(878);

    const req = buildWildcardRequest(result.squad, OPTIONS);
    expect(req.squad![0]).toEqual({ element_id: 101, selling_price: 50, purchase_price: null });
    expect(req.bank).toBe(23);
  });

  it("refuses an incomplete saved squad with a sentence", () => {
    const picks = SAVED.picks.slice();
    picks[4] = null;
    const result = wildcardFromSavedSquad({ picks, bankTenths: 0 }, INDEX);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.message).toMatch(/14 of 15/);
  });
});

describe("parsing the response", () => {
  it("reads scratch mode, with the wildcard-only fields null", () => {
    const r = parseIdealSquadResponse(scratchWire());
    expect(r.players).toHaveLength(15);
    expect(r.keptCount).toBeNull();
    expect(r.gainVsHold).toBeNull();
    expect(r.costTenths).toBe(900);
    expect(r.budgetTenths).toBe(1000);
    expect(r.bankAfterTenths).toBe(100);
    expect(r.formation).toBe("3-4-3");
    expect(r.perGwBreakdown[0].nFixtures.get(13)).toBe(2);
    expect(r.perGwBreakdown[0].nFixtures.get(3)).toBe(0);
  });

  it("reads kept and cost separately from price in wildcard mode", () => {
    const wire = scratchWire();
    wire.players[2] = wirePlayer(3, "DEF", { kept: true, price: 58, cost: 56 });
    wire.kept_count = 1;
    wire.gain_vs_hold = 11.4;
    const r = parseIdealSquadResponse(wire);
    const kept = r.players.find((p) => p.elementId === 3)!;
    expect(kept.kept).toBe(true);
    expect(kept.priceTenths).toBe(58);
    expect(kept.costTenths).toBe(56);
    expect(r.keptCount).toBe(1);
    expect(r.gainVsHold).toBe(11.4);
  });

  it("treats a missing cost as the list price, not zero", () => {
    const wire = scratchWire();
    const { cost: _cost, ...noCost } = wire.players[0];
    void _cost;
    wire.players[0] = noCost as WireIdealPlayer;
    expect(parseIdealSquadResponse(wire).players[0].costTenths).toBe(60);
  });

  it("refuses a body with no players", () => {
    expect(() => parseIdealSquadResponse({ xi: [] })).toThrow(/no players/);
  });
});

describe("the pitch", () => {
  it("splits the XI into formation rows by position and the bench in bench_order", () => {
    const lineup = lineupFromResponse(parseIdealSquadResponse(scratchWire()));
    expect(lineup.rows.GKP.map((p) => p.elementId)).toEqual([1]);
    expect(lineup.rows.DEF.map((p) => p.elementId)).toEqual([3, 4, 5]);
    expect(lineup.rows.MID.map((p) => p.elementId)).toEqual([8, 9, 10, 11]);
    expect(lineup.rows.FWD.map((p) => p.elementId)).toEqual([13, 14, 15]);
    expect(lineup.bench.map((p) => p.elementId)).toEqual([2, 6, 12, 7]);
    expect(lineup.formationMatches).toBe(true);
  });

  it("notices a formation string that disagrees with the players", () => {
    const wire = scratchWire();
    wire.formation = "4-4-2";
    expect(lineupFromResponse(parseIdealSquadResponse(wire)).formationMatches).toBe(false);
    expect(parseFormation("5-3-2")).toEqual({ GKP: 1, DEF: 5, MID: 3, FWD: 2 });
    expect(parseFormation("nonsense")).toBeNull();
  });
});

describe("wildcard changes", () => {
  it("lists who comes in, who goes out, and kept players whose cost is not their price", () => {
    const wire = scratchWire();
    // Two of the result are players the manager holds (ids 101, 108 in the builder).
    wire.players[0] = wirePlayer(101, "GKP", { kept: true, price: 50, cost: 50 });
    wire.players[7] = wirePlayer(108, "MID", { kept: true, price: 60, cost: 58 });
    const r = parseIdealSquadResponse(wire);
    const saved = wildcardFromSavedSquad(SAVED, INDEX);
    if (!saved.ok) throw new Error("fixture");
    const changes = wildcardChanges(r, saved.squad);
    expect(changes.kept.map((p) => p.elementId)).toEqual([101, 108]);
    expect(changes.discounted.map((p) => p.elementId)).toEqual([108]);
    expect(changes.incoming).toHaveLength(13);
    expect(changes.outgoing).toHaveLength(13);
    expect(changes.outgoing.some((h) => h.elementId === 101)).toBe(false);
  });
});

describe("builder hand-off", () => {
  it("lays the fifteen into 2-5-5-3 slot order with bank_after as the bank", () => {
    const squad = toBuilderSquad(parseIdealSquadResponse(scratchWire()));
    expect(squad.bankTenths).toBe(100);
    expect(squad.picks).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]);
    expect(squad.picks).toHaveLength(15);
  });
});

describe("requestIdeal", () => {
  function fakeFetch(status: number, body: unknown, capture?: { url?: string; body?: string }) {
    return (async (url: string, init?: RequestInit) => {
      if (capture) {
        capture.url = url;
        capture.body = init?.body as string;
      }
      return new Response(JSON.stringify(body), { status });
    }) as unknown as typeof fetch;
  }

  it("posts to /squad/ideal without a squad key for from-scratch", async () => {
    const capture: { url?: string; body?: string } = {};
    const result = await requestIdeal(buildScratchRequest(1000, OPTIONS), {
      baseUrl: "http://opt",
      fetchImpl: fakeFetch(200, scratchWire(), capture),
    });
    expect(result.ok).toBe(true);
    expect(capture.url).toBe("http://opt/squad/ideal");
    expect(JSON.parse(capture.body!)).not.toHaveProperty("squad");
  });

  it("turns a 422 into the service's own sentence", async () => {
    const result = await requestIdeal(buildScratchRequest(500, OPTIONS), {
      baseUrl: "http://opt",
      fetchImpl: fakeFetch(422, {
        detail: "no legal fifteen fits a budget of 500 tenths (the cheapest possible squad costs more)",
      }),
    });
    expect(result).toMatchObject({ ok: false, kind: "rejected" });
    if (!result.ok) expect(result.message).toMatch(/^no legal fifteen fits/);
  });

  it("an unreachable service is a calm value, not a throw", async () => {
    const result = await requestIdeal(buildScratchRequest(1000, OPTIONS), {
      baseUrl: "http://opt",
      fetchImpl: (async () => {
        throw new TypeError("fetch failed");
      }) as unknown as typeof fetch,
    });
    expect(result).toMatchObject({ ok: false, kind: "unreachable" });
  });
});
