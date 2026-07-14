"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Country-code dropdown + local number, emitting a normalized E.164 string
 * ("+919876543210") to the parent via onChange. Empty local number emits "".
 * India is the default since that's most of our users; every value is still a
 * plain E.164 string so the parent form + backend stay unchanged.
 */

type Country = { code: string; dial: string; name: string; flag: string };

// Curated list — common markets first, then alphabetical. Dial codes are the
// country calling codes without the leading "+".
const COUNTRIES: Country[] = [
  { code: "IN", dial: "91", name: "India", flag: "🇮🇳" },
  { code: "US", dial: "1", name: "United States", flag: "🇺🇸" },
  { code: "GB", dial: "44", name: "United Kingdom", flag: "🇬🇧" },
  { code: "CA", dial: "1", name: "Canada", flag: "🇨🇦" },
  { code: "AU", dial: "61", name: "Australia", flag: "🇦🇺" },
  { code: "AE", dial: "971", name: "UAE", flag: "🇦🇪" },
  { code: "SG", dial: "65", name: "Singapore", flag: "🇸🇬" },
  { code: "SA", dial: "966", name: "Saudi Arabia", flag: "🇸🇦" },
  { code: "DE", dial: "49", name: "Germany", flag: "🇩🇪" },
  { code: "FR", dial: "33", name: "France", flag: "🇫🇷" },
  { code: "ES", dial: "34", name: "Spain", flag: "🇪🇸" },
  { code: "IT", dial: "39", name: "Italy", flag: "🇮🇹" },
  { code: "NL", dial: "31", name: "Netherlands", flag: "🇳🇱" },
  { code: "IE", dial: "353", name: "Ireland", flag: "🇮🇪" },
  { code: "CH", dial: "41", name: "Switzerland", flag: "🇨🇭" },
  { code: "SE", dial: "46", name: "Sweden", flag: "🇸🇪" },
  { code: "NO", dial: "47", name: "Norway", flag: "🇳🇴" },
  { code: "BR", dial: "55", name: "Brazil", flag: "🇧🇷" },
  { code: "MX", dial: "52", name: "Mexico", flag: "🇲🇽" },
  { code: "ZA", dial: "27", name: "South Africa", flag: "🇿🇦" },
  { code: "NG", dial: "234", name: "Nigeria", flag: "🇳🇬" },
  { code: "KE", dial: "254", name: "Kenya", flag: "🇰🇪" },
  { code: "PK", dial: "92", name: "Pakistan", flag: "🇵🇰" },
  { code: "BD", dial: "880", name: "Bangladesh", flag: "🇧🇩" },
  { code: "LK", dial: "94", name: "Sri Lanka", flag: "🇱🇰" },
  { code: "NP", dial: "977", name: "Nepal", flag: "🇳🇵" },
  { code: "PH", dial: "63", name: "Philippines", flag: "🇵🇭" },
  { code: "ID", dial: "62", name: "Indonesia", flag: "🇮🇩" },
  { code: "MY", dial: "60", name: "Malaysia", flag: "🇲🇾" },
  { code: "TH", dial: "66", name: "Thailand", flag: "🇹🇭" },
  { code: "VN", dial: "84", name: "Vietnam", flag: "🇻🇳" },
  { code: "JP", dial: "81", name: "Japan", flag: "🇯🇵" },
  { code: "KR", dial: "82", name: "South Korea", flag: "🇰🇷" },
  { code: "CN", dial: "86", name: "China", flag: "🇨🇳" },
  { code: "HK", dial: "852", name: "Hong Kong", flag: "🇭🇰" },
  { code: "NZ", dial: "64", name: "New Zealand", flag: "🇳🇿" },
  { code: "QA", dial: "974", name: "Qatar", flag: "🇶🇦" },
  { code: "KW", dial: "965", name: "Kuwait", flag: "🇰🇼" },
];

// Longest dial codes first so prefix-matching an existing E.164 value is greedy.
const BY_DIAL_DESC = [...COUNTRIES].sort((a, b) => b.dial.length - a.dial.length);

