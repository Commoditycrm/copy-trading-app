"use client";

import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";
import { PageLoading } from "@/components/PageLoading";
import type { RetryInterval, SubscriberSettings, TraderSettings, User } from "@/lib/types";

const RETRY_OPTIONS: { value: RetryInterval; label: string }[] = [
  { value: "never", label: "Never (REJECT)" },
  { value: "1m", label: "After 1 min" },
  { value: "2m", label: "After 2 min" },
  { value: "3m", label: "After 3 min" },
  { value: "5m", label: "After 5 min" },
];

const RETRY_COUNT_OPTIONS = [1, 2, 3, 4, 5].map(n => ({
  value: String(n),
  label: n === 1 ? "1 retry" : `${n} retries`,
}));

/** Cross-navigation cache for the pnl.tick payload fields the Risk
 *  Controls panel renders.
 *
 *  Problem this solves: the panel's live numbers come in on a
 *  ~10-second SSE tick. The numbers were stored as useState on the
 *  SettingsPage component, so navigating away and back unmounted the
 *  page and reset everything to null — the user saw "—" / "0" for up
 *  to 10s every time they re-opened Settings.
 *
 *  Now: every tick writes the latest values both to a module-level
 *  variable (survives client-side navigation since the module stays
 *  loaded) and to sessionStorage (survives hard refresh of the page,
 *  but clears when the tab closes — so a stale cache from yesterday
 *  can't mislead today).
 *
 *  Stored fields are the pure transient-display ones; nothing that
 *  affects business logic (limits etc.) is cached here. */
const TICK_CACHE_KEY = "trading-app:pnl-tick-cache";

interface TickCache {
  beginning_day_balance: string | null;
  todays_trading_value: string | null;
}

const EMPTY_TICK_CACHE: TickCache = {
  beginning_day_balance: null,
  todays_trading_value: null,
};

function readTickCache(): TickCache {
  // SSR guard — Next.js renders this module on the server during the
  // initial RSC pass, where `sessionStorage` doesn't exist.
  if (typeof window === "undefined") return EMPTY_TICK_CACHE;
  try {
    const raw = window.sessionStorage.getItem(TICK_CACHE_KEY);
    if (!raw) return EMPTY_TICK_CACHE;
    const parsed = JSON.parse(raw);
    return {
      beginning_day_balance: typeof parsed.beginning_day_balance === "string" ? parsed.beginning_day_balance : null,
      todays_trading_value: typeof parsed.todays_trading_value === "string" ? parsed.todays_trading_value : null,
    };
  } catch {
    return EMPTY_TICK_CACHE;
  }
}

function writeTickCache(patch: Partial<TickCache>) {
  if (typeof window === "undefined") return;
  try {
    const current = readTickCache();
    const next = { ...current, ...patch };
    window.sessionStorage.setItem(TICK_CACHE_KEY, JSON.stringify(next));
  } catch { /* quota / disabled storage — silent */ }
}

