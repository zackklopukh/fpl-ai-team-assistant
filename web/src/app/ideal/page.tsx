import type { Metadata } from "next";

import IdealTeam, { type TeamInfo } from "@/components/IdealTeam";
import { dataSource, getCurrentGameweek, getPlayers, getTeams } from "@/lib/db";
import { getFixturesInRange } from "@/lib/fixtures";
import { buildNextFixtures } from "@/lib/nextFixtures";
import { POSITION_BY_ELEMENT_TYPE, type SquadPlayer } from "@/lib/squad";
import type { PlayerWithTeam } from "@/lib/types";

export const metadata: Metadata = {
  title: "Ideal team",
  description:
    "The best fifteen for the next few gameweeks — from scratch on a budget, or as a wildcard from your own squad — laid out on the pitch with the reasoning behind it.",
};

/** Same staleness budget as the squad builder: prices move nightly. */
export const revalidate = 900;

/** The same narrow view the builder uses, so a hand-off is the same data. */
function toSquadPlayer(player: PlayerWithTeam): SquadPlayer {
  return {
    elementId: player.element_id,
    webName: player.web_name,
    fullName: player.full_name,
    position: POSITION_BY_ELEMENT_TYPE[player.element_type],
    teamFplId: player.team_fpl_id,
    teamName: player.team_name,
    teamShortName: player.team_short_name,
    teamCode: player.team_code,
    priceTenths: player.now_cost_tenths,
    status: player.status,
    totalPoints: player.total_points,
  };
}

export default async function IdealPage() {
  const [players, gameweek, teams] = await Promise.all([
    getPlayers(),
    getCurrentGameweek(),
    getTeams(),
  ]);
  const options = players.map(toSquadPlayer);
  const source = dataSource();

  // The lineup is for the first gameweek of the horizon, which is this one.
  const gw = gameweek?.gw ?? null;
  const nextFixtures =
    gw == null
      ? {}
      : buildNextFixtures(
          await getFixturesInRange(gw, gw),
          new Map(teams.map((t) => [t.fpl_id, t.short_name])),
          gw,
        );

  // By fpl_id, for a player the optimizer returns that our list lacks: the
  // shirt still gets its club's colours and code.
  const teamInfo: Record<number, TeamInfo> = {};
  for (const t of teams) {
    teamInfo[t.fpl_id] = { name: t.name, shortName: t.short_name, code: t.code };
  }

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-10 sm:px-6 lg:px-8">
      <header className="mb-8 flex flex-col gap-3">
        <h1 className="text-3xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
          Ideal team
        </h1>
        <p className="max-w-2xl text-sm leading-6 text-slate-600 dark:text-slate-300">
          The fifteen with the most expected points over the next few gameweeks
          — either from scratch on a budget, or as a wildcard rebuilt from the
          squad you have, where the players you keep are worth what they sell
          for. Nothing you enter here is stored.
        </p>
      </header>

      <IdealTeam
        players={options}
        teams={teamInfo}
        currentGw={gw}
        nextFixtures={nextFixtures}
        dataNote={
          source === "seed"
            ? `Names and clubs from the bundled sample data (${options.length} players) — set DATABASE_URL for the live list.`
            : null
        }
      />
    </div>
  );
}
