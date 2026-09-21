"use client";

/**
 * The /ideal page's interactive half: controls, the solve, and the answer drawn
 * the way FPL draws a team.
 *
 * What ARCHITECTURE.md asks of any recommendation applies here too: the
 * reasoning sits next to the verdict, the per-gameweek working is one click
 * away, and `data_as_of` is a visible line, not a tooltip.
 *
 * Wildcard mode reports `gain_vs_hold` as the headline and stops there. Whether
 * to play the chip now or save it for a double or blank gameweek is beyond what
 * a five-gameweek window can see, so this page does not pretend to decide it.
 *
 * Privacy (CLAUDE.md invariant 4): a team ID lives in component state for the
 * length of the visit and nowhere else — not the URL, not localStorage. The
 * saved squad is read from this browser and only ever posted to the solver.
 */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import LineupPitch from "@/components/LineupPitch";
import { formatPrice } from "@/lib/format";
import {
  buildScratchRequest,
  buildWildcardRequest,
  horizonLabel,
  lineupFromResponse,
  parseBudgetInput,
  positionOfElementType,
  toBuilderSquad,
  wildcardBudgetTenths,
  wildcardChanges,
  wildcardFromImport,
  wildcardFromSavedSquad,
  type ImportedSquadForWildcard,
  type WildcardSquad,
} from "@/lib/ideal";
import type { NextFixturesByTeam } from "@/lib/nextFixtures";
import {
  DEFAULT_HORIZON,
  DEFAULT_IDEAL_BUDGET_TENTHS,
  MAX_HORIZON,
  MIN_HORIZON,
  parseIdealSquadResponse,
  type IdealPlayer,
  type IdealSquadResponse,
} from "@/lib/optimizerTypes";
import { buildPlayerIndex, type SquadPlayer } from "@/lib/squad";
import { useSquad } from "@/lib/useSquad";

export interface TeamInfo {
  name: string;
  shortName: string;
  code: number;
}

export interface IdealTeamProps {
  players: SquadPlayer[];
  teams: Record<number, TeamInfo>;
  currentGw: number | null;
  nextFixtures: NextFixturesByTeam;
  dataNote?: string | null;
}

type Mode = "scratch" | "wildcard";
type Source = "team-id" | "saved";

interface Solved {
  response: IdealSquadResponse;
  mode: Mode;
  wildcard: WildcardSquad | null;
  horizon: number;
  gw: number;
}

type Status = "idle" | "importing" | "solving" | "done" | "error";

const INPUT =
  "mt-1 rounded border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 focus:border-sky-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-50";
const LABEL = "block text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400";

/** Same as the advice panel: an absolute UTC time, not "2 hours ago". */
function formatAsOf(iso: string): string {
  const date = new Date(iso);
  if (!iso || Number.isNaN(date.getTime())) return "unknown";
  return (
    new Intl.DateTimeFormat("en-GB", {
      weekday: "short",
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "UTC",
      hour12: false,
    }).format(date) + " UTC"
  );
}

function looksStale(iso: string): boolean {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return false;
  return Date.now() - then > 12 * 60 * 60 * 1000;
}

function signed(value: number): string {
  return `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(1)}`;
}

async function readError(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => null);
  const error = (body as { error?: unknown } | null)?.error;
  return typeof error === "string" && error.trim() !== "" ? error : fallback;
}

