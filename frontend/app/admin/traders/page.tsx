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
  created_at: string;
}

export default function AdminTradersPage() {
  const router = useRouter();
  const [traders, setTraders] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const data = await api<AdminUser[]>("/api/admin/users");
        // Traders only; drop fake load-test rows (managed on the Load Test page).
        setTraders(data.filter(u => u.role === "trader" && !u.email.startsWith("fake-load-test-")));
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
                {["Trader", "Business", "Status", "Joined", ""].map(h => (
                  <th key={h} className="text-left px-4 py-3 font-semibold" style={{ color: "var(--text-2)" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr><td colSpan={5} className="px-4 py-8 text-center" style={{ color: "var(--muted)" }}>No traders match.</td></tr>
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
