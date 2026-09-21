/**
 * A starting eleven on the pitch in its formation, with the bench beneath —
 * how FPL draws a team on its Points screen.
 *
 * Separate from Pitch.tsx on purpose: that one holds a squad (fifteen slots in
 * 2-5-5-3, every card a button to change the slot), this one holds a lineup
 * (eleven in the formation the solver chose, four substitutes in autosub
 * order, nothing to click). Same turf, same cards, same widths — so the two
 * pages look like one app.
 */

import PlayerCard from "@/components/PlayerCard";
import type { Lineup } from "@/lib/ideal";
import type { NextFixturesByTeam } from "@/lib/nextFixtures";
import type { IdealPlayer } from "@/lib/optimizerTypes";
import { POSITIONS, POSITION_LABEL, type Position, type SquadPlayer } from "@/lib/squad";
import { positionOfElementType } from "@/lib/ideal";

export interface LineupPitchProps {
  lineup: Lineup;
  captain: number;
  viceCaptain: number;
  /** Resolve a player to the card's shape (club colours, short name). */
  resolve: (player: IdealPlayer) => SquadPlayer;
  nextFixtures: NextFixturesByTeam;
  /** Wildcard: mark kept and new players, and show selling price where it differs. */
  showKept: boolean;
}

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

function Card({
  player,
  position,
  props,
}: {
  player: IdealPlayer;
  position: Position;
  props: LineupPitchProps;
}) {
  const card = props.resolve(player);
  return (
    <PlayerCard
      position={position}
      player={card}
      opponents={props.nextFixtures[player.teamFplId]}
      badge={
        player.elementId === props.captain
          ? "C"
          : player.elementId === props.viceCaptain
            ? "V"
            : null
      }
      xp={player.xp}
      marker={props.showKept ? (player.kept ? "kept" : "new") : null}
      costTenths={props.showKept ? player.costTenths : null}
    />
  );
}

export default function LineupPitch(props: LineupPitchProps) {
  const { lineup } = props;
  return (
    <div className="overflow-hidden rounded-xl">
      <div
        className="relative px-1 py-4 sm:px-4 sm:py-6"
        style={{
          background:
            "repeating-linear-gradient(180deg, #15803d 0 44px, #166534 44px 88px)",
        }}
      >
        <Markings />
        <div className="relative flex flex-col gap-3 sm:gap-5">
          {POSITIONS.map((position) => (
            <ol
              key={position}
              aria-label={`Starting ${POSITION_LABEL[position].toLowerCase()}s`}
              className="flex items-start justify-center gap-1 sm:gap-3"
            >
              {lineup.rows[position].map((player) => (
                <li key={player.elementId} className="w-[19%] max-w-[104px] sm:w-[17%]">
                  <Card player={player} position={position} props={props} />
                </li>
              ))}
            </ol>
          ))}
        </div>
      </div>

      <div className="bg-emerald-950 px-1 pb-3 pt-2 sm:px-4">
        <p className="mb-1.5 text-center text-[10px] font-semibold uppercase tracking-[0.15em] text-emerald-200/80">
          Substitutes, in autosub order
        </p>
        <ol aria-label="Substitutes" className="flex items-start justify-center gap-1 sm:gap-3">
          {lineup.bench.map((player, i) => {
            const position = positionOfElementType(player.elementType) ?? "MID";
            // FPL's convention: the substitute goalkeeper first, unnumbered,
            // then the outfield subs numbered in the order they come on.
            const outfieldNumber =
              lineup.bench.slice(0, i + 1).filter((p) => p.elementType !== 1).length;
            return (
              <li key={player.elementId} className="w-[19%] max-w-[104px] sm:w-[17%]">
                <p className="mb-0.5 text-center text-[10px] font-medium text-emerald-100/80">
                  {position === "GKP" ? "GKP" : `${outfieldNumber}. ${position}`}
                </p>
                <Card player={player} position={position} props={props} />
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}
