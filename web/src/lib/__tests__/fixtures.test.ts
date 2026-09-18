import { describe, expect, it } from "vitest";

import {
  firstScheduledGameweek,
  groupStatsByGameweek,
  isScheduled,
  lastScheduledGameweek,
  seedFixtureSample,
  teamDifficulty,
  teamFixturesByGameweek,
  toTeamFixture,
  unscheduledFixtures,
  type Fixture,
  type PlayerGameweekStat,
} from "../fixtures";

/**
 * The seed sample has no double, no blank and no unscheduled fixture — it is a
 * four-gameweek capture of an ordinary run. So the cases that actually matter
 * are built here by hand. That is the point: these are the shapes the real
 * table produces in December and the seed never will.
 */
function fixture(partial: Partial<Fixture> & { fixture_id: number }): Fixture {
  return {
    season: "2026-27",
    gw: 5,
    team_h_fpl_id: 1,
    team_a_fpl_id: 2,
    team_h_difficulty: 3,
    team_a_difficulty: 3,
    kickoff_time: "2026-09-19T14:00:00Z",
    started: false,
    finished: false,
    finished_provisional: false,
    minutes: 0,
    team_h_score: null,
    team_a_score: null,
    ...partial,
  };
}

const ARS = 1;
const AVL = 2;
const BOU = 3;
const BRE = 4;

describe("toTeamFixture — which side the difficulty is read from", () => {
  // The single easiest thing to invert in this whole file. team_h_difficulty is
  // the FDR *for the home team*, not the difficulty of the home team.
  const f = fixture({
    fixture_id: 100,
    team_h_fpl_id: ARS,
    team_a_fpl_id: AVL,
    team_h_difficulty: 2,
    team_a_difficulty: 5,
  });

  it("gives the home team its own difficulty", () => {
    const view = toTeamFixture(f, ARS);
    expect(view).not.toBeNull();
    expect(view!.is_home).toBe(true);
    expect(view!.difficulty).toBe(2);
    expect(view!.opponent_fpl_id).toBe(AVL);
  });

  it("gives the away team its own difficulty, not the home team's", () => {
    const view = toTeamFixture(f, AVL);
    expect(view).not.toBeNull();
    expect(view!.is_home).toBe(false);
    expect(view!.difficulty).toBe(5);
    expect(view!.opponent_fpl_id).toBe(ARS);
  });

  it("maps scores to the viewing team's side", () => {
    const played = fixture({
      fixture_id: 101,
      team_h_fpl_id: ARS,
      team_a_fpl_id: AVL,
      team_h_score: 3,
      team_a_score: 0,
      finished: true,
    });
    expect(toTeamFixture(played, ARS)!.goals_for).toBe(3);
    expect(toTeamFixture(played, ARS)!.goals_against).toBe(0);
    expect(toTeamFixture(played, AVL)!.goals_for).toBe(0);
    expect(toTeamFixture(played, AVL)!.goals_against).toBe(3);
  });

  it("returns null for a team that is not in the fixture", () => {
    expect(toTeamFixture(f, BOU)).toBeNull();
  });
});

describe("unscheduled fixtures (null gw)", () => {
  const unscheduled = fixture({
    fixture_id: 200,
    gw: null,
    team_h_fpl_id: ARS,
    team_a_fpl_id: BOU,
    kickoff_time: null,
  });

  it("is not treated as scheduled", () => {
    expect(isScheduled(unscheduled)).toBe(false);
  });

  it("has no team view, rather than crashing or landing in gameweek 0", () => {
    expect(toTeamFixture(unscheduled, ARS)).toBeNull();
  });

  it("is excluded from a gameweek window without throwing", () => {
    const weeks = teamFixturesByGameweek([unscheduled], ARS, 5, 7);
    expect(weeks).toHaveLength(3);
    expect(weeks.every((w) => w.isBlank)).toBe(true);
  });

  it("contributes nothing to aggregate difficulty", () => {
    const d = teamDifficulty([unscheduled], ARS, 5, 7);
    expect(d.fixtureCount).toBe(0);
    expect(d.totalDifficulty).toBe(0);
  });

  it("is still surfaced separately, not silently dropped", () => {
    const scheduled = fixture({ fixture_id: 201, gw: 6 });
    expect(unscheduledFixtures([unscheduled, scheduled])).toEqual([unscheduled]);
  });
});

