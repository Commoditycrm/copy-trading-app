"use client";

/**
 * Snapshot page. Default view shows every snapshot taken TODAY, stacked newest
 * first — so a fresh Exit doesn't hide the earlier ones; the whole day stays on
 * screen. ?id=… opens one specific snapshot from the History page. History
 * itself is untouched: each snapshot is still its own record.
 *
 * Each snapshot renders as a self-contained <SnapshotBlock/> that owns its own
 * re-entry state and Re-Enter/delete calls (targeted by snapshot id + row index).
 */
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { PercentInput } from "@/components/PercentInput";
import type { User } from "@/lib/types";

type Status = "filled" | "working" | "pending" | "expired";

interface SnapPos {
  symbol: string;
  instrument_type: string;
  quantity: string;            // signed
  price: string | null;        // exit price / share
  current_price: string | null; // live market price / share
  pdc: string | null;           // previous day's market close / share
  reentry_price: string | null; // fill price (filled) or resting limit (working)
  default_mode: "market" | "pct" | "limit"; // re-entry default chosen at exit
  default_value: string | null;
  default_basis: "current" | "reference" | "exit" | null; // basis for a "pct" default
  option_expiry: string | null;
  option_strike: string | null;
  option_right: string | null;
  reentry_status: Status;
}
interface Snapshot {
  id: string;
  created_at: string;
  positions: SnapPos[];
  summary: { total: number; filled: number; working: number; pending: number; expired?: number };
}

const STATUS_STYLE: Record<Status, { bg: string; color: string; label: string }> = {
  filled:  { bg: "var(--good-soft)", color: "var(--good)", label: "Back in" },
  working: { bg: "rgba(250,204,21,0.12)", color: "#facc15", label: "Resting" },
  pending: { bg: "var(--panel-2)", color: "var(--muted)", label: "To re-enter" },
  expired: { bg: "rgba(239,68,68,0.12)", color: "var(--bad)", label: "Expired" },
};

