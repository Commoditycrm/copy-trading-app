"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

interface AdminUser {
  id: string;
  email: string;
  role: string;
  display_name: string | null;
  business_name: string | null;
  is_active: boolean;
  /** Admin allow-list for the Sell-All / Snapshot / Re-entry suite. */
  sell_all_access: boolean;
  created_at: string;
}

export default function AdminTradersPage() {
  const router = useRouter();
  const [traders, setTraders] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [hideFor, setHideFor] = useState<AdminUser | null>(null); // trader in the hide-history modal

  async function toggleSellAll(t: AdminUser) {
    const enabled = !t.sell_all_access;
    setBusy(t.id);
    try {
      await api(`/api/admin/users/${t.id}/sell-all-access`, {
        method: "PATCH",
        body: JSON.stringify({ enabled }),
      });
      notify.success(`Sell-All ${enabled ? "enabled" : "disabled"} for ${t.email}`);
      setTraders(ts => ts.map(x => x.id === t.id ? { ...x, sell_all_access: enabled } : x));
    } catch (e) {
      notify.fromError(e, "Could not update Sell-All access");
    } finally {
      setBusy(null);
    }
  }

  useEffect(() => {
    (async () => {
      try {
        // Server filters to traders + excludes load-test users. High limit keeps
        // this (small) trader list on one page for the client-side search box.
        const page = await api<{ items: AdminUser[] }>("/api/admin/users?role=trader&limit=200");
        setTraders(page.items);
      } catch (e) {
        notify.fromError(e, "Could not load traders");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const filtered = traders.filter(t => {
    if (!search) return true;
    const q = search.toLowerCase();
    return (
      t.email.toLowerCase().includes(q) ||
      (t.display_name ?? "").toLowerCase().includes(q) ||
      (t.business_name ?? "").toLowerCase().includes(q)
    );
  });

  function open(t: AdminUser) {
    const name = t.display_name || t.business_name || t.email;
    router.push(`/admin/traders/${t.id}?name=${encodeURIComponent(name)}`);
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-bold">Traders</h2>
        <p className="text-sm mt-0.5" style={{ color: "var(--muted)" }}>
          {traders.length} trader{traders.length === 1 ? "" : "s"} · click a row to view that trader&apos;s performance table.
        </p>
      </div>

      <input
        type="text"
        placeholder="Search name, business, or email…"
        aria-label="Search traders"
        value={search}
        onChange={e => setSearch(e.target.value)}
        className="text-sm px-3 py-1.5 rounded-lg"
        style={{ background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)", color: "var(--text)", outline: "none", minWidth: 260 }}
      />

      {loading ? (
        <div style={{ color: "var(--muted)" }}>Loading traders…</div>
      ) : (
        <div className="rounded-xl overflow-auto" style={{ border: "1px solid var(--border)", maxHeight: "70vh" }}>
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
              <tr style={{ background: "rgba(255,255,255,0.03)", borderBottom: "1px solid var(--border)" }}>
                {["Trader", "Business", "Status", "Sell-All", "P&L", "Joined", ""].map(h => (
                  <th key={h} className="text-left px-4 py-3 font-semibold" style={{ color: "var(--text-2)" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr><td colSpan={7} className="px-4 py-8 text-center" style={{ color: "var(--muted)" }}>No traders match.</td></tr>
              ) : (
                filtered.map((t, i) => (
                  <tr
                    key={t.id}
                    onClick={() => open(t)}
                    className="cursor-pointer transition-colors hover:bg-[var(--panel-2)]"
                    style={{ borderBottom: i < filtered.length - 1 ? "1px solid var(--border)" : "none" }}
                    title="View performance"
                  >
                    <td className="px-4 py-3">
                      <div className="font-medium">{t.display_name ?? t.email}</div>
                      {t.display_name && <div className="text-xs" style={{ color: "var(--muted)" }}>{t.email}</div>}
                    </td>
                    <td className="px-4 py-3" style={{ color: "var(--text-2)" }}>{t.business_name ?? "—"}</td>
                    <td className="px-4 py-3">
                      <span className="text-xs font-medium px-2 py-0.5 rounded-full" style={{
                        background: t.is_active ? "rgba(34,197,94,0.12)" : "rgba(239,68,68,0.12)",
                        color: t.is_active ? "#22c55e" : "#ef4444",
                      }}>
                        {t.is_active ? "Active" : "Inactive"}
                      </span>
                    </td>
                    {/* Sell-All access toggle — clear ON (solid green) / OFF (grey).
                        stopPropagation so it doesn't open the trader detail row. */}
                    <td className="px-4 py-3">
                      <button
                        type="button"
                        disabled={busy === t.id}
                        onClick={(e) => { e.stopPropagation(); toggleSellAll(t); }}
                        title={t.sell_all_access
                          ? "Sell-All enabled — click to disable"
                          : "Sell-All disabled — click to enable"}
                        className="inline-flex items-center gap-1.5 text-xs font-semibold px-2.5 py-1 rounded-full transition-colors disabled:opacity-50"
                        style={{
                          background: t.sell_all_access ? "var(--good)" : "var(--panel-2)",
                          color: t.sell_all_access ? "#fff" : "var(--muted)",
                          border: `1px solid ${t.sell_all_access ? "var(--good)" : "var(--border)"}`,
                          cursor: busy === t.id ? "not-allowed" : "pointer",
                        }}
                      >
                        <span style={{
                          width: 7, height: 7, borderRadius: "9999px",
                          background: t.sell_all_access ? "#fff" : "var(--muted)",
                        }} />
                        {t.sell_all_access ? "ON" : "OFF"}
                      </button>
                    </td>
                    {/* Hide P&L — opens the hide/unhide modal; stopPropagation so
                        it doesn't open the trader detail row. */}
                    <td className="px-4 py-3">
                      <button
                        type="button"
                        onClick={(e) => { e.stopPropagation(); setHideFor(t); }}
                        title="Hide this trader's order history + P&L"
                        className="text-xs px-3 py-1 rounded-lg transition-colors"
                        style={{ background: "var(--panel-2)", color: "var(--text-2)", border: "1px solid var(--border)" }}
                      >
                        Hide P&amp;L
                      </button>
                    </td>
                    <td className="px-4 py-3 text-xs" style={{ color: "var(--muted)" }}>
                      {new Date(t.created_at).toLocaleDateString("en-US", { timeZone: "America/New_York" })}
                    </td>
                    <td className="px-4 py-3 text-right" style={{ color: "var(--muted)" }}>›</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {hideFor && (
        <HideHistoryModal user={hideFor} onClose={() => setHideFor(null)} />
      )}
    </div>
  );
}

// ── Hide / unhide a trader's order history + P&L ──────────────────────────────
// Soft-delete: rows keep their DB records so a broker re-sync won't recreate
// them, but they vanish from every history / P&L view. Reversible via Unhide.
function HideHistoryModal({
  user, onClose,
}: {
  user: AdminUser;
  onClose: () => void;
}) {
  const [from, setFrom] = useState("");
  const [to, setTo]     = useState("");
  const [busy, setBusy] = useState(false);
  const [counts, setCounts] = useState<{ orders_hidden: number; snapshots_hidden: number } | null>(null);

  const refreshCounts = useCallback(async () => {
    try {
      setCounts(await api(`/api/admin/users/${user.id}/orders/hidden-count`));
    } catch { /* non-blocking */ }
  }, [user.id]);
  useEffect(() => { refreshCounts(); }, [refreshCounts]);

  async function hide() {
    // End date required; start optional (empty = from the beginning). The API's
    // `to` is exclusive, so send the day AFTER the picked end date to make the
    // selected end day inclusive ("hide up to and including this date").
    if (!to) { notify.error("Pick an end date"); return; }
    const end = new Date(`${to}T00:00:00Z`);
    end.setUTCDate(end.getUTCDate() + 1);
    const body: Record<string, string> = { to: end.toISOString().slice(0, 10) };
    if (from) body.from = from;
    setBusy(true);
    try {
      const res = await api<{ orders_hidden: number; snapshots_hidden: number }>(
        `/api/admin/users/${user.id}/orders/hide`,
        { method: "POST", body: JSON.stringify(body) },
      );
      notify.success(`Hid ${res.orders_hidden} orders + ${res.snapshots_hidden} P&L days`);
      await refreshCounts();
    } catch (e) {
      notify.fromError(e, "Could not hide history");
    } finally {
      setBusy(false);
    }
  }

  async function unhideAll() {
    setBusy(true);
    try {
      const res = await api<{ orders_unhidden: number; snapshots_unhidden: number }>(
        `/api/admin/users/${user.id}/orders/unhide`,
        { method: "POST", body: JSON.stringify({}) },
      );
      notify.success(`Restored ${res.orders_unhidden} orders + ${res.snapshots_unhidden} P&L days`);
      await refreshCounts();
    } catch (e) {
      notify.fromError(e, "Could not unhide history");
    } finally {
      setBusy(false);
    }
  }

  const nHidden = (counts?.orders_hidden ?? 0) + (counts?.snapshots_hidden ?? 0);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.55)" }}
      onClick={onClose}
    >
      <div
        className="rounded-xl w-full max-w-md p-5 space-y-4"
        style={{ background: "var(--panel)", border: "1px solid var(--border)" }}
        onClick={e => e.stopPropagation()}
      >
        <div>
          <h3 className="text-lg font-bold">Hide order history &amp; P&amp;L</h3>
          <p className="text-sm mt-0.5" style={{ color: "var(--muted)" }}>{user.email}</p>
        </div>

        {counts && nHidden > 0 && (
          <div
            className="text-xs px-3 py-2 rounded-lg"
            style={{ background: "rgba(250,204,21,0.08)", color: "#facc15", border: "1px solid rgba(250,204,21,0.2)" }}
          >
            Currently hidden: {counts.orders_hidden} orders, {counts.snapshots_hidden} P&amp;L days.
          </div>
        )}

        <div className="space-y-3 text-sm">
          <div className="flex items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs" style={{ color: "var(--muted)" }}>Start date (optional)</span>
              <input
                type="date" value={from} aria-label="Start date (optional; empty = from the beginning)"
                onChange={e => setFrom(e.target.value)}
                className="text-xs px-2 py-1 rounded-lg"
                style={{ background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)", color: "var(--text)" }}
              />
            </label>
            <span className="pb-1.5" style={{ color: "var(--muted)" }}>→</span>
            <label className="flex flex-col gap-1">
              <span className="text-xs" style={{ color: "var(--muted)" }}>End date</span>
              <input
                type="date" value={to} aria-label="End date (inclusive)"
                onChange={e => setTo(e.target.value)}
                className="text-xs px-2 py-1 rounded-lg"
                style={{ background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)", color: "var(--text)" }}
              />
            </label>
          </div>
          <p className="text-xs" style={{ color: "var(--muted)" }}>
            Hides everything up to and including the end date. Leave the start empty
            to hide from the beginning. Hidden rows survive a broker re-sync and stay
            hidden until you Unhide.
          </p>
        </div>

        <div className="flex items-center justify-between pt-2">
          <button
            disabled={busy || nHidden === 0}
            onClick={unhideAll}
            className="text-sm px-3 py-1.5 rounded-lg"
            style={{
              background: "rgba(34,197,94,0.10)", color: "#22c55e",
              border: "1px solid rgba(34,197,94,0.25)",
              cursor: busy || nHidden === 0 ? "not-allowed" : "pointer",
              opacity: nHidden === 0 ? 0.5 : 1,
            }}
          >
            Unhide all
          </button>
          <div className="flex items-center gap-2">
            <button
              disabled={busy}
              onClick={onClose}
              className="text-sm px-3 py-1.5 rounded-lg"
              style={{ background: "var(--panel-2)", color: "var(--text-2)", border: "1px solid var(--border)" }}
            >
              Cancel
            </button>
            <button
              disabled={busy}
              onClick={hide}
              className="text-sm px-3 py-1.5 rounded-lg font-semibold"
              style={{
                background: "rgba(239,68,68,0.12)", color: "#ef4444",
                border: "1px solid rgba(239,68,68,0.3)",
                cursor: busy ? "not-allowed" : "pointer",
              }}
            >
              {busy ? "Working…" : "Hide"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
