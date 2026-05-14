"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";
import type { SubscriberSettings, TraderSettings, User } from "@/lib/types";

export default function SettingsPage() {
  const [user, setUser] = useState<User | null>(null);
  const [sub, setSub] = useState<SubscriberSettings | null>(null);
  const [trd, setTrd] = useState<TraderSettings | null>(null);
  const [traders, setTraders] = useState<{ id: string; display_name: string | null; email: string }[]>([]);
  const [multInput, setMultInput] = useState("");
  const [multBusy, setMultBusy] = useState(false);
  const [limitInput, setLimitInput] = useState("");
  const [limitBusy, setLimitBusy] = useState(false);

  useEffect(() => {
    (async () => {
      const u = await api<User>("/api/auth/me");
      setUser(u);
      if (u.role === "subscriber") {
        const s = await api<SubscriberSettings>("/api/settings/subscriber");
        setSub(s);
        setMultInput(parseFloat(s.multiplier).toString());
        setLimitInput(s.daily_loss_limit ?? "");
        setTraders(await api("/api/settings/traders"));
      } else {
        setTrd(await api<TraderSettings>("/api/settings/trader"));
      }
    })().catch(e => notify.fromError(e, "Could not load settings"));
  }, []);

  // Listen for the auto-pause event from the backend so the UI reacts instantly
  // when the daily-loss limit fires (no refresh needed).
  useEventStream((evt) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const e = evt as any;
    if (e?.type === "copy.auto_paused") {
      notify.error(
        `Copy trading auto-paused — today's loss ($${e.todays_realized_pnl}) hit your daily limit ($${e.daily_loss_limit}).`,
        { autoClose: false }   // sticky — important enough to require dismissal
      );
      // Pull fresh settings so the UI's copy toggle now shows OFF.
      api<SubscriberSettings>("/api/settings/subscriber").then(setSub);
    }
  });

  async function toggleCopy(next: boolean) {
    setSub(await api<SubscriberSettings>("/api/settings/subscriber/copy", {
      method: "PATCH", body: JSON.stringify({ copy_enabled: next })
    }));
  }
  async function follow(traderId: string | null) {
    setSub(await api<SubscriberSettings>("/api/settings/subscriber/follow", {
      method: "PATCH", body: JSON.stringify({ trader_id: traderId })
    }));
  }
  async function saveMultiplier() {
    setMultBusy(true);
    try {
      const n = Number(multInput);
      if (!Number.isFinite(n) || n <= 0 || n > 10) {
        throw new ApiError(422, "multiplier must be between 0.1 and 10");
      }
      const rounded = (Math.round(n * 10) / 10).toFixed(1);
      const s = await api<SubscriberSettings>("/api/settings/subscriber/multiplier", {
        method: "PATCH",
        body: JSON.stringify({ multiplier: rounded }),
      });
      setSub(s);
      setMultInput(parseFloat(s.multiplier).toString());
      notify.success(`Multiplier set to ×${parseFloat(s.multiplier).toString()}`);
    } catch (e) {
      notify.fromError(e, "Could not update multiplier");
    } finally {
      setMultBusy(false);
    }
  }
  async function saveLimit() {
    setLimitBusy(true);
    try {
      const trimmed = limitInput.trim();
      const body = { daily_loss_limit: trimmed === "" ? null : trimmed };
      const s = await api<SubscriberSettings>("/api/settings/subscriber/daily-loss-limit", {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      setSub(s);
      setLimitInput(s.daily_loss_limit ?? "");
      notify.success(s.daily_loss_limit ? `Daily loss limit set to $${s.daily_loss_limit}` : "Daily loss limit cleared");
    } catch (e) {
      notify.fromError(e, "Could not update daily loss limit");
    } finally {
      setLimitBusy(false);
    }
  }
  async function toggleTrading(next: boolean) {
    setTrd(await api<TraderSettings>("/api/settings/trader", {
      method: "PATCH", body: JSON.stringify({ trading_enabled: next })
    }));
  }

  if (!user) return <p style={{color: "var(--muted)"}}>Loading…</p>;

  // Helper to format a Decimal-like string as USD; "$" sign + 2 dp.
  const fmt = (v: string | null | undefined): string => {
    if (v === null || v === undefined || v === "") return "—";
    const n = Number(v);
    if (!Number.isFinite(n)) return v;
    return n.toLocaleString(undefined, { style: "currency", currency: "USD" });
  };

  // Drop trailing zeros from the backend's "1.300" → "1.3", "1.000" → "1".
  const fmtMultiplier = (v: string): string => {
    const n = parseFloat(v);
    return Number.isFinite(n) ? n.toString() : v;
  };

  const todaysPnL = sub ? Number(sub.todays_realized_pnl ?? "0") : 0;
  const limit = sub?.daily_loss_limit ? Number(sub.daily_loss_limit) : null;
  const headroom = limit !== null ? limit + todaysPnL : null;  // todaysPnL is negative when losing
  const limitPct = limit !== null && limit > 0 ? Math.min(100, Math.max(0, (-todaysPnL / limit) * 100)) : 0;

  return (
    <div className="space-y-6 max-w-2xl">
      <h1 className="text-2xl font-semibold">Settings</h1>

      {user.role === "subscriber" && sub && (
        <>
          <section className="p-4 rounded border space-y-3" style={{borderColor: "var(--border)", background: "var(--panel)"}}>
            <h2 className="font-medium">Following trader</h2>
            <select
              value={sub.following_trader_id ?? ""}
              onChange={e => follow(e.target.value || null)}
              className="w-full p-2 rounded bg-transparent border"
              style={{borderColor: "var(--border)"}}
            >
              <option value="">— not following anyone —</option>
              {traders.map(t => (
                <option key={t.id} value={t.id}>{t.display_name ?? t.email}</option>
              ))}
            </select>
          </section>

          <section className="p-4 rounded border space-y-3" style={{borderColor: "var(--border)", background: "var(--panel)"}}>
            <h2 className="font-medium">Trade multiplier</h2>
            <p className="text-sm" style={{color: "var(--muted)"}}>
              Each mirrored order will be sized at trader_qty × this multiplier. Default is 1. Max 10.
            </p>
            <div className="flex items-center gap-2">
              <input
                type="number" step="0.1" min="0.1" max="10"
                className="w-32 p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}}
                value={multInput}
                onChange={(e) => setMultInput(e.target.value)}
              />
              <button
                onClick={saveMultiplier}
                disabled={multBusy || parseFloat(multInput) === parseFloat(sub.multiplier)}
                className="px-4 py-2 rounded font-medium inline-flex items-center gap-2"
                style={{background: "var(--accent)", color: "#06121f"}}
              >
                <span>Save</span>
                {multBusy && <Spinner />}
              </button>
              {parseFloat(multInput) !== parseFloat(sub.multiplier) && (
                <button
                  onClick={() => setMultInput(parseFloat(sub.multiplier).toString())}
                  className="px-3 py-2 text-sm rounded border"
                  style={{borderColor: "var(--border)", color: "var(--muted)"}}
                >
                  Reset
                </button>
              )}
              <span className="text-sm ml-2" style={{color: "var(--muted)"}}>
                current: ×{fmtMultiplier(sub.multiplier)}
              </span>
            </div>
          </section>

          <section className="p-4 rounded border space-y-3" style={{borderColor: "var(--border)", background: "var(--panel)"}}>
            <h2 className="font-medium">Daily Loss Limit</h2>
            <p className="text-sm" style={{color: "var(--muted)"}}>
              When today&rsquo;s realized loss reaches this amount, copy trading turns OFF automatically. Resets daily at UTC midnight. Leave blank to disable.
            </p>

            {/* today's P&L + headroom display */}
            <div className="grid grid-cols-3 gap-4 p-3 rounded" style={{background: "rgba(255,255,255,0.02)"}}>
              <div>
                <div className="text-[10px] uppercase tracking-wider" style={{color: "var(--muted)"}}>Today&rsquo;s P&amp;L</div>
                <div className="text-sm font-medium mt-0.5" style={{color: todaysPnL >= 0 ? "var(--good)" : "var(--bad)"}}>
                  {fmt(sub.todays_realized_pnl)}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider" style={{color: "var(--muted)"}}>Limit</div>
                <div className="text-sm font-medium mt-0.5">{fmt(sub.daily_loss_limit)}</div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider" style={{color: "var(--muted)"}}>Headroom</div>
                <div className="text-sm font-medium mt-0.5" style={{color: (headroom ?? 1) > 0 ? "var(--text)" : "var(--bad)"}}>
                  {limit === null ? "—" : fmt(String(headroom))}
                </div>
              </div>
            </div>

            {limit !== null && (
              <div className="h-1 rounded overflow-hidden" style={{background: "var(--border)"}}>
                <div
                  style={{
                    width: `${limitPct}%`,
                    height: "100%",
                    background: limitPct >= 100 ? "var(--bad)" : limitPct >= 75 ? "#f59e0b" : "var(--good)",
                    transition: "width 0.3s ease",
                  }}
                />
              </div>
            )}

            <div className="flex items-center gap-2">
              <span style={{color: "var(--muted)"}}>$</span>
              <input
                type="number" step="0.01" min="0"
                placeholder="(no limit)"
                className="w-40 p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}}
                value={limitInput}
                onChange={(e) => setLimitInput(e.target.value)}
              />
              <button
                onClick={saveLimit}
                disabled={limitBusy || limitInput === (sub.daily_loss_limit ?? "")}
                className="px-4 py-2 rounded font-medium inline-flex items-center gap-2"
                style={{background: "var(--accent)", color: "#06121f"}}
              >
                <span>Save</span>
                {limitBusy && <Spinner />}
              </button>
              {sub.daily_loss_limit !== null && (
                <button
                  onClick={() => { setLimitInput(""); }}
                  className="px-3 py-2 text-sm rounded border"
                  style={{borderColor: "var(--border)", color: "var(--muted)"}}
                  title="Clear the limit (then click Save)"
                >
                  Clear
                </button>
              )}
            </div>
          </section>

          <section className="p-4 rounded border space-y-3" style={{borderColor: "var(--border)", background: "var(--panel)"}}>
            <div className="flex items-center justify-between">
              <div>
                <h2 className="font-medium">Copy trading</h2>
                <p className="text-sm" style={{color: "var(--muted)"}}>
                  When ON, your linked broker accounts mirror the trader at multiplier ×{fmtMultiplier(sub.multiplier)}.
                </p>
              </div>
              <button
                onClick={() => toggleCopy(!sub.copy_enabled)}
                className="px-4 py-2 rounded font-medium"
                style={{background: sub.copy_enabled ? "var(--good)" : "var(--border)", color: sub.copy_enabled ? "#06121f" : "var(--text)"}}
              >
                {sub.copy_enabled ? "ON" : "OFF"}
              </button>
            </div>
          </section>
        </>
      )}

      {user.role === "trader" && trd && (
        <section className="p-4 rounded border space-y-3" style={{borderColor: "var(--border)", background: "var(--panel)"}}>
          <div className="flex items-center justify-between">
            <div>
              <h2 className="font-medium">Master trading switch</h2>
              <p className="text-sm" style={{color: "var(--muted)"}}>
                When OFF, the platform refuses to place new orders (yours and any subscriber mirrors).
              </p>
            </div>
            <button
              onClick={() => toggleTrading(!trd.trading_enabled)}
              className="px-4 py-2 rounded font-medium"
              style={{background: trd.trading_enabled ? "var(--good)" : "var(--border)", color: trd.trading_enabled ? "#06121f" : "var(--text)"}}
            >
              {trd.trading_enabled ? "ON" : "OFF"}
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
