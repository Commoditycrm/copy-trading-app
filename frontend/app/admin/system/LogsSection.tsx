"use client";

/**
 * Admin System → "Logs". Which operational logs could be cleared, a confirmed
 * cleanup that the host agent carries out (scripts/host_log_cleanup.py). The
 * agent decides what is eligible; database records,
 * security logs and the journal are never touched. While cleanup is off on the
 * host, every request is a dry run.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { fmtBytes } from "./StorageSection";

interface Item { category: string; path: string; container: string | null; bytes: number; action: "truncate" | "delete" }
interface Result {
  request_id: string; status: string; dry_run: boolean; freed_bytes: number; categories: string[];
  reason: string | null; finished_at: string | null; items: { path: string; action: string; bytes: number; ok: boolean }[];
}
interface Logs {
  status: "ok" | "unavailable";
  reason?: string;
  inventory_id?: string;
  generated_at?: string;
  stale?: boolean;
  enabled?: boolean;
  categories?: Record<string, { label: string; files: number; bytes: number }>;
  items?: Item[];
  never_cleared?: string[];
  docker_rotation?: Record<string, string> | null;
  pending_request?: string | null;
  last_result?: Result | null;
}

const card = "rounded-xl p-4";
const cardStyle = { background: "var(--panel-2)", border: "1px solid var(--border)" };
const muted = { color: "var(--muted)" };
const warn = "#b45309";

export default function LogsSection() {
  const [s, setS] = useState<Logs | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [reviewing, setReviewing] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setS(await api<Logs>("/api/admin/system/logs"));
    } catch (e) {
      notify.fromError(e, "Could not load logs");
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [load]);

  const chosen = useMemo(() => (s?.items ?? []).filter((i) => picked.has(i.category)), [s, picked]);
  const expected = chosen.reduce((a, i) => a + i.bytes, 0);

  async function submit() {
    if (!s?.inventory_id) return;
    setBusy(true);
    try {
      const r = await api<{ dry_run: boolean; files: number; expected_bytes: number }>("/api/admin/system/logs/cleanup", {
        method: "POST",
        body: JSON.stringify({ categories: Array.from(picked), inventory_id: s.inventory_id, confirm: true }),
      });
      notify.success(r.dry_run ? `Dry run requested for ${r.files} files` : `Cleanup requested: ${r.files} files`);
      setReviewing(false);
      setPicked(new Set());
      setTimeout(load, 3000);
    } catch (e) {
      notify.fromError(e, "Cleanup refused");
    } finally {
      setBusy(false);
    }
  }

  if (!s) return null;

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h3 className="text-lg font-bold">Logs</h3>
          <p className="text-xs mt-0.5" style={muted}>
            Operational logs only. Orders, fills, audit records, the database, security logs and the journal are never cleared.
          </p>
        </div>
        {s.status === "ok" && (
          <button disabled={busy || !!s.pending_request || s.stale || picked.size === 0}
                  onClick={() => setReviewing(true)}
                  className="text-sm px-3 py-1.5 rounded-lg"
                  style={{ background: "var(--panel)", border: "1px solid var(--border)", opacity: picked.size === 0 ? 0.5 : 1 }}>
            Clear logs…
          </button>
        )}
      </div>

      {s.status === "unavailable" ? (
        <div className={card} style={{ ...cardStyle, border: `1px solid ${warn}` }}>
          <div className="text-sm font-semibold" style={{ color: warn }}>Log management unavailable</div>
          <div className="text-xs mt-1" style={muted}>{s.reason}</div>
        </div>
      ) : (
        <>
          {!s.enabled && (
            <div className="rounded-xl p-3 text-xs" style={{ ...cardStyle, border: `1px solid ${warn}`, color: warn }}>
              Cleanup is in dry-run mode on this server: a request reports what would be freed and deletes nothing.
            </div>
          )}
          {s.stale && <div className="text-xs" style={{ color: warn }}>The log list is out of date; it refreshes every 5 minutes.</div>}

          <div className={card} style={cardStyle}>
            <table className="w-full text-xs tabular-nums">
              <thead>
                <tr style={muted}>
                  <th className="text-left py-1 pr-2" />
                  <th className="text-left pr-2">Logs</th>
                  <th className="text-right pr-2">Files</th>
                  <th className="text-right">Size</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(s.categories ?? {}).map(([key, c]) => (
                  <tr key={key} style={{ borderTop: "1px solid var(--border)" }}>
                    <td className="py-1.5 pr-2">
                      <input type="checkbox" aria-label={`Select ${c.label}`} checked={picked.has(key)} disabled={c.files === 0}
                             onChange={(e) => setPicked((p) => { const n = new Set(p); if (e.target.checked) n.add(key); else n.delete(key); return n; })} />
                    </td>
                    <td className="pr-2">{c.label}</td>
                    <td className="text-right pr-2">{c.files}</td>
                    <td className="text-right">{fmtBytes(c.bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="text-[11px] mt-2" style={muted}>
              Never cleared: {(s.never_cleared ?? []).join("; ")}.
              {s.docker_rotation && ` Docker already rotates container logs at ${s.docker_rotation["max-size"]} × ${s.docker_rotation["max-file"]} files.`}
            </div>
          </div>

          {s.last_result && (
            <div className="text-xs" style={muted}>
              Last request {s.last_result.finished_at ? new Date(s.last_result.finished_at).toLocaleString() : ""}: {s.last_result.status}
              {s.last_result.dry_run ? " (dry run)" : ""} — {s.last_result.status === "refused" ? s.last_result.reason
                : `${s.last_result.items.length} files, ${fmtBytes(s.last_result.freed_bytes)} ${s.last_result.dry_run ? "would be" : ""} freed`}.
            </div>
          )}
        </>
      )}

      {reviewing && (
        <div role="dialog" aria-modal="true" className="fixed inset-0 z-50 flex items-center justify-center p-4"
             style={{ background: "rgba(0,0,0,0.6)" }}>
          <div className="rounded-xl p-5 w-full max-w-3xl max-h-[85vh] overflow-auto" style={{ background: "var(--panel)", border: "1px solid var(--border)" }}>
            <div className="text-base font-bold">{s.enabled ? "Clear these logs?" : "Dry run: these logs would be cleared"}</div>
            <div className="text-xs mt-1" style={muted}>
              {chosen.length} files · expected to free {fmtBytes(expected)}. Live container logs are emptied in place (the
              containers keep logging); rotated files are deleted. Recorded in the audit log.
            </div>
            <table className="w-full text-xs mt-3 tabular-nums">
              <tbody>
                {chosen.map((i) => (
                  <tr key={i.path} style={{ borderTop: "1px solid var(--border)" }}>
                    <td className="py-1 pr-2 font-mono break-all">{i.container ?? i.path}</td>
                    <td className="pr-2" style={muted}>{i.action === "truncate" ? "empty" : "delete"}</td>
                    <td className="text-right">{fmtBytes(i.bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="flex justify-end gap-2 mt-4">
              <button onClick={() => setReviewing(false)} className="text-sm px-3 py-1.5 rounded-lg" style={{ border: "1px solid var(--border)" }}>Cancel</button>
              <button disabled={busy} onClick={submit} className="text-sm px-3 py-1.5 rounded-lg font-semibold"
                      style={{ background: s.enabled ? "var(--bad, #b3261e)" : "var(--accent)", color: "white" }}>
                {s.enabled ? `Clear ${chosen.length} files` : "Run dry run"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
