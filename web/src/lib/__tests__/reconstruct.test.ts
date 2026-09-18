/**
 * Tests for the squad reconstruction, ported from ingest/tests/test_money.py and
 * ingest/tests/test_squad_reconstruct.py.
 *
 * They guard the invariant most likely to produce a wrong answer that looks
 * right: an off-by-one selling price, which becomes an off-by-one squad value,
 * which costs someone a transfer.
 *
 * The fixtures are the real ones the Python tests use — team 895045 at GW4 —
 * read off disk rather than imported, so the repo has one copy of that data and
 * the two implementations are tested against the same bytes. If these and the
 * Python tests ever disagree, the Python ones are right.
 *
 * The FPL client (lib/fpl.ts) is exercised here too, against a mocked `fetch`.
 * It has no test file of its own because it is the same slice of behaviour: what
 * the request path does with a 404, a block page, and a squad that is not public
 * yet.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  MAX_FREE_TRANSFERS,
  MissingPriceError,
  buildPriceList,
  freeTransfers,
  holdingSellingPrice,
  reconstructPurchasePrices,
  reconstructSquad,
  sellingPrice,
  squadValueTenths,
  startPrice,
  transfersByGameweek,
  type PicksPayload,
  type PriceList,
  type PriceRow,
  type TransferRow,
} from "../reconstruct";

const FIXTURES = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../../../ingest/tests/fixtures",
);

function fixture<T>(name: string): T {
  return JSON.parse(readFileSync(join(FIXTURES, `${name}.json`), "utf8")) as T;
}

const SEASON = "2026-27";

interface BootstrapElement {
  id: number;
  now_cost: number;
  cost_change_start?: number;
  web_name?: string;
  element_type?: number;
  team?: number;
}

/**
 * The web app takes prices from its own database, not from bootstrap. The
 * fixture is a bootstrap payload, so it is adapted here — the same adaptation
 * the route does from a `players` row.
 */
function pricesFromBootstrap(bootstrap: { elements: BootstrapElement[] }): PriceList {
  return buildPriceList(
    bootstrap.elements.map(
      (element): PriceRow => ({
        elementId: element.id,
        nowCostTenths: element.now_cost,
        costChangeStartTenths: element.cost_change_start ?? 0,
        webName: element.web_name ?? null,
        elementType: element.element_type ?? null,
        teamFplId: element.team ?? null,
      }),
    ),
  );
}

const bootstrap = fixture<{ elements: BootstrapElement[] }>("bootstrap_static");
const transfers = fixture<TransferRow[]>("entry_transfers");
const picks = fixture<PicksPayload>("entry_picks");
const prices = pricesFromBootstrap(bootstrap);

// ---------------------------------------------------------------------------
// Money
// ---------------------------------------------------------------------------

describe("sellingPrice", () => {
  it("returns what it cost when the price has not moved", () => {
    expect(sellingPrice(50, 50)).toBe(50);
  });

  it.each([
    [50, 51, 50], // rose 0.1 — half of one tenth floors to nothing
    [50, 52, 51], // rose 0.2 — keeps 0.1
    [50, 53, 51], // rose 0.3 — keeps 0.1, the floored half
    [50, 54, 52], // rose 0.4 — keeps 0.2
    [50, 55, 52], // rose 0.5 — keeps 0.2
    [130, 137, 133], // rose 0.7 — keeps 0.3
  ])("the seller keeps half a rise, rounded down (%i -> %i)", (purchase, current, expected) => {
    expect(sellingPrice(purchase, current)).toBe(expected);
  });

  it.each([
    [50, 49],
    [50, 45],
    [130, 121],
  ])("passes a fall on in full (%i -> %i)", (purchase, current) => {
    expect(sellingPrice(purchase, current)).toBe(current);
  });

  it("keeps no profit after a rise and a fall back below the purchase price", () => {
    // Bought at 50, peaked at 56, now back to 48.
    expect(sellingPrice(50, 48)).toBe(48);
  });

  it("always returns an integer", () => {
    // The rule floors a halved difference. A float here means a corrupted squad
    // value later, so assert integer-ness, not just the value.
    const result = sellingPrice(55, 58);
    expect(Number.isInteger(result)).toBe(true);
    expect(result).toBe(56);
  });
});

