/**
 * /players — the player table.
 *
 * Server component: it queries Postgres (or the seed) and hands plain rows to
 * the one client component that needs interactivity. No 'use client' here, and
 * nothing from this file ships to the browser except its rendered HTML.
 */

import type { Metadata } from "next";
import Link from "next/link";

import PlayerTable from "@/components/PlayerTable";
import { getPlayerData } from "@/lib/db";
import { formatDeadline } from "@/lib/format";

export const metadata: Metadata = {
  title: "Player table",
  description:
    "Every Fantasy Premier League player, searchable and sortable by price, form, points and ownership.",
};

// Reference data is rebuilt by cron every 30 minutes; there is no reason for a
// visitor to trigger a fresh query more often than that.
export const revalidate = 900;

export default async function PlayersPage() {
  const { players, currentGameweek, source } = await getPlayerData();

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-10 sm:px-6 lg:px-8">
      <header className="mb-8 flex flex-col gap-3">
        <Link
          href="/"
          className="text-xs font-medium text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
        >
          ← Back to overview
        </Link>
        <h1 className="text-3xl font-semibold tracking-tight text-slate-900 dark:text-slate-50 sm:text-4xl">
          Player table
        </h1>
        <p className="max-w-2xl text-sm leading-6 text-slate-600 dark:text-slate-300">
          Every player in the game, with the numbers that actually move a squad
          decision. Click a column heading to sort; search by player or club.
          Prices are shown to the tenth of a million, exactly as the game stores
          them.
        </p>

        <dl className="flex flex-wrap gap-x-8 gap-y-2 text-xs text-slate-500 dark:text-slate-400">
          <div className="flex gap-2">
            <dt className="font-medium text-slate-700 dark:text-slate-200">Players</dt>
            <dd className="tabular-nums">{players.length}</dd>
          </div>
          {currentGameweek && (
            <>
              <div className="flex gap-2">
                <dt className="font-medium text-slate-700 dark:text-slate-200">
                  Gameweek
                </dt>
                <dd className="tabular-nums">{currentGameweek.gw}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="font-medium text-slate-700 dark:text-slate-200">
                  Deadline
                </dt>
                <dd className="tabular-nums">
                  {formatDeadline(currentGameweek.deadline_time)}
                </dd>
              </div>
            </>
          )}
          <div className="flex gap-2">
            <dt className="font-medium text-slate-700 dark:text-slate-200">Source</dt>
            <dd>{source === "database" ? "Live database" : "Local seed sample"}</dd>
          </div>
        </dl>

        {source === "seed" && (
          <p className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200">
            No database connection is configured, so this page is showing the
            checked-in sample of {players.length} players. Set{" "}
            <code className="font-mono">DATABASE_URL</code> to read the real
            table.
          </p>
        )}
      </header>

      <PlayerTable players={players} />
    </div>
  );
}