export default function SettingsPage() {
  const [user, setUser] = useState<User | null>(null);
  const [sub, setSub] = useState<SubscriberSettings | null>(null);
  const [trd, setTrd] = useState<TraderSettings | null>(null);
  const [traders, setTraders] = useState<{ id: string; display_name: string | null; email: string }[]>([]);
  const [multInput, setMultInput] = useState("");
  const [multBusy, setMultBusy] = useState(false);
  const [limitInput, setLimitInput] = useState("");
  const [limitBusy, setLimitBusy] = useState(false);
  const [profitInput, setProfitInput] = useState("");
  const [profitBusy, setProfitBusy] = useState(false);
  // Max-per-contract is UI-only — no Today/Headroom readouts. Persisted
  // so the value survives refresh.
  const [maxContractInput, setMaxContractInput] = useState("");
  const [maxContractBusy, setMaxContractBusy] = useState(false);
  // Max-account-pct-per-day is enforced server-side by pnl_poller.
  // `equity` comes in on every pnl.tick event so the panel can render
  // the dynamic dollar threshold (equity × pct/100). Null until the
  // first tick lands — typically within 60s of page load.
  const [maxPctInput, setMaxPctInput] = useState("");
  const [maxPctBusy, setMaxPctBusy] = useState(false);
  // Auto-liquidation floor — when broker-reported equity drops to or
  // below this dollar value, pnl_poller flattens the account at market
  // and disables copy until the subscriber manually re-enables.
  const [autoLiqInput, setAutoLiqInput] = useState("");
  const [autoLiqBusy, setAutoLiqBusy] = useState(false);
  // Per-position TP/SL — when any open position's unrealized P&L %
  // breaches one of these thresholds, that position is closed at
  // market by pnl_poller. Per-position only: does NOT pause copy.
  const [posTpInput, setPosTpInput] = useState("");
  const [posTpBusy, setPosTpBusy] = useState(false);
  const [posSlInput, setPosSlInput] = useState("");
  const [posSlBusy, setPosSlBusy] = useState(false);
  // Today's starting account balance from Alpaca (`last_equity`, =
  // equity at yesterday's close). Hydrated from sessionStorage so
  // navigating away and back keeps the last value visible while the
  // next pnl.tick is in flight, instead of flashing "—" for 10s.
  const [beginningDayBalance, setBeginningDayBalance] = useState<string | null>(
    () => readTickCache().beginning_day_balance,
  );
  // Live broker equity, refreshed by every pnl.tick. Used by the
  // Auto-liquidation (take-profit) row to show how close the unrealized
  // P&L is to the target. Null until the first tick lands.
  const [equity, setEquity] = useState<string | null>(null);
  // Today's UNREALIZED P&L (mark-to-market on still-open positions),
  // computed server-side as todays_total_pl − today_realized_pnl. The
  // take-profit auto-liquidation trigger compares this against
  // `auto_liquidation_limit`. Null when beginning_day_balance isn't
  // available (some SnapTrade brokers).
  const [unrealizedPl, setUnrealizedPl] = useState<string | null>(null);
  // Today's filled-trade notional in USD — same cross-nav cache.
  const [todaysTradingValue, setTodaysTradingValue] = useState<string | null>(
    () => readTickCache().todays_trading_value,
  );

  useEffect(() => {
    (async () => {
      const u = await api<User>("/api/auth/me");
      setUser(u);
      if (u.role === "subscriber") {
        const s = await api<SubscriberSettings>("/api/settings/subscriber");
        setSub(s);
        setMultInput(parseFloat(s.multiplier).toString());
        // The UI now uses the % variants; fall back to legacy USD only
        // if the user hasn't set a pct yet (smooth transition).
        setLimitInput(s.daily_loss_limit_pct ?? "");
        setProfitInput(s.daily_profit_limit_pct ?? "");
        setMaxContractInput(s.max_per_contract ?? "");
        setMaxPctInput(s.max_account_pct_per_day ?? "");
        setAutoLiqInput(s.auto_liquidation_limit ?? "");
        setPosTpInput(s.position_tp_pct ?? "");
        setPosSlInput(s.position_sl_pct ?? "");
        setTraders(await api("/api/settings/traders"));
      } else {
        setTrd(await api<TraderSettings>("/api/settings/trader"));
      }
    })().catch(e => notify.fromError(e, "Could not load settings"));
  }, []);

  useEventStream((evt) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const e = evt as any;
    if (e?.type === "copy.auto_paused") {
      // The same event fires for loss / profit / pct triggers — branch
      // on `reason` so the toast doesn't say "today's loss ... ($null)"
      // when the actual cause was the profit or pct cap.
      const reason = e.reason as string | undefined;
      let msg: string;
      if (reason === "daily_loss_limit_pct") {
        msg = `Copy trading auto-paused — today's loss ($${e.todays_realized_pnl}) hit your ${e.daily_loss_limit_pct}% daily loss limit (≈ $${e.loss_pct_dollars} of your day-start balance).`;
      } else if (reason === "daily_profit_limit_pct") {
        msg = `Copy trading auto-paused — today's profit ($${e.todays_realized_pnl}) hit your ${e.daily_profit_limit_pct}% daily profit limit (≈ $${e.profit_pct_dollars} of your day-start balance).`;
      } else if (reason === "daily_profit_limit") {
        msg = `Copy trading auto-paused — today's profit ($${e.todays_realized_pnl}) hit your daily profit limit ($${e.daily_profit_limit}).`;
      } else if (reason === "max_account_pct_per_day") {
        msg = `Copy trading auto-paused — today's trading ($${e.todays_trading_value}) hit ${e.max_account_pct_per_day}% of your day-start balance.`;
      } else {
        msg = `Copy trading auto-paused — today's loss ($${e.todays_realized_pnl}) hit your daily loss limit ($${e.daily_loss_limit}).`;
      }
      notify.error(msg, { autoClose: false });
      // Pull fresh settings (copy_enabled, pnl_auto_paused_at, etc.) but
      // PRESERVE `todays_realized_pnl` from the last pnl.tick. The /GET
      // endpoint sums our local fills table, which lags the broker's
      // equity-delta value the tick uses — re-fetching naively flashes
      // the value back to $0 for ~10s until the next tick re-syncs.
      api<SubscriberSettings>("/api/settings/subscriber").then((fresh) => {
        setSub((prev) => prev ? { ...fresh, todays_realized_pnl: prev.todays_realized_pnl } : fresh);
      });
      return;
    }
    if (e?.type === "copy.auto_resumed") {
      // Fires when a daily-limit pause expires at UTC midnight. The
      // subscriber's copy_enabled has just flipped back to true on the
      // server; refresh local state so the toggle in the sidebar +
      // Settings header tracks. Same PRESERVE-pnl pattern as the pause
      // handler above so the P&L tile doesn't flash to 0 mid-tick.
      notify.success("Copy trading auto-resumed for the new day.");
      api<SubscriberSettings>("/api/settings/subscriber").then((fresh) => {
        setSub((prev) => prev ? { ...fresh, todays_realized_pnl: prev.todays_realized_pnl } : fresh);
      });
      return;
    }
    if (e?.type === "copy.auto_liquidated") {
      notify.success(
        `Take-profit hit — unrealized profit reached $${e.unrealized_pl} ` +
        `(target $${e.auto_liquidation_limit}). ` +
        `${e.closed ?? 0} position(s) closed, ${e.cancelled ?? 0} open order(s) cancelled. ` +
        `Copy trading is OFF until you re-enable it.`,
        { autoClose: false },
      );
      // CRITICAL: preserve the prior P&L value through this fetch.
      // Liquidation just placed close orders at the broker; the fills
      // haven't synced into our local DB yet, so the GET endpoint will
      // return today_realized_pnl=$0 momentarily and the "Today" cell
      // would flash to $0 until the next tick (~10s later). Keep the
      // live value from the last pnl.tick instead.
      api<SubscriberSettings>("/api/settings/subscriber").then((fresh) => {
        setSub((prev) => prev ? { ...fresh, todays_realized_pnl: prev.todays_realized_pnl } : fresh);
      });
      return;
    }
    // pnl_poller publishes this every 60s with the latest equity-delta
    // P&L from Alpaca + the current copy_enabled flag. Merge into local
    // state so the P&L Limit panel updates live without a manual refresh.
    if (e?.type === "pnl.tick") {
      if (typeof e.beginning_day_balance === "string") {
        setBeginningDayBalance(e.beginning_day_balance);
        writeTickCache({ beginning_day_balance: e.beginning_day_balance });
      }
      if (typeof e.todays_trading_value === "string") {
        setTodaysTradingValue(e.todays_trading_value);
        writeTickCache({ todays_trading_value: e.todays_trading_value });
      }
      if (typeof e.equity === "string") {
        setEquity(e.equity);
      }
      if (typeof e.unrealized_pl === "string" || e.unrealized_pl === null) {
        setUnrealizedPl(e.unrealized_pl);
      }
      // The tick carries the canonical DB state for every limit field —
      // overwrite with the tick value (null included) so a freshly-
      // cleared limit doesn't stay stuck in the UI. Only "today" /
      // copy_enabled fall back to prev when missing from the payload.
      setSub(prev => prev ? {
        ...prev,
        todays_realized_pnl: e.todays_realized_pnl ?? prev.todays_realized_pnl,
        daily_loss_limit: e.daily_loss_limit ?? null,
        daily_profit_limit: e.daily_profit_limit ?? null,
        daily_loss_limit_pct: e.daily_loss_limit_pct ?? null,
        daily_profit_limit_pct: e.daily_profit_limit_pct ?? null,
        max_per_contract: e.max_per_contract ?? null,
        max_account_pct_per_day: e.max_account_pct_per_day ?? null,
        auto_liquidation_limit: e.auto_liquidation_limit ?? null,
        position_tp_pct: e.position_tp_pct ?? null,
        position_sl_pct: e.position_sl_pct ?? null,
        copy_enabled: e.copy_enabled ?? prev.copy_enabled,
      } : prev);
    }
    // Per-position TP/SL — pnl_poller fires this whenever it
    // auto-closes a position. Surface a non-modal toast and refetch
    // the row so the panel reflects any state changes immediately.
    if (e?.type === "position.auto_closed") {
      const legLabel = e.leg === "tp" ? "take-profit" : "stop-loss";
      const threshold = e.leg === "tp" ? e.position_tp_pct : e.position_sl_pct;
      notify.warn(
        `${e.symbol} closed automatically at ${e.pct}% ` +
        `(${legLabel} threshold ${threshold}%). ` +
        `Other positions and copy trading are unaffected.`,
        { autoClose: 10000 },
      );
      return;
    }
  });

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
        method: "PATCH", body: JSON.stringify({ multiplier: rounded }),
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
      const body = { daily_loss_limit_pct: trimmed === "" ? null : trimmed };
      const s = await api<SubscriberSettings>("/api/settings/subscriber/daily-loss-limit-pct", {
        method: "PATCH", body: JSON.stringify(body),
      });
      setSub(s);
      setLimitInput(s.daily_loss_limit_pct ?? "");
      notify.success(s.daily_loss_limit_pct ? `Daily loss limit set to ${s.daily_loss_limit_pct}%` : "Daily loss limit cleared");
    } catch (e) {
      notify.fromError(e, "Could not update daily loss limit");
    } finally {
      setLimitBusy(false);
    }
  }
  async function saveProfit() {
    setProfitBusy(true);
    try {
      const trimmed = profitInput.trim();
      const body = { daily_profit_limit_pct: trimmed === "" ? null : trimmed };
      const s = await api<SubscriberSettings>("/api/settings/subscriber/daily-profit-limit-pct", {
        method: "PATCH", body: JSON.stringify(body),
      });
      setSub(s);
      setProfitInput(s.daily_profit_limit_pct ?? "");
      notify.success(s.daily_profit_limit_pct ? `Daily profit limit set to ${s.daily_profit_limit_pct}%` : "Daily profit limit cleared");
    } catch (e) {
      notify.fromError(e, "Could not update daily profit limit");
    } finally {
      setProfitBusy(false);
    }
  }
  async function saveMaxContract() {
    setMaxContractBusy(true);
    try {
      const trimmed = maxContractInput.trim();
      const body = { max_per_contract: trimmed === "" ? null : trimmed };
      const s = await api<SubscriberSettings>("/api/settings/subscriber/max-per-contract", {
        method: "PATCH", body: JSON.stringify(body),
      });
      setSub(s);
      setMaxContractInput(s.max_per_contract ?? "");
      notify.success(s.max_per_contract ? `Max per contract set to $${s.max_per_contract}` : "Max per contract cleared");
    } catch (e) {
      notify.fromError(e, "Could not update max per contract");
    } finally {
      setMaxContractBusy(false);
    }
  }
  async function saveMaxPct() {
    setMaxPctBusy(true);
    try {
      const trimmed = maxPctInput.trim();
      const body = { max_account_pct_per_day: trimmed === "" ? null : trimmed };
      const s = await api<SubscriberSettings>("/api/settings/subscriber/max-account-pct", {
        method: "PATCH", body: JSON.stringify(body),
      });
      setSub(s);
      setMaxPctInput(s.max_account_pct_per_day ?? "");
      notify.success(s.max_account_pct_per_day ? `Max ${s.max_account_pct_per_day}% per day set` : "Max % per day cleared");
    } catch (e) {
      notify.fromError(e, "Could not update max % per day");
    } finally {
      setMaxPctBusy(false);
    }
  }
  async function saveAutoLiq() {
    setAutoLiqBusy(true);
    try {
      const trimmed = autoLiqInput.trim();
      const body = { auto_liquidation_limit: trimmed === "" ? null : trimmed };
      const s = await api<SubscriberSettings>("/api/settings/subscriber/auto-liquidation-limit", {
        method: "PATCH", body: JSON.stringify(body),
      });
      setSub(s);
      setAutoLiqInput(s.auto_liquidation_limit ?? "");
      notify.success(
        s.auto_liquidation_limit
          ? `Auto-liquidation floor set to $${s.auto_liquidation_limit}`
          : "Auto-liquidation cleared",
      );
    } catch (e) {
      notify.fromError(e, "Could not update auto-liquidation limit");
    } finally {
      setAutoLiqBusy(false);
    }
  }
  async function savePosTp() {
    setPosTpBusy(true);
    try {
      const trimmed = posTpInput.trim();
      const body = { position_tp_pct: trimmed === "" ? null : trimmed };
      const s = await api<SubscriberSettings>("/api/settings/subscriber/position-tp-pct", {
        method: "PATCH", body: JSON.stringify(body),
      });
      setSub(s);
      setPosTpInput(s.position_tp_pct ?? "");
      notify.success(
        s.position_tp_pct
          ? `Position take-profit set to ${s.position_tp_pct}%`
          : "Position take-profit cleared",
      );
    } catch (e) {
      notify.fromError(e, "Could not update position take-profit");
    } finally {
      setPosTpBusy(false);
    }
  }
  async function savePosSl() {
    setPosSlBusy(true);
    try {
      const trimmed = posSlInput.trim();
      const body = { position_sl_pct: trimmed === "" ? null : trimmed };
      const s = await api<SubscriberSettings>("/api/settings/subscriber/position-sl-pct", {
        method: "PATCH", body: JSON.stringify(body),
      });
      setSub(s);
      setPosSlInput(s.position_sl_pct ?? "");
      notify.success(
        s.position_sl_pct
          ? `Position stop-loss set to ${s.position_sl_pct}%`
          : "Position stop-loss cleared",
      );
    } catch (e) {
      notify.fromError(e, "Could not update position stop-loss");
    } finally {
      setPosSlBusy(false);
    }
  }
  async function setRetryInterval(direction: "open" | "close", value: RetryInterval) {
    try {
      const body = direction === "open"
        ? { retry_interval_open: value }
        : { retry_interval_close: value };
      const s = await api<SubscriberSettings>(
        "/api/settings/subscriber/retry-interval",
        { method: "PATCH", body: JSON.stringify(body) },
      );
      setSub(s);
    } catch (e) {
      notify.fromError(e, "Could not update retry interval");
    }
  }

  async function setRetryMaxAttempts(value: number) {
    try {
      const s = await api<SubscriberSettings>(
        "/api/settings/subscriber/retry-interval",
        { method: "PATCH", body: JSON.stringify({ retry_max_attempts: value }) },
      );
      setSub(s);
    } catch (e) {
      notify.fromError(e, "Could not update retry count");
    }
  }

  async function saveSymbolFilter(
    which: "symbol_exclusion_list" | "symbol_inclusion_list",
    next: string[],
  ) {
    if (!sub) return;
    const prev = sub;
    setSub({ ...sub, [which]: next });
    try {
      const s = await api<SubscriberSettings>(
        "/api/settings/subscriber/symbol-filter",
        { method: "PATCH", body: JSON.stringify({ [which]: next }) },
      );
      setSub(s);
    } catch (e) {
      setSub(prev);
      notify.fromError(e, "Could not update symbol filter");
    }
  }

  async function toggleTrading(next: boolean) {
    setTrd(await api<TraderSettings>("/api/settings/trader", {
      method: "PATCH", body: JSON.stringify({ trading_enabled: next })
    }));
  }

  // Gate the page until BOTH the user identity AND the role-specific
  // settings row have landed — otherwise the subscriber/trader sections
  // briefly render empty while the second fetch is in flight.
  const settingsReady = user && (
    (user.role === "subscriber" && sub) ||
    (user.role === "trader" && trd) ||
    (user.role !== "subscriber" && user.role !== "trader")
  );
  if (!settingsReady) return <PageLoading />;

  const fmt = (v: string | null | undefined): string => {
    if (v === null || v === undefined || v === "") return "—";
    const n = Number(v);
    if (!Number.isFinite(n)) return v;
    return n.toLocaleString(undefined, { style: "currency", currency: "USD" });
  };
  const fmtMultiplier = (v: string): string => {
    const n = parseFloat(v);
    return Number.isFinite(n) ? n.toString() : v;
  };

  const todaysPnL = sub ? Number(sub.todays_realized_pnl ?? "0") : 0;
  // Daily loss limit is now a PERCENTAGE of the broker's beginning-day
  // balance. Derive the dollar threshold from baseNum * pct / 100, then
  // headroom is "dollars of loss capacity left" (max(0, threshold − loss)).
  const lossPctNum = sub?.daily_loss_limit_pct ? Number(sub.daily_loss_limit_pct) : null;
  // Only count the LOSS portion against the loss-limit headroom — see
  // pre-existing comment on max(0,-pnl) clamp.
  // We need baseNum below — let it hoist via the maxPct block which
  // defines it; for the loss/profit blocks we re-derive here.
  const _baseEarly = beginningDayBalance ? Number(beginningDayBalance) : null;
  const lossPctDollars = (lossPctNum !== null && _baseEarly !== null && _baseEarly > 0)
    ? (_baseEarly * lossPctNum / 100)
    : null;
  // Keep `limit` name so JSX below doesn't have to change everywhere —
  // it now holds the derived dollar threshold instead of the raw USD value.
  const limit = lossPctDollars;
  const headroom = limit !== null ? Math.max(0, limit - Math.max(0, -todaysPnL)) : null;
  const limitPct = limit !== null && limit > 0 ? Math.min(100, Math.max(0, (-todaysPnL / limit) * 100)) : 0;
  // Profit-limit mirror.
  const profitPctNum = sub?.daily_profit_limit_pct ? Number(sub.daily_profit_limit_pct) : null;
  const profitPctDollars = (profitPctNum !== null && _baseEarly !== null && _baseEarly > 0)
    ? (_baseEarly * profitPctNum / 100)
    : null;
  const profitLimit = profitPctDollars;
  const profitHeadroom = profitLimit !== null ? Math.max(0, profitLimit - Math.max(0, todaysPnL)) : null;
  // Max-% derived values — pulled up here so the table row can read
  // them. balance × pct/100 = the dollar threshold; today's trading
  // value (from the SSE tick) is the metric compared against it.
  const maxPctNum = sub?.max_account_pct_per_day ? Number(sub.max_account_pct_per_day) : null;
  const baseNum = beginningDayBalance ? Number(beginningDayBalance) : null;
  const tvNum = todaysTradingValue ? Number(todaysTradingValue) : 0;
  const maxPctLimitDollars = (maxPctNum && baseNum && baseNum > 0)
    ? (baseNum * maxPctNum / 100) : null;
  const maxPctHeadroom = maxPctLimitDollars !== null
    ? Math.max(0, maxPctLimitDollars - tvNum) : null;
  const maxPctConsumed = (maxPctLimitDollars && maxPctLimitDollars > 0)
    ? Math.min(100, Math.max(0, (tvNum / maxPctLimitDollars) * 100)) : 0;
  const profitPct = profitLimit !== null && profitLimit > 0
    ? Math.min(100, Math.max(0, (Math.max(0, todaysPnL) / profitLimit) * 100))
    : 0;

  const followedTrader = sub?.following_trader_id
    ? traders.find(t => t.id === sub.following_trader_id) ?? null
    : null;

  return (
    <div className="space-y-5 max-w-6xl pb-12">
      {user.role === "subscriber" && sub && (
        <>
          {/* ── Trader + Multiplier ─────────────────────────────────── */}
          <Card
            icon={<IconUsers />}
            title="Following trader & multiplier"
            hint="Pick whose trades to mirror, and scale them by a factor of 0.1× to 10×."
          >
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <Field label="Following">
                <SelectInput
                  value={sub.following_trader_id ?? ""}
                  onChange={v => follow(v || null)}
                  options={[
                    { value: "", label: "— not following anyone —" },
                    ...traders.map(t => ({ value: t.id, label: t.display_name ?? t.email })),
                  ]}
                />
              </Field>

              <Field label={<>Multiplier <span className="opacity-50">· 0.1–10</span></>}>
                <div className="flex items-center gap-2">
                  <NumberInput
                    value={multInput}
                    onChange={setMultInput}
                    step={0.1} min={0.1} max={10}
                    className="w-24"
                  />
                  <span className="text-xs tabular-nums whitespace-nowrap" style={{ color: "var(--muted)" }}>
                    current ×{fmtMultiplier(sub.multiplier)}
                  </span>
                  <PrimaryButton
                    busy={multBusy}
                    onClick={saveMultiplier}
                    disabled={multBusy || parseFloat(multInput) === parseFloat(sub.multiplier)}
                    className="ml-auto"
                  >
                    Save
                  </PrimaryButton>
                </div>
              </Field>
            </div>
          </Card>

          {/* ── Risk Controls (all four limits in one table) ──────────── */}
          {/* One unified surface for every kill-switch on the account.
              Each row is its own subtle card with a color-coded left
              accent, live "Today" reading, inline input + Save, and a
              progress bar across the bottom. The four rows share one
              header so the eye doesn't have to re-orient between cards. */}
          <Card
            icon={<IconShield />}
            title="Risk Controls"
            hint="Loss / profit / size / capital caps. When any live limit is hit, copy turns OFF for the day and auto-resumes the next UTC day."
          >
            <div className="space-y-2.5">
              {/* Individual Position (TP/SL) — surfaced FIRST so the
                  most-used per-position control sits at the top of the
                  Risk Controls table. Doesn't share the Limit /
                  Threshold / Today / Headroom / Used column layout
                  (it has two inputs, no live readouts), so the column
                  legend renders BELOW this row, right above the
                  LimitRow grid where those columns actually apply. */}
              <PositionTpSlRow
                tpInput={posTpInput}
                onTpInput={setPosTpInput}
                tpBusy={posTpBusy}
                onTpSave={savePosTp}
                tpCurrent={sub.position_tp_pct}
                slInput={posSlInput}
                onSlInput={setPosSlInput}
                slBusy={posSlBusy}
                onSlSave={savePosSl}
                slCurrent={sub.position_sl_pct}
              />

              {/* Desktop column legend for the LimitRow grid below.
                  Hidden on mobile where rows stack their own labels.
                  Lives here (not at the top of the card) because the
                  Individual Position row above doesn't share these
                  columns. USD column added between Today and Headroom
                  to surface the actual dollar trigger derived from
                  the % the trader typed. */}
              <div
                className="hidden md:grid items-center gap-3 px-4 pt-1 pb-1 text-[9px] uppercase tracking-widest"
                style={{
                  gridTemplateColumns: "1.5fr 1.3fr 0.8fr 0.9fr 0.9fr 0.5fr",
                  color: "var(--muted)",
                }}
              >
                <div>Limit</div>
                <div>Threshold</div>
                <div>Today</div>
                <div>USD</div>
                <div>Headroom</div>
                <div className="text-right">Used</div>
              </div>
              <LimitRow
                accent="#ef4444"
                icon={<IconTrendDown />}
                title="Daily loss limit"
                subtitle="Pause copy when today's loss reaches this % of your day-start balance."
                todayLabel="Today P&L"
                todayValue={fmt(String(todaysPnL))}
                todayColor={todaysPnL >= 0 ? "var(--good)" : "var(--bad)"}
                inputPrefix="%"
                input={limitInput}
                onInput={setLimitInput}
                busy={limitBusy}
                onSave={saveLimit}
                current={sub.daily_loss_limit_pct}
                hasLimit={limit !== null}
                thresholdUsdDisplay={limit === null ? "—" : fmt(String(limit))}
                headroomDisplay={limit === null ? "—" : fmt(String(headroom))}
                headroomColor={(headroom ?? 1) > 0 ? "var(--text)" : "var(--bad)"}
                pctConsumed={limitPct}
              />
              <LimitRow
                accent="#22c55e"
                icon={<IconTrendUp />}
                title="Daily profit limit"
                subtitle="Pause copy when today's profit reaches this % of your day-start balance."
                todayLabel="Today P&L"
                todayValue={fmt(String(todaysPnL))}
                todayColor={todaysPnL >= 0 ? "var(--good)" : "var(--bad)"}
                inputPrefix="%"
                input={profitInput}
                onInput={setProfitInput}
                busy={profitBusy}
                onSave={saveProfit}
                current={sub.daily_profit_limit_pct}
                hasLimit={profitLimit !== null}
                thresholdUsdDisplay={profitLimit === null ? "—" : fmt(String(profitLimit))}
                headroomDisplay={profitLimit === null ? "—" : fmt(String(profitHeadroom))}
                headroomColor={(profitHeadroom ?? 1) > 0 ? "var(--text)" : "var(--bad)"}
                pctConsumed={profitPct}
              />
              <LimitRow
                accent="#f59e0b"
                icon={<IconPercent />}
                title="Max % of account per day"
                subtitle="Pause when today's trading value reaches this % of the day's starting balance."
                todayLabel="Day-Start"
                todayValue={beginningDayBalance !== null ? fmt(beginningDayBalance) : "—"}
                inputPrefix="%"
                input={maxPctInput}
                onInput={setMaxPctInput}
                busy={maxPctBusy}
                onSave={saveMaxPct}
                current={sub.max_account_pct_per_day}
                hasLimit={maxPctLimitDollars !== null}
                thresholdUsdDisplay={maxPctLimitDollars === null ? "—" : fmt(String(maxPctLimitDollars))}
                headroomDisplay={maxPctLimitDollars === null ? "—" : fmt(String(maxPctHeadroom))}
                headroomColor={(maxPctHeadroom ?? 1) > 0 ? "var(--text)" : "var(--bad)"}
                pctConsumed={maxPctConsumed}
              />

              <LimitRow
                accent="#3b82f6"
                icon={<IconLayers />}
                title="Max per contract"
                subtitle="This feature is not available yet."
                todayLabel="—"
                todayValue="—"
                inputPrefix="USD"
                input={maxContractInput}
                onInput={setMaxContractInput}
                busy={maxContractBusy}
                onSave={saveMaxContract}
                current={sub.max_per_contract}
                hasLimit={false}
                thresholdUsdDisplay="—"
                headroomDisplay="—"
              />
            </div>
          </Card>

          {/* ── Auto-liquidation — its own surface ───────────────────────
              Separated from Risk Controls so the trader sees this as a
              distinct take-profit instrument, not a fifth daily cap. The
              semantic is different too: it locks in a winning day by
              CLOSING positions, where the Risk Controls rows just pause
              new mirror entries. */}
          <Card
            icon={<IconTrendUp />}
            title="Auto-Liquidation (Take-Profit)"
            hint="When today's unrealized profit reaches the target, every open position is closed at market and copy turns OFF. Manual re-enable required — it does NOT auto-resume next day."
          >
            <div
              className="hidden md:grid items-center gap-3 px-4 pb-2 text-[9px] uppercase tracking-widest"
              style={{
                gridTemplateColumns: "1.5fr 1.3fr 0.8fr 0.9fr 0.9fr 0.5fr",
                color: "var(--muted)",
              }}
            >
              <div>Target</div>
              <div>Set</div>
              <div>Profit</div>
              <div>USD</div>
              <div>Headroom</div>
              <div className="text-right">Progress</div>
            </div>
            {(() => {
              const unrealizedNum = unrealizedPl !== null ? Number(unrealizedPl) : null;
              const liqLimitNum = sub.auto_liquidation_limit ? Number(sub.auto_liquidation_limit) : null;
              // Headroom = how much MORE profit you need before the
              // trigger fires. Clamped to 0 so once you've reached the
              // limit the cell reads "$0.00" instead of a negative.
              const liqHeadroom =
                unrealizedNum !== null && liqLimitNum !== null
                  ? Math.max(0, liqLimitNum - unrealizedNum)
                  : null;
              // Progress bar: unrealized / target, clamped 0–100.
              const liqPctConsumed =
                unrealizedNum !== null && liqLimitNum !== null && liqLimitNum > 0
                  ? Math.min(100, Math.max(0, (unrealizedNum / liqLimitNum) * 100))
                  : 0;
              return (
                <LimitRow
                  accent="#22c55e"
                  icon={<IconTrendUp />}
                  title="Auto-liquidation target"
                  subtitle="Sell everything + disable copy when today's unrealized profit hits this dollar value."
                  todayLabel="Profit"
                  todayValue={unrealizedPl !== null ? fmt(unrealizedPl) : "—"}
                  todayColor={(unrealizedNum ?? 0) >= 0 ? "var(--good)" : "var(--bad)"}
                  inputPrefix="USD"
                  input={autoLiqInput}
                  onInput={setAutoLiqInput}
                  busy={autoLiqBusy}
                  onSave={saveAutoLiq}
                  current={sub.auto_liquidation_limit}
                  hasLimit={liqLimitNum !== null}
                  thresholdUsdDisplay="—"
                  headroomDisplay={
                    liqLimitNum === null
                      ? "—"
                      : liqHeadroom !== null
                        ? fmt(String(liqHeadroom))
                        : "—"
                  }
                  // Headroom text stays neutral — this is a take-profit,
                  // not a stop-loss, so hitting $0 headroom is *good*.
                  headroomColor="var(--text)"
                  pctConsumed={liqPctConsumed}
                  // Tell LimitRow to keep the progress bar green even at
                  // 100% — the default treats max-progress as danger
                  // (red), which is wrong for a take-profit target.
                  successProgress
                />
              );
            })()}
          </Card>

          {/* ── Symbol filters ─────────────────────────────────────── */}
          <Card
            icon={<IconFilter />}
            title="Trade Settings"
            hint="Filter which trader trades get copied to you. Empty = mirror everything."
          >
            {/* Two filter panels stacked with a single hairline divider
                between — no nested boxes. */}
            <div className="space-y-4">
              <FilterPanel
                title="Exclusion list"
                description={<>Trades on these symbols are <strong>NOT</strong> copied.</>}
                counter={`${sub.symbol_exclusion_list.length} ${sub.symbol_exclusion_list.length === 1 ? "symbol" : "symbols"}`}
                symbols={sub.symbol_exclusion_list}
                onChange={(n) => saveSymbolFilter("symbol_exclusion_list", n)}
                placeholder="e.g. TSLA — Enter or comma to add"
              />
              <FilterPanel
                title="Inclusion list"
                description={<>When non-empty, <strong>ONLY</strong> these symbols are copied.</>}
                counter={sub.symbol_inclusion_list.length === 0
                  ? "all symbols"
                  : `${sub.symbol_inclusion_list.length} ${sub.symbol_inclusion_list.length === 1 ? "symbol" : "symbols"} only`}
                symbols={sub.symbol_inclusion_list}
                onChange={(n) => saveSymbolFilter("symbol_inclusion_list", n)}
                placeholder="e.g. AAPL — Enter or comma to add"
              />
            </div>
          </Card>

          {/* ── Retry policy ───────────────────────────────────────── */}
          <Card
            icon={<IconRefresh />}
            title="Retry on broker errors"
            hint="For transient failures only (network / 5xx / rate-limit). User-fixable errors never retry."
          >
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <Field label="Opening positions">
                <SelectInput
                  value={sub.retry_interval_open}
                  onChange={(v) => setRetryInterval("open", v as RetryInterval)}
                  options={RETRY_OPTIONS.map(o => ({ value: o.value, label: o.label }))}
                />
              </Field>
              <Field label="Closing positions">
                <SelectInput
                  value={sub.retry_interval_close}
                  onChange={(v) => setRetryInterval("close", v as RetryInterval)}
                  options={RETRY_OPTIONS.map(o => ({ value: o.value, label: o.label }))}
                />
              </Field>
              <Field label="Max retries">
                <SelectInput
                  value={String(sub.retry_max_attempts ?? 1)}
                  onChange={(v) => setRetryMaxAttempts(Number(v))}
                  options={RETRY_COUNT_OPTIONS}
                />
              </Field>
            </div>
          </Card>
        </>
      )}

      {user.role === "trader" && trd && (
        <Card
          icon={<IconPower />}
          title="Master trading switch"
          hint="When OFF, the platform refuses to place new orders (yours and any subscriber mirrors)."
        >
          <div className="flex items-center justify-between gap-3">
            <Pill
              dot={trd.trading_enabled ? "var(--good)" : "var(--bad)"}
              label="State"
              value={trd.trading_enabled ? "ON" : "OFF"}
              valueColor={trd.trading_enabled ? "var(--good)" : "var(--bad)"}
            />
            <button
              onClick={() => toggleTrading(!trd.trading_enabled)}
              className="px-4 py-2 text-sm rounded-lg font-medium transition-all hover:scale-[1.02] active:scale-[0.98]"
              style={{
                background: trd.trading_enabled ? "var(--bad)" : "var(--good)",
                color: "#06121f",
                boxShadow: "0 4px 14px -4px rgba(0,0,0,0.4)",
              }}
            >
              {trd.trading_enabled ? "Turn OFF" : "Turn ON"}
            </button>
          </div>
        </Card>
      )}
    </div>
  );
}