describe("startPrice", () => {
  it("backs out the season change", () => {
    expect(startPrice(55, 5)).toBe(50);
    expect(startPrice(45, -5)).toBe(50);
    expect(startPrice(50, 0)).toBe(50);
  });
});

describe("squadValueTenths", () => {
  it("is sellable value plus bank", () => {
    const holdings = [
      { elementId: 1, purchaseTenths: 50, currentTenths: 53 }, // sells 51
      { elementId: 2, purchaseTenths: 100, currentTenths: 98 }, // sells 98
    ];
    expect(squadValueTenths(holdings, 7)).toBe(51 + 98 + 7);
  });

  it("applies the selling rule to a holding", () => {
    expect(
      holdingSellingPrice({ elementId: 1, purchaseTenths: 47, currentTenths: 49 }),
    ).toBe(48);
  });
});

// ---------------------------------------------------------------------------
// Purchase prices
// ---------------------------------------------------------------------------

function priceList(rows: Record<number, [now: number, change: number]>): PriceList {
  return buildPriceList(
    Object.entries(rows).map(([id, [now, change]]) => ({
      elementId: Number(id),
      nowCostTenths: now,
      costChangeStartTenths: change,
    })),
  );
}

describe("reconstructPurchasePrices", () => {
  it("falls back to day-one prices for an untouched squad", () => {
    const result = reconstructPurchasePrices([1, 2], [], priceList({ 1: [55, 5], 2: [100, -2] }));
    expect(Object.fromEntries(result)).toEqual({ 1: 50, 2: 102 });
  });

  it("uses the price actually paid for a transferred-in player", () => {
    const log: TransferRow[] = [
      {
        element_in: 9,
        element_in_cost: 76,
        element_out: 1,
        element_out_cost: 95,
        event: 4,
        time: "2026-09-12T12:17:51Z",
      },
    ];
    const result = reconstructPurchasePrices([9], log, priceList({ 9: [80, 4] }));
    expect(result.get(9)).toBe(76);
  });

  it("resolves a buy-back to the most recent purchase", () => {
    // Bought in GW2 at 70, sold in GW5, bought back in GW9 at 82. Rows are
    // deliberately out of order, as the API returns them newest-first.
    const log: TransferRow[] = [
      { element_in: 7, element_in_cost: 82, element_out: 3, element_out_cost: 50, event: 9, time: "2026-10-25T10:00:00Z" },
      { element_in: 3, element_in_cost: 50, element_out: 7, element_out_cost: 74, event: 5, time: "2026-09-20T10:00:00Z" },
      { element_in: 7, element_in_cost: 70, element_out: 2, element_out_cost: 45, event: 2, time: "2026-08-28T10:00:00Z" },
    ];
    expect(reconstructPurchasePrices([7], log, priceList({ 7: [85, 15] })).get(7)).toBe(82);
  });

  it("resolves two buys inside one gameweek by time, not by event", () => {
    const log: TransferRow[] = [
      { element_in: 5, element_in_cost: 60, element_out: 4, element_out_cost: 55, event: 7, time: "2026-10-10T09:00:00Z" },
      { element_in: 5, element_in_cost: 58, element_out: 6, element_out_cost: 50, event: 7, time: "2026-10-09T09:00:00Z" },
    ];
    expect(reconstructPurchasePrices([5], log, priceList({ 5: [62, 2] })).get(5)).toBe(60);
  });

  it("gets the same answer whatever order the log arrives in", () => {
    // Sorting by event alone leaves same-gameweek rows to chance, and the input
    // order is whatever the API felt like. Reversing it must change nothing.
    const log: TransferRow[] = [
      { element_in: 5, element_in_cost: 60, element_out: 4, element_out_cost: 55, event: 7, time: "2026-10-10T09:00:00Z" },
      { element_in: 5, element_in_cost: 58, element_out: 6, element_out_cost: 50, event: 7, time: "2026-10-09T09:00:00Z" },
    ];
    const forward = reconstructPurchasePrices([5], log, priceList({ 5: [62, 2] }));
    const backward = reconstructPurchasePrices([5], log.slice().reverse(), priceList({ 5: [62, 2] }));
    expect(backward.get(5)).toBe(forward.get(5));
  });

  it("does not reorder the caller's array", () => {
    const log: TransferRow[] = [
      { element_in: 5, element_in_cost: 60, element_out: 4, element_out_cost: 55, event: 7, time: "2026-10-10T09:00:00Z" },
      { element_in: 5, element_in_cost: 58, element_out: 6, element_out_cost: 50, event: 7, time: "2026-10-09T09:00:00Z" },
    ];
    const before = log.map((row) => row.time);
    reconstructPurchasePrices([5], log, priceList({ 5: [62, 2] }));
    expect(log.map((row) => row.time)).toEqual(before);
  });

  it("throws on an unknown element rather than pricing it at zero", () => {
    // Usually stale reference data or an element id from another season, which
    // is exactly the corruption the (season, element_id) key exists to prevent.
    expect(() => reconstructPurchasePrices([999], [], priceList({ 1: [50, 0] }))).toThrow(
      MissingPriceError,
    );
  });
});

