"use client";

/**
 * Strike picker — replaces a native <select> so we can cap the open list
 * height (native popups are browser-controlled and ignore CSS sizing,
 * which is why a 50-strike chain takes over the whole screen).
 */
import { useEffect, useRef, useState } from "react";
import { Spinner } from "@/components/Spinner";

interface Props {
  value: string;                                   // selected strike as a string
  strikes: number[];                               // sorted list of available strikes
  loading: boolean;
  disabled?: boolean;
  /** Placeholder shown in the trigger when no value is set. */
  placeholder: string;
  onChange: (v: string) => void;
  className?: string;
  style?: React.CSSProperties;
}

const fmtStrike = (n: number) => n.toLocaleString(undefined, { minimumFractionDigits: 2 });

export function StrikePicker({
  value,
  strikes,
  loading,
  disabled = false,
  placeholder,
  onChange,
  className = "",
  style,
}: Props) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Outside-click + Esc close.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Scroll the selected item into view when the list opens.
  useEffect(() => {
    if (!open || !value || !listRef.current) return;
    const sel = listRef.current.querySelector<HTMLButtonElement>(`[data-strike="${value}"]`);
    sel?.scrollIntoView({ block: "center" });
  }, [open, value]);

  return (
    <div ref={wrapRef} className={`relative ${className}`}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen(o => !o)}
        className="w-full p-2 rounded border text-left flex items-center justify-between"
        style={{ ...style, opacity: disabled ? 0.6 : 1, cursor: disabled ? "not-allowed" : "pointer" }}
      >
        <span style={{ color: value ? "var(--text)" : "var(--muted)" }}>
          {loading
            ? <span className="inline-flex items-center gap-2"><Spinner /> loading…</span>
            : (value ? fmtStrike(Number(value)) : placeholder)}
        </span>
        <svg width="12" height="12" viewBox="0 0 20 20" fill="none" aria-hidden>
          <path d="M5 8l5 5 5-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && !disabled && strikes.length > 0 && (
        <div
          ref={listRef}
          className="absolute z-20 mt-1 w-full rounded border overflow-y-auto"
          style={{
            background: "var(--panel)",
            borderColor: "var(--border)",
            maxHeight: 192,                         // ~6 rows; the actual "compact" cap
            boxShadow: "0 8px 24px -8px rgba(0,0,0,0.45)",
          }}
        >
          {strikes.map(s => {
            const sv = String(s);
            const active = sv === value;
            return (
              <button
                type="button"
                key={sv}
                data-strike={sv}
                onClick={() => { onChange(sv); setOpen(false); }}
                className="w-full text-left px-2 py-1 text-sm transition-colors"
                style={{
                  background: active ? "rgba(10,115,168,0.16)" : "transparent",
                  color: active ? "var(--accent)" : "var(--text)",
                  fontWeight: active ? 600 : 400,
                }}
                onMouseEnter={e => { if (!active) (e.currentTarget.style.background = "rgba(255,255,255,0.04)"); }}
                onMouseLeave={e => { if (!active) (e.currentTarget.style.background = "transparent"); }}
              >
                {fmtStrike(s)}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
