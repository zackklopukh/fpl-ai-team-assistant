/**
 * Server and edge instrumentation.
 *
 * Two jobs, in this order:
 *
 *   1. Validate the environment once, at startup, where a deployer sees it.
 *   2. Start Sentry — but only if a DSN exists. With no DSN this file does
 *      nothing at all: the SDK is never even imported, no network call is made,
 *      and the app behaves exactly as it does today. Running this project
 *      locally must not require a Sentry account, and it does not.
 *
 * ARCHITECTURE.md picks Sentry for the failures you will not reproduce locally
 * — screenshot parses, one browser's canvas behaviour, an upstream 403 at
 * 02:00. Everything it sends is scrubbed first: this project holds no accounts
 * and must not start leaking user state through its error reporter. Squad
 * contents live in `localStorage` and the URL, and an FPL team ID is a real
 * identifier, so neither may ever reach Sentry.
 *
 * The client half lives in `instrumentation-client.ts`, which Next loads into
 * the browser bundle. The scrubber is duplicated there rather than shared,
 * because this module is server-only and importing it into the client bundle
 * would pull the server SDK in with it.
 */

import { assertEnv, isSentryEnabled } from "@/lib/env";

// ---------------------------------------------------------------------------
// Scrubbing
// ---------------------------------------------------------------------------

/** A base64-ish squad token from the URL encoding, or any long opaque blob. */
const SQUAD_TOKEN = /\b[A-Za-z0-9_-]{16,}\b/g;
/** FPL entry (team) IDs are 5-8 digits. Element ids are 1-3 and stay readable. */
const TEAM_ID = /\b\d{5,}\b/g;
const SQUAD_PARAM = /([?&#](?:squad|s|team|entry|teamId)=)[^&#\s]*/gi;

function redactText(value: string): string {
  return value
    .replace(SQUAD_PARAM, "$1[redacted]")
    .replace(SQUAD_TOKEN, "[redacted]")
    .replace(TEAM_ID, "[id]");
}

/**
 * Keep origin and shape, drop content. `/api/squad/1234567` tells us the route
 * failed; the number tells us who. We want the first and not the second.
 */
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
  server_name?: unknown;
  message?: string;
  transaction?: string;
  exception?: { values?: Array<{ value?: string }> };
  breadcrumbs?: Array<{
    category?: string;
    message?: string;
    data?: Record<string, unknown>;
  }>;
  extra?: Record<string, unknown>;
  contexts?: Record<string, unknown>;
};

/**
 * Runs on every event, server and client. Deliberately an allow-list for
 * headers and a shredder for everything else — a new Sentry integration that
 * starts attaching something should arrive stripped, not attached.
 */
function scrub<T>(event: T): T {
  const e = event as LooseEvent;

  if (e.request) {
    if (e.request.url) e.request.url = redactUrl(e.request.url);
    delete e.request.cookies;
    delete e.request.query_string;
    delete e.request.data;
    // A referer carries the previous squad URL, so it does not survive either.
    const ua = e.request.headers?.["user-agent"];
    e.request.headers = ua ? { "user-agent": ua } : {};
  }

  // No accounts means no user, and Sentry's inferred IP is still an identifier.
  delete e.user;
  delete e.server_name;

  if (e.message) e.message = redactText(e.message);
  if (e.transaction) e.transaction = redactUrl(e.transaction);

  for (const value of e.exception?.values ?? []) {
    if (value.value) value.value = redactText(value.value);
  }

  if (e.breadcrumbs) {
    e.breadcrumbs = e.breadcrumbs.map((crumb) => {
      const next = { ...crumb };
      if (next.message) next.message = redactText(next.message);
      // Console and XHR breadcrumbs are where squad objects would ride along.
      if (next.category === "console" || next.category === "ui.input") {
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

  // Nothing attaches `extra` today; if something starts, it arrives scrubbed.
  if (e.extra) {
    for (const [key, val] of Object.entries(e.extra)) {
      if (typeof val === "string") e.extra[key] = redactText(val);
    }
  }

  return event;
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

export async function register(): Promise<void> {
  // Throws on a production deployment missing something genuinely required;
  // warns everywhere else, including during `next build`.
  assertEnv();

  if (!isSentryEnabled) return;

  const [Sentry, env] = await Promise.all([
    import("@sentry/nextjs"),
    import("@/lib/env"),
  ]);

  Sentry.init({
    dsn: env.sentryDsn,
    environment: env.deployEnvironment,
    release: env.releaseId,
    // Never opt in to PII. This project has none and should acquire none.
    sendDefaultPii: false,
    sampleRate: env.sentryServerSampleRate,
    tracesSampleRate: env.sentryTracesSampleRate,
    // The free tier is ~5k events/month and traffic is concentrated in the two
    // hours before a deadline, so a loop must not be able to spend it all.
    maxBreadcrumbs: 20,
    beforeSend: scrub,
    beforeSendTransaction: scrub,
    beforeBreadcrumb(crumb) {
      // A fetch to the FPL API carries a team ID in its path.
      if (crumb.data?.url && typeof crumb.data.url === "string") {
        crumb.data.url = redactUrl(crumb.data.url);
      }
      return crumb;
    },
  });
}

/**
 * Next calls this for every uncaught error in a server component, route handler
 * or middleware. Without a DSN it is a no-op, and the dynamic import means the
 * SDK is not loaded at all in that case.
 */
export async function onRequestError(
  ...args: unknown[]
): Promise<void> {
  if (!isSentryEnabled) return;
  const Sentry = await import("@sentry/nextjs");
  const capture = Sentry.captureRequestError as unknown as (
    ...a: unknown[]
  ) => void;
  capture(...args);
}
