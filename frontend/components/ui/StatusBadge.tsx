import { ReactNode } from "react";

type Tone = "good" | "bad" | "warn" | "accent" | "muted";

const TONE: Record<Tone, { bg: string; fg: string; bd: string }> = {
  good: { bg: "var(--good-soft)", fg: "var(--good)", bd: "rgba(34,197,94,0.30)" },
  bad: { bg: "var(--bad-soft)", fg: "var(--bad)", bd: "rgba(239,68,68,0.30)" },
  warn: { bg: "rgba(255,200,87,0.12)", fg: "var(--warn)", bd: "rgba(255,200,87,0.32)" },
  accent: { bg: "var(--accent-glow)", fg: "var(--accent-2)", bd: "rgba(10,115,168,0.35)" },
  muted: { bg: "var(--panel)", fg: "var(--muted)", bd: "var(--border)" },
};

// Tolerant mapping — handles order statuses AND broker connection statuses,
// any case, with/without underscores.
const STATUS_TONE: Record<string, Tone> = {
  // order lifecycle
  filled: "good",
  completed: "good",
  partially_filled: "warn",
  partial_fill: "warn",
  pending: "warn",
  pending_new: "warn",
  accepted: "warn",
  submitted: "warn",
  new: "warn",
  cancelled: "muted",
  canceled: "muted",
  expired: "muted",
  rejected: "bad",
  failed: "bad",
  error: "bad",
  retry_pending: "warn",
  // broker / listener connection
  connected: "good",
  active: "good",
  connecting: "warn",
  reconnecting: "warn",
  disconnected: "muted",
  credentials_invalid: "bad",
};

function labelize(s: string): string {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Status pill for orders / broker connections. Always renders a text label
 * (the dot is decorative, aria-hidden) so it's never icon-only.
 */
export function StatusBadge({
  status,
  label,
  dot = true,
  icon,
  className = "",
}: {
  status: string;
  label?: ReactNode;
  dot?: boolean;
  icon?: ReactNode;
  className?: string;
}) {
  const key = (status || "").toLowerCase().trim();
  const tone = STATUS_TONE[key] ?? "muted";
  const c = TONE[tone];
  const text = label ?? labelize(key || "unknown");
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-chip text-[11px] font-medium whitespace-nowrap ${className}`}
      style={{ background: c.bg, color: c.fg, border: `1px solid ${c.bd}` }}
    >
      {icon ??
        (dot && (
          <span
            className="inline-block rounded-full"
            style={{ width: 6, height: 6, background: c.fg }}
            aria-hidden
          />
        ))}
      {text}
    </span>
  );
}
