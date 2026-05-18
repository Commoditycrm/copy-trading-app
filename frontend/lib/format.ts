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

/** Full timestamp with milliseconds — e.g. "May 15, 2026, 01:30:00.842 AM".
 *  Used for trade rows where sub-second ordering matters. When `timeZone`
 *  is given (an IANA name like "America/New_York"), the time is rendered
 *  in that zone with a short abbreviation appended (EDT/EST). */
export function fmtDateTimeMs(
  input: string | number | Date | null | undefined,
  timeZone?: string,
): string {
  if (!input) return "—";
  const d = input instanceof Date ? input : new Date(input);
  if (Number.isNaN(d.getTime())) return "—";
  const opts: Intl.DateTimeFormatOptions = {
    ...DATETIME_OPTS,
    ...(timeZone ? { timeZone, timeZoneName: "short" } : {}),
  };
  const base = d.toLocaleString("en-US", opts);
  // The base looks like "May 15, 2026, 01:30:00 AM EDT" — insert ".NNN" after
  // the seconds and before AM/PM (and any trailing tz abbreviation).
  // We compute ms from the underlying UTC instant; the zone only shifts the
  // display, not the absolute ms count.
  const ms = String(d.getMilliseconds()).padStart(3, "0");
  return base.replace(/(\d{2}:\d{2}:\d{2})( ?[AP]M)?/, `$1.${ms}$2`);
}

/** Human-readable duration between two timestamps — e.g. "342ms", "1.2s",
 *  "2m 15s", "1h 04m". Returns "—" if either side is missing or invalid,
 *  or if the duration is negative. */
export function fmtDuration(
  start: string | number | Date | null | undefined,
  end: string | number | Date | null | undefined,
): string {
  if (!start || !end) return "—";
  const a = start instanceof Date ? start : new Date(start);
  const b = end instanceof Date ? end : new Date(end);
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return "—";
  const ms = b.getTime() - a.getTime();
  if (ms < 0) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)}s`;
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${m}m ${String(s).padStart(2, "0")}s`;
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