function parse(value: string): { dial: string; local: string } {
  const digits = (value || "").replace(/[^\d+]/g, "");
  if (digits.startsWith("+")) {
    const d = digits.slice(1);
    for (const c of BY_DIAL_DESC) {
      if (d.startsWith(c.dial)) return { dial: c.dial, local: d.slice(c.dial.length) };
    }
    return { dial: "91", local: d };
  }
  return { dial: "91", local: digits.replace(/\D/g, "") };
}

export function PhoneInput({
  value,
  onChange,
  id,
}: {
  value: string;
  onChange: (e164: string) => void;
  id?: string;
}) {
  const init = parse(value);
  const [country, setCountry] = useState<Country>(
    () => COUNTRIES.find((c) => c.dial === init.dial) ?? COUNTRIES[0],
  );
  const [local, setLocal] = useState(init.local);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const wrapRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  function emit(d: string, l: string) {
    const digits = l.replace(/\D/g, "");
    onChange(digits ? `+${d}${digits}` : "");
  }

  // Close on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    // Focus the search box when the menu opens.
    searchRef.current?.focus();
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Search matches country name, ISO code, or dial code — even though the list
  // shows only the flag + dial code, so typing "india" or "91" both work.
  const q = query.trim().toLowerCase();
  const filtered = q
    ? COUNTRIES.filter(
        (c) =>
          c.name.toLowerCase().includes(q) ||
          c.code.toLowerCase().includes(q) ||
          c.dial.includes(q),
      )
    : COUNTRIES;

  function pick(c: Country) {
    setCountry(c);
    emit(c.dial, local);
    setOpen(false);
    setQuery("");
  }

  return (
    <div className="flex gap-2">
      <div ref={wrapRef} className="relative shrink-0">
        <button
          type="button"
          aria-label="Country code"
          aria-haspopup="listbox"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
          className="p-2.5 inline-flex items-center gap-1.5 rounded-md w-full"
          style={{ background: "var(--bg)", border: "1px solid var(--border)", color: "var(--text)" }}
        >
          <span>{country.flag}</span>
          <span className="tabular-nums">+{country.dial}</span>
          <span style={{ color: "var(--muted)", fontSize: 10 }}>▾</span>
        </button>

        {open && (
          <div
            className="absolute z-50 mt-1 rounded-lg overflow-hidden"
            style={{
              background: "var(--panel)",
              border: "1px solid var(--border)",
              boxShadow: "0 16px 40px -18px rgba(0,0,0,0.6)",
              width: 160,
            }}
          >
            <div className="p-1.5" style={{ borderBottom: "1px solid var(--border)" }}>
              <input
                ref={searchRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && filtered[0]) { e.preventDefault(); pick(filtered[0]); } }}
                placeholder="Search…"
                className="w-full text-sm px-2 py-1 rounded-md"
                style={{ background: "var(--bg)", border: "1px solid var(--border)", color: "var(--text)", outline: "none" }}
              />
            </div>
            <div className="overflow-auto" style={{ maxHeight: 220 }}>
              {filtered.length === 0 ? (
                <div className="px-3 py-2 text-xs" style={{ color: "var(--muted)" }}>No match</div>
              ) : (
                filtered.map((c) => (
                  <button
                    key={c.code}
                    type="button"
                    onClick={() => pick(c)}
                    className="w-full flex items-center gap-2 px-3 py-2 text-sm text-left transition-colors hover:bg-[var(--panel-2)]"
                    style={{ background: c.code === country.code ? "var(--panel-2)" : "transparent", color: "var(--text)" }}
                    title={c.name}
                  >
                    <span>{c.flag}</span>
                    <span className="tabular-nums">+{c.dial}</span>
                  </button>
                ))
              )}
            </div>
          </div>
        )}
      </div>

      <input
        id={id}
        type="tel"
        inputMode="tel"
        autoComplete="tel-national"
        placeholder="98765 43210"
        value={local}
        onChange={(e) => { setLocal(e.target.value); emit(country.dial, e.target.value); }}
        className="flex-1 p-2.5"
        maxLength={15}
      />
    </div>
  );
}