// ── Reusable building blocks ────────────────────────────────────────────

/** Polished card shell: refined border, subtle gradient, icon-anchored
 *  title row, and an optional hint paragraph below. Uses CSS vars from
 *  the theme so it picks up dark/light mode automatically. */
function Card({
  icon, title, hint, children,
}: {
  icon?: React.ReactNode;
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      className="rounded-xl border overflow-hidden"
      style={{
        borderColor: "var(--border)",
        background: "linear-gradient(180deg, var(--panel) 0%, rgba(0,0,0,0.18) 100%)",
        boxShadow: "0 1px 0 rgba(255,255,255,0.03) inset, 0 8px 24px -16px rgba(0,0,0,0.5)",
      }}
    >
      <header
        className="flex items-start gap-2.5 px-4 py-3 border-b"
        style={{ borderColor: "var(--border)" }}
      >
        {icon && (
          <span
            className="grid place-items-center w-7 h-7 rounded-md shrink-0"
            style={{ background: "rgba(255,255,255,0.04)", color: "var(--accent-2, var(--accent))" }}
          >
            {icon}
          </span>
        )}
        <div className="min-w-0">
          <h2 className="text-sm font-semibold leading-tight">{title}</h2>
          {hint && <p className="text-[11px] mt-1 leading-snug" style={{ color: "var(--muted)" }}>{hint}</p>}
        </div>
      </header>
      <div className="px-4 py-3">{children}</div>
    </section>
  );
}

