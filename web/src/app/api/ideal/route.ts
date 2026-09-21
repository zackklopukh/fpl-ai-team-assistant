/**
 * POST /api/ideal — the browser's route to the ideal-fifteen solver.
 *
 * Thin, like api/optimize: it exists so `OPTIMIZER_URL` stays server-side, and
 * so the season is the app's rather than the caller's (CLAUDE.md invariant 2).
 *
 * Deliberately NOT logged to `recommendation_log`. That table evaluates advice
 * on a manager's own squad; a from-scratch team is not that, and a wildcard
 * request carries a real squad there is no reason to keep. Nothing from the
 * request is written anywhere, and no request header is read.
 *
 * Money is integer tenths all the way through.
 */

import { NextResponse } from "next/server";

import { SEASON } from "@/lib/db";
import { FAILURE_STATUS, requestIdeal, validateIdealRequest } from "@/lib/ideal";
import { optimizerBaseUrl, optimizerTimeoutMs } from "@/lib/optimizer";
import type { IdealSquadRequest } from "@/lib/optimizerTypes";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function fail(status: number, error: string) {
  return NextResponse.json({ error }, { status, headers: { "Cache-Control": "no-store" } });
}

export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return fail(400, "That request body was not JSON.");
  }

  const req = { ...(body as object), season: SEASON } as IdealSquadRequest;

  const problem = validateIdealRequest(req);
  if (problem) return fail(400, problem);

  const result = await requestIdeal(req, {
    baseUrl: optimizerBaseUrl(),
    timeoutMs: optimizerTimeoutMs(),
  });

  if (!result.ok) {
    return fail(FAILURE_STATUS[result.kind], result.message);
  }

  return NextResponse.json(result.raw, {
    headers: {
      // A wildcard answer depends on one manager's squad; the service caches
      // its own solves, so nothing is gained by a shared cache here.
      "Cache-Control": "private, no-store",
    },
  });
}
