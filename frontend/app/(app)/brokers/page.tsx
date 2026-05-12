"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { BrokerAccount, SyncResult } from "@/lib/types";

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
  const params = useSearchParams();
  const [accounts, setAccounts] = useState<BrokerAccount[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function load() {
    setAccounts(await api<BrokerAccount[]>("/api/brokers"));
  }

  async function sync() {
    setBusy(true);
    setErr(null);
    try {
      const res = await api<SyncResult>("/api/brokers/sync", { method: "POST" });
      setAccounts(res.accounts);
      if (res.added || res.removed) {
        setInfo(`Synced: +${res.added} added, ${res.removed} removed.`);
      } else {
        setInfo("No new connections found at SnapTrade.");
      }
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : "sync failed");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    load();
    // If we landed back here from the SnapTrade portal redirect, auto-sync.
    if (params.get("snaptrade") === "connected") {
      sync();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function connect() {
    setBusy(true);
    setErr(null);
    setInfo(null);
    try {
      const res = await api<{ redirect_uri: string }>("/api/brokers/portal-url", {
        method: "POST",
      });
      // Open SnapTrade Connection Portal in a new tab. After the user finishes,
      // SnapTrade redirects back to /brokers?snaptrade=connected, and the
      // useEffect on this page will auto-sync.
      window.open(res.redirect_uri, "_blank", "noopener,noreferrer");
      setInfo("Opened SnapTrade Connection Portal in a new tab. After you finish linking, click Refresh.");
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : "could not open portal");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    if (!confirm("Disconnect this brokerage?")) return;
    await api(`/api/brokers/${id}`, { method: "DELETE" });
    load();
  }

  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({});
  async function refreshBalance(id: string) {
    setRefreshing(p => ({ ...p, [id]: true }));
    try {
      const updated = await api<BrokerAccount>(`/api/brokers/${id}/refresh-balance`, { method: "POST" });
      setAccounts(cur => cur.map(a => (a.id === id ? updated : a)));
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : "balance refresh failed");
    } finally {
      setRefreshing(p => ({ ...p, [id]: false }));
    }
  }

  return (
    <div className="space-y-8 max-w-4xl">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Broker connections</h1>
        <div className="flex gap-2">
          <button
            onClick={sync}
            disabled={busy}
            className="px-3 py-2 text-sm rounded border"
            style={{ borderColor: "var(--border)" }}
          >
            Refresh
          </button>
          <button
            onClick={connect}
            disabled={busy}
            className="px-4 py-2 rounded font-medium"
            style={{ background: "var(--accent)", color: "#06121f" }}
          >
            {busy ? "…" : "Connect a brokerage"}
          </button>
        </div>
      </div>

      <p className="text-sm" style={{ color: "var(--muted)" }}>
        Connections are managed by SnapTrade. Click "Connect a brokerage" to open SnapTrade's portal —
        it'll let you pick from supported brokers (Alpaca, Schwab, E*TRADE, Webull, and others) and
        authenticate with that broker directly. We never see your broker credentials.
      </p>

      {info && (
        <div className="p-3 rounded border text-sm" style={{ borderColor: "var(--accent)", color: "var(--accent)" }}>
          {info}
        </div>
      )}
      {err && (
        <div className="p-3 rounded border text-sm" style={{ borderColor: "var(--bad)", color: "var(--bad)" }}>
          {err}
        </div>
      )}

      <section className="space-y-3">
        <h2 className="text-sm uppercase" style={{ color: "var(--muted)" }}>Your connections</h2>
        {accounts.length === 0 && (
          <p style={{ color: "var(--muted)" }}>
            No brokers connected yet. Click "Connect a brokerage" above to get started.
          </p>
        )}
        <div className="space-y-2">
          {accounts.map(a => (
            <div
              key={a.id}
              className="p-4 rounded border"
              style={{ borderColor: "var(--border)", background: "var(--panel)" }}
            >
              <div className="flex items-start justify-between">
                <div>
                  <div className="font-medium">
                    {a.label}
                    <span className="text-xs uppercase ml-2" style={{ color: "var(--muted)" }}>
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
                  className="px-3 py-1 text-sm rounded border"
                  style={{ borderColor: "var(--bad)", color: "var(--bad)" }}
                >
                  Disconnect
                </button>
              </div>

              {/* balance row */}
              <div className="mt-3 pt-3 border-t flex items-end justify-between" style={{ borderColor: "var(--border)" }}>
                <div className="grid grid-cols-3 gap-6 flex-1">
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Cash</div>
                    <div className="text-sm font-medium mt-0.5">{fmtMoney(a.cash, a.currency)}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Buying power</div>
                    <div className="text-sm font-medium mt-0.5">{fmtMoney(a.buying_power, a.currency)}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Total equity</div>
                    <div className="text-sm font-medium mt-0.5">{fmtMoney(a.total_equity, a.currency)}</div>
                  </div>
                </div>
                <div className="flex items-center gap-2 ml-4">
                  <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                    updated {fmtRelative(a.balance_updated_at)}
                  </span>
                  <button
                    onClick={() => refreshBalance(a.id)}
                    disabled={refreshing[a.id]}
                    className="px-2 py-1 text-sm rounded border"
                    style={{ borderColor: "var(--border)" }}
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
    </div>
  );
}
