import type { Metadata } from "next";
import Link from "next/link";

import { getCurrentGameweek, dataSource } from "@/lib/db";
import { formatDeadline } from "@/lib/format";

export const metadata: Metadata = {
  title: "FPL Squad Lab — rate and optimise your Fantasy squad",
  description:
    "An independent Fantasy Premier League squad rater and optimiser. Browse every player, then build a squad and see what the solver would change.",
};

export const revalidate = 900;

export default async function Home() {
  const gameweek = await getCurrentGameweek();
  const source = dataSource();

  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-16 sm:px-6 lg:px-8">
      <section className="flex flex-col gap-6">
        <p className="text-xs font-medium uppercase tracking-[0.2em] text-slate-500 dark:text-slate-400">
          Independent · Open data · No accounts
        </p>
        <h1 className="text-4xl font-semibold tracking-tight text-slate-900 dark:text-slate-50 sm:text-5xl">
          Squad Lab
        </h1>
        <p className="max-w-2xl text-lg leading-8 text-slate-600 dark:text-slate-300">
          A Fantasy Premier League squad rater and optimiser. Every player&apos;s
          price, form, points and ownership in one dense table, and a solver that
          tells you what a transfer is actually worth — including when the answer
          is &ldquo;hold&rdquo;.
        </p>

        {gameweek && (
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Next deadline —{" "}
            <span className="font-medium text-slate-700 tabular-nums dark:text-slate-200">
              {gameweek.name}, {formatDeadline(gameweek.deadline_time)}
            </span>
          </p>
        )}

        <div className="flex flex-wrap gap-3">
          <Link
            href="/players"
            className="rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
          >
            Browse the player table
          </Link>
          <Link
            href="/squad"
            className="rounded-lg border border-slate-300 px-4 py-2.5 text-sm font-medium text-slate-700 transition-colors hover:border-slate-500 hover:text-slate-900 dark:border-slate-700 dark:text-slate-200 dark:hover:border-slate-500 dark:hover:text-white"
          >
            Build a squad
          </Link>
        </div>
      </section>

      <section className="mt-16 grid gap-6 sm:grid-cols-3">
        <Card
          title="Read the pool"
          body="Every player, sortable by price, form, total points and ownership, with injury and suspension status spelled out rather than colour-coded."
          href="/players"
          linkLabel="Player table"
        />
        <Card
          title="Build a squad"
          body="Pick fifteen players against a £100.0m budget. Your squad lives in your browser and in the URL — nothing is stored here, because there are no accounts."
          href="/squad"
          linkLabel="Squad builder"
        />
        <Card
          title="See the reasoning"
          body="Recommendations come with the expected-points breakdown behind them. The explanation is the point; a verdict you cannot check is not worth much."
        />
      </section>

      <section className="mt-16 rounded-xl border border-slate-200 p-6 dark:border-slate-800">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-700 dark:text-slate-200">
          How it works
        </h2>
        <ul className="mt-4 space-y-3 text-sm leading-6 text-slate-600 dark:text-slate-300">
          <li>
            <span className="font-medium text-slate-900 dark:text-slate-100">
              Data is pulled on a schedule,
            </span>{" "}
            not while you wait. A cron job syncs prices, form and injury news into
            a database; these pages only ever read it.
          </li>
          <li>
            <span className="font-medium text-slate-900 dark:text-slate-100">
              Prices are exact.
            </span>{" "}
            Money is held as tenths of a million throughout, so a squad value is
            never a rounding artefact.
          </li>
          <li>
            <span className="font-medium text-slate-900 dark:text-slate-100">
              Nothing about you is stored.
            </span>{" "}
            No sign-in, no profile, no analytics on your squad. Recommendations
            are logged anonymously so the model can be checked against reality.
          </li>
        </ul>
        {source === "seed" && (
          <p className="mt-4 text-xs text-amber-700 dark:text-amber-400">
            Running on the local sample dataset — no database is configured for
            this environment.
          </p>
        )}
      </section>
    </div>
  );
}

function Card({
  title,
  body,
  href,
  linkLabel,
}: {
  title: string;
  body: string;
  href?: string;
  linkLabel?: string;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-xl border border-slate-200 p-5 dark:border-slate-800">
      <h2 className="text-base font-semibold text-slate-900 dark:text-slate-50">
        {title}
      </h2>
      <p className="flex-1 text-sm leading-6 text-slate-600 dark:text-slate-300">
        {body}
      </p>
      {href && linkLabel && (
        <Link
          href={href}
          className="text-sm font-medium text-slate-900 underline decoration-slate-300 underline-offset-4 hover:decoration-slate-900 dark:text-slate-100 dark:decoration-slate-600 dark:hover:decoration-slate-100"
        >
          {linkLabel} →
        </Link>
      )}
    </div>
  );
}