function Field({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-[10px] uppercase tracking-wider mb-1.5 font-medium" style={{ color: "var(--muted)" }}>
        {label}
      </label>
      {children}
    </div>
  );
}

function NumberInput({
  value, onChange, step, min, max, className = "", placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  step?: number;
  min?: number;
  max?: number;
  className?: string;
  placeholder?: string;
}) {
  return (
    <input
      type="number" step={step} min={min} max={max} placeholder={placeholder}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={`px-3 py-2 text-sm rounded-lg bg-transparent border tabular-nums transition-colors focus:outline-none focus:border-[var(--accent)] ${className}`}
      style={{ borderColor: "var(--border)" }}
    />
  );
}

function SelectInput({
  value, onChange, options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full px-3 py-2 text-sm rounded-lg bg-transparent border transition-colors focus:outline-none focus:border-[var(--accent)] cursor-pointer"
      style={{ borderColor: "var(--border)" }}
    >
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

function PrimaryButton({
  busy, onClick, disabled, children, className = "",
}: {
  busy?: boolean;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`px-4 py-2 text-xs rounded-lg font-semibold inline-flex items-center gap-1.5 transition-all disabled:opacity-30 disabled:cursor-not-allowed hover:enabled:scale-[1.03] active:enabled:scale-[0.97] ${className}`}
      style={{
        background: "var(--accent)",
        color: "#06121f",
      }}
    >
      {busy && <Spinner />}
      {children}
    </button>
  );
}

function Pill({ dot, label, value, valueColor }: {
  dot: string;
  label: string;
  value?: string;
  valueColor?: string;
}) {
  return (
    <span className="inline-flex items-center gap-2 text-xs">
      <span
        aria-hidden
        style={{
          width: 7, height: 7, borderRadius: "50%", background: dot,
          boxShadow: `0 0 8px ${dot}`,
          display: "inline-block",
        }}
      />
      <span style={{ color: "var(--muted)" }}>{label}</span>
      {value !== undefined && (
        <strong style={{ color: valueColor ?? "var(--text)" }}>{value}</strong>
      )}
    </span>
  );
}

function StatusItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-baseline gap-1.5 text-xs">
      <span className="text-[10px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>
        {label}
      </span>
      <span>{children}</span>
    </span>
  );
}

