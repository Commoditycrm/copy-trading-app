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
  alpaca:    { name: "Alpaca",    tagline: "Direct API keys",                    latency: "Realtime", latencyTone: "good", accent: "#f5a623" },
  // Direct Webull integration removed — Webull is connected via SnapTrade.
  // Kept in the map so old broker_account rows with broker="webull" still
  // render with a valid avatar/name in the connected-list, but the picker
  // no longer surfaces it as a connect option.
  webull:    { name: "Webull",    tagline: "(via SnapTrade)",                    latency: "5–60s",    latencyTone: "warn", accent: "#3b82f6" },
  snaptrade: { name: "SnapTrade", tagline: "20+ brokers · hosted",               latency: "5–60s",    latencyTone: "warn", accent: "#14b8a6" },
  ibkr:      { name: "IBKR",      tagline: "Interactive Brokers · direct OAuth", latency: "~2–5s",    latencyTone: "good", accent: "#d04a02" },
};

// Picker order — "webull" excluded; subscribers should click SnapTrade and
// pick Webull from inside the SnapTrade portal.
const BROKER_ORDER: BrokerName[] = ["alpaca", "snaptrade", "ibkr"];

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

/** Listener-gating checkboxes for one connected broker. Controlled by the
 *  parent (BrokersPage) — each toggle calls onChange with a partial patch
 *  that the parent applies optimistically + PATCHes to
 *  /api/brokers/{id}/settings. The three flags are independent:
 *    - Auto Pull Orders   = master switch (listener on/off)
 *    - Bring open orders  = include non-FILLED statuses
 *    - Bring Filled orders = include FILLED status
 *  When Auto Pull is off, the two child filters are visually dimmed
 *  because they're effectively no-ops until the master is re-enabled. */
type GatingPatch = Partial<{
  auto_pull_orders: boolean;
  bring_open_orders: boolean;
  bring_filled_orders: boolean;
}>;

