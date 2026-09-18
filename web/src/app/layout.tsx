import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import { siteUrl } from "@/lib/env";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  // Without this, OG image URLs resolve against localhost and a shared squad
  // link unfurls as nothing. Sharing a squad is a core feature here, so the
  // card is part of the product rather than decoration.
  metadataBase: new URL(siteUrl),
  title: {
    default: "FPL Squad Lab",
    template: "%s · FPL Squad Lab",
  },
  description:
    "An independent Fantasy Premier League squad rater and optimiser. Not affiliated with the Premier League or Fantasy Premier League.",
  openGraph: {
    type: "website",
    siteName: "FPL Squad Lab",
    url: siteUrl,
  },
  twitter: { card: "summary_large_image" },
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col bg-white text-slate-900 dark:bg-slate-950 dark:text-slate-100">
        <header className="border-b border-slate-200 dark:border-slate-800">
          <nav className="mx-auto flex w-full max-w-6xl items-center justify-between gap-4 px-4 py-4 sm:px-6 lg:px-8">
            <Link
              href="/"
              className="text-sm font-semibold tracking-tight text-slate-900 dark:text-slate-50"
            >
              Squad Lab
            </Link>
            <div className="flex items-center gap-4 text-sm">
              <Link
                href="/players"
                className="text-slate-600 transition-colors hover:text-slate-900 dark:text-slate-300 dark:hover:text-white"
              >
                Players
              </Link>
              <Link
                href="/fixtures"
                className="text-slate-600 transition-colors hover:text-slate-900 dark:text-slate-300 dark:hover:text-white"
              >
                Fixtures
              </Link>
              <Link
                href="/squad"
                className="text-slate-600 transition-colors hover:text-slate-900 dark:text-slate-300 dark:hover:text-white"
              >
                Squad
              </Link>
              {/* The primary way in: ARCHITECTURE.md makes the API path the
                  main route and the screenshot a fallback, so this is the entry
                  point rather than a secondary feature. */}
              <Link
                href="/import"
                className="rounded-md bg-slate-900 px-3 py-1.5 font-medium text-white transition-colors hover:bg-slate-700 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
              >
                Import squad
              </Link>
            </div>
          </nav>
        </header>

        <main className="flex-1">{children}</main>

        <footer className="border-t border-slate-200 dark:border-slate-800">
          <div className="mx-auto w-full max-w-6xl px-4 py-8 text-xs leading-5 text-slate-500 sm:px-6 lg:px-8 dark:text-slate-400">
            <p>
              Squad Lab is an independent hobby project. It is not affiliated
              with, endorsed by, or associated with the Premier League or Fantasy
              Premier League. Club names are used descriptively; no club crests,
              kits or official marks appear on this site.
            </p>
            <p className="mt-2">
              Player data is sourced from publicly available Fantasy Premier
              League statistics and refreshed on a schedule.
            </p>
          </div>
        </footer>
      </body>
    </html>
  );
}
