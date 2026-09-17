"use client";

/**
 * One squad slot: a searchable combobox over the player list.
 *
 * This is the manual path from ARCHITECTURE.md ("Keep a manual path") — the
 * fallback when screenshot parsing fails and the only way in for someone whose
 * screenshot is a photo of a laptop. On a fifteen-slot form it has to work
 * without a mouse, so it implements the ARIA combobox pattern properly:
 * arrow keys move the active option, Enter picks it, Escape closes, and the
 * active option is announced through aria-activedescendant.
 */

import { useEffect, useId, useMemo, useRef, useState } from "react";

import { formatPrice } from "@/lib/format";
import {
  MAX_PER_CLUB,
  POSITION_LABEL,
  type Position,
  type SquadPlayer,
} from "@/lib/squad";

const MAX_VISIBLE = 40;

export interface PlayerPickerProps {
  slotIndex: number;
  position: Position;
  selected: SquadPlayer | null;
  options: readonly SquadPlayer[];
  /** Element ids already in another slot — never pickable twice. */
  pickedIds: ReadonlySet<number>;
  /** Clubs already at the limit, so the picker can warn before the error appears. */
  fullClubs: ReadonlyMap<number, string>;
  /** What this slot can spend without going into the red: bank + any refund. */
  affordableTenths: number;
  onPick: (player: SquadPlayer | null) => void;
}

function matches(player: SquadPlayer, query: string): boolean {
  if (!query) return true;
  const needle = query.toLowerCase();
  return (
    player.webName.toLowerCase().includes(needle) ||
    (player.fullName ?? "").toLowerCase().includes(needle) ||
    player.teamShortName.toLowerCase().includes(needle) ||
    player.teamName.toLowerCase().includes(needle)
  );
}

