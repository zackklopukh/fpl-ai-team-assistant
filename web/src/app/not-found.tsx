/**
 * 404.
 *
 * A 404 here is usually one of three things, and the copy addresses all three
 * rather than apologising in general:
 *
 *   - a player URL from a previous season (element ids are reassigned every
 *     summer, so last year's link points at nobody, or at somebody else),
 *   - a shared squad link that was truncated by a chat client,
 *   - a guessed or stale path.
 *
 * Server component on purpose: nothing here is interactive, so it costs no JS.
 */

import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Page not found",
  robots: { index: false, follow: true },
};

const destinations = [
  {
    href: "/players",
    label: "All players",
    blurb: "Every player with prices, form and expected goals.",
  },
  {
    href: "/squad",
    label: "My squad",
    blurb: "Build or reopen a squad and get it rated.",
  },
  {
    href: "/fixtures",
    label: "Fixtures",
    blurb: "Upcoming fixture difficulty, including doubles and blanks.",
  },
];

export default function NotFound() {
  return (
    <div className="mx-auto w-full max-w-2xl px-4 py-16 sm:px-6 lg:px-8">
      <p className="text-xs font-medium uppercase tracking-widest text-slate-500 dark:text-slate-400">
        404
      </p>
      <h1 className="mt-3 text-2xl font-semibold tracking-tight text-slate-900 sm:text-3xl dark:text-slate-50">
        There&rsquo;s nothing at this address
      </h1>
      <p className="mt-4 text-sm leading-6 text-slate-600 dark:text-slate-300">
        If you followed a link to a player, it may be from a previous season —
        Fantasy Premier League reassigns player ids every summer, so old links
        stop resolving. If someone shared a squad with you, check the address
        wasn&rsquo;t cut short: a squad link is long and some chat apps trim it.
      </p>

      <ul className="mt-8 divide-y divide-slate-200 border-y border-slate-200 dark:divide-slate-800 dark:border-slate-800">
        {destinations.map((d) => (
          <li key={d.href}>
            <Link
              href={d.href}
              className="group flex flex-col gap-1 py-4 transition-colors"
            >
              <span className="text-sm font-medium text-slate-900 group-hover:underline dark:text-slate-50">
                {d.label}
              </span>
              <span className="text-sm text-slate-600 dark:text-slate-400">
                {d.blurb}
              </span>
            </Link>
          </li>
        ))}
      </ul>

      <p className="mt-8 text-sm text-slate-600 dark:text-slate-300">
        Anything you had built is still in this browser.{" "}
        <Link
          href="/squad"
          className="underline underline-offset-4 hover:text-slate-900 dark:hover:text-white"
        >
          Reopen your squad
        </Link>
        .
      </p>
    </div>
  );
}
