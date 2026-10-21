"use client";

/**
 * How many requests went to Webull for your app key, and what made them.
 *
 * Reads GET /api/brokers/webull-usage — our own counters, never Webull itself —
 * every 15s while the tab is visible. Hidden for accounts without Webull.
 * Collapsed it is one line; expanded it splits the calls by source (Positions
 * page, order poll, P&L poller, auto-trim, sign-in …) and by Webull endpoint.
 */
import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Activity } from "lucide-react";
import { api } from "@/lib/api";

interface Usage {
  has_webull: boolean;
  minutes: number;
  total: number;
  rate_limited: number;
  per_minute: { minute: number; calls: number }[];
  by_caller: { caller: string; calls: number; rate_limited: number }[];
  by_endpoint: { endpoint: string; calls: number }[];
  /** Each 429: what made the call and which Webull endpoint refused it. */
  rate_limited_by?: { caller: string; endpoint: string; calls: number }[];
  /** Calls held back for their turn on an endpoint's 2-per-2s counter instead
   *  of being refused (services/webull_pacer.py). */
  paced?: { endpoint: string; calls: number; avg_wait_ms: number }[];
  /** Sent at once because the endpoint's queue was already full — more reads
   *  wanted than Webull allows; these are the calls that can still be refused. */
  queue_full?: { endpoint: string; calls: number }[];
}

const WINDOWS = [1, 5, 15, 60] as const;

