import { ReactNode } from "react";

type Level = "warn" | "danger" | "info";

const STYLES: Record<Level, { bg: string; bd: string; fg: string; icon: string }> = {
  warn: { bg: "rgba(255,200,87,0.10)", bd: "rgba(255,200,87,0.35)", fg: "var(--warn)", icon: "⚠" },
  danger: { bg: "var(--bad-soft)", bd: "rgba(239,68,68,0.35)", fg: "var(--bad)", icon: "⛔" },
  info: { bg: "var(--accent-glow)", bd: "rgba(10,115,168,0.35)", fg: "var(--accent-2)", icon: "ℹ" },
};

/**
 * Prominent risk/warning callout — for copy-trading risk notices, broker
 * connection problems, destructive-action warnings. role="alert" so screen
 * readers announce it; never icon-only (always has text).
 */
export function RiskWarningBanner({
  level = "warn",
  title,
  children,
  action,
  className = "",
}: {
  level?: Level;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  const s = STYLES[level];
  return (
    <div
      role="alert"
      className={`flex items-start gap-3 px-4 py-3 rounded-token text-sm animate-fade-in ${className}`}
      style={{ background: s.bg, border: `1px solid ${s.bd}` }}
    >
      <span aria-hidden className="text-base leading-5" style={{ color: s.fg }}>
        {s.icon}
      </span>
      <div className="min-w-0 flex-1" style={{ color: "var(--text-2)" }}>
        {title && (
          <div className="font-semibold" style={{ color: s.fg }}>
            {title}
          </div>
        )}
        {children && <div className="mt-0.5 leading-relaxed">{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
