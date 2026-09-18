/**
 * Club colours for the generic shirt on player cards.
 *
 * Colours only, used descriptively. The shirt they fill is one shape drawn for
 * this site, identical for every club: a solid body with a thin collar and cuff
 * trim. It deliberately does not reproduce any club's kit design — no stripes,
 * hoops, contrasting sleeves or sashes — because the design, not the colour, is
 * what makes a kit recognisable as the club's own (CLAUDE.md: no crests, kits or
 * official marks).
 *
 * Keyed by the team `code`, never `fpl_id`. FPL renumbers `fpl_id` 1-20
 * alphabetically every season, so three promoted clubs shift everyone after
 * them and a colour map keyed by it would paint half the league wrong in August.
 * `code` is stable across seasons.
 *
 * A club missing from this map — a newly promoted one, most likely — falls back
 * to a neutral shirt rather than failing, and its short code on the chest still
 * identifies it.
 */

export interface ClubColours {
  /** Shirt body. */
  primary: string;
  /** Collar and cuff trim. */
  secondary: string;
}

const CLUB_COLOURS: Record<number, ClubColours> = {
  3: { primary: "#EF0107", secondary: "#FFFFFF" }, // Arsenal
  7: { primary: "#670E36", secondary: "#95BFE5" }, // Aston Villa
  36: { primary: "#0057B8", secondary: "#FFFFFF" }, // Brighton
  91: { primary: "#DA291C", secondary: "#000000" }, // Bournemouth
  94: { primary: "#E30613", secondary: "#FFFFFF" }, // Brentford
  8: { primary: "#034694", secondary: "#FFFFFF" }, // Chelsea
  9: { primary: "#59CBE8", secondary: "#FFFFFF" }, // Coventry City
  31: { primary: "#1B458F", secondary: "#C4122E" }, // Crystal Palace
  11: { primary: "#003399", secondary: "#FFFFFF" }, // Everton
  54: { primary: "#FFFFFF", secondary: "#000000" }, // Fulham
  88: { primary: "#F5A12D", secondary: "#000000" }, // Hull City
  40: { primary: "#3A64A3", secondary: "#FFFFFF" }, // Ipswich Town
  2: { primary: "#FFFFFF", secondary: "#1D428A" }, // Leeds
  14: { primary: "#C8102E", secondary: "#FFFFFF" }, // Liverpool
  43: { primary: "#6CABDD", secondary: "#FFFFFF" }, // Man City
  1: { primary: "#DA291C", secondary: "#FFFFFF" }, // Man Utd
  4: { primary: "#241F20", secondary: "#FFFFFF" }, // Newcastle
  17: { primary: "#DD0000", secondary: "#FFFFFF" }, // Nott'm Forest
  56: { primary: "#EB172B", secondary: "#FFFFFF" }, // Sunderland
  6: { primary: "#FFFFFF", secondary: "#132257" }, // Spurs
};

const NEUTRAL: ClubColours = { primary: "#94A3B8", secondary: "#F8FAFC" };

/**
 * Goalkeepers wear a shirt distinct from their outfield side, as they do on
 * the pitch. One fixed colour for every keeper, with the club's colour kept in
 * the trim so the club still reads at a glance.
 */
const GOALKEEPER_BODY = "#F4B400";

export function clubColours(teamCode: number | null | undefined): ClubColours {
  if (teamCode == null) return NEUTRAL;
  return CLUB_COLOURS[teamCode] ?? NEUTRAL;
}

export function shirtColours(
  teamCode: number | null | undefined,
  isGoalkeeper: boolean,
): ClubColours {
  const club = clubColours(teamCode);
  if (!isGoalkeeper) return club;
  return { primary: GOALKEEPER_BODY, secondary: club.primary };
}

/**
 * Black or white, whichever reads better on `hex`. Uses WCAG relative
 * luminance, so a pale sky blue gets dark text and a claret gets light text.
 */
export function readableTextOn(hex: string): "#0F172A" | "#FFFFFF" {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return "#0F172A";
  const n = parseInt(m[1], 16);
  const channel = (v: number) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  const luminance =
    0.2126 * channel((n >> 16) & 0xff) +
    0.7152 * channel((n >> 8) & 0xff) +
    0.0722 * channel(n & 0xff);
  // The crossover where white and near-black text have equal contrast.
  return luminance > 0.179 ? "#0F172A" : "#FFFFFF";
}
