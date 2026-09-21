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
  /**
   * Omit for a card that only displays (the /ideal lineup). The card then
   * renders as a plain element and its accessible name drops "Change player".
   */
  onSelect?: () => void;
  /** Optional extras, for lineups. None of them are shown unless passed. */
  /** Captain or vice-captain armband. */
  badge?: "C" | "V" | null;
  /** Expected points to show under the name, e.g. over a horizon. */
  xp?: number | null;
  /** Wildcard: already held ("kept") or a new signing ("new"). */
  marker?: "kept" | "new" | null;
  /**
   * Integer tenths: what the player costs this manager, when it can differ from
   * the list price (a kept player's selling price). Shown in place of the price,
   * with the list price beside it, only when the two differ.
   */
  costTenths?: number | null;
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
  badge = null,
  xp = null,
  marker = null,
  costTenths = null,
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

  const costDiffers = costTenths !== null && costTenths !== player.priceTenths;
  const hasExtras = badge !== null || marker !== null || xp !== null || costDiffers;

  const body = (
    <>
      {marker && (
        <span
          className={[
            "absolute left-0 top-0 z-10 rounded px-1 text-[8px] font-bold uppercase leading-[14px] tracking-wide sm:text-[9px]",
            marker === "new" ? "bg-sky-500 text-white" : "bg-white/85 text-emerald-900",
          ].join(" ")}
        >
          {marker === "new" ? "New" : "Kept"}
        </span>
      )}
      {badge && (
        <span
          className={[
            "absolute right-0.5 top-0 z-10 flex h-4 w-4 items-center justify-center rounded-full text-[9px] font-bold leading-none ring-1 sm:h-5 sm:w-5 sm:text-[10px]",
            badge === "C"
              ? "bg-slate-900 text-white ring-white"
              : "bg-white text-slate-900 ring-slate-900/40",
          ].join(" ")}
        >
          {badge}
        </span>
      )}
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
      {xp !== null && (
        <span className="w-full bg-slate-50 px-1 py-0.5 text-center text-[9px] font-semibold tabular-nums leading-tight text-emerald-800 sm:text-[11px]">
          {xp.toFixed(1)} xP
        </span>
      )}
      {costDiffers && (
        <span
          className="w-full truncate bg-amber-100 px-1 py-0.5 text-center text-[8px] font-medium tabular-nums leading-tight text-amber-900 sm:text-[10px]"
          title={`Sells for ${formatPrice(costTenths as number)}; list price ${formatPrice(player.priceTenths)}`}
        >
          list {formatPrice(player.priceTenths)}
        </span>
      )}
      <span className="w-full rounded-b bg-emerald-950/80 px-1 py-0.5 text-center text-[10px] font-semibold tabular-nums leading-tight text-white sm:text-xs">
        {formatPrice(costDiffers ? (costTenths as number) : player.priceTenths)}
      </span>
    </>
  );

  const describedPrice = costDiffers
    ? `Costs ${formatPrice(costTenths as number)} to keep, list price ${formatPrice(player.priceTenths)}.`
    : `${formatPrice(player.priceTenths)}.`;
  const extrasLabel = [
    badge === "C" ? " Captain." : badge === "V" ? " Vice-captain." : "",
    marker === "new" ? " New signing." : marker === "kept" ? " Kept from your squad." : "",
    xp !== null ? ` ${xp.toFixed(1)} expected points.` : "",
  ].join("");
  const baseLabel =
    `${player.webName}, ${player.teamName}, ${POSITION_LABEL[position].toLowerCase()}. ` +
    `Next: ${describeOpponents(opponents)}. ${describedPrice}`;

  if (!onSelect) {
    return (
      <div
        role="group"
        aria-label={`${baseLabel}${extrasLabel}`}
        className="relative flex w-full flex-col items-center rounded-md px-0.5 pb-0.5 pt-0.5"
      >
        {body}
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      aria-label={`${baseLabel}${extrasLabel} Change player.`}
      className={`${hasExtras ? "relative " : ""}flex w-full flex-col items-center rounded-md px-0.5 pb-0.5 pt-0.5 transition hover:bg-white/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-white ${ring}`}
    >
      {body}
    </button>
  );
}
