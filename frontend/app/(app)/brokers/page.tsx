"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { ConfirmModal } from "@/components/ConfirmModal";
import type { BrokerAccount, BrokerName } from "@/lib/types";

/** Per-broker presentation metadata — drives the picker tiles, the
 *  connected-account avatar, and the latency badge. Keeping it in one
 *  place means adding a broker later is a single entry. */
const BROKER_META: Record<BrokerName, {
  name: string;
  tagline: string;
  latency: string;
  latencyTone: "good" | "warn";
  accent: string;
}> = {
  alpaca:    { name: "Alpaca",    tagline: "Direct API keys",      latency: "Realtime", latencyTone: "good", accent: "#f5a623" },
  webull:    { name: "Webull",    tagline: "Login + MFA",          latency: "~2s",      latencyTone: "good", accent: "#3b82f6" },
  snaptrade: { name: "SnapTrade", tagline: "20+ brokers · hosted", latency: "5–60s",    latencyTone: "warn", accent: "#14b8a6" },
};

const BROKER_ORDER: BrokerName[] = ["alpaca", "webull", "snaptrade"];

/** Rounded-square avatar with the broker's initial in its brand hue. */
function BrokerAvatar({ broker, size = 40 }: { broker: BrokerName; size?: number }) {
  const meta = BROKER_META[broker];
  return (
    <div
      className="grid place-items-center font-semibold shrink-0"
      style={{
        width: size,
        height: size,
        borderRadius: Math.round(size * 0.28),
        background: `${meta.accent}1f`,
        color: meta.accent,
        border: `1px solid ${meta.accent}40`,
        fontSize: Math.round(size * 0.42),
        lineHeight: 1,
      }}
    >
      {meta.name[0]}
    </div>
  );
}

/** Connection-status pill with a leading status dot. */
function StatusPill({ status }: { status: BrokerAccount["connection_status"] }) {
  const cls =
    status === "connected" ? "chip chip-good"
    : status === "error"   ? "chip chip-bad"
    : "chip";
  return (
    <span className={cls}>
      <span
        className="inline-block rounded-full"
        style={{ width: 6, height: 6, background: "currentColor" }}
      />
      {status}
    </span>
  );
}

/** Latency badge for the picker tiles. */
function LatencyBadge({ broker }: { broker: BrokerName }) {
  const meta = BROKER_META[broker];
  const tone = meta.latencyTone === "good"
    ? { color: "var(--good)", bg: "var(--good-soft)", bd: "rgba(34,197,94,0.25)" }
    : { color: "var(--warn)", bg: "rgba(255,200,87,0.12)", bd: "rgba(255,200,87,0.30)" };
  return (
    <span
      className="chip"
      style={{ color: tone.color, background: tone.bg, borderColor: tone.bd }}
    >
      {meta.latency}
    </span>
  );
}

/** One labelled metric in the balance row. */
function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>{label}</div>
      <div className="text-sm font-semibold mt-1 num">{value}</div>
    </div>
  );
}

/** Paper / Live account-mode radio group. ``value`` true = paper.
 *  ``name`` must be unique per form so the radios don't cross-bind.
 *  Live is tinted red so it's unmistakable before submit. */
