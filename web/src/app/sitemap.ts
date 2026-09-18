import type { MetadataRoute } from "next";

import { siteUrl } from "@/lib/env";

/**
 * Static routes are listed literally. Player pages are added when the player
 * list can be read, and skipped when it cannot.
 *
 * That last part is deliberate. This file runs at build time, when there may be
 * no database — CI builds without one and so does a fresh clone — and a sitemap
 * is not worth failing a deploy over. The import is dynamic and the whole thing
 * is wrapped, so the worst case is a sitemap with five URLs instead of seven
 * hundred.
 *
 * Squad URLs are never listed. A squad is a shareable link, not a public page,
 * and the encoded squad lives in the fragment where a crawler cannot see it —
 * which is the correct arrangement and should stay that way.
 */

const STATIC_ROUTES: Array<{
  path: string;
  changeFrequency: MetadataRoute.Sitemap[number]["changeFrequency"];
  priority: number;
}> = [
  { path: "/", changeFrequency: "daily", priority: 1 },
  { path: "/players", changeFrequency: "daily", priority: 0.9 },
  { path: "/fixtures", changeFrequency: "daily", priority: 0.7 },
  { path: "/squad", changeFrequency: "weekly", priority: 0.6 },
  { path: "/import", changeFrequency: "monthly", priority: 0.4 },
];

async function playerRoutes(
  lastModified: Date,
): Promise<MetadataRoute.Sitemap> {
  try {
    const { getPlayers } = await import("@/lib/db");
    const players = await getPlayers();
    return players.map((player) => ({
      url: `${siteUrl}/players/${player.element_id}`,
      lastModified,
      changeFrequency: "daily" as const,
      priority: 0.5,
    }));
  } catch (err) {
    console.warn(
      "[sitemap] Player pages omitted:",
      err instanceof Error ? err.message : String(err),
    );
    return [];
  }
}

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const lastModified = new Date();

  return [
    ...STATIC_ROUTES.map((route) => ({
      url: `${siteUrl}${route.path === "/" ? "" : route.path}`,
      lastModified,
      changeFrequency: route.changeFrequency,
      priority: route.priority,
    })),
    ...(await playerRoutes(lastModified)),
  ];
}
