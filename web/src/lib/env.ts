/**
 * The one typed place environment variables are read.
 *
 * Two things this file exists to get right.
 *
 * **"Unset" and "set to a placeholder" are the same thing.** `.env.example` (and
 * therefore most people's `.env` on day one) carries `REPLACE_WITH_PASSWORD`,
 * `PROJECT_REF` and an empty anon key. A connection string still holding one of
 * those is not a connection string, and treating it as one produces a confusing
 * auth error instead of an honest "not configured yet". `lib/db.ts` already made
 * this call — `isPlaceholder` here is the same predicate, deliberately, and this
 * file does not introduce a second notion of configured.
 *
 * **Required and optional are not the same failure.** A genuinely required
 * variable missing in production is a deploy that should not have gone out, so
 * it throws at startup where the deployer sees it. An optional one missing is
 * the normal state of a fresh clone — no Sentry account, no Supabase project,
 * no Modal deployment — and it must degrade quietly. Nobody should need an
 * account with anything to run this locally.
 *
 * Client-safety note: every read below is a *static* `process.env.FOO`
 * expression. Next only inlines `NEXT_PUBLIC_*` into the client bundle, and it
 * can only inline what it can see literally, so dynamic `process.env[name]`
 * would silently break in the browser. Do not refactor these into a loop.
 */

// ---------------------------------------------------------------------------
// Raw reads
// ---------------------------------------------------------------------------

/**
 * The placeholders shipped in `.env.example` and `.env`. Kept character-for-
 * character in step with the same check in `lib/db.ts`.
 */
function isPlaceholder(value: string): boolean {
  return /PASSWORD|PROJECT_REF|REPLACE_WITH|YOURNAME|placeholder/i.test(value);
}

/** Trimmed value, or undefined when unset, empty, or still a placeholder. */
function clean(value: string | undefined): string | undefined {
  const trimmed = value?.trim();
  if (!trimmed) return undefined;
  if (isPlaceholder(trimmed)) return undefined;
  return trimmed;
}

const raw = {
  DATABASE_URL: clean(process.env.DATABASE_URL),
  NEXT_PUBLIC_SUPABASE_URL: clean(process.env.NEXT_PUBLIC_SUPABASE_URL),
  NEXT_PUBLIC_SUPABASE_ANON_KEY: clean(process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY),
  OPTIMIZER_URL: clean(process.env.OPTIMIZER_URL),
  FPL_USER_AGENT: clean(process.env.FPL_USER_AGENT),
  FPL_SEASON: clean(process.env.FPL_SEASON),
  SENTRY_DSN: clean(process.env.SENTRY_DSN),
  NEXT_PUBLIC_SENTRY_DSN: clean(process.env.NEXT_PUBLIC_SENTRY_DSN),
  SENTRY_TRACES_SAMPLE_RATE: clean(process.env.SENTRY_TRACES_SAMPLE_RATE),
  SENTRY_ERROR_SAMPLE_RATE: clean(process.env.SENTRY_ERROR_SAMPLE_RATE),
  NEXT_PUBLIC_SITE_URL: clean(process.env.NEXT_PUBLIC_SITE_URL),
  VERCEL_URL: clean(process.env.VERCEL_URL),
  VERCEL_ENV: clean(process.env.VERCEL_ENV),
  VERCEL_GIT_COMMIT_SHA: clean(process.env.VERCEL_GIT_COMMIT_SHA),
  NEXT_PUBLIC_VERCEL_ENV: clean(process.env.NEXT_PUBLIC_VERCEL_ENV),
  NEXT_PUBLIC_VERCEL_URL: clean(process.env.NEXT_PUBLIC_VERCEL_URL),
  ALLOW_SEED_IN_PRODUCTION: clean(process.env.ALLOW_SEED_IN_PRODUCTION),
} as const;

// ---------------------------------------------------------------------------
// Where we are
// ---------------------------------------------------------------------------

/**
 * `next build` runs this module (instrumentation and static generation both
 * import it). A build must never fail for a missing runtime secret: CI builds
 * with no database on purpose, and so does a fresh clone.
 */
const isBuildPhase = process.env.NEXT_PHASE === "phase-production-build";

