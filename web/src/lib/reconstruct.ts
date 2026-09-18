/**
 * Rebuild a public manager's squad, with purchase and selling prices.
 *
 * A direct TypeScript port of `ingest/money.py` and `ingest/squad_reconstruct.py`.
 * The Python versions stay the reference implementation; if the two ever disagree,
 * the Python one is right and this file has a bug.
 *
 * Given nothing but a team id, the public API gives you the squad
 * (`entry/{id}/event/{gw}/picks/`) and every price ever paid
 * (`entry/{id}/transfers/`). Today's prices come from our own database, not from
 * `bootstrap-static/` — the web app never calls that endpoint from a request path
 * (CLAUDE.md invariant 3). Walk the transfer log forward from the original squad
 * and you know the purchase price of every player held; apply FPL's selling rule
 * and you have the selling price — the number that actually constrains a
 * transfer — without a screenshot and without a logged-in session.
 *
 * Pure: no network, no database, no React. It takes payloads someone else
 * fetched, which is what makes it testable offline against a recorded fixture.
 *
 * Money is integer tenths throughout (CLAUDE.md invariant 1). There is not one
 * float and not one division in this file: the selling rule floors a halved
 * difference, and float arithmetic turns that into an off-by-one squad value
 * that looks almost right and costs the user a transfer. Tenths become a display
 * string in exactly one place, lib/format.ts.
 *
 * Two caveats worth knowing before trusting a number:
 *
 *  - Picks are private until that gameweek's deadline passes. A manager planning
 *    transfers on a Friday has a squad this cannot see; `picks: null` is an
 *    ordinary outcome here, not an error.
 *  - Selling prices are computed against *today's* prices. Reconstructing a
 *    squad days after the deadline it was set at will not match the squad value
 *    FPL showed at that deadline, because prices moved in between. That gap is
 *    price movement, not a bug.
 *
 * Deliberately imports nothing: like lib/squad.ts, the rules here do not depend
 * on the database row shape, so a caller adapts its rows to `PriceRow` rather
 * than this file learning the schema.
 */

// ---------------------------------------------------------------------------
// Money (ingest/money.py)
// ---------------------------------------------------------------------------

/**
 * What the manager gets back for a player they paid `purchaseTenths` for.
 *
 * The one rule everything else derives from: a player's selling price is the
 * purchase price plus half of any rise, rounded down to the nearest £0.1m. A
 * fall is passed on in full.
 *
 *   sellingPrice(50, 53) === 51   rose 0.3, keeps half rounded down
 *   sellingPrice(50, 54) === 52   rose 0.4, keeps exactly half
 *   sellingPrice(50, 47) === 47   falls are passed on in full
 */
export function sellingPrice(purchaseTenths: number, currentTenths: number): number {
  if (currentTenths <= purchaseTenths) return currentTenths;
  const rise = currentTenths - purchaseTenths;
  // Integer halving, floored. `>> 1` rather than `/ 2` so no float ever exists:
  // rise is always a small positive integer here, well inside 32 bits.
  return purchaseTenths + (rise >> 1);
}

/**
 * A player's price on day one of the season.
 *
 * This is the purchase price for anyone still holding a player from their
 * original squad, which is most of the squad for most managers.
 */
export function startPrice(nowCostTenths: number, costChangeStartTenths: number): number {
  return nowCostTenths - costChangeStartTenths;
}

/** A player currently in a squad, with what they cost and what they fetch. */
export interface Holding {
  elementId: number;
  purchaseTenths: number;
  currentTenths: number;
}

export function holdingSellingPrice(holding: Holding): number {
  return sellingPrice(holding.purchaseTenths, holding.currentTenths);
}

/** Total sellable value: what the squad fetches plus what is in the bank. */
export function squadValueTenths(holdings: readonly Holding[], bankTenths: number): number {
  let total = bankTenths;
  for (const holding of holdings) total += holdingSellingPrice(holding);
  return total;
}

// ---------------------------------------------------------------------------
// Payload shapes
//
// Only the fields this file reads are declared. The FPL payloads carry a great
// deal more and none of it is our business.
// ---------------------------------------------------------------------------

