/**
 * Each club's opponents in one gameweek, for the "next opponent" line on a card.
 *
 * Pure, so the two cases that break naive versions are testable: a club with
 * TWO fixtures in the gameweek (a double) gets both, and a club with NONE (a
 * blank) gets an empty list. The empty list is the point — a card must be able
 * to say "no game this week" instead of silently showing nothing, or worse,
 * last week's opponent. CLAUDE.md invariant 6: never "the fixture this
 * gameweek".
 */

import type { Fixture } from "@/lib/fixtures";

export interface NextOpponent {
  opponentShortName: string;
  isHome: boolean;
  /** FDR for the club this is viewed from, read off its own side. */
  difficulty: number | null;
}

/** team_fpl_id -> that club's opponents in the gameweek, in kickoff order. */
export type NextFixturesByTeam = Record<number, NextOpponent[]>;

export function buildNextFixtures(
  fixtures: readonly Fixture[],
  shortNameByTeam: ReadonlyMap<number, string>,
  gw: number,
): NextFixturesByTeam {
  const out: NextFixturesByTeam = {};
  for (const teamId of shortNameByTeam.keys()) out[teamId] = [];

  const inGw = fixtures
    .filter((f) => f.gw === gw)
    .slice()
    .sort((a, b) => (a.kickoff_time ?? "").localeCompare(b.kickoff_time ?? ""));

  for (const f of inGw) {
    (out[f.team_h_fpl_id] ??= []).push({
      opponentShortName: shortNameByTeam.get(f.team_a_fpl_id) ?? "???",
      isHome: true,
      difficulty: f.team_h_difficulty,
    });
    (out[f.team_a_fpl_id] ??= []).push({
      opponentShortName: shortNameByTeam.get(f.team_h_fpl_id) ?? "???",
      isHome: false,
      difficulty: f.team_a_difficulty,
    });
  }
  return out;
}

/** "BUR (H)", "BUR (H), LIV (A)" for a double, or "—" for a blank. */
export function formatOpponents(opponents: readonly NextOpponent[] | undefined): string {
  if (!opponents || opponents.length === 0) return "—";
  return opponents
    .map((o) => `${o.opponentShortName} (${o.isHome ? "H" : "A"})`)
    .join(", ");
}