function Divider() {
  return <span className="h-3.5 w-px" style={{ background: "var(--border)" }} aria-hidden />;
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wider font-medium" style={{ color: "var(--muted)" }}>{label}</div>
      <div className="font-semibold mt-0.5 tabular-nums text-sm" style={{ color: color ?? "var(--text)" }}>{value}</div>
    </div>
  );
}


/** One row of the consolidated Risk Controls table.
 *
 *  Visual rhythm:
 *    - Color-coded left accent rail (per limit type)
 *    - Icon + title + subtitle at the start
 *    - Live "Today" value, inline input + Save, headroom, used%
 *    - Slim animated progress bar across the bottom edge
 *    - Hover lifts the card 1px for tactility
 *
 *  Designed to read as a table at md+ widths (columns align with the
 *  legend above), and as a stack on mobile (each row's content drops
 *  into a single column with field labels back inline).
 *
 *  `hasLimit` controls whether the headroom / progress bar render — for
 *  the UI-only Max-per-contract row, both stay "—" / hidden. */
function LimitRow({
  accent, icon, title, subtitle,
  todayLabel, todayValue, todayColor,
  inputPrefix, input, onInput, busy, onSave, current,
  hasLimit, thresholdHint,
  thresholdUsdDisplay,
  headroomDisplay, headroomColor,
  pctConsumed,
  successProgress,
}: {
  accent: string;
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  todayLabel: string;
  todayValue: string;
  todayColor?: string;
  inputPrefix: string;
  input: string;
  onInput: (v: string) => void;
  busy: boolean;
  onSave: () => void;
  current: string | null;
  hasLimit: boolean;
  thresholdHint?: string;
  /** The threshold expressed in USD. For pct-based rows this is
   *  ``balance × pct / 100`` (the absolute dollar amount the limit
   *  trips at). Renders in its own column between "Today" and
   *  "Headroom" so the trader sees both the % they typed AND the
   *  actual dollar trigger. Pass "—" (or omit) when the row doesn't
   *  have a meaningful USD value (e.g. Max-per-contract, which IS
   *  USD already). */
  thresholdUsdDisplay?: string;
  headroomDisplay: string;
  headroomColor?: string;
  pctConsumed?: number;
  /** When true, the progress bar keeps the accent color all the way to
   *  100% (no orange-at-75% or red-at-100% transitions). Use this for
   *  rows where reaching the limit is the *desired* outcome — e.g. the
   *  take-profit Auto-Liquidation card, where 100% means "you locked in
   *  your target gain", not "you blew past a stop-loss". */
  successProgress?: boolean;
}) {
  const barPct = Math.max(0, Math.min(100, pctConsumed ?? 0));
  const barTone = successProgress
    ? accent
    : barPct >= 100 ? "var(--bad)"
      : barPct >= 75 ? "#f59e0b"
        : accent;

  return (
    <div
      className="relative rounded-xl border overflow-hidden"
      style={{
        background:
          "linear-gradient(135deg, rgba(255,255,255,0.025) 0%, rgba(255,255,255,0.005) 60%, rgba(0,0,0,0.15) 100%)",
        borderColor: "var(--border)",
        boxShadow:
          "inset 0 1px 0 rgba(255,255,255,0.03), 0 1px 2px rgba(0,0,0,0.2)",
      }}
    >
      {/* Left accent rail — fades top→bottom for a softer feel */}
      <div
        aria-hidden
        className="absolute left-0 top-0 bottom-0 w-[3px]"
        style={{
          background: `linear-gradient(180deg, ${accent} 0%, ${accent}66 60%, transparent 100%)`,
          boxShadow: `0 0 12px -2px ${accent}80`,
        }}
      />

      {/* Grid: matches the legend above on md+, stacks on mobile */}
      <div
        className="grid items-center gap-3 p-4 pl-5"
        style={{ gridTemplateColumns: "minmax(0,1fr)" }}
      >
        <div
          className="md:grid md:items-center md:gap-3"
          style={{ gridTemplateColumns: "1.5fr 1.3fr 0.8fr 0.9fr 0.9fr 0.5fr" }}
        >
          {/* Limit name + subtitle */}
          <div className="min-w-0 flex items-start gap-2.5">
            <div
              className="shrink-0 mt-0.5 rounded-md p-1.5"
              style={{
                color: accent,
                background: `${accent}1a`,
                border: `1px solid ${accent}33`,
              }}
            >
              {icon}
            </div>
            <div className="min-w-0">
              <div className="text-sm font-semibold leading-tight">{title}</div>
              <div className="text-[11px] mt-0.5 leading-snug" style={{ color: "var(--muted)" }}>
                {subtitle}
              </div>
            </div>
          </div>

          {/* Threshold input + Save inline — moved BEFORE "Today" so the
              column the user actively edits sits next to the row title. */}
          <div className="mt-3 md:mt-0">
            <div className="md:hidden text-[9px] uppercase tracking-widest mb-0.5" style={{ color: "var(--muted)" }}>
              Threshold
            </div>
            <div className="flex items-center gap-2">
              <div
                className="flex-1 inline-flex items-center rounded-lg border overflow-hidden transition-colors focus-within:border-[var(--accent)]"
                style={{
                  borderColor: "var(--border)",
                  background: "rgba(0,0,0,0.25)",
                }}
              >
                <span
                  className="px-2.5 py-2 text-[10px] font-semibold border-r tabular-nums self-stretch inline-flex items-center"
                  style={{ color: "var(--muted)", borderColor: "var(--border)" }}
                >
                  {inputPrefix}
                </span>
                <input
                  type="number"
                  step={inputPrefix === "%" ? 0.5 : 0.01}
                  min={0}
                  max={inputPrefix === "%" ? 100 : undefined}
                  placeholder="no limit"
                  className="flex-1 w-full px-2 py-2 text-xs tabular-nums"
                  style={{
                    border: "none",
                    background: "transparent",
                    outline: "none",
                    borderRadius: 0,
                    color: "var(--text)",
                  }}
                  value={input}
                  onChange={(e) => onInput(e.target.value)}
                />
              </div>
              <PrimaryButton
                busy={busy}
                onClick={onSave}
                disabled={busy || input === (current ?? "")}
              >
                Save
              </PrimaryButton>
            </div>
            {thresholdHint && (
              <div className="text-[10px] mt-1 tabular-nums" style={{ color: "var(--muted)" }}>
                {thresholdHint}
              </div>
            )}
          </div>

          {/* Today — now sits AFTER the threshold input, matching the new
              column legend (Limit · Threshold · Today · USD · Headroom · Used). */}
          <div className="mt-3 md:mt-0">
            <div className="md:hidden text-[9px] uppercase tracking-widest mb-0.5" style={{ color: "var(--muted)" }}>
              {todayLabel}
            </div>
            <div
              className="text-sm font-semibold tabular-nums"
              style={{ color: todayColor ?? "var(--text)" }}
            >
              {todayValue}
            </div>
          </div>

          {/* USD — absolute dollar value the threshold trips at.
              For pct-based rows this is balance × pct / 100, computed
              by the caller (we need today's balance which isn't local
              to this component). Passed in via thresholdUsdDisplay. */}
          <div className="mt-3 md:mt-0">
            <div className="md:hidden text-[9px] uppercase tracking-widest mb-0.5" style={{ color: "var(--muted)" }}>
              USD
            </div>
            <div
              className="text-sm font-semibold tabular-nums"
              style={{ color: thresholdUsdDisplay && thresholdUsdDisplay !== "—" ? "var(--text)" : "var(--muted)" }}
            >
              {thresholdUsdDisplay ?? "—"}
            </div>
          </div>

          {/* Headroom */}
          <div className="mt-3 md:mt-0">
            <div className="md:hidden text-[9px] uppercase tracking-widest mb-0.5" style={{ color: "var(--muted)" }}>
              Headroom
            </div>
            <div
              className="text-sm font-semibold tabular-nums"
              style={{ color: headroomColor ?? "var(--text)" }}
            >
              {headroomDisplay}
            </div>
          </div>

          {/* Used % */}
          <div className="mt-3 md:mt-0 md:text-right">
            <div className="md:hidden text-[9px] uppercase tracking-widest mb-0.5" style={{ color: "var(--muted)" }}>
              Used
            </div>
            <div
              className="text-sm font-semibold tabular-nums"
              style={{
                color:
                  !hasLimit ? "var(--muted)"
                    // Take-profit rows treat hitting the limit as a *win*,
                    // so the percentage text stays in the accent color
                    // (green) all the way to 100% instead of going
                    // amber → red like the stop-loss style limits.
                    : successProgress ? accent
                      : barPct >= 100 ? "var(--bad)"
                        : barPct >= 75 ? "#f59e0b"
                          : "var(--text)",
              }}
            >
              {hasLimit ? `${Math.round(barPct)}%` : "—"}
            </div>
          </div>
        </div>
      </div>

      {/* Bottom progress bar — gradient + glow tied to limit accent.
          No track background: when used% is 0, nothing renders, so the
          last row's bottom edge doesn't show a faint horizontal strip. */}
      {hasLimit && barPct > 0 && (
        <div className="h-1 w-full overflow-hidden">
          <div
            className="h-full"
            style={{
              width: `${barPct}%`,
              background: `linear-gradient(90deg, ${barTone} 0%, ${barTone}cc 100%)`,
              boxShadow: `0 0 14px -2px ${barTone}`,
              transition: "width 0.4s ease, background 0.2s linear",
            }}
          />
        </div>
      )}
    </div>
  );
}