/** One row of `entry/{id}/transfers/`. Costs are in tenths. */
export interface TransferRow {
  element_in: number;
  element_in_cost: number;
  element_out?: number;
  element_out_cost?: number;
  event: number;
  /** ISO-8601 UTC. The tiebreaker within a gameweek — see the sort below. */
  time: string;
}

export interface PickRow {
  element: number;
  /** 1-15; 12-15 are the bench, in order. */
  position?: number;
  multiplier?: number;
  is_captain?: boolean;
  is_vice_captain?: boolean;
  element_type?: number;
}

export interface PicksPayload {
  active_chip?: string | null;
  entry_history?: {
    event?: number | null;
    bank?: number | null;
    value?: number | null;
  } | null;
  picks?: PickRow[];
}

export interface EntryPayload {
  name?: string | null;
  player_first_name?: string | null;
  player_last_name?: string | null;
  current_event?: number | null;
  last_deadline_bank?: number | null;
  last_deadline_value?: number | null;
}

/**
 * One row of the price list — today's price and the season's change so far.
 *
 * Sourced from our `players` table (or the checked-in seed) via lib/db.ts, never
 * from a live `bootstrap-static/` call.
 */
export interface PriceRow {
  elementId: number;
  nowCostTenths: number;
  costChangeStartTenths: number;
  webName?: string | null;
  elementType?: number | null;
  teamFplId?: number | null;
  teamName?: string | null;
  teamShortName?: string | null;
}

export type PriceList = ReadonlyMap<number, PriceRow>;

export function buildPriceList(rows: Iterable<PriceRow>): PriceList {
  const index = new Map<number, PriceRow>();
  for (const row of rows) index.set(row.elementId, row);
  return index;
}

/**
 * A squad member we can neither price nor find a purchase for.
 *
 * Usually stale reference data or an element id from another season, which is
 * exactly the corruption the (season, element_id) key exists to prevent. Pricing
 * them at zero would produce a squad value that is wrong and looks fine, so this
 * fails loudly instead.
 */
export class MissingPriceError extends Error {
  readonly elementId: number;

  constructor(elementId: number) {
    super(
      `element ${elementId} is in the squad but not in the price list — ` +
        "stale reference data, or an element id from another season",
    );
    this.name = "MissingPriceError";
    this.elementId = elementId;
  }
}

/**
 * Work out what a manager paid for each player they currently hold.
 *
 * `transfers` is the raw `entry/{id}/transfers/` payload — newest first, as the
 * API returns it.
 *
 * A player bought more than once — sold in GW4, bought back in GW9 — must
 * resolve to the *most recent* purchase, which is why the log is walked
 * oldest-first and later buys overwrite earlier ones. The sort key is
 * `(event, time)` and not `event` alone: two buys of the same player inside one
 * gameweek happen, and sorting by event alone leaves the order to chance and
 * silently picks the wrong price.
 *
 * Anyone in the squad with no purchase in the log came from the original squad,
 * so their purchase price is their day-one price.
 */
export function reconstructPurchasePrices(
  currentSquad: readonly number[],
  transfers: readonly TransferRow[],
  prices: PriceList,
): Map<number, number> {
  const paid = new Map<number, number>();

  const ordered = transfers.slice().sort((a, b) => {
    if (a.event !== b.event) return a.event - b.event;
    const at = a.time ?? "";
    const bt = b.time ?? "";
    // ISO-8601 UTC strings compare correctly as strings, and staying with
    // strings avoids inventing a timezone for a malformed one.
    return at < bt ? -1 : at > bt ? 1 : 0;
  });

  for (const row of ordered) paid.set(row.element_in, row.element_in_cost);

  const purchasePrices = new Map<number, number>();

  for (const elementId of currentSquad) {
    const fromLog = paid.get(elementId);
    if (fromLog !== undefined) {
      purchasePrices.set(elementId, fromLog);
      continue;
    }

    const row = prices.get(elementId);
    if (!row) throw new MissingPriceError(elementId);

    purchasePrices.set(
      elementId,
      startPrice(row.nowCostTenths, row.costChangeStartTenths ?? 0),
    );
  }

  return purchasePrices;
}

