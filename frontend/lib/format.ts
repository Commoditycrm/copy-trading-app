/**
 * Date / time formatting helpers — single source of truth so the whole app
 * shows dates the same way.
 *
 * Format target: "May 15, 2026, 01:30:00 AM" for full timestamps,
 *                "May 15, 2026"               for date-only fields.
 */

const DATETIME_OPTS: Intl.DateTimeFormatOptions = {
  month: "short",
  day: "numeric",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: true,
};

const DATE_OPTS: Intl.DateTimeFormatOptions = {
  month: "short",
  day: "numeric",
  year: "numeric",
};

/** Full timestamp — e.g. "May 15, 2026, 01:30:00 AM". */
export function fmtDateTime(input: string | number | Date | null | undefined): string {
  if (!input) return "—";
  const d = input instanceof Date ? input : new Date(input);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-US", DATETIME_OPTS);
}

/** Date only — e.g. "May 15, 2026". For date-only ISO strings ("2026-05-15"),
 *  pass them through unchanged-ish — we anchor to UTC midnight to avoid
 *  timezone roll-over (otherwise 2026-05-15 might render as May 14 in
 *  negative-UTC-offset zones). */
export function fmtDate(input: string | Date | null | undefined): string {
  if (!input) return "—";
  let d: Date;
  if (input instanceof Date) {
    d = input;
  } else if (/^\d{4}-\d{2}-\d{2}$/.test(input)) {
    d = new Date(input + "T00:00:00Z");
    if (Number.isNaN(d.getTime())) return input;
    return d.toLocaleDateString("en-US", { ...DATE_OPTS, timeZone: "UTC" });
  } else {
    d = new Date(input);
  }
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", DATE_OPTS);
}
