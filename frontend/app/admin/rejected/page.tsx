"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import Pagination from "@/components/Pagination";
import { notify } from "@/lib/toast";

interface Rejection {
  order_id: string;
  user_email: string | null;
  user_name: string | null;
  user_role: string | null;
  is_mirror: boolean;
  symbol: string;
  side: string;
  instrument_type: string;
  quantity: string;
  status: string;         // "rejected" | "retry_pending"
  reject_reason: string | null;
  broker: string | null;
  created_at: string | null;
  // Payload fields — used to reconstruct the order as sent to the broker.
  order_type: string;
  limit_price: string | null;
  stop_price: string | null;
  option_expiry: string | null;
  option_strike: string | null;
  option_right: string | null;
  is_closing: boolean;
  broker_order_id: string | null;
  broker_call_ms: number | null;
}

// Rebuild the order the way it was sent to the broker. The raw broker request
// body isn't persisted, so this is reconstructed from the stored columns.
// time_in_force is omitted on purpose — it's an adapter default ("day"), not
// stored per order.
function buildPayload(r: Rejection): Record<string, unknown> {
  const p: Record<string, unknown> = {
    symbol: r.symbol,
    side: r.side,
    qty: r.quantity,
    type: r.order_type,
  };
  if (r.limit_price) p.limit_price = r.limit_price;
  if (r.stop_price && Number(r.stop_price) !== 0) p.stop_price = r.stop_price;
  if (r.instrument_type === "option") {
    p.instrument = "option";
    if (r.option_expiry) p.expiry = r.option_expiry;
    if (r.option_strike) p.strike = r.option_strike;
    if (r.option_right) p.right = r.option_right;
  }
  p.is_closing = r.is_closing;
  return p;
}

// An order that never hit the broker: no broker id and no round-trip time.
// These are internal rejections (e.g. credential decrypt) — there is no
// broker payload/response to show.
const neverSent = (r: Rejection) =>
  !r.broker_order_id && (r.broker_call_ms === null || r.broker_call_ms === undefined);

const ROLE_COLORS: Record<string, { bg: string; color: string }> = {
  trader:     { bg: "rgba(10,115,168,0.15)", color: "var(--accent)" },
  subscriber: { bg: "rgba(34,197,94,0.12)",  color: "#22c55e" },
  admin:      { bg: "rgba(239,68,68,0.12)",   color: "#ef4444" },
};

const STATUS_COLORS: Record<string, { bg: string; color: string }> = {
  rejected:      { bg: "rgba(239,68,68,0.12)", color: "#ef4444" },
  retry_pending: { bg: "rgba(255,200,87,0.14)", color: "#f59e0b" },
};