/** Vercel sets VERCEL_ENV to production | preview | development. */
export const deployEnvironment: "production" | "preview" | "development" =
  raw.VERCEL_ENV === "production"
    ? "production"
    : raw.VERCEL_ENV === "preview"
      ? "preview"
      : raw.NEXT_PUBLIC_VERCEL_ENV === "production"
        ? "production"
        : raw.NEXT_PUBLIC_VERCEL_ENV === "preview"
          ? "preview"
          : "development";

/** Serving real traffic on a production deployment — not a build, not a preview. */
const isProductionRuntime = deployEnvironment === "production" && !isBuildPhase;

export const releaseId = raw.VERCEL_GIT_COMMIT_SHA?.slice(0, 12);

// ---------------------------------------------------------------------------
// Database
// ---------------------------------------------------------------------------

/**
 * The Postgres connection string, or undefined when unconfigured. Undefined is
 * a supported state: `lib/db.ts` serves the checked-in seed instead.
 */
export const databaseUrl = ((): string | undefined => {
  const url = raw.DATABASE_URL;
  if (!url) return undefined;
  if (!url.startsWith("postgres://") && !url.startsWith("postgresql://")) {
    return undefined;
  }
  return url;
})();

export const isDatabaseConfigured = databaseUrl !== undefined;

/** Season string used as the first half of every natural key. */
export const fplSeason = raw.FPL_SEASON;

/** Sent on the two per-manager FPL calls. Has a working default in `lib/fpl.ts`. */
export const fplUserAgent = raw.FPL_USER_AGENT;

/** Base URL of the Modal-hosted optimizer. Undefined means "no optimizer yet". */
export const optimizerUrl = raw.OPTIMIZER_URL?.replace(/\/+$/, "");

export const supabaseUrl = raw.NEXT_PUBLIC_SUPABASE_URL;
export const supabaseAnonKey = raw.NEXT_PUBLIC_SUPABASE_ANON_KEY;

// ---------------------------------------------------------------------------
// Canonical site URL
// ---------------------------------------------------------------------------

/**
 * Used by `robots.ts`, `sitemap.ts` and Open Graph metadata. Preview
 * deployments get their own generated hostname, which is correct — a preview
 * should not advertise the production canonical.
 */
export const siteUrl = ((): string => {
  const explicit = raw.NEXT_PUBLIC_SITE_URL;
  if (explicit) return explicit.replace(/\/+$/, "");
  const vercel = raw.VERCEL_URL ?? raw.NEXT_PUBLIC_VERCEL_URL;
  if (vercel) return `https://${vercel.replace(/\/+$/, "")}`;
  return "http://localhost:3000";
})();

/** Only a production deployment with a real hostname should be indexed. */
export const isIndexable =
  deployEnvironment === "production" && !siteUrl.startsWith("http://localhost");

// ---------------------------------------------------------------------------
// Sentry
// ---------------------------------------------------------------------------

/**
 * Absent DSN is the normal, supported state — instrumentation does nothing at
 * all and the app behaves identically. `SENTRY_DSN` covers the server;
 * `NEXT_PUBLIC_SENTRY_DSN` is the only one the browser can see, and a DSN is
 * public by design (it is a write-only ingest key).
 */
export const sentryDsn = raw.SENTRY_DSN ?? raw.NEXT_PUBLIC_SENTRY_DSN;
export const publicSentryDsn = raw.NEXT_PUBLIC_SENTRY_DSN;
export const isSentryEnabled = Boolean(sentryDsn);

function rate(value: string | undefined, fallback: number): number {
  if (value === undefined) return fallback;
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0 || n > 1) return fallback;
  return n;
}

/**
 * Free tier is roughly 5k events and 10k performance units a month, and this
 * app's traffic is not evenly spread — essentially all of it arrives in the two
 * hours before a deadline (ARCHITECTURE.md, "Deadline concentration"). One bad
 * deploy on a Friday evening can spend a month's quota in twenty minutes, so
 * the defaults sample rather than send everything. Raise them deliberately, per
 * environment, when you are chasing something.
 */
export const sentryTracesSampleRate = rate(
  raw.SENTRY_TRACES_SAMPLE_RATE,
  deployEnvironment === "production" ? 0.02 : 0,
);

/** Server errors are rarer and more actionable than browser noise, so 1.0. */
export const sentryServerSampleRate = rate(raw.SENTRY_ERROR_SAMPLE_RATE, 1);

/** Browser errors include extension and network noise; a quarter is plenty. */
export const sentryClientSampleRate = rate(
  raw.SENTRY_ERROR_SAMPLE_RATE,
  deployEnvironment === "production" ? 0.25 : 1,
);