// ---------------------------------------------------------------------------
// The real squad
// ---------------------------------------------------------------------------

describe("reconstructSquad, against real GW4 data for team 895045", () => {
  const squad = reconstructSquad({ prices, transfers, picks, entry: null, season: SEASON });

  it("has fifteen players", () => {
    expect(squad.picksAvailable).toBe(true);
    expect(squad.players).toHaveLength(15);
  });

  it("keeps the picks in order", () => {
    expect(squad.players.map((p) => p.elementId)).toEqual(
      (picks.picks ?? []).map((p) => p.element),
    );
    expect(squad.players.map((p) => p.squadPosition)).toEqual(
      Array.from({ length: 15 }, (_, i) => i + 1),
    );
  });

  it("starts eleven and benches four", () => {
    expect(squad.players.filter((p) => p.onBench)).toHaveLength(4);
  });

  it("prices every player as a plausible integer tenths", () => {
    for (const player of squad.players) {
      for (const value of [
        player.purchasePriceTenths,
        player.currentPriceTenths,
        player.sellingPriceTenths,
      ]) {
        expect(Number.isInteger(value)).toBe(true);
        // 3.8 is the cheapest a player has ever been, 16.0 the dearest.
        expect(value).toBeGreaterThanOrEqual(35);
        expect(value).toBeLessThanOrEqual(160);
      }
    }
  });

  it("follows the selling rule for every player", () => {
    for (const player of squad.players) {
      expect(player.sellingPriceTenths).toBe(
        sellingPrice(player.purchasePriceTenths, player.currentPriceTenths),
      );
      // The manager keeps at most half a rise, so this is an invariant of the
      // rule, not a coincidence of this squad.
      expect(player.sellingPriceTenths).toBeLessThanOrEqual(player.currentPriceTenths);
      expect(player.sellingPriceTenths).toBeGreaterThanOrEqual(
        Math.min(player.purchasePriceTenths, player.currentPriceTenths),
      );
    }
  });

  it("values the squad as the selling total plus the bank", () => {
    expect(squad.squadValueTenths).toBe(
      (squad.sellingTotalTenths ?? 0) + (squad.bankTenths ?? 0),
    );
  });

  it("lands near what FPL reported at that deadline", () => {
    // FPL said 1008 at the GW4 deadline; this values the same squad at today's
    // prices, days later. Assert the neighbourhood, never the exact figure — the
    // gap is price movement and it grows every day the fixture ages.
    const reported = picks.entry_history?.value ?? 0;
    expect(Math.abs((squad.squadValueTenths ?? 0) - reported)).toBeLessThanOrEqual(20);
  });

  it("takes the bank and the gameweek from the picks payload", () => {
    expect(squad.bankTenths).toBe(picks.entry_history?.bank);
    expect(squad.gw).toBe(picks.entry_history?.event);
    expect(squad.activeChip).toBe(picks.active_chip);
  });

  it("flags one captain and one vice-captain", () => {
    expect(squad.players.filter((p) => p.isCaptain)).toHaveLength(1);
    expect(squad.players.filter((p) => p.isViceCaptain)).toHaveLength(1);
  });

  it("holds every transferred-in player at a price that is actually in the log", () => {
    const paid = new Map<number, Set<number>>();
    for (const row of transfers) {
      const set = paid.get(row.element_in) ?? new Set<number>();
      set.add(row.element_in_cost);
      paid.set(row.element_in, set);
    }
    for (const player of squad.players) {
      const known = paid.get(player.elementId);
      if (known) expect(known.has(player.purchasePriceTenths)).toBe(true);
    }
  });

  it("holds a player bought back later in the same gameweek at the later price", () => {
    // Element 40 was bought in GW4 at 75, sold, then bought back later the same
    // gameweek at 76. Only the times distinguish them. Resolving by event alone
    // would silently pick 75.
    const held = squad.players.find((p) => p.elementId === 40);
    expect(held?.purchasePriceTenths).toBe(76);
  });

  it("reports rather than invents a price it does not have", () => {
    // The bootstrap fixture is trimmed to keep the repo small, so some picks are
    // missing from it. That is survivable only because each was bought in the
    // log, which carries the price paid.
    const bought = new Set(transfers.map((row) => row.element_in));
    for (const elementId of squad.unpricedElements) {
      expect(bought.has(elementId)).toBe(true);
    }
    for (const player of squad.players) {
      expect(prices.has(player.elementId) || bought.has(player.elementId)).toBe(true);
    }
  });

  it("carries no team id, because that identifies the user", () => {
    // CLAUDE.md invariant 4. The id is used for a request and forgotten; it must
    // not ride along in the object that gets logged, cached or serialised.
    expect(JSON.stringify(squad)).not.toContain("895045");
  });

  it("estimates a sane number of free transfers", () => {
    expect(squad.freeTransfers).toBeGreaterThanOrEqual(0);
    expect(squad.freeTransfers).toBeLessThanOrEqual(MAX_FREE_TRANSFERS);
    expect(squad.freeTransfersIsEstimate).toBe(true);
  });
});

