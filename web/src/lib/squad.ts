/**
 * The squad model and the FPL rules that govern it.
 *
 * Pure functions only — no React, no browser APIs, no I/O. The hook in
 * lib/useSquad.ts owns state; this file owns truth about what a legal squad is,
 * so the same rules can be reused by a future screenshot-confirmation screen or
 * by anything that validates a decoded share link.
 *
 * Money is integer tenths everywhere (CLAUDE.md invariant 1). 1000 is £100.0m.
 * There is not a single division by ten in this file: formatting is lib/format.ts's
 * job and doing it twice is how squad values end up a tenth out.
 */

/** 1 GKP, 2 DEF, 3 MID, 4 FWD — matches `players.element_type`. */
export type ElementType = 1 | 2 | 3 | 4;

export type Position = "GKP" | "DEF" | "MID" | "FWD";

export const POSITIONS: readonly Position[] = ["GKP", "DEF", "MID", "FWD"];

export const POSITION_BY_ELEMENT_TYPE: Record<ElementType, Position> = {
  1: "GKP",
  2: "DEF",
  3: "MID",
  4: "FWD",
};

export const POSITION_LABEL: Record<Position, string> = {
  GKP: "Goalkeeper",
  DEF: "Defender",
  MID: "Midfielder",
  FWD: "Forward",
};

/** Squad composition: exactly this many of each, 15 in total. */
export const SQUAD_QUOTA: Record<Position, number> = {
  GKP: 2,
  DEF: 5,
  MID: 5,
  FWD: 3,
};

export const SQUAD_SIZE = 15;
export const XI_SIZE = 11;
export const MAX_PER_CLUB = 3;
/** £100.0m, in tenths. */
export const BUDGET_TENTHS = 1000;

/** Legal starting elevens: 1 GKP, 3-5 DEF, 2-5 MID, 1-3 FWD, 11 players. */
export const XI_MIN: Record<Position, number> = { GKP: 1, DEF: 3, MID: 2, FWD: 1 };
export const XI_MAX: Record<Position, number> = { GKP: 1, DEF: 5, MID: 5, FWD: 3 };

/**
 * The 15 slots, in a fixed order, each bound to a position.
 *
 * Fixing the order is what makes the URL encoding positional (no need to store a
 * position per pick) and what lets each combobox filter to one position.
 */
export const SLOT_POSITIONS: readonly Position[] = [
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
];

/**
 * One option in a picker: the minimum a player must carry to be picked, priced
 * and counted against the club limit. Deliberately narrower than
 * `PlayerWithTeam` in lib/types.ts so the rules do not depend on the schema.
 */
export interface SquadPlayer {
  elementId: number;
  webName: string;
  fullName?: string | null;
  position: Position;
  teamFplId: number;
  teamName: string;
  teamShortName: string;
  /**
   * The club's stable `code`, for shirt colours. Display only. Not `fpl_id`,
   * which FPL renumbers every season.
   */
  teamCode?: number | null;
  priceTenths: number;
  /** Display only, never a rule input: `players.status` and season points. */
  status?: string | null;
  totalPoints?: number | null;
}

/**
 * The squad as the app holds it: 15 slots (null when empty) plus the bank.
 *
 * Bank is stored, not derived, because a real manager's bank is whatever FPL
 * says it is — squad value and purchase prices drift apart as prices move. For a
 * squad built from scratch the two coincide, and `applyPick` keeps them so.
 */
export interface SquadState {
  /** Always SQUAD_SIZE long, indexed by SLOT_POSITIONS. */
  picks: (number | null)[];
  bankTenths: number;
}

export function emptySquad(): SquadState {
  return { picks: new Array(SQUAD_SIZE).fill(null), bankTenths: BUDGET_TENTHS };
}

export function isSlotIndex(i: number): boolean {
  return Number.isInteger(i) && i >= 0 && i < SQUAD_SIZE;
}

/** A player lookup, by element id. Pickers build one from the player list. */
export type PlayerIndex = ReadonlyMap<number, SquadPlayer>;

export function buildPlayerIndex(players: readonly SquadPlayer[]): PlayerIndex {
  const index = new Map<number, SquadPlayer>();
  for (const player of players) index.set(player.elementId, player);
  return index;
}

