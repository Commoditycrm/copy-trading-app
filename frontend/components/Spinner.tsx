/**
 * Inline spinner used inside buttons while a request is in flight.
 * Sized in `em` so it scales with the button's font-size, and uses
 * currentColor so it matches the button's text colour automatically.
 *
 * Usage:
 *   <button disabled={busy} className="inline-flex items-center gap-1.5">
 *     <span>Save</span>
 *     {busy && <Spinner />}
 *   </button>
 */
export function Spinner() {
  return (
    <svg
      className="animate-spin"
      width="0.85em"
      height="0.85em"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" opacity="0.25" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}
