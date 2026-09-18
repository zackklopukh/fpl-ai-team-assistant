/**
 * The web side of the optimizer contract: parsing, failure states, the log row,
 * and that a plan actually renders.
 *
 * The network is always mocked. These tests are about what this app does with
 * an answer, not about whether the solver is right — optimizer/tests owns that.
 *
 * Imports are relative because the test runner has no path aliases configured.
 */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import PlanCard from "../../components/PlanCard";
import {
  DEFAULT_OPTIMIZER_URL,
  LOG_ROW_FIELDS,
  buildLogRow,
  buildOptimizeRequest,
  optimizerBaseUrl,
  requestOptimize,
  squadStateFromRequest,
  validateOptimizeRequest,
} from "../optimizer";
import {
  describeDetail,
  isHoldPlan,
  parseNFixtures,
  parseOptimizeResponse,
  type WireOptimizeResponse,
} from "../optimizerTypes";
import {
  SQUAD_SIZE,
  buildPlayerIndex,
  squadHash,
  type SquadPlayer,
  type SquadState,
} from "../squad";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const POSITION_ORDER = [
  "GKP",
  "GKP",
  "DEF",
  "DEF",
  "DEF",
  "DEF",
  "DEF",
  "MID",
  "MID",
  "MID",
  "MID",
  "MID",
  "FWD",
  "FWD",
  "FWD",
] as const;

const PLAYERS: SquadPlayer[] = POSITION_ORDER.map((position, i) => ({
  elementId: 100 + i,
  webName: `Player${100 + i}`,
  position,
  teamFplId: (i % 5) + 1,
  teamName: `Club ${(i % 5) + 1}`,
  teamShortName: `C${(i % 5) + 1}`,
  priceTenths: 45 + i,
}));

const INDEX = buildPlayerIndex(PLAYERS);

function fullSquad(): SquadState {
  return { picks: PLAYERS.map((p) => p.elementId), bankTenths: 7 };
}

