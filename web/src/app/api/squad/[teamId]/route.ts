/**
 * GET /api/squad/{teamId} — a public manager's squad, with selling prices.
 *
 * The one route in the app that touches the FPL API, and it does so under the
 * conditions CLAUDE.md invariant 3 attaches to that exception: responses cached
 * for an hour (lib/fpl.ts) and rate-limited per team id (lib/rateLimit.ts).
 * Current prices come from our own database, never from a live bootstrap call.
 *
 * Privacy (CLAUDE.md invariant 4): the team id is the user's and identifies
 * them. It is read from the path, used for the length of the request, and
 * forgotten. It is not written to the database, not logged, not put in the
 * recommendation log, and not echoed back in the response body.
 *
 * Errors are answered in plain sentences. An upstream body is never forwarded —
 * it may be an HTML block page, and it is not the user's problem either way.
 */

import { NextResponse } from "next/server";

import { SEASON, dataSource, getPlayers } from "@/lib/db";
import { DatabaseUnavailableError } from "@/lib/pg";
import { FplError, fetchManagerPayloads } from "@/lib/fpl";
import { squadImportLimiter } from "@/lib/rateLimit";
import {
  MissingPriceError,
  buildPriceList,
  reconstructSquad,
  type PriceRow,
  type ReconstructedSquad,
} from "@/lib/reconstruct";
import type { PlayerWithTeam } from "@/lib/types";

// pg needs Node, and this route is per-user by definition: never prerendered.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** FPL entry ids are small positive integers; this is a generous ceiling. */
const MAX_TEAM_ID = 99_999_999;

export interface SquadImportResponse extends ReconstructedSquad {
  /** Whether prices came from the database or the checked-in seed. */
  priceSource: "database" | "seed";
  /** A sentence for the user when there is nothing to show. */
  note: string | null;
}

interface ErrorBody {
  error: string;
}

function fail(status: number, error: string, headers?: HeadersInit) {
  return NextResponse.json<ErrorBody>({ error }, { status, headers });
}

function toPriceRow(player: PlayerWithTeam): PriceRow {
  return {
    elementId: player.element_id,
    nowCostTenths: player.now_cost_tenths,
    costChangeStartTenths: player.cost_change_start_tenths,
    webName: player.web_name,
    elementType: player.element_type,
    teamFplId: player.team_fpl_id,
    teamName: player.team_name,
    teamShortName: player.team_short_name,
  };
}

export async function GET(
  _request: Request,
  context: { params: Promise<{ teamId: string }> },
) {
  const { teamId: raw } = await context.params;

  // A positive integer and nothing else. "12.5", "-3", "abc" and "007abc" are
  // all rejected here rather than becoming a doomed upstream call.
  if (!/^\d+$/.test(raw)) {
    return fail(400, "That does not look like a team ID. It is the number in the URL when you view your own team on the FPL site.");
  }
  const teamId = Number(raw);
  if (!Number.isSafeInteger(teamId) || teamId <= 0 || teamId > MAX_TEAM_ID) {
    return fail(400, "That team ID is out of range.");
  }

  const limit = squadImportLimiter.take(`squad-import:${teamId}`);
  if (!limit.allowed) {
    const seconds = Math.ceil(limit.retryAfterMs / 1000);
    return fail(
      429,
      `Too many imports for this team in a short time. Try again in about ${seconds} second${seconds === 1 ? "" : "s"}.`,
      { "Retry-After": String(seconds) },
    );
  }

  let payloads;
  try {
    payloads = await fetchManagerPayloads(teamId);
  } catch (err) {
    if (err instanceof FplError) {
      switch (err.kind) {
        case "not-found":
          return fail(404, "No FPL team has that ID. Check the number in the URL when you view your own team.");
        case "timeout":
          return fail(504, "The FPL site did not answer in time. It is usually busiest around a deadline — try again in a minute.");
        case "malformed":
          return fail(502, "The FPL site returned something we could not read. Try again shortly.");
        default:
          return fail(502, "The FPL site is not responding right now. Try again in a few minutes.");
      }
    }
    // Deliberately not logging the error object: it can carry the request URL,
    // which carries the team id.
    console.error("[api/squad] unexpected failure during import");
    return fail(500, "Something went wrong rebuilding that squad.");
  }

  // The price list comes from our database. If that is unreachable, say so —
  // valuing the squad against the 159-player sample instead is what produced
  // "missing from our price list" for real squads when the pool was full.
  let players: Awaited<ReturnType<typeof getPlayers>>;
  try {
    players = await getPlayers(SEASON);
  } catch (err) {
    if (err instanceof DatabaseUnavailableError) {
      return fail(
        503,
        "Our player database is busy or unreachable right now, so the squad cannot be valued. Try again in a minute.",
        { "Retry-After": "30" },
      );
    }
    throw err;
  }
  const prices = buildPriceList(players.map(toPriceRow));

  let squad: ReconstructedSquad;
  try {
    squad = reconstructSquad({
      prices,
      transfers: payloads.transfers,
      picks: payloads.picks,
      entry: payloads.entry,
      season: SEASON,
    });
  } catch (err) {
    if (err instanceof MissingPriceError) {
      // A squad member our player list has never heard of. Almost always stale
      // reference data or a season rollover, and pricing them at zero would be
      // worse than saying so.
      return fail(
        503,
        "One of those players is missing from our price list, so the squad cannot be valued yet. This usually clears after the next data sync.",
      );
    }
    console.error("[api/squad] reconstruction failed");
    return fail(500, "Something went wrong rebuilding that squad.");
  }

  const body: SquadImportResponse = {
    ...squad,
    priceSource: dataSource(),
    note: squad.picksAvailable
      ? null
      : payloads.gw === null
        ? "This team has no public gameweek yet — squads become visible once the first deadline passes."
        : "This team's picks are not public yet. FPL keeps a squad private until that gameweek's deadline passes.",
  };

  return NextResponse.json(body, {
    headers: {
      // Per-user data: cached in our process, never in a shared CDN.
      "Cache-Control": "private, no-store",
    },
  });
}