describe("double gameweeks", () => {
  // Arsenal play twice in GW6: home to Bournemouth, away at Brentford.
  const fixtures: Fixture[] = [
    fixture({
      fixture_id: 300,
      gw: 6,
      team_h_fpl_id: ARS,
      team_a_fpl_id: BOU,
      team_h_difficulty: 2,
      team_a_difficulty: 4,
      kickoff_time: "2026-10-10T14:00:00Z",
    }),
    fixture({
      fixture_id: 301,
      gw: 6,
      team_h_fpl_id: BRE,
      team_a_fpl_id: ARS,
      team_h_difficulty: 4,
      team_a_difficulty: 3,
      kickoff_time: "2026-10-13T19:00:00Z",
    }),
  ];

  it("returns two fixtures for the gameweek, not one", () => {
    const weeks = teamFixturesByGameweek(fixtures, ARS, 6, 6);
    expect(weeks).toHaveLength(1);
    expect(weeks[0].fixtures).toHaveLength(2);
    expect(weeks[0].isDouble).toBe(true);
    expect(weeks[0].isBlank).toBe(false);
  });

  it("orders the two by kickoff", () => {
    const [week] = teamFixturesByGameweek(fixtures, ARS, 6, 6);
    expect(week.fixtures.map((f) => f.fixture_id)).toEqual([300, 301]);
  });

  it("reads each side's difficulty independently", () => {
    const [week] = teamFixturesByGameweek(fixtures, ARS, 6, 6);
    expect(week.fixtures[0].is_home).toBe(true);
    expect(week.fixtures[0].difficulty).toBe(2); // home side of fixture 300
    expect(week.fixtures[1].is_home).toBe(false);
    expect(week.fixtures[1].difficulty).toBe(3); // away side of fixture 301
  });

  it("counts BOTH fixtures in aggregate difficulty", () => {
    const d = teamDifficulty(fixtures, ARS, 6, 6);
    expect(d.fixtureCount).toBe(2);
    expect(d.totalDifficulty).toBe(5); // 2 + 3, not 2 and not 2.5
    expect(d.averageDifficulty).toBe(2.5);
    expect(d.doubleGws).toEqual([6]);
  });

  it("does not make the opponents doubles too", () => {
    expect(teamDifficulty(fixtures, BOU, 6, 6).fixtureCount).toBe(1);
    expect(teamDifficulty(fixtures, BRE, 6, 6).fixtureCount).toBe(1);
  });
});

describe("blank gameweeks", () => {
  // Bournemouth and Brentford play each other in GW6; Arsenal blank.
  const fixtures: Fixture[] = [
    fixture({
      fixture_id: 400,
      gw: 6,
      team_h_fpl_id: BOU,
      team_a_fpl_id: BRE,
      team_h_difficulty: 3,
      team_a_difficulty: 3,
    }),
  ];

  it("returns an empty list for the blanking team, not a missing entry", () => {
    const weeks = teamFixturesByGameweek(fixtures, ARS, 6, 6);
    expect(weeks).toHaveLength(1);
    expect(weeks[0].fixtures).toEqual([]);
    expect(weeks[0].isBlank).toBe(true);
    expect(weeks[0].isDouble).toBe(false);
  });

  it("contributes ZERO to the total, not a neutral or average value", () => {
    const d = teamDifficulty(fixtures, ARS, 6, 6);
    expect(d.fixtureCount).toBe(0);
    expect(d.totalDifficulty).toBe(0);
    // Explicitly not 3 (neutral FDR) and not the league average.
    expect(d.totalDifficulty).not.toBe(3);
    expect(d.blankGws).toEqual([6]);
  });

  it("reports a null average rather than dividing by zero", () => {
    const d = teamDifficulty(fixtures, ARS, 6, 6);
    expect(d.averageDifficulty).toBeNull();
    expect(Number.isNaN(d.averageDifficulty as unknown as number)).toBe(false);
  });

  it("lowers the total for a window where one week blanks", () => {
    const window: Fixture[] = [
      fixture({ fixture_id: 401, gw: 5, team_h_fpl_id: ARS, team_h_difficulty: 4 }),
      // no ARS fixture in gw 6
      fixture({ fixture_id: 402, gw: 7, team_h_fpl_id: ARS, team_h_difficulty: 4 }),
    ];
    const d = teamDifficulty(window, ARS, 5, 7);
    expect(d.fixtureCount).toBe(2);
    expect(d.totalDifficulty).toBe(8);
    expect(d.blankGws).toEqual([6]);
    expect(d.byGameweek.map((w) => w.fixtures.length)).toEqual([1, 0, 1]);
  });
});