/** The players actually picked, in slot order, skipping empty and unknown ids. */
export function pickedPlayers(
  squad: SquadState,
  index: PlayerIndex,
): SquadPlayer[] {
  const out: SquadPlayer[] = [];
  for (const id of squad.picks) {
    if (id == null) continue;
    const player = index.get(id);
    if (player) out.push(player);
  }
  return out;
}

/** Sum of the picked players' prices, in tenths. Integer arithmetic only. */
export function squadCostTenths(squad: SquadState, index: PlayerIndex): number {
  let total = 0;
  for (const player of pickedPlayers(squad, index)) total += player.priceTenths;
  return total;
}

/**
 * Cost + bank. For a squad built inside the budget this is BUDGET_TENTHS; a
 * squad imported from FPL later can exceed it, which is a rise in value, not an
 * error.
 */
export function squadValueTenths(squad: SquadState, index: PlayerIndex): number {
  return squadCostTenths(squad, index) + squad.bankTenths;
}

export function countByPosition(
  players: readonly SquadPlayer[],
): Record<Position, number> {
  const counts: Record<Position, number> = { GKP: 0, DEF: 0, MID: 0, FWD: 0 };
  for (const player of players) counts[player.position] += 1;
  return counts;
}

export interface ClubCount {
  teamFplId: number;
  teamName: string;
  teamShortName: string;
  count: number;
}

/** Players per club, busiest first, for the running totals strip. */
export function countByClub(players: readonly SquadPlayer[]): ClubCount[] {
  const counts = new Map<number, ClubCount>();
  for (const player of players) {
    const existing = counts.get(player.teamFplId);
    if (existing) {
      existing.count += 1;
    } else {
      counts.set(player.teamFplId, {
        teamFplId: player.teamFplId,
        teamName: player.teamName,
        teamShortName: player.teamShortName,
        count: 1,
      });
    }
  }
  return [...counts.values()].sort(
    (a, b) => b.count - a.count || a.teamShortName.localeCompare(b.teamShortName),
  );
}

/**
 * Apply a pick to a slot, moving the cost through the bank.
 *
 * Buying spends from the bank, selling returns to it, so bank + cost stays put.
 * The subtraction is integer and stays integer; an unknown or mismatched player
 * leaves the squad alone rather than silently corrupting the bank.
 */
export function applyPick(
  squad: SquadState,
  slotIndex: number,
  player: SquadPlayer | null,
  index: PlayerIndex,
): SquadState {
  if (!isSlotIndex(slotIndex)) return squad;
  if (player && player.position !== SLOT_POSITIONS[slotIndex]) return squad;

  const previousId = squad.picks[slotIndex];
  const previous = previousId == null ? null : index.get(previousId) ?? null;

  const picks = squad.picks.slice();
  picks[slotIndex] = player ? player.elementId : null;

  const refund = previous ? previous.priceTenths : 0;
  const spend = player ? player.priceTenths : 0;

  return { picks, bankTenths: squad.bankTenths + refund - spend };
}

/** Whether this player is already in the squad (optionally ignoring one slot). */
export function isPicked(
  squad: SquadState,
  elementId: number,
  exceptSlot?: number,
): boolean {
  return squad.picks.some(
    (id, i) => id === elementId && (exceptSlot == null || i !== exceptSlot),
  );
}

export type IssueCode =
  | "incomplete"
  | "duplicate"
  | "unknown-player"
  | "position-count"
  | "club-limit"
  | "over-budget"
  | "negative-bank"
  | "xi-size"
  | "xi-position"
  | "xi-not-in-squad";

export type IssueSeverity = "error" | "warning";

export interface ValidationIssue {
  code: IssueCode;
  severity: IssueSeverity;
  /** Specific and actionable — names the club, the position, the amount. */
  message: string;
  /** Slots the issue points at, for highlighting.  */
  slots?: number[];
}

export interface SquadValidation {
  issues: ValidationIssue[];
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
  /** Complete, legal and affordable. */
  valid: boolean;
  filled: number;
  costTenths: number;
  bankTenths: number;
  positionCounts: Record<Position, number>;
  clubCounts: ClubCount[];
}

