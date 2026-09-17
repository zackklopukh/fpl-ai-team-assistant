import { describe, expect, it } from "vitest";

import {
  ENCODING_VERSION,
  decodeSquad,
  encodeSquad,
  isSquadCode,
} from "../encode";
import { SQUAD_SIZE, emptySquad, type SquadState } from "../squad";

const FULL_SQUAD: SquadState = {
  picks: [1, 2, 40, 55, 120, 233, 401, 7, 88, 199, 342, 615, 15, 270, 1023],
  bankTenths: 7,
};

/** A 23-byte code with an arbitrary version byte, built without the encoder. */
function codeWithVersionByte(version: number): string {
  const bytes = new Uint8Array(23);
  bytes[0] = version;
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

describe("encodeSquad / decodeSquad", () => {
  it("round-trips a full squad exactly", () => {
    const code = encodeSquad(FULL_SQUAD);
    const result = decodeSquad(code);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.version).toBe(ENCODING_VERSION);
    expect(result.squad.picks).toEqual(FULL_SQUAD.picks);
    expect(result.squad.bankTenths).toBe(FULL_SQUAD.bankTenths);
  });

  it("round-trips empty slots as null, not 0", () => {
    const partial: SquadState = {
      picks: [1, null, 40, null, null, 233, 401, 7, null, 199, 342, 615, 15, null, 99],
      bankTenths: 235,
    };

    const result = decodeSquad(encodeSquad(partial));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.squad.picks).toEqual(partial.picks);
    expect(result.squad.picks).toHaveLength(SQUAD_SIZE);
  });

  it("round-trips an empty squad", () => {
    const result = decodeSquad(encodeSquad(emptySquad()));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.squad).toEqual(emptySquad());
  });

  it("round-trips a negative bank (a squad mid-edit can be overspent)", () => {
    const result = decodeSquad(encodeSquad({ ...FULL_SQUAD, bankTenths: -37 }));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.squad.bankTenths).toBe(-37);
  });

  it("produces a short, URL-safe code", () => {
    const code = encodeSquad(FULL_SQUAD);
    expect(code).toHaveLength(31);
    expect(code).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(encodeURIComponent(code)).toBe(code);
  });

  it("gives different squads different codes", () => {
    const other: SquadState = { ...FULL_SQUAD, picks: FULL_SQUAD.picks.slice() };
    other.picks[3] = 56;
    expect(encodeSquad(other)).not.toBe(encodeSquad(FULL_SQUAD));
    expect(encodeSquad({ ...FULL_SQUAD, bankTenths: 8 })).not.toBe(
      encodeSquad(FULL_SQUAD),
    );
  });

  it("rejects an element id that does not fit the format", () => {
    expect(() =>
      encodeSquad({ ...FULL_SQUAD, picks: [...FULL_SQUAD.picks.slice(1), 1024] }),
    ).toThrow(RangeError);
  });

  it("rejects the wrong number of slots", () => {
    expect(() => encodeSquad({ picks: [1, 2, 3], bankTenths: 0 })).toThrow(RangeError);
  });
});

describe("decodeSquad is total", () => {
  it("reports an empty or missing code", () => {
    for (const input of ["", "   ", null, undefined]) {
      const result = decodeSquad(input as string);
      expect(result.ok).toBe(false);
      if (result.ok) return;
      expect(result.error).toBe("empty");
      expect(result.message).toMatch(/no squad/i);
    }
  });

  it("rejects characters that are not part of a code", () => {
    const code = encodeSquad(FULL_SQUAD);
    const result = decodeSquad(`${code.slice(0, code.length - 1)}!`);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.error).toBe("not-base64url");
  });

  it("rejects a truncated code without crashing", () => {
    const code = encodeSquad(FULL_SQUAD);
    for (let cut = 1; cut < code.length; cut += 1) {
      const result = decodeSquad(code.slice(0, code.length - cut));
      expect(result.ok).toBe(false);
      if (result.ok) continue;
      expect(["wrong-length", "corrupt", "unsupported-version"]).toContain(result.error);
      expect(result.message.length).toBeGreaterThan(10);
    }
  });

  it("rejects a code with something appended", () => {
    const result = decodeSquad(`${encodeSquad(FULL_SQUAD)}AAAA`);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.error).toBe("wrong-length");
  });

  it("catches a hand-edited character with the checksum", () => {
    const code = encodeSquad(FULL_SQUAD);
    let corruptedFound = 0;

    for (let i = 0; i < code.length; i += 1) {
      const replacement = code[i] === "A" ? "B" : "A";
      const edited = `${code.slice(0, i)}${replacement}${code.slice(i + 1)}`;
      const result = decodeSquad(edited);
      if (!result.ok) {
        corruptedFound += 1;
        expect(["corrupt", "unsupported-version"]).toContain(result.error);
      }
    }

    // Not every single-character edit is detectable by an 8-bit checksum, but the
    // overwhelming majority must be, and none may crash.
    expect(corruptedFound).toBeGreaterThan(code.length - 4);
  });

  it("rejects an unknown encoding version by name", () => {
    const result = decodeSquad(codeWithVersionByte(ENCODING_VERSION + 1));
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.error).toBe("unsupported-version");
    expect(result.message).toContain(String(ENCODING_VERSION + 1));
    expect(result.message).toContain(String(ENCODING_VERSION));
  });

  it("rejects version 0 and version 255 too", () => {
    for (const version of [0, 2, 9, 255]) {
      const result = decodeSquad(codeWithVersionByte(version));
      expect(result.ok).toBe(false);
      if (result.ok) continue;
      expect(result.error).toBe("unsupported-version");
    }
  });

  it("never throws, whatever it is handed", () => {
    const inputs = [
      "",
      "=",
      "====",
      "A",
      "AAAA",
      "💥💥💥",
      "#s=",
      "a".repeat(1000),
      encodeSquad(FULL_SQUAD).toUpperCase(),
      encodeSquad(FULL_SQUAD).split("").reverse().join(""),
    ];
    for (const input of inputs) {
      expect(() => decodeSquad(input)).not.toThrow();
    }
  });

  it("isSquadCode agrees with decodeSquad", () => {
    expect(isSquadCode(encodeSquad(FULL_SQUAD))).toBe(true);
    expect(isSquadCode("nonsense")).toBe(false);
  });
});
