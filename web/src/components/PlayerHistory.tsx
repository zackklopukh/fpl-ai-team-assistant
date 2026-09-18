/**
 * Per-gameweek history for one player, from `player_gw_stats`.
 *
 * Deliberately NOT a client component: this is a table with no interaction, so
 * it renders on the server and ships no JavaScript.
 *
 * The one-to-many rule applies here too, in a slightly different form. A row of
 * `player_gw_stats` is per FIXTURE, not per gameweek — the primary key is
 * (season, element_id, gw, fixture_id). So a double gameweek is two rows and a
 * blank is no row at all. This renders both rows of a double, grouped under one
 * gameweek heading, and never collapses them into "GW6".
 */

import { formatNumber, formatPrice } from "@/lib/format";
import type { PlayerGameweekStat } from "@/lib/fixtures";
import { groupStatsByGameweek } from "@/lib/fixtures";

function opponentLabel(
  stat: PlayerGameweekStat,
  teamShortNames: Record<number, string>,
): string {
  const short =
    stat.opponent_team_fpl_id === null
      ? "???"
      : teamShortNames[stat.opponent_team_fpl_id] ?? "???";
  if (stat.was_home === null) return short;
  return `${short} (${stat.was_home ? "H" : "A"})`;
}

export default function PlayerHistory({
  stats,
  teamShortNames,
  emptyNote,
}: {
  stats: PlayerGameweekStat[];
  teamShortNames: Record<number, string>;
  /** Why the history is empty, when it is. Shown instead of a bare blank table. */
  emptyNote?: string;
}) {
  const grouped = groupStatsByGameweek(stats);

  if (grouped.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-slate-300 px-4 py-8 text-center text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400">
        <p className="font-medium text-slate-700 dark:text-slate-200">
          No gameweek history
        </p>
        <p className="mx-auto mt-1 max-w-md text-xs leading-5">
          {emptyNote ??
            "No rows in player_gw_stats for this player yet. A blank gameweek produces no row at all, which is correct — read blanks off the fixture list above."}
        </p>
      </div>
    );
  }

  const totals = stats.reduce(
    (acc, s) => ({
      minutes: acc.minutes + s.minutes,
      points: acc.points + s.total_points,
      goals: acc.goals + s.goals_scored,
      assists: acc.assists + s.assists,
      bonus: acc.bonus + s.bonus,
    }),
    { minutes: 0, points: 0, goals: 0, assists: 0, bonus: 0 },
  );

  return (
    <div className="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-800">
      <table className="w-full min-w-[40rem] border-collapse text-sm">
        <caption className="sr-only">
          Per-gameweek history. A double gameweek contributes two rows under one
          gameweek; a blank contributes none.
        </caption>
        <thead>
          <tr className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500 dark:bg-slate-900/60 dark:text-slate-400">
            <th scope="col" className="px-3 py-2 text-left font-medium">
              GW
            </th>
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Opponent
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Min
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Pts
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              G
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              A
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium" title="Expected goals">
              xG
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium" title="Expected assists">
              xA
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Bonus
            </th>
            <th
              scope="col"
              className="px-3 py-2 text-right font-medium"
              title="The player's price during that gameweek, not today's"
            >
              Price then
            </th>
          </tr>
        </thead>
        <tbody>
          {grouped.map(({ gw, stats: weekStats }) =>
            weekStats.map((stat, index) => (
              <tr
                key={`${stat.gw}-${stat.fixture_id}`}
                className="border-b border-slate-100 last:border-0 dark:border-slate-900"
              >
                <th
                  scope="row"
                  className="px-3 py-2 text-left font-medium tabular-nums text-slate-900 dark:text-slate-100"
                >
                  {index === 0 ? gw : ""}
                  {weekStats.length > 1 && (
                    <span className="ml-1 text-[0.65rem] font-semibold uppercase text-indigo-700 dark:text-indigo-300">
                      {index === 0 ? "×2" : "·"}
                    </span>
                  )}
                </th>
                <td className="px-3 py-2 text-slate-600 dark:text-slate-300">
                  {opponentLabel(stat, teamShortNames)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-600 dark:text-slate-300">
                  {stat.minutes}
                </td>
                <td className="px-3 py-2 text-right tabular-nums font-medium text-slate-900 dark:text-slate-100">
                  {stat.total_points}
                  {!stat.bonus_settled && (
                    <span
                      className="ml-1 text-[0.65rem] font-normal text-amber-700 dark:text-amber-400"
                      title="Bonus points not yet settled"
                    >
                      prov
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-600 dark:text-slate-300">
                  {stat.goals_scored}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-600 dark:text-slate-300">
                  {stat.assists}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-600 dark:text-slate-300">
                  {formatNumber(stat.expected_goals, 2)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-600 dark:text-slate-300">
                  {formatNumber(stat.expected_assists, 2)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-600 dark:text-slate-300">
                  {stat.bonus}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-600 dark:text-slate-300">
                  {stat.value_tenths === null ? "—" : formatPrice(stat.value_tenths)}
                </td>
              </tr>
            )),
          )}
        </tbody>
        <tfoot>
          <tr className="bg-slate-50 text-sm font-medium dark:bg-slate-900/60">
            <th scope="row" colSpan={2} className="px-3 py-2 text-left">
              {stats.length} appearance{stats.length === 1 ? "" : "s"}
            </th>
            <td className="px-3 py-2 text-right tabular-nums">{totals.minutes}</td>
            <td className="px-3 py-2 text-right tabular-nums">{totals.points}</td>
            <td className="px-3 py-2 text-right tabular-nums">{totals.goals}</td>
            <td className="px-3 py-2 text-right tabular-nums">{totals.assists}</td>
            <td className="px-3 py-2 text-right" colSpan={2}>
              <span className="sr-only">Expected totals not summed here</span>
            </td>
            <td className="px-3 py-2 text-right tabular-nums">{totals.bonus}</td>
            <td className="px-3 py-2" />
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