/** Compact two-input row for per-position TP/SL.
 *
 *  Different from LimitRow because the per-position thresholds don't
 *  have a single "Today / Headroom / Used" reading — they apply to
 *  every open position individually. So we drop those columns entirely
 *  and just expose two percent inputs (TP + SL) on one row, each with
 *  its own Save button. Matches LimitRow's visual rhythm (left accent
 *  rail, rounded card, inset shadow) so the row sits naturally inside
 *  the Risk Controls table. */
function PositionTpSlRow({
  tpInput, onTpInput, tpBusy, onTpSave, tpCurrent,
  slInput, onSlInput, slBusy, onSlSave, slCurrent,
}: {
  tpInput: string;
  onTpInput: (v: string) => void;
  tpBusy: boolean;
  onTpSave: () => void;
  tpCurrent: string | null;
  slInput: string;
  onSlInput: (v: string) => void;
  slBusy: boolean;
  onSlSave: () => void;
  slCurrent: string | null;
}) {
  // Split-accent rail: green on top (TP), red on bottom (SL). Both
  // colors match the saved-row tones used elsewhere in the panel.
  const TP_ACCENT = "#10b981";
  const SL_ACCENT = "#dc2626";
  return (
    <div
      className="relative rounded-xl border overflow-hidden"
      style={{
        background:
          "linear-gradient(135deg, rgba(255,255,255,0.025) 0%, rgba(255,255,255,0.005) 60%, rgba(0,0,0,0.15) 100%)",
        borderColor: "var(--border)",
        boxShadow:
          "inset 0 1px 0 rgba(255,255,255,0.03), 0 1px 2px rgba(0,0,0,0.2)",
      }}
    >
      {/* Left accent rail — top half green (TP), bottom half red (SL).
          Visually signals "two limits live in this row." */}
      <div
        aria-hidden
        className="absolute left-0 top-0 bottom-0 w-[3px]"
        style={{
          background:
            `linear-gradient(180deg, ${TP_ACCENT} 0%, ${TP_ACCENT}66 45%, ${SL_ACCENT}66 55%, ${SL_ACCENT} 100%)`,
          boxShadow: `0 0 12px -2px ${TP_ACCENT}80`,
        }}
      />

      <div className="p-4 pl-5">
        {/* Title + subtitle */}
        <div className="flex items-start gap-2.5 mb-3">
          <div
            className="shrink-0 mt-0.5 rounded-md p-1.5"
            style={{
              color: TP_ACCENT,
              background: `${TP_ACCENT}1a`,
              border: `1px solid ${TP_ACCENT}33`,
            }}
          >
            <IconTrendUp />
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold leading-tight">
              Individual Position (TP/SL)
            </div>
            <div className="text-[11px] mt-0.5 leading-snug" style={{ color: "var(--muted)" }}>
              Auto-close any open position whose unrealized P&L hits the take-profit % or drops below the stop-loss %. Per-position — does not pause copy.
            </div>
          </div>
        </div>

        {/* Two inputs side-by-side. Each shows accent-colored prefix
            (TP / SL), the percent value, and its own Save button. */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <PercentInputCell
            accent={TP_ACCENT}
            label="Take-profit"
            prefix="TP %"
            input={tpInput}
            onInput={onTpInput}
            busy={tpBusy}
            onSave={onTpSave}
            current={tpCurrent}
          />
          <PercentInputCell
            accent={SL_ACCENT}
            label="Stop-loss"
            prefix="SL %"
            input={slInput}
            onInput={onSlInput}
            busy={slBusy}
            onSave={onSlSave}
            current={slCurrent}
          />
        </div>
      </div>
    </div>
  );
}

/** One percent-input cell — accent-colored prefix, numeric input,
 *  inline Save. Save is disabled when the input matches the persisted
 *  value (no-op edit). Used twice inside PositionTpSlRow. */
function PercentInputCell({
  accent, label, prefix, input, onInput, busy, onSave, current,
}: {
  accent: string;
  label: string;
  prefix: string;
  input: string;
  onInput: (v: string) => void;
  busy: boolean;
  onSave: () => void;
  current: string | null;
}) {
  return (
    <div>
      <div
        className="text-[10px] uppercase tracking-widest mb-1"
        style={{ color: accent }}
      >
        {label}
      </div>
      <div className="flex items-center gap-2">
        <div
          className="flex-1 inline-flex items-center rounded-lg border overflow-hidden transition-colors focus-within:border-[var(--accent)]"
          style={{
            borderColor: "var(--border)",
            background: "rgba(0,0,0,0.25)",
          }}
        >
          <span
            className="px-2.5 py-2 text-[10px] font-semibold border-r tabular-nums self-stretch inline-flex items-center"
            style={{ color: accent, borderColor: "var(--border)" }}
          >
            {prefix}
          </span>
          <input
            type="number"
            step={0.5}
            min={0}
            placeholder="no limit"
            className="flex-1 w-full px-2 py-2 text-xs tabular-nums"
            style={{
              border: "none",
              background: "transparent",
              outline: "none",
              borderRadius: 0,
              color: "var(--text)",
            }}
            value={input}
            onChange={(e) => onInput(e.target.value)}
          />
        </div>
        <PrimaryButton
          busy={busy}
          onClick={onSave}
          disabled={busy || input === (current ?? "")}
        >
          Save
        </PrimaryButton>
      </div>
    </div>
  );
}

function IconTrendDown() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="23 18 13.5 8.5 8.5 13.5 1 6" />
      <polyline points="17 18 23 18 23 12" />
    </svg>
  );
}
function IconTrendUp() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="23 6 13.5 15.5 8.5 10.5 1 18" />
      <polyline points="17 6 23 6 23 12" />
    </svg>
  );
}
function IconLayers() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="12 2 2 7 12 12 22 7 12 2" />
      <polyline points="2 17 12 22 22 17" />
      <polyline points="2 12 12 17 22 12" />
    </svg>
  );
}
function IconPercent() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="19" y1="5" x2="5" y2="19" />
      <circle cx="6.5" cy="6.5" r="2.5" />
      <circle cx="17.5" cy="17.5" r="2.5" />
    </svg>
  );
}


