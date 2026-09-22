"use client";

/**
 * Admin System → Usage — live telemetry for the Lightsail box this deployment
 * runs on (prod admin shows prod, QA admin shows QA). CPU / memory / disk / swap
 * / load / network, refreshed every few seconds. Read on-box via psutil; no AWS
 * credentials involved.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

interface Usage {
  hostname: string;
  sampled_at: string;
  boot_time: string;
  uptime_seconds: number;
  process_count: number;
  cpu: { percent: number; cores: number; per_core: number[]; load_avg: number[] | null };
  memory: { total: number; used: number; available: number; percent: number };
  swap: { total: number; used: number; percent: number };
  disk: { mount: string; total: number; used: number; free: number; percent: number };
  network: { bytes_sent: number; bytes_recv: number; packets_sent: number; packets_recv: number };
}

const REFRESH_MS = 3000;

function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), u.length - 1);
  return `${(n / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}

function fmtUptime(s: number): string {
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return [d ? `${d}d` : "", h ? `${h}h` : "", `${m}m`].filter(Boolean).join(" ");
}

/** Green under 70%, amber 70–90%, red above — same thresholds across meters. */
function barColor(pct: number): string {
  if (pct >= 90) return "var(--bad, #b3261e)";
  if (pct >= 70) return "#b45309";
  return "var(--good, #16794a)";
}

function Meter({ label, pct, sub }: { label: string; pct: number; sub?: string }) {
  const p = Math.max(0, Math.min(100, pct));
  return (
    <div
      className="rounded-xl p-4"
      style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}
    >
      <div className="flex items-baseline justify-between">
        <span className="text-sm font-semibold" style={{ color: "var(--text-2)" }}>{label}</span>
        <span className="text-2xl font-bold tabular-nums" style={{ color: barColor(p) }}>
          {p.toFixed(0)}%
        </span>
      </div>
      <div className="mt-2 h-2.5 rounded-full overflow-hidden" style={{ background: "var(--border)" }}>
        <div
          className="h-full rounded-full"
          style={{ width: `${p}%`, background: barColor(p), transition: "width 500ms ease" }}
        />
      </div>
      {sub && <div className="mt-1.5 text-xs tabular-nums" style={{ color: "var(--muted)" }}>{sub}</div>}
    </div>
  );
}

export default function SystemUsagePage() {
  const [u, setU] = useState<Usage | null>(null);
  const [err, setErr] = useState(false);
  const [live, setLive] = useState(true);
  const firstLoad = useRef(true);

  const load = useCallback(async () => {
    try {
      const r = await api<Usage>("/api/admin/system/usage");
      setU(r);
      setErr(false);
    } catch (e) {
      setErr(true);
      if (firstLoad.current) notify.fromError(e, "Could not load system usage");
    } finally {
      firstLoad.current = false;
    }
  }, []);

  useEffect(() => {
    load();
    if (!live) return;
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [load, live]);

  const card = "rounded-xl p-4";
  const cardStyle = { background: "var(--panel-2)", border: "1px solid var(--border)" };

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold">System · Usage</h2>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            Live CPU, memory and disk for this server{u ? ` — ${u.hostname}` : ""}.
            {u && <> Updated {new Date(u.sampled_at).toLocaleTimeString()}.</>}
          </p>
        </div>
        <button
          onClick={() => setLive((v) => !v)}
          className="text-sm px-3 py-1.5 rounded-lg"
          style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}
        >
          {live ? "⏸ Pause" : "▶ Resume"} live
        </button>
      </div>

      {err && !u && (
        <div className="rounded-xl p-4 text-sm" style={{ background: "var(--panel-2)", border: "1px solid var(--bad, #b3261e)", color: "var(--bad, #b3261e)" }}>
          Couldn&apos;t reach the server telemetry endpoint.
        </div>
      )}

      {u && (
        <>
          {/* Headline meters */}
          <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
            <Meter
              label="CPU"
              pct={u.cpu.percent}
              sub={`${u.cpu.cores} vCPU${u.cpu.load_avg ? ` · load ${u.cpu.load_avg.join(" / ")}` : ""}`}
            />
            <Meter
              label="Memory"
              pct={u.memory.percent}
              sub={`${fmtBytes(u.memory.used)} / ${fmtBytes(u.memory.total)} · ${fmtBytes(u.memory.available)} free`}
            />
            <Meter
              label="Disk"
              pct={u.disk.percent}
              sub={`${fmtBytes(u.disk.used)} / ${fmtBytes(u.disk.total)} · ${fmtBytes(u.disk.free)} free`}
            />
            <Meter
              label="Swap"
              pct={u.swap.percent}
              sub={u.swap.total > 0 ? `${fmtBytes(u.swap.used)} / ${fmtBytes(u.swap.total)}` : "not configured"}
            />
          </div>

          {/* Per-core CPU */}
          <div className={card} style={cardStyle}>
            <div className="text-sm font-semibold mb-3" style={{ color: "var(--text-2)" }}>Per-core CPU</div>
            <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))" }}>
              {u.cpu.per_core.map((c, i) => (
                <div key={i}>
                  <div className="flex justify-between text-xs mb-1" style={{ color: "var(--muted)" }}>
                    <span>core {i}</span><span className="tabular-nums">{c.toFixed(0)}%</span>
                  </div>
                  <div className="h-2 rounded-full overflow-hidden" style={{ background: "var(--border)" }}>
                    <div className="h-full rounded-full" style={{ width: `${c}%`, background: barColor(c), transition: "width 500ms ease" }} />
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Facts */}
          <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
            <div className={card} style={cardStyle}>
              <div className="text-xs" style={{ color: "var(--muted)" }}>Uptime</div>
              <div className="text-lg font-semibold mt-1">{fmtUptime(u.uptime_seconds)}</div>
              <div className="text-xs mt-1" style={{ color: "var(--muted)" }}>
                since {new Date(u.boot_time).toLocaleString()}
              </div>
            </div>
            <div className={card} style={cardStyle}>
              <div className="text-xs" style={{ color: "var(--muted)" }}>Processes</div>
              <div className="text-lg font-semibold mt-1 tabular-nums">{u.process_count.toLocaleString()}</div>
            </div>
            <div className={card} style={cardStyle}>
              <div className="text-xs" style={{ color: "var(--muted)" }}>Network out (since boot)</div>
              <div className="text-lg font-semibold mt-1 tabular-nums">{fmtBytes(u.network.bytes_sent)}</div>
            </div>
            <div className={card} style={cardStyle}>
              <div className="text-xs" style={{ color: "var(--muted)" }}>Network in (since boot)</div>
              <div className="text-lg font-semibold mt-1 tabular-nums">{fmtBytes(u.network.bytes_recv)}</div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
