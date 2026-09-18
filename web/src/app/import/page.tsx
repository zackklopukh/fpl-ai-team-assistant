import type { Metadata } from "next";
import Link from "next/link";

import TeamIdImport from "@/components/TeamIdImport";
import { dataSource, getPlayers } from "@/lib/db";
import { POSITION_BY_ELEMENT_TYPE, type SquadPlayer } from "@/lib/squad";
import type { PlayerWithTeam } from "@/lib/types";

export const metadata: Metadata = {
  title: "Import your squad",
  description:
    "Type your public FPL team ID and get your real squad, with the selling price of every player. No screenshot, no sign-in, nothing stored.",
};

/** Prices move nightly; the player list here is only for the hand-off. */
export const revalidate = 900;

function toSquadPlayer(player: PlayerWithTeam): SquadPlayer {
  return {
    elementId: player.element_id,
    webName: player.web_name,
    fullName: player.full_name,
    position: POSITION_BY_ELEMENT_TYPE[player.element_type],
    teamFplId: player.team_fpl_id,
    teamName: player.team_name,
    teamShortName: player.team_short_name,
    priceTenths: player.now_cost_tenths,
    status: player.status,
    totalPoints: player.total_points,
  };
}

export default async function ImportPage() {
  const players = await getPlayers();
  const options = players.map(toSquadPlayer);
  const source = dataSource();

  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-10 sm:px-6 lg:px-8">
      <header className="mb-8 flex flex-col gap-3">
        <h1 className="text-3xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
          Import your squad
        </h1>
        <p className="max-w-2xl text-sm leading-6 text-slate-600 dark:text-slate-300">
          Your FPL team ID is public, and so is your transfer history. That is
          enough to rebuild your squad and work out what every player actually
          sells for — the number that decides whether a transfer is affordable —
          without a screenshot and without signing in anywhere.
        </p>
        <p className="max-w-2xl text-sm leading-6 text-slate-600 dark:text-slate-300">
          The ID is used for this one request and then forgotten. There are no
          accounts here and nothing about you is stored: what you keep is fifteen
          players in this browser, the same as if you had{" "}
          <Link href="/squad" className="underline underline-offset-2">
            picked them by hand
          </Link>
          .
        </p>
      </header>

      <TeamIdImport
        players={options}
        dataNote={
          source === "seed"
            ? `Prices from the bundled sample data (${options.length} players) — set DATABASE_URL for the live list.`
            : `Prices from the last data sync (${options.length} players).`
        }
      />

      <section className="mt-12 rounded-xl border border-slate-200 p-6 text-sm leading-6 text-slate-600 dark:border-slate-800 dark:text-slate-300">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-700 dark:text-slate-200">
          Two things this cannot do
        </h2>
        <ul className="flex list-disc flex-col gap-2 pl-5">
          <li>
            <strong className="font-medium text-slate-800 dark:text-slate-100">
              Show an unplayed gameweek.
            </strong>{" "}
            FPL keeps a squad private until that gameweek&apos;s deadline passes,
            so a team you edited on Friday morning still reads as last
            week&apos;s until Saturday.
          </li>
          <li>
            <strong className="font-medium text-slate-800 dark:text-slate-100">
              Match an old deadline exactly.
            </strong>{" "}
            Selling prices are worked out against today&apos;s prices. A squad
            rebuilt days after a deadline will differ from what FPL showed then,
            by however much prices moved in between.
          </li>
        </ul>
      </section>
    </div>
  );
}
