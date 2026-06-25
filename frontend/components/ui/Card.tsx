import { HTMLAttributes, ReactNode } from "react";

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Adds hover-lift + pointer cursor (use for clickable cards). */
  interactive?: boolean;
  /** Default padding (p-5). Set false for tables/custom inner layout. */
  padded?: boolean;
}

/** Section container — wraps the existing `.card` surface utility. */
export function Card({
  interactive,
  padded = true,
  className = "",
  children,
  ...rest
}: CardProps) {
  return (
    <div
      className={`card ${padded ? "p-5" : ""} ${interactive ? "hover-lift cursor-pointer" : ""} ${className}`}
      {...rest}
    >
      {children}
    </div>
  );
}

/** Standard card header: title + optional subtitle on the left, action on the right. */
export function CardHeader({
  title,
  subtitle,
  action,
  className = "",
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`flex items-start justify-between gap-3 mb-4 ${className}`}>
      <div className="min-w-0">
        <h3 className="text-sm font-semibold text-ink truncate">{title}</h3>
        {subtitle && <p className="text-xs text-muted mt-0.5">{subtitle}</p>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
