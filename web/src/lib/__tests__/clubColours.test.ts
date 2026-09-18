import { describe, expect, it } from "vitest";

import { clubColours, readableTextOn, shirtColours } from "../clubColours";

describe("clubColours", () => {
  it("looks clubs up by their stable code", () => {
    // Arsenal is code 3 every season; its fpl_id is whatever the alphabet says.
    expect(clubColours(3).primary).toBe("#EF0107");
  });

  it("falls back to a neutral shirt for a club it does not know", () => {
    // A newly promoted club should still render, not crash or vanish.
    const unknown = clubColours(99999);
    expect(unknown.primary).toMatch(/^#[0-9A-F]{6}$/i);
    expect(clubColours(null)).toEqual(unknown);
  });

  it("dresses goalkeepers differently, keeping the club colour in the trim", () => {
    const outfield = shirtColours(3, false);
    const keeper = shirtColours(3, true);
    expect(keeper.primary).not.toBe(outfield.primary);
    expect(keeper.secondary).toBe(outfield.primary);
  });
});

describe("readableTextOn", () => {
  it("puts dark text on pale shirts and white text on dark ones", () => {
    expect(readableTextOn("#FFFFFF")).toBe("#0F172A");
    expect(readableTextOn("#6CABDD")).toBe("#0F172A"); // sky blue
    expect(readableTextOn("#670E36")).toBe("#FFFFFF"); // claret
    expect(readableTextOn("#241F20")).toBe("#FFFFFF"); // near black
  });

  it("does not throw on a malformed colour", () => {
    expect(readableTextOn("not-a-colour")).toBe("#0F172A");
  });
});
