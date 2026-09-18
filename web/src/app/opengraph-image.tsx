import { ImageResponse } from "next/og";

/**
 * The card a pasted link unfurls into.
 *
 * Shareable squad links are a core feature — the URL encoding exists precisely
 * so a squad can be sent to someone — so what a link looks like in a chat
 * window is product surface, not decoration.
 *
 * Deliberately generic and typographic. CLAUDE.md forbids Premier League and
 * club trademarks, which rules out crests, kits and official logos, and anything
 * that could read as official affiliation. The disclaimer is on the card itself
 * rather than only in the footer, because the card is what most people will see
 * first and it is the one piece of this site that travels on its own.
 *
 * Nothing here reads request data, so the same image is generated for every
 * page and a squad can never leak into it.
 */

export const alt =
  "Squad Lab — an independent Fantasy Premier League squad rater and optimiser";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpengraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          background: "#020617",
          color: "#f8fafc",
          padding: "72px",
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "16px" }}>
          <div
            style={{
              width: "14px",
              height: "44px",
              background: "#38bdf8",
              borderRadius: "3px",
            }}
          />
          <div
            style={{
              fontSize: "26px",
              letterSpacing: "0.32em",
              textTransform: "uppercase",
              color: "#94a3b8",
            }}
          >
            Squad Lab
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column" }}>
          <div
            style={{
              fontSize: "84px",
              fontWeight: 700,
              lineHeight: 1.05,
              letterSpacing: "-0.03em",
            }}
          >
            Rate your squad.
          </div>
          <div
            style={{
              fontSize: "84px",
              fontWeight: 700,
              lineHeight: 1.05,
              letterSpacing: "-0.03em",
              color: "#38bdf8",
            }}
          >
            Then improve it.
          </div>
          <div
            style={{
              marginTop: "28px",
              fontSize: "32px",
              color: "#cbd5e1",
              maxWidth: "860px",
              lineHeight: 1.35,
            }}
          >
            Expected points, fixture difficulty and transfer plans for Fantasy
            Premier League. No account needed.
          </div>
        </div>

        <div
          style={{
            display: "flex",
            fontSize: "22px",
            color: "#64748b",
            borderTop: "1px solid #1e293b",
            paddingTop: "24px",
          }}
        >
          Independent project. Not affiliated with, endorsed by or associated
          with the Premier League or Fantasy Premier League.
        </div>
      </div>
    ),
    size,
  );
}
