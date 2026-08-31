"use client";

/**
 * Snapshot page — the full detail of the current Sell-All exit snapshot:
 * when it was taken, every order's exit price, each order's re-entry status,
 * and a per-order Re-Enter (at market or a % below the exit price). Plus a
 * Re-Enter All for the pending ones. Fill-aware: filled/resting orders are
 * skipped so you never double-buy.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

type Status = "filled" | "working" | "pending";

interface SnapPos {
  symbol: string;
  instrument_type: string;
  quantity: string;            // signed
  price: string | null;        // exit price / share
  option_expiry: string | null;
  option_strike: string | null;
  option_right: string | null;
  reentry_status: Status;
}
interface Snapshot {
  id: string;
  created_at: string;
  positions: SnapPos[];
  summary: { total: number; filled: number; working: number; pending: number };
}

const STATUS_STYLE: Record<Status, { bg: string; color: string; label: string }> = {
  filled:  { bg: "var(--good-soft)", color: "var(--good)", label: "Back in" },
  working: { bg: "rgba(250,204,21,0.12)", color: "#facc15", label: "Resting" },
  pending: { bg: "var(--panel-2)", color: "var(--muted)", label: "To re-enter" },
};

function fmtMoney(v: string | null): string {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return Number.isFinite(n) ? `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : String(v);
}

export default function SnapshotPage() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [globalDisc, setGlobalDisc] = useState("");
  const [rowDisc, setRowDisc] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null); // "all" or a symbol

  const load = useCallback(async () => {
    try {
      const r = await api<{ snapshot: Snapshot | null }>("/api/positions/snapshots/latest");
      setSnap(r.snapshot);
    } catch (e) {
      notify.fromError(e, "Could not load snapshot");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Poll while anything is unresolved so fills flip to "Back in" live.
  useEffect(() => {
    if (!snap || snap.summary.pending + snap.summary.working === 0) return;
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, [snap, load]);

  function discFor(sym: string): number | null {
    const raw = rowDisc[sym] ?? globalDisc;
    const d = parseFloat(raw);
    return !isNaN(d) && d > 0 && d <= 100 ? d : null;
  }

  async function reEnter(scope: "all" | string) {
    setBusy(scope);
    try {
      const params = new URLSearchParams();
      if (scope === "all") {
        const d = parseFloat(globalDisc);
        if (!isNaN(d) && d > 0 && d <= 100) params.set("discount_percent", String(d));
      } else {
        params.set("symbol", scope);
        const d = discFor(scope);
        if (d !== null) params.set("discount_percent", String(d));
      }
      const qs = params.toString();
      const res = await api<{ placed_count: number; skipped_count: number; failed_count: number }>(
        `/api/positions/re-enter${qs ? `?${qs}` : ""}`,
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

  // Staleness: exit prices age with the snapshot, so re-entering "% below" an
  // old price stops being meaningful. Warn once it's a couple of days old.
  const ageDays = snap ? Math.floor((Date.now() - new Date(snap.created_at).getTime()) / 86_400_000) : 0;
  const ageLabel = ageDays <= 0 ? "today" : ageDays === 1 ? "1 day ago" : `${ageDays} days ago`;
  const stale = ageDays >= 2;

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-xl font-bold">Exit Snapshot</h2>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            The positions from your last <b>Exit All</b>, with each order&apos;s exit price. Re-enter any order
            individually — at market, or a % below its exit price.
          </p>
        </div>
        {snap && (
          <div className="text-sm text-right" style={{ color: "var(--text-2)" }}>
            <div>
              <span style={{ color: "var(--muted)" }}>Taken:</span> {new Date(snap.created_at).toLocaleString()}
              <span style={{ color: stale ? "var(--bad)" : "var(--muted)" }}> ({ageLabel})</span>
            </div>
            <div className="mt-0.5">
              <span style={{ color: "var(--good)" }}>{snap.summary.filled}/{snap.summary.total} back in</span>
              {snap.summary.working > 0 && <span style={{ color: "#facc15" }}> · {snap.summary.working} resting</span>}
              {snap.summary.pending > 0 && <span style={{ color: "var(--muted)" }}> · {snap.summary.pending} to go</span>}
            </div>
          </div>
        )}
      </div>

      {loading ? (
        <div style={{ color: "var(--muted)" }}>Loading…</div>
      ) : !snap || snap.positions.length === 0 ? (
        <div className="rounded-xl p-10 text-center" style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
          No snapshot yet. Use <b>Exit My Positions</b> on the Trade Panel to close your positions — a snapshot is saved automatically, and you can re-enter it here.
        </div>
      ) : (
        <>
          {stale && (
            <div className="rounded-xl px-4 py-2.5 text-sm"
                 style={{ background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.3)", color: "var(--bad)" }}>
              ⚠ This snapshot was taken <b>{ageLabel}</b> — the exit prices are stale, so re-entering a
              &ldquo;% below&rdquo; the exit price may not reflect the current market. Prefer a market re-entry,
              or take a fresh Exit snapshot.
            </div>
          )}

          {/* Re-Enter All bar */}
          <div className="rounded-xl px-4 py-3 flex items-center justify-between gap-3 flex-wrap"
               style={{ background: "var(--panel)", border: "1px solid var(--border)" }}>
            <span className="text-sm" style={{ color: "var(--text-2)" }}>
              Re-enter everything not back yet ({snap.summary.pending} pending):
            </span>
            <div className="flex items-center gap-2">
              <div className="inline-flex items-center gap-1.5 px-2 py-1 rounded-lg"
                   style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}
                   title="Optional: re-buy each at this % below its exit price. Empty = market.">
                <span className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: "var(--text-2)" }}>%&nbsp;below</span>
                <input type="number" min="0" max="100" step="0.5" value={globalDisc}
                       onChange={(e) => setGlobalDisc(e.target.value)} placeholder="mkt"
                       aria-label="Discount percent for Re-Enter All"
                       className="w-14 text-xs rounded-md px-1.5 py-0.5 outline-none"
                       style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--text)" }} />
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
                    <th className={`${th} text-right`} style={{ color: "var(--muted)" }}>Exit Price</th>
                    <th className={`${th} text-left`} style={{ color: "var(--muted)" }}>Status</th>
                    <th className={`${th} text-right`} style={{ color: "var(--muted)" }}>Re-Enter</th>
                  </tr>
                </thead>
                <tbody>
                  {snap.positions.map((p) => {
                    const qty = Number(p.quantity);
                    const side = qty >= 0 ? "Long" : "Short";
                    const st = STATUS_STYLE[p.reentry_status];
                    const canReenter = p.reentry_status === "pending";
                    return (
                      <tr key={p.symbol} style={{ borderBottom: "1px solid var(--border)" }}>
                        <td className={`${td} font-medium`}>{p.symbol}</td>
                        <td className={td} style={{ color: qty >= 0 ? "var(--good)" : "var(--bad)" }}>{side}</td>
                        <td className={`${td} text-right num`}>{Math.abs(qty)}</td>
                        <td className={`${td} text-right num`}>{fmtMoney(p.price)}</td>
                        <td className={td}>
                          <span className="text-xs px-2 py-0.5 rounded-full font-medium" style={{ background: st.bg, color: st.color }}>
                            {st.label}
                          </span>
                        </td>
                        <td className={`${td} text-right`}>
                          <div className="inline-flex items-center gap-2 justify-end">
                            <div className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md"
                                 style={{ background: "var(--panel-2)", border: "1px solid var(--border)", opacity: canReenter ? 1 : 0.4 }}
                                 title="Optional: % below this order's exit price. Empty = market (or the top box's value).">
                              <input type="number" min="0" max="100" step="0.5"
                                     value={rowDisc[p.symbol] ?? ""} disabled={!canReenter}
                                     onChange={(e) => setRowDisc((m) => ({ ...m, [p.symbol]: e.target.value }))}
                                     placeholder={globalDisc || "mkt"}
                                     aria-label={`Discount percent for ${p.symbol}`}
                                     className="w-12 text-xs rounded px-1 py-0.5 outline-none"
                                     style={{ background: "var(--panel)", border: "1px solid var(--border)", color: "var(--text)" }} />
                              <span className="text-[9px]" style={{ color: "var(--muted)" }}>%</span>
                            </div>
                            <button type="button" onClick={() => reEnter(p.symbol)}
                                    disabled={busy !== null || !canReenter}
                                    className="px-2.5 py-1 rounded-lg text-xs font-semibold disabled:opacity-50 disabled:cursor-not-allowed"
                                    style={{ background: "var(--panel-2)", color: "var(--text)", border: "1px solid var(--border)" }}>
                              {busy === p.symbol ? "…" : "Re-Enter"}
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
        </>
      )}
    </div>
  );
}
