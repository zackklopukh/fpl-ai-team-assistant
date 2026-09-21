"use client";

/**
 * The manual squad builder: fifteen slots, running totals, live validation and
 * a shareable link.
 *
 * Everything here is client-side by design. The squad lives in localStorage and
 * in the URL fragment and nowhere else — no account, no server-side store, and
 * nothing about the person using it ever leaves the browser (CLAUDE.md
 * invariant 4).
 *
 * Club names and three-letter codes only: no crests, kits or league marks.
 */

import { useEffect, useMemo, useState } from "react";

import AdvicePanel from "@/components/AdvicePanel";
import Pitch from "@/components/Pitch";
import PlayerPicker from "@/components/PlayerPicker";
import { formatPrice } from "@/lib/format";
import type { NextFixturesByTeam } from "@/lib/nextFixtures";
import {
  BUDGET_TENTHS,
  MAX_PER_CLUB,
  POSITIONS,
  POSITION_LABEL,
  SLOT_POSITIONS,
  SQUAD_QUOTA,
  SQUAD_SIZE,
  buildPlayerIndex,
  squadHash,
  validateSquad,
  type Position,
  type SquadPlayer,
} from "@/lib/squad";
import { useSquad } from "@/lib/useSquad";

export interface SquadBuilderProps {
  players: readonly SquadPlayer[];
  /** Shown so a user knows how fresh the prices are. */
  dataNote?: string;
  /**
   * The gameweek to plan from. Optional: when the page does not supply one the
   * advice panel starts at gameweek 1 and the user can correct it.
   */
  currentGw?: number | null;
  /**
   * Each club's opponents in the next gameweek, for the cards. A club mapped to
   * [] has a blank; one mapped to two entries has a double.
   */
  nextFixtures?: NextFixturesByTeam;
  /** The gameweek `nextFixtures` describes, for the caption under the pitch. */
  nextGw?: number | null;
}

/** "5.5" -> 55. The one direction pounds are allowed to travel, and only here. */
function parseTenths(text: string): number | null {
  const trimmed = text.trim().replace(/^£/, "");
  if (!/^-?\d{0,4}(\.\d)?$/.test(trimmed) || trimmed === "" || trimmed === "-") {
    return null;
  }
  const negative = trimmed.startsWith("-");
  const [whole, tenth] = trimmed.replace("-", "").split(".");
  const value = Number(whole || "0") * 10 + Number(tenth ?? "0");
  return negative ? -value : value;
}

function tenthsToInput(tenths: number): string {
  const sign = tenths < 0 ? "-" : "";
  const abs = Math.abs(tenths);
  return `${sign}${Math.floor(abs / 10)}.${abs % 10}`;
}

