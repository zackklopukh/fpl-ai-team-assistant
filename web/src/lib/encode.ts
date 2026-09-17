/**
 * Compact, versioned URL encoding of a squad.
 *
 * ARCHITECTURE.md ("Where squad state lives instead") calls this out as worth
 * doing early: fifteen element ids plus a bank figure compress to a short
 * string, and that buys shareable links, bookmarkable squads and a trivial way
 * to reproduce a bug report — without storing a single byte server-side.
 *
 * Format, version 1 — 23 bytes, 31 base64url characters:
 *
 *   byte  0        encoding version
 *   bytes 1..21    163 packed bits, then 5 bits of zero padding:
 *                    15 x 10 bits  element id, 0 meaning an empty slot
 *                    1  x 13 bits  bank in tenths, offset by BANK_OFFSET
 *   byte  22       checksum of bytes 0..21
 *
 * Ten bits per id because FPL element ids run under 1024; JSON would be roughly
 * five times longer before base64 even gets involved. The checksum is not
 * security, it is so a truncated or hand-edited link fails loudly instead of
 * decoding into a plausible but wrong squad.
 *
 * Decoding is total. Every malformed input returns { ok: false } with a message
 * a person can act on; nothing here throws and nothing returns half a squad.
 */

import { SQUAD_SIZE, type SquadState } from "./squad";

/** Bump when the packed layout changes. Old versions are rejected, not guessed at. */
export const ENCODING_VERSION = 1;

const ID_BITS = 10;
const MAX_ELEMENT_ID = (1 << ID_BITS) - 1; // 1023

const BANK_BITS = 13;
/** Bank is signed: a squad can be built over budget mid-edit. */
const BANK_OFFSET = 1024;
const MIN_BANK_TENTHS = -BANK_OFFSET;
const MAX_BANK_TENTHS = (1 << BANK_BITS) - 1 - BANK_OFFSET; // 7167

const PAYLOAD_BITS = SQUAD_SIZE * ID_BITS + BANK_BITS;
const PAYLOAD_BYTES = Math.ceil(PAYLOAD_BITS / 8);
const TOTAL_BYTES = 1 + PAYLOAD_BYTES + 1;
/** Unpadded base64url length of TOTAL_BYTES — a code is always exactly this long. */
export const CODE_LENGTH = Math.ceil((TOTAL_BYTES * 8) / 6);

const B64_ALPHABET =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

const B64_LOOKUP: Record<string, number> = (() => {
  const lookup: Record<string, number> = {};
  for (let i = 0; i < B64_ALPHABET.length; i += 1) lookup[B64_ALPHABET[i]] = i;
  // Tolerate links that have been through something that swapped the alphabet.
  lookup["+"] = 62;
  lookup["/"] = 63;
  return lookup;
})();

function toBase64Url(bytes: Uint8Array): string {
  let out = "";
  for (let i = 0; i < bytes.length; i += 3) {
    const b0 = bytes[i];
    const b1 = i + 1 < bytes.length ? bytes[i + 1] : 0;
    const b2 = i + 2 < bytes.length ? bytes[i + 2] : 0;
    out += B64_ALPHABET[b0 >> 2];
    out += B64_ALPHABET[((b0 & 0x03) << 4) | (b1 >> 4)];
    if (i + 1 < bytes.length) out += B64_ALPHABET[((b1 & 0x0f) << 2) | (b2 >> 6)];
    if (i + 2 < bytes.length) out += B64_ALPHABET[b2 & 0x3f];
  }
  return out;
}

function fromBase64Url(text: string): Uint8Array | null {
  const clean = text.replace(/=+$/, "");
  if (clean.length === 0) return null;
  if (clean.length % 4 === 1) return null; // no byte count produces this

  const bytes = new Uint8Array(Math.floor((clean.length * 6) / 8));
  let bits = 0;
  let acc = 0;
  let out = 0;

  for (let i = 0; i < clean.length; i += 1) {
    const value = B64_LOOKUP[clean[i]];
    if (value === undefined) return null;
    acc = (acc << 6) | value;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      bytes[out] = (acc >> bits) & 0xff;
      out += 1;
    }
  }
  return bytes.subarray(0, out);
}

