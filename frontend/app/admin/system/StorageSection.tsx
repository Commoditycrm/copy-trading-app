"use client";

/**
 * Admin System → "Storage". How the EC2 host's disk is used, measured on the
 * host every 15 minutes (scripts/host_storage.py). The categories don't
 * overlap and add up to "used".
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";

interface Category { key: string; label: string; bytes: number; pct_of_used: number | null; pct_of_disk: number | null }

interface Storage {
  status: "ok" | "unavailable";
  reason?: string;
  action_needed?: string;
  collected_at?: string;
  filesystem?: { total: number; used: number; available: number };
  categories?: Category[];
  stale?: boolean;
  age_seconds?: number | null;
}

const card = "rounded-xl p-4";
const cardStyle = { background: "var(--panel-2)", border: "1px solid var(--border)" };
const muted = { color: "var(--muted)" };
// one hue per category, fixed order so the bar and the table match
const COLORS = ["#0a73a8", "#7c3aed", "#db2777", "#d97706", "#059669", "#0891b2", "#65a30d", "#dc2626", "#64748b", "#94a3b8"];

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let v = n, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : v >= 10 ? 1 : 2)} ${u[i]}`;
}

export default function StorageSection() {
  const [s, setS] = useState<Storage | null>(null);
  const [err, setErr] = useState(false);

  const load = useCallback(async () => {
    try { setS(await api<Storage>("/api/admin/system/storage")); setErr(false); } catch { setErr(true); }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 5 * 60 * 1000);
    return () => clearInterval(t);
  }, [load]);

  if (err && !s) return <div className={card} style={cardStyle}><span className="text-sm" style={{ color: "var(--bad)" }}>Couldn&apos;t load storage.</span></div>;
  if (!s) return null;

  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-lg font-bold">Storage</h3>
        <p className="text-xs mt-0.5" style={muted}>EC2 host disk, measured on the host. Categories don&apos;t overlap and add up to the space used.</p>
      </div>

      {s.status === "unavailable" ? (
        <div className={card} style={{ ...cardStyle, border: "1px solid #b45309" }}>
          <div className="text-sm font-semibold" style={{ color: "#b45309" }}>Storage breakdown unavailable</div>
          <div className="text-xs mt-1" style={muted}>{s.reason} {s.action_needed}</div>
        </div>
      ) : (
        <>
          {s.stale && (
            <div className="rounded-xl p-3 text-xs" style={{ ...cardStyle, border: "1px solid #b45309", color: "#b45309" }}>
              Out of date — last measured {s.age_seconds != null ? `${Math.round(s.age_seconds / 60)} min ago` : "a while ago"}.
            </div>
          )}

          <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}>
            {[["Used", s.filesystem!.used], ["Available", s.filesystem!.available], ["Disk size", s.filesystem!.total]].map(([k, v]) => (
              <div key={k as string} className={card} style={cardStyle}>
                <div className="text-xs" style={muted}>{k}</div>
                <div className="text-lg font-semibold mt-1 tabular-nums">{fmtBytes(v as number)}</div>
                {k === "Used" && <div className="text-xs mt-1" style={muted}>{((100 * s.filesystem!.used) / s.filesystem!.total).toFixed(1)}% of the disk</div>}
              </div>
            ))}
          </div>

          <div className={card} style={cardStyle}>
            <div className="flex h-3 rounded overflow-hidden mb-3" role="img"
                 aria-label="Disk used by category" style={{ background: "var(--panel)" }}>
              {s.categories!.map((c, i) => (
                <div key={c.key} title={`${c.label}: ${fmtBytes(c.bytes)}`}
                     style={{ width: `${c.pct_of_disk ?? 0}%`, background: COLORS[i % COLORS.length] }} />
              ))}
            </div>
            <table className="w-full text-xs tabular-nums">
              <thead>
                <tr style={muted}>
                  <th className="text-left py-1 pr-2">Category</th>
                  <th className="text-right pr-2">Size</th>
                  <th className="text-right pr-2">% of used</th>
                  <th className="text-right">% of disk</th>
                </tr>
              </thead>
              <tbody>
                {s.categories!.map((c, i) => (
                  <tr key={c.key} style={{ borderTop: "1px solid var(--border)" }}>
                    <td className="py-1 pr-2">
                      <span className="inline-block w-2 h-2 rounded-sm mr-2" style={{ background: COLORS[i % COLORS.length] }} />
                      {c.label}
                    </td>
                    <td className="text-right pr-2">{fmtBytes(c.bytes)}</td>
                    <td className="text-right pr-2">{c.pct_of_used != null ? `${c.pct_of_used.toFixed(1)}%` : "—"}</td>
                    <td className="text-right">{c.pct_of_disk != null ? `${c.pct_of_disk.toFixed(1)}%` : "—"}</td>
                  </tr>
                ))}
                <tr style={{ borderTop: "1px solid var(--border)", fontWeight: 600 }}>
                  <td className="py-1 pr-2">Total used</td>
                  <td className="text-right pr-2">{fmtBytes(s.filesystem!.used)}</td>
                  <td className="text-right pr-2">100%</td>
                  <td className="text-right">{((100 * s.filesystem!.used) / s.filesystem!.total).toFixed(1)}%</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div className="text-[11px]" style={muted}>Measured {s.collected_at ? new Date(s.collected_at).toLocaleString() : "—"}; refreshed every 15 minutes.</div>
        </>
      )}
    </div>
  );
}
