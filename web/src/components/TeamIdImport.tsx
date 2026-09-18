"use client";

/**
 * Import a real squad from a public FPL team ID.
 *
 * The flow is deliberately two-step: fetch and *preview*, then commit. Nothing
 * touches the saved squad until the user has seen the fifteen players and their
 * selling prices and said yes. That is the same rule the screenshot path follows
 * (CLAUDE.md invariant 7) and it is worth just as much here — a wrong team ID
 * silently replacing someone's squad would be a bad afternoon.
 *
 * The team ID never leaves this request. It is not stored in localStorage, not
 * put in the URL, and not kept in the imported squad: what gets saved is fifteen
 * element ids and a bank, exactly as if they had been picked by hand.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useId, useMemo, useRef, useState } from "react";

import { formatPrice } from "@/lib/format";
import {
  POSITION_BY_ELEMENT_TYPE,
  SLOT_POSITIONS,
  SQUAD_SIZE,
  buildPlayerIndex,
  emptySquad,
  type ElementType,
  type Position,
  type SquadPlayer,
  type SquadState,
} from "@/lib/squad";
import { useSquad } from "@/lib/useSquad";

/** The response shape of GET /api/squad/{teamId}, narrowed to what is rendered. */
interface ImportedPlayer {
  elementId: number;
  webName: string | null;
  elementType: number | null;
  teamShortName: string | null;
  squadPosition: number | null;
  onBench: boolean;
  isCaptain: boolean;
  isViceCaptain: boolean;
  purchasePriceTenths: number;
  currentPriceTenths: number;
  sellingPriceTenths: number;
  priced: boolean;
}

interface ImportedSquad {
  season: string;
  picksAvailable: boolean;
  managerName: string | null;
  teamName: string | null;
  gw: number | null;
  activeChip: string | null;
  bankTenths: number | null;
  players: ImportedPlayer[];
  sellingTotalTenths: number | null;
  squadValueTenths: number | null;
  freeTransfers: number | null;
  freeTransfersIsEstimate: boolean;
  unpricedElements: number[];
  priceSource: "database" | "seed";
  note: string | null;
}

export interface TeamIdImportProps {
  /** The season's players, for the squad index the builder shares. */
  players: SquadPlayer[];
  dataNote?: string;
}

type Phase =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; squad: ImportedSquad };

const CHIP_LABELS: Record<string, string> = {
  wildcard: "Wildcard",
  freehit: "Free Hit",
  bboost: "Bench Boost",
  "3xc": "Triple Captain",
  manager: "Assistant Manager",
};

function positionOf(player: ImportedPlayer): Position | null {
  const type = player.elementType;
  if (type === 1 || type === 2 || type === 3 || type === 4) {
    return POSITION_BY_ELEMENT_TYPE[type as ElementType];
  }
  return null;
}

/**
 * Lay the imported fifteen into the builder's fixed slot order.
 *
 * SLOT_POSITIONS is 2 GKP, 5 DEF, 5 MID, 3 FWD, and a legal FPL squad is exactly
 * that, so this is a regrouping and not a decision. Anything that does not fit —
 * which would mean FPL changed the composition — is reported rather than dropped
 * on the floor.
 */
function toSquadState(imported: ImportedSquad): {
  squad: SquadState;
  unplaced: ImportedPlayer[];
} {
  const byPosition = new Map<Position, number[]>();
  const unplaced: ImportedPlayer[] = [];

  for (const player of imported.players) {
    const position = positionOf(player);
    if (!position) {
      unplaced.push(player);
      continue;
    }
    const list = byPosition.get(position) ?? [];
    list.push(player.elementId);
    byPosition.set(position, list);
  }

  const picks: (number | null)[] = new Array(SQUAD_SIZE).fill(null);
  SLOT_POSITIONS.forEach((position, slot) => {
    const queue = byPosition.get(position);
    const next = queue?.shift();
    picks[slot] = next ?? null;
  });

  for (const [, leftovers] of byPosition) {
    for (const elementId of leftovers) {
      const player = imported.players.find((p) => p.elementId === elementId);
      if (player) unplaced.push(player);
    }
  }

  return {
    squad: {
      picks,
      bankTenths: imported.bankTenths ?? emptySquad().bankTenths,
    },
    unplaced,
  };
}

function money(tenths: number | null): string {
  return tenths === null ? "—" : formatPrice(tenths);
}