function checksum(bytes: Uint8Array, length: number): number {
  let hash = 0x811c9dc5 >>> 0;
  for (let i = 0; i < length; i += 1) {
    hash ^= bytes[i];
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash & 0xff;
}

class BitWriter {
  private readonly bytes: Uint8Array;
  private bit = 0;

  constructor(byteLength: number) {
    this.bytes = new Uint8Array(byteLength);
  }

  write(value: number, width: number): void {
    for (let i = width - 1; i >= 0; i -= 1) {
      const set = (value >> i) & 1;
      if (set) this.bytes[this.bit >> 3] |= 0x80 >> (this.bit & 7);
      this.bit += 1;
    }
  }

  done(): Uint8Array {
    return this.bytes;
  }
}

class BitReader {
  private bit = 0;

  constructor(private readonly bytes: Uint8Array) {}

  read(width: number): number {
    let value = 0;
    for (let i = 0; i < width; i += 1) {
      const byte = this.bytes[this.bit >> 3] ?? 0;
      value = (value << 1) | ((byte >> (7 - (this.bit & 7))) & 1);
      this.bit += 1;
    }
    return value;
  }
}

export interface EncodableSquad {
  picks: readonly (number | null)[];
  bankTenths: number;
}

export type DecodeError =
  | "empty"
  | "not-base64url"
  | "wrong-length"
  | "unsupported-version"
  | "corrupt";

export type DecodeResult =
  | { ok: true; squad: SquadState; version: number }
  | { ok: false; error: DecodeError; message: string };

/**
 * Pack a squad into a URL-safe string.
 *
 * Throws only on input that could never have come from this app: an element id
 * that does not fit in ten bits, or the wrong number of slots. Bank is clamped
 * rather than thrown on, because a mid-edit bank can wander and losing the whole
 * link over it would be worse than losing the exact figure.
 */
export function encodeSquad(squad: EncodableSquad): string {
  if (squad.picks.length !== SQUAD_SIZE) {
    throw new RangeError(
      `A squad has ${SQUAD_SIZE} slots, got ${squad.picks.length}.`,
    );
  }

  const writer = new BitWriter(PAYLOAD_BYTES);
  for (const id of squad.picks) {
    const value = id ?? 0;
    if (!Number.isInteger(value) || value < 0 || value > MAX_ELEMENT_ID) {
      throw new RangeError(
        `Element id ${id} does not fit in ${ID_BITS} bits (0-${MAX_ELEMENT_ID}).`,
      );
    }
    writer.write(value, ID_BITS);
  }

  const bank = Math.max(
    MIN_BANK_TENTHS,
    Math.min(MAX_BANK_TENTHS, Math.round(squad.bankTenths)),
  );
  writer.write(bank + BANK_OFFSET, BANK_BITS);

  const bytes = new Uint8Array(TOTAL_BYTES);
  bytes[0] = ENCODING_VERSION;
  bytes.set(writer.done(), 1);
  bytes[TOTAL_BYTES - 1] = checksum(bytes, TOTAL_BYTES - 1);

  return toBase64Url(bytes);
}

function fail(error: DecodeError, message: string): DecodeResult {
  return { ok: false, error, message };
}

/**
 * Unpack a squad string. Never throws, never returns a partial squad.
 */
export function decodeSquad(text: string | null | undefined): DecodeResult {
  if (typeof text !== "string" || text.trim() === "") {
    return fail("empty", "There is no squad in this link.");
  }

  const trimmed = text.trim().replace(/=+$/, "");
  if (trimmed.length !== CODE_LENGTH) {
    return fail(
      "wrong-length",
      `A squad code is ${CODE_LENGTH} characters; this one is ${trimmed.length}. It looks truncated, or something was appended to the link.`,
    );
  }

  const bytes = fromBase64Url(trimmed);
  if (!bytes) {
    return fail(
      "not-base64url",
      "This squad link contains characters that are not part of a squad code.",
    );
  }

  const version = bytes[0];
  if (version !== ENCODING_VERSION) {
    return fail(
      "unsupported-version",
      `This squad link uses encoding version ${version}; this app reads version ${ENCODING_VERSION}.`,
    );
  }

  if (bytes[TOTAL_BYTES - 1] !== checksum(bytes, TOTAL_BYTES - 1)) {
    return fail(
      "corrupt",
      "This squad link failed its checksum — a character was probably changed or lost in transit.",
    );
  }

  const reader = new BitReader(bytes.subarray(1, 1 + PAYLOAD_BYTES));
  const picks: (number | null)[] = [];
  for (let i = 0; i < SQUAD_SIZE; i += 1) {
    const id = reader.read(ID_BITS);
    picks.push(id === 0 ? null : id);
  }
  const bankTenths = reader.read(BANK_BITS) - BANK_OFFSET;

  return { ok: true, version, squad: { picks, bankTenths } };
}

/** Round-trip helper for tests and for anything that wants a canonical string. */
export function isSquadCode(text: string): boolean {
  return decodeSquad(text).ok;
}
