"use client";

/**
 * Squad state: React on top of lib/squad.ts, persisted to localStorage and
 * mirrored into the URL.
 *
 * Two rules from ARCHITECTURE.md drive the shape of this file.
 *
 * "Version the localStorage schema" — the stored blob carries a version number.
 * A returning user whose saved squad predates a shape change gets a clean
 * discard and a sentence explaining it, not a crash they cannot clear.
 *
 * "No accounts, no user data" — this is the whole persistence layer. Nothing is
 * sent anywhere. The share code lives in the URL *fragment*, which browsers do
 * not put on the wire, so even a shared link never reaches the server.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { decodeSquad, encodeSquad } from "./encode";
import {
  BUDGET_TENTHS,
  SQUAD_SIZE,
  applyPick,
  emptySquad,
  type PlayerIndex,
  type SquadPlayer,
  type SquadState,
} from "./squad";

export const STORAGE_KEY = "fpl.squad";
/** Bump when the stored shape changes. A mismatch discards, it never migrates blind. */
export const STORAGE_VERSION = 1;
/** The fragment key: /squad#s=<code> */
export const URL_FRAGMENT_KEY = "s";

interface StoredSquad {
  version: number;
  picks: (number | null)[];
  bankTenths: number;
}

export interface SquadNotice {
  kind: "info" | "warning";
  message: string;
}

function isPlainSlotArray(value: unknown): value is (number | null)[] {
  return (
    Array.isArray(value) &&
    value.length === SQUAD_SIZE &&
    value.every((item) => item === null || (Number.isInteger(item) && (item as number) > 0))
  );
}

type LoadResult = { squad: SquadState | null; notice: SquadNotice | null };

/** Read localStorage defensively: it can throw, and what is in it can be anything. */
function loadFromStorage(): LoadResult & { available: boolean } {
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    // Private browsing, blocked site data, or a locked-down embedded webview.
    return {
      squad: null,
      available: false,
      notice: {
        kind: "warning",
        message:
          "Your browser is blocking site storage, so this squad will not be here when you come back. Copy the share link to keep it.",
      },
    };
  }

  if (!raw) return { squad: null, available: true, notice: null };

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {
      squad: null,
      available: true,
      notice: {
        kind: "info",
        message: "Your saved squad could not be read, so we have started a fresh one.",
      },
    };
  }

  if (typeof parsed !== "object" || parsed === null) {
    return {
      squad: null,
      available: true,
      notice: {
        kind: "info",
        message: "Your saved squad could not be read, so we have started a fresh one.",
      },
    };
  }

  const stored = parsed as Partial<StoredSquad>;

  if (stored.version !== STORAGE_VERSION) {
    return {
      squad: null,
      available: true,
      notice: {
        kind: "info",
        message:
          "Your saved squad was stored in an older format, so we have started a fresh one.",
      },
    };
  }

  if (!isPlainSlotArray(stored.picks) || !Number.isInteger(stored.bankTenths)) {
    return {
      squad: null,
      available: true,
      notice: {
        kind: "info",
        message: "Your saved squad looked damaged, so we have started a fresh one.",
      },
    };
  }

  return {
    squad: { picks: stored.picks.slice(), bankTenths: stored.bankTenths as number },
    available: true,
    notice: null,
  };
}

function readFragmentCode(): string | null {
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash) return null;
  const params = new URLSearchParams(hash);
  const code = params.get(URL_FRAGMENT_KEY);
  return code && code.trim() !== "" ? code.trim() : null;
}

function writeFragmentCode(code: string): void {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  params.set(URL_FRAGMENT_KEY, code);
  const next = `${window.location.pathname}${window.location.search}#${params.toString()}`;
  // replaceState, not push: editing a squad should not fill the back button.
  window.history.replaceState(null, "", next);
}

export interface UseSquadResult {
  squad: SquadState;
  /** False during SSR and the first paint; storage is only read after mount. */
  hydrated: boolean;
  notice: SquadNotice | null;
  dismissNotice: () => void;
  setPick: (slotIndex: number, player: SquadPlayer | null) => void;
  setBankTenths: (tenths: number) => void;
  reset: () => void;
  replaceSquad: (squad: SquadState) => void;
  /** The encoded squad, or null if it could not be encoded. */
  shareCode: string | null;
  shareUrl: string | null;
  storageAvailable: boolean;
}

