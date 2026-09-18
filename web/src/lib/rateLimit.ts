/**
 * A per-key token bucket, in process memory.
 *
 * Why this exists: `entry/{id}/` and `entry/{id}/transfers/` are the only two
 * exceptions to the rule that no user-facing request touches the FPL API
 * (CLAUDE.md invariant 3), and the exception comes with conditions — cache each
 * response for at least an hour, and rate-limit per team id so one person
 * holding down refresh cannot turn into a hundred upstream calls from a shared
 * Vercel IP.
 *
 * **Be honest about what this bounds.** The state lives in one Node process.
 * Serverless means several processes, scaled up and torn down at the platform's
 * discretion, so a caller spread across instances gets a multiple of this limit
 * and a cold start resets it entirely. That is fine for what it is for: it
 * bounds the common case — a bored user, a retry loop, a page that mounts twice
 * — and it does nothing at all against a determined attacker. The fix for that
 * is a shared counter, and CLAUDE.md says not to add Redis without a measured
 * reason. There is no measured reason yet.
 *
 * Keys are hashed before they are stored. The caller passes a team id, which is
 * the user's and identifies them (CLAUDE.md invariant 4); nothing in this module
 * needs to be able to read it back, so it never holds it. Buckets are memory
 * only, expire on their own, and are never logged or persisted.
 */

/** Tokens a bucket holds when full — the burst one key may spend at once. */
export const DEFAULT_CAPACITY = 5;
/** How long a fully spent bucket takes to refill, in milliseconds. */
export const DEFAULT_REFILL_MS = 60_000;
/** Buckets idle longer than this are dropped, so the map cannot grow forever. */
export const IDLE_EVICTION_MS = 10 * 60_000;
/** A hard ceiling on tracked keys, in case eviction cannot keep up. */
export const MAX_TRACKED_KEYS = 10_000;

export interface RateLimitOptions {
  capacity?: number;
  refillMs?: number;
  /** Injectable clock. Tests drive it; production leaves it alone. */
  now?: () => number;
}

export interface RateLimitResult {
  allowed: boolean;
  /** Whole tokens left after this call. */
  remaining: number;
  /** Milliseconds until one token is back. 0 when the call was allowed. */
  retryAfterMs: number;
}

interface Bucket {
  /** Fractional on purpose: the bucket refills continuously, not in steps. */
  tokens: number;
  updatedAt: number;
}

/**
 * 32-bit FNV-1a over the key. Not cryptographic and does not need to be — it
 * only has to be stable within a process and not be the team id.
 */
function hashKey(key: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < key.length; i += 1) {
    hash ^= key.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return (hash >>> 0).toString(36);
}

export class RateLimiter {
  private readonly buckets = new Map<string, Bucket>();
  private readonly capacity: number;
  private readonly refillMs: number;
  private readonly now: () => number;

  constructor(options: RateLimitOptions = {}) {
    this.capacity = Math.max(1, Math.trunc(options.capacity ?? DEFAULT_CAPACITY));
    this.refillMs = Math.max(1, Math.trunc(options.refillMs ?? DEFAULT_REFILL_MS));
    this.now = options.now ?? (() => Date.now());
  }

  /** Tokens per millisecond. Kept as a rate so a partial wait earns a partial token. */
  private get refillPerMs(): number {
    return this.capacity / this.refillMs;
  }

  /** Spend a token for `key`, or report how long until one is available. */
  take(key: string): RateLimitResult {
    const now = this.now();
    const id = hashKey(key);

    this.evictIdle(now);

    const bucket = this.buckets.get(id) ?? { tokens: this.capacity, updatedAt: now };

    const elapsed = Math.max(0, now - bucket.updatedAt);
    bucket.tokens = Math.min(this.capacity, bucket.tokens + elapsed * this.refillPerMs);
    bucket.updatedAt = now;

    if (bucket.tokens >= 1) {
      bucket.tokens -= 1;
      this.buckets.set(id, bucket);
      return {
        allowed: true,
        remaining: Math.floor(bucket.tokens),
        retryAfterMs: 0,
      };
    }

    this.buckets.set(id, bucket);
    return {
      allowed: false,
      remaining: 0,
      retryAfterMs: Math.ceil((1 - bucket.tokens) / this.refillPerMs),
    };
  }

  /** Test seam: forget everything. Never called on a request path. */
  reset(): void {
    this.buckets.clear();
  }

  /** Test seam: how many keys are tracked right now. */
  get size(): number {
    return this.buckets.size;
  }

  private evictIdle(now: number): void {
    // A full bucket nobody has touched in a while is indistinguishable from a
    // key that was never seen, so dropping it changes no decision.
    for (const [id, bucket] of this.buckets) {
      if (now - bucket.updatedAt > IDLE_EVICTION_MS) this.buckets.delete(id);
    }
    // Still over the ceiling: drop the oldest entries. Map iterates in insertion
    // order, which is close enough to oldest-first for a safety valve.
    while (this.buckets.size >= MAX_TRACKED_KEYS) {
      const oldest = this.buckets.keys().next();
      if (oldest.done) break;
      this.buckets.delete(oldest.value);
    }
  }
}

/**
 * The limiter the squad import route shares.
 *
 * Module scope, so it survives between requests handled by the same warm
 * instance and is rebuilt on a cold start. Five imports in a burst, refilling to
 * five over a minute: enough that a real person never meets it, little enough
 * that a refresh loop does.
 */
export const squadImportLimiter = new RateLimiter();
