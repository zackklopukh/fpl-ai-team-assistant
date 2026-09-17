/**
 * Display helpers.
 *
 * This is the ONLY place integer tenths become a display string. If another
 * file divides by ten, two files now own the rule and one of them will drift.
 */

import type { ElementType, Player, PlayerStatus } from "@/lib/types";

/** `formatPrice(55)` -> "£5.5m". Input is integer tenths, never pounds. */
export function formatPrice(tenths: number): string {
  const sign = tenths < 0 ? "-" : "";
  const abs = Math.abs(tenths);
  return `${sign}£${Math.floor(abs / 10)}.${abs % 10}m`;
}

/** A price *change* in tenths: "+£0.2m", "-£0.1m", "£0.0m". */
export function formatPriceChange(tenths: number): string {
  const sign = tenths > 0 ? "+" : "";
  return `${sign}${formatPrice(tenths)}`;
}

const POSITION_NAMES: Record<ElementType, string> = {
  1: "Goalkeeper",
  2: "Defender",
  3: "Midfielder",
  4: "Forward",
};

const POSITION_SHORT: Record<ElementType, string> = {
  1: "GKP",
  2: "DEF",
  3: "MID",
  4: "FWD",
};

export function positionName(elementType: ElementType): string {
  return POSITION_NAMES[elementType] ?? "Unknown";
}

export function positionShort(elementType: ElementType): string {
  return POSITION_SHORT[elementType] ?? "—";
}

export const POSITIONS: ReadonlyArray<{
  value: ElementType;
  name: string;
  short: string;
}> = [1, 2, 3, 4].map((t) => ({
  value: t as ElementType,
  name: POSITION_NAMES[t as ElementType],
  short: POSITION_SHORT[t as ElementType],
}));

/** How a status reads to a human, and how worried the UI should look. */
export type StatusTone = "ok" | "warn" | "bad";

export interface StatusLabel {
  /** Short badge text. */
  short: string;
  /** Full sentence, suitable for a tooltip or a detail line. */
  long: string;
  tone: StatusTone;
}

const STATUS_LABELS: Record<PlayerStatus, StatusLabel> = {
  a: { short: "Available", long: "Available", tone: "ok" },
  d: { short: "Doubtful", long: "Doubtful", tone: "warn" },
  i: { short: "Injured", long: "Injured", tone: "bad" },
  s: { short: "Suspended", long: "Suspended", tone: "bad" },
  u: { short: "Unavailable", long: "Unavailable", tone: "bad" },
  n: { short: "On loan", long: "On loan or out of the squad", tone: "bad" },
};

/**
 * Status as something a human reads. The chance-of-playing percentage is the
 * most useful part of a doubt, so it is folded in when FPL supplies one.
 */
export function formatStatus(
  player: Pick<Player, "status" | "chance_of_playing_this_round" | "news">,
): StatusLabel {
  const base = STATUS_LABELS[player.status] ?? {
    short: "Unknown",
    long: "Status unknown",
    tone: "warn" as StatusTone,
  };

  const chance = player.chance_of_playing_this_round;
  if (chance !== null && chance !== undefined && chance < 100) {
    // FPL flags a player with a reduced chance even while status is 'a'.
    // Reporting "Available" there would bury the one fact that matters.
    const label = player.status === "a" ? "Doubtful" : base.short;
    return {
      short: `${label} ${chance}%`,
      long: `${label} — ${chance}% chance of playing this gameweek`,
      tone: chance === 0 ? "bad" : base.tone === "ok" ? "warn" : base.tone,
    };
  }
  return base;
}

/** The news line, trimmed; empty news reads as no news. */
export function formatNews(news: string | null): string | null {
  const trimmed = news?.trim();
  return trimmed ? trimmed : null;
}

/** Ranking order for sorting by severity: available first, worst last. */
export const STATUS_SEVERITY: Record<PlayerStatus, number> = {
  a: 0,
  d: 1,
  s: 2,
  i: 3,
  u: 4,
  n: 5,
};

/** Fixed-decimal number, or an em dash when the value is missing. */
export function formatNumber(value: number | null, decimals = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toFixed(decimals);
}

/** Ownership as a percentage: 40.9 -> "40.9%". */
export function formatPercent(value: number | null, decimals = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value.toFixed(decimals)}%`;
}

/** Integer with thousands separators. */
export function formatInt(value: number | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-GB");
}

/** A deadline, in UTC, spelled out. The deadline is the clock everything derives from. */
export function formatDeadline(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    hour12: false,
  }).format(date) + " UTC";
}
