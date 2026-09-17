import { describe, expect, it } from "vitest";

import {
  BUDGET_TENTHS,
  MAX_PER_CLUB,
  SLOT_POSITIONS,
  SQUAD_SIZE,
  applyPick,
  buildPlayerIndex,
  countByClub,
  emptySquad,
  formationName,
  isValidFormation,
  legalFormations,
  squadCostTenths,
  squadHash,
  squadValueTenths,
  validateSquad,
  validateXI,
  type Position,
  type SquadPlayer,
  type SquadState,
} from "../squad";

const TEAMS = [
  { id: 1, name: "Arsenal", short: "ARS" },
  { id: 2, name: "Brighton", short: "BHA" },
  { id: 3, name: "Chelsea", short: "CHE" },
  { id: 4, name: "Everton", short: "EVE" },
  { id: 5, name: "Fulham", short: "FUL" },
  { id: 6, name: "Liverpool", short: "LIV" },
];

function player(
  elementId: number,
  position: Position,
  priceTenths: number,
  teamIndex: number,
): SquadPlayer {
  const team = TEAMS[teamIndex % TEAMS.length];
  return {
    elementId,
    webName: `Player ${elementId}`,
    position,
    teamFplId: team.id,
    teamName: team.name,
    teamShortName: team.short,
    priceTenths,
  };
}

/** 15 players, one per slot, spread across clubs so nobody exceeds three. */
function makeSquadPlayers(price = 60): SquadPlayer[] {
  return SLOT_POSITIONS.map((position, slot) =>
    player(100 + slot, position, price, slot % TEAMS.length),
  );
}

function fill(players: SquadPlayer[]): {
  squad: SquadState;
  index: ReturnType<typeof buildPlayerIndex>;
} {
  const index = buildPlayerIndex(players);
  let squad = emptySquad();
  players.forEach((p, slot) => {
    squad = applyPick(squad, slot, p, index);
  });
  return { squad, index };
}

describe("budget arithmetic", () => {
  it("stays integral and conserves cost + bank", () => {
    const players = makeSquadPlayers().map((p, i) =>
      // Prices that would produce classic float error if divided by ten first.
      ({ ...p, priceTenths: 45 + i }),
    );
    const { squad, index } = fill(players);

    const cost = squadCostTenths(squad, index);
    expect(Number.isInteger(cost)).toBe(true);
    expect(cost).toBe(players.reduce((sum, p) => sum + p.priceTenths, 0));
    expect(Number.isInteger(squad.bankTenths)).toBe(true);
    expect(cost + squad.bankTenths).toBe(BUDGET_TENTHS);
    expect(squadValueTenths(squad, index)).toBe(BUDGET_TENTHS);
  });

  it("returns the exact price to the bank when a player is swapped out", () => {
    const players = makeSquadPlayers();
    const { squad } = fill(players);
    const replacement = player(900, "MID", 133, 0);
    const withReplacement = applyPick(squad, 7, replacement, buildPlayerIndex([...players, replacement]));

    expect(withReplacement.bankTenths).toBe(squad.bankTenths + 60 - 133);
    expect(Number.isInteger(withReplacement.bankTenths)).toBe(true);

    const cleared = applyPick(
      withReplacement,
      7,
      null,
      buildPlayerIndex([...players, replacement]),
    );
    expect(cleared.bankTenths).toBe(squad.bankTenths + 60);
    expect(cleared.picks[7]).toBeNull();
  });

  it("flags an overspend with the amount, not a generic message", () => {
    const players = makeSquadPlayers(80); // 15 x £8.0m = £120.0m
    const { squad, index } = fill(players);
    const result = validateSquad(squad, index);

    expect(squad.bankTenths).toBe(BUDGET_TENTHS - 1200);
    const overspend = result.errors.find((issue) => issue.code === "negative-bank");
    expect(overspend?.message).toContain("£20.0m");
    expect(result.valid).toBe(false);
  });

  it("refuses a player in a slot of the wrong position", () => {
    const players = makeSquadPlayers();
    const index = buildPlayerIndex(players);
    const keeper = players[0];
    const before = emptySquad();
    const after = applyPick(before, 7, keeper, index); // slot 7 is a MID slot

    expect(after).toBe(before);
  });
});

