"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { SubscriberSettings, TraderSettings, User } from "@/lib/types";

export default function SettingsPage() {
  const [user, setUser] = useState<User | null>(null);
  const [sub, setSub] = useState<SubscriberSettings | null>(null);
  const [trd, setTrd] = useState<TraderSettings | null>(null);
  const [traders, setTraders] = useState<{ id: string; display_name: string | null; email: string }[]>([]);
  const [multInput, setMultInput] = useState("");
  const [multBusy, setMultBusy] = useState(false);
  const [multErr, setMultErr] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      const u = await api<User>("/api/auth/me");
      setUser(u);
      if (u.role === "subscriber") {
        const s = await api<SubscriberSettings>("/api/settings/subscriber");
        setSub(s);
        setMultInput(s.multiplier);
        setTraders(await api("/api/settings/traders"));
      } else {
        setTrd(await api<TraderSettings>("/api/settings/trader"));
      }
    })().catch(e => setErr(e instanceof ApiError ? String(e.detail) : String(e)));
  }, []);

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
    setMultErr(null);
    setMultBusy(true);
    try {
      const s = await api<SubscriberSettings>("/api/settings/subscriber/multiplier", {
        method: "PATCH",
        body: JSON.stringify({ multiplier: multInput }),
      });
      setSub(s);
      setMultInput(s.multiplier);
    } catch (e) {
      setMultErr(e instanceof ApiError ? String(e.detail) : "could not update multiplier");
    } finally {
      setMultBusy(false);
    }
  }
  async function toggleTrading(next: boolean) {
    setTrd(await api<TraderSettings>("/api/settings/trader", {
      method: "PATCH", body: JSON.stringify({ trading_enabled: next })
    }));
  }

  if (err) return <p style={{color: "var(--bad)"}}>{err}</p>;
  if (!user) return <p style={{color: "var(--muted)"}}>Loading…</p>;

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
                type="number" step="0.001" min="0.001" max="10"
                className="w-32 p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}}
                value={multInput}
                onChange={(e) => setMultInput(e.target.value)}
              />
              <button
                onClick={saveMultiplier}
                disabled={multBusy || multInput === sub.multiplier}
                className="px-4 py-2 rounded font-medium"
                style={{background: "var(--accent)", color: "#06121f"}}
              >
                {multBusy ? "Saving…" : "Save"}
              </button>
              {multInput !== sub.multiplier && (
                <button
                  onClick={() => { setMultInput(sub.multiplier); setMultErr(null); }}
                  className="px-3 py-2 text-sm rounded border"
                  style={{borderColor: "var(--border)", color: "var(--muted)"}}
                >
                  Reset
                </button>
              )}
              <span className="text-sm ml-2" style={{color: "var(--muted)"}}>
                current: ×{sub.multiplier} (tier: {sub.subscription_tier})
              </span>
            </div>
            {multErr && <p className="text-sm" style={{color: "var(--bad)"}}>{multErr}</p>}
          </section>

          <section className="p-4 rounded border space-y-3" style={{borderColor: "var(--border)", background: "var(--panel)"}}>
            <div className="flex items-center justify-between">
              <div>
                <h2 className="font-medium">Copy trading</h2>
                <p className="text-sm" style={{color: "var(--muted)"}}>
                  When ON, your linked broker accounts mirror the trader at multiplier ×{sub.multiplier}.
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