// ---------------------------------------------------------------------------
// Free transfers (ingest/squad_reconstruct.py)
// ---------------------------------------------------------------------------

/** Free transfers accumulate to a cap. A rule FPL has changed before. */
export const MAX_FREE_TRANSFERS = 5;

/** Chips that make a gameweek's transfers cost nothing. */
export const UNLIMITED_TRANSFER_CHIPS: ReadonlySet<string> = new Set([
  "wildcard",
  "freehit",
]);

/** How many transfers were made in each gameweek. */
export function transfersByGameweek(
  transfers: readonly TransferRow[],
): Map<number, number> {
  const counts = new Map<number, number>();
  for (const row of transfers) {
    const event = Math.trunc(row.event);
    counts.set(event, (counts.get(event) ?? 0) + 1);
  }
  return counts;
}

/**
 * Free transfers available going into the gameweek after `upToGw`.
 *
 * An estimate, and labelled as one in the output. The transfer log is public but
 * the chip log is not — a caller holding one gameweek's picks knows about one
 * chip. Pass `chipGws` when you have more; transfers made under a wildcard or
 * free hit cost nothing.
 *
 * The rule modelled: one free transfer per gameweek, unused ones carry over to a
 * cap of five, and you can never bank fewer than none.
 */
export function freeTransfers(
  transfers: readonly TransferRow[],
  upToGw: number,
  chipGws: ReadonlyMap<number, string | null> = new Map(),
): number {
  const used = transfersByGameweek(transfers);

  let available = 1;
  for (let gw = 1; gw <= upToGw; gw += 1) {
    if (gw > 1) available = Math.min(MAX_FREE_TRANSFERS, available + 1);
    const chip = chipGws.get(gw);
    if (chip && UNLIMITED_TRANSFER_CHIPS.has(chip)) continue;
    available = Math.max(0, available - (used.get(gw) ?? 0));
  }

  return Math.min(MAX_FREE_TRANSFERS, available + 1);
}

// ---------------------------------------------------------------------------
// The squad
// ---------------------------------------------------------------------------

export interface ReconstructedPlayer {
  elementId: number;
  webName: string | null;
  elementType: number | null;
  teamFplId: number | null;
  teamName: string | null;
  teamShortName: string | null;
  /** 1-15 as FPL orders them: 1-11 start, 12-15 are the bench in order. */
  squadPosition: number | null;
  onBench: boolean;
  isCaptain: boolean;
  isViceCaptain: boolean;
  multiplier: number | null;
  purchasePriceTenths: number;
  currentPriceTenths: number;
  sellingPriceTenths: number;
  /** False when today's price was missing and the purchase price stood in. */
  priced: boolean;
}

export interface ReconstructedSquad {
  season: string;
  /** False pre-deadline: the picks endpoint 404s until the deadline passes. */
  picksAvailable: boolean;
  managerName: string | null;
  teamName: string | null;
  gw: number | null;
  activeChip: string | null;
  bankTenths: number | null;
  players: ReconstructedPlayer[];
  sellingTotalTenths: number | null;
  squadValueTenths: number | null;
  freeTransfers: number | null;
  freeTransfersIsEstimate: boolean;
  /** Squad members with no current price — stale reference data, reported. */
  unpricedElements: number[];
}

export interface ReconstructInput {
  prices: PriceList;
  transfers: readonly TransferRow[];
  /** Null when the gameweek's deadline has not passed yet. */
  picks: PicksPayload | null;
  entry?: EntryPayload | null;
  season: string;
}

function managerName(entry: EntryPayload): string | null {
  const parts = [entry.player_first_name, entry.player_last_name].filter(
    (part): part is string => typeof part === "string" && part.trim() !== "",
  );
  return parts.length > 0 ? parts.join(" ") : null;
}

