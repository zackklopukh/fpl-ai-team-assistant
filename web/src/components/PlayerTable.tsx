"use client";

/**
 * The interactive surface of the player table: search, position filter, sort.
 *
 * Everything else about /players is a server component. Keep it that way — the
 * only state here is three small pieces of UI state, and the player rows arrive
 * already fetched and already formatted-as-numbers from the server.
 */

import { useMemo, useState } from "react";

import {
  POSITIONS,
  formatNumber,
  formatPercent,
  formatPrice,
  formatStatus,
  positionShort,
  STATUS_SEVERITY,
} from "@/lib/format";
import type { ElementType, PlayerWithTeam } from "@/lib/types";

type SortKey =
  | "web_name"
  | "team_short_name"
  | "element_type"
  | "now_cost_tenths"
  | "form"
  | "total_points"
  | "selected_by_percent"
  | "status";

type SortDirection = "asc" | "desc";

interface Column {
  key: SortKey;
  label: string;
  /** Right-aligned figures get tabular numerals. */
  numeric?: boolean;
  title?: string;
}

const COLUMNS: Column[] = [
  { key: "web_name", label: "Player" },
  { key: "team_short_name", label: "Team" },
  { key: "element_type", label: "Pos" },
  { key: "now_cost_tenths", label: "Price", numeric: true },
  { key: "form", label: "Form", numeric: true, title: "Points per match over the last 30 days" },
  { key: "total_points", label: "Points", numeric: true, title: "Total points this season" },
  { key: "selected_by_percent", label: "Owned", numeric: true, title: "Share of managers selecting this player" },
  { key: "status", label: "Status" },
];

/** Numeric columns read better descending first; text columns ascending. */
const DEFAULT_DIRECTION: Record<SortKey, SortDirection> = {
  web_name: "asc",
  team_short_name: "asc",
  element_type: "asc",
  now_cost_tenths: "desc",
  form: "desc",
  total_points: "desc",
  selected_by_percent: "desc",
  status: "asc",
};

function sortValue(player: PlayerWithTeam, key: SortKey): number | string {
  switch (key) {
    case "web_name":
      return player.web_name.toLocaleLowerCase();
    case "team_short_name":
      return player.team_short_name.toLocaleLowerCase();
    case "status":
      return STATUS_SEVERITY[player.status] ?? 9;
    case "form":
      return player.form ?? -1;
    case "selected_by_percent":
      return player.selected_by_percent ?? -1;
    default:
      return player[key];
  }
}

const TONE_CLASSES: Record<string, string> = {
  ok: "text-slate-500 dark:text-slate-400",
  warn: "text-amber-700 dark:text-amber-400",
  bad: "text-rose-700 dark:text-rose-400",
};