export default function SquadBuilder({
  players,
  dataNote,
  currentGw,
  nextFixtures = {},
  nextGw,
}: SquadBuilderProps) {
  const index = useMemo(() => buildPlayerIndex(players), [players]);

  // Which pitch slot the picker below is editing. `openCount` is part of the
  // picker's key so that every card click remounts it straight into search —
  // including a second click on the same card after the picker was closed.
  const [activeSlot, setActiveSlot] = useState<number | null>(null);
  const [openCount, setOpenCount] = useState(0);

  function selectSlot(slotIndex: number) {
    setActiveSlot(slotIndex);
    setOpenCount((n) => n + 1);
  }

  const byPosition = useMemo(() => {
    const map: Record<Position, SquadPlayer[]> = { GKP: [], DEF: [], MID: [], FWD: [] };
    for (const player of players) map[player.position].push(player);
    for (const position of POSITIONS) {
      map[position].sort(
        (a, b) =>
          b.priceTenths - a.priceTenths || a.webName.localeCompare(b.webName),
      );
    }
    return map;
  }, [players]);

  const {
    squad,
    hydrated,
    notice,
    dismissNotice,
    setPick,
    setBankTenths,
    reset,
    shareUrl,
    storageAvailable,
  } = useSquad(index);

  const validation = useMemo(() => validateSquad(squad, index), [squad, index]);

  const pickedIds = useMemo(
    () => new Set(squad.picks.filter((id): id is number => id != null)),
    [squad.picks],
  );

  const fullClubs = useMemo(() => {
    const map = new Map<number, string>();
    for (const club of validation.clubCounts) {
      if (club.count >= MAX_PER_CLUB) map.set(club.teamFplId, club.teamName);
    }
    return map;
  }, [validation.clubCounts]);

  // While the bank field has focus the user's half-typed text wins; the rest of
  // the time it shows whatever the picks have made the bank.
  const [bankDraft, setBankDraft] = useState<string | null>(null);
  const bankInput = bankDraft ?? tenthsToInput(squad.bankTenths);

  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2500);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copyLink() {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
    } catch {
      // Clipboard permission denied, or an insecure origin. The link is on
      // screen in a selectable field, so there is always a manual way out.
      setCopied(false);
      window.prompt("Copy this link", shareUrl);
    }
  }

  const spentOnFilled = validation.costTenths;

  return (
    <div className="flex flex-col gap-6">
      {notice && (
        <div
          role="status"
          className="flex items-start justify-between gap-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200"
        >
          <p>{notice.message}</p>
          <button
            type="button"
            onClick={dismissNotice}
            className="shrink-0 rounded px-2 py-0.5 text-xs font-medium underline focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Running totals. Sticky from `sm` up, where it is one slim row. On a
          phone it wraps to two rows plus club chips — tall enough to cover the
          pitch entirely while scrolling through it — so there it scrolls away. */}
      <section
        aria-label="Squad totals"
        className="z-10 rounded-lg border border-slate-200 bg-white/95 p-4 backdrop-blur sm:sticky sm:top-0 dark:border-slate-800 dark:bg-slate-950/95"
      >
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Squad cost
            </dt>
            <dd className="text-lg font-semibold tabular-nums text-slate-900 dark:text-slate-50">
              {formatPrice(spentOnFilled)}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              In the bank
            </dt>
            <dd
              className={[
                "text-lg font-semibold tabular-nums",
                squad.bankTenths < 0
                  ? "text-rose-600 dark:text-rose-400"
                  : "text-slate-900 dark:text-slate-50",
              ].join(" ")}
            >
              {formatPrice(squad.bankTenths)}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Squad value
            </dt>
            <dd className="text-lg font-semibold tabular-nums text-slate-900 dark:text-slate-50">
              {formatPrice(spentOnFilled + squad.bankTenths)}
            </dd>
          </div>
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Players
            </dt>
            <dd className="text-lg font-semibold tabular-nums text-slate-900 dark:text-slate-50">
              {validation.filled}
              <span className="text-slate-400">/{SQUAD_SIZE}</span>
            </dd>
          </div>
        </dl>

        {validation.clubCounts.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {validation.clubCounts.map((club) => (
              <span
                key={club.teamFplId}
                title={`${club.count} ${club.teamName} ${club.count === 1 ? "player" : "players"}`}
                className={[
                  "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
                  club.count > MAX_PER_CLUB
                    ? "bg-rose-100 text-rose-800 dark:bg-rose-500/20 dark:text-rose-200"
                    : club.count === MAX_PER_CLUB
                      ? "bg-amber-100 text-amber-900 dark:bg-amber-500/20 dark:text-amber-200"
                      : "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
                ].join(" ")}
              >
                {club.teamShortName}
                <span className="tabular-nums">{club.count}</span>
              </span>
            ))}
          </div>
        )}
      </section>

      {/* Live validation, not on submit: the form has fifteen chances to go wrong. */}
      <section aria-live="polite" aria-label="Squad problems" className="flex flex-col gap-2">
        {validation.valid && (
          <p className="rounded-lg border border-emerald-300 bg-emerald-50 px-4 py-3 text-sm text-emerald-900 dark:border-emerald-500/40 dark:bg-emerald-500/10 dark:text-emerald-200">
            Legal squad — 2 goalkeepers, 5 defenders, 5 midfielders, 3 forwards, no
            more than {MAX_PER_CLUB} from a club, {formatPrice(squad.bankTenths)} left
            in the bank.
          </p>
        )}
        {validation.issues.map((issue, i) => (
          <p
            key={`${issue.code}-${i}`}
            className={[
              "rounded-lg border px-4 py-3 text-sm",
              issue.severity === "error"
                ? "border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-500/40 dark:bg-rose-500/10 dark:text-rose-200"
                : "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200",
            ].join(" ")}
          >
            {issue.message}
          </p>
        ))}
      </section>

      {/* The fifteen, on a pitch. Click a card to change that slot. */}
      <section aria-label="Your squad" className="flex flex-col gap-3">
        <Pitch
          squad={squad}
          index={index}
          nextFixtures={nextFixtures}
          activeSlot={activeSlot}
          onSelectSlot={selectSlot}
        />

        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500 dark:text-slate-400">
          <span className="flex gap-3 tabular-nums">
            {POSITIONS.map((position) => (
              <span key={position}>
                {position}{" "}
                <span
                  className={
                    validation.positionCounts[position] === SQUAD_QUOTA[position]
                      ? "text-emerald-700 dark:text-emerald-400"
                      : ""
                  }
                >
                  {validation.positionCounts[position]}/{SQUAD_QUOTA[position]}
                </span>
              </span>
            ))}
          </span>
          {nextGw != null && (
            <span>
              Opponents shown are for gameweek {nextGw}. — means no fixture.
            </span>
          )}
        </div>

        {/* The editor for whichever card was clicked. The picker is the same
            accessible combobox the slot list used, so search, keyboard use and
            the budget and club-limit warnings all carry over unchanged. */}
        {activeSlot == null ? (
          <p className="rounded-lg border border-dashed border-slate-300 px-4 py-3 text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400">
            Click a shirt, or an empty slot, to pick a player for it.
          </p>
        ) : (
          (() => {
            const position = SLOT_POSITIONS[activeSlot];
            const id = squad.picks[activeSlot];
            const selected = id != null ? index.get(id) ?? null : null;
            return (
              <div className="flex flex-col gap-1.5">
                <div className="flex items-baseline justify-between text-sm">
                  <span className="font-semibold text-slate-900 dark:text-slate-100">
                    {selected
                      ? `Change ${selected.webName}`
                      : `Pick a ${POSITION_LABEL[position].toLowerCase()}`}
                  </span>
                  <button
                    type="button"
                    onClick={() => setActiveSlot(null)}
                    className="rounded px-2 py-0.5 text-xs text-slate-500 hover:text-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:text-slate-400 dark:hover:text-slate-100"
                  >
                    Done
                  </button>
                </div>
                <PlayerPicker
                  key={`${activeSlot}-${openCount}`}
                  startEditing
                  slotIndex={activeSlot}
                  position={position}
                  selected={selected}
                  options={byPosition[position]}
                  pickedIds={pickedIds}
                  fullClubs={fullClubs}
                  affordableTenths={squad.bankTenths + (selected?.priceTenths ?? 0)}
                  onPick={(player) => setPick(activeSlot, player)}
                />
              </div>
            );
          })()
        )}
      </section>

      {/* The payoff: a legal fifteen turned into ranked, explained advice.
          Mounted here rather than on its own page so the squad and the answer
          stay on screen together — a recommendation you have to navigate away
          from to read is one you cannot check against your own squad. */}
      <AdvicePanel
        squad={squad}
        index={index}
        squadValid={validation.valid}
        currentGw={currentGw}
        nextFixtures={nextFixtures}
      />

      {/* Bank, share and reset. */}
      <section className="flex flex-col gap-4 rounded-lg border border-slate-200 p-4 dark:border-slate-800">
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <label
              htmlFor="bank"
              className="block text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400"
            >
              Bank (£m)
            </label>
            <input
              id="bank"
              inputMode="decimal"
              value={bankInput}
              onBlur={() => setBankDraft(null)}
              onChange={(event) => {
                setBankDraft(event.target.value);
                const tenths = parseTenths(event.target.value);
                if (tenths !== null) setBankTenths(tenths);
              }}
              className="mt-1 w-28 rounded border border-slate-300 bg-white px-2 py-1.5 text-sm tabular-nums text-slate-900 focus:border-sky-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-50"
            />
            <p className="mt-1 max-w-xs text-xs text-slate-500 dark:text-slate-400">
              Starts at {formatPrice(BUDGET_TENTHS)} and moves as you pick. Set it by
              hand to match a squad whose value has changed.
            </p>
          </div>

          <button
            type="button"
            onClick={reset}
            className="rounded border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
          >
            Start again
          </button>
        </div>

        <div className="flex flex-col gap-2">
          <label
            htmlFor="share"
            className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400"
          >
            Shareable link
          </label>
          <div className="flex flex-wrap gap-2">
            <input
              id="share"
              readOnly
              value={shareUrl ?? ""}
              onFocus={(event) => event.currentTarget.select()}
              className="min-w-0 flex-1 rounded border border-slate-300 bg-slate-50 px-2 py-1.5 font-mono text-xs text-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
            />
            <button
              type="button"
              onClick={copyLink}
              disabled={!shareUrl}
              className="rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 disabled:opacity-40 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
            >
              {copied ? "Copied" : "Copy link"}
            </button>
          </div>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            The squad is encoded in the link itself, after the <code>#</code>, so it
            is never sent to the server. Anyone you send it to opens this page with
            your squad already filled in.
            {!storageAvailable &&
              " Your browser is blocking site storage, so the link is the only way to keep this squad."}
          </p>
          {hydrated && validation.filled > 0 && (
            <p className="text-xs text-slate-400 dark:text-slate-500">
              Squad hash <code className="font-mono">{squadHash(squad)}</code> — what
              a recommendation would be logged under. It identifies the squad, never
              you.
            </p>
          )}
          {dataNote && (
            <p className="text-xs text-slate-400 dark:text-slate-500">{dataNote}</p>
          )}
        </div>
      </section>
    </div>
  );
}