export default function IdealTeam({
  players,
  teams,
  currentGw,
  nextFixtures,
  dataNote,
}: IdealTeamProps) {
  const router = useRouter();
  const index = useMemo(() => buildPlayerIndex(players), [players]);
  const { squad: savedSquad, hydrated, replaceSquad } = useSquad(index);

  const [mode, setMode] = useState<Mode>("scratch");
  const [budgetInput, setBudgetInput] = useState(formatPrice(DEFAULT_IDEAL_BUDGET_TENTHS).replace(/^£|m$/g, ""));
  const [source, setSource] = useState<Source>("team-id");
  const [teamId, setTeamId] = useState("");
  const [horizon, setHorizon] = useState(DEFAULT_HORIZON);
  const [differential, setDifferential] = useState(false);
  const [maxOwnership, setMaxOwnership] = useState(15);

  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [solved, setSolved] = useState<Solved | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [showWorking, setShowWorking] = useState(false);

  // One import per team ID per visit: the route is rate-limited, and changing
  // the horizon should not re-read the transfer log.
  const importCache = useRef<Map<string, { squad: WildcardSquad; teamName: string | null }>>(new Map());
  const [importedName, setImportedName] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  const busy = status === "importing" || status === "solving";
  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => setElapsed((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  const budgetTenths = parseBudgetInput(budgetInput);

  // What the saved squad would bring to a wildcard, for the approximation notice.
  const savedWildcard = useMemo(
    () => (hydrated ? wildcardFromSavedSquad(savedSquad, index) : null),
    [hydrated, savedSquad, index],
  );

  const solve = useCallback(async () => {
    if (currentGw == null) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setError(null);
    setElapsed(0);

    const options = {
      currentGw,
      horizon,
      // The route replaces this with the app's season (CLAUDE.md invariant 2).
      season: "",
      maxOwnership: differential ? maxOwnership : null,
    };

    const failWith = (message: string) => {
      if (controller.signal.aborted) return;
      setError(message);
      setStatus("error");
    };

    try {
      let wildcard: WildcardSquad | null = null;

      if (mode === "scratch") {
        if (budgetTenths === null) {
          failWith("Enter a budget in millions with at most one decimal place, between £0.0m and £200.0m — for example 100.0 or 85.5.");
          return;
        }
      } else if (source === "saved") {
        const result = wildcardFromSavedSquad(savedSquad, index);
        if (!result.ok) return failWith(result.message);
        wildcard = result.squad;
        setImportedName(null);
      } else {
        const trimmed = teamId.trim();
        if (!/^\d+$/.test(trimmed)) {
          return failWith(
            "A team ID is a number — nothing else. Look at the URL when you view your own team on the FPL site.",
          );
        }
        const cached = importCache.current.get(trimmed);
        if (cached) {
          wildcard = cached.squad;
          setImportedName(cached.teamName);
        } else {
          setStatus("importing");
          const res = await fetch(`/api/squad/${trimmed}`, {
            signal: controller.signal,
            headers: { Accept: "application/json" },
          });
          if (!res.ok) {
            return failWith(await readError(res, "That import did not work. Try again in a moment."));
          }
          const body = (await res.json()) as ImportedSquadForWildcard & { teamName?: string | null };
          const result = wildcardFromImport(body);
          if (!result.ok) return failWith(result.message);
          wildcard = result.squad;
          importCache.current.set(trimmed, { squad: wildcard, teamName: body.teamName ?? null });
          setImportedName(body.teamName ?? null);
        }
      }

      setStatus("solving");
      const request =
        mode === "scratch"
          ? buildScratchRequest(budgetTenths as number, options)
          : buildWildcardRequest(wildcard as WildcardSquad, options);

      const res = await fetch("/api/ideal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
        signal: controller.signal,
      });
      if (!res.ok) {
        return failWith(
          await readError(res, "The optimizer is not reachable right now. Your squad is safe — try again in a moment."),
        );
      }
      const response = parseIdealSquadResponse(await res.json());
      if (controller.signal.aborted) return;
      setSolved({ response, mode, wildcard, horizon: request.horizon, gw: request.current_gw });
      setStatus("done");
    } catch (err) {
      if (controller.signal.aborted) return;
      failWith(
        err instanceof Error && err.name === "MalformedResponseError"
          ? err.message
          : "The optimizer is not reachable right now. Your squad is safe — try again in a moment.",
      );
    }
  }, [
    currentGw,
    horizon,
    differential,
    maxOwnership,
    mode,
    budgetTenths,
    source,
    savedSquad,
    index,
    teamId,
  ]);

  // The default from-scratch team is the same for every visitor and the solver
  // caches it, so it is shown straight away rather than behind a button. The
  // ref makes it a request on mount only, not on every edit to the controls.
  // Its cleanup aborts it, so a remount (StrictMode does one in development)
  // asks again instead of leaving a cancelled request spinning forever.
  const solveRef = useRef(solve);
  useEffect(() => {
    solveRef.current = solve;
  }, [solve]);
  useEffect(() => {
    if (currentGw == null) return;
    void solveRef.current();
    return () => abortRef.current?.abort();
  }, [currentGw]);

  const loadIntoBuilder = useCallback(() => {
    if (!solved) return;
    replaceSquad(toBuilderSquad(solved.response));
    router.push("/squad");
  }, [solved, replaceSquad, router]);

  /** A response player as a card: our names and club colours, the solver's price. */
  const resolve = useCallback(
    (player: IdealPlayer): SquadPlayer => {
      const known = index.get(player.elementId);
      if (known) return { ...known, priceTenths: player.priceTenths };
      const team = teams[player.teamFplId];
      return {
        elementId: player.elementId,
        webName: player.webName,
        position: positionOfElementType(player.elementType) ?? "MID",
        teamFplId: player.teamFplId,
        teamName: team?.name ?? "Unknown club",
        teamShortName: team?.shortName ?? "???",
        teamCode: team?.code ?? null,
        priceTenths: player.priceTenths,
      };
    },
    [index, teams],
  );

  const nameOf = useCallback(
    (elementId: number, fallback?: string | null): string =>
      index.get(elementId)?.webName ??
      solved?.response.players.find((p) => p.elementId === elementId)?.webName ??
      fallback ??
      `Player ${elementId}`,
    [index, solved],
  );

  if (currentGw == null) {
    return (
      <p className="rounded border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200">
        There is no upcoming gameweek in the data yet, so there is nothing to
        plan for. This usually clears after the next data sync.
      </p>
    );
  }

  const response = solved?.response ?? null;
  const lineup = response ? lineupFromResponse(response) : null;
  const span = horizonLabel(response, solved?.gw ?? currentGw, solved?.horizon ?? horizon);
  const isWildcard = solved?.mode === "wildcard" && response?.gainVsHold !== null;
  const changes = response && solved?.wildcard ? wildcardChanges(response, solved.wildcard) : null;

  return (
    <div className="flex flex-col gap-6">
      {/* Controls. */}
      <section
        aria-label="What to solve for"
        className="flex flex-col gap-4 rounded-lg border border-slate-200 p-4 dark:border-slate-800"
      >
        <div role="radiogroup" aria-label="Mode" className="flex flex-wrap gap-2">
          {(
            [
              ["scratch", "From scratch", "Any fifteen a budget buys"],
              ["wildcard", "Wildcard", "Rebuild from your squad"],
            ] as const
          ).map(([value, title, sub]) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={mode === value}
              onClick={() => setMode(value)}
              className={[
                "flex min-w-[10rem] flex-1 flex-col rounded-lg border px-3 py-2 text-left transition focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 sm:flex-none",
                mode === value
                  ? "border-sky-500 bg-sky-50 dark:border-sky-400 dark:bg-sky-500/10"
                  : "border-slate-200 hover:border-slate-400 dark:border-slate-700 dark:hover:border-slate-500",
              ].join(" ")}
            >
              <span className="text-sm font-semibold text-slate-900 dark:text-slate-50">{title}</span>
              <span className="text-xs text-slate-500 dark:text-slate-400">{sub}</span>
            </button>
          ))}
        </div>

        <div className="flex flex-wrap items-end gap-4">
          {mode === "scratch" ? (
            <div>
              <label htmlFor="ideal-budget" className={LABEL}>
                Budget (£m)
              </label>
              <input
                id="ideal-budget"
                inputMode="decimal"
                value={budgetInput}
                onChange={(e) => setBudgetInput(e.target.value)}
                aria-invalid={budgetTenths === null}
                className={`${INPUT} w-24 tabular-nums`}
              />
            </div>
          ) : (
            <fieldset className="flex flex-col gap-1">
              <legend className={LABEL}>Your squad</legend>
              <div className="mt-1 flex flex-wrap gap-3 text-sm text-slate-800 dark:text-slate-100">
                <label className="flex items-center gap-1.5">
                  <input
                    type="radio"
                    name="ideal-source"
                    checked={source === "team-id"}
                    onChange={() => setSource("team-id")}
                  />
                  FPL team ID <span className="text-xs text-slate-500 dark:text-slate-400">(exact)</span>
                </label>
                <label className="flex items-center gap-1.5">
                  <input
                    type="radio"
                    name="ideal-source"
                    checked={source === "saved"}
                    onChange={() => setSource("saved")}
                  />
                  My saved squad <span className="text-xs text-slate-500 dark:text-slate-400">(approximate)</span>
                </label>
              </div>
            </fieldset>
          )}

          {mode === "wildcard" && source === "team-id" && (
            <div>
              <label htmlFor="ideal-team-id" className={LABEL}>
                Team ID
              </label>
              <input
                id="ideal-team-id"
                value={teamId}
                onChange={(e) => setTeamId(e.target.value)}
                inputMode="numeric"
                autoComplete="off"
                placeholder="895045"
                className={`${INPUT} w-32 tabular-nums`}
              />
            </div>
          )}

          <div>
            <label htmlFor="ideal-horizon" className={LABEL}>
              Gameweeks ahead
            </label>
            <select
              id="ideal-horizon"
              value={horizon}
              onChange={(e) => setHorizon(Number(e.target.value))}
              className={INPUT}
            >
              {Array.from({ length: MAX_HORIZON - MIN_HORIZON + 1 }, (_, i) => i + MIN_HORIZON).map(
                (value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ),
              )}
            </select>
          </div>

          <button
            type="button"
            onClick={() => void solve()}
            disabled={busy}
            className="rounded bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? "Working…" : mode === "scratch" ? "Find the team" : "Find the wildcard"}
          </button>
        </div>

        <p className="text-xs text-slate-500 dark:text-slate-400">
          Planning from gameweek {currentGw}.
          {mode === "scratch"
            ? " Every player costs his list price; £100.0m is what every manager starts with."
            : " The money is your bank plus what your fifteen sell for, and a player you keep costs his selling price, not his list price."}
        </p>

        {mode === "wildcard" && source === "team-id" && (
          <p className="max-w-2xl text-xs leading-5 text-slate-500 dark:text-slate-400">
            Your team ID is the number after <code>/entry/</code> in the address
            bar when you view your team on the FPL site. It is public, it is used
            for this one request, and it is not stored.
          </p>
        )}

        {mode === "wildcard" && source === "saved" && (
          <div
            data-testid="approximate-notice"
            className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm leading-6 text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200"
          >
            <p>
              <strong className="font-semibold">This budget is an approximation.</strong>{" "}
              The squad builder remembers which players you picked, not what you
              paid for them, so each one is valued at today&apos;s list price. If
              you have held anyone through a price rise, FPL will pay you only
              half of that rise, so your real budget is lower. Use your FPL team
              ID for the exact figure.
            </p>
            {savedWildcard &&
              (savedWildcard.ok ? (
                <p className="mt-1 tabular-nums">
                  Approximate budget: {formatPrice(wildcardBudgetTenths(savedWildcard.squad))} (
                  {formatPrice(savedWildcard.squad.bankTenths)} in the bank).
                </p>
              ) : (
                <p className="mt-1">{savedWildcard.message}</p>
              ))}
          </div>
        )}

        {/* Differential mode — the advice panel's control and explanation. */}
        <div className="rounded border border-slate-200 p-3 dark:border-slate-800">
          <label className="flex items-center gap-2 text-sm font-medium text-slate-800 dark:text-slate-100">
            <input
              type="checkbox"
              checked={differential}
              onChange={(e) => setDifferential(e.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-sky-600 focus-visible:ring-2 focus-visible:ring-sky-500 dark:border-slate-600"
            />
            Differential mode
          </label>
          <p className="mt-1 max-w-2xl text-xs text-slate-500 dark:text-slate-400">
            A correct optimizer converges on the template squad, because the
            template is popular precisely because it is close to optimal. Cap
            ownership to ask for an answer fewer people have.
          </p>
          {differential && (
            <div className="mt-3 flex items-center gap-3">
              <label
                htmlFor="ideal-ownership"
                className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400"
              >
                Max ownership
              </label>
              <input
                id="ideal-ownership"
                type="range"
                min={1}
                max={100}
                step={1}
                value={maxOwnership}
                onChange={(e) => setMaxOwnership(Number(e.target.value))}
                className="w-40 sm:w-48"
              />
              <span className="tabular-nums text-sm text-slate-800 dark:text-slate-100">
                {maxOwnership}%
              </span>
            </div>
          )}
        </div>

        {dataNote && <p className="text-xs text-slate-500 dark:text-slate-400">{dataNote}</p>}
      </section>

      {/* Status. A fixed-height line, so starting a solve does not shove the
          answer down the page. */}
      <div aria-live="polite" className="min-h-[2.75rem]">
        {busy && (
          <p className="rounded border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm text-slate-700 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200">
            {status === "importing"
              ? "Reading your transfer log and working out what each player sells for"
              : "Solving"}
            {elapsed > 0 ? ` — ${elapsed}s` : ""}. A longer horizon takes several
            seconds, and the first request after a quiet spell has to wake the
            solver as well.
          </p>
        )}
        {status === "error" && error && (
          <p
            role="alert"
            className="rounded border border-rose-300 bg-rose-50 px-4 py-2.5 text-sm text-rose-900 dark:border-rose-500/40 dark:bg-rose-500/10 dark:text-rose-200"
          >
            {error}
          </p>
        )}
        {status === "done" && response && (
          <p className="px-1 py-2.5 text-xs text-slate-500 dark:text-slate-400">
            {solved?.mode === "wildcard"
              ? `Wildcard from ${
                  solved.wildcard?.source === "saved"
                    ? "your saved squad (approximate selling prices)"
                    : importedName
                      ? `${importedName} (exact selling prices)`
                      : "your FPL team (exact selling prices)"
                }`
              : "From scratch"}
            {" · "}solved in {(response.solveMs / 1000).toFixed(1)}s.
          </p>
        )}
      </div>

      {/* The answer. Kept on screen, dimmed, while a new one is worked out. */}
      <div
        className={
          (busy || status === "error") && response
            ? "opacity-60 transition-opacity"
            : "transition-opacity"
        }
      >
        {status === "error" && response && (
          <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">
            Below is the last team that solved, not an answer for these settings.
          </p>
        )}
        {!response || !lineup ? (
          <PitchPlaceholder busy={busy} />
        ) : (
          <div className="flex flex-col gap-6">
            {isWildcard && response.gainVsHold !== null && (
              <section
                aria-label="Gain over holding"
                data-testid="gain-vs-hold"
                className="rounded-xl border border-sky-300 bg-sky-50 p-5 dark:border-sky-500/40 dark:bg-sky-500/10"
              >
                <p className="text-xs font-semibold uppercase tracking-wide text-sky-800 dark:text-sky-200">
                  Wildcard gain over keeping your squad, {span}
                </p>
                <p className="mt-1 text-5xl font-semibold tabular-nums tracking-tight text-slate-900 dark:text-slate-50">
                  {signed(response.gainVsHold)}
                  <span className="ml-2 text-lg font-medium text-slate-500 dark:text-slate-400">
                    expected points
                  </span>
                </p>
                <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-700 dark:text-slate-200">
                  That is how many more points this fifteen is projected to score
                  than your current squad left unchanged over the same{" "}
                  {response.perGwBreakdown.length || solved?.horizon} gameweek
                  {(response.perGwBreakdown.length || solved?.horizon) === 1 ? "" : "s"}.
                  Whether to play the wildcard now or save it for a double or
                  blank gameweek later is your call — those are further ahead than
                  this window can see.
                </p>
              </section>
            )}

            <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
              <div className="flex flex-col gap-2">
                <LineupPitch
                  lineup={lineup}
                  captain={response.captain}
                  viceCaptain={response.viceCaptain}
                  resolve={resolve}
                  nextFixtures={nextFixtures}
                  showKept={solved?.mode === "wildcard"}
                />
                <p className="text-xs leading-5 text-slate-500 dark:text-slate-400">
                  Opponents are for gameweek {solved?.gw ?? currentGw}. xP is each
                  player&apos;s expected points over {span} if he plays every game.
                  {solved?.mode === "wildcard" &&
                    " Kept players show what they cost you — their selling price — with the list price above it where the two differ."}
                </p>
                {!lineup.formationMatches && (
                  <p className="text-xs text-amber-700 dark:text-amber-300">
                    The solver said {response.formation}, but its eleven do not line
                    up that way. The pitch shows each player in his own position.
                  </p>
                )}
              </div>

              <div className="flex flex-col gap-4">
                <dl className="grid grid-cols-2 gap-x-4 gap-y-3 rounded-lg border border-slate-200 p-4 text-sm dark:border-slate-800">
                  <Stat label={`Projected, ${span}`} wide>
                    <span className="text-2xl font-semibold">{response.totalXp.toFixed(1)}</span>{" "}
                    <span className="text-slate-500 dark:text-slate-400">pts</span>
                  </Stat>
                  <Stat label="Cost">
                    {formatPrice(response.costTenths)}
                    <span className="block text-xs font-normal text-slate-500 dark:text-slate-400">
                      of {formatPrice(response.budgetTenths)}
                    </span>
                  </Stat>
                  <Stat label="Left over">{formatPrice(response.bankAfterTenths)}</Stat>
                  <Stat label="Formation">{response.formation || "—"}</Stat>
                  {solved?.mode === "wildcard" && response.keptCount !== null && (
                    <Stat label="Kept">{response.keptCount} of 15</Stat>
                  )}
                  <Stat label="Captain">{nameOf(response.captain)}</Stat>
                  <Stat label="Vice">{nameOf(response.viceCaptain)}</Stat>
                </dl>

                {response.reasoning && (
                  <section
                    aria-label="Why this team"
                    className="rounded-lg border-l-4 border-sky-500 bg-slate-50 p-4 text-sm leading-6 text-slate-800 dark:bg-slate-900 dark:text-slate-100"
                  >
                    <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                      Why this team
                    </h2>
                    <p>{response.reasoning}</p>
                  </section>
                )}

                {response.truncated && (
                  <p className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200">
                    The solver hit its time limit and returned the best team it had
                    found. It is a good one, but it is not proven best.
                  </p>
                )}
              </div>
            </div>

            {changes && solved?.wildcard && (
              <section
                aria-label="Changes"
                className="grid gap-4 rounded-lg border border-slate-200 p-4 sm:grid-cols-2 dark:border-slate-800"
              >
                <div>
                  <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-50">
                    In ({changes.incoming.length})
                  </h2>
                  <ul className="mt-2 flex flex-col gap-1 text-sm">
                    {changes.incoming.length === 0 && (
                      <li className="text-slate-500 dark:text-slate-400">Nobody — your fifteen is already the best.</li>
                    )}
                    {changes.incoming.map((p) => (
                      <li key={p.elementId} className="flex justify-between gap-3">
                        <span className="text-slate-900 dark:text-slate-100">
                          {nameOf(p.elementId, p.webName)}
                          <span className="ml-1 text-xs text-slate-500 dark:text-slate-400">
                            {resolve(p).teamShortName} · {positionOfElementType(p.elementType)}
                          </span>
                        </span>
                        <span className="tabular-nums text-slate-700 dark:text-slate-300">
                          {formatPrice(p.costTenths)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-50">
                    Out ({changes.outgoing.length})
                  </h2>
                  <ul className="mt-2 flex flex-col gap-1 text-sm">
                    {changes.outgoing.length === 0 && (
                      <li className="text-slate-500 dark:text-slate-400">Nobody.</li>
                    )}
                    {changes.outgoing.map((h) => (
                      <li key={h.elementId} className="flex justify-between gap-3">
                        <span className="text-slate-900 dark:text-slate-100">
                          {nameOf(h.elementId, h.webName)}
                        </span>
                        <span className="tabular-nums text-slate-700 dark:text-slate-300">
                          sells for {formatPrice(h.sellingPriceTenths)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
                <div className="sm:col-span-2">
                  <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-50">
                    Kept ({changes.kept.length})
                  </h2>
                  <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                    {changes.kept.length === 0
                      ? "None of your current players make the fifteen."
                      : changes.kept.map((p) => nameOf(p.elementId, p.webName)).join(", ")}
                  </p>
                  {changes.discounted.length > 0 && (
                    <ul className="mt-2 flex flex-col gap-1 text-xs text-slate-600 dark:text-slate-300">
                      {changes.discounted.map((p) => (
                        <li key={p.elementId} className="tabular-nums">
                          {nameOf(p.elementId, p.webName)} costs you{" "}
                          <strong className="font-semibold">{formatPrice(p.costTenths)}</strong> to keep
                          — his selling price — against a list price of {formatPrice(p.priceTenths)}.
                        </li>
                      ))}
                    </ul>
                  )}
                  <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                    Budget: {formatPrice(response.budgetTenths)} = {formatPrice(solved.wildcard.bankTenths)}{" "}
                    in the bank plus what your fifteen sell for
                    {solved.wildcard.approximate ? " (approximated at today's prices)" : ""}. Selling
                    price is what you paid plus half of any rise, rounded down.
                  </p>
                </div>
              </section>
            )}

            {/* The working. */}
            <section className="rounded-lg border border-slate-200 p-4 dark:border-slate-800">
              <button
                type="button"
                onClick={() => setShowWorking((v) => !v)}
                aria-expanded={showWorking}
                aria-controls="ideal-working"
                className="rounded text-sm font-medium text-sky-700 underline underline-offset-2 hover:text-sky-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 dark:text-sky-300 dark:hover:text-sky-200"
              >
                {showWorking ? "Hide the working" : "Show the working"}
              </button>
              <div id="ideal-working" hidden={!showWorking} className="mt-3 overflow-x-auto">
                <table className="w-full min-w-[26rem] text-left text-sm">
                  <thead>
                    <tr className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
                      <th scope="col" className="py-1 pr-3 font-medium">GW</th>
                      <th scope="col" className="py-1 pr-3 font-medium">Projected</th>
                      <th scope="col" className="py-1 pr-3 font-medium">Captain</th>
                      <th scope="col" className="py-1 font-medium">Fixtures</th>
                    </tr>
                  </thead>
                  <tbody>
                    {response.perGwBreakdown.map((week) => {
                      // Doubles and blanks change a gameweek's shape (CLAUDE.md
                      // invariant 6), so they are named rather than averaged away.
                      const blanks: number[] = [];
                      const doubles: number[] = [];
                      for (const id of response.xi) {
                        const n = week.nFixtures.get(id);
                        if (n === 0) blanks.push(id);
                        else if (n !== undefined && n > 1) doubles.push(id);
                      }
                      return (
                        <tr key={week.gw} className="border-t border-slate-100 dark:border-slate-800">
                          <th scope="row" className="py-1.5 pr-3 font-medium tabular-nums text-slate-900 dark:text-slate-100">
                            {week.gw}
                          </th>
                          <td className="py-1.5 pr-3 tabular-nums text-slate-700 dark:text-slate-200">
                            {week.xp.toFixed(1)}
                          </td>
                          <td className="py-1.5 pr-3 text-slate-700 dark:text-slate-200">
                            {nameOf(week.captainElementId)}
                          </td>
                          <td className="py-1.5 text-xs text-slate-600 dark:text-slate-300">
                            {blanks.length === 0 && doubles.length === 0
                              ? "One game each for the starting eleven"
                              : [
                                  doubles.length > 0 &&
                                    `Double: ${doubles.map((id) => nameOf(id)).join(", ")}`,
                                  blanks.length > 0 &&
                                    `Blank: ${blanks.map((id) => nameOf(id)).join(", ")}`,
                                ]
                                  .filter(Boolean)
                                  .join(" · ")}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                  <tfoot>
                    <tr className="border-t border-slate-200 dark:border-slate-700">
                      <th scope="row" className="py-1.5 pr-3 font-medium text-slate-900 dark:text-slate-100">
                        Total
                      </th>
                      <td className="py-1.5 pr-3 font-semibold tabular-nums text-slate-900 dark:text-slate-100">
                        {response.totalXp.toFixed(1)}
                      </td>
                      <td colSpan={2} className="py-1.5 text-xs text-slate-500 dark:text-slate-400">
                        Starting eleven plus the captain&apos;s second score, each gameweek.
                      </td>
                    </tr>
                  </tfoot>
                </table>
              </div>
            </section>

            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={loadIntoBuilder}
                disabled={busy}
                className="rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:opacity-60 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
              >
                Load this fifteen into the squad builder
              </button>
              <span className="text-xs text-slate-500 dark:text-slate-400">
                Replaces the squad saved in this browser, with{" "}
                {formatPrice(response.bankAfterTenths)} in the bank.
              </span>
            </div>

            <p
              data-testid="data-as-of"
              className={[
                "rounded border px-3 py-2 text-xs",
                looksStale(response.dataAsOf)
                  ? "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200"
                  : "border-slate-200 text-slate-500 dark:border-slate-800 dark:text-slate-400",
              ].join(" ")}
            >
              Prices and expected points as of{" "}
              <strong className="font-semibold">{formatAsOf(response.dataAsOf)}</strong>.
              {looksStale(response.dataAsOf) &&
                " Prices change overnight, so this answer may already be out of date — check before you transfer."}{" "}
              Model {response.modelVersion}, solver {response.solverVersion}, solved in{" "}
              {response.solveMs}ms.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({
  label,
  wide = false,
  children,
}: {
  label: string;
  wide?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={wide ? "col-span-2" : undefined}>
      <dt className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className="mt-0.5 font-medium tabular-nums text-slate-900 dark:text-slate-50">{children}</dd>
    </div>
  );
}

/** The empty pitch, the same height as a real one, so the answer lands in place. */
function PitchPlaceholder({ busy }: { busy: boolean }) {
  const rows = [1, 4, 4, 2];
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_18rem]" aria-hidden="true">
      <div className="overflow-hidden rounded-xl">
        <div
          className="flex flex-col gap-3 px-1 py-4 sm:gap-5 sm:px-4 sm:py-6"
          style={{
            background: "repeating-linear-gradient(180deg, #15803d 0 44px, #166534 44px 88px)",
          }}
        >
          {rows.map((n, r) => (
            <div key={r} className="flex justify-center gap-1 sm:gap-3">
              {Array.from({ length: n }, (_, i) => (
                <div
                  key={i}
                  className={`h-[88px] w-[19%] max-w-[104px] rounded-md bg-white/10 sm:h-[112px] sm:w-[17%] ${busy ? "animate-pulse" : ""}`}
                />
              ))}
            </div>
          ))}
        </div>
        <div className="flex justify-center gap-1 bg-emerald-950 px-1 pb-3 pt-7 sm:gap-3 sm:px-4">
          {[0, 1, 2, 3].map((i) => (
            <div
              key={i}
              className={`h-[88px] w-[19%] max-w-[104px] rounded-md bg-white/10 sm:h-[112px] sm:w-[17%] ${busy ? "animate-pulse" : ""}`}
            />
          ))}
        </div>
      </div>
      <div className="hidden h-64 rounded-lg border border-slate-200 lg:block dark:border-slate-800" />
    </div>
  );
}
