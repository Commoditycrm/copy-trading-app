"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";
import type { RetryInterval, SubscriberSettings, TraderSettings, User } from "@/lib/types";

const RETRY_OPTIONS: { value: RetryInterval; label: string }[] = [
  { value: "never", label: "Never (REJECT immediately)" },
  { value: "1m",    label: "Retry after 1 minute" },
  { value: "2m",    label: "Retry after 2 minutes" },
  { value: "3m",    label: "Retry after 3 minutes" },
  { value: "5m",    label: "Retry after 5 minutes" },
];

// ── Shared helpers ────────────────────────────────────────────────────────────

/** Format a Decimal-like string as USD ("$1,234.56"). */
function fmtUSD(v: string | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return v;
  return n.toLocaleString(undefined, { style: "currency", currency: "USD" });
}

/** Drop trailing zeros: "1.300" → "1.3", "1.000" → "1". */
function fmtNum(v: string): string {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n.toString() : v;
}

/** Given a percentage string and an equity string, compute the dollar equivalent. */
function pctToDollar(pct: string | null | undefined, equity: string | null | undefined): string {
  if (!pct || !equity) return "";
  const p = Number(pct);
  const e = Number(equity);
  if (!Number.isFinite(p) || !Number.isFinite(e)) return "";
  return fmtUSD(String((e * p) / 100));
}

