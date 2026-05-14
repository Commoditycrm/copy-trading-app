"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import type { BrokerAccount } from "@/lib/types";

function statusColor(s: BrokerAccount["connection_status"]): string {
  return s === "connected" ? "var(--good)" : s === "error" ? "var(--bad)" : "var(--muted)";
}

function fmtMoney(amount: string | null, currency: string | null): string {
  if (amount === null) return "—";
  const v = Number(amount);
  if (!Number.isFinite(v)) return "—";
  try {
    return v.toLocaleString(undefined, {
      style: "currency",
      currency: currency || "USD",
      maximumFractionDigits: 2,
    });
  } catch {
    return `${v.toFixed(2)} ${currency ?? ""}`.trim();
  }
}

function fmtRelative(iso: string | null): string {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return "just now";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}

export default function BrokersPage() {
  const [accounts, setAccounts] = useState<BrokerAccount[]>([]);
  const [label, setLabel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [paper, setPaper] = useState(true);
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({});

  async function load() {
    setAccounts(await api<BrokerAccount[]>("/api/brokers"));
  }
  useEffect(() => { load(); }, []);

  async function connect(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/api/brokers", {
        method: "POST",
        body: JSON.stringify({
          broker: "alpaca",
          label: label || (paper ? "Alpaca Paper" : "Alpaca"),
          alpaca: { api_key: apiKey, api_secret: apiSecret, paper },
        }),
      });
      setLabel(""); setApiKey(""); setApiSecret("");
      notify.success("Broker connected — balance fetched");
      await load();
    } catch (e) {
      notify.fromError(e, "Broker connect failed");
    } finally {
      setBusy(false);
    }
  }

  async function refreshBalance(id: string) {
    setRefreshing(p => ({ ...p, [id]: true }));
    try {
      const updated = await api<BrokerAccount>(`/api/brokers/${id}/refresh-balance`, { method: "POST" });
      setAccounts(cur => cur.map(a => (a.id === id ? updated : a)));
      notify.success("Balance refreshed");
    } catch (e) {
      notify.fromError(e, "Balance refresh failed");
    } finally {
      setRefreshing(p => ({ ...p, [id]: false }));
    }
  }

  async function remove(id: string) {
    if (!confirm("Disconnect this brokerage?")) return;
    try {
      await api(`/api/brokers/${id}`, { method: "DELETE" });
      notify.success("Broker disconnected");
    } catch (e) {
      notify.fromError(e, "Disconnect failed");
    }
    load();
  }

  return (
    <div className="space-y-8 max-w-4xl">
      <h1 className="text-2xl font-semibold">Broker connections</h1>

      <p className="text-sm" style={{ color: "var(--muted)" }}>
        Currently supported: <strong>Alpaca</strong>. Paper and live both work. Get your API keys from
        the Alpaca dashboard (app.alpaca.markets). Keys never leave the server — they're encrypted
        at rest with Fernet (AES-128).
      </p>

      <section className="space-y-3">
        <h2 className="text-sm uppercase tracking-wider" style={{ color: "var(--muted)" }}>Your connections</h2>
        {accounts.length === 0 && <p style={{ color: "var(--muted)" }}>No brokers connected yet — fill in the form below to add one.</p>}
        <div className="space-y-2">
          {accounts.map(a => (
            <div key={a.id} className="card p-4">
              <div className="flex items-start justify-between">
                <div>
                  <div className="font-medium">
                    {a.label}
                    <span className="text-xs uppercase ml-2 tracking-wider" style={{ color: "var(--muted)" }}>
                      {a.broker}{a.is_paper ? " · paper" : ""}{a.supports_fractional ? " · fractional" : ""}
                    </span>
                  </div>
                  <div className="text-xs mt-1" style={{ color: statusColor(a.connection_status) }}>
                    ● {a.connection_status}
                  </div>
                  {a.broker_account_number && (
                    <div className="text-xs mt-1" style={{ color: "var(--muted)" }}>
                      account: {a.broker_account_number}
                    </div>
                  )}
                  {a.last_error && (
                    <div className="text-xs mt-1" style={{ color: "var(--bad)" }}>{a.last_error}</div>
                  )}
                </div>
                <button
                  onClick={() => remove(a.id)}
                  className="btn-ghost px-3 py-1 text-sm"
                  style={{ color: "var(--bad)", borderColor: "rgba(255,107,107,0.4)" }}
                >
                  Disconnect
                </button>
              </div>

              {/* Balance row */}
              <div className="mt-3 pt-3 border-t flex items-end justify-between" style={{ borderColor: "var(--border)" }}>
                <div className="grid grid-cols-3 gap-6 flex-1">
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Cash</div>
                    <div className="text-sm font-medium mt-0.5 num">{fmtMoney(a.cash, a.currency)}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Buying power</div>
                    <div className="text-sm font-medium mt-0.5 num">{fmtMoney(a.buying_power, a.currency)}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Total equity</div>
                    <div className="text-sm font-medium mt-0.5 num">{fmtMoney(a.total_equity, a.currency)}</div>
                  </div>
                </div>
                <div className="flex items-center gap-2 ml-4">
                  <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                    updated {fmtRelative(a.balance_updated_at)}
                  </span>
                  <button
                    onClick={() => refreshBalance(a.id)}
                    disabled={refreshing[a.id]}
                    className="btn-ghost px-2 py-1 text-sm"
                    title="Refresh balance"
                  >
                    {refreshing[a.id] ? "…" : "↻"}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="card p-5 space-y-4 max-w-lg">
        <h2 className="font-semibold">Connect an Alpaca account</h2>
        <p className="text-xs" style={{ color: "var(--muted)" }}>
          From <a href="https://app.alpaca.markets" target="_blank" rel="noreferrer" className="underline" style={{ color: "var(--accent)" }}>app.alpaca.markets</a>:
          {" "}select Paper Trading → click your name → API Keys → Generate.
        </p>
        <form onSubmit={connect} className="space-y-3">
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Label (optional)</label>
            <input className="w-full p-2" placeholder="Alpaca Paper" value={label} onChange={e => setLabel(e.target.value)} />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>API key ID</label>
            <input className="w-full p-2 font-mono text-sm" placeholder="PKxxxxxxxxxxxxxxxxxx" value={apiKey} onChange={e => setApiKey(e.target.value)} required />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Secret key</label>
            <input className="w-full p-2 font-mono text-sm" placeholder="(only shown once at generation)" type="password" value={apiSecret} onChange={e => setApiSecret(e.target.value)} required />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={paper} onChange={e => setPaper(e.target.checked)} />
            <span>Paper-trading account (recommended for testing)</span>
          </label>
          <button disabled={busy} className="btn-primary px-4 py-2 text-sm">
            {busy ? "Verifying…" : "Connect"}
          </button>
        </form>
      </section>
    </div>
  );
}
