import { ReactNode } from "react";

/** Centered empty state — icon + title + description + optional action. */
export function EmptyState({
  icon,
  title,
  description,
  action,
  className = "",
}: {
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`flex flex-col items-center justify-center text-center px-6 py-14 animate-fade-in ${className}`}
    >
      {icon && (
        <div
          className="mb-3 grid place-items-center w-12 h-12 rounded-full"
          style={{ background: "var(--panel-2)", color: "var(--muted)" }}
          aria-hidden
        >
          {icon}
        </div>
      )}
      <h3 className="text-sm font-semibold text-ink">{title}</h3>
      {description && <p className="text-xs text-muted mt-1 max-w-sm leading-relaxed">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