function fmtMoney(v: string | null): string {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return Number.isFinite(n) ? `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : String(v);
}

/** Option expiry "YYYY-MM-DD" → "10 Jul 26"; "—" for stocks / no expiry. */
function fmtExpiry(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso.length === 10 ? iso + "T00:00:00Z" : iso);
  if (Number.isNaN(d.getTime())) return iso;
  const mon = d.toLocaleDateString("en-US", { month: "short", timeZone: "UTC" });
  return `${d.getUTCDate()} ${mon} ${String(d.getUTCFullYear()).slice(-2)}`;
}

/** Full descriptor like Order History — stock: "MSFT"; option: "MSFT C $492.5 9 Sep 26"
 *  (ticker · C/P · $strike · expiry). */
function positionLabel(p: SnapPos): string {
  if (p.instrument_type !== "option") return p.symbol.toUpperCase();
  const cp = p.option_right === "call" ? "C" : p.option_right === "put" ? "P" : "";
  const strike = p.option_strike != null && p.option_strike !== "" ? `$${Number(p.option_strike)}` : "";
  const exp = p.option_expiry ? fmtExpiry(p.option_expiry) : "";
  return [p.symbol.toUpperCase(), cp, strike, exp].filter(Boolean).join(" ");
}

// One combined choice for the re-entry price:
//  market        — buy back now at market
//  pct_current   — a resting limit % below the live market price
//  pct_reference — a resting limit % below the previous day close (PDC)
//  at_exit       — a resting limit % below the recorded exit price (0% = at exit)
//  trailing      — a trailing-stop BUY at a trail % (stock only)
//  limit         — an exact $ limit price (per-row only)
type ReChoice = "market" | "pct_current" | "pct_reference" | "at_exit" | "trailing" | "limit";
const isPctChoice = (c: ReChoice) => c === "pct_current" || c === "pct_reference" || c === "at_exit";
const takesPct = (c: ReChoice) => isPctChoice(c) || c === "trailing";
const choiceBasis = (c: ReChoice) =>
  c === "pct_reference" ? "reference" : c === "at_exit" ? "exit" : "current";

/**
 * One snapshot, fully self-contained: its own table, Re-Enter All bar, per-row
 * Re-Enter / delete, and re-entry state. `initial` seeds it (from the today
 * feed) so the page doesn't double-fetch; it still reloads itself live on order
 * events. All Re-Enter / delete calls target this snapshot's id + row index.
 */
function SnapshotBlock({ snapshotId, initial }: { snapshotId: string; initial?: Snapshot }) {
  const [snap, setSnap] = useState<Snapshot | null>(initial ?? null);
  const [loading, setLoading] = useState(!initial);
  const [globalChoice, setGlobalChoice] = useState<Exclude<ReChoice, "limit">>("pct_current");
  const [globalDisc, setGlobalDisc] = useState("");
  const [rowChoice, setRowChoice] = useState<Record<string, ReChoice>>({});
  const [rowVal, setRowVal] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null); // "all" or a row index

  // Pre-fill each row's re-entry control from the default chosen at exit time,
  // without overwriting anything already edited. Keyed by array index (the same
  // symbol can appear twice and must be re-entered independently).
  const prefill = useCallback((s: Snapshot) => {
    setRowChoice((prev) => {
      const next = { ...prev };
      s.positions.forEach((p, i) => {
        const k = String(i);
        if (k in next) return;
        if (p.default_mode === "pct")
          next[k] = p.default_basis === "reference" ? "pct_reference"
            : p.default_basis === "exit" ? "at_exit" : "pct_current";
        else if (p.default_mode === "limit") next[k] = "limit";
        else if (p.default_mode === "market") next[k] = "market";
      });
      return next;
    });
    setRowVal((prev) => {
      const next = { ...prev };
      s.positions.forEach((p, i) => {
        const k = String(i);
        if (!(k in next) && p.default_value != null) next[k] = p.default_value;
      });
      return next;
    });
  }, []);

  const load = useCallback(async () => {
    try {
      const r = await api<{ snapshot: Snapshot | null }>(`/api/positions/snapshots/latest?snapshot_id=${snapshotId}`);
      setSnap(r.snapshot);
      if (r.snapshot) prefill(r.snapshot);
    } catch (e) {
      notify.fromError(e, "Could not load snapshot");
    } finally {
      setLoading(false);
    }
  }, [snapshotId, prefill]);

  // Seed prefill from `initial`; fetch fresh if we weren't handed one.
  useEffect(() => {
    if (initial) prefill(initial);
    else load();
  }, [initial, load, prefill]);

  // Live: reload when an order event lands so a re-entry flips to "Back in".
  useEventStream((evt) => {
    if (typeof evt?.type === "string" && evt.type.startsWith("order.")) load();
  });

  // Backstop poll while anything is unresolved (in case an event is missed).
  useEffect(() => {
    if (!snap || snap.summary.pending + snap.summary.working === 0) return;
    const id = setInterval(load, 5_000);
    return () => clearInterval(id);
  }, [snap, load]);

  async function deleteRow(index: number) {
    if (!snap) return;
    if (!confirm("Remove this order from the snapshot? This only clears the re-entry record — it won't touch any order already placed.")) return;
    setBusy("delrow:" + index);
    try {
      await api(`/api/positions/snapshots/${snap.id}/positions/${index}`, { method: "DELETE" });
      notify.success("Removed from snapshot.");
      setRowChoice({}); setRowVal({});   // indices shift after a removal — re-prefill fresh
      await load();
    } catch (e) {
      notify.fromError(e, "Could not remove order");
    } finally {
      setBusy(null);
    }
  }

  async function reEnter(scope: "all" | string) {
    if (!snap) return;
    setBusy(scope);
    try {
      const params = new URLSearchParams();
      params.set("snapshot_id", snap.id);
      if (scope === "all") {
        if (globalChoice === "trailing") {
          const d = parseFloat(globalDisc);
          if (!isNaN(d) && d > 0 && d <= 100) params.set("trail_percent", String(d));
        } else if (globalChoice !== "market") {
          params.set("basis", choiceBasis(globalChoice));
          const d = parseFloat(globalDisc);
          if (!isNaN(d) && d > 0 && d <= 100) params.set("discount_percent", String(d));
        }
      } else {
        params.set("index", scope);
        const choice = rowChoice[scope] ?? "market";
        const v = parseFloat(rowVal[scope] ?? "");
        if (choice === "trailing") {
          if (!isNaN(v) && v > 0 && v <= 100) params.set("trail_percent", String(v));
        } else if (choice === "limit") {
          if (!isNaN(v) && v > 0) params.set("limit_price", String(v));
        } else if (choice !== "market") {
          params.set("basis", choiceBasis(choice));
          if (!isNaN(v) && v > 0 && v <= 100) params.set("discount_percent", String(v));
        }
      }
      const res = await api<{ placed_count: number; skipped_count: number; failed_count: number }>(
        `/api/positions/re-enter?${params.toString()}`,
        { method: "POST" },
      );
      if (res.placed_count === 0 && res.failed_count === 0) notify.info("Nothing new to re-enter.");
      else if (res.failed_count === 0) notify.success(`Re-entered ${res.placed_count} order${res.placed_count === 1 ? "" : "s"}.`);
      else notify.warn(`Re-entered ${res.placed_count}; ${res.failed_count} failed — check Order History.`);
      await load();
    } catch (e) {
      notify.fromError(e, "Re-enter failed");
    } finally {
      setBusy(null);
    }
  }

  const th = "px-4 py-3 text-xs font-semibold whitespace-nowrap";
  const td = "px-4 py-3 text-sm whitespace-nowrap";

  if (loading) return <div style={{ color: "var(--muted)" }}>Loading…</div>;
  if (!snap || snap.positions.length === 0) return null; // caller shows the empty state

  return (
    <div className="space-y-3">
      {/* Per-snapshot header: when it was taken + status counts. */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="text-sm font-semibold" style={{ color: "var(--text-2)" }}>
          <span style={{ color: "var(--muted)" }}>Taken </span>{new Date(snap.created_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
          <span style={{ color: "var(--muted)" }}> · {snap.summary.total} order{snap.summary.total === 1 ? "" : "s"}</span>
        </div>
        <div className="text-sm">
          <span style={{ color: "var(--good)" }}>{snap.summary.filled}/{snap.summary.total} back in</span>
          {snap.summary.working > 0 && <span style={{ color: "#facc15" }}> · {snap.summary.working} resting</span>}
          {snap.summary.pending > 0 && <span style={{ color: "var(--muted)" }}> · {snap.summary.pending} to go</span>}
          {(snap.summary.expired ?? 0) > 0 && <span style={{ color: "var(--bad)" }}> · {snap.summary.expired} expired</span>}
        </div>
      </div>

      {/* Re-Enter All bar */}
      <div className="rounded-xl px-4 py-3 flex items-center justify-between gap-3 flex-wrap"
           style={{ background: "var(--panel)", border: "1px solid var(--border)" }}>
        <span className="text-sm" style={{ color: "var(--text-2)" }}>
          Re-enter everything not back yet ({snap.summary.pending} pending):
        </span>
        <div className="flex items-center gap-2">
          <div className="inline-flex items-center rounded-lg h-7 px-1 gap-1"
               style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}
               title="Re-enter every pending position at market, or a resting limit % below the live price (Market) or the previous day close (PDC).">
            <select value={globalChoice}
                    onChange={(e) => { setGlobalChoice(e.target.value as Exclude<ReChoice, "limit">); setGlobalDisc(""); }}
                    aria-label="Re-Enter All choice"
                    className="text-xs outline-none cursor-pointer"
                    style={{ background: "transparent", border: "none", color: "var(--text)" }}>
              <option value="market">Market</option>
              <option value="pct_current">% below Market</option>
              <option value="pct_reference">% below PDC</option>
              <option value="at_exit">% below Exit</option>
              <option value="trailing">Trailing %</option>
            </select>
            {takesPct(globalChoice) && (
              <div className="inline-flex items-center gap-0.5">
                <PercentInput min="0" max="100" step="0.5" value={globalDisc}
                       onChange={(e) => setGlobalDisc(e.target.value)} placeholder={globalChoice === "trailing" ? "trail" : "%"}
                       aria-label={globalChoice === "trailing" ? "Trail percent for Re-Enter All" : "Discount percent for Re-Enter All"}
                       className="w-10 text-xs outline-none text-center"
                       style={{ background: "transparent", border: "none", color: "var(--text)" }} />
                <span className="text-[9px]" style={{ color: "var(--muted)" }}>%</span>
              </div>
            )}
          </div>
          <button type="button" onClick={() => reEnter("all")}
                  disabled={busy !== null || snap.summary.pending === 0}
                  className="px-3 py-1.5 rounded-lg text-xs font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
                  style={{ background: "var(--accent)", color: "var(--accent-ink)", border: "1px solid var(--accent)" }}>
            {busy === "all" ? "Re-entering…" : snap.summary.pending === 0 ? "All re-entered" : `Re-Enter All (${snap.summary.pending})`}
          </button>
        </div>
      </div>

      {/* Per-order table */}
      <div className="rounded-xl overflow-hidden" style={{ border: "1px solid var(--border)" }}>
        <div className="overflow-auto" style={{ maxHeight: "62vh" }}>
          <table className="w-full">
            <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
              <tr style={{ borderBottom: "1px solid var(--border)" }}>
                <th className={`${th} text-left`} style={{ color: "var(--muted)" }}>Symbol</th>
                <th className={`${th} text-left`} style={{ color: "var(--muted)" }}>Side</th>
                <th className={`${th} text-right`} style={{ color: "var(--muted)" }}>Qty</th>
                <th className={`${th} text-left`} style={{ color: "var(--muted)" }} title="Option expiry date (— for stocks)">Expiry</th>
                <th className={`${th} text-right`} style={{ color: "var(--muted)" }}>Exit Price</th>
                <th className={`${th} text-right`} style={{ color: "var(--muted)" }}>Current Price</th>
                <th className={`${th} text-right`} style={{ color: "var(--muted)" }} title="Previous day's market close price">PDC</th>
                <th className={`${th} text-right`} style={{ color: "var(--muted)" }}>Re-Entry Price</th>
                <th className={`${th} text-right`} style={{ color: "var(--muted)" }}>Change / sh</th>
                <th className={`${th} text-right`} style={{ color: "var(--muted)" }} title="Current price vs exit price, as a %">%</th>
                <th className={`${th} text-left`} style={{ color: "var(--muted)" }}>Status</th>
                <th className={`${th} text-right`} style={{ color: "var(--muted)" }}>Re-Enter</th>
              </tr>
            </thead>
            <tbody>
              {snap.positions.map((p, i) => {
                const rowKey = String(i);
                const qty = Number(p.quantity);
                const side = qty >= 0 ? "Long" : "Short";
                const st = STATUS_STYLE[p.reentry_status];
                const canReenter = p.reentry_status === "pending";
                const exitP = p.price != null ? Number(p.price) : null;
                const reP = p.reentry_price != null ? Number(p.reentry_price) : null;
                const changePerSh =
                  p.reentry_status === "filled" && exitP != null && reP != null ? exitP - reP : null;
                const choice: ReChoice = rowChoice[rowKey] ?? "market";
                const isPct = isPctChoice(choice);
                const rv = parseFloat(rowVal[rowKey] ?? "");
                const curP = p.current_price != null ? Number(p.current_price) : null;
                const pdcP = p.pdc != null ? Number(p.pdc) : null;
                const basisPx = choice === "pct_reference" ? pdcP : choice === "at_exit" ? exitP : curP;
                const rvValid = !isNaN(rv) && rv > 0 && rv <= 100;
                const targetPx =
                  choice === "at_exit"
                    ? (basisPx != null ? basisPx * (1 - (rvValid ? rv : 0) / 100) : null)
                    : (isPct && rvValid && basisPx != null ? basisPx * (1 - rv / 100) : null);
                const pctVsExit =
                  exitP != null && exitP !== 0 && curP != null ? ((curP - exitP) / exitP) * 100 : null;
                return (
                  <tr key={rowKey} style={{ borderBottom: "1px solid var(--border)", opacity: p.reentry_status === "expired" ? 0.55 : 1 }}>
                    <td className={`${td} font-medium`}>{positionLabel(p)}</td>
                    <td className={td} style={{ color: qty >= 0 ? "var(--good)" : "var(--bad)" }}>{side}</td>
                    <td className={`${td} text-right num`}>{Math.abs(qty)}</td>
                    <td className={`${td} text-left num`}
                        style={{ color: p.reentry_status === "expired" ? "var(--bad)" : "var(--text-2)" }}>
                      {fmtExpiry(p.option_expiry)}
                    </td>
                    <td className={`${td} text-right num`}>{fmtMoney(p.price)}</td>
                    <td className={`${td} text-right num`} style={{ color: "var(--text-2)" }}>{fmtMoney(p.current_price)}</td>
                    <td className={`${td} text-right num`} style={{ color: "var(--text-2)" }} title="Previous day's market close price">{fmtMoney(p.pdc)}</td>
                    <td className={`${td} text-right num`} style={{ color: "var(--text-2)" }}>
                      {p.reentry_status === "filled"
                        ? fmtMoney(p.reentry_price)
                        : p.reentry_status === "working"
                          ? (p.reentry_price ? `resting @ ${fmtMoney(p.reentry_price)}` : "resting")
                          : "—"}
                    </td>
                    <td className={`${td} text-right num`} style={{
                      color: changePerSh == null ? "var(--muted)" : changePerSh > 0 ? "var(--good)" : changePerSh < 0 ? "var(--bad)" : "var(--text-2)",
                    }}>
                      {changePerSh == null ? "—" : `${changePerSh > 0 ? "+" : ""}${changePerSh.toFixed(2)}`}
                    </td>
                    <td className={`${td} text-right num`} style={{
                      color: pctVsExit == null ? "var(--muted)" : pctVsExit > 0 ? "var(--good)" : pctVsExit < 0 ? "var(--bad)" : "var(--text-2)",
                    }} title="Current price vs exit price">
                      {pctVsExit == null ? "—" : `${pctVsExit > 0 ? "+" : ""}${pctVsExit.toFixed(2)}%`}
                    </td>
                    <td className={td}>
                      <span className="text-xs px-2 py-0.5 rounded-full font-medium" style={{ background: st.bg, color: st.color }}>
                        {st.label}
                      </span>
                    </td>
                    <td className={`${td} text-right`}>
                      <div className="inline-flex items-center gap-2 justify-end whitespace-nowrap">
                        <div className="inline-flex items-stretch rounded-md overflow-hidden"
                             style={{ border: "1px solid var(--border)", opacity: canReenter ? 1 : 0.4 }}>
                          <select value={choice} disabled={!canReenter}
                                  onChange={(e) => {
                                    setRowChoice((m) => ({ ...m, [rowKey]: e.target.value as ReChoice }));
                                    setRowVal((m) => ({ ...m, [rowKey]: "" }));
                                  }}
                                  aria-label={`Re-entry type for ${p.symbol}`}
                                  className="text-xs px-1.5 py-1 outline-none"
                                  style={{ background: "var(--panel-2)", border: "none", color: "var(--text)" }}>
                            <option value="market">Market</option>
                            <option value="pct_current">% below Market</option>
                            <option value="pct_reference" disabled={p.instrument_type === "option"}>% below PDC</option>
                            <option value="at_exit">% below Exit</option>
                            <option value="trailing" disabled={p.instrument_type === "option"}>Trailing %</option>
                            <option value="limit">Limit $</option>
                          </select>
                          {(takesPct(choice) || choice === "limit") && (
                            <div className="inline-flex items-center gap-0.5 px-1.5"
                                 style={{ background: "var(--panel)", borderLeft: "1px solid var(--border)" }}>
                              {choice === "limit" && <span className="text-[9px]" style={{ color: "var(--muted)" }}>$</span>}
                              <PercentInput min="0" max={choice === "limit" ? undefined : 100} step={choice === "limit" ? 0.01 : 0.5}
                                     value={rowVal[rowKey] ?? ""} disabled={!canReenter}
                                     onChange={(e) => setRowVal((m) => ({ ...m, [rowKey]: e.target.value }))}
                                     placeholder={choice === "limit" ? "price" : choice === "trailing" ? "trail" : "%"}
                                     aria-label={`${choice === "limit" ? "Limit price" : choice === "trailing" ? "Trail percent" : "Percent below"} for ${p.symbol}`}
                                     className="w-14 text-xs py-0.5 outline-none"
                                     style={{ background: "transparent", border: "none", color: "var(--text)" }} />
                              {choice !== "limit" && <span className="text-[9px]" style={{ color: "var(--muted)" }}>%</span>}
                            </div>
                          )}
                        </div>
                        {targetPx != null && (
                          <span className="text-[10px] num" style={{ color: "var(--muted)" }}>
                            = ${targetPx.toFixed(2)}
                          </span>
                        )}
                        <button type="button" onClick={() => reEnter(rowKey)}
                                disabled={busy !== null || !canReenter}
                                className="px-2.5 py-1 rounded-lg text-xs font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
                                style={{ background: "var(--panel-2)", color: "var(--text)", border: "1px solid var(--border)" }}>
                          {busy === rowKey ? "…" : "Re-Enter"}
                        </button>
                        <button type="button" onClick={() => deleteRow(i)}
                                disabled={busy !== null}
                                title="Remove this order from the snapshot"
                                aria-label={`Remove ${positionLabel(p)} from snapshot`}
                                className="px-2 py-1 rounded-lg text-xs font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
                                style={{ background: "rgba(239,68,68,0.10)", color: "var(--bad)", border: "1px solid rgba(239,68,68,0.25)" }}>
                          {busy === "delrow:" + i ? "…" : "✕"}
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export default function SnapshotPage() {
  // ?id=… opens one specific snapshot from History; no id = today's snapshots.
  const idParam = useSearchParams().get("id");
  const [access, setAccess] = useState<boolean | null>(null); // null = still checking
  const [today, setToday] = useState<Snapshot[] | null>(null);
  const [loading, setLoading] = useState(true);

  const checkAccess = useCallback(() => {
    api<User>("/api/auth/me")
      .then((u) => setAccess((u.role === "trader" || u.role === "subscriber") && !!u.sell_all_access))
      .catch(() => setAccess(false));
  }, []);
  useEffect(() => { checkAccess(); }, [checkAccess]);

  // Load today's snapshots (default view). A new Exit adds another one, so
  // reload the set on order events too.
  const loadToday = useCallback(async () => {
    if (idParam) { setLoading(false); return; }
    try {
      const r = await api<{ snapshots: Snapshot[] }>("/api/positions/snapshots/today");
      setToday(r.snapshots);
    } catch (e) {
      notify.fromError(e, "Could not load today's snapshots");
    } finally {
      setLoading(false);
    }
  }, [idParam]);
  useEffect(() => { loadToday(); }, [loadToday]);

  useEventStream((evt) => {
    if (typeof evt?.type !== "string") return;
    if (evt.type === "access.sell_all_changed") { checkAccess(); return; }
    // A fresh Exit creates a new snapshot — pull the day's set again so it shows
    // up without a manual refresh. (Each block also self-reloads on order.*)
    if (evt.type.startsWith("order.")) loadToday();
  });

  if (access === false) {
    return (
      <div className="space-y-5">
        <h2 className="text-xl font-bold">Exit Snapshot</h2>
        <div className="rounded-xl p-10 text-center" style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
          The Snapshot &amp; Re-entry feature isn&apos;t enabled for your account. Ask an admin to enable it.
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-xl font-bold">Exit Snapshot</h2>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            {idParam
              ? <>A single snapshot from your history. Re-enter any order individually or all at once.</>
              : <>Every snapshot you&apos;ve taken <b>today</b>, newest first — each Exit keeps its own, so nothing disappears. Re-enter any order at market, or a % below its exit price.</>}
          </p>
          <Link href="/snapshot/history" className="text-sm font-medium inline-block mt-1.5" style={{ color: "var(--accent)" }}>
            View snapshot history →
          </Link>
          {idParam && (
            <div className="text-xs mt-1" style={{ color: "var(--muted)" }}>
              Viewing a past snapshot. <Link href="/snapshot" style={{ color: "var(--accent)" }}>Back to today →</Link>
            </div>
          )}
        </div>
      </div>

      {idParam ? (
        // One specific snapshot from the history.
        <SnapshotBlock key={idParam} snapshotId={idParam} />
      ) : loading ? (
        <div style={{ color: "var(--muted)" }}>Loading…</div>
      ) : !today || today.length === 0 ? (
        <div className="rounded-xl p-10 text-center" style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
          No snapshot taken today. Use <b>Exit My Positions</b> on the Trade Panel to close your positions — a snapshot is saved
          automatically, and you can re-enter it here. <Link href="/snapshot/history" style={{ color: "var(--accent)" }}>Browse history →</Link>
        </div>
      ) : (
        // All of today's snapshots, stacked newest first.
        <div className="space-y-8">
          {today.map((s) => <SnapshotBlock key={s.id} snapshotId={s.id} initial={s} />)}
        </div>
      )}
    </div>
  );
}