/** Tenths to a display string, local to messages. lib/format.ts owns the UI's. */
function money(tenths: number): string {
  const sign = tenths < 0 ? "-" : "";
  const abs = Math.abs(tenths);
  const whole = Math.floor(abs / 10);
  const tenth = abs % 10;
  return `${sign}£${whole}.${tenth}m`;
}

function plural(n: number, one: string, many = `${one}s`): string {
  return n === 1 ? one : many;
}

/**
 * Validate a squad and say precisely what is wrong.
 *
 * "You have 4 Arsenal players — the limit is 3" is actionable; "invalid squad"
 * is not, and on a 15-slot form the difference is whether anyone finishes.
 */
export function validateSquad(
  squad: SquadState,
  index: PlayerIndex,
): SquadValidation {
  const issues: ValidationIssue[] = [];

  const filledSlots: number[] = [];
  const unknownSlots: number[] = [];
  const seen = new Map<number, number[]>();

  squad.picks.forEach((id, slot) => {
    if (id == null) return;
    filledSlots.push(slot);
    const slots = seen.get(id) ?? [];
    slots.push(slot);
    seen.set(id, slots);
    if (!index.has(id)) unknownSlots.push(slot);
  });

  const players = pickedPlayers(squad, index);
  const positionCounts = countByPosition(players);
  const clubCounts = countByClub(players);
  const costTenths = squadCostTenths(squad, index);
  const filled = filledSlots.length;

  if (filled < SQUAD_SIZE) {
    const missing = SQUAD_SIZE - filled;
    issues.push({
      code: "incomplete",
      severity: "error",
      message: `Pick ${missing} more ${plural(missing, "player")} — ${filled} of ${SQUAD_SIZE} slots filled.`,
      slots: squad.picks
        .map((id, slot) => (id == null ? slot : -1))
        .filter((slot) => slot >= 0),
    });
  }

  if (unknownSlots.length > 0) {
    issues.push({
      code: "unknown-player",
      severity: "error",
      message:
        unknownSlots.length === 1
          ? `One pick is not in this season's player list — clear it and pick again.`
          : `${unknownSlots.length} picks are not in this season's player list — clear them and pick again.`,
      slots: unknownSlots,
    });
  }

  for (const [id, slots] of seen) {
    if (slots.length < 2) continue;
    const player = index.get(id);
    const name = player ? player.webName : `Player ${id}`;
    issues.push({
      code: "duplicate",
      severity: "error",
      message: `${name} is picked ${slots.length} times — a squad can only hold a player once.`,
      slots,
    });
  }

  for (const position of POSITIONS) {
    const want = SQUAD_QUOTA[position];
    const have = positionCounts[position];
    if (have > want) {
      issues.push({
        code: "position-count",
        severity: "error",
        message: `You have ${have} ${plural(have, POSITION_LABEL[position].toLowerCase())} — a squad needs exactly ${want}.`,
      });
    }
  }

  for (const club of clubCounts) {
    if (club.count > MAX_PER_CLUB) {
      issues.push({
        code: "club-limit",
        severity: "error",
        message: `You have ${club.count} ${club.teamName} players — the limit is ${MAX_PER_CLUB}.`,
      });
    }
  }

  if (squad.bankTenths < 0) {
    issues.push({
      code: "negative-bank",
      severity: "error",
      message: `You are ${money(-squad.bankTenths)} short — sell someone or pick cheaper cover.`,
    });
  } else if (costTenths + squad.bankTenths > BUDGET_TENTHS) {
    issues.push({
      code: "over-budget",
      severity: "warning",
      message: `This squad is worth ${money(costTenths + squad.bankTenths)}, above the ${money(BUDGET_TENTHS)} starting budget — fine for a team whose value has risen, not for a new one.`,
    });
  }

  const errors = issues.filter((issue) => issue.severity === "error");
  const warnings = issues.filter((issue) => issue.severity === "warning");

  return {
    issues,
    errors,
    warnings,
    valid: errors.length === 0 && filled === SQUAD_SIZE,
    filled,
    costTenths,
    bankTenths: squad.bankTenths,
    positionCounts,
    clubCounts,
  };
}

