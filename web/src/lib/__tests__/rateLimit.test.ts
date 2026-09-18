/**
 * Tests for the per-team-id token bucket.
 *
 * The clock is injected, so nothing here sleeps: a rate limiter tested with real
 * timers is a slow test that fails on a loaded machine.
 */

import { describe, expect, it } from "vitest";

import {
  DEFAULT_CAPACITY,
  IDLE_EVICTION_MS,
  RateLimiter,
  squadImportLimiter,
} from "../rateLimit";

/** A limiter with a hand-cranked clock. */
function limiterAt(start = 1_000_000, capacity = 3, refillMs = 3000) {
  let now = start;
  const limiter = new RateLimiter({ capacity, refillMs, now: () => now });
  return {
    limiter,
    advance(ms: number) {
      now += ms;
    },
  };
}

describe("RateLimiter", () => {
  it("allows a burst up to capacity and then stops", () => {
    const { limiter } = limiterAt();

    expect(limiter.take("895045").allowed).toBe(true);
    expect(limiter.take("895045").allowed).toBe(true);
    expect(limiter.take("895045").allowed).toBe(true);

    const blocked = limiter.take("895045");
    expect(blocked.allowed).toBe(false);
    expect(blocked.remaining).toBe(0);
    expect(blocked.retryAfterMs).toBeGreaterThan(0);
  });

  it("recovers as the bucket refills", () => {
    const { limiter, advance } = limiterAt();
    for (let i = 0; i < 3; i += 1) limiter.take("895045");

    const blocked = limiter.take("895045");
    expect(blocked.allowed).toBe(false);

    // Not quite enough for a whole token.
    advance(blocked.retryAfterMs - 1);
    expect(limiter.take("895045").allowed).toBe(false);

    // The wait it asked for is a wait that works.
    advance(2);
    expect(limiter.take("895045").allowed).toBe(true);
  });

  it("refills to the cap and no further", () => {
    const { limiter, advance } = limiterAt();
    for (let i = 0; i < 3; i += 1) limiter.take("x");

    advance(60_000); // an age

    expect(limiter.take("x").allowed).toBe(true);
    expect(limiter.take("x").allowed).toBe(true);
    expect(limiter.take("x").allowed).toBe(true);
    expect(limiter.take("x").allowed).toBe(false);
  });

  it("limits each team id separately", () => {
    const { limiter } = limiterAt();
    for (let i = 0; i < 3; i += 1) limiter.take("895045");

    expect(limiter.take("895045").allowed).toBe(false);
    // Someone else's import is not the first person's fault.
    expect(limiter.take("123456").allowed).toBe(true);
  });

  it("counts down the tokens it reports", () => {
    const { limiter } = limiterAt();
    expect(limiter.take("a").remaining).toBe(2);
    expect(limiter.take("a").remaining).toBe(1);
    expect(limiter.take("a").remaining).toBe(0);
  });

  it("does not hold the key it was given", () => {
    // CLAUDE.md invariant 4: a team id identifies the user. The bucket only ever
    // needs to recognise the key again, so it keeps a hash and not the id.
    const { limiter } = limiterAt();
    limiter.take("895045");
    expect(JSON.stringify([...(limiter as unknown as { buckets: Map<string, unknown> }).buckets.keys()])).not.toContain(
      "895045",
    );
  });

  it("forgets keys that have gone quiet, so the map cannot grow forever", () => {
    const { limiter, advance } = limiterAt();
    limiter.take("one");
    limiter.take("two");
    expect(limiter.size).toBe(2);

    advance(IDLE_EVICTION_MS + 1);
    limiter.take("three");

    expect(limiter.size).toBe(1);
  });

  it("treats a forgotten key as a fresh one", () => {
    const { limiter, advance } = limiterAt();
    for (let i = 0; i < 3; i += 1) limiter.take("one");
    expect(limiter.take("one").allowed).toBe(false);

    advance(IDLE_EVICTION_MS + 1);

    // Which is correct, not a loophole: by then the bucket would have refilled
    // many times over anyway.
    expect(limiter.take("one").allowed).toBe(true);
  });

  it("clamps a nonsense configuration rather than dividing by zero", () => {
    const limiter = new RateLimiter({ capacity: 0, refillMs: 0 });
    const first = limiter.take("k");
    expect(first.allowed).toBe(true);
    expect(Number.isFinite(first.retryAfterMs)).toBe(true);
  });
});

describe("squadImportLimiter", () => {
  it("lets a real person import a few times a minute", () => {
    squadImportLimiter.reset();
    for (let i = 0; i < DEFAULT_CAPACITY; i += 1) {
      expect(squadImportLimiter.take("shared-instance-test").allowed).toBe(true);
    }
    expect(squadImportLimiter.take("shared-instance-test").allowed).toBe(false);
    squadImportLimiter.reset();
  });
});
