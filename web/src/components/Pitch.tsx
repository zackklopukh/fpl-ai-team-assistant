/**
 * The squad laid out on a pitch: goalkeepers at the top, then defenders,
 * midfielders and forwards — the arrangement of FPL's transfers screen.
 *
 * All fifteen go on the pitch in 2-5-5-3 rows rather than an XI plus a bench,
 * because the builder holds a squad, not a lineup. Choosing the XI is the
 * optimizer's job, and its plans show one.
 */

import PlayerCard from "@/components/PlayerCard";
import type { NextFixturesByTeam } from "@/lib/nextFixtures";
import {
  POSITIONS,
  POSITION_LABEL,
  SLOT_POSITIONS,
  type PlayerIndex,
  type SquadState,
} from "@/lib/squad";

export interface PitchProps {
  squad: SquadState;
  index: PlayerIndex;
  nextFixtures: NextFixturesByTeam;
  activeSlot: number | null;
  onSelectSlot: (slotIndex: number) => void;
}

/** Pitch markings for the attacking half, drawn behind the rows. */
function Markings() {
  return (
    <svg
      viewBox="0 0 100 140"
      preserveAspectRatio="none"
      className="pointer-events-none absolute inset-0 h-full w-full"
      aria-hidden="true"
    >
      <g fill="none" stroke="rgba(255,255,255,0.35)" strokeWidth="0.5">
        <rect x="3" y="2" width="94" height="136" />
        <rect x="22" y="2" width="56" height="22" />
        <rect x="37" y="2" width="26" height="8" />
        <path d="M40 24 A12 9 0 0 0 60 24" />
        <line x1="3" y1="138" x2="97" y2="138" />
        <path d="M36 138 A14 12 0 0 1 64 138" />
      </g>
    </svg>
  );
}

export default function Pitch({
  squad,
  index,
  nextFixtures,
  activeSlot,
  onSelectSlot,
}: PitchProps) {
  return (
    <div
      className="relative overflow-hidden rounded-xl px-1 py-4 sm:px-4 sm:py-6"
      style={{
        // Mown stripes. Plain gradients, so the pitch costs no image request.
        background:
          "repeating-linear-gradient(180deg, #15803d 0 44px, #166534 44px 88px)",
      }}
    >
      <Markings />

      <div className="relative flex flex-col gap-3 sm:gap-5">
        {POSITIONS.map((position) => {
          const slots = SLOT_POSITIONS.flatMap((p, i) => (p === position ? [i] : []));
          return (
            <ol
              key={position}
              aria-label={`${POSITION_LABEL[position]}s`}
              className="flex items-start justify-center gap-1 sm:gap-3"
            >
              {slots.map((slotIndex) => {
                const id = squad.picks[slotIndex];
                const player = id != null ? index.get(id) ?? null : null;
                return (
                  <li key={slotIndex} className="w-[19%] max-w-[104px] sm:w-[17%]">
                    <PlayerCard
                      position={position}
                      player={player}
                      opponents={player ? nextFixtures[player.teamFplId] : undefined}
                      active={activeSlot === slotIndex}
                      onSelect={() => onSelectSlot(slotIndex)}
                    />
                  </li>
                );
              })}
            </ol>
          );
        })}
      </div>
    </div>
  );
}