export default function TeamIdImport({ players, dataNote }: TeamIdImportProps) {
  const router = useRouter();
  const index = useMemo(() => buildPlayerIndex(players), [players]);
  const { replaceSquad } = useSquad(index);

  const [teamId, setTeamId] = useState("");
  const [phase, setPhase] = useState<Phase>({ status: "idle" });
  const inFlight = useRef<AbortController | null>(null);
  const fieldId = useId();
  const helpId = `${fieldId}-help`;

  const submit = useCallback(
    async (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault();

      const trimmed = teamId.trim();
      if (!/^\d+$/.test(trimmed)) {
        setPhase({
          status: "error",
          message:
            "A team ID is a number — nothing else. Look at the URL when you view your own team on the FPL site.",
        });
        return;
      }

      inFlight.current?.abort();
      const controller = new AbortController();
      inFlight.current = controller;
      setPhase({ status: "loading" });

      try {
        const response = await fetch(`/api/squad/${trimmed}`, {
          signal: controller.signal,
          headers: { Accept: "application/json" },
        });

        let body: unknown = null;
        try {
          body = await response.json();
        } catch {
          body = null;
        }

        if (!response.ok) {
          const message =
            body && typeof body === "object" && typeof (body as { error?: unknown }).error === "string"
              ? (body as { error: string }).error
              : "That import did not work. Try again in a moment.";
          setPhase({ status: "error", message });
          return;
        }

        setPhase({ status: "ready", squad: body as ImportedSquad });
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setPhase({
          status: "error",
          message:
            "We could not reach the site to run that import. Check your connection and try again.",
        });
      }
    },
    [teamId],
  );

  const commit = useCallback(
    (imported: ImportedSquad) => {
      const { squad } = toSquadState(imported);
      replaceSquad(squad);
      router.push("/squad");
    },
    [replaceSquad, router],
  );

  return (
    <div className="flex flex-col gap-8">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <label
          htmlFor={fieldId}
          className="text-sm font-medium text-slate-800 dark:text-slate-100"
        >
          Your FPL team ID
        </label>
        <div className="flex flex-wrap items-start gap-3">
          <input
            id={fieldId}
            name="teamId"
            value={teamId}
            onChange={(e) => setTeamId(e.target.value)}
            inputMode="numeric"
            autoComplete="off"
            placeholder="895045"
            aria-describedby={helpId}
            className="w-48 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tabular-nums text-slate-900 outline-none transition-colors focus:border-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:focus:border-slate-400"
          />
          <button
            type="submit"
            disabled={phase.status === "loading"}
            className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
          >
            {phase.status === "loading" ? "Fetching squad…" : "Import squad"}
          </button>
        </div>
        <p id={helpId} className="max-w-2xl text-sm leading-6 text-slate-600 dark:text-slate-300">
          Sign in at the FPL site, open <em>Pick Team</em> or <em>Points</em>, and
          look at the address bar:{" "}
          <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-800 dark:bg-slate-800 dark:text-slate-200">
            fantasy.premierleague.com/entry/<strong>895045</strong>/event/4
          </code>
          . The number after <code className="text-xs">/entry/</code> is your team
          ID. It is public, so this works without signing in here — and we never
          store it.
        </p>
        {dataNote && (
          <p className="text-xs text-slate-500 dark:text-slate-400">{dataNote}</p>
        )}
      </form>

      {phase.status === "loading" && (
        <p
          role="status"
          className="rounded-lg border border-slate-200 px-4 py-3 text-sm text-slate-600 dark:border-slate-800 dark:text-slate-300"
        >
          Reading the transfer log and working out what each player sells for…
        </p>
      )}

      {phase.status === "error" && (
        <p
          role="alert"
          className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200"
        >
          {phase.message}
        </p>
      )}

      {phase.status === "ready" && (
        <SquadPreview
          imported={phase.squad}
          knownIds={index}
          onUse={() => commit(phase.squad)}
        />
      )}
    </div>
  );
}