/** A response shaped exactly as optimizer/contract.py serialises one. */
function wireResponse(): WireOptimizeResponse {
  return {
    baseline_xp: 61.25,
    plans: [
      {
        label: "Hold",
        transfers_in: [],
        transfers_out: [],
        hit_cost: 0,
        xi: [100, 102, 103, 104, 107, 108, 109, 110, 112, 113, 114],
        bench_order: [101, 105, 106, 111],
        captain: 112,
        vice_captain: 108,
        delta_xp: 0.0,
        bank_after: 7,
        per_gw_breakdown: [
          {
            gw: 6,
            xp: 20.5,
            captain_element_id: 112,
            // dict[int, int] in Python: STRING keys over JSON.
            n_fixtures: { "100": 1, "102": 0, "112": 2 },
          },
          { gw: 7, xp: 20.25, captain_element_id: 112, n_fixtures: { "100": 1 } },
          { gw: 8, xp: 20.5, captain_element_id: 108, n_fixtures: {} },
        ],
        reasoning:
          "Rolling the transfer is worth more than any single move available at this budget.",
      },
      {
        label: "One transfer",
        transfers_in: [{ element_id: 900, web_name: "Newman", price: 78 }],
        transfers_out: [{ element_id: 114, web_name: "Player114", price: 59 }],
        hit_cost: 0,
        xi: [100, 102, 103, 104, 107, 108, 109, 110, 112, 113, 900],
        bench_order: [101, 105, 106, 111],
        captain: 112,
        vice_captain: 900,
        delta_xp: 2.4,
        bank_after: 0,
        per_gw_breakdown: [
          { gw: 6, xp: 21.4, captain_element_id: 112, n_fixtures: { "900": 2 } },
        ],
        reasoning: "Newman has a double in GW6 and the budget covers him exactly.",
      },
      {
        label: "Take a -4",
        transfers_in: [
          { element_id: 900, web_name: "Newman", price: 78 },
          { element_id: 901, web_name: "Otherman", price: 55 },
        ],
        transfers_out: [
          { element_id: 114, web_name: "Player114", price: 59 },
          { element_id: 113, web_name: "Player113", price: 58 },
        ],
        hit_cost: 4,
        xi: [100, 102, 103, 104, 107, 108, 109, 110, 112, 900, 901],
        bench_order: [101, 105, 106, 111],
        captain: 112,
        vice_captain: 900,
        delta_xp: -0.6,
        bank_after: null,
        per_gw_breakdown: [],
        reasoning: "Two moves do not clear the four points they cost.",
      },
    ],
    model_version: "seed-xp-0.1",
    solver_version: "greedy-0.1",
    data_as_of: "2026-09-16T05:04:11Z",
    solve_ms: 42,
    truncated: false,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fetchReturning(response: Response | (() => Promise<Response>)): typeof fetch {
  return (async () =>
    typeof response === "function" ? response() : response) as unknown as typeof fetch;
}

// ---------------------------------------------------------------------------
// Parsing a well-formed response
// ---------------------------------------------------------------------------

describe("parseOptimizeResponse", () => {
  it("reads every field of a well-formed response", () => {
    const parsed = parseOptimizeResponse(wireResponse());

    expect(parsed.baselineXp).toBe(61.25);
    expect(parsed.modelVersion).toBe("seed-xp-0.1");
    expect(parsed.solverVersion).toBe("greedy-0.1");
    expect(parsed.dataAsOf).toBe("2026-09-16T05:04:11Z");
    expect(parsed.solveMs).toBe(42);
    expect(parsed.truncated).toBe(false);
    expect(parsed.plans).toHaveLength(3);

    const [hold, one, hit] = parsed.plans;
    expect(isHoldPlan(hold)).toBe(true);
    expect(hold.bankAfterTenths).toBe(7);

    expect(isHoldPlan(one)).toBe(false);
    expect(one.transfersIn[0]).toEqual({
      elementId: 900,
      webName: "Newman",
      priceTenths: 78,
    });
    expect(one.transfersOut[0].priceTenths).toBe(59);
    expect(one.deltaXp).toBe(2.4);

    expect(hit.hitCost).toBe(4);
    // null must stay null: 0 would be a claim about the bank.
    expect(hit.bankAfterTenths).toBeNull();
  });

  it("keeps a bank_after of zero distinct from an absent one", () => {
    const wire = wireResponse();
    const parsed = parseOptimizeResponse(wire);
    expect(parsed.plans[1].bankAfterTenths).toBe(0);
    expect(parsed.plans[2].bankAfterTenths).toBeNull();
  });

  it("refuses a body that is not a result", () => {
    expect(() => parseOptimizeResponse(null)).toThrow(/not a result/i);
    expect(() => parseOptimizeResponse({ baseline_xp: 1 })).toThrow(/no plans/i);
  });

  it("survives a plan missing optional fields", () => {
    const parsed = parseOptimizeResponse({ plans: [{ label: "Hold" }] });
    const plan = parsed.plans[0];
    expect(plan.xi).toEqual([]);
    expect(plan.perGwBreakdown).toEqual([]);
    expect(plan.reasoning).toBe("");
    expect(plan.bankAfterTenths).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// String-keyed n_fixtures
// ---------------------------------------------------------------------------

describe("n_fixtures", () => {
  it("parses string keys into a map keyed by number", () => {
    const map = parseNFixtures({ "328": 2, "12": 0, "5": 1 });
    expect(map.get(328)).toBe(2);
    expect(map.get(12)).toBe(0);
    expect(map.get(5)).toBe(1);
    // The keys are numbers, not the strings they arrived as.
    expect([...map.keys()].every((k) => typeof k === "number")).toBe(true);
    expect(map.has(328)).toBe(true);
  });

  it("survives a missing or wrongly typed n_fixtures", () => {
    expect(parseNFixtures(undefined).size).toBe(0);
    expect(parseNFixtures([1, 2, 3]).size).toBe(0);
    expect(parseNFixtures("nope").size).toBe(0);
  });

  it("carries blanks and doubles through a full parse", () => {
    const parsed = parseOptimizeResponse(wireResponse());
    const week6 = parsed.plans[0].perGwBreakdown[0];
    expect(week6.gw).toBe(6);
    // 0 is a blank and 2 a double — both survive, and a blank is not confused
    // with a missing entry.
    expect(week6.nFixtures.get(102)).toBe(0);
    expect(week6.nFixtures.get(112)).toBe(2);
    expect(week6.nFixtures.get(999)).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

describe("PlanCard", () => {
  const parsed = parseOptimizeResponse(wireResponse());

  function render(rank: number): string {
    return renderToStaticMarkup(
      createElement(PlanCard, {
        plan: parsed.plans[rank],
        baselineXp: parsed.baselineXp,
        index: INDEX,
        rank,
      }),
    );
  }

  it("shows the reasoning, which is the product more than the verdict is", () => {
    expect(render(1)).toContain(
      "Newman has a double in GW6 and the budget covers him exactly.",
    );
  });

  it("presents a hold as a real option, not a null result", () => {
    const html = render(0);
    expect(html).toContain("Hold");
    expect(html).toContain("No transfer");
    expect(html).toContain("Keep all fifteen");
    expect(html).toContain('data-plan-hold="true"');
  });

  it("names the transfers and prices them from format.ts", () => {
    const html = render(1);
    expect(html).toContain("Newman");
    expect(html).toContain("Player114");
    expect(html).toContain("£7.8m");
    expect(html).toContain("£5.9m");
  });

  it("frames delta_xp as points against holding and shows the hit", () => {
    expect(render(1)).toContain("points vs holding");
    expect(render(1)).toContain("+2.4");
    const hit = render(2);
    expect(hit).toContain("−4 point hit");
    expect(hit).toContain("after the hit");
  });

  it("shows bank_after when present and omits it when null", () => {
    expect(render(0)).toContain("Bank after");
    expect(render(2)).not.toContain("Bank after");
  });

  it("renders a row per gameweek, naming blanks and doubles", () => {
    const html = render(0);
    expect(html.split('data-testid="breakdown-row"').length - 1).toBe(3);
    expect(html).toContain("double:");
    expect(html).toContain("blank:");
  });

  it("resolves element ids to player names via the index", () => {
    const html = render(0);
    expect(html).toContain("Player112");
    expect(html).not.toContain("Player 112"); // the unknown-id fallback
  });
});

// ---------------------------------------------------------------------------
// Failure states
// ---------------------------------------------------------------------------

describe("requestOptimize", () => {
  const request = buildOptimizeRequest(fullSquad(), INDEX, {
    freeTransfers: 1,
    currentGw: 6,
    horizon: 3,
    season: "2026-27",
  });

  it("parses a 200", async () => {
    const result = await requestOptimize(request, {
      baseUrl: "http://optimizer.test",
      fetchImpl: fetchReturning(jsonResponse(wireResponse())),
    });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.response.plans).toHaveLength(3);
      // The untouched body is kept for the log and the proxy.
      expect((result.raw as WireOptimizeResponse).model_version).toBe("seed-xp-0.1");
    }
  });

  it("reads a 422 whose detail is a plain string (the service's own checks)", async () => {
    const result = await requestOptimize(request, {
      fetchImpl: fetchReturning(
        jsonResponse({ detail: "squad has 6 defenders, expected 5" }, 422),
      ),
    });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.kind).toBe("rejected");
      expect(result.status).toBe(422);
      expect(result.message).toBe("squad has 6 defenders, expected 5");
    }
  });

  it("reads a 422 whose detail is a pydantic error list", async () => {
    const result = await requestOptimize(request, {
      fetchImpl: fetchReturning(
        jsonResponse(
          {
            detail: [
              {
                type: "greater_than_equal",
                loc: ["body", "bank"],
                msg: "Input should be greater than or equal to 0",
              },
              {
                type: "int_parsing",
                loc: ["body", "squad", 3, "selling_price"],
                msg: "Input should be a valid integer",
              },
            ],
          },
          422,
        ),
      ),
    });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.kind).toBe("rejected");
      // Readable by a person: the field is named and "body" is not in the way.
      expect(result.message).toContain("bank: Input should be greater than or equal to 0");
      expect(result.message).toContain("squad → player 4 → selling price");
      expect(result.message).not.toContain("body");
      expect(result.message).not.toContain("[object Object]");
    }
  });

  it("always produces a sentence, even from a 422 with an unreadable detail", async () => {
    const result = await requestOptimize(request, {
      fetchImpl: fetchReturning(jsonResponse({ detail: [] }, 422)),
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.message).toMatch(/could not use that squad/i);
  });

  it("degrades gracefully when the service is unreachable", async () => {
    const result = await requestOptimize(request, {
      fetchImpl: (async () => {
        throw new TypeError("fetch failed");
      }) as unknown as typeof fetch,
    });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.kind).toBe("unreachable");
      expect(result.message).toMatch(/not reachable/i);
      // Nothing thrown at the caller — failure is a value.
      expect(result.message.length).toBeGreaterThan(0);
    }
  });

  it("reports a timeout as a timeout, not as a fault", async () => {
    const never: typeof fetch = ((_url: string, init?: RequestInit) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          const err = new Error("aborted");
          err.name = "AbortError";
          reject(err);
        });
      })) as unknown as typeof fetch;

    const result = await requestOptimize(request, { fetchImpl: never, timeoutMs: 5 });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.kind).toBe("timeout");
      expect(result.message).toMatch(/did not answer in time/i);
      // A cold start is a normal condition, and the message says so.
      expect(result.message).toMatch(/sleeps when idle|wake/i);
    }
  });

  it("separates a 5xx from a refusal", async () => {
    const result = await requestOptimize(request, {
      fetchImpl: fetchReturning(jsonResponse({ detail: "solver exploded" }, 500)),
    });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.kind).toBe("server");
      expect(result.status).toBe(500);
      expect(result.message).toContain("solver exploded");
    }
  });

  it("calls a 200 with an unreadable body malformed rather than success", async () => {
    const result = await requestOptimize(request, {
      fetchImpl: fetchReturning(jsonResponse({ nonsense: true })),
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.kind).toBe("malformed");
  });
});

