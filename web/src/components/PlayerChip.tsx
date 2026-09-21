/**
 * A player named in advice: enough to know who he is without leaving the page,
 * and one click from everything else.
 *
 * "Buy Gonzalo" on its own is useless — which Gonzalo, which club, what does he
 * cost, is he fit, who does he play next. So wherever advice names a player it
 * names him like this: a small club-colour shirt, the name linking to his
 * player page, then club · position · next opponent · price, an availability
 * flag when he is not fully fit, and the projected points the plan credited him
 * with. The player page opens in a new tab, because the advice it came from is
 * held in memory and would be lost by navigating away.
 *
 * Relative imports: rendered inside components the test runner renders, and it
 * has no path aliases.
 */

import Shirt from "./Shirt";
import { formatPrice } from "../lib/format";
import { formatOpponents, type NextOpponent } from "../lib/nextFixtures";
import { POSITION_LABEL, type PlayerIndex } from "../lib/squad";

const STATUS_FLAG: Record<string, { label: string; className: string }> = {
  d: { label: "Doubtful", className: "bg-amber-100 text-amber-900 dark:bg-amber-500/20 dark:text-amber-200" },
  i: { label: "Injured", className: "bg-rose-100 text-rose-900 dark:bg-rose-500/20 dark:text-rose-200" },
  s: { label: "Suspended", className: "bg-rose-100 text-rose-900 dark:bg-rose-500/20 dark:text-rose-200" },
  u: { label: "Unavailable", className: "bg-rose-100 text-rose-900 dark:bg-rose-500/20 dark:text-rose-200" },
  n: { label: "Out of squad", className: "bg-rose-100 text-rose-900 dark:bg-rose-500/20 dark:text-rose-200" },
};

export interface PlayerChipProps {
  elementId: number;
  index: PlayerIndex;
  /** Fallback name when the player is not in the index (should not happen). */
  fallbackName?: string;
  /** Opponents next gameweek: [] is a blank, two entries a double. */
  opponents?: readonly NextOpponent[];
  /**
   * Integer tenths to show instead of today's list price — a sale shows what
   * the manager banks, which is the selling price, not the list price.
   */
  priceTenths?: number;
  priceLabel?: string;
  /** Projected points over the plan's horizon, and the words for the horizon. */
  xp?: number | null;
  horizonLabel?: string;
}

export default function PlayerChip({
  elementId,
  index,
  fallbackName,
  opponents,
  priceTenths,
  priceLabel,
  xp = null,
  horizonLabel,
}: PlayerChipProps) {
  const player = index.get(elementId);
  const name = player?.webName ?? fallbackName ?? `Player ${elementId}`;
  const price = priceTenths ?? player?.priceTenths;
  const flag = player?.status ? STATUS_FLAG[player.status] : undefined;
  const isBlank = opponents !== undefined && opponents.length === 0;

  const meta = [
    player ? player.teamShortName : null,
    player ? player.position : null,
    opponents !== undefined ? `next ${formatOpponents(opponents)}` : null,
  ].filter(Boolean);

  return (
    <span className="flex min-w-0 items-start gap-2">
      {player ? (
        <Shirt
          teamCode={player.teamCode}
          shortName={player.teamShortName}
          isGoalkeeper={player.position === "GKP"}
          className="mt-0.5 h-7 w-8 shrink-0"
        />
      ) : null}
      <span className="flex min-w-0 flex-col">
        <span className="flex flex-wrap items-center gap-1.5">
          <a
            href={`/players/${elementId}`}
            target="_blank"
            rel="noopener"
            title={
              player
                ? `${player.fullName ?? name} — ${player.teamName}, ${POSITION_LABEL[player.position].toLowerCase()}. Opens his stats in a new tab.`
                : "Opens his stats in a new tab."
            }
            className="font-medium text-slate-900 underline decoration-slate-300 underline-offset-2 hover:decoration-sky-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:text-slate-50 dark:decoration-slate-600"
          >
            {name}
          </a>
          {flag && (
            <span className={`rounded px-1 text-[10px] font-semibold ${flag.className}`}>
              {flag.label}
            </span>
          )}
        </span>
        <span className="text-xs text-slate-500 dark:text-slate-400">
          {meta.join(" · ")}
          {isBlank && " (blank)"}
          {price !== undefined && (
            <>
              {meta.length > 0 ? " · " : ""}
              <span className="tabular-nums" title={priceLabel}>
                {formatPrice(price)}
              </span>
            </>
          )}
        </span>
        {xp !== null && (
          <span className="text-xs tabular-nums text-emerald-800 dark:text-emerald-300">
            {xp.toFixed(1)} pts projected{horizonLabel ? ` ${horizonLabel}` : ""}
          </span>
        )}
      </span>
    </span>
  );
}
