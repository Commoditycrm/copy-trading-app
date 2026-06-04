"use client";

/**
 * SearchableSelect — combobox-style dropdown used in place of native <select>
 * across the trade panel. Behaves like a select (single value, options list)
 * but renders an editable text input + a custom floating menu, so we can
 * style the trigger consistently with the other form inputs and the menu
 * with our own theme tokens.
 *
 * Features
 * --------
 *  - Optional inline search (filters the options by substring; on by default)
 *  - Chevron trigger icon (not the search magnifier — that pattern is for
 *    server-side autocompletes, which this isn't)
 *  - Outside-click + Esc close
 *  - Selected item highlighted; auto-scrolled into view when the menu opens
 *  - Loading spinner replaces the chevron when `loading` is true
 *  - Honors `disabled` for both the trigger and the menu
 *
 * Use it like:
 *   <SearchableSelect
 *     value={orderType}
 *     options={[{ value: "market", label: "Market" }, ...]}
 *     onChange={v => setOrderType(v as OrderType)}
 *     searchable={false}
 *     style={{ height: 34 }}
 *   />
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Spinner } from "@/components/Spinner";

export interface SelectOption {
  value: string;
  label: string;
}

interface Props {
  value: string;
  options: SelectOption[];
  placeholder?: string;
  disabled?: boolean;
  loading?: boolean;
  /** Show a text-input filter inside the trigger when the menu is open. Defaults to true. */
  searchable?: boolean;
  onChange: (value: string) => void;
  className?: string;
  /** Style on the trigger box. Use `height` to align with sibling inputs. */
  style?: React.CSSProperties;
}

const triggerBaseStyle: React.CSSProperties = {
  background: "#07090b",
  border: "1px solid var(--border)",
  borderRadius: 8,
};

export function SearchableSelect({
  value,
  options,
  placeholder = "Select",
  disabled = false,
  loading = false,
  searchable = true,
  onChange,
  className = "",
  style,
}: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const selected = useMemo(() => options.find(o => o.value === value), [options, value]);
  const filtered = useMemo(() => {
    if (!searchable || !query.trim()) return options;
    const q = query.toLowerCase();
    return options.filter(o => o.label.toLowerCase().includes(q));
  }, [options, query, searchable]);

  // Outside-click + Esc close. We also reset the search query so the next
  // time it opens it doesn't show stale filter text.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
        setQuery("");
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        setQuery("");
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Auto-scroll the selected row into view when the menu opens — so the
  // user can immediately see where they are in a long list.
  //
  // We DO NOT use `Element.scrollIntoView()` here: by spec it walks every
  // scrollable ancestor up to the document, so a list item being out of
  // view inside our menu was also scrolling the whole page to the top.
  // Instead we set `scrollTop` directly on the list container — that
  // moves *only* the menu, never the page.
  useEffect(() => {
    if (!open || !value || !listRef.current) return;
    const list = listRef.current;
    const sel = list.querySelector<HTMLButtonElement>(`[data-val="${CSS.escape(value)}"]`);
    if (!sel) return;
    list.scrollTop = sel.offsetTop - list.clientHeight / 2 + sel.offsetHeight / 2;
  }, [open, value]);

  function pick(v: string) {
    onChange(v);
    setOpen(false);
    setQuery("");
  }

  // Trigger display value rule:
  //  - menu closed → show the selected option's label (or "" so the
  //    placeholder shows)
  //  - menu open + searchable → show the user's live query (the input
  //    acts as the search box)
  //  - menu open + not searchable → show the selected label (read-only)
  const displayValue = open && searchable
    ? query
    : (selected?.label ?? "");

  return (
    <div ref={wrapRef} className={`relative ${className}`}>
      {/* Single-box trigger — the chevron is absolutely positioned over the
          input so there's no inner divider between text and icon. The input
          reserves 30px of right padding for the chevron to sit in. */}
      <div
        className="relative w-full"
        style={{
          ...triggerBaseStyle,
          ...style,
          opacity: disabled ? 0.6 : 1,
        }}
        onClick={(e) => {
          if (disabled) return;
          // If the click landed on the <input> itself, its own `onFocus`
          // handler already opened the menu — running our toggle here
          // would race and instantly close it. (That was the "click and
          // it expands then collapses" bug.) Let the input own that case.
          if (e.target === inputRef.current) return;
          // Use the previous state callback so we only schedule a focus
          // when we're *opening* the menu — focusing while closing would
          // refire `onFocus` and re-open the menu immediately.
          setOpen(prev => {
            const next = !prev;
            if (next) {
              // Defer focus so the input is in the DOM before we focus it.
              // `preventScroll: true` stops the browser from scrolling the
              // page (or any ancestor) to bring the input into view.
              setTimeout(() => inputRef.current?.focus({ preventScroll: true }), 0);
            }
            return next;
          });
        }}
      >
        <input
          ref={inputRef}
          type="text"
          value={displayValue}
          onChange={e => setQuery(e.target.value)}
          onFocus={() => setOpen(true)}
          disabled={disabled}
          readOnly={!searchable || !open}
          placeholder={selected ? "" : placeholder}
          className="block w-full text-sm outline-none"
          style={{
            padding: "0 30px 0 10px",
            color: "var(--text)",
            background: "transparent",
            border: 0,
            height: "100%",
            cursor: disabled
              ? "not-allowed"
              : (searchable && open ? "text" : "pointer"),
          }}
        />
        <span
          className="absolute right-0 top-0 h-full pr-2.5 inline-flex items-center pointer-events-none"
          style={{ color: "var(--text-2)" }}
        >
          {loading
            ? <Spinner />
            : (
              <svg
                width="14"
                height="14"
                viewBox="0 0 20 20"
                fill="none"
                aria-hidden
                style={{
                  transition: "transform 150ms",
                  transform: open ? "rotate(180deg)" : "rotate(0deg)",
                }}
              >
                <path
                  d="M5 8l5 5 5-5"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            )}
        </span>
      </div>

      {open && !disabled && (
        <div
          ref={listRef}
          className="absolute left-0 right-0 z-20 mt-1 rounded-lg overflow-y-auto py-1"
          style={{
            background: "#07090b",
            border: "1px solid var(--border)",
            maxHeight: 220,
            boxShadow: "0 12px 30px -8px rgba(0,0,0,0.55)",
          }}
        >
          {filtered.length === 0 ? (
            <div className="px-3 py-2 text-xs" style={{ color: "var(--muted)" }}>
              No matches
            </div>
          ) : (
            filtered.map(opt => {
              const active = opt.value === value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  data-val={opt.value}
                  onClick={() => pick(opt.value)}
                  className="w-full text-left px-3 py-1.5 text-sm transition-colors"
                  style={{
                    background: active ? "rgba(255,255,255,0.06)" : "transparent",
                    color: active ? "var(--text)" : "var(--text-2)",
                  }}
                  onMouseEnter={e => {
                    e.currentTarget.style.background = active
                      ? "rgba(255,255,255,0.08)"
                      : "rgba(255,255,255,0.04)";
                  }}
                  onMouseLeave={e => {
                    e.currentTarget.style.background = active
                      ? "rgba(255,255,255,0.06)"
                      : "transparent";
                  }}
                >
                  {opt.label}
                </button>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}