describe("reconstructSquad with no picks yet", () => {
  it("is not a crash", () => {
    const result = reconstructSquad({
      prices,
      transfers,
      picks: null,
      season: SEASON,
    });
    expect(result.picksAvailable).toBe(false);
    expect(result.players).toEqual([]);
    expect(result.sellingTotalTenths).toBeNull();
  });

  it("still fills in what the entry summary knows", () => {
    const result = reconstructSquad({
      prices,
      transfers,
      picks: null,
      entry: {
        name: "Zack's XI",
        player_first_name: "Zack",
        player_last_name: "K",
        current_event: 5,
        last_deadline_bank: 3,
        last_deadline_value: 1010,
      },
      season: SEASON,
    });
    expect(result.teamName).toBe("Zack's XI");
    expect(result.managerName).toBe("Zack K");
    expect(result.gw).toBe(5);
    expect(result.bankTenths).toBe(3);
    expect(result.squadValueTenths).toBe(1010);
  });
});

describe("a squad member missing from the price list", () => {
  const bought: TransferRow[] = [
    {
      element_in: 99,
      element_in_cost: 70,
      element_out: 1,
      element_out_cost: 50,
      event: 3,
      time: "2026-09-01T10:00:00Z",
    },
  ];
  const onePick: PicksPayload = {
    entry_history: { event: 3, bank: 5 },
    active_chip: null,
    picks: [{ element: 99, position: 1, multiplier: 1 }],
  };

  it("is reported, not thrown, when the log knows what was paid", () => {
    // Stale reference data. Only today's price is missing, and denying the user
    // the other fourteen players over it would be the wrong trade.
    const result = reconstructSquad({
      prices: priceList({ 1: [50, 0] }),
      transfers: bought,
      picks: onePick,
      season: SEASON,
    });
    expect(result.unpricedElements).toEqual([99]);
    expect(result.players[0].purchasePriceTenths).toBe(70);
    expect(result.players[0].sellingPriceTenths).toBe(70);
    expect(result.players[0].priced).toBe(false);
  });

  it("is thrown when nothing at all is known about the player", () => {
    // No price and no purchase is not a stale row, it is a guess, and a guessed
    // squad value is worse than an error.
    expect(() =>
      reconstructSquad({
        prices: priceList({ 1: [50, 0] }),
        transfers: [],
        picks: onePick,
        season: SEASON,
      }),
    ).toThrow(MissingPriceError);
  });
});

