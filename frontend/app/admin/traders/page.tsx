"use client";

import { useEffect, useState } from "react";
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
                {["Trader", "Business", "Status", "Sell-All", "Joined", ""].map(h => (
                  <th key={h} className="text-left px-4 py-3 font-semibold" style={{ color: "var(--text-2)" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr><td colSpan={6} className="px-4 py-8 text-center" style={{ color: "var(--muted)" }}>No traders match.</td></tr>
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
    </div>
  );
}