// ---------------------------------------------------------------------------
// Startup validation
// ---------------------------------------------------------------------------

type Check = {
  name: string;
  ok: boolean;
  /** What breaks without it — shown in both the throw and the warning. */
  consequence: string;
};

/**
 * Required means: a production deployment without it is serving something
 * wrong, quietly. `DATABASE_URL` is the only one that qualifies. Without it the
 * site does not crash — it serves the checked-in seed, which is *worse* than
 * crashing in production because it looks like a working site with stale
 * September prices. That is exactly the failure this check exists to make loud.
 */
const REQUIRED_IN_PRODUCTION: Check[] = [
  {
    name: "DATABASE_URL",
    ok: isDatabaseConfigured,
    consequence:
      "the site silently serves the checked-in seed snapshot instead of live prices",
  },
];

/** Missing these is fine. They cost a feature, not correctness. */
const OPTIONAL: Check[] = [
  {
    name: "OPTIMIZER_URL",
    ok: optimizerUrl !== undefined,
    consequence: "squad optimisation is unavailable; the rest of the site works",
  },
  {
    name: "SENTRY_DSN / NEXT_PUBLIC_SENTRY_DSN",
    ok: isSentryEnabled,
    consequence: "errors are logged to the console only, never reported",
  },
  {
    name: "NEXT_PUBLIC_SITE_URL",
    ok: raw.NEXT_PUBLIC_SITE_URL !== undefined,
    consequence: `canonical URLs and share images fall back to ${siteUrl}`,
  },
  {
    name: "FPL_SEASON",
    ok: fplSeason !== undefined,
    consequence: "the season falls back to the one baked into the seed file",
  },
  {
    name: "FPL_USER_AGENT",
    ok: fplUserAgent !== undefined,
    consequence:
      "team-ID import identifies itself with a default UA and is likelier to be blocked",
  },
];

let validated = false;

/**
 * Called once from `instrumentation.ts`.
 *
 * Throws only when a production deployment is actually about to serve traffic.
 * During `next build`, in development, and on preview deployments it degrades
 * to a one-time warning — a build with no credentials must keep working,
 * because that is what CI does and what a new contributor has.
 */
export function assertEnv(): void {
  if (validated) return;
  validated = true;

  const missingRequired = REQUIRED_IN_PRODUCTION.filter((c) => !c.ok);
  const missingOptional = OPTIONAL.filter((c) => !c.ok);

  if (missingRequired.length > 0) {
    const detail = missingRequired
      .map((c) => `  - ${c.name}: ${c.consequence}`)
      .join("\n");

    // The escape hatch. Refusing to boot is right for a first deploy that was
    // never configured; it is wrong at 23:00 on a Friday when a rotated
    // Supabase password takes the whole site down instead of degrading to the
    // seed. Setting ALLOW_SEED_IN_PRODUCTION=1 turns the throw into a shout.
    const allowDegraded = raw.ALLOW_SEED_IN_PRODUCTION === "1";

    if (isProductionRuntime && !allowDegraded) {
      throw new Error(
        "Missing required environment variables in production:\n" +
          detail +
          "\n\nSet them in Vercel → Project → Settings → Environment Variables " +
          "(Production), then redeploy. A value still containing PASSWORD, " +
          "PROJECT_REF or REPLACE_WITH counts as unset. See docs/DEPLOYMENT.md.",
      );
    }

    console.warn(
      isProductionRuntime
        ? `[env] PRODUCTION IS RUNNING DEGRADED (ALLOW_SEED_IN_PRODUCTION=1):\n${detail}`
        : `[env] Not configured (fine outside production):\n${detail}`,
    );
  }

  if (missingOptional.length > 0) {
    console.info(
      "[env] Optional and unset:\n" +
        missingOptional.map((c) => `  - ${c.name}: ${c.consequence}`).join("\n"),
    );
  }
}

/** A snapshot for diagnostics. Deliberately carries no secret values. */
export function envSummary(): Record<string, string | boolean> {
  return {
    deployEnvironment,
    siteUrl,
    database: isDatabaseConfigured ? "configured" : "seed fallback",
    optimizer: optimizerUrl ? "configured" : "unavailable",
    sentry: isSentryEnabled ? "enabled" : "disabled",
    indexable: isIndexable,
    release: releaseId ?? "local",
  };
}
