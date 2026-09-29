"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ExportButton } from "@/components/ExportButton";
import { Download } from "lucide-react";

// Column headers MUST match backend admin._fanout_export_columns() exactly —
// the export selects columns by header string.
const TRADE_COLS = [
  "Trade Time (EST)", "Trader", "Trader Email", "Symbol", "Side", "Type", "Order Type",
  "Trade Qty", "Expected Price", "Filled Price", "Subscribers", "Mirrors Submitted",
  "Mirror Errors", "Detection Lag (ms)", "Fanout Duration (ms)", "Total Time (ms)",
];
const MIRROR_COLS = [
  "Subscriber", "Subscriber Email", "Mirror Status", "Broker", "Mirror Qty", "Mirror Filled Qty",
  "Mirror Expected Price", "Mirror Filled Price", "Pick Lag (ms)", "Eligibility Lag (ms)",
  "Broker Lag (ms)", "Subscriber Lag (ms)", "Reject Reason", "Mirror Broker Order ID", "Parent Order ID",
];
const ALL_COLS = [...TRADE_COLS, ...MIRROR_COLS];

/** ISO week string ("2026-W39") → that week's Mon 00:00 .. Sun 23:59:59. */
function weekToRange(week: string): { from: string; to: string } | null {
  const m = /^(\d{4})-W(\d{2})$/.exec(week);
  if (!m) return null;
  const year = +m[1], wk = +m[2];
  const jan4 = new Date(Date.UTC(year, 0, 4));
  const jan4Dow = (jan4.getUTCDay() + 6) % 7;            // Mon = 0
  const mon = new Date(jan4);
  mon.setUTCDate(jan4.getUTCDate() - jan4Dow + (wk - 1) * 7);
  const sun = new Date(mon);
  sun.setUTCDate(mon.getUTCDate() + 6);
  return {
    from: `${mon.toISOString().slice(0, 10)}T00:00:00`,
    to: `${sun.toISOString().slice(0, 10)}T23:59:59`,
  };
}

/** Export dialog for the admin Performance sheet: pick a period (all / date
 *  range / week) and exactly which columns go in the file. Reuses ExportButton
 *  (authed blob download) as the confirming action. */
export function ExportDialog({ search, side }: { search: string; side: string }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"all" | "range" | "week">("all");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [week, setWeek] = useState("");
  const [cols, setCols] = useState<Set<string>>(() => new Set(ALL_COLS));
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const path = useMemo(() => {
    const p = new URLSearchParams();
    if (search.trim()) p.set("search", search.trim());
    if (side !== "all") p.set("side", side);
    if (mode === "range") {
      if (from) p.set("from", `${from}T00:00:00`);
      if (to) p.set("to", `${to}T23:59:59`);
    } else if (mode === "week" && week) {
      const r = weekToRange(week);
      if (r) { p.set("from", r.from); p.set("to", r.to); }
    }
    // Only send `columns` when it's a real subset — omitting it means "all".
    const ordered = ALL_COLS.filter((c) => cols.has(c));
    if (ordered.length && ordered.length < ALL_COLS.length) p.set("columns", ordered.join(","));
    const qs = p.toString();
    return `/api/admin/performance/export${qs ? `?${qs}` : ""}`;
  }, [search, side, mode, from, to, week, cols]);

  const toggle = (c: string) => setCols((prev) => {
    const next = new Set(prev);
    if (next.has(c)) next.delete(c); else next.add(c);
    return next;
  });
  const setGroup = (group: string[], on: boolean) => setCols((prev) => {
    const next = new Set(prev);
    for (const c of group) { if (on) next.add(c); else next.delete(c); }
    return next;
  });

  const radio = "text-xs px-2.5 py-1 rounded-md cursor-pointer";
  const field = "text-xs px-2 py-1 rounded-md";
  const fieldStyle = { background: "rgba(255,255,255,0.06)", border: "1px solid var(--border)", color: "var(--text)" };
  const nSel = cols.size;

  function ColGroup({ title, group }: { title: string; group: string[] }) {
    const allOn = group.every((c) => cols.has(c));
    return (
      <div className="mb-2">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: "var(--muted)" }}>{title}</span>
          <button type="button" className="text-[10px]" style={{ color: "var(--accent)" }}
                  onClick={() => setGroup(group, !allOn)}>{allOn ? "clear" : "all"}</button>
        </div>
        <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
          {group.map((c) => (
            <label key={c} className="flex items-center gap-1.5 text-xs cursor-pointer" style={{ color: "var(--text-2)" }}>
              <input type="checkbox" checked={cols.has(c)} onChange={() => toggle(c)} />
              <span className="truncate">{c}</span>
            </label>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div ref={ref} className="relative">
      <button type="button" onClick={() => setOpen((o) => !o)}
              className="text-sm px-3 py-1.5 rounded-lg inline-flex items-center gap-1.5"
              style={{ background: "rgba(255,255,255,0.06)", border: "1px solid var(--border)", color: "var(--text-2)" }}>
        <Download size={14} /> Export…
      </button>
      {open && (
        <div className="absolute right-0 z-30 mt-1 rounded-xl p-3 shadow-xl"
             style={{ background: "var(--panel)", border: "1px solid var(--border)", width: 420, maxHeight: "72vh", overflowY: "auto" }}>
          {/* Period */}
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide" style={{ color: "var(--muted)" }}>Period</div>
          <div className="flex gap-1 mb-2">
            {([["all", "All time"], ["range", "Date range"], ["week", "Week"]] as const).map(([m, lbl]) => (
              <button key={m} type="button" onClick={() => setMode(m)} className={radio}
                      style={mode === m
                        ? { background: "var(--accent)", color: "var(--accent-ink)", border: "1px solid var(--accent)" }
                        : { background: "transparent", color: "var(--text-2)", border: "1px solid var(--border)" }}>
                {lbl}
              </button>
            ))}
          </div>
          {mode === "range" && (
            <div className="flex items-center gap-2 mb-3">
              <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} aria-label="From date" className={field} style={fieldStyle} />
              <span className="text-xs" style={{ color: "var(--muted)" }}>to</span>
              <input type="date" value={to} onChange={(e) => setTo(e.target.value)} aria-label="To date" className={field} style={fieldStyle} />
            </div>
          )}
          {mode === "week" && (
            <div className="mb-3">
              <input type="week" value={week} onChange={(e) => setWeek(e.target.value)} aria-label="Week" className={field} style={fieldStyle} />
            </div>
          )}

          {/* Columns */}
          <div className="flex items-center justify-between mb-1">
            <span className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: "var(--muted)" }}>Columns ({nSel})</span>
            <div className="flex gap-2">
              <button type="button" className="text-[10px]" style={{ color: "var(--accent)" }} onClick={() => setCols(new Set(ALL_COLS))}>select all</button>
              <button type="button" className="text-[10px]" style={{ color: "var(--muted)" }} onClick={() => setCols(new Set())}>clear</button>
            </div>
          </div>
          <ColGroup title="Trade" group={TRADE_COLS} />
          <ColGroup title="Mirror (subscriber)" group={MIRROR_COLS} />

          <div className="mt-1">
            <ExportButton path={path} variant="primary" fullWidth label={`Download .xlsx (${nSel} col${nSel === 1 ? "" : "s"})`}
                          fallbackName="kopyya-fanouts.xlsx" disabled={nSel === 0} />
          </div>
        </div>
      )}
    </div>
  );
}