/** Single exclusion/inclusion list. No box around it — the parent Card
 *  + a hairline divider provide visual grouping. */
function FilterPanel({
  title, description, counter, symbols, onChange, placeholder,
}: {
  title: string;
  description: React.ReactNode;
  counter: string;
  symbols: string[];
  onChange: (next: string[]) => void;
  placeholder: string;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="text-sm font-semibold">{title}</div>
          <p className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>{description}</p>
        </div>
        <span
          className="text-[10px] px-2 py-0.5 rounded-full tabular-nums whitespace-nowrap"
          style={{
            background: "rgba(255,255,255,0.06)",
            color: "var(--muted)",
          }}
        >
          {counter}
        </span>
      </div>
      <ChipInput
        symbols={symbols}
        onChange={onChange}
        placeholder={placeholder}
      />
    </div>
  );
}

/** Chip-style symbol input. Add via Enter or comma. Backspace on empty
 *  input removes the last chip. */
function ChipInput({ symbols, onChange, placeholder }: {
  symbols: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");

  function commit(raw: string) {
    const parts = raw.split(/[,\s]+/).map(s => s.trim().toUpperCase()).filter(Boolean);
    if (parts.length === 0) return;
    const set = new Set(symbols);
    const next = [...symbols];
    for (const p of parts) {
      if (!set.has(p)) { set.add(p); next.push(p); }
    }
    if (next.length !== symbols.length) onChange(next);
    setDraft("");
  }

  function remove(sym: string) {
    onChange(symbols.filter(s => s !== sym));
  }

  const empty = useMemo(() => symbols.length === 0, [symbols]);

  // Bordered box explicitly kept here (user request) — the rounded
  // rectangle signals "this is an editable input field" more clearly
  // than a bare row of chips with an underline. focus-within lights
  // the whole border to accent.
  return (
    <div
      className="rounded-lg border px-2 py-1.5 flex flex-wrap items-center gap-1.5 min-h-[40px] transition-colors focus-within:border-[var(--accent)]"
      style={{ borderColor: "var(--border)", background: "rgba(0,0,0,0.15)" }}
      onClick={(e) => {
        const inp = (e.currentTarget.querySelector("input") as HTMLInputElement | null);
        inp?.focus();
      }}
    >
      {symbols.map(sym => (
        <span
          key={sym}
          className="inline-flex items-center gap-1.5 pl-2 pr-1 py-0.5 text-xs rounded-md"
          style={{
            background: "rgba(255,255,255,0.08)",
            color: "var(--text)",
            fontWeight: 500,
            lineHeight: "1.5",
          }}
        >
          {sym}
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); remove(sym); }}
            aria-label={`Remove ${sym}`}
            className="opacity-50 hover:opacity-100 transition-opacity leading-none w-4 h-4 grid place-items-center rounded hover:bg-white/10"
            style={{ color: "var(--text)", fontSize: "14px" }}
          >
            ×
          </button>
        </span>
      ))}
      <input
        type="text"
        value={draft}
        onChange={(e) => {
          const v = e.target.value;
          if (/[,\s]$/.test(v)) commit(v);
          else setDraft(v);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); commit(draft); }
          else if (e.key === "Backspace" && draft === "" && symbols.length > 0) {
            remove(symbols[symbols.length - 1]);
          }
        }}
        onBlur={() => { if (draft.trim()) commit(draft); }}
        placeholder={empty ? placeholder : ""}
        className="flex-1 min-w-[120px] px-1.5 py-1 text-xs"
        style={{
          // Wipe the global input[type="text"] border + background from
          // globals.css so the inner input sits flush inside the
          // bordered chip shell instead of rendering its own box.
          border: "none",
          background: "transparent",
          outline: "none",
          borderRadius: 0,
          color: "var(--text)",
        }}
      />
    </div>
  );
}

// ── Inline SVG icons (no extra dep) ─────────────────────────────────────

function IconUsers() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  );
}
function IconShield() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  );
}
function IconFilter() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
    </svg>
  );
}
function IconRefresh() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
    </svg>
  );
}
function IconPower() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18.36 6.64a9 9 0 1 1-12.73 0" />
      <line x1="12" y1="2" x2="12" y2="12" />
    </svg>
  );
}