// ---------------------------------------------------------------------------
// Free transfers
// ---------------------------------------------------------------------------

function log(events: number[]): TransferRow[] {
  return events.map((event, i) => ({
    element_in: i + 1,
    element_in_cost: 50,
    event,
    time: `2026-08-0${(i % 9) + 1}T10:00:00Z`,
  }));
}

describe("freeTransfers", () => {
  it("banks to the cap for a manager who has never transferred", () => {
    expect(freeTransfers([], 20)).toBe(MAX_FREE_TRANSFERS);
  });

  it("banks nothing new at one transfer a week", () => {
    expect(freeTransfers(log([2, 3, 4, 5]), 5)).toBe(2);
  });

  it("cannot be pushed negative by a hit", () => {
    expect(freeTransfers(log([2, 2, 2, 2, 2, 2]), 2)).toBe(1);
  });

  it("accumulates saving, but only to the cap", () => {
    expect(freeTransfers([], 3)).toBe(4);
    expect(freeTransfers([], 30)).toBe(MAX_FREE_TRANSFERS);
  });

  it("charges nothing for transfers made under a wildcard", () => {
    const wildcarded = log([4, 4, 4, 4, 4, 4, 4, 4]);
    const without = freeTransfers(wildcarded, 4);
    const withChip = freeTransfers(wildcarded, 4, new Map([[4, "wildcard"]]));
    expect(without).toBeLessThan(withChip);
    expect(withChip).toBe(5);
  });

  it("counts transfers per gameweek", () => {
    const counts = transfersByGameweek(transfers);
    expect([...counts.values()].reduce((a, b) => a + b, 0)).toBe(transfers.length);
  });
});