export default function PlayerTable({ players }: { players: PlayerWithTeam[] }) {
  const [search, setSearch] = useState("");
  const [position, setPosition] = useState<ElementType | "all">("all");
  const [sortKey, setSortKey] = useState<SortKey>("total_points");
  const [direction, setDirection] = useState<SortDirection>("desc");

  const rows = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    const filtered = players.filter((p) => {
      if (position !== "all" && p.element_type !== position) return false;
      if (!needle) return true;
      return (
        p.web_name.toLocaleLowerCase().includes(needle) ||
        (p.full_name ?? "").toLocaleLowerCase().includes(needle) ||
        p.team_name.toLocaleLowerCase().includes(needle) ||
        p.team_short_name.toLocaleLowerCase().includes(needle)
      );
    });

    const sign = direction === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const av = sortValue(a, sortKey);
      const bv = sortValue(b, sortKey);
      if (av === bv) return a.web_name.localeCompare(b.web_name);
      if (typeof av === "string" || typeof bv === "string") {
        return String(av).localeCompare(String(bv)) * sign;
      }
      return (av - bv) * sign;
    });
  }, [players, search, position, sortKey, direction]);

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setDirection((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setDirection(DEFAULT_DIRECTION[key]);
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <label className="flex-1 sm:max-w-xs">
          <span className="sr-only">Search players</span>
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by player or club…"
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none placeholder:text-slate-400 focus:border-slate-500 focus:ring-2 focus:ring-slate-900/10 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:placeholder:text-slate-500 dark:focus:border-slate-400 dark:focus:ring-slate-100/10"
          />
        </label>

        <div
          role="group"
          aria-label="Filter by position"
          className="flex flex-wrap gap-1 rounded-lg border border-slate-200 p-1 dark:border-slate-800"
        >
          <FilterButton
            active={position === "all"}
            onClick={() => setPosition("all")}
            label="All"
          />
          {POSITIONS.map((p) => (
            <FilterButton
              key={p.value}
              active={position === p.value}
              onClick={() => setPosition(p.value)}
              label={p.short}
              title={p.name}
            />
          ))}
        </div>
      </div>

      <p className="text-xs text-slate-500 dark:text-slate-400" aria-live="polite">
        {rows.length} of {players.length} players
      </p>

      <div className="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-800">
        <table className="w-full min-w-[42rem] border-collapse text-sm">
          <thead>
            <tr className="bg-slate-50 dark:bg-slate-900/60">
              {COLUMNS.map((col) => {
                const active = col.key === sortKey;
                return (
                  <th
                    key={col.key}
                    scope="col"
                    title={col.title}
                    aria-sort={
                      active
                        ? direction === "asc"
                          ? "ascending"
                          : "descending"
                        : "none"
                    }
                    className={`border-b border-slate-200 px-3 py-2 font-medium dark:border-slate-800 ${
                      col.numeric ? "text-right" : "text-left"
                    }`}
                  >
                    <button
                      type="button"
                      onClick={() => toggleSort(col.key)}
                      className={`inline-flex items-center gap-1 whitespace-nowrap rounded px-1 py-0.5 text-xs uppercase tracking-wide transition-colors hover:text-slate-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-900/20 dark:hover:text-slate-100 dark:focus-visible:ring-slate-100/20 ${
                        active
                          ? "text-slate-900 dark:text-slate-100"
                          : "text-slate-500 dark:text-slate-400"
                      }`}
                    >
                      {col.label}
                      <span aria-hidden="true" className="text-[0.65rem]">
                        {active ? (direction === "asc" ? "▲" : "▼") : "•"}
                      </span>
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const status = formatStatus(p);
              return (
                <tr
                  key={`${p.season}-${p.element_id}`}
                  className="border-b border-slate-100 last:border-0 odd:bg-white even:bg-slate-50/60 hover:bg-slate-100 dark:border-slate-900 dark:odd:bg-transparent dark:even:bg-slate-900/40 dark:hover:bg-slate-800/60"
                >
                  <th scope="row" className="px-3 py-2 text-left font-medium text-slate-900 dark:text-slate-100">
                    <span className="block">{p.web_name}</span>
                    <span className="block text-xs font-normal text-slate-500 dark:text-slate-400">
                      {p.full_name?.trim() || p.web_name}
                    </span>
                  </th>
                  <td className="px-3 py-2 text-slate-600 dark:text-slate-300">
                    <span title={p.team_name}>{p.team_short_name}</span>
                  </td>
                  <td className="px-3 py-2">
                    <span className="rounded border border-slate-300 px-1.5 py-0.5 text-xs font-medium text-slate-600 dark:border-slate-700 dark:text-slate-300">
                      {positionShort(p.element_type)}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-900 dark:text-slate-100">
                    {formatPrice(p.now_cost_tenths)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-600 dark:text-slate-300">
                    {formatNumber(p.form)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums font-medium text-slate-900 dark:text-slate-100">
                    {p.total_points}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-600 dark:text-slate-300">
                    {formatPercent(p.selected_by_percent)}
                  </td>
                  <td className={`px-3 py-2 ${TONE_CLASSES[status.tone]}`}>
                    <span title={p.news?.trim() || status.long}>{status.short}</span>
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={COLUMNS.length}
                  className="px-3 py-10 text-center text-slate-500 dark:text-slate-400"
                >
                  No players match that search.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function FilterButton({
  active,
  onClick,
  label,
  title,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-pressed={active}
      className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
        active
          ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
          : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
      }`}
    >
      {label}
    </button>
  );
}
