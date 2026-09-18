import type { MetadataRoute } from "next";

import { isIndexable, siteUrl } from "@/lib/env";

/**
 * Preview deployments and local runs must not be indexed — every Vercel preview
 * gets its own hostname, and a search engine finding one produces duplicate
 * content pointing at a deployment that will be deleted. `isIndexable` is true
 * only for a production deployment with a real hostname.
 *
 * `/api/` is disallowed everywhere: the team-ID import route is rate-limited
 * per caller, and a crawler walking it burns that budget against the FPL API
 * for nobody's benefit.
 */
export default function robots(): MetadataRoute.Robots {
  if (!isIndexable) {
    return { rules: [{ userAgent: "*", disallow: "/" }] };
  }

  return {
    rules: [
      {
        userAgent: "*",
        allow: "/",
        disallow: ["/api/"],
      },
    ],
    sitemap: `${siteUrl}/sitemap.xml`,
    host: siteUrl,
  };
}
