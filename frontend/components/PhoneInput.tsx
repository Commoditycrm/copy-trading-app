"use client";

import { useState } from "react";

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
  const [dial, setDial] = useState(init.dial);
  const [local, setLocal] = useState(init.local);

  function emit(d: string, l: string) {
    const digits = l.replace(/\D/g, "");
    onChange(digits ? `+${d}${digits}` : "");
  }

  return (
    <div className="flex gap-2">
      <select
        aria-label="Country code"
        value={dial}
        onChange={(e) => { setDial(e.target.value); emit(e.target.value, local); }}
        className="p-2.5 shrink-0"
        style={{ maxWidth: 118 }}
      >
        {COUNTRIES.map((c) => (
          <option key={c.code} value={c.dial}>
            {c.flag} +{c.dial}
          </option>
        ))}
      </select>
      <input
        id={id}
        type="tel"
        inputMode="tel"
        autoComplete="tel-national"
        placeholder="98765 43210"
        value={local}
        onChange={(e) => { setLocal(e.target.value); emit(dial, e.target.value); }}
        className="flex-1 p-2.5"
        maxLength={15}
      />
    </div>
  );
}
