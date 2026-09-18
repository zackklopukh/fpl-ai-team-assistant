import { describe, expect, it } from "vitest";

import type { Fixture } from "../fixtures";
import { buildNextFixtures, formatOpponents } from "../nextFixtures";

function fixture(
  id: number,
  gw: number | null,
  home: number,
  away: number,
  kickoff: string,
): Fixture {
  return {
    season: "2026-27",
    fixture_id: id,
    gw,
    team_h_fpl_id: home,
    team_a_fpl_id: away,
    team_h_difficulty: 2,
    team_a_difficulty: 4,
    kickoff_time: kickoff,
    started: false,
    finished: false,
    finished_provisional: false,
    minutes: 0,
    team_h_score: null,
    team_a_score: null,
  };
}

const NAMES = new Map([
  [1, "ARS"],
  [2, "AVL"],
  [3, "BOU"],
  [4, "BRE"],
]);

describe("buildNextFixtures", () => {
  it("gives each side of a fixture the other as its opponent, home and away", () => {
    const out = buildNextFixtures([fixture(1, 5, 1, 2, "2026-09-19T14:00:00Z")], NAMES, 5);
    expect(out[1]).toEqual([{ opponentShortName: "AVL", isHome: true, difficulty: 2 }]);
    expect(out[2]).toEqual([{ opponentShortName: "ARS", isHome: false, difficulty: 4 }]);
  });

  it("reads difficulty off each club's own side, not the home side for both", () => {
    const out = buildNextFixtures([fixture(1, 5, 1, 2, "2026-09-19T14:00:00Z")], NAMES, 5);
    expect(out[1][0].difficulty).toBe(2);
    expect(out[2][0].difficulty).toBe(4);
  });

  it("gives a club with a double both opponents, in kickoff order", () => {
    const out = buildNextFixtures(
      [
        fixture(2, 5, 3, 1, "2026-09-22T19:00:00Z"),
        fixture(1, 5, 1, 2, "2026-09-19T14:00:00Z"),
      ],
      NAMES,
      5,
    );
    expect(out[1].map((o) => o.opponentShortName)).toEqual(["AVL", "BOU"]);
    expect(out[1].map((o) => o.isHome)).toEqual([true, false]);
  });

  it("gives a club with a blank an empty list, not a missing key", () => {
    const out = buildNextFixtures([fixture(1, 5, 1, 2, "2026-09-19T14:00:00Z")], NAMES, 5);
    // Brentford does not play. The key must exist so a card can say so.
    expect(out[4]).toEqual([]);
  });

  it("ignores other gameweeks and unscheduled fixtures", () => {
    const out = buildNextFixtures(
      [
        fixture(1, 6, 1, 2, "2026-09-26T14:00:00Z"),
        fixture(2, null, 3, 4, ""),
      ],
      NAMES,
      5,
    );
    for (const id of NAMES.keys()) expect(out[id]).toEqual([]);
  });
});

describe("formatOpponents", () => {
  it("formats a single fixture", () => {
    expect(formatOpponents([{ opponentShortName: "BUR", isHome: true, difficulty: 2 }])).toBe(
      "BUR (H)",
    );
  });

  it("formats a double as both fixtures", () => {
    expect(
      formatOpponents([
        { opponentShortName: "BUR", isHome: true, difficulty: 2 },
        { opponentShortName: "LIV", isHome: false, difficulty: 5 },
      ]),
    ).toBe("BUR (H), LIV (A)");
  });

  it("formats a blank as a dash rather than an empty string", () => {
    expect(formatOpponents([])).toBe("—");
    expect(formatOpponents(undefined)).toBe("—");
  });
});