describe("describeDetail", () => {
  it("handles both 422 shapes and gives up honestly on neither", () => {
    expect(describeDetail("plain sentence")).toBe("plain sentence");
    expect(describeDetail([{ loc: ["body", "horizon"], msg: "too large" }])).toBe(
      "horizon: too large",
    );
    expect(describeDetail(undefined)).toBeNull();
    expect(describeDetail("   ")).toBeNull();
  });

  it("truncates a wall of validation errors", () => {
    const many = Array.from({ length: 9 }, (_, i) => ({
      loc: ["body", "squad", i],
      msg: "bad",
    }));
    const message = describeDetail(many) ?? "";
    expect(message).toContain("and 5 more");
  });
});

// ---------------------------------------------------------------------------
// The request
// ---------------------------------------------------------------------------

describe("buildOptimizeRequest", () => {
  it("sends fifteen players with selling prices in integer tenths", () => {
    const request = buildOptimizeRequest(fullSquad(), INDEX, {
      freeTransfers: 1,
      currentGw: 6,
      horizon: 3,
      season: "2026-27",
    });
    expect(request.squad).toHaveLength(SQUAD_SIZE);
    expect(request.bank).toBe(7);
    expect(request.squad[0].selling_price).toBe(45);
    expect(request.squad.every((p) => Number.isInteger(p.selling_price))).toBe(true);
    expect(request.max_ownership).toBeNull();
  });

  it("clamps the horizon and free transfers to the contract's range", () => {
    const request = buildOptimizeRequest(fullSquad(), INDEX, {
      freeTransfers: 99,
      currentGw: 99,
      horizon: 40,
      season: "2026-27",
      maxOwnership: 250,
    });
    expect(request.horizon).toBe(5);
    expect(request.free_transfers).toBe(5);
    expect(request.current_gw).toBe(38);
    expect(request.max_ownership).toBe(100);
  });

  it("rejects a request that is not a squad of fifteen", () => {
    expect(validateOptimizeRequest({})).toMatch(/15 players/);
    expect(
      validateOptimizeRequest(
        buildOptimizeRequest(fullSquad(), INDEX, {
          freeTransfers: 1,
          currentGw: 6,
          horizon: 3,
          season: "2026-27",
        }),
      ),
    ).toBeNull();
  });
});

