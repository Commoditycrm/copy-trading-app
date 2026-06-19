"use client";

import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { Search, Trash2, Users, X } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { ConfirmModal } from "@/components/ConfirmModal";
import { AnimatedNumber } from "@/components/dashboard/AnimatedNumber";
import { fmtSignedUsd } from "@/lib/format";
import type { SubscriberSummary } from "@/lib/types";

export default function SubscribersPage() {
  const [rows, setRows] = useState<SubscriberSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState<{ ids: string[]; label: string } | null>(null);
  const [removing, setRemoving] = useState(false);
  const [search, setSearch] = useState("");

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

  // Presentational filter (by name / email).
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter(r =>
      r.email.toLowerCase().includes(q) ||
      (r.display_name ?? "").toLowerCase().includes(q)
    );
  }, [rows, search]);

  const allSelected = filtered.length > 0 && filtered.every(r => selected.has(r.user_id));
  const someSelected = selected.size > 0 && !allSelected;

  function toggleAll() {
    if (allSelected) setSelected(new Set());
    else setSelected(new Set(filtered.map(r => r.user_id)));
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
        removed === 1 ? "Subscriber removed" : `${removed} subscribers removed`
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

  const total = rows.length;
  const active = rows.filter(r => r.copy_enabled).length;
  const withBroker = rows.filter(r => r.broker_count > 0).length;

  return (
    <div className="max-w-[1100px]">
      {/* Summary tiles */}
      <div className="grid grid-cols-3 gap-3 mb-4">
        <SummaryTile label="Total" node={<AnimatedNumber value={total} format={(n) => String(Math.round(n))} className="num num-lg" />} />
        <SummaryTile label="Copy ON" tone="good" node={<AnimatedNumber value={active} format={(n) => String(Math.round(n))} className="num num-lg" />} />
        <SummaryTile
          label="Broker connected"
          tone={withBroker === total && total > 0 ? "good" : "neutral"}
          node={<AnimatedNumber value={withBroker} format={(n) => String(Math.round(n))} className="num num-lg" />}
        />
      </div>

      {/* Toolbar: bulk actions OR search */}
      <div className="flex items-center justify-between mb-3 gap-3 flex-wrap min-h-[34px]">
        {selected.size > 0 ? (
          <div className="flex items-center gap-3">
            <span className="text-sm"><strong>{selected.size}</strong> selected</span>
            <button onClick={() => setSelected(new Set())} className="btn-ghost px-3 py-1 text-xs">Clear</button>
            <button
              onClick={() => requestRemove(Array.from(selected))}
              className="btn-danger px-3 py-1 text-xs inline-flex items-center gap-1.5"
            >
              <Trash2 size={13} /> Remove selected ({selected.size})
            </button>
          </div>
        ) : <span />}
        <div className="relative ml-auto">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: "var(--muted)" }} />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search name or email…"
            className="pl-8 pr-8 py-1.5 text-sm w-52 sm:w-64"
            aria-label="Search subscribers"
          />
          {search && (
            <button type="button" onClick={() => setSearch("")} aria-label="Clear search"
              className="absolute right-2 top-1/2 -translate-y-1/2 focus-ring rounded" style={{ color: "var(--muted)" }}>
              <X size={14} />
            </button>
          )}
        </div>
      </div>

      <div className="card overflow-hidden">
        <div className="overflow-auto" style={{ maxHeight: "calc(100vh - 280px)" }}>
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10" style={{ background: "var(--panel)", boxShadow: "0 1px 0 var(--border)" }}>
              <tr>
                <th className="px-4 py-3 w-10">
                  <input
                    type="checkbox"
                    aria-label="Select all subscribers"
                    checked={allSelected}
                    ref={el => { if (el) el.indeterminate = someSelected; }}
                    onChange={toggleAll}
                    disabled={loading || filtered.length === 0}
                  />
                </th>
                {["Subscriber", "Copy", "Broker", "30d realized P&L", ""].map(h =>
                  <th key={h} className="text-left px-4 py-3 font-medium" style={{ color: "var(--muted)" }}>{h}</th>
                )}
              </tr>
            </thead>
            <tbody>
              {loading && Array.from({ length: 5 }).map((_, i) => (
                <tr key={`sk-${i}`} className="border-t" style={{ borderColor: "var(--border)" }}>
                  {Array.from({ length: 6 }).map((__, j) => (
                    <td key={j} className="px-4 py-3.5"><div className="skeleton h-4 w-full" style={{ minWidth: 40 }} /></td>
                  ))}
                </tr>
              ))}
              {!loading && filtered.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-3 py-14">
                    <div className="flex flex-col items-center justify-center text-center gap-2" style={{ color: "var(--muted)" }}>
                      <Users size={28} />
                      <div className="text-sm" style={{ color: "var(--text)" }}>
                        {rows.length === 0 ? "No subscribers yet" : `No subscribers match “${search}”`}
                      </div>
                      <div className="text-xs">People who follow you and turn copy on will appear here.</div>
                    </div>
                  </td>
                </tr>
              )}
              {!loading && filtered.map(r => {
                const isSelected = selected.has(r.user_id);
                return (
                  <tr key={r.user_id} className="border-t transition-colors hover:bg-[var(--panel-2)]" style={{ borderColor: "var(--border)" }}>
                    <td className="px-4 py-3.5">
                      <input
                        type="checkbox"
                        aria-label={`Select ${r.email}`}
                        checked={isSelected}
                        onChange={() => toggleOne(r.user_id)}
                      />
                    </td>
                    <td className="px-4 py-3.5">
                      <div className="font-medium" style={{ color: "var(--text)" }}>{r.display_name ?? r.email}</div>
                      <div className="text-xs" style={{ color: "var(--muted)" }}>{r.email}</div>
                    </td>
                    <td className="px-4 py-3.5">
                      <span
                        className="chip uppercase font-semibold"
                        style={{
                          background: r.copy_enabled ? "var(--good-soft)" : "var(--panel-2)",
                          color: r.copy_enabled ? "var(--good)" : "var(--muted)",
                          borderColor: "transparent",
                        }}
                      >
                        {r.copy_enabled ? "On" : "Off"}
                      </span>
                    </td>
                    <td className="px-4 py-3.5">
                      <span
                        className="chip"
                        style={{
                          background: r.broker_count > 0 ? "var(--good-soft)" : "var(--panel-2)",
                          color: r.broker_count > 0 ? "var(--good)" : "var(--muted)",
                          borderColor: "transparent",
                        }}
                      >
                        {r.broker_count > 0 ? "Connected" : "Not connected"}
                      </span>
                    </td>
                    <td className="px-4 py-3.5 num font-medium" style={{ color: Number(r.realized_pnl_30d) >= 0 ? "var(--good)" : "var(--bad)" }}>
                      {fmtSignedUsd(r.realized_pnl_30d)}
                    </td>
                    <td className="px-4 py-3.5 text-right">
                      <button
                        onClick={() => requestRemove([r.user_id])}
                        aria-label={`Remove ${r.email}`}
                        title="Remove subscriber"
                        className="btn-danger-soft px-3 py-1 text-xs inline-flex items-center gap-1.5"
                      >
                        <Trash2 size={13} /> Remove
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <ConfirmModal
        open={confirming !== null}
        title={`Remove ${confirming?.label ?? "subscriber"}?`}
        message={
          <>
            They will stop receiving new copy trades from you. Their account,
            broker connections, and order history are preserved — they can
            re-follow you any time.
          </>
        }
        confirmLabel="Remove"
        cancelLabel="Cancel"
        variant="danger"
        busy={removing}
        onConfirm={confirmRemove}
        onCancel={() => { if (!removing) setConfirming(null); }}
      />
    </div>
  );
}

function SummaryTile({
  label,
  node,
  tone = "neutral",
}: {
  label: string;
  node: React.ReactNode;
  tone?: "neutral" | "good";
}) {
  const color = tone === "good" ? "var(--good)" : "var(--text)";
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
      className="card p-4"
    >
      <div className="text-[11px] font-medium uppercase tracking-wider" style={{ color: "var(--muted)" }}>{label}</div>
      <div className="mt-1.5" style={{ color }}>{node}</div>
    </motion.div>
  );
}
