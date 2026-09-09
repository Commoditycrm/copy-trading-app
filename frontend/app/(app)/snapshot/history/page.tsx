"use client";

/**
 * Snapshot history — every Sell-All snapshot the user has taken, newest first.
 * Click one to open its detail (/snapshot?id=…); delete removes just the
 * re-entry record, never any orders already placed.
 */
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import type { User } from "@/lib/types";

interface SnapListItem {
  id: string;
  created_at: string;
  active: boolean;
  total: number;
  filled: number;
  working: number;
  pending: number;
  expired: number;
}

export default function SnapshotHistoryPage() {
  const router = useRouter();
  const [snaps, setSnaps] = useState<SnapListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [access, setAccess] = useState<boolean | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api<{ snapshots: SnapListItem[] }>("/api/positions/snapshots");
      setSnaps(r.snapshots);
    } catch (e) {
      notify.fromError(e, "Could not load snapshot history");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    api<User>("/api/auth/me")
      .then((u) => setAccess((u.role === "trader" || u.role === "subscriber") && !!u.sell_all_access))
      .catch(() => setAccess(false));
  }, []);
  useEffect(() => { load(); }, [load]);

  async function del(id: string) {
    if (!confirm("Delete this snapshot? This only removes the re-entry record — it does NOT touch any orders you already placed.")) return;
    setBusy(id);
    try {
      await api(`/api/positions/snapshots/${id}`, { method: "DELETE" });
      notify.success("Snapshot deleted.");
      setSnaps((xs) => xs.filter((s) => s.id !== id));
    } catch (e) {
      notify.fromError(e, "Could not delete snapshot");
    } finally {
      setBusy(null);
    }
  }

  if (access === false) {
    return (
      <div className="space-y-5">
        <h2 className="text-xl font-bold">Snapshot History</h2>
        <div className="rounded-xl p-10 text-center" style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
          The Snapshot &amp; Re-entry feature isn&apos;t enabled for your account. Ask an admin to enable it.
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-bold">Snapshot History</h2>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          Every Exit snapshot you&apos;ve taken. Click one to open it and re-enter its orders.
        </p>
        <Link href="/snapshot" className="text-sm font-medium inline-block mt-1.5" style={{ color: "var(--accent)" }}>
          ← Current snapshot
        </Link>
      </div>

      {loading ? (
        <div style={{ color: "var(--muted)" }}>Loading…</div>
      ) : snaps.length === 0 ? (
        <div className="rounded-xl p-10 text-center" style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
          No snapshots yet. Use <b>Exit My Positions</b> on the Trade Panel to take one.
        </div>
      ) : (
        <div className="rounded-xl overflow-hidden" style={{ border: "1px solid var(--border)" }}>
          <table className="w-full">
            <thead style={{ background: "var(--panel)" }}>
              <tr style={{ borderBottom: "1px solid var(--border)" }}>
                <th className="px-4 py-3 text-xs font-semibold text-left" style={{ color: "var(--muted)" }}>Taken</th>
                <th className="px-4 py-3 text-xs font-semibold text-right" style={{ color: "var(--muted)" }}>Orders</th>
                <th className="px-4 py-3 text-xs font-semibold text-left" style={{ color: "var(--muted)" }}>Status</th>
                <th className="px-4 py-3 text-xs font-semibold text-right" style={{ color: "var(--muted)" }}></th>
              </tr>
            </thead>
            <tbody>
              {snaps.map((s) => (
                <tr key={s.id}
                    onClick={() => router.push(`/snapshot?id=${s.id}`)}
                    className="cursor-pointer transition-colors hover:brightness-110"
                    style={{ borderBottom: "1px solid var(--border)" }}>
                  <td className="px-4 py-3 text-sm">
                    {new Date(s.created_at).toLocaleString()}
                    {s.active && (
                      <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded-full align-middle"
                            style={{ background: "var(--good-soft)", color: "var(--good)" }}>current</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-sm text-right num">{s.total}</td>
                  <td className="px-4 py-3 text-sm" style={{ color: "var(--text-2)" }}>
                    <span style={{ color: "var(--good)" }}>{s.filled}/{s.total} back</span>
                    {s.working > 0 && <span style={{ color: "#facc15" }}> · {s.working} resting</span>}
                    {s.pending > 0 && <span style={{ color: "var(--muted)" }}> · {s.pending} to go</span>}
                    {s.expired > 0 && <span style={{ color: "var(--bad)" }}> · {s.expired} expired</span>}
                  </td>
                  <td className="px-4 py-3 text-right whitespace-nowrap">
                    <Link href={`/snapshot?id=${s.id}`} onClick={(e) => e.stopPropagation()}
                          className="text-xs font-semibold px-3 py-1 rounded-lg"
                          style={{ background: "var(--panel-2)", color: "var(--text)", border: "1px solid var(--border)" }}>
                      Open →
                    </Link>
                    <button type="button" disabled={busy === s.id}
                            onClick={(e) => { e.stopPropagation(); del(s.id); }}
                            className="ml-2 text-xs font-semibold px-3 py-1 rounded-lg disabled:opacity-50"
                            style={{ background: "rgba(239,68,68,0.10)", color: "var(--bad)", border: "1px solid rgba(239,68,68,0.25)" }}>
                      {busy === s.id ? "Deleting…" : "Delete"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