describe("a window with both a double and a blank", () => {
  const fixtures: Fixture[] = [
    // GW5: single, away, FDR 4 for ARS
    fixture({
      fixture_id: 500,
      gw: 5,
      team_h_fpl_id: AVL,
      team_a_fpl_id: ARS,
      team_h_difficulty: 3,
      team_a_difficulty: 4,
    }),
    // GW6: blank for ARS
    fixture({ fixture_id: 501, gw: 6, team_h_fpl_id: BOU, team_a_fpl_id: BRE }),
    // GW7: double, FDR 2 and 5
    fixture({
      fixture_id: 502,
      gw: 7,
      team_h_fpl_id: ARS,
      team_a_fpl_id: BOU,
      team_h_difficulty: 2,
      kickoff_time: "2026-10-17T14:00:00Z",
    }),
    fixture({
      fixture_id: 503,
      gw: 7,
      team_h_fpl_id: BRE,
      team_a_fpl_id: ARS,
      team_a_difficulty: 5,
      kickoff_time: "2026-10-20T19:00:00Z",
    }),
  ];

  it("counts three fixtures over three gameweeks", () => {
    const d = teamDifficulty(fixtures, ARS, 5, 7);
    expect(d.fixtureCount).toBe(3);
    expect(d.totalDifficulty).toBe(11); // 4 + 0 + (2 + 5)
    expect(d.blankGws).toEqual([6]);
    expect(d.doubleGws).toEqual([7]);
    expect(d.byGameweek.map((w) => w.fixtures.length)).toEqual([1, 0, 2]);
  });

  it("returns one entry per gameweek in the window, always", () => {
    const d = teamDifficulty(fixtures, ARS, 5, 9);
    expect(d.byGameweek.map((w) => w.gw)).toEqual([5, 6, 7, 8, 9]);
    expect(d.blankGws).toEqual([6, 8, 9]);
  });

  it("ignores fixtures outside the window", () => {
    const d = teamDifficulty(fixtures, ARS, 5, 6);
    expect(d.fixtureCount).toBe(1);
    expect(d.totalDifficulty).toBe(4);
  });

  it("handles a null FDR as zero without dropping the fixture", () => {
    const nullFdr = [
      fixture({
        fixture_id: 600,
        gw: 5,
        team_h_fpl_id: ARS,
        team_h_difficulty: null,
      }),
    ];
    const d = teamDifficulty(nullFdr, ARS, 5, 5);
    expect(d.fixtureCount).toBe(1);
    expect(d.totalDifficulty).toBe(0);
    expect(d.averageDifficulty).toBe(0);
  });
});

describe("window helpers", () => {
  const fixtures: Fixture[] = [
    fixture({ fixture_id: 700, gw: 2 }),
    fixture({ fixture_id: 701, gw: 4 }),
    fixture({ fixture_id: 702, gw: null }),
  ];

  it("finds the first scheduled gameweek at or after a preference", () => {
    expect(firstScheduledGameweek(fixtures, 1)).toBe(2);
    expect(firstScheduledGameweek(fixtures, 3)).toBe(4);
    expect(firstScheduledGameweek(fixtures, 5)).toBeNull();
  });

  it("finds the last scheduled gameweek, ignoring null gw", () => {
    expect(lastScheduledGameweek(fixtures)).toBe(4);
    expect(lastScheduledGameweek([fixtures[2]])).toBeNull();
  });
});

describe("seed sample", () => {
  const seedRows = seedFixtureSample();

  it("is the 40-fixture capture, all scheduled", () => {
    expect(seedRows).toHaveLength(40);
    expect(unscheduledFixtures(seedRows)).toEqual([]);
  });

  it("covers gameweeks 1 to 4 only — the reason pages fall back", () => {
    expect(firstScheduledGameweek(seedRows, 1)).toBe(1);
    expect(lastScheduledGameweek(seedRows)).toBe(4);
    expect(firstScheduledGameweek(seedRows, 5)).toBeNull();
  });

  it("gives every team exactly one fixture per covered gameweek", () => {
    // No double and no blank in the sample. Asserted so that if the seed is
    // ever regenerated with one, this test says so rather than the UI quietly
    // changing shape.
    for (let team = 1; team <= 20; team += 1) {
      const d = teamDifficulty(seedRows, team, 1, 4);
      expect(d.fixtureCount).toBe(4);
      expect(d.blankGws).toEqual([]);
      expect(d.doubleGws).toEqual([]);
    }
  });

  it("reads real FDR off both sides of a real fixture", () => {
    // Fixture 1 in the capture: team 1 at home to team 7, FDR 2 / 5.
    const first = seedRows.find((f) => f.fixture_id === 1)!;
    expect(toTeamFixture(first, first.team_h_fpl_id)!.difficulty).toBe(
      first.team_h_difficulty,
    );
    expect(toTeamFixture(first, first.team_a_fpl_id)!.difficulty).toBe(
      first.team_a_difficulty,
    );
    expect(first.team_h_difficulty).not.toBe(first.team_a_difficulty);
  });
});

describe("groupStatsByGameweek", () => {
  function stat(gw: number, fixtureId: number): PlayerGameweekStat {
    return {
      season: "2026-27",
      element_id: 1,
      gw,
      fixture_id: fixtureId,
      minutes: 90,
      total_points: 6,
      starts: 1,
      goals_scored: 0,
      assists: 0,
      clean_sheets: 1,
      goals_conceded: 0,
      yellow_cards: 0,
      red_cards: 0,
      saves: 2,
      bonus: 0,
      bps: 20,
      defensive_contribution: 0,
      expected_goals: 0,
      expected_assists: 0,
      expected_goal_involvements: 0,
      expected_goals_conceded: 0.5,
      was_home: true,
      opponent_team_fpl_id: 7,
      value_tenths: 60,
      bonus_settled: true,
    };
  }

  it("keeps both rows of a double gameweek", () => {
    const grouped = groupStatsByGameweek([stat(6, 10), stat(6, 11), stat(7, 12)]);
    expect(grouped.map((g) => g.gw)).toEqual([6, 7]);
    expect(grouped[0].stats).toHaveLength(2);
    expect(grouped[1].stats).toHaveLength(1);
  });

  it("returns nothing for no history", () => {
    expect(groupStatsByGameweek([])).toEqual([]);
  });
});