function SquadPreview({
  imported,
  knownIds,
  onUse,
}: {
  imported: ImportedSquad;
  knownIds: ReadonlyMap<number, SquadPlayer>;
  onUse: () => void;
}) {
  const { unplaced } = useMemo(() => toSquadState(imported), [imported]);

  if (!imported.picksAvailable) {
    return (
      <section className="flex flex-col gap-3 rounded-xl border border-amber-300 bg-amber-50 p-5 dark:border-amber-900 dark:bg-amber-950/30">
        <h2 className="text-sm font-semibold text-amber-900 dark:text-amber-100">
          {imported.teamName ?? "That team"} — no squad to show yet
        </h2>
        <p className="text-sm leading-6 text-amber-900 dark:text-amber-100">
          {imported.note ??
            "FPL keeps a squad private until that gameweek's deadline passes."}
        </p>
        {imported.squadValueTenths !== null && (
          <p className="text-sm text-amber-900 dark:text-amber-100">
            At the last deadline the team was worth{" "}
            <span className="tabular-nums">{money(imported.squadValueTenths)}</span>{" "}
            with <span className="tabular-nums">{money(imported.bankTenths)}</span> in
            the bank.
          </p>
        )}
        <p className="text-sm text-amber-900 dark:text-amber-100">
          You can still{" "}
          <Link href="/squad" className="underline underline-offset-2">
            build the squad by hand
          </Link>
          .
        </p>
      </section>
    );
  }

  const missingFromPool = imported.players.filter((p) => !knownIds.has(p.elementId));

  return (
    <section className="flex flex-col gap-4">
      <header className="flex flex-col gap-1">
        <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
          {imported.teamName ?? "Squad"}
          {imported.managerName && (
            <span className="font-normal text-slate-500 dark:text-slate-400">
              {" "}
              · {imported.managerName}
            </span>
          )}
        </h2>
        <p className="text-sm text-slate-600 dark:text-slate-300">
          Gameweek {imported.gw}
          {imported.activeChip &&
            ` · ${CHIP_LABELS[imported.activeChip] ?? imported.activeChip} played`}{" "}
          · {imported.players.length} players. Nothing is saved until you press
          the button below.
        </p>
      </header>

      <div className="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-800">
        <table className="w-full min-w-[34rem] border-collapse text-sm">
          <thead>
            <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500 dark:border-slate-800 dark:text-slate-400">
              <th scope="col" className="px-3 py-2 font-medium">Player</th>
              <th scope="col" className="px-3 py-2 font-medium">Club</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Bought</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Now</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Sells for</th>
            </tr>
          </thead>
          <tbody>
            {imported.players.map((player) => (
              <tr
                key={player.elementId}
                className="border-b border-slate-100 last:border-0 dark:border-slate-800/60"
              >
                <td className="px-3 py-2 text-slate-900 dark:text-slate-100">
                  <span className={player.onBench ? "text-slate-500 dark:text-slate-400" : ""}>
                    {player.webName ?? `Player #${player.elementId}`}
                  </span>
                  {player.isCaptain && <Badge>C</Badge>}
                  {player.isViceCaptain && <Badge>V</Badge>}
                  {player.onBench && <Badge>Bench</Badge>}
                </td>
                <td className="px-3 py-2 text-slate-600 dark:text-slate-300">
                  {player.teamShortName ?? "—"}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-600 dark:text-slate-300">
                  {money(player.purchasePriceTenths)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-600 dark:text-slate-300">
                  {money(player.currentPriceTenths)}
                </td>
                <td className="px-3 py-2 text-right font-medium tabular-nums text-slate-900 dark:text-slate-100">
                  {money(player.sellingPriceTenths)}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t border-slate-200 text-slate-700 dark:border-slate-800 dark:text-slate-200">
              <td className="px-3 py-2 font-medium" colSpan={4}>
                Selling total
              </td>
              <td className="px-3 py-2 text-right font-semibold tabular-nums">
                {money(imported.sellingTotalTenths)}
              </td>
            </tr>
            <tr className="text-slate-700 dark:text-slate-200">
              <td className="px-3 py-2 font-medium" colSpan={4}>
                Bank
              </td>
              <td className="px-3 py-2 text-right tabular-nums">
                {money(imported.bankTenths)}
              </td>
            </tr>
            <tr className="text-slate-700 dark:text-slate-200">
              <td className="px-3 py-2 font-medium" colSpan={4}>
                Squad value
              </td>
              <td className="px-3 py-2 text-right font-semibold tabular-nums">
                {money(imported.squadValueTenths)}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>

      <ul className="flex flex-col gap-1 text-xs leading-5 text-slate-500 dark:text-slate-400">
        <li>
          Selling price is what you paid plus half of any rise, rounded down. A
          fall is passed on in full.
        </li>
        <li>
          Prices are today&apos;s
          {imported.priceSource === "seed" ? " (from the bundled sample data)" : ""}, so
          this will not match the value FPL showed at an older deadline — that gap
          is price movement, not an error.
        </li>
        {imported.freeTransfers !== null && (
          <li>
            Free transfers: {imported.freeTransfers} — estimated from the public
            transfer log, which does not include every chip.
          </li>
        )}
        {missingFromPool.length > 0 && (
          <li className="text-amber-700 dark:text-amber-300">
            {missingFromPool.length} of these players are not in our current
            player list, so the builder will ask you to replace them. That usually
            means our data needs a sync.
          </li>
        )}
        {unplaced.length > 0 && (
          <li className="text-amber-700 dark:text-amber-300">
            {unplaced.length} players did not fit the standard 2-5-5-3 squad shape
            and were left out of the hand-off.
          </li>
        )}
      </ul>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={onUse}
          className="rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
        >
          Use this squad
        </button>
        <span className="text-xs text-slate-500 dark:text-slate-400">
          This replaces whatever squad is currently saved in this browser.
        </span>
      </div>
    </section>
  );
}

function Badge({ children }: { children: React.ReactNode }) {
  return (
    <span className="ml-1.5 rounded border border-slate-300 px-1 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400">
      {children}
    </span>
  );
}
