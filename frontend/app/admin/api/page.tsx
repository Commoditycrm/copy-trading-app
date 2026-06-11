"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

/** Shape returned by /api/admin/config/alpaca-pnl-poll-interval. Same
 *  fields as the fanout-batch-threshold endpoint plus min/max so the
 *  UI input can bound itself client-side. */
interface AlpacaPollIntervalState {
  default: number;
  override: number | null;
  effective: number;
  min: number;
  max: number;
}

export default function AdminApiPage() {
  const [state, setState] = useState<AlpacaPollIntervalState | null>(null);
  const [loading, setLoading] = useState(true);
  const [input, setInput] = useState("");
  const [saving, setSaving] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const s = await api<AlpacaPollIntervalState>(
        "/api/admin/config/alpaca-pnl-poll-interval",
      );
      setState(s);
      // Seed the input with whatever the effective value is, so
      // changing OFF a default to an override (or vice versa) doesn't
      // start from a blank.
      setInput(String(s.effective));
    } catch (e) {
      notify.fromError(e, "Could not load API config");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function save(value: number | null) {
    setSaving(true);
    try {
      const s = await api<AlpacaPollIntervalState>(
        "/api/admin/config/alpaca-pnl-poll-interval",
        { method: "PATCH", body: JSON.stringify({ interval_s: value }) },
      );
      setState(s);
      setInput(String(s.effective));
      notify.success(
        value === null
          ? `Cleared override — using default ${s.default}s`
          : `Alpaca poll interval set to ${value}s`,
      );
    } catch (e) {
      notify.fromError(e, "Could not update poll interval");
    } finally {
      setSaving(false);
    }
  }

  if (loading || !state) {
    return <div style={{ color: "var(--muted)" }}>Loading…</div>;
  }

  // Whether the user has edited the input away from the current
  // override (or away from the default if no override). Disables Save
  // when nothing's changed.
  const inputNum = Number(input);
  const inputValid =
    Number.isFinite(inputNum) &&
    Number.isInteger(inputNum) &&
    inputNum >= state.min &&
    inputNum <= state.max;
  const changed = inputValid && inputNum !== state.effective;
  const usingOverride = state.override !== null;

  return (
    <div className="space-y-5 max-w-3xl">
      <div>
        <h2 className="text-xl font-bold">API Configuration</h2>
        <p className="text-sm mt-0.5" style={{ color: "var(--muted)" }}>
          Runtime-tunable knobs for the platform's broker integrations.
          Changes apply on the very next poll tick — no backend restart needed.
        </p>
      </div>

      {/* ── Alpaca P&L poll interval card ──────────────────────────────── */}
      <section
        className="rounded-xl p-5"
        style={{
          border: "1px solid var(--border)",
          background:
            "linear-gradient(135deg, rgba(255,255,255,0.025) 0%, rgba(255,255,255,0.005) 60%, rgba(0,0,0,0.15) 100%)",
        }}
      >
        <div className="flex items-start justify-between gap-4 mb-4">
          <div className="min-w-0">
            <h3 className="text-base font-semibold">Alpaca P&amp;L polling interval</h3>
            <p className="text-[12px] mt-1 leading-snug" style={{ color: "var(--muted)" }}>
              How often the background poller asks Alpaca for each connected
              account's equity, today's P&amp;L, and positions. Lower = faster
              kill-switch and position TP/SL reaction; higher = fewer API calls
              against Alpaca's 200/min/account budget. SnapTrade has its own
              cadence (60s) that's not tunable from here.
            </p>
          </div>
          {/* Status badge — Default vs Override */}
          <span
            className="text-[10px] uppercase tracking-widest px-2 py-1 rounded-full font-semibold shrink-0"
            style={{
              background: usingOverride
                ? "rgba(245,158,11,0.12)"
                : "rgba(34,197,94,0.12)",
              color: usingOverride ? "#f59e0b" : "#22c55e",
              border: usingOverride
                ? "1px solid rgba(245,158,11,0.25)"
                : "1px solid rgba(34,197,94,0.25)",
            }}
          >
            {usingOverride ? "Override" : "Default"}
          </span>
        </div>

        {/* Stat row */}
        <div className="grid grid-cols-3 gap-3 mb-5">
          <Stat
            label="Effective"
            value={`${state.effective}s`}
            color={usingOverride ? "#f59e0b" : "var(--text)"}
          />
          <Stat label="Default" value={`${state.default}s`} />
          <Stat
            label="Override"
            value={state.override !== null ? `${state.override}s` : "—"}
            color={state.override !== null ? "var(--text)" : "var(--muted)"}
          />
        </div>

        {/* Editor */}
        <div className="flex items-center gap-3">
          <div
            className="flex-1 inline-flex items-center rounded-lg border overflow-hidden transition-colors focus-within:border-[var(--accent)]"
            style={{
              borderColor: "var(--border)",
              background: "rgba(0,0,0,0.25)",
            }}
          >
            <span
              className="px-3 py-2 text-[10px] font-semibold border-r tabular-nums self-stretch inline-flex items-center"
              style={{ color: "var(--muted)", borderColor: "var(--border)" }}
            >
              SECONDS
            </span>
            <input
              type="number"
              step={1}
              min={state.min}
              max={state.max}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              className="flex-1 w-full px-3 py-2 text-sm tabular-nums"
              style={{
                border: "none",
                background: "transparent",
                outline: "none",
                color: "var(--text)",
              }}
            />
          </div>
          <button
            disabled={saving || !changed}
            onClick={() => save(inputNum)}
            className="px-4 py-2 text-xs rounded-lg font-semibold transition-all disabled:opacity-30 disabled:cursor-not-allowed hover:enabled:scale-[1.03] active:enabled:scale-[0.97]"
            style={{ background: "var(--accent)", color: "#06121f" }}
          >
            Save
          </button>
          <button
            disabled={saving || !usingOverride}
            onClick={() => save(null)}
            className="px-4 py-2 text-xs rounded-lg font-semibold transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
            style={{
              background: "rgba(255,255,255,0.05)",
              color: "var(--text-2)",
              border: "1px solid var(--border)",
            }}
          >
            Reset to default
          </button>
        </div>

        {/* Helper line */}
        <div className="mt-3 text-[11px]" style={{ color: "var(--muted)" }}>
          Allowed range: {state.min}–{state.max} seconds.
          {!inputValid && input !== "" && (
            <span style={{ color: "var(--bad)" }}>
              {" "}— enter a whole number in the allowed range.
            </span>
          )}
        </div>

        {/* Rate-limit hint */}
        <div
          className="mt-4 p-3 rounded-lg text-[11px] leading-snug"
          style={{
            border: "1px solid var(--border)",
            background: "rgba(255,255,255,0.02)",
            color: "var(--muted)",
          }}
        >
          <strong style={{ color: "var(--text-2)" }}>Rough math:</strong>{" "}
          each connected Alpaca account costs <span className="tabular-nums">{Math.round(60 / state.effective)}</span> req/min.
          With Alpaca's 200/min/account budget, you have plenty of headroom even at 5s.
          The poller skips subscribers who have no kill switches or position TP/SL
          set, so total API load is usually well below the per-account ceiling.
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div
      className="rounded-lg px-3 py-2.5"
      style={{ background: "rgba(0,0,0,0.2)", border: "1px solid var(--border)" }}
    >
      <div className="text-[9px] uppercase tracking-widest font-medium" style={{ color: "var(--muted)" }}>
        {label}
      </div>
      <div
        className="font-semibold mt-0.5 tabular-nums text-base"
        style={{ color: color ?? "var(--text)" }}
      >
        {value}
      </div>
    </div>
  );
}
