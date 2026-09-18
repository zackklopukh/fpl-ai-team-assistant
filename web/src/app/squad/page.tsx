import type { Metadata } from "next";

import SquadBuilder from "@/components/SquadBuilder";
import { dataSource, getCurrentGameweek, getPlayers, getTeams } from "@/lib/db";
import { getFixturesInRange } from "@/lib/fixtures";
import { buildNextFixtures } from "@/lib/nextFixtures";
import { POSITION_BY_ELEMENT_TYPE, type SquadPlayer } from "@/lib/squad";
import type { PlayerWithTeam } from "@/lib/types";

export const metadata: Metadata = {
  title: "Build a squad",
  description:
    "Pick fifteen players, see the cost, the bank and the club limits as you go, and share the squad with a link. No account, nothing stored.",
};

/** Prices move nightly; fifteen minutes of staleness is fine and keeps the page cheap. */
export const revalidate = 900;

/**
 * The picker only needs a narrow view of a player — the rules in lib/squad.ts
 * deliberately do not depend on the database row shape.
 */
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

export default async function SquadPage() {
  const [players, gameweek, teams] = await Promise.all([
    getPlayers(),
    getCurrentGameweek(),
    getTeams(),
  ]);
  const options = players.map(toSquadPlayer);
  const source = dataSource();

  // Opponents for the gameweek being planned, for the cards on the pitch.
  const nextGw = gameweek?.gw ?? null;
  const nextFixtures =
    nextGw == null
      ? {}
      : buildNextFixtures(
          await getFixturesInRange(nextGw, nextGw),
          new Map(teams.map((t) => [t.fpl_id, t.short_name])),
          nextGw,
        );

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-10 sm:px-6 lg:px-8">
      <header className="mb-8 flex flex-col gap-3">
        <h1 className="text-3xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
          Build a squad
        </h1>
        <p className="max-w-2xl text-sm leading-6 text-slate-600 dark:text-slate-300">
          Fifteen slots, searchable by player or club. Totals and rule breaches
          update as you pick. There is no sign-in: the squad is kept in this
          browser and in the link, and it is never sent anywhere.
        </p>
      </header>

      {/* currentGw is the gameweek advice is asked for. Letting it default
          silently would ask the optimizer about the wrong week, and the answer
          would look like a model error rather than a plumbing one. */}
      <SquadBuilder
        players={options}
        currentGw={gameweek?.gw}
        nextFixtures={nextFixtures}
        nextGw={nextGw}
        dataNote={
          source === "seed"
            ? `Prices from the bundled sample data (${options.length} players) — set DATABASE_URL for the live list.`
            : `Prices from the last data sync (${options.length} players).`
        }
      />
    </div>
  );
}