function Badge({ text, map }: { text: string; map: Record<string, { bg: string; color: string }> }) {
  const c = map[text] ?? { bg: "var(--panel-2)", color: "var(--text-2)" };
  return (
    <span
      className="text-xs font-semibold px-2 py-0.5 rounded-full uppercase tracking-wider whitespace-nowrap"
      style={{ background: c.bg, color: c.color }}
    >
      {text.replace(/_/g, " ")}
    </span>
  );
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

type RejSortKey = "user" | "role" | "symbol" | "broker" | "status" | "created_at";

// Clickable header cell — neutral ↕ when inactive, direction arrow when active.
function SortableTh({
  label, colKey, sortKey, sortDir, onSort,
}: {
  label: string;
  colKey: RejSortKey;
  sortKey: RejSortKey;
  sortDir: "asc" | "desc";
  onSort: (k: RejSortKey) => void;
}) {
  const active = sortKey === colKey;
  return (
    <th
      onClick={() => onSort(colKey)}
      className="text-left px-4 py-3 font-semibold cursor-pointer select-none"
      style={{ color: active ? "var(--text)" : "var(--text-2)" }}
      title={`Sort by ${label}`}
    >
      {label}
      <span style={{ marginLeft: 5, fontSize: 10, opacity: active ? 1 : 0.35 }}>
        {active ? (sortDir === "asc" ? "▲" : "▼") : "↕"}
      </span>
    </th>
  );
}

export default function AdminRejectedPage() {
  const [rows, setRows] = useState<Rejection[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [role, setRole] = useState<"all" | "trader" | "subscriber">("all");
  const [statusFilter, setStatusFilter] = useState<"all" | "rejected" | "retry_pending">("all");
  const [broker, setBroker] = useState<string>("all");
  const [delivery, setDelivery] = useState<"all" | "sent" | "never">("all");
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<RejSortKey>("created_at");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  // Client-side pagination over the filtered set (this table is bounded/capped,
  // and its many filters + broker/role chips need the full set in memory).
  const [offset, setOffset] = useState(0);
  const PAGE_SIZE = 25;

  function toggleRow(id: string) {
    setExpanded(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  function toggleSort(k: RejSortKey) {
    if (sortKey === k) setSortDir(d => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(k); setSortDir("asc"); }
  }

  async function load() {
    setLoading(true);
    try {
      const res = await api<{ rejections: Rejection[]; truncated: boolean }>(
        "/api/admin/rejected-orders?limit=300",
      );
      setRows(res.rejections);
      setTruncated(res.truncated);
    } catch (e) {
      notify.fromError(e, "Could not load rejected orders");
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { load(); }, []);

  // Brokers present in the current result set — drives the broker filter chips.
  const brokers = useMemo(
    () => Array.from(new Set(rows.map(r => r.broker).filter(Boolean) as string[])).sort(),
    [rows],
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const out = rows.filter(r => {
      if (role !== "all" && r.user_role !== role) return false;
      if (statusFilter !== "all" && r.status !== statusFilter) return false;
      if (broker !== "all" && r.broker !== broker) return false;
      if (delivery === "sent" && neverSent(r)) return false;
      if (delivery === "never" && !neverSent(r)) return false;
      if (!q) return true;
      return (
        (r.user_email ?? "").toLowerCase().includes(q) ||
        (r.user_name ?? "").toLowerCase().includes(q) ||
        r.symbol.toLowerCase().includes(q) ||
        (r.reject_reason ?? "").toLowerCase().includes(q)
      );
    });

    const dir = sortDir === "asc" ? 1 : -1;
    const userLabel = (r: Rejection) => (r.user_name ?? r.user_email ?? "").toLowerCase();
    out.sort((a, b) => {
      switch (sortKey) {
        case "user":       return userLabel(a).localeCompare(userLabel(b)) * dir;
        case "role":       return (a.user_role ?? "").localeCompare(b.user_role ?? "") * dir;
        case "symbol":     return a.symbol.localeCompare(b.symbol) * dir;
        case "broker":     return (a.broker ?? "").localeCompare(b.broker ?? "") * dir;
        case "status":     return a.status.localeCompare(b.status) * dir;
        case "created_at": return ((a.created_at ? Date.parse(a.created_at) : 0) - (b.created_at ? Date.parse(b.created_at) : 0)) * dir;
        default:           return 0;
      }
    });
    return out;
  }, [rows, role, statusFilter, broker, delivery, search, sortKey, sortDir]);

  const countByRole = (r: string) => rows.filter(x => x.user_role === r).length;

  // Reset to page 1 whenever a filter/sort changes; slice the filtered set.
  useEffect(() => { setOffset(0); }, [role, statusFilter, broker, delivery, search, sortKey, sortDir]);
  const pageRows = filtered.slice(offset, offset + PAGE_SIZE);

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold">Rejected trades</h2>
          <p className="text-sm mt-0.5" style={{ color: "var(--muted)" }}>
            Every order that failed — status rejected or awaiting retry — with the broker&apos;s reason.
            {truncated && " Showing the 300 most recent."}
          </p>
        </div>
        <button
          onClick={load}
          className="text-sm px-3 py-1.5 rounded-lg"
          style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}
        >
          Refresh
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3">
        <input
          type="text"
          placeholder="Search user, symbol, or reason…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="text-sm px-3 py-1.5 rounded-lg"
          style={{ background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)", color: "var(--text)", outline: "none", minWidth: 240 }}
        />
        <div className="flex gap-1">
          {(["all", "trader", "subscriber"] as const).map(r => (
            <button
              key={r}
              onClick={() => setRole(r)}
              className="text-xs px-3 py-1 rounded-full capitalize font-medium transition-colors"
              style={{
                background: role === r ? "var(--accent)" : "var(--panel-2)",
                color:      role === r ? "var(--accent-ink)" : "var(--text-2)",
                border:     "1px solid " + (role === r ? "var(--accent)" : "var(--border)"),
              }}
            >
              {r === "all" ? `All (${rows.length})` : `${r}s (${countByRole(r)})`}
            </button>
          ))}
        </div>
        <div className="flex gap-1">
          {(["all", "rejected", "retry_pending"] as const).map(s => (
            <button
              key={s}
              onClick={() => setStatusFilter(s)}
              className="text-xs px-3 py-1 rounded-full font-medium transition-colors"
              style={{
                background: statusFilter === s ? "var(--accent)" : "var(--panel-2)",
                color:      statusFilter === s ? "var(--accent-ink)" : "var(--text-2)",
                border:     "1px solid " + (statusFilter === s ? "var(--accent)" : "var(--border)"),
              }}
            >
              {s === "all" ? "Any status" : s.replace(/_/g, " ")}
            </button>
          ))}
        </div>

        {/* Broker filter — only shown when there's more than one broker to pick from */}
        {brokers.length > 1 && (
          <div className="flex gap-1">
            {["all", ...brokers].map(b => (
              <button
                key={b}
                onClick={() => setBroker(b)}
                className="text-xs px-3 py-1 rounded-full capitalize font-medium transition-colors"
                style={{
                  background: broker === b ? "var(--accent)" : "var(--panel-2)",
                  color:      broker === b ? "var(--accent-ink)" : "var(--text-2)",
                  border:     "1px solid " + (broker === b ? "var(--accent)" : "var(--border)"),
                }}
              >
                {b === "all" ? "Any broker" : b}
              </button>
            ))}
          </div>
        )}

        {/* Delivery filter — did the order reach the broker or fail internally first? */}
        <div className="flex gap-1">
          {([["all", "Any"], ["sent", "Reached broker"], ["never", "Never sent"]] as const).map(([val, lbl]) => (
            <button
              key={val}
              onClick={() => setDelivery(val)}
              className="text-xs px-3 py-1 rounded-full font-medium transition-colors"
              style={{
                background: delivery === val ? "var(--accent)" : "var(--panel-2)",
                color:      delivery === val ? "var(--accent-ink)" : "var(--text-2)",
                border:     "1px solid " + (delivery === val ? "var(--accent)" : "var(--border)"),
              }}
            >
              {lbl}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      {loading ? (
        <div style={{ color: "var(--muted)" }}>Loading rejected orders…</div>
      ) : (
        <div className="rounded-xl overflow-auto" style={{ border: "1px solid var(--border)", maxHeight: "70vh" }}>
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
              <tr style={{ background: "rgba(255,255,255,0.03)", borderBottom: "1px solid var(--border)" }}>
                <SortableTh label="User"   colKey="user"       sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <SortableTh label="Role"   colKey="role"       sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <SortableTh label="Trade"  colKey="symbol"     sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <SortableTh label="Broker" colKey="broker"     sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <SortableTh label="Status" colKey="status"     sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
                <th className="text-left px-4 py-3 font-semibold" style={{ color: "var(--text-2)" }}>Reason</th>
                <SortableTh label="When"   colKey="created_at" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center" style={{ color: "var(--muted)" }}>
                    No rejected trades match this filter.
                  </td>
                </tr>
              ) : (
                pageRows.map((r, i) => {
                  const open = expanded.has(r.order_id);
                  const border = i < pageRows.length - 1 ? "1px solid var(--border)" : "none";
                  return (
                  <Fragment key={r.order_id}>
                  <tr
                    onClick={() => toggleRow(r.order_id)}
                    className="cursor-pointer transition-colors"
                    style={{ borderBottom: open ? "none" : border }}
                    title="Click to see the payload and broker response"
                  >
                    <td className="px-4 py-3">
                      <div className="font-medium flex items-center gap-1.5" style={{ color: "var(--text)" }}>
                        <span style={{ color: "var(--muted)", fontSize: 11 }}>{open ? "▾" : "▸"}</span>
                        {r.user_name ?? r.user_email ?? "—"}
                      </div>
                      {r.user_name && <div className="text-xs pl-4" style={{ color: "var(--muted)" }}>{r.user_email}</div>}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-1.5">
                        <Badge text={r.user_role ?? "—"} map={ROLE_COLORS} />
                        {r.is_mirror && <span className="text-[10px]" style={{ color: "var(--muted)" }}>mirror</span>}
                      </div>
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap">
                      <span className="uppercase font-semibold" style={{ color: r.side === "buy" ? "#22c55e" : "#ef4444" }}>{r.side}</span>
                      {" "}<span style={{ color: "var(--text)" }}>{r.symbol}</span>
                      <span className="text-xs" style={{ color: "var(--muted)" }}> ×{r.quantity}</span>
                    </td>
                    <td className="px-4 py-3" style={{ color: "var(--text-2)" }}>{r.broker ?? "—"}</td>
                    <td className="px-4 py-3"><Badge text={r.status} map={STATUS_COLORS} /></td>
                    <td className="px-4 py-3" style={{ color: r.reject_reason ? "var(--bad)" : "var(--muted)", maxWidth: 360 }}>
                      <div className="truncate" style={{ maxWidth: 340 }}>{r.reject_reason ?? "—"}</div>
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-xs" style={{ color: "var(--muted)" }}>{fmtTime(r.created_at)}</td>
                  </tr>

                  {open && (
                    <tr style={{ borderBottom: border, background: "rgba(255,255,255,0.02)" }}>
                      <td colSpan={7} className="px-4 pb-4 pt-1">
                        <div className="grid gap-4" style={{ gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)" }}>
                          {/* Reconstructed payload */}
                          <div>
                            <div className="text-xs font-semibold mb-1.5 flex items-center gap-2" style={{ color: "var(--text-2)" }}>
                              Payload sent to broker
                              {neverSent(r) && (
                                <span className="text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider"
                                  style={{ background: "rgba(245,158,11,0.14)", color: "#f59e0b" }}>
                                  never sent
                                </span>
                              )}
                            </div>
                            <pre className="text-xs rounded-lg p-3 overflow-x-auto"
                              style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text)", fontFamily: "monospace", margin: 0 }}>
{JSON.stringify(buildPayload(r), null, 2)}
                            </pre>
                            <div className="text-[11px] mt-1" style={{ color: "var(--muted)" }}>
                              {neverSent(r)
                                ? "This order was rejected internally before the broker call — the payload above is what we would have sent."
                                : "Reconstructed from stored order fields. time_in_force isn’t persisted (adapter default: day)."}
                            </div>
                          </div>

                          {/* Broker response / reason */}
                          <div>
                            <div className="text-xs font-semibold mb-1.5" style={{ color: "var(--text-2)" }}>Broker response</div>
                            <div className="text-xs space-y-1" style={{ color: "var(--text-2)" }}>
                              <div className="flex justify-between gap-3">
                                <span style={{ color: "var(--muted)" }}>Broker order ID</span>
                                <span style={{ fontFamily: "monospace" }}>{r.broker_order_id ?? "— (none issued)"}</span>
                              </div>
                              <div className="flex justify-between gap-3">
                                <span style={{ color: "var(--muted)" }}>Round-trip</span>
                                <span style={{ fontFamily: "monospace" }}>{r.broker_call_ms != null ? `${r.broker_call_ms.toLocaleString()}ms` : "— (never called)"}</span>
                              </div>
                            </div>
                            <div className="text-xs font-semibold mt-3 mb-1" style={{ color: "var(--text-2)" }}>Reject reason (full)</div>
                            <pre className="text-xs rounded-lg p-3 overflow-x-auto whitespace-pre-wrap"
                              style={{ background: "var(--panel-2)", border: "1px solid rgba(239,68,68,0.25)", color: "var(--bad)", fontFamily: "monospace", margin: 0 }}>
{r.reject_reason ?? "—"}
                            </pre>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                  </Fragment>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      )}
      {filtered.length > PAGE_SIZE && (
        <Pagination total={filtered.length} limit={PAGE_SIZE} offset={offset} onChange={setOffset} />
      )}
    </div>
  );
}