/** Formation validity for a starting eleven, by position counts alone. */
export function isValidFormation(counts: Record<Position, number>): boolean {
  const total = POSITIONS.reduce((sum, position) => sum + counts[position], 0);
  if (total !== XI_SIZE) return false;
  return POSITIONS.every(
    (position) =>
      counts[position] >= XI_MIN[position] && counts[position] <= XI_MAX[position],
  );
}

/** "3-4-3" — outfield only, as FPL writes it. */
export function formationName(counts: Record<Position, number>): string {
  return `${counts.DEF}-${counts.MID}-${counts.FWD}`;
}

/** Every legal formation, for a formation picker. */
export function legalFormations(): Record<Position, number>[] {
  const out: Record<Position, number>[] = [];
  for (let def = XI_MIN.DEF; def <= XI_MAX.DEF; def += 1) {
    for (let mid = XI_MIN.MID; mid <= XI_MAX.MID; mid += 1) {
      const fwd = XI_SIZE - 1 - def - mid;
      const counts: Record<Position, number> = { GKP: 1, DEF: def, MID: mid, FWD: fwd };
      if (isValidFormation(counts)) out.push(counts);
    }
  }
  return out;
}

/** Validate a starting eleven drawn from a squad. */
export function validateXI(
  xi: readonly number[],
  squad: SquadState,
  index: PlayerIndex,
): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  const unique = [...new Set(xi)];

  if (unique.length !== XI_SIZE) {
    issues.push({
      code: "xi-size",
      severity: "error",
      message: `A starting eleven needs ${XI_SIZE} different players — this has ${unique.length}.`,
    });
  }

  const notInSquad = unique.filter((id) => !squad.picks.includes(id));
  if (notInSquad.length > 0) {
    issues.push({
      code: "xi-not-in-squad",
      severity: "error",
      message: `${notInSquad.length} of the starting eleven ${plural(notInSquad.length, "is", "are")} not in the 15.`,
    });
  }

  const players = unique
    .map((id) => index.get(id))
    .filter((player): player is SquadPlayer => player != null);
  const counts = countByPosition(players);

  for (const position of POSITIONS) {
    const have = counts[position];
    if (have < XI_MIN[position]) {
      issues.push({
        code: "xi-position",
        severity: "error",
        message: `Start at least ${XI_MIN[position]} ${plural(XI_MIN[position], POSITION_LABEL[position].toLowerCase())} — you have ${have}.`,
      });
    } else if (have > XI_MAX[position]) {
      issues.push({
        code: "xi-position",
        severity: "error",
        message: `Start at most ${XI_MAX[position]} ${plural(XI_MAX[position], POSITION_LABEL[position].toLowerCase())} — you have ${have}.`,
      });
    }
  }

  return issues;
}

/**
 * 32-bit FNV-1a. Not cryptographic; it does not need to be. It needs to be
 * stable across browsers and across runs, which is why it is written out rather
 * than pulled from a dependency.
 */
function fnv1a(input: string, seed: number): number {
  let hash = seed >>> 0;
  for (let i = 0; i < input.length; i += 1) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash >>> 0;
}

function hex8(value: number): string {
  return (value >>> 0).toString(16).padStart(8, "0");
}

/**
 * The anonymous key a recommendation is logged under.
 *
 * Sorted element ids plus the bank, and nothing else — no device id, no session,
 * nothing derived from a person. Sorting is what makes it stable: the same 15
 * players entered in a different slot order must hash the same, or the log
 * cannot tell two identical squads apart. Empty slots are included as `_` so a
 * partial squad cannot collide with a different partial squad.
 */
export function squadHash(squad: SquadState): string {
  const ids = squad.picks
    .filter((id): id is number => id != null)
    .slice()
    .sort((a, b) => a - b);
  const empty = squad.picks.length - ids.length;
  const payload = `v1|${ids.join(",")}|${empty}|${squad.bankTenths}`;
  return hex8(fnv1a(payload, 0x811c9dc5)) + hex8(fnv1a(payload, 0x9e3779b9));
}