describe("optimizerBaseUrl", () => {
  it("defaults to local dev and trims a trailing slash", () => {
    expect(optimizerBaseUrl({})).toBe(DEFAULT_OPTIMIZER_URL);
    expect(optimizerBaseUrl({ OPTIMIZER_URL: "https://x.modal.run/" })).toBe(
      "https://x.modal.run",
    );
  });
});

// ---------------------------------------------------------------------------
// The anonymous recommendation log
// ---------------------------------------------------------------------------

describe("buildLogRow", () => {
  const request = buildOptimizeRequest(fullSquad(), INDEX, {
    freeTransfers: 1,
    currentGw: 6,
    horizon: 3,
    season: "2026-27",
  });
  const wire = wireResponse();
  const row = buildLogRow(request, parseOptimizeResponse(wire), wire);

  it("carries the squad hash, and the same one the builder shows", () => {
    expect(row.squad_hash).toBe(squadHash(fullSquad()));
    expect(row.squad_hash).toMatch(/^[0-9a-f]{16}$/);
  });

  it("hashes the same squad the same however the slots were ordered", () => {
    const shuffled: SquadState = {
      picks: [...fullSquad().picks].reverse(),
      bankTenths: 7,
    };
    expect(squadHash(shuffled)).toBe(row.squad_hash);
    expect(squadStateFromRequest(request).picks).toHaveLength(SQUAD_SIZE);
  });

  it("carries what the model is evaluated on", () => {
    expect(row.gw).toBe(6);
    expect(row.horizon).toBe(3);
    expect(row.model_version).toBe("seed-xp-0.1");
    expect(row.solver_version).toBe("greedy-0.1");
    expect(row.baseline_xp).toBe(61.25);
    expect(row.data_as_of).toBe("2026-09-16T05:04:11Z");
    expect(row.solve_ms).toBe(42);
    expect(JSON.stringify(row.payload)).toContain("Newman");
  });

  it("has exactly the declared fields and no others", () => {
    expect(Object.keys(row).sort()).toEqual([...LOG_ROW_FIELDS].sort());
  });

  it("contains no identifier of any kind", () => {
    const serialised = JSON.stringify(row).toLowerCase();
    for (const forbidden of [
      "team_id",
      "teamid",
      "entry",
      "manager",
      "session",
      "cookie",
      "ip_",
      "ipaddress",
      "user_agent",
      "useragent",
      "referrer",
      "email",
      "share",
    ]) {
      expect(serialised).not.toContain(forbidden);
    }
  });
});