export default function PlayerPicker({
  slotIndex,
  position,
  selected,
  options,
  pickedIds,
  fullClubs,
  affordableTenths,
  onPick,
}: PlayerPickerProps) {
  const [editing, setEditing] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);

  const baseId = useId();
  const listId = `${baseId}-listbox`;
  const labelId = `${baseId}-label`;
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const results = useMemo(() => {
    const filtered = options.filter((player) => matches(player, query));
    return filtered.slice(0, MAX_VISIBLE);
  }, [options, query]);

  const hiddenCount = useMemo(() => {
    if (!query) return Math.max(0, options.length - MAX_VISIBLE);
    return Math.max(0, options.filter((p) => matches(p, query)).length - MAX_VISIBLE);
  }, [options, query]);

  // The list shrinks as the query narrows; never point past the end of it.
  const active = results.length === 0 ? 0 : Math.min(activeIndex, results.length - 1);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  // Keep the active option in view when arrowing through a long list.
  useEffect(() => {
    if (!editing) return;
    const list = listRef.current;
    const option = list?.children[active] as HTMLElement | undefined;
    option?.scrollIntoView({ block: "nearest" });
  }, [active, editing]);

  // Close on a click outside, the one thing keyboard handling does not cover.
  useEffect(() => {
    if (!editing) return;
    function onPointerDown(event: MouseEvent | TouchEvent) {
      if (!containerRef.current?.contains(event.target as Node)) {
        setEditing(false);
        setQuery("");
      }
    }
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [editing]);

  function choose(player: SquadPlayer) {
    if (pickedIds.has(player.elementId) && selected?.elementId !== player.elementId) {
      return;
    }
    onPick(player);
    setEditing(false);
    setQuery("");
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((i) => (results.length === 0 ? 0 : (i + 1) % results.length));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((i) =>
        results.length === 0 ? 0 : (i - 1 + results.length) % results.length,
      );
    } else if (event.key === "Home") {
      event.preventDefault();
      setActiveIndex(0);
    } else if (event.key === "End") {
      event.preventDefault();
      setActiveIndex(Math.max(0, results.length - 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const player = results[active];
      if (player) choose(player);
    } else if (event.key === "Escape") {
      event.preventDefault();
      setEditing(false);
      setQuery("");
    } else if (event.key === "Tab") {
      setEditing(false);
      setQuery("");
    }
  }

  const slotLabel = `${POSITION_LABEL[position]} ${slotIndex + 1}`;

  if (!editing) {
    return (
      <div
        ref={containerRef}
        className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white p-2 dark:border-slate-800 dark:bg-slate-900"
      >
        {selected ? (
          <>
            <span
              className="inline-flex h-7 w-11 shrink-0 items-center justify-center rounded bg-slate-100 text-[11px] font-semibold tracking-wide text-slate-600 dark:bg-slate-800 dark:text-slate-300"
              aria-hidden="true"
            >
              {selected.teamShortName}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-slate-900 dark:text-slate-50">
                {selected.webName}
              </span>
              <span className="block truncate text-xs text-slate-500 dark:text-slate-400">
                {selected.teamName} · {formatPrice(selected.priceTenths)}
                {typeof selected.totalPoints === "number"
                  ? ` · ${selected.totalPoints} pts`
                  : ""}
              </span>
            </span>
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="rounded border border-slate-200 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
            >
              Change<span className="sr-only"> {slotLabel}</span>
            </button>
            <button
              type="button"
              onClick={() => onPick(null)}
              className="rounded border border-transparent px-2 py-1 text-xs font-medium text-slate-500 hover:bg-slate-50 hover:text-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100"
            >
              Clear<span className="sr-only"> {slotLabel}</span>
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="flex w-full items-center gap-2 rounded px-1 py-1 text-left text-sm text-slate-500 hover:text-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:text-slate-400 dark:hover:text-slate-100"
          >
            <span
              className="inline-flex h-7 w-11 shrink-0 items-center justify-center rounded border border-dashed border-slate-300 text-[11px] font-semibold text-slate-400 dark:border-slate-700"
              aria-hidden="true"
            >
              {position}
            </span>
            <span>Pick a {POSITION_LABEL[position].toLowerCase()}</span>
          </button>
        )}
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="relative rounded-lg border border-sky-500 bg-white p-2 ring-1 ring-sky-500/30 dark:bg-slate-900"
    >
      <label id={labelId} htmlFor={`${baseId}-input`} className="sr-only">
        Search for a {POSITION_LABEL[position].toLowerCase()} for {slotLabel}
      </label>
      <input
        id={`${baseId}-input`}
        ref={inputRef}
        type="text"
        role="combobox"
        aria-expanded="true"
        aria-controls={listId}
        aria-labelledby={labelId}
        aria-autocomplete="list"
        aria-activedescendant={
          results[active] ? `${baseId}-option-${results[active].elementId}` : undefined
        }
        autoComplete="off"
        value={query}
        placeholder={`Search ${POSITION_LABEL[position].toLowerCase()}s or clubs`}
        onChange={(event) => {
          setQuery(event.target.value);
          setActiveIndex(0);
        }}
        onKeyDown={onKeyDown}
        className="w-full rounded bg-transparent px-1 py-1 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none dark:text-slate-50"
      />

      <ul
        id={listId}
        ref={listRef}
        role="listbox"
        aria-label={`${POSITION_LABEL[position]}s`}
        className="absolute left-0 right-0 top-full z-20 mt-1 max-h-72 overflow-y-auto rounded-lg border border-slate-200 bg-white py-1 shadow-lg dark:border-slate-700 dark:bg-slate-900"
      >
        {results.length === 0 && (
          <li className="px-3 py-2 text-sm text-slate-500 dark:text-slate-400">
            No {POSITION_LABEL[position].toLowerCase()} matches “{query}”.
          </li>
        )}

        {results.map((player, index) => {
          const alreadyPicked =
            pickedIds.has(player.elementId) && selected?.elementId !== player.elementId;
          const clubFull =
            fullClubs.has(player.teamFplId) && selected?.teamFplId !== player.teamFplId;
          const tooExpensive = player.priceTenths > affordableTenths;
          const isActive = index === active;

          return (
            <li
              key={player.elementId}
              id={`${baseId}-option-${player.elementId}`}
              role="option"
              aria-selected={isActive}
              aria-disabled={alreadyPicked || undefined}
              onMouseEnter={() => setActiveIndex(index)}
              onMouseDown={(event) => {
                event.preventDefault();
                choose(player);
              }}
              className={[
                "flex cursor-pointer items-center gap-2 px-3 py-2 text-sm",
                isActive ? "bg-sky-50 dark:bg-sky-950/60" : "",
                alreadyPicked ? "cursor-not-allowed opacity-50" : "",
              ].join(" ")}
            >
              <span
                className="inline-flex h-6 w-10 shrink-0 items-center justify-center rounded bg-slate-100 text-[11px] font-semibold text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                aria-hidden="true"
              >
                {player.teamShortName}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium text-slate-900 dark:text-slate-50">
                  {player.webName}
                </span>
                <span className="block truncate text-xs text-slate-500 dark:text-slate-400">
                  {player.teamName}
                  {typeof player.totalPoints === "number"
                    ? ` · ${player.totalPoints} pts`
                    : ""}
                  {alreadyPicked ? " · already in your squad" : ""}
                  {!alreadyPicked && clubFull
                    ? ` · ${MAX_PER_CLUB} ${player.teamShortName} already`
                    : ""}
                </span>
              </span>
              <span
                className={[
                  "shrink-0 text-sm tabular-nums",
                  tooExpensive
                    ? "text-rose-600 dark:text-rose-400"
                    : "text-slate-700 dark:text-slate-200",
                ].join(" ")}
              >
                {formatPrice(player.priceTenths)}
              </span>
            </li>
          );
        })}

        {hiddenCount > 0 && (
          <li className="px-3 py-2 text-xs text-slate-500 dark:text-slate-400">
            {hiddenCount} more — keep typing to narrow it down.
          </li>
        )}
      </ul>
    </div>
  );
}