/**
 * Build a squad with purchase and selling prices from already-fetched payloads.
 *
 * Note what is *not* in the result: the team id. It is the user's, it identifies
 * them, and CLAUDE.md invariant 4 says nothing about an identifiable user is
 * stored or logged. It is used for the duration of a request and then forgotten,
 * so it never enters this object and never reaches a log line or the
 * recommendation log. (The Python reference does carry it — that one runs on a
 * CLI, not a request path.)
 */
export function reconstructSquad({
  prices,
  transfers,
  picks,
  entry,
  season,
}: ReconstructInput): ReconstructedSquad {
  const entryPayload: EntryPayload = entry ?? {};

  const base: ReconstructedSquad = {
    season,
    picksAvailable: picks !== null,
    managerName: managerName(entryPayload),
    teamName: entryPayload.name ?? null,
    gw: null,
    activeChip: null,
    bankTenths: null,
    players: [],
    sellingTotalTenths: null,
    squadValueTenths: null,
    freeTransfers: null,
    freeTransfersIsEstimate: true,
    unpricedElements: [],
  };

  if (picks === null) {
    // Pre-deadline. The entry summary still has last deadline's bank and value,
    // which is better than nothing for a caller that wants to show something.
    return {
      ...base,
      gw: entryPayload.current_event ?? null,
      bankTenths: entryPayload.last_deadline_bank ?? null,
      squadValueTenths: entryPayload.last_deadline_value ?? null,
    };
  }

  const history = picks.entry_history ?? {};
  const gw = Math.trunc(history.event ?? entryPayload.current_event ?? 0);
  const activeChip = picks.active_chip ?? null;
  const pickRows = picks.picks ?? [];

  const squad = pickRows.map((pick) => Math.trunc(pick.element));
  const purchasePrices = reconstructPurchasePrices(squad, transfers, prices);

  // A squad member absent from the price list but present in the transfer log
  // means stale reference data, not a missing player. Reported rather than
  // thrown so one bad id does not deny the user the other fourteen; their
  // selling price falls back to what they cost, the only defensible guess.
  const unpricedElements = squad.filter((elementId) => !prices.has(elementId));

  const holdings: Holding[] = squad.map((elementId) => {
    const purchaseTenths = purchasePrices.get(elementId) as number;
    return {
      elementId,
      purchaseTenths,
      currentTenths: prices.get(elementId)?.nowCostTenths ?? purchaseTenths,
    };
  });
  const byId = new Map(holdings.map((holding) => [holding.elementId, holding]));

  const players: ReconstructedPlayer[] = pickRows.map((pick) => {
    const elementId = Math.trunc(pick.element);
    const holding = byId.get(elementId) as Holding;
    const meta = prices.get(elementId);
    const squadPosition = pick.position ?? null;
    return {
      elementId,
      webName: meta?.webName ?? null,
      elementType: meta?.elementType ?? pick.element_type ?? null,
      teamFplId: meta?.teamFplId ?? null,
      teamName: meta?.teamName ?? null,
      teamShortName: meta?.teamShortName ?? null,
      squadPosition,
      onBench: (squadPosition ?? 0) > 11,
      isCaptain: Boolean(pick.is_captain),
      isViceCaptain: Boolean(pick.is_vice_captain),
      multiplier: pick.multiplier ?? null,
      purchasePriceTenths: holding.purchaseTenths,
      currentPriceTenths: holding.currentTenths,
      sellingPriceTenths: holdingSellingPrice(holding),
      priced: meta !== undefined,
    };
  });

  const bankRaw = history.bank ?? entryPayload.last_deadline_bank ?? 0;
  const bankTenths = Math.trunc(bankRaw);

  const chipGws = new Map<number, string | null>();
  if (activeChip) chipGws.set(gw, activeChip);

  let sellingTotalTenths = 0;
  for (const holding of holdings) sellingTotalTenths += holdingSellingPrice(holding);

  return {
    ...base,
    gw,
    activeChip,
    bankTenths,
    players,
    sellingTotalTenths,
    squadValueTenths: squadValueTenths(holdings, bankTenths),
    freeTransfers: freeTransfers(transfers, gw, chipGws),
    freeTransfersIsEstimate: true,
    unpricedElements,
  };
}