function PaperLiveRadio({
  value,
  onChange,
  name,
  note,
}: {
  value: boolean;
  onChange: (paper: boolean) => void;
  name: string;
  note?: string;
}) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider mb-1.5" style={{ color: "var(--muted)" }}>
        Account mode
      </div>
      <div className="flex items-center gap-5">
        <label className="flex items-center gap-2 text-sm cursor-pointer">
          <input
            type="radio"
            name={name}
            checked={value === true}
            onChange={() => onChange(true)}
            style={{ accentColor: "var(--accent)" }}
          />
          <span style={{ color: value ? "var(--text)" : "var(--muted)" }}>Paper</span>
        </label>
        <label className="flex items-center gap-2 text-sm cursor-pointer">
          <input
            type="radio"
            name={name}
            checked={value === false}
            onChange={() => onChange(false)}
            style={{ accentColor: "var(--bad)" }}
          />
          <span style={{ color: !value ? "var(--bad)" : "var(--muted)" }}>Live</span>
        </label>
      </div>
      {note && (
        <p className="text-[10px] mt-1.5" style={{ color: "var(--muted)" }}>{note}</p>
      )}
    </div>
  );
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
  const [paper, setPaper] = useState(false);   // default Live

  // Webull form state. We keep username/password in state across the
  // two API hops so the user only types them once.
  const [wbUsername, setWbUsername] = useState("");
  const [wbPassword, setWbPassword] = useState("");
  const [wbMfa, setWbMfa] = useState("");
  const [wbTradePin, setWbTradePin] = useState("");
  const [wbPaper, setWbPaper] = useState(false);   // default Live
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

  // Disconnect confirmation — holds the account id pending removal (or
  // null when the modal is closed) + a busy flag for the in-flight DELETE.
  const [disconnectId, setDisconnectId] = useState<string | null>(null);
  const [disconnectBusy, setDisconnectBusy] = useState(false);

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
  // The `finishFiredRef` guard is what prevents a double /finish: React
  // Strict Mode in dev double-invokes effects on mount, and the
  // router.replace below changes `searchParams`, which would otherwise
  // re-trigger this hook. The ref survives strict-mode remounts (same
  // instance) so /finish fires exactly once.
  //
  // IMPORTANT: we deliberately do NOT use a `cancelled` flag + cleanup
  // here. With the ref guard already ensuring single execution, a
  // cancelled flag is actively harmful — strict mode's simulated
  // unmount fires the cleanup and sets cancelled=true on the *only*
  // in-flight request, which then bails out before load()/setBusy(false)
  // run. That left the Connect spinner stuck and the connected card
  // missing until a manual refresh, even though the backend had
  // already created the connection. React 18 makes setState after a
  // (simulated) unmount safe, so letting the async run to completion is
  // correct.
  const finishFiredRef = useRef(false);
  useEffect(() => {
    if (searchParams.get("snaptrade_connected") !== "1") return;
    if (finishFiredRef.current) return;
    finishFiredRef.current = true;
    (async () => {
      setBusy(true);
      try {
        const label = sessionStorage.getItem("snaptrade:label") || "SnapTrade";
        await api("/api/brokers/snaptrade/finish", {
          method: "POST",
          body: JSON.stringify({ label }),
        });
        sessionStorage.removeItem("snaptrade:label");
        notify.success("SnapTrade connected — balance fetched");
        await load();
      } catch (e) {
        notify.fromError(e, "SnapTrade connect failed");
      } finally {
        setBusy(false);
        router.replace("/brokers");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  function resetConnectForms() {
    setLabel(""); setApiKey(""); setApiSecret(""); setPaper(false);
    setWbUsername(""); setWbPassword(""); setWbMfa(""); setWbTradePin("");
    setWbPhase("idle"); setWbPaper(false);
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

  // Open the confirm modal (actual DELETE happens in confirmDisconnect).
  function remove(id: string) {
    setDisconnectId(id);
  }

  async function confirmDisconnect() {
    if (!disconnectId) return;
    setDisconnectBusy(true);
    try {
      await api(`/api/brokers/${disconnectId}`, { method: "DELETE" });
      notify.success("Broker disconnected");
      setDisconnectId(null);
      await load();
    } catch (e) {
      notify.fromError(e, "Disconnect failed");
    } finally {
      setDisconnectBusy(false);
    }
  }

  const hasConnected = accounts.length > 0;

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-2xl font-semibold">Broker connections</h1>
        <p className="text-sm mt-1.5" style={{ color: "var(--muted)" }}>
          One broker per account — connecting a new one replaces the previous.
          Keys and passwords are encrypted at rest with Fernet (AES-128) and
          never leave the server.
        </p>
      </div>

      {/* ── Connected account(s) ──────────────────────────────────────── */}
      {accounts.map(a => {
        const meta = BROKER_META[a.broker];
        return (
          <div key={a.id} className="card p-5">
            <div className="flex items-start justify-between gap-3.5">
              {/* Avatar is vertically centered against the two-line text
                  block (items-center). The Disconnect button stays pinned
                  to the top via the outer items-start. */}
              <div className="flex items-center gap-3.5 min-w-0">
                <BrokerAvatar broker={a.broker} size={46} />
                <div className="min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-semibold text-[15px] leading-none">{a.label}</span>
                    {/* Skip the broker-name chip when the label already is the
                        broker name — avoids "Alpaca  Alpaca". */}
                    {meta && a.label.trim().toLowerCase() !== meta.name.toLowerCase() && (
                      <span className="chip">{meta.name}</span>
                    )}
                    {a.is_paper && <span className="chip">paper</span>}
                    {a.supports_fractional && <span className="chip">fractional</span>}
                  </div>
                  <div className="flex items-center gap-2 mt-2 flex-wrap">
                    <StatusPill status={a.connection_status} />
                    {a.broker_account_number && (
                      <span className="text-xs num" style={{ color: "var(--muted)" }}>
                        · {a.broker_account_number}
                      </span>
                    )}
                  </div>
                  {a.last_error && (
                    <div className="text-xs mt-2" style={{ color: "var(--bad)" }}>{a.last_error}</div>
                  )}
                </div>
              </div>
              <button
                onClick={() => remove(a.id)}
                className="btn-danger-soft px-3 py-1.5 text-xs font-medium shrink-0"
              >
                Disconnect
              </button>
            </div>

            {/* Balance row */}
            <div className="mt-5 pt-4 hairline grid grid-cols-3 gap-4">
              <Stat label="Cash" value={fmtMoney(a.cash, a.currency)} />
              <Stat label="Buying power" value={fmtMoney(a.buying_power, a.currency)} />
              <Stat label="Total equity" value={fmtMoney(a.total_equity, a.currency)} />
            </div>
            <div className="mt-3 flex items-center justify-between">
              <span className="text-[10px]" style={{ color: "var(--faint)" }}>
                Updated {fmtRelative(a.balance_updated_at)}
              </span>
              <button
                onClick={() => refreshBalance(a.id)}
                disabled={refreshing[a.id]}
                className="btn-ghost px-2.5 py-1 text-xs inline-flex items-center gap-1.5"
                title="Refresh balance"
              >
                <span className={refreshing[a.id] ? "animate-spin" : ""} style={{ display: "inline-block" }}>↻</span>
                <span>Refresh</span>
              </button>
            </div>
          </div>
        );
      })}

      {hasConnected && (
        <p className="text-xs" style={{ color: "var(--faint)" }}>
          Want a different broker? Disconnect the one above first — only one
          broker can be active per account.
        </p>
      )}

      {hasConnected ? null : (
      <section className="card p-5 space-y-5">
        {/* Broker picker — clickable tiles. Switching wipes the in-progress
            form for the other broker so we don't post stale fields. */}
        <div>
          <div className="text-[11px] uppercase tracking-wider mb-2" style={{ color: "var(--muted)" }}>
            Choose a broker to connect
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5">
            {BROKER_ORDER.map(b => {
              const meta = BROKER_META[b];
              const active = chosenBroker === b;
              return (
                <button
                  key={b}
                  type="button"
                  onClick={() => { setChosenBroker(b); resetConnectForms(); }}
                  className="text-left p-3 rounded-xl transition"
                  style={{
                    border: active ? "1px solid var(--accent-2)" : "1px solid var(--border)",
                    background: active ? "var(--accent-glow)" : "var(--panel)",
                    boxShadow: active
                      ? "inset 0 0 0 1px var(--accent-2), 0 8px 24px -14px var(--accent-glow)"
                      : "none",
                  }}
                >
                  <div className="flex items-center gap-2.5">
                    <BrokerAvatar broker={b} size={34} />
                    <div className="min-w-0">
                      <div className="font-medium text-sm leading-tight">{meta.name}</div>
                      <div className="text-[11px] truncate" style={{ color: "var(--muted)" }}>{meta.tagline}</div>
                    </div>
                  </div>
                  <div className="mt-2.5">
                    <LatencyBadge broker={b} />
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        <div className="hairline" />

        {chosenBroker === "alpaca" && (
          <>
            <div className="flex items-center gap-2.5">
              <BrokerAvatar broker="alpaca" size={32} />
              <h2 className="font-semibold">Connect an Alpaca account</h2>
            </div>
            <p className="text-xs" style={{ color: "var(--muted)" }}>
              From <a href="https://app.alpaca.markets" target="_blank" rel="noreferrer" className="underline" style={{ color: "var(--accent)" }}>app.alpaca.markets</a>:
              {" "}select Paper Trading → click your name → API Keys → Generate.
            </p>
            <form onSubmit={connectAlpaca} className="space-y-3">
              <div>
                <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Label</label>
                <input type="text" className="w-full p-2.5" placeholder="Alpaca Paper" value={label} onChange={e => setLabel(e.target.value)} required />
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>API key ID</label>
                  <input type="text" className="w-full p-2.5 font-mono text-sm" placeholder="PKxxxxxxxxxxxxxxxxxx" value={apiKey} onChange={e => setApiKey(e.target.value)} required />
                </div>
                <div>
                  <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Secret key</label>
                  <input type="password" className="w-full p-2.5 font-mono text-sm" placeholder="(shown once at generation)" value={apiSecret} onChange={e => setApiSecret(e.target.value)} required />
                </div>
              </div>
              <PaperLiveRadio
                value={paper}
                onChange={setPaper}
                name="alpaca-mode"
                note="Live places real-money orders. Paper is recommended for testing."
              />
              <button disabled={busy} className="btn-primary px-4 py-2 text-sm inline-flex items-center gap-2">
                <span>Connect</span>
                {busy && <Spinner />}
              </button>
            </form>
          </>
        )}

        {chosenBroker === "webull" && (
          <>
            <div className="flex items-center gap-2.5">
              <BrokerAvatar broker="webull" size={32} />
              <h2 className="font-semibold">Connect a Webull account</h2>
            </div>
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
                  <input type="text" className="w-full p-2.5" placeholder="Webull Paper" value={label} onChange={e => setLabel(e.target.value)} required />
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Email or phone</label>
                    <input type="text" className="w-full p-2.5" placeholder="you@example.com" value={wbUsername} onChange={e => setWbUsername(e.target.value)} required />
                  </div>
                  <div>
                    <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Password</label>
                    <input type="password" className="w-full p-2.5" value={wbPassword} onChange={e => setWbPassword(e.target.value)} required />
                  </div>
                </div>
                <PaperLiveRadio
                  value={wbPaper}
                  onChange={setWbPaper}
                  name="webull-mode"
                  note="Live places real-money orders. Webull paper and live use separate auth endpoints — switching after MFA is sent invalidates the code."
                />
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
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>MFA code</label>
                    <input type="text" inputMode="numeric" className="w-full p-2.5 font-mono" placeholder="6-digit code" value={wbMfa} onChange={e => setWbMfa(e.target.value)} required />
                  </div>
                  <div>
                    <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Trade PIN</label>
                    <input type="password" inputMode="numeric" className="w-full p-2.5 font-mono" placeholder="6-digit trade PIN" value={wbTradePin} onChange={e => setWbTradePin(e.target.value)} required />
                  </div>
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
            <div className="flex items-center gap-2.5">
              <BrokerAvatar broker="snaptrade" size={32} />
              <h2 className="font-semibold">Connect via SnapTrade</h2>
            </div>
            <p className="text-xs" style={{ color: "var(--muted)" }}>
              SnapTrade is a hosted broker aggregator — pick from Robinhood,
              E*TRADE, Tradier, Schwab, Webull and ~15 others on their
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
                  className="w-full p-2.5"
                  placeholder="Webull via SnapTrade"
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
                  className="w-full p-2.5 font-mono text-sm"
                  placeholder="WEBULL, ETRADE, TRADIER, … (leave blank to pick on portal)"
                  value={stBrokerSlug}
                  onChange={e => setStBrokerSlug(e.target.value.toUpperCase())}
                />
                <p className="text-[10px] mt-1" style={{ color: "var(--muted)" }}>
                  SnapTrade broker slug. Skip this to see the full picker on the portal.
                </p>
              </div>
              <PaperLiveRadio
                value={stPaper}
                onChange={setStPaper}
                name="snaptrade-mode"
                note="Informational only — SnapTrade routes by the account you sign into on the portal."
              />
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

      {/* Disconnect confirmation — replaces the native browser confirm(). */}
      <ConfirmModal
        open={disconnectId !== null}
        title="Disconnect broker?"
        message={(() => {
          const acct = accounts.find(a => a.id === disconnectId);
          const name = acct ? (BROKER_META[acct.broker]?.name ?? acct.broker) : "this broker";
          return (
            <>
              This disconnects <strong>{acct?.label ?? name}</strong> and stops
              mirroring its trades. Your order history is kept — you can reconnect
              anytime.
            </>
          );
        })()}
        confirmLabel="Disconnect"
        cancelLabel="Cancel"
        variant="danger"
        busy={disconnectBusy}
        onConfirm={confirmDisconnect}
        onCancel={() => { if (!disconnectBusy) setDisconnectId(null); }}
      />
    </div>
  );
}