// ── Risk limit section — shared chrome ───────────────────────────────────────
function RiskSection({
  title,
  description,
  hint,
  pctValue,
  onPctChange,
  onSave,
  onClear,
  busy,
  unchanged,
  equity,
  progressPct,
  progressColor,
  children,
}: {
  title: string;
  description: string;
  hint?: string;
  pctValue: string;
  onPctChange: (v: string) => void;
  onSave: () => void;
  onClear: () => void;
  busy: boolean;
  unchanged: boolean;
  equity: string | null | undefined;
  progressPct?: number;
  progressColor?: string;
  children?: React.ReactNode;
}) {
  const dollarEq = pctToDollar(pctValue, equity);
  return (
    <section className="p-4 rounded border space-y-3" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
      <div>
        <h2 className="font-medium">{title}</h2>
        <p className="text-sm mt-0.5" style={{ color: "var(--muted)" }}>{description}</p>
      </div>

      {children}

      {progressPct !== undefined && progressPct > 0 && (
        <div className="h-1 rounded overflow-hidden" style={{ background: "var(--border)" }}>
          <div
            style={{
              width: `${Math.min(100, progressPct)}%`,
              height: "100%",
              background: progressColor ?? (progressPct >= 100 ? "var(--bad)" : progressPct >= 75 ? "#f59e0b" : "var(--good)"),
              transition: "width 0.3s ease",
            }}
          />
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative">
          <input
            type="number" step="0.1" min="0" max="100"
            placeholder="(disabled)"
            className="w-36 p-2 rounded bg-transparent border pr-7"
            style={{ borderColor: "var(--border)" }}
            value={pctValue}
            onChange={(e) => onPctChange(e.target.value)}
          />
          <span className="absolute right-2 top-1/2 -translate-y-1/2 text-sm pointer-events-none" style={{ color: "var(--muted)" }}>%</span>
        </div>
        <button
          onClick={onSave}
          disabled={busy || unchanged}
          className="px-4 py-2 rounded font-medium inline-flex items-center gap-2"
          style={{ background: "var(--accent)", color: "#06121f", opacity: (busy || unchanged) ? 0.5 : 1 }}
        >
          <span>Save</span>
          {busy && <Spinner />}
        </button>
        {pctValue !== "" && (
          <button
            onClick={onClear}
            className="px-3 py-2 text-sm rounded border"
            style={{ borderColor: "var(--border)", color: "var(--muted)" }}
            title="Clear this limit"
          >
            Clear
          </button>
        )}
        {dollarEq && (
          <span className="text-xs" style={{ color: "var(--muted)" }}>
            ≈ {dollarEq} of your account
          </span>
        )}
      </div>

      {!equity && (
        <p className="text-xs" style={{ color: "#f59e0b" }}>
          ⚠ No broker account balance found — connect your broker first for this limit to take effect.
        </p>
      )}
    </section>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────
export default function SettingsPage() {
  const [user, setUser]   = useState<User | null>(null);
  const [sub, setSub]     = useState<SubscriberSettings | null>(null);
  const [trd, setTrd]     = useState<TraderSettings | null>(null);
  const [traders, setTraders] = useState<{ id: string; display_name: string | null; email: string }[]>([]);

  // Multiplier
  const [multInput, setMultInput] = useState("");
  const [multBusy, setMultBusy]   = useState(false);

  // Daily loss limit % (new)
  const [dailyPctInput, setDailyPctInput] = useState("");
  const [dailyPctBusy, setDailyPctBusy]   = useState(false);

  // Per-trade loss limit %
  const [tradePctInput, setTradePctInput] = useState("");
  const [tradePctBusy, setTradePctBusy]   = useState(false);

  // Max drawdown %
  const [drawdownInput, setDrawdownInput] = useState("");
  const [drawdownBusy, setDrawdownBusy]   = useState(false);

  useEffect(() => {
    (async () => {
      const u = await api<User>("/api/auth/me");
      setUser(u);
      if (u.role === "subscriber") {
        const s = await api<SubscriberSettings>("/api/settings/subscriber");
        applySettings(s);
        setTraders(await api("/api/settings/traders"));
      } else {
        setTrd(await api<TraderSettings>("/api/settings/trader"));
      }
    })().catch(e => notify.fromError(e, "Could not load settings"));
  }, []);

  function applySettings(s: SubscriberSettings) {
    setSub(s);
    setMultInput(parseFloat(s.multiplier).toString());
    setDailyPctInput(s.daily_loss_limit_pct ?? "");
    setTradePctInput(s.per_trade_loss_limit_pct ?? "");
    setDrawdownInput(s.max_drawdown_pct ?? "");
  }

  // SSE: auto-pause events from the backend
  useEventStream((evt) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const e = evt as any;
    if (e?.type === "copy.auto_paused") {
      const messages: Record<string, string> = {
        daily_loss_limit:     `Copy paused — today's loss hit your daily dollar limit.`,
        daily_loss_limit_pct: `Copy paused — today's loss (${fmtUSD(e.todays_realized_pnl)}) exceeded your daily ${e.daily_loss_limit_pct}% limit (${fmtUSD(e.dollar_limit)}).`,
        per_trade_loss_limit: `Copy paused — last trade lost ${fmtUSD(e.last_trade_pnl)}, exceeding your ${e.per_trade_loss_limit_pct}% per-trade limit.`,
        max_drawdown:         `Copy paused — account dropped to ${fmtUSD(e.current_equity)}, exceeding your ${e.max_drawdown_pct}% max drawdown.`,
      };
      notify.error(messages[e.reason] ?? "Copy trading auto-paused.", { autoClose: false });
      api<SubscriberSettings>("/api/settings/subscriber").then(applySettings);
    }
  });

  async function follow(traderId: string | null) {
    const s = await api<SubscriberSettings>("/api/settings/subscriber/follow", {
      method: "PATCH", body: JSON.stringify({ trader_id: traderId }),
    });
    applySettings(s);
  }

  async function saveMultiplier() {
    setMultBusy(true);
    try {
      const n = Number(multInput);
      if (!Number.isFinite(n) || n <= 0 || n > 10)
        throw new ApiError(422, "Multiplier must be between 0.1 and 10");
      const rounded = (Math.round(n * 10) / 10).toFixed(1);
      const s = await api<SubscriberSettings>("/api/settings/subscriber/multiplier", {
        method: "PATCH", body: JSON.stringify({ multiplier: rounded }),
      });
      applySettings(s);
      notify.success(`Multiplier set to ×${parseFloat(s.multiplier)}`);
    } catch (e) {
      notify.fromError(e, "Could not update multiplier");
    } finally {
      setMultBusy(false);
    }
  }

  async function saveDailyPct() {
    setDailyPctBusy(true);
    try {
      const trimmed = dailyPctInput.trim();
      const s = await api<SubscriberSettings>("/api/settings/subscriber/daily-loss-limit-pct", {
        method: "PATCH",
        body: JSON.stringify({ daily_loss_limit_pct: trimmed === "" ? null : trimmed }),
      });
      applySettings(s);
      notify.success(s.daily_loss_limit_pct
        ? `Daily loss limit set to ${parseFloat(s.daily_loss_limit_pct)}% of account`
        : "Daily loss limit cleared");
    } catch (e) {
      notify.fromError(e, "Could not update daily loss limit");
    } finally {
      setDailyPctBusy(false);
    }
  }

  async function saveTradePct() {
    setTradePctBusy(true);
    try {
      const trimmed = tradePctInput.trim();
      const s = await api<SubscriberSettings>("/api/settings/subscriber/per-trade-loss-limit", {
        method: "PATCH",
        body: JSON.stringify({ per_trade_loss_limit_pct: trimmed === "" ? null : trimmed }),
      });
      applySettings(s);
      notify.success(s.per_trade_loss_limit_pct
        ? `Per-trade loss limit set to ${parseFloat(s.per_trade_loss_limit_pct)}%`
        : "Per-trade loss limit cleared");
    } catch (e) {
      notify.fromError(e, "Could not update per-trade loss limit");
    } finally {
      setTradePctBusy(false);
    }
  }

  async function saveDrawdown() {
    setDrawdownBusy(true);
    try {
      const trimmed = drawdownInput.trim();
      const s = await api<SubscriberSettings>("/api/settings/subscriber/max-drawdown", {
        method: "PATCH",
        body: JSON.stringify({ max_drawdown_pct: trimmed === "" ? null : trimmed }),
      });
      applySettings(s);
      notify.success(s.max_drawdown_pct
        ? `Max drawdown set to ${parseFloat(s.max_drawdown_pct)}% — baseline captured at ${fmtUSD(s.max_drawdown_equity_baseline)}`
        : "Max drawdown protection disabled");
    } catch (e) {
      notify.fromError(e, "Could not update max drawdown");
    } finally {
      setDrawdownBusy(false);
    }
  }

  async function setRetryInterval(direction: "open" | "close", value: RetryInterval) {
    try {
      const body = direction === "open"
        ? { retry_interval_open: value }
        : { retry_interval_close: value };
      const s = await api<SubscriberSettings>("/api/settings/subscriber/retry-interval", {
        method: "PATCH", body: JSON.stringify(body),
      });
      applySettings(s);
      const verb = direction === "open" ? "opening" : "closing";
      notify.success(
        value === "never"
          ? `Retry for ${verb} positions disabled`
          : `Retry for ${verb} positions: ${RETRY_OPTIONS.find(o => o.value === value)?.label}`
      );
    } catch (e) {
      notify.fromError(e, "Could not update retry interval");
    }
  }

  async function toggleTrading(next: boolean) {
    setTrd(await api<TraderSettings>("/api/settings/trader", {
      method: "PATCH", body: JSON.stringify({ trading_enabled: next }),
    }));
  }

  if (!user) return <p style={{ color: "var(--muted)" }}>Loading…</p>;

  // ── Computed display values ────────────────────────────────────────────────
  const todaysPnL   = sub ? Number(sub.todays_realized_pnl ?? "0") : 0;
  const equity      = sub?.account_equity;

  // Daily loss % progress bar
  const dailyLimitDollar = (sub?.daily_loss_limit_pct && equity)
    ? Number(equity) * Number(sub.daily_loss_limit_pct) / 100 : null;
  const dailyHeadroom = dailyLimitDollar !== null ? dailyLimitDollar + todaysPnL : null;
  const dailyProgress = dailyLimitDollar ? Math.min(100, (-todaysPnL / dailyLimitDollar) * 100) : 0;

  // Max drawdown progress bar
  const drawdownBaseline = sub?.max_drawdown_equity_baseline ? Number(sub.max_drawdown_equity_baseline) : null;
  const drawdownPct = sub?.max_drawdown_pct ? Number(sub.max_drawdown_pct) : null;
  const drawdownMin = (drawdownBaseline && drawdownPct) ? drawdownBaseline * (1 - drawdownPct / 100) : null;
  const currentEquity = equity ? Number(equity) : null;
  const drawdownProgress = (drawdownBaseline && drawdownMin !== null && currentEquity !== null)
    ? Math.min(100, Math.max(0, ((drawdownBaseline - currentEquity) / (drawdownBaseline - drawdownMin)) * 100))
    : 0;

  return (
    <div className="space-y-6 max-w-2xl">
      <h1 className="text-2xl font-semibold">Settings</h1>

      {/* ── Account equity pill ─────────────────────────────────────────── */}
      {user.role === "subscriber" && equity && (
        <div
          className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium"
          style={{ background: "rgba(34,197,94,0.1)", color: "#22c55e", border: "1px solid rgba(34,197,94,0.2)" }}
        >
          <span>Account equity</span>
          <span className="font-bold">{fmtUSD(equity)}</span>
        </div>
      )}

      {user.role === "subscriber" && sub && (
        <>
          {/* Following trader */}
          <section className="p-4 rounded border space-y-3" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
            <h2 className="font-medium">Following trader</h2>
            <select
              value={sub.following_trader_id ?? ""}
              onChange={e => follow(e.target.value || null)}
              className="w-full p-2 rounded bg-transparent border"
              style={{ borderColor: "var(--border)" }}
            >
              <option value="">— not following anyone —</option>
              {traders.map(t => (
                <option key={t.id} value={t.id}>{t.display_name ?? t.email}</option>
              ))}
            </select>
          </section>

          {/* Trade multiplier */}
          <section className="p-4 rounded border space-y-3" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
            <h2 className="font-medium">Trade multiplier</h2>
            <p className="text-sm" style={{ color: "var(--muted)" }}>
              Each mirrored order will be sized at trader_qty × this multiplier. Default is 1. Max 10.
            </p>
            <div className="flex items-center gap-2">
              <input
                type="number" step="0.1" min="0.1" max="10"
                className="w-32 p-2 rounded bg-transparent border" style={{ borderColor: "var(--border)" }}
                value={multInput}
                onChange={(e) => setMultInput(e.target.value)}
              />
              <button
                onClick={saveMultiplier}
                disabled={multBusy || parseFloat(multInput) === parseFloat(sub.multiplier)}
                className="px-4 py-2 rounded font-medium inline-flex items-center gap-2"
                style={{ background: "var(--accent)", color: "#06121f" }}
              >
                <span>Save</span>
                {multBusy && <Spinner />}
              </button>
              {parseFloat(multInput) !== parseFloat(sub.multiplier) && (
                <button
                  onClick={() => setMultInput(parseFloat(sub.multiplier).toString())}
                  className="px-3 py-2 text-sm rounded border"
                  style={{ borderColor: "var(--border)", color: "var(--muted)" }}
                >
                  Reset
                </button>
              )}
              <span className="text-sm ml-2" style={{ color: "var(--muted)" }}>
                current: ×{fmtNum(sub.multiplier)}
              </span>
            </div>
          </section>

          {/* ── Risk Controls heading ──────────────────────────────────────── */}
          <div className="pt-2">
            <h2 className="text-base font-semibold">Risk Controls</h2>
            <p className="text-sm mt-0.5" style={{ color: "var(--muted)" }}>
              Set any of these to auto-pause copy trading when a loss threshold is hit.
              All limits are calculated as a percentage of your current account equity.
              Leave blank to disable.
            </p>
          </div>

          {/* Daily Loss Limit % */}
          <RiskSection
            title="Daily Loss Limit"
            description="If today's total realized losses exceed this % of your account, copy trading pauses automatically."
            equity={equity}
            pctValue={dailyPctInput}
            onPctChange={setDailyPctInput}
            onSave={saveDailyPct}
            onClear={() => setDailyPctInput("")}
            busy={dailyPctBusy}
            unchanged={dailyPctInput === (sub.daily_loss_limit_pct ?? "")}
            progressPct={dailyProgress}
          >
            {/* Today's P&L + headroom */}
            <div className="grid grid-cols-3 gap-4 p-3 rounded" style={{ background: "rgba(255,255,255,0.02)" }}>
              <div>
                <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Today&rsquo;s P&amp;L</div>
                <div className="text-sm font-medium mt-0.5" style={{ color: todaysPnL >= 0 ? "var(--good)" : "var(--bad)" }}>
                  {fmtUSD(sub.todays_realized_pnl)}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Limit</div>
                <div className="text-sm font-medium mt-0.5">
                  {dailyLimitDollar !== null ? fmtUSD(String(dailyLimitDollar)) : "—"}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Headroom</div>
                <div className="text-sm font-medium mt-0.5" style={{ color: (dailyHeadroom ?? 1) > 0 ? "var(--text)" : "var(--bad)" }}>
                  {dailyHeadroom !== null ? fmtUSD(String(dailyHeadroom)) : "—"}
                </div>
              </div>
            </div>
          </RiskSection>

          {/* Per-Trade Loss Limit % */}
          <RiskSection
            title="Per-Trade Loss Limit"
            description="If any single copied trade results in a loss greater than this % of your account, copy trading pauses. Checked at the start of each new fanout."
            equity={equity}
            pctValue={tradePctInput}
            onPctChange={setTradePctInput}
            onSave={saveTradePct}
            onClear={() => setTradePctInput("")}
            busy={tradePctBusy}
            unchanged={tradePctInput === (sub.per_trade_loss_limit_pct ?? "")}
          />

          {/* Max Drawdown % */}
          <RiskSection
            title="Max Drawdown Protection"
            description="If your account equity drops more than this % below the baseline (captured when you save this setting), copy trading pauses."
            equity={equity}
            pctValue={drawdownInput}
            onPctChange={setDrawdownInput}
            onSave={saveDrawdown}
            onClear={() => setDrawdownInput("")}
            busy={drawdownBusy}
            unchanged={drawdownInput === (sub.max_drawdown_pct ?? "")}
            progressPct={drawdownProgress}
            progressColor={drawdownProgress >= 100 ? "var(--bad)" : drawdownProgress >= 75 ? "#f59e0b" : "var(--good)"}
          >
            {sub.max_drawdown_equity_baseline && (
              <div className="grid grid-cols-3 gap-4 p-3 rounded" style={{ background: "rgba(255,255,255,0.02)" }}>
                <div>
                  <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Baseline</div>
                  <div className="text-sm font-medium mt-0.5">{fmtUSD(sub.max_drawdown_equity_baseline)}</div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Current Equity</div>
                  <div className="text-sm font-medium mt-0.5" style={{ color: "var(--good)" }}>{fmtUSD(equity)}</div>
                </div>
                <div>
                  <div className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>Pause Threshold</div>
                  <div className="text-sm font-medium mt-0.5" style={{ color: "var(--bad)" }}>
                    {drawdownMin !== null ? fmtUSD(String(drawdownMin.toFixed(2))) : "—"}
                  </div>
                </div>
              </div>
            )}
          </RiskSection>

          {/* Retry mirror orders on broker errors */}
          <section className="p-4 rounded border space-y-4" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
            <div>
              <h2 className="font-medium">Retry mirror orders on broker errors</h2>
              <p className="text-sm" style={{ color: "var(--muted)" }}>
                If your broker is unreachable when a mirror order is placed (network blip,
                5xx error, rate limit), the platform can wait and try once more. Set &ldquo;Never&rdquo;
                to keep the old behaviour (immediately reject). User-fixable errors
                (insufficient buying power, expired option, etc.) never retry regardless &mdash;
                they&rsquo;d just fail the same way next time.
              </p>
              <p className="text-xs mt-2" style={{ color: "var(--muted)" }}>
                If the retry also fails, you&rsquo;ll get a notification in your inbox.
              </p>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="space-y-2">
                <label className="text-sm font-medium block">Opening positions</label>
                <select
                  className="w-full p-2 rounded border bg-transparent"
                  style={{ borderColor: "var(--border)" }}
                  value={sub.retry_interval_open}
                  onChange={(e) => setRetryInterval("open", e.target.value as RetryInterval)}
                >
                  {RETRY_OPTIONS.map(opt => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
                <p className="text-xs" style={{ color: "var(--muted)" }}>
                  Applies to new positions the trader opens (BUY mirrors).
                </p>
              </div>
              <div className="space-y-2">
                <label className="text-sm font-medium block">Closing positions</label>
                <select
                  className="w-full p-2 rounded border bg-transparent"
                  style={{ borderColor: "var(--border)" }}
                  value={sub.retry_interval_close}
                  onChange={(e) => setRetryInterval("close", e.target.value as RetryInterval)}
                >
                  {RETRY_OPTIONS.map(opt => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
                <p className="text-xs" style={{ color: "var(--muted)" }}>
                  Applies to exit / close-position orders. Late closes can affect P&amp;L &mdash;
                  consider a shorter interval here than for opens.
                </p>
              </div>
            </div>
          </section>
        </>
      )}

      {/* Trader view */}
      {user.role === "trader" && trd && (
        <section className="p-4 rounded border space-y-3" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
          <div className="flex items-center justify-between">
            <div>
              <h2 className="font-medium">Master trading switch</h2>
              <p className="text-sm" style={{ color: "var(--muted)" }}>
                When OFF, the platform refuses to place new orders (yours and any subscriber mirrors).
              </p>
            </div>
            <button
              onClick={() => toggleTrading(!trd.trading_enabled)}
              className="px-4 py-2 rounded font-medium"
              style={{
                background: trd.trading_enabled ? "var(--good)" : "var(--border)",
                color: trd.trading_enabled ? "#06121f" : "var(--text)",
              }}
            >
              {trd.trading_enabled ? "ON" : "OFF"}
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