describe("squad composition", () => {
  it("accepts a legal 2/5/5/3 squad inside the budget", () => {
    const { squad, index } = fill(makeSquadPlayers(45));
    const result = validateSquad(squad, index);

    expect(result.valid).toBe(true);
    expect(result.errors).toEqual([]);
    expect(result.filled).toBe(SQUAD_SIZE);
    expect(result.positionCounts).toEqual({ GKP: 2, DEF: 5, MID: 5, FWD: 3 });
  });

  it("names the number of slots still to fill", () => {
    const players = makeSquadPlayers();
    const index = buildPlayerIndex(players);
    let squad = emptySquad();
    squad = applyPick(squad, 0, players[0], index);
    squad = applyPick(squad, 1, players[1], index);

    const issue = validateSquad(squad, index).errors.find(
      (i) => i.code === "incomplete",
    );
    expect(issue?.message).toBe("Pick 13 more players — 2 of 15 slots filled.");
    expect(issue?.slots).toHaveLength(13);
  });

  it("rejects too many of a position", () => {
    const players = makeSquadPlayers();
    // Hand-build an illegal shape: applyPick would not allow it.
    const sixth = player(500, "MID", 50, 0);
    const index = buildPlayerIndex([...players, sixth]);
    const squad: SquadState = {
      picks: players.map((p) => p.elementId),
      bankTenths: 100,
    };
    squad.picks[12] = sixth.elementId; // a MID in a FWD slot

    const result = validateSquad(squad, index);
    const issue = result.errors.find((i) => i.code === "position-count");
    expect(issue?.message).toBe("You have 6 midfielders — a squad needs exactly 5.");
    expect(result.valid).toBe(false);
  });

  it("enforces the three-per-club limit and names the club", () => {
    const players = makeSquadPlayers();
    // Four Arsenal players: slots 0, 6 and 12 are already ARS (index 0 of TEAMS).
    players[1] = { ...players[1], teamFplId: 1, teamName: "Arsenal", teamShortName: "ARS" };
    const { squad, index } = fill(players);

    const result = validateSquad(squad, index);
    const issue = result.errors.find((i) => i.code === "club-limit");
    expect(issue?.message).toBe("You have 4 Arsenal players — the limit is 3.");
    expect(result.valid).toBe(false);

    const arsenal = result.clubCounts.find((club) => club.teamFplId === 1);
    expect(arsenal?.count).toBe(4);
  });

  it("allows exactly three from one club", () => {
    const { squad, index } = fill(makeSquadPlayers(45));
    const result = validateSquad(squad, index);
    expect(Math.max(...result.clubCounts.map((c) => c.count))).toBe(MAX_PER_CLUB);
    expect(result.errors.some((i) => i.code === "club-limit")).toBe(false);
  });

  it("names a duplicated player", () => {
    const players = makeSquadPlayers();
    const index = buildPlayerIndex(players);
    const squad: SquadState = {
      picks: players.map((p) => p.elementId),
      bankTenths: 100,
    };
    squad.picks[8] = squad.picks[7];

    const issue = validateSquad(squad, index).errors.find((i) => i.code === "duplicate");
    expect(issue?.message).toContain("Player 107");
    expect(issue?.message).toContain("picked 2 times");
    expect(issue?.slots).toEqual([7, 8]);
  });

  it("flags a pick that is not in the player list", () => {
    const players = makeSquadPlayers();
    const index = buildPlayerIndex(players);
    const squad: SquadState = { picks: players.map((p) => p.elementId), bankTenths: 0 };
    squad.picks[4] = 999;

    const issue = validateSquad(squad, index).errors.find(
      (i) => i.code === "unknown-player",
    );
    expect(issue?.slots).toEqual([4]);
  });

  it("counts clubs busiest first", () => {
    const players = makeSquadPlayers();
    const counts = countByClub(players);
    expect(counts[0].count).toBeGreaterThanOrEqual(counts[counts.length - 1].count);
  });
});