export function useSquad(index: PlayerIndex): UseSquadResult {
  const [squad, setSquad] = useState<SquadState>(() => emptySquad());
  const [hydrated, setHydrated] = useState(false);
  const [notice, setNotice] = useState<SquadNotice | null>(null);
  const [storageAvailable, setStorageAvailable] = useState(true);

  // So the "could not be saved" warning appears once, not on every keystroke.
  const storageWarnedRef = useRef(false);

  // Load once, on mount. The URL wins when it has a squad in it; localStorage
  // otherwise.
  //
  // This is the one place state is set from an effect, and it has to be: both
  // sources are browser-only, so reading them during render would either break
  // server rendering or produce a hydration mismatch. It runs once, on an empty
  // dependency list, which is exactly the one-off external-read the lint rule's
  // "cascading renders" warning is not about.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    const code = readFragmentCode();
    if (code) {
      const result = decodeSquad(code);
      if (result.ok) {
        setSquad(result.squad);
        setHydrated(true);
        return;
      }
      const fallback = loadFromStorage();
      setStorageAvailable(fallback.available);
      setSquad(fallback.squad ?? emptySquad());
      setNotice({
        kind: "warning",
        message: `${result.message}${
          fallback.squad ? " We have loaded your saved squad instead." : ""
        }`,
      });
      setHydrated(true);
      return;
    }

    const loaded = loadFromStorage();
    setStorageAvailable(loaded.available);
    if (loaded.squad) setSquad(loaded.squad);
    if (loaded.notice) setNotice(loaded.notice);
    setHydrated(true);
  }, []);
  /* eslint-enable react-hooks/set-state-in-effect */

  const shareCode = useMemo(() => {
    try {
      return encodeSquad(squad);
    } catch {
      return null;
    }
  }, [squad]);

  const shareUrl = useMemo(() => {
    // Only after mount: the server has no origin to build the link from, and
    // rendering one there would mismatch on hydration.
    if (!hydrated || !shareCode || typeof window === "undefined") return null;
    const base = `${window.location.origin}${window.location.pathname}`;
    return `${base}#${URL_FRAGMENT_KEY}=${shareCode}`;
  }, [hydrated, shareCode]);

  /**
   * Write the squad to storage and to the fragment.
   *
   * This runs on commit rather than in an effect on purpose: the write is a side
   * effect of an edit, not of a render, and doing it here means a failure can be
   * reported straight away instead of on the next pass.
   */
  const persist = useCallback((next: SquadState) => {
    try {
      const stored: StoredSquad = {
        version: STORAGE_VERSION,
        picks: next.picks,
        bankTenths: next.bankTenths,
      };
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
    } catch {
      // Quota, private browsing, or site data blocked. The app keeps working.
      if (!storageWarnedRef.current) {
        storageWarnedRef.current = true;
        setStorageAvailable(false);
        setNotice({
          kind: "warning",
          message:
            "This squad could not be saved in your browser. Copy the share link to keep it.",
        });
      }
    }

    try {
      writeFragmentCode(encodeSquad(next));
    } catch {
      // An unencodable squad simply leaves the fragment as it was.
    }
  }, []);

  const commit = useCallback(
    (next: SquadState) => {
      setSquad(next);
      persist(next);
    },
    [persist],
  );

  const setPick = useCallback(
    (slotIndex: number, player: SquadPlayer | null) => {
      commit(applyPick(squad, slotIndex, player, index));
    },
    [commit, squad, index],
  );

  const setBankTenths = useCallback(
    (tenths: number) => {
      const value = Number.isFinite(tenths) ? Math.round(tenths) : 0;
      commit({ ...squad, bankTenths: value });
    },
    [commit, squad],
  );

  const replaceSquad = useCallback(
    (next: SquadState) => {
      commit({ picks: next.picks.slice(), bankTenths: next.bankTenths });
    },
    [commit],
  );

  const reset = useCallback(() => {
    commit(emptySquad());
    setNotice(null);
  }, [commit]);

  const dismissNotice = useCallback(() => setNotice(null), []);

  return {
    squad,
    hydrated,
    notice,
    dismissNotice,
    setPick,
    setBankTenths,
    reset,
    replaceSquad,
    shareCode,
    shareUrl,
    storageAvailable,
  };
}

export { BUDGET_TENTHS };
