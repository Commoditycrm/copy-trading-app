import { Spinner } from "@/components/Spinner";

/** Centered full-route loading view.
 *
 *  Pages that fetch data on mount use this as an early-return while the
 *  initial fetch is in flight, so users don't see a flash of empty-state
 *  UI followed by a layout jump when the data lands. The container fills
 *  the available height (the AppShell main slot is a flex column), so
 *  the spinner sits in the visual center of the content area rather than
 *  glued to the top. ``label`` is rendered into ``aria-label`` only —
 *  screen readers still announce a busy state without a visible "Loading"
 *  string cluttering the UI.
 *
 *  Usage::
 *
 *      if (loading) return <PageLoading />;
 *      // …regular render
 */
export function PageLoading({ label = "Loading" }: { label?: string }) {
  // Spinner is sized in `em` (0.85em). Bumping the wrapper's font-size
  // gives us a larger page-level spinner without touching the in-button
  // Spinner uses elsewhere. text-5xl = 48px → spinner ≈ 41px.
  return (
    <div
      className="flex flex-1 items-center justify-center w-full min-h-[40vh] text-5xl"
      role="status"
      aria-busy
      aria-live="polite"
      aria-label={label}
      style={{ color: "var(--accent)" }}
    >
      <Spinner />
    </div>
  );
}
