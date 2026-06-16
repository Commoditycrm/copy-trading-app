"use client";

import { ReactNode, useState } from "react";

export interface TabItem {
  key: string;
  label: ReactNode;
}

/**
 * Segmented tab control. Controlled (pass `value` + `onChange`) or
 * uncontrolled (defaults to the first item).
 */
export function Tabs({
  items,
  value,
  onChange,
  className = "",
}: {
  items: TabItem[];
  value?: string;
  onChange?: (key: string) => void;
  className?: string;
}) {
  const [internal, setInternal] = useState(items[0]?.key);
  const active = value ?? internal;
  const set = (k: string) => {
    setInternal(k);
    onChange?.(k);
  };
  return (
    <div
      role="tablist"
      className={`inline-flex items-center gap-1 p-1 rounded-token ${className}`}
      style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}
    >
      {items.map((t) => {
        const on = t.key === active;
        return (
          <button
            key={t.key}
            role="tab"
            aria-selected={on}
            onClick={() => set(t.key)}
            className="px-3 py-1.5 rounded-chip text-xs font-medium transition-colors focus-ring"
            style={{
              background: on ? "var(--accent)" : "transparent",
              color: on ? "var(--accent-ink)" : "var(--text-2)",
            }}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}
