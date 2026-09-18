"use client";

/**
 * The route error boundary.
 *
 * Three rules it exists to satisfy.
 *
 * **Nothing from the error reaches the page.** `error.message` is the server's
 * message in a server-component failure, and it can carry a connection string,
 * a query, or an upstream response. Next already replaces it with a generic
 * string in production, but this component does not rely on that — it renders
 * the `digest` and nothing else. The digest is the one thing worth showing: it
 * is the key that ties what the visitor saw to the report.
 *
 * **It is reported anyway.** A visitor seeing nothing useful is only acceptable
 * because someone else sees everything. Without a Sentry DSN that degrades to a
 * console error, which is the local-development case.
 *
 * **It must not read as data loss.** The squad lives in `localStorage` and in
 * the URL, so it is still there — but the person looking at a crash does not
 * know that, and the obvious fear is that their 15 picks are gone. Say so
 * plainly and give them a way back that keeps the URL intact.
 */

import Link from "next/link";
import { useEffect } from "react";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const Sentry = await import("@sentry/nextjs");
        // No client is initialised without a DSN, so this is a no-op then.
        if (!cancelled) {
          Sentry.captureException(error, {
            tags: { boundary: "app/error" },
            // The digest is the only thing that links this to the server log.
            extra: { digest: error.digest ?? "none" },
          });
        }
      } catch {
        // The reporter failing must never replace the error the user hit.
      }
      if (process.env.NODE_ENV !== "production") {
        console.error("[error boundary]", error);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [error]);

  return (
    <div className="mx-auto w-full max-w-2xl px-4 py-16 sm:px-6 lg:px-8">
      <p className="text-xs font-medium uppercase tracking-widest text-slate-500 dark:text-slate-400">
        Something broke
      </p>
      <h1 className="mt-3 text-2xl font-semibold tracking-tight text-slate-900 sm:text-3xl dark:text-slate-50">
        This page didn&rsquo;t load
      </h1>
      <p className="mt-4 text-sm leading-6 text-slate-600 dark:text-slate-300">
        An error on our side stopped this page rendering. It has been reported
        automatically — you don&rsquo;t need to do anything.
      </p>

      <div className="mt-6 rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm leading-6 text-slate-700 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200">
        <p className="font-medium text-slate-900 dark:text-slate-50">
          Your squad is safe.
        </p>
        <p className="mt-1">
          Squads are stored in this browser and in the page address — never on a
          server. Nothing here has been lost, and the link you were on still
          works.
        </p>
      </div>

      <div className="mt-8 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={reset}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
        >
          Try again
        </button>
        <Link
          href="/squad"
          className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition-colors hover:border-slate-400 hover:text-slate-900 dark:border-slate-700 dark:text-slate-200 dark:hover:border-slate-500 dark:hover:text-white"
        >
          Back to my squad
        </Link>
        <Link
          href="/players"
          className="text-sm text-slate-600 underline underline-offset-4 transition-colors hover:text-slate-900 dark:text-slate-300 dark:hover:text-white"
        >
          Browse players
        </Link>
      </div>

      {error.digest ? (
        <p className="mt-8 font-mono text-xs text-slate-400 dark:text-slate-500">
          Reference: {error.digest}
        </p>
      ) : null}
    </div>
  );
}
