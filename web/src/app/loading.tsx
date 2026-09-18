/**
 * The root loading state, shown while a server component streams.
 *
 * The point of this file is *not* a spinner. It is that the skeleton occupies
 * the same vertical space as the page that replaces it, so the content does not
 * jump when it arrives. Every block below has an explicit height, and the
 * container has the same max width and padding as the real pages. Change those
 * and this starts causing the layout shift it exists to prevent.
 *
 * `animate-pulse` is the only animation, and it disappears under
 * `prefers-reduced-motion` via the media query in the wrapper class list.
 */

function Line({ className = "" }: { className?: string }) {
  return (
    <div
      className={`rounded bg-slate-200 dark:bg-slate-800 ${className}`}
      aria-hidden="true"
    />
  );
}

export default function Loading() {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-busy="true"
      className="mx-auto w-full max-w-6xl px-4 py-10 sm:px-6 lg:px-8 motion-safe:animate-pulse"
    >
      <span className="sr-only">Loading…</span>

      {/* Page heading: h-8 matches a text-2xl/3xl title plus its leading. */}
      <Line className="h-8 w-56" />
      <Line className="mt-3 h-4 w-80 max-w-full" />

      {/* Filter/summary strip. */}
      <div className="mt-8 flex flex-wrap gap-3">
        <Line className="h-9 w-40" />
        <Line className="h-9 w-28" />
        <Line className="h-9 w-28" />
      </div>

      {/* Twelve rows at the same 3.25rem the player table uses. */}
      <div className="mt-6 divide-y divide-slate-200 border-y border-slate-200 dark:divide-slate-800 dark:border-slate-800">
        {Array.from({ length: 12 }, (_, i) => (
          <div key={i} className="flex h-13 items-center gap-4">
            <Line className="h-4 w-4 shrink-0 rounded-full" />
            <Line className="h-4 w-44 max-w-[45%]" />
            <Line className="ml-auto h-4 w-12" />
            <Line className="hidden h-4 w-12 sm:block" />
            <Line className="hidden h-4 w-12 md:block" />
          </div>
        ))}
      </div>
    </div>
  );
}
