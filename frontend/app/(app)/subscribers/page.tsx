"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import type { SubscriberSummary } from "@/lib/types";

export default function SubscribersPage() {
  const [rows, setRows] = useState<SubscriberSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState<{ ids: string[]; label: string } | null>(null);
  const [removing, setRemoving] = useState(false);

  async function load() {
    try { setRows(await api<SubscriberSummary[]>("/api/subscribers")); }
    catch (e) { notify.fromError(e, "Could not load subscribers"); }
    finally { setLoading(false); }
  }
  useEffect(() => { load(); }, []);

  // Drop selections that point at rows no longer present (e.g. after a refresh
  // or bulk delete). Keeps "Delete selected (N)" honest.
  useEffect(() => {
    const visible = new Set(rows.map(r => r.user_id));
    setSelected(prev => {
      const next = new Set<string>();
      prev.forEach(id => { if (visible.has(id)) next.add(id); });
      return next.size === prev.size ? prev : next;
    });
  }, [rows]);

  const allSelected = rows.length > 0 && selected.size === rows.length;
  const someSelected = selected.size > 0 && selected.size < rows.length;

  function toggleAll() {
    if (allSelected) setSelected(new Set());
    else setSelected(new Set(rows.map(r => r.user_id)));
  }

  function toggleOne(id: string) {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function requestRemove(ids: string[]) {
    if (ids.length === 0) return;
    const label = ids.length === 1
      ? rows.find(r => r.user_id === ids[0])?.display_name
        ?? rows.find(r => r.user_id === ids[0])?.email
        ?? "this subscriber"
      : `${ids.length} subscribers`;
    setConfirming({ ids, label });
  }

  async function confirmRemove() {
    if (!confirming) return;
    setRemoving(true);
    try {
      let removed = 0;
      if (confirming.ids.length === 1) {
        const res = await api<{ removed: number }>(`/api/subscribers/${confirming.ids[0]}`, {
          method: "DELETE",
        });
        removed = res.removed ?? 1;
      } else {
        const res = await api<{ removed: number }>("/api/subscribers/bulk-remove", {
          method: "POST",
          body: JSON.stringify({ subscriber_ids: confirming.ids }),
        });
        removed = res.removed ?? 0;
      }
      notify.success(
        removed === 1
          ? "Subscriber removed"
          : `${removed} subscribers removed`
      );
      setSelected(new Set());
      setConfirming(null);
      load();
    } catch (e) {
      notify.fromError(e, "Could not remove subscriber(s)");
    } finally {
      setRemoving(false);
    }
  }

  const bulkBar = useMemo(() => {
    if (selected.size === 0) return null;
    return (
      <div
        className="flex items-center justify-between px-3 py-2 rounded border"
        style={{ borderColor: "var(--border)", background: "var(--panel)" }}
      >
        <div className="text-sm">
          <strong>{selected.size}</strong> selected
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setSelected(new Set())}
            className="px-3 py-1 text-sm rounded border"
            style={{ borderColor: "var(--border)" }}
          >
            Clear
          </button>
          <button
            onClick={() => requestRemove(Array.from(selected))}
            className="px-3 py-1 text-sm rounded"
            style={{ background: "var(--bad)", color: "#fff" }}
          >
            Remove selected ({selected.size})
          </button>
        </div>
      </div>
    );
  }, [selected]);

  return (
    <div className="space-y-4">
      {bulkBar}

      <div className="overflow-x-auto rounded border" style={{borderColor: "var(--border)"}}>
        <table className="w-full text-sm">
          <thead style={{background: "var(--panel)"}}>
            <tr>
              <th className="px-3 py-2 w-10">
                <input
                  type="checkbox"
                  aria-label="Select all subscribers"
                  checked={allSelected}
                  ref={el => { if (el) el.indeterminate = someSelected; }}
                  onChange={toggleAll}
                  disabled={loading || rows.length === 0}
                />
              </th>
              {["Subscriber", "Copy", "Brokers", "30d realized P&L", ""].map(h =>
                <th key={h} className="text-left px-3 py-2 font-medium" style={{color: "var(--muted)"}}>{h}</th>
              )}
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={6} className="px-3 py-8 text-center" style={{color: "var(--muted)"}}>
                  <span className="inline-flex items-center gap-2">
                    <Spinner />
                    <span>Loading subscribers…</span>
                  </span>
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && (
              <tr><td colSpan={6} className="px-3 py-6 text-center" style={{color: "var(--muted)"}}>No subscribers yet.</td></tr>
            )}
            {rows.map(r => {
              const pnl = Number(r.realized_pnl_30d);
              const isSelected = selected.has(r.user_id);
              return (
                <tr key={r.user_id} className="border-t" style={{borderColor: "var(--border)"}}>
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      aria-label={`Select ${r.email}`}
                      checked={isSelected}
                      onChange={() => toggleOne(r.user_id)}
                    />
                  </td>
                  <td className="px-3 py-2">
                    <div>{r.display_name ?? r.email}</div>
                    <div className="text-xs" style={{color: "var(--muted)"}}>{r.email}</div>
                  </td>
                  <td className="px-3 py-2">
                    <span style={{color: r.copy_enabled ? "var(--good)" : "var(--muted)"}}>
                      {r.copy_enabled ? "ON" : "OFF"}
                    </span>
                  </td>
                  <td className="px-3 py-2">{r.broker_count}</td>
                  <td className="px-3 py-2" style={{color: pnl >= 0 ? "var(--good)" : "var(--bad)"}}>
                    {pnl.toLocaleString(undefined, { style: "currency", currency: "USD" })}
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => requestRemove([r.user_id])}
                      aria-label={`Remove ${r.email}`}
                      title="Remove subscriber"
                      className="px-3 py-1 text-sm rounded border"
                      style={{ borderColor: "var(--border)", color: "var(--bad)" }}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {confirming && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="confirm-remove-title"
          className="fixed inset-0 z-50 flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.5)" }}
          onClick={() => !removing && setConfirming(null)}
        >
          <div
            className="w-full max-w-md rounded border p-4"
            style={{ borderColor: "var(--border)", background: "var(--panel)" }}
            onClick={e => e.stopPropagation()}
          >
            <h2 id="confirm-remove-title" className="text-base font-semibold mb-2">
              Remove {confirming.label}?
            </h2>
            <p className="text-sm mb-4" style={{ color: "var(--muted)" }}>
              They will stop receiving new copy trades from you. Their account,
              broker connections, and order history are preserved — they can
              re-follow you any time.
            </p>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setConfirming(null)}
                disabled={removing}
                className="px-3 py-1 text-sm rounded border"
                style={{ borderColor: "var(--border)" }}
              >
                Cancel
              </button>
              <button
                onClick={confirmRemove}
                disabled={removing}
                className="px-3 py-1 text-sm rounded inline-flex items-center gap-2"
                style={{ background: "var(--bad)", color: "#fff" }}
              >
                {removing && <Spinner />}
                {removing ? "Removing…" : "Remove"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