export function WebullUsage() {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [open, setOpen] = useState(false);
  const [minutes, setMinutes] = useState<number>(5);

  useEffect(() => {
    let alive = true;
    const load = () => {
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      api<Usage>(`/api/brokers/webull-usage?minutes=${minutes}`)
        .then((u) => { if (alive) setUsage(u); })
        .catch(() => { /* a readout — never worth an error toast */ });
    };
    load();
    const t = setInterval(load, 15_000);
    return () => { alive = false; clearInterval(t); };
  }, [minutes]);

  if (!usage?.has_webull) return null;

  const perMin = usage.minutes > 0 ? usage.total / usage.minutes : 0;
  const limitedTone = usage.rate_limited > 0 ? "var(--bad)" : "var(--muted)";
  const limitedBy = usage.rate_limited_by ?? [];
  const paced = usage.paced ?? [];
  const pacedTotal = paced.reduce((n, p) => n + p.calls, 0);
  const queueFull = usage.queue_full ?? [];
  const queueFullTotal = queueFull.reduce((n, q) => n + q.calls, 0);

  return (
    <div
      className="rounded-xl text-[12px]"
      style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left"
        aria-expanded={open}
        title="Requests sent to Webull for your app key — what made them, and how many were rate-limited"
      >
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <Activity size={13} style={{ color: "var(--accent-2)" }} />
        <span style={{ color: "var(--text)" }}>Webull calls</span>
        <span>· last {usage.minutes} min:</span>
        <span className="num" style={{ color: "var(--text)", fontWeight: 600 }}>{usage.total}</span>
        <span>({perMin.toFixed(1)}/min)</span>
        <span style={{ color: limitedTone }}>· {usage.rate_limited} rate-limited</span>
        {pacedTotal > 0 && <span style={{ color: "var(--muted)" }}>· {pacedTotal} waited their turn</span>}
        {queueFullTotal > 0 && <span style={{ color: "var(--warn)" }}>· {queueFullTotal} sent without waiting (queue full)</span>}
      </button>

      {open && (
        <div className="px-3 pb-3 space-y-3">
          <div className="flex items-center gap-1.5">
            <span style={{ color: "var(--muted)" }}>Window</span>
            {WINDOWS.map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMinutes(m)}
                className="px-2 py-0.5 rounded-full text-[11px]"
                style={{
                  background: minutes === m ? "var(--accent-glow)" : "transparent",
                  border: `1px solid ${minutes === m ? "rgba(44,147,197,0.45)" : "var(--border)"}`,
                  color: minutes === m ? "var(--accent-2)" : "var(--muted)",
                }}
              >
                {m < 60 ? `${m} min` : "1 hr"}
              </button>
            ))}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <table className="w-full">
              <thead>
                <tr style={{ color: "var(--muted)" }}>
                  <th className="text-left font-medium py-1">What made the call</th>
                  <th className="text-right font-medium py-1">Calls</th>
                  <th className="text-right font-medium py-1">Rate-limited</th>
                </tr>
              </thead>
              <tbody>
                {usage.by_caller.length === 0 && (
                  <tr><td colSpan={3} className="py-1" style={{ color: "var(--muted)" }}>No calls in this window.</td></tr>
                )}
                {usage.by_caller.map((c) => (
                  <tr key={c.caller} style={{ borderTop: "1px solid var(--border)" }}>
                    <td className="py-1" style={{ color: "var(--text)" }}>{c.caller}</td>
                    <td className="py-1 text-right num">{c.calls}</td>
                    <td className="py-1 text-right num" style={{ color: c.rate_limited ? "var(--bad)" : "var(--muted)" }}>
                      {c.rate_limited}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <table className="w-full">
              <thead>
                <tr style={{ color: "var(--muted)" }}>
                  <th className="text-left font-medium py-1">Webull endpoint</th>
                  <th className="text-right font-medium py-1">Calls</th>
                </tr>
              </thead>
              <tbody>
                {usage.by_endpoint.map((e) => (
                  <tr key={e.endpoint} style={{ borderTop: "1px solid var(--border)" }}>
                    <td className="py-1 font-mono text-[11px]" style={{ color: "var(--text)" }}>{e.endpoint}</td>
                    <td className="py-1 text-right num">{e.calls}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(limitedBy.length > 0 || paced.length > 0 || queueFull.length > 0) && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {limitedBy.length > 0 && (
                <table className="w-full">
                  <thead>
                    <tr style={{ color: "var(--muted)" }}>
                      <th className="text-left font-medium py-1">Rate-limited: what made the call</th>
                      <th className="text-left font-medium py-1">Endpoint that refused it</th>
                      <th className="text-right font-medium py-1">429s</th>
                    </tr>
                  </thead>
                  <tbody>
                    {limitedBy.map((l) => (
                      <tr key={`${l.caller}|${l.endpoint}`} style={{ borderTop: "1px solid var(--border)" }}>
                        <td className="py-1" style={{ color: "var(--text)" }}>{l.caller}</td>
                        <td className="py-1 font-mono text-[11px]">{l.endpoint}</td>
                        <td className="py-1 text-right num" style={{ color: "var(--bad)" }}>{l.calls}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {paced.length > 0 && (
                <table className="w-full">
                  <thead>
                    <tr style={{ color: "var(--muted)" }} title="Held back a moment so two calls never reach the same Webull endpoint at once — instead of being refused">
                      <th className="text-left font-medium py-1">Waited their turn</th>
                      <th className="text-right font-medium py-1">Calls</th>
                      <th className="text-right font-medium py-1">Avg wait</th>
                    </tr>
                  </thead>
                  <tbody>
                    {paced.map((p) => (
                      <tr key={p.endpoint} style={{ borderTop: "1px solid var(--border)" }}>
                        <td className="py-1 font-mono text-[11px]" style={{ color: "var(--text)" }}>{p.endpoint}</td>
                        <td className="py-1 text-right num">{p.calls}</td>
                        <td className="py-1 text-right num">{(p.avg_wait_ms / 1000).toFixed(2)} s</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {queueFull.length > 0 && (
                <table className="w-full">
                  <thead>
                    <tr style={{ color: "var(--muted)" }} title="More reads were wanted than Webull allows, so these went out without waiting — the calls that can still be refused">
                      <th className="text-left font-medium py-1">Sent without waiting (queue full)</th>
                      <th className="text-right font-medium py-1">Calls</th>
                    </tr>
                  </thead>
                  <tbody>
                    {queueFull.map((q) => (
                      <tr key={q.endpoint} style={{ borderTop: "1px solid var(--border)" }}>
                        <td className="py-1 font-mono text-[11px]" style={{ color: "var(--text)" }}>{q.endpoint}</td>
                        <td className="py-1 text-right num" style={{ color: "var(--warn)" }}>{q.calls}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
          <p className="text-[11px]" style={{ color: "var(--muted)" }}>
            Counted across the whole app (pages and background jobs) for your Webull app key.
            Refreshes every 15 s; this readout itself never calls Webull.
          </p>
        </div>
      )}
    </div>
  );
}
