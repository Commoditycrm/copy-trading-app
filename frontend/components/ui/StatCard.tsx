import { ReactNode } from "react";

interface StatCardProps {
  label: ReactNode;
  value: ReactNode;
  /** Secondary line (e.g. "vs yesterday"). */
  hint?: ReactNode;
  icon?: ReactNode;
  /** Signed delta with color, e.g. P&L change. */
  delta?: { value: ReactNode; tone?: "good" | "bad" | "flat" } | null;
  className?: string;
}

/**
 * Metric/stat tile — large tabular-num value with an optional colored delta.
 * Aliased as MetricCard for naming flexibility.
 */
export function StatCard({ label, value, hint, icon, delta, className = "" }: StatCardProps) {
  const deltaColor =
    delta?.tone === "good"
      ? "var(--good)"
      : delta?.tone === "bad"
        ? "var(--bad)"
        : "var(--muted)";
  return (
    <div className={`card p-4 hover-lift ${className}`}>
      <div className="flex items-center justify-between">
        <span className="text-[11px] uppercase tracking-wider text-muted">{label}</span>
        {icon && <span className="text-muted">{icon}</span>}
      </div>
      <div className="num num-lg mt-2 text-ink">{value}</div>
      <div className="flex items-center gap-2 mt-1 min-h-[18px]">
        {delta != null && (
          <span className="text-xs font-medium num" style={{ color: deltaColor }}>
            {delta.value}
          </span>
        )}
        {hint && <span className="text-xs text-muted">{hint}</span>}
      </div>
    </div>
  );
}

export { StatCard as MetricCard };