// ---------------------------------------------------------------------------
// The FPL client (lib/fpl.ts) — everything network-facing is mocked.
// ---------------------------------------------------------------------------

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("lib/fpl", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    const { clearFplCache } = await import("../fpl");
    clearFplCache();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends the configured User-Agent and never touches bootstrap-static", async () => {
    const { getEntry } = await import("../fpl");
    process.env.FPL_USER_AGENT = "fpl-test/1.0 (+mailto:someone@example.com)";
    fetchMock.mockResolvedValue(jsonResponse({ current_event: 4 }));

    await getEntry(895045);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://fantasy.premierleague.com/api/entry/895045/");
    expect((init.headers as Record<string, string>)["User-Agent"]).toContain("fpl-test");
    for (const call of fetchMock.mock.calls) {
      expect(String(call[0])).not.toContain("bootstrap-static");
    }
  });

  it("caches a response instead of asking again", async () => {
    const { getEntryTransfers } = await import("../fpl");
    fetchMock.mockResolvedValue(jsonResponse([]));

    await getEntryTransfers(895045);
    await getEntryTransfers(895045);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("asks for at least an hour of platform caching", async () => {
    const { CACHE_SECONDS, getEntry } = await import("../fpl");
    fetchMock.mockResolvedValue(jsonResponse({}));

    await getEntry(1);

    expect(CACHE_SECONDS).toBeGreaterThanOrEqual(3600);
    expect(fetchMock.mock.calls[0][1].next.revalidate).toBe(CACHE_SECONDS);
  });

  it("turns a 404 team id into a not-found error", async () => {
    const { FplError, getEntry } = await import("../fpl");
    fetchMock.mockResolvedValue(new Response("Not found", { status: 404 }));

    await expect(getEntry(999_999_999)).rejects.toMatchObject({ kind: "not-found" });
    await expect(getEntry(999_999_999)).rejects.toBeInstanceOf(FplError);
    // Negatively cached: a typo is not asked for twice.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reads picks that are not public yet as null, not as a failure", async () => {
    const { getEntryPicks } = await import("../fpl");
    fetchMock.mockResolvedValue(new Response("Not found", { status: 404 }));

    await expect(getEntryPicks(895045, 9)).resolves.toBeNull();
  });

  it("never carries an upstream body into the error", async () => {
    const { getEntry } = await import("../fpl");
    fetchMock.mockResolvedValue(
      new Response("<html>Attention Required! | Cloudflare</html>", { status: 403 }),
    );

    await expect(getEntry(1)).rejects.toMatchObject({ kind: "unavailable", status: 403 });
    await getEntry(1).catch((err: Error) => {
      expect(err.message).not.toContain("Cloudflare");
    });
  });

  it("reports a timeout as a timeout", async () => {
    const { getEntry } = await import("../fpl");
    const aborted = new Error("timed out");
    aborted.name = "TimeoutError";
    fetchMock.mockRejectedValue(aborted);

    await expect(getEntry(1)).rejects.toMatchObject({ kind: "timeout" });
  });

  it("fetches entry, transfers and picks for the current gameweek", async () => {
    const { fetchManagerPayloads } = await import("../fpl");
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith("/transfers/")) return Promise.resolve(jsonResponse(transfers));
      if (url.includes("/event/")) return Promise.resolve(jsonResponse(picks));
      return Promise.resolve(jsonResponse({ current_event: 4, name: "Zack's XI" }));
    });

    const payloads = await fetchManagerPayloads(895045);

    expect(payloads.gw).toBe(4);
    expect(payloads.picks?.picks).toHaveLength(15);
    expect(payloads.transfers.length).toBe(transfers.length);
    expect(fetchMock.mock.calls.map((c) => String(c[0]))).toContain(
      "https://fantasy.premierleague.com/api/entry/895045/event/4/picks/",
    );
  });

  it("returns no picks before the season has a current gameweek", async () => {
    const { fetchManagerPayloads } = await import("../fpl");
    fetchMock.mockImplementation((url: string) =>
      Promise.resolve(jsonResponse(url.endsWith("/transfers/") ? [] : { current_event: null })),
    );

    const payloads = await fetchManagerPayloads(895045);
    expect(payloads.gw).toBeNull();
    expect(payloads.picks).toBeNull();
  });

  it("end to end: mocked payloads through to selling prices", async () => {
    const { fetchManagerPayloads } = await import("../fpl");
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith("/transfers/")) return Promise.resolve(jsonResponse(transfers));
      if (url.includes("/event/")) return Promise.resolve(jsonResponse(picks));
      return Promise.resolve(jsonResponse({ current_event: 4 }));
    });

    const payloads = await fetchManagerPayloads(895045);
    const squad = reconstructSquad({
      prices,
      transfers: payloads.transfers,
      picks: payloads.picks,
      entry: payloads.entry,
      season: SEASON,
    });

    expect(squad.players).toHaveLength(15);
    expect(squad.sellingTotalTenths).toBeGreaterThan(900);
  });
});
