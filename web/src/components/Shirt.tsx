/**
 * A generic football shirt, drawn for this site and identical for every club.
 *
 * Only the fill changes: a solid body in the club's colour with a thin collar
 * and cuff trim in its second colour, plus the club's short code on the chest.
 * No club's kit design is reproduced — see lib/clubColours.ts for why that line
 * matters. The short code also means colour is never the only way to tell two
 * red shirts apart.
 */

import { readableTextOn, shirtColours } from "@/lib/clubColours";

export interface ShirtProps {
  teamCode?: number | null;
  shortName?: string | null;
  isGoalkeeper?: boolean;
  /** An empty slot: a dashed outline with the position in place of a club. */
  empty?: boolean;
  label?: string | null;
  className?: string;
}

// Body with set-in sleeves, as one closed path on a 64x56 grid.
const BODY =
  "M22 4 L16 6 L3 14 L9 26 L15 23 L15 52 L49 52 L49 23 L55 26 L61 14 L48 6 L42 4 " +
  "Q32 12 22 4 Z";
const COLLAR = "M22 4 Q32 12 42 4 L39 3 Q32 8.5 25 3 Z";
const LEFT_CUFF = "M3 14 L9 26 L10.8 25.1 L4.8 13.2 Z";
const RIGHT_CUFF = "M61 14 L55 26 L53.2 25.1 L59.2 13.2 Z";

export default function Shirt({
  teamCode,
  shortName,
  isGoalkeeper = false,
  empty = false,
  label,
  className,
}: ShirtProps) {
  if (empty) {
    return (
      <svg viewBox="0 0 64 56" className={className} aria-hidden="true">
        <path
          d={BODY}
          fill="rgba(255,255,255,0.08)"
          stroke="rgba(255,255,255,0.55)"
          strokeWidth="1.5"
          strokeDasharray="3 2.5"
          strokeLinejoin="round"
        />
        {label && (
          <text
            x="32"
            y="36"
            textAnchor="middle"
            fontSize="10"
            fontWeight="700"
            fill="rgba(255,255,255,0.85)"
          >
            {label}
          </text>
        )}
      </svg>
    );
  }

  const { primary, secondary } = shirtColours(teamCode, isGoalkeeper);
  const text = readableTextOn(primary);

  return (
    <svg viewBox="0 0 64 56" className={className} aria-hidden="true">
      <path
        d={BODY}
        fill={primary}
        stroke="rgba(15,23,42,0.45)"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      <path d={COLLAR} fill={secondary} />
      <path d={LEFT_CUFF} fill={secondary} />
      <path d={RIGHT_CUFF} fill={secondary} />
      {shortName && (
        <text
          x="32"
          y="37"
          textAnchor="middle"
          fontSize="9.5"
          fontWeight="800"
          letterSpacing="0.5"
          fill={text}
        >
          {shortName}
        </text>
      )}
    </svg>
  );
}