function AutoPullOrders({ acct, onChange, disabled }: {
  acct: BrokerAccount;
  onChange: (patch: GatingPatch) => void;
  disabled?: boolean;
}) {
  const childDim = !acct.auto_pull_orders;
  const checkboxStyle = { accentColor: "var(--accent)" } as const;
  return (
    <div className="mt-4 pt-4 hairline flex items-center flex-wrap gap-x-5 gap-y-2 text-sm">
      <label className="flex items-center gap-2 font-medium cursor-pointer select-none">
        <input
          type="checkbox"
          className="h-3.5 w-3.5 cursor-pointer"
          style={checkboxStyle}
          checked={acct.auto_pull_orders}
          disabled={disabled}
          onChange={(e) => onChange({ auto_pull_orders: e.target.checked })}
        />
        <span>Auto Pull Orders</span>
      </label>
      <span aria-hidden className="h-4 w-px" style={{ background: "var(--border)" }} />
      <label
        className="flex items-center gap-2 cursor-pointer select-none"
        style={{ color: "var(--text-2)", opacity: childDim ? 0.5 : 1 }}
        title={childDim ? "Enable Auto Pull Orders to use this filter." : undefined}
      >
        <input
          type="checkbox"
          className="h-3.5 w-3.5 cursor-pointer"
          style={checkboxStyle}
          checked={acct.bring_open_orders}
          disabled={disabled}
          onChange={(e) => onChange({ bring_open_orders: e.target.checked })}
        />
        <span>Bring open orders</span>
      </label>
      <label
        className="flex items-center gap-2 cursor-pointer select-none"
        style={{ color: "var(--text-2)", opacity: childDim ? 0.5 : 1 }}
        title={childDim ? "Enable Auto Pull Orders to use this filter." : undefined}
      >
        <input
          type="checkbox"
          className="h-3.5 w-3.5 cursor-pointer"
          style={checkboxStyle}
          checked={acct.bring_filled_orders}
          disabled={disabled}
          onChange={(e) => onChange({ bring_filled_orders: e.target.checked })}
        />
        <span>Bring Filled orders</span>
      </label>
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

  // Direct Webull state removed — connect Webull via SnapTrade below.

  // SnapTrade form state — much smaller because the actual auth happens
  // on SnapTrade's hosted portal. We collect a label, optionally a
  // pre-selected broker slug (left blank lets the user pick on the portal),
  // and a paper-trading hint that's mostly informational (most SnapTrade
  // brokers don't expose a 'paper' flag; we store it for our own UI).
  const [stLabel, setStLabel] = useState("");
  const [stBrokerSlug, setStBrokerSlug] = useState("");
  const [stPaper, setStPaper] = useState(false);

  // IBKR direct-OAuth form state. Each field maps 1:1 to the backend's
  // IbkrCredentialsIn schema. Long fields (signing_key, access_token_secret)
  // use textareas because IBKR keys can run to several hundred characters
  // for RSA-style OAuth configurations.
  const [ibkrLabel, setIbkrLabel] = useState("");
  const [ibkrAccountId, setIbkrAccountId] = useState("");
  const [ibkrConsumerKey, setIbkrConsumerKey] = useState("");
  const [ibkrSigningKey, setIbkrSigningKey] = useState("");
  const [ibkrAccessToken, setIbkrAccessToken] = useState("");
  const [ibkrAccessTokenSecret, setIbkrAccessTokenSecret] = useState("");
  const [ibkrPaper, setIbkrPaper] = useState(false);

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
    setStLabel(""); setStBrokerSlug(""); setStPaper(false);
    setIbkrLabel(""); setIbkrAccountId(""); setIbkrConsumerKey("");
    setIbkrSigningKey(""); setIbkrAccessToken(""); setIbkrAccessTokenSecret("");
    setIbkrPaper(false);
  }

  async function connectIbkr(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/api/brokers", {
        method: "POST",
        body: JSON.stringify({
          broker: "ibkr",
          label: ibkrLabel.trim(),
          ibkr: {
            consumer_key:        ibkrConsumerKey.trim(),
            signing_key:         ibkrSigningKey.trim(),
            access_token:        ibkrAccessToken.trim(),
            access_token_secret: ibkrAccessTokenSecret.trim(),
            account_id:          ibkrAccountId.trim(),
            paper:               ibkrPaper,
          },
        }),
      });
      await load();
      resetConnectForms();
      notify.success("IBKR connected");
    } catch (e) {
      notify.fromError(e, "IBKR connect failed");
    } finally {
      setBusy(false);
    }
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

  // Direct Webull connect handlers removed — see SnapTrade flow below.

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

  /** Optimistic update + PATCH for the listener-gating checkboxes. We flip
   *  the local state first so the checkbox feels instant, then PATCH;
   *  on failure we revert and toast so the UI doesn't lie. */
  async function patchSettings(id: string, patch: GatingPatch) {
    const prev = accounts.find(a => a.id === id);
    if (!prev) return;
    setAccounts(cur => cur.map(a => (a.id === id ? { ...a, ...patch } : a)));
    try {
      const updated = await api<BrokerAccount>(`/api/brokers/${id}/settings`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      setAccounts(cur => cur.map(a => (a.id === id ? updated : a)));
    } catch (e) {
      // Revert to the pre-toggle snapshot.
      setAccounts(cur => cur.map(a => (a.id === id ? prev : a)));
      notify.fromError(e, "Could not update broker settings");
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
    <div className="space-y-6 max-w-[760px]">
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

            <AutoPullOrders acct={a} onChange={(patch) => patchSettings(a.id, patch)} />
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
          {/* 3 columns once the screen is wide enough so the three remaining
              brokers (Alpaca / SnapTrade / IBKR) fill the row evenly — no
              empty 4th slot now that direct Webull is gone. */}
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

        {/* Direct Webull connect form removed — users now connect Webull
            through SnapTrade. The broker picker no longer offers Webull
            as a standalone option (see BROKER_ORDER). */}

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

        {chosenBroker === "ibkr" && (
          <>
            <div className="flex items-center gap-2.5">
              <BrokerAvatar broker="ibkr" size={32} />
              <h2 className="font-semibold">Connect Interactive Brokers</h2>
            </div>
            <p className="text-xs" style={{ color: "var(--muted)" }}>
              Direct IBKR integration via their OAuth 1.0a Web API — no
              aggregator, no local gateway. Generate the four OAuth values
              in your IBKR Client Portal under{" "}
              <strong>Settings → API → OAuth</strong> (self-service
              consumer registration) and paste them here together with
              your IBKR account number. Mirror latency is typically 2–5s.
            </p>
            <form onSubmit={connectIbkr} className="space-y-3">
              <div>
                <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Label</label>
                <input
                  type="text"
                  className="w-full p-2.5"
                  placeholder="My IBKR account"
                  value={ibkrLabel}
                  onChange={e => setIbkrLabel(e.target.value)}
                  required
                />
              </div>
              <div>
                <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Account ID</label>
                <input
                  type="text"
                  className="w-full p-2.5 font-mono text-sm"
                  placeholder="U1234567"
                  value={ibkrAccountId}
                  onChange={e => setIbkrAccountId(e.target.value)}
                  required
                />
              </div>
              <div>
                <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Consumer key</label>
                <input
                  type="text"
                  className="w-full p-2.5 font-mono text-sm"
                  value={ibkrConsumerKey}
                  onChange={e => setIbkrConsumerKey(e.target.value)}
                  required
                />
              </div>
              <div>
                <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Signing key</label>
                <textarea
                  className="w-full p-2.5 font-mono text-xs"
                  rows={3}
                  placeholder="Paste your consumer signing key (long base64 / PEM)"
                  value={ibkrSigningKey}
                  onChange={e => setIbkrSigningKey(e.target.value)}
                  required
                />
              </div>
              <div>
                <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Access token</label>
                <input
                  type="text"
                  className="w-full p-2.5 font-mono text-sm"
                  value={ibkrAccessToken}
                  onChange={e => setIbkrAccessToken(e.target.value)}
                  required
                />
              </div>
              <div>
                <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Access token secret</label>
                <textarea
                  className="w-full p-2.5 font-mono text-xs"
                  rows={3}
                  value={ibkrAccessTokenSecret}
                  onChange={e => setIbkrAccessTokenSecret(e.target.value)}
                  required
                />
              </div>
              <PaperLiveRadio
                value={ibkrPaper}
                onChange={setIbkrPaper}
                name="ibkr-mode"
                note="Set to match the IBKR account behind this access token (paper vs live)."
              />
              <button disabled={busy} className="btn-primary px-4 py-2 text-sm inline-flex items-center gap-2">
                <span>Connect IBKR</span>
                {busy && <Spinner />}
              </button>
              <p className="text-[10px]" style={{ color: "var(--muted)" }}>
                We verify the credentials with a live IBKR call before
                saving. Stored Fernet-encrypted at rest; never logged.
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
