"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import type { BrokerAccount, BrokerName } from "@/lib/types";

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

/** Connecting Webull is a two-step flow (request MFA → submit code).
 *  Tracking that as a phase keeps the form components small and avoids
 *  conditional `disabled` mess inside one giant <form>. */
type WebullPhase = "idle" | "mfa-sent";

export default function BrokersPage() {
  const [accounts, setAccounts] = useState<BrokerAccount[]>([]);

  // Which broker the user has chosen to connect. Defaults to alpaca to
  // preserve the previous behaviour for existing users.
  const [chosenBroker, setChosenBroker] = useState<BrokerName>("alpaca");

  // Alpaca form state
  const [label, setLabel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [paper, setPaper] = useState(true);

  // Webull form state. We keep username/password in state across the
  // two API hops so the user only types them once.
  const [wbUsername, setWbUsername] = useState("");
  const [wbPassword, setWbPassword] = useState("");
  const [wbMfa, setWbMfa] = useState("");
  const [wbTradePin, setWbTradePin] = useState("");
  const [wbPaper, setWbPaper] = useState(true);
  const [wbPhase, setWbPhase] = useState<WebullPhase>("idle");

  // SnapTrade form state — much smaller because the actual auth happens
  // on SnapTrade's hosted portal. We collect a label, optionally a
  // pre-selected broker slug (left blank lets the user pick on the portal),
  // and a paper-trading hint that's mostly informational (most SnapTrade
  // brokers don't expose a 'paper' flag; we store it for our own UI).
  const [stLabel, setStLabel] = useState("");
  const [stBrokerSlug, setStBrokerSlug] = useState("");
  const [stPaper, setStPaper] = useState(false);

  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({});

  const router = useRouter();
  const searchParams = useSearchParams();

  async function load() {
    setAccounts(await api<BrokerAccount[]>("/api/brokers"));
  }
  useEffect(() => { load(); }, []);

  // SnapTrade portal completion handler. The portal redirects back to
  // /brokers?snaptrade_connected=1 — when we see that param we call
  // /finish to persist the new connection. Strip the param afterwards
  // so a refresh doesn't re-trigger the call.
  //
  // The `finishFiredRef` guard is critical: React Strict Mode in dev
  // double-invokes effects on mount, and `searchParams` references can
  // also re-trigger this hook. Without the ref guard we'd fire /finish
  // twice in parallel — both calls would race past `_evict_existing_
  // brokers` before either had inserted, and we'd end up with two
  // BrokerAccount rows pointing at the same SnapTrade authorization
  // (both starting their own polling listeners and double-processing
  // every trade). useRef survives strict-mode double-renders because
  // it's the same ref object across both effect invocations.
  const finishFiredRef = useRef(false);
  useEffect(() => {
    if (searchParams.get("snaptrade_connected") !== "1") return;
    if (finishFiredRef.current) return;
    finishFiredRef.current = true;
    let cancelled = false;
    (async () => {
      setBusy(true);
      try {
        const label = sessionStorage.getItem("snaptrade:label") || "SnapTrade";
        await api("/api/brokers/snaptrade/finish", {
          method: "POST",
          body: JSON.stringify({ label }),
        });
        if (cancelled) return;
        sessionStorage.removeItem("snaptrade:label");
        notify.success("SnapTrade connected — balance fetched");
        await load();
      } catch (e) {
        if (!cancelled) notify.fromError(e, "SnapTrade connect failed");
      } finally {
        if (!cancelled) {
          setBusy(false);
          router.replace("/brokers");
        }
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  function resetConnectForms() {
    setLabel(""); setApiKey(""); setApiSecret("");
    setWbUsername(""); setWbPassword(""); setWbMfa(""); setWbTradePin("");
    setWbPhase("idle");
    setStLabel(""); setStBrokerSlug(""); setStPaper(false);
  }

  async function connectAlpaca(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/api/brokers", {
        method: "POST",
        body: JSON.stringify({
          broker: "alpaca",
          label: label.trim(),
          alpaca: { api_key: apiKey, api_secret: apiSecret, paper },
        }),
      });
      resetConnectForms();
      notify.success("Alpaca connected — balance fetched");
      await load();
    } catch (e) {
      notify.fromError(e, "Alpaca connect failed");
    } finally {
      setBusy(false);
    }
  }

  async function startWebullMfa(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/api/brokers/webull/start-mfa", {
        method: "POST",
        body: JSON.stringify({ username: wbUsername, paper: wbPaper }),
      });
      setWbPhase("mfa-sent");
      notify.success("MFA code sent — check your phone or email");
    } catch (e) {
      notify.fromError(e, "Webull MFA request failed");
    } finally {
      setBusy(false);
    }
  }

  async function finishWebullConnect(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/api/brokers", {
        method: "POST",
        body: JSON.stringify({
          broker: "webull",
          label: label.trim(),
          webull: {
            username: wbUsername,
            password: wbPassword,
            mfa_code: wbMfa,
            trade_pin: wbTradePin,
            paper: wbPaper,
          },
        }),
      });
      resetConnectForms();
      notify.success("Webull connected — balance fetched");
      await load();
    } catch (e) {
      notify.fromError(e, "Webull connect failed");
    } finally {
      setBusy(false);
    }
  }

  async function startSnaptrade(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const trimmedLabel = stLabel.trim();
      // Stash the label so the redirect-back handler can pass it to /finish.
      // SnapTrade's portal navigates the window away from our app, so we
      // can't hold it in component state across the round-trip.
      sessionStorage.setItem("snaptrade:label", trimmedLabel);
      const resp = await api<{ portal_url: string }>("/api/brokers/snaptrade/start", {
        method: "POST",
        body: JSON.stringify({
          label: trimmedLabel,
          broker_slug: stBrokerSlug.trim() || null,
          paper: stPaper,
        }),
      });
      // Same-tab redirect — SnapTrade sends the user back via
      // ``custom_redirect`` to /brokers?snaptrade_connected=1 where our
      // useEffect calls /finish.
      window.location.href = resp.portal_url;
    } catch (e) {
      sessionStorage.removeItem("snaptrade:label");
      notify.fromError(e, "SnapTrade start failed");
      setBusy(false);
    }
    // NOTE: no setBusy(false) on success — the page is navigating away.
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

  const hasConnected = accounts.length > 0;

  return (
    <div className="space-y-8 max-w-4xl">
      <h1 className="text-2xl font-semibold">Broker connections</h1>

      <p className="text-sm" style={{ color: "var(--muted)" }}>
        One broker per account — connecting a new one replaces the previous
        connection. Keys/passwords never leave the server: encrypted at rest
        with Fernet (AES-128).
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
                    className="btn-ghost px-2 py-1 text-sm inline-flex items-center gap-1.5"
                    title="Refresh balance"
                  >
                    <span>↻</span>
                    {refreshing[a.id] && <Spinner />}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {hasConnected ? null : (
      <section className="card p-5 space-y-4 max-w-lg">
        {/* Broker selector — switching wipes the in-progress form for the
            other broker so we don't post stale fields. */}
        <div>
          <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Choose broker</label>
          <select
            value={chosenBroker}
            onChange={e => {
              setChosenBroker(e.target.value as BrokerName);
              resetConnectForms();
            }}
            className="w-full p-2"
          >
            <option value="alpaca">Alpaca (direct, realtime WebSocket)</option>
            <option value="webull">Webull (direct, polling ~2s)</option>
            <option value="snaptrade">SnapTrade (aggregator — 20+ brokers, polling ~5–60s)</option>
          </select>
        </div>

        {chosenBroker === "alpaca" && (
          <>
            <h2 className="font-semibold">Connect an Alpaca account</h2>
            <p className="text-xs" style={{ color: "var(--muted)" }}>
              From <a href="https://app.alpaca.markets" target="_blank" rel="noreferrer" className="underline" style={{ color: "var(--accent)" }}>app.alpaca.markets</a>:
              {" "}select Paper Trading → click your name → API Keys → Generate.
            </p>
            <form onSubmit={connectAlpaca} className="space-y-3">
              <div>
                <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Label</label>
                <input type="text" className="w-full p-2" placeholder="Alpaca Paper" value={label} onChange={e => setLabel(e.target.value)} required />
              </div>
              <div>
                <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>API key ID</label>
                <input type="text" className="w-full p-2 font-mono text-sm" placeholder="PKxxxxxxxxxxxxxxxxxx" value={apiKey} onChange={e => setApiKey(e.target.value)} required />
              </div>
              <div>
                <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Secret key</label>
                <input type="password" className="w-full p-2 font-mono text-sm" placeholder="(only shown once at generation)" value={apiSecret} onChange={e => setApiSecret(e.target.value)} required />
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={paper} onChange={e => setPaper(e.target.checked)} />
                <span>Paper-trading account (recommended for testing)</span>
              </label>
              <button disabled={busy} className="btn-primary px-4 py-2 text-sm inline-flex items-center gap-2">
                <span>Connect</span>
                {busy && <Spinner />}
              </button>
            </form>
          </>
        )}

        {chosenBroker === "webull" && (
          <>
            <h2 className="font-semibold">Connect a Webull account</h2>
            <p className="text-xs" style={{ color: "var(--muted)" }}>
              Webull uses login credentials + MFA (no API keys). Real-time
              order updates are <em>polled</em> every ~2s — comparable latency
              to Alpaca&apos;s socket in practice. You&apos;ll also need your
              6-digit Webull trade PIN.
            </p>
            {wbPhase === "idle" && (
              <form onSubmit={startWebullMfa} className="space-y-3">
                <div>
                  <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Label</label>
                  <input type="text" className="w-full p-2" placeholder="Webull Paper" value={label} onChange={e => setLabel(e.target.value)} required />
                </div>
                <div>
                  <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Email or phone</label>
                  <input type="text" className="w-full p-2" placeholder="you@example.com" value={wbUsername} onChange={e => setWbUsername(e.target.value)} required />
                </div>
                <div>
                  <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Password</label>
                  <input type="password" className="w-full p-2" value={wbPassword} onChange={e => setWbPassword(e.target.value)} required />
                </div>
                {/* Mode toggle — defaults to paper. Bumped to a 2-button
                    pill instead of a checkbox because users were missing
                    the checkbox and submitting in paper mode by accident
                    when they meant live, then wondering why their live
                    orders weren't reflecting. */}
                <div>
                  <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Account mode</label>
                  <div className="inline-flex rounded-md overflow-hidden" style={{ border: "1px solid var(--border)" }}>
                    <button
                      type="button"
                      onClick={() => setWbPaper(true)}
                      className="px-3 py-1.5 text-sm"
                      style={{
                        background: wbPaper ? "var(--accent)" : "transparent",
                        color: wbPaper ? "#fff" : "var(--muted)",
                      }}
                    >
                      Paper
                    </button>
                    <button
                      type="button"
                      onClick={() => setWbPaper(false)}
                      className="px-3 py-1.5 text-sm"
                      style={{
                        background: !wbPaper ? "var(--bad)" : "transparent",
                        color: !wbPaper ? "#fff" : "var(--muted)",
                      }}
                    >
                      Live
                    </button>
                  </div>
                  <p className="text-[10px] mt-1" style={{ color: "var(--muted)" }}>
                    Live places real-money orders. Webull paper and live use
                    separate auth endpoints — switching after MFA is sent
                    invalidates the code.
                  </p>
                </div>
                <button disabled={busy} className="btn-primary px-4 py-2 text-sm inline-flex items-center gap-2">
                  <span>Send MFA code</span>
                  {busy && <Spinner />}
                </button>
              </form>
            )}
            {wbPhase === "mfa-sent" && (
              <form onSubmit={finishWebullConnect} className="space-y-3">
                <p className="text-xs" style={{ color: "var(--good)" }}>
                  ✓ MFA code sent to <span className="font-mono">{wbUsername}</span>. Enter it below along with your trade PIN.
                </p>
                {/* Confirm-the-mode banner in step 2. Users were missing the
                    paper checkbox in step 1 and only realising they'd
                    connected paper after orders weren't going to their
                    real account. Showing it here gives them a last chance
                    to bail. */}
                <div
                  className="text-xs px-3 py-2 rounded-md"
                  style={{
                    border: `1px solid ${wbPaper ? "var(--accent)" : "var(--bad)"}55`,
                    background: `${wbPaper ? "var(--accent)" : "var(--bad)"}15`,
                    color: wbPaper ? "var(--accent)" : "var(--bad)",
                  }}
                >
                  Connecting <strong>{wbPaper ? "Paper" : "Live"}</strong> Webull account.
                  {" "}
                  {!wbPaper && <>This will place <strong>real-money orders</strong>.</>}
                  {" "}
                  Wrong mode? Click <strong>Back</strong> and request a fresh MFA code.
                </div>
                <div>
                  <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>MFA code</label>
                  <input type="text" inputMode="numeric" className="w-full p-2 font-mono" placeholder="6-digit code" value={wbMfa} onChange={e => setWbMfa(e.target.value)} required />
                </div>
                <div>
                  <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Trade PIN</label>
                  <input type="password" inputMode="numeric" className="w-full p-2 font-mono" placeholder="6-digit trade PIN" value={wbTradePin} onChange={e => setWbTradePin(e.target.value)} required />
                </div>
                <div className="flex items-center gap-2">
                  <button disabled={busy} className="btn-primary px-4 py-2 text-sm inline-flex items-center gap-2">
                    <span>Connect Webull</span>
                    {busy && <Spinner />}
                  </button>
                  <button
                    type="button"
                    onClick={() => { setWbPhase("idle"); setWbMfa(""); setWbTradePin(""); }}
                    className="btn-ghost px-3 py-2 text-sm"
                  >
                    Back
                  </button>
                </div>
              </form>
            )}
          </>
        )}

        {chosenBroker === "snaptrade" && (
          <>
            <h2 className="font-semibold">Connect via SnapTrade</h2>
            <p className="text-xs" style={{ color: "var(--muted)" }}>
              SnapTrade is a hosted broker aggregator — pick from Robinhood,
              E*TRADE, Tradier, IBKR, Schwab, Webull and ~15 others on their
              portal. Credentials never touch our server.
              {" "}
              <strong>Heads up:</strong> order updates are polled, so
              end-to-end mirror latency is typically 5–60s vs. &lt;1s for
              Alpaca-direct. Best for breadth of broker coverage, not
              fastest mirroring.
            </p>
            <form onSubmit={startSnaptrade} className="space-y-3">
              <div>
                <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Label</label>
                <input
                  type="text"
                  className="w-full p-2"
                  placeholder="Robinhood via SnapTrade"
                  value={stLabel}
                  onChange={e => setStLabel(e.target.value)}
                  required
                />
              </div>
              <div>
                <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>
                  Pre-select broker (optional)
                </label>
                <input
                  type="text"
                  className="w-full p-2 font-mono text-sm"
                  placeholder="ROBINHOOD, ETRADE, TRADIER, IBKR, … (leave blank to pick on portal)"
                  value={stBrokerSlug}
                  onChange={e => setStBrokerSlug(e.target.value.toUpperCase())}
                />
                <p className="text-[10px] mt-1" style={{ color: "var(--muted)" }}>
                  SnapTrade broker slug. Skip this to see the full picker on the portal.
                </p>
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={stPaper} onChange={e => setStPaper(e.target.checked)} />
                <span>Paper-trading account (informational only)</span>
              </label>
              <button disabled={busy} className="btn-primary px-4 py-2 text-sm inline-flex items-center gap-2">
                <span>Open SnapTrade portal</span>
                {busy && <Spinner />}
              </button>
              <p className="text-[10px]" style={{ color: "var(--muted)" }}>
                You&apos;ll be redirected to SnapTrade&apos;s portal to sign in to
                your broker. After you finish there, you&apos;ll be sent back
                here automatically.
              </p>
            </form>
          </>
        )}
      </section>
      )}
    </div>
  );
}
