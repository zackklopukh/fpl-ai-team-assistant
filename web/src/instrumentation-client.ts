/**
 * Browser instrumentation. Next loads this module into the client bundle before
 * hydration; it is the client half of `instrumentation.ts`.
 *
 * With no `NEXT_PUBLIC_SENTRY_DSN` this does nothing: the SDK is behind a
 * dynamic import, so it is not fetched, not parsed and not run, and the page
 * ships no extra bytes for it. That is the current state of the project and the
 * state a new contributor starts in, and it must stay comfortable.
 *
 * The scrubber below duplicates the one in `instrumentation.ts` on purpose.
 * That module is server-only, and importing it here would drag the server SDK
 * into the browser bundle. Two small copies beat one shared file that has to be
 * safe in both runtimes — but they must be changed together.
 *
 * What must never reach Sentry, from the browser especially: the squad (which
 * lives in `localStorage` and in the URL), and any FPL team ID.
 */

import {
  deployEnvironment,
  publicSentryDsn,
  releaseId,
  sentryClientSampleRate,
  sentryTracesSampleRate,
} from "@/lib/env";

const SQUAD_TOKEN = /\b[A-Za-z0-9_-]{16,}\b/g;
const TEAM_ID = /\b\d{5,}\b/g;
const SQUAD_PARAM = /([?&#](?:squad|s|team|entry|teamId)=)[^&#\s]*/gi;

function redactText(value: string): string {
  return value
    .replace(SQUAD_PARAM, "$1[redacted]")
    .replace(SQUAD_TOKEN, "[redacted]")
    .replace(TEAM_ID, "[id]");
}

function redactUrl(value: string): string {
  try {
    const url = new URL(value, "http://local");
    const path = url.pathname
      .split("/")
      .map((seg) => (/^\d{4,}$/.test(seg) ? "[id]" : seg))
      .join("/");
    return `${url.origin === "http://local" ? "" : url.origin}${path}`;
  } catch {
    return redactText(value);
  }
}

type LooseEvent = {
  request?: {
    url?: string;
    headers?: Record<string, string>;
    cookies?: unknown;
    query_string?: unknown;
    data?: unknown;
  };
  user?: unknown;
  message?: string;
  transaction?: string;
  exception?: { values?: Array<{ value?: string }> };
  breadcrumbs?: Array<{
    category?: string;
    message?: string;
    data?: Record<string, unknown>;
  }>;
  extra?: Record<string, unknown>;
};

function scrub<T>(event: T): T {
  const e = event as LooseEvent;

  if (e.request) {
    // The browser's own URL is the single most likely place a squad leaks:
    // the shareable squad string lives in the fragment.
    if (e.request.url) e.request.url = redactUrl(e.request.url);
    delete e.request.cookies;
    delete e.request.query_string;
    delete e.request.data;
    const ua = e.request.headers?.["user-agent"];
    e.request.headers = ua ? { "user-agent": ua } : {};
  }

  delete e.user;

  if (e.message) e.message = redactText(e.message);
  if (e.transaction) e.transaction = redactUrl(e.transaction);

  for (const value of e.exception?.values ?? []) {
    if (value.value) value.value = redactText(value.value);
  }

  if (e.breadcrumbs) {
    e.breadcrumbs = e.breadcrumbs.map((crumb) => {
      const next = { ...crumb };
      if (next.message) next.message = redactText(next.message);
      if (
        next.category === "console" ||
        next.category === "ui.input" ||
        next.category === "ui.click"
      ) {
        delete next.data;
      } else if (next.data) {
        const data: Record<string, unknown> = {};
        for (const [key, val] of Object.entries(next.data)) {
          data[key] = typeof val === "string" ? redactUrl(val) : val;
        }
        next.data = data;
      }
      return next;
    });
  }

  if (e.extra) {
    for (const [key, val] of Object.entries(e.extra)) {
      if (typeof val === "string") e.extra[key] = redactText(val);
    }
  }

  return event;
}

if (publicSentryDsn) {
  void import("@sentry/nextjs").then((Sentry) => {
    Sentry.init({
      dsn: publicSentryDsn,
      environment: deployEnvironment,
      release: releaseId,
      sendDefaultPii: false,
      sampleRate: sentryClientSampleRate,
      tracesSampleRate: sentryTracesSampleRate,
      maxBreadcrumbs: 20,
      // Session Replay would record the squad on screen. Never enable it here.
      integrations: (defaults) =>
        defaults.filter(
          (integration) =>
            !/Replay|BrowserProfiling/i.test(integration.name ?? ""),
        ),
      // Browser noise that is never actionable and would eat the free tier.
      ignoreErrors: [
        "ResizeObserver loop limit exceeded",
        "ResizeObserver loop completed with undelivered notifications",
        "Non-Error promise rejection captured",
        /^Failed to fetch$/,
        /^NetworkError/,
        /extension\//i,
      ],
      denyUrls: [/^chrome-extension:\/\//, /^moz-extension:\/\//],
      beforeSend: scrub,
      beforeSendTransaction: scrub,
      beforeBreadcrumb(crumb) {
        if (typeof crumb.data?.url === "string") {
          crumb.data.url = redactUrl(crumb.data.url);
        }
        return crumb;
      },
    });
  });
}

/**
 * Next calls this on every client-side route change. Exported unconditionally
 * because Next reads the export at build time; it resolves to a no-op when the
 * SDK was never initialised.
 */
export async function onRouterTransitionStart(
  ...args: unknown[]
): Promise<void> {
  if (!publicSentryDsn) return;
  const Sentry = await import("@sentry/nextjs");
  const hook = (Sentry as unknown as Record<string, unknown>)
    .captureRouterTransitionStart;
  if (typeof hook === "function") {
    (hook as (...a: unknown[]) => void)(...args);
  }
}
