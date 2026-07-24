"use client";

/**
 * Shared pagination control for server-paginated tables. Drives every table off
 * the backend's Page<T> envelope ({items,total,limit,offset}). Purely
 * presentational — the parent owns the offset state and refetches on change.
 */
import { useMemo } from "react";

export interface PaginationProps {
  total: number;
  limit: number;
  offset: number;
  onChange: (nextOffset: number) => void;
  pageSizeOptions?: number[];
  onLimitChange?: (nextLimit: number) => void;
  disabled?: boolean;
}

// Windowed page numbers with ellipses, e.g. 1 … 4 5 [6] 7 8 … 20.
function pageWindow(current: number, last: number): (number | "…")[] {
  if (last <= 7) return Array.from({ length: last }, (_, i) => i + 1);
  const out: (number | "…")[] = [1];
  const start = Math.max(2, current - 1);
  const end = Math.min(last - 1, current + 1);
  if (start > 2) out.push("…");
  for (let p = start; p <= end; p++) out.push(p);
  if (end < last - 1) out.push("…");
  out.push(last);
  return out;
}

export default function Pagination({
  total, limit, offset, onChange, pageSizeOptions, onLimitChange, disabled,
}: PaginationProps) {
  const lastPage = Math.max(1, Math.ceil(total / limit));
  const current = Math.floor(offset / limit) + 1;
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  const pages = useMemo(() => pageWindow(current, lastPage), [current, lastPage]);

  const go = (page: number) => {
    if (disabled) return;
    const p = Math.min(Math.max(1, page), lastPage);
    onChange((p - 1) * limit);
  };

  const btn: React.CSSProperties = {
    minWidth: 32, height: 32, padding: "0 8px", borderRadius: 8,
    border: "1px solid var(--border)", background: "var(--panel)",
    color: "var(--text)", cursor: disabled ? "default" : "pointer",
    fontSize: 13, opacity: disabled ? 0.5 : 1,
  };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", marginTop: 12 }}>
      <span style={{ color: "var(--text-2)", fontSize: 13 }}>
        {total === 0 ? "No results" : `Showing ${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}`}
      </span>

      <div style={{ display: "flex", alignItems: "center", gap: 4, marginLeft: "auto" }}>
        <button type="button" style={btn} onClick={() => go(current - 1)} disabled={disabled || current <= 1} aria-label="Previous page">‹</button>
        {pages.map((p, i) =>
          p === "…" ? (
            <span key={`e${i}`} style={{ color: "var(--muted)", padding: "0 4px" }}>…</span>
          ) : (
            <button
              key={p}
              type="button"
              onClick={() => go(p)}
              disabled={disabled}
              aria-current={p === current ? "page" : undefined}
              style={{
                ...btn,
                borderColor: p === current ? "var(--accent)" : "var(--border)",
                background: p === current ? "var(--accent)" : "var(--panel)",
                color: p === current ? "#fff" : "var(--text)",
                fontWeight: p === current ? 600 : 400,
              }}
            >
              {p}
            </button>
          )
        )}
        <button type="button" style={btn} onClick={() => go(current + 1)} disabled={disabled || current >= lastPage} aria-label="Next page">›</button>
      </div>

      {pageSizeOptions && onLimitChange && (
        <select
          value={limit}
          onChange={(e) => onLimitChange(Number(e.target.value))}
          disabled={disabled}
          style={{ ...btn, minWidth: 72 }}
          aria-label="Rows per page"
        >
          {pageSizeOptions.map((n) => (
            <option key={n} value={n}>{n} / page</option>
          ))}
        </select>
      )}
    </div>
  );
}