describe("formations", () => {
  it("accepts the legal shapes and rejects the rest", () => {
    expect(isValidFormation({ GKP: 1, DEF: 3, MID: 4, FWD: 3 })).toBe(true);
    expect(isValidFormation({ GKP: 1, DEF: 5, MID: 4, FWD: 1 })).toBe(true);
    expect(isValidFormation({ GKP: 1, DEF: 4, MID: 4, FWD: 2 })).toBe(true);

    expect(isValidFormation({ GKP: 0, DEF: 4, MID: 4, FWD: 3 })).toBe(false);
    expect(isValidFormation({ GKP: 2, DEF: 4, MID: 4, FWD: 1 })).toBe(false);
    expect(isValidFormation({ GKP: 1, DEF: 2, MID: 5, FWD: 3 })).toBe(false);
    expect(isValidFormation({ GKP: 1, DEF: 6, MID: 3, FWD: 1 })).toBe(false);
    expect(isValidFormation({ GKP: 1, DEF: 4, MID: 1, FWD: 5 })).toBe(false);
    expect(isValidFormation({ GKP: 1, DEF: 3, MID: 3, FWD: 3 })).toBe(false); // 10
  });

  it("enumerates every legal formation, all of eleven players", () => {
    const formations = legalFormations();
    expect(formations.length).toBeGreaterThan(0);
    for (const counts of formations) {
      expect(counts.GKP + counts.DEF + counts.MID + counts.FWD).toBe(11);
      expect(isValidFormation(counts)).toBe(true);
    }
    expect(formations.map(formationName)).toContain("3-4-3");
    expect(formations.map(formationName)).toContain("5-4-1");
    expect(formations.map(formationName)).not.toContain("2-5-3");
  });

  it("validates an XI against the squad it came from", () => {
    const players = makeSquadPlayers(45);
    const { squad, index } = fill(players);

    // 1 GKP, 5 DEF, 3 MID, 2 FWD
    const xi = [
      players[0],
      ...players.slice(2, 7),
      ...players.slice(7, 10),
      ...players.slice(12, 14),
    ].map((p) => p.elementId);

    expect(validateXI(xi, squad, index)).toEqual([]);

    const tooFewDefenders = [
      players[0],
      ...players.slice(2, 4),
      ...players.slice(7, 12),
      ...players.slice(12, 15),
    ].map((p) => p.elementId);
    const issues = validateXI(tooFewDefenders, squad, index);
    expect(issues.some((i) => i.message.includes("at least 3 defenders"))).toBe(true);

    const outsider = validateXI([...xi.slice(1), 9999], squad, index);
    expect(outsider.some((i) => i.code === "xi-not-in-squad")).toBe(true);
  });
});

describe("squadHash", () => {
  it("is stable across orderings of the same squad", () => {
    const players = makeSquadPlayers(45);
    const { squad } = fill(players);

    const shuffled: SquadState = {
      picks: squad.picks.slice().reverse(),
      bankTenths: squad.bankTenths,
    };

    expect(squadHash(shuffled)).toBe(squadHash(squad));
  });

  it("is stable across calls and looks like a hash", () => {
    const { squad } = fill(makeSquadPlayers(45));
    expect(squadHash(squad)).toBe(squadHash(squad));
    expect(squadHash(squad)).toMatch(/^[0-9a-f]{16}$/);
  });

  it("changes when the squad or the bank changes", () => {
    const players = makeSquadPlayers(45);
    const { squad } = fill(players);
    const base = squadHash(squad);

    expect(squadHash({ ...squad, bankTenths: squad.bankTenths + 1 })).not.toBe(base);

    const swapped = applyPick(
      squad,
      7,
      player(700, "MID", 45, 1),
      buildPlayerIndex([...players, player(700, "MID", 45, 1)]),
    );
    expect(squadHash(swapped)).not.toBe(base);
  });

  it("distinguishes a partial squad from a different partial squad", () => {
    const players = makeSquadPlayers(45);
    const index = buildPlayerIndex(players);
    let a = emptySquad();
    a = applyPick(a, 0, players[0], index);
    let b = emptySquad();
    b = applyPick(b, 1, players[1], index);

    expect(squadHash(a)).not.toBe(squadHash(b));
  });

  it("depends on nothing but the picks and the bank", () => {
    const players = makeSquadPlayers(45);
    const { squad } = fill(players);
    const renamed = squadHash({
      picks: squad.picks.slice(),
      bankTenths: squad.bankTenths,
    });
    expect(renamed).toBe(squadHash(squad));
  });
});
