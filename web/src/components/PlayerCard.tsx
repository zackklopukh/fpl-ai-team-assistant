/**
 * One player on the pitch: shirt, name, next opponent, price.
 *
 * A button, because every card is a way to change that slot. The accessible
 * name spells out everything the card shows, so a screen reader hears the same
 * card a sighted user sees — including a blank gameweek, which on screen is a
 * dash and would otherwise be read as nothing at all.
 */

import Shirt from "@/components/Shirt";
import { formatPrice } from "@/lib/format";
import { formatOpponents, type NextOpponent } from "@/lib/nextFixtures";
import { POSITION_LABEL, type Position, type SquadPlayer } from "@/lib/squad";

export interface PlayerCardProps {
  position: Position;
  player: SquadPlayer | null;
  /** Opponents in the next gameweek: [] is a blank, two entries a double. */
  opponents?: readonly NextOpponent[];
  active?: boolean;
  onSelect: () => void;
}

function describeOpponents(opponents: readonly NextOpponent[] | undefined): string {
  if (!opponents || opponents.length === 0) return "no fixture next gameweek";
  return opponents
    .map((o) => `${o.opponentShortName} ${o.isHome ? "at home" : "away"}`)
    .join(" and ");
}

export default function PlayerCard({
  position,
  player,
  opponents,
  active = false,
  onSelect,
}: PlayerCardProps) {
  const ring = active
    ? "ring-2 ring-sky-300 ring-offset-2 ring-offset-emerald-700"
    : "";

  if (!player) {
    return (
      <button
        type="button"
        onClick={onSelect}
        aria-label={`Empty ${POSITION_LABEL[position].toLowerCase()} slot. Pick a player.`}
        aria-pressed={active}
        className={`group flex w-full flex-col items-center rounded-md px-0.5 pb-1 pt-0.5 transition hover:bg-white/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-white ${ring}`}
      >
        <Shirt empty label={position} className="h-10 w-12 sm:h-12 sm:w-14" />
        <span className="mt-0.5 rounded bg-white/15 px-1.5 py-0.5 text-[10px] font-medium text-white/90 sm:text-[11px]">
          Add
        </span>
      </button>
    );
  }

  const isBlank = !opponents || opponents.length === 0;
  const isDouble = (opponents?.length ?? 0) > 1;

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      aria-label={
        `${player.webName}, ${player.teamName}, ${POSITION_LABEL[position].toLowerCase()}. ` +
        `Next: ${describeOpponents(opponents)}. ${formatPrice(player.priceTenths)}. Change player.`
      }
      className={`flex w-full flex-col items-center rounded-md px-0.5 pb-0.5 pt-0.5 transition hover:bg-white/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-white ${ring}`}
    >
      <Shirt
        teamCode={player.teamCode}
        shortName={player.teamShortName}
        isGoalkeeper={position === "GKP"}
        className="h-10 w-12 drop-shadow-sm sm:h-12 sm:w-14"
      />
      <span className="mt-0.5 w-full overflow-hidden rounded-t bg-white px-1 py-0.5 text-center text-[10px] font-semibold leading-tight text-slate-900 sm:text-xs">
        <span className="block truncate">{player.webName}</span>
      </span>
      <span
        className={[
          "w-full truncate px-1 py-0.5 text-center text-[9px] font-medium leading-tight sm:text-[11px]",
          isBlank
            ? "bg-slate-300 text-slate-600"
            : isDouble
              ? "bg-indigo-100 text-indigo-900"
              : "bg-slate-100 text-slate-700",
        ].join(" ")}
        title={isBlank ? "Blank gameweek — no fixture" : undefined}
      >
        {formatOpponents(opponents)}
      </span>
      <span className="w-full rounded-b bg-emerald-950/80 px-1 py-0.5 text-center text-[10px] font-semibold tabular-nums leading-tight text-white sm:text-xs">
        {formatPrice(player.priceTenths)}
      </span>
    </button>
  );
}
