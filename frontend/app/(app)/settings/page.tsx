"use client";

import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";
import type { RetryInterval, SubscriberSettings, TraderSettings, User } from "@/lib/types";

const RETRY_OPTIONS: { value: RetryInterval; label: string }[] = [
  { value: "never", label: "Never (REJECT)" },
  { value: "1m",    label: "After 1 min" },
  { value: "2m",    label: "After 2 min" },
  { value: "3m",    label: "After 3 min" },
  { value: "5m",    label: "After 5 min" },
];

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

  useEffect(() => {
    (async () => {
      const u = await api<User>("/api/auth/me");
      setUser(u);
      if (u.role === "subscriber") {
        const s = await api<SubscriberSettings>("/api/settings/subscriber");
        setSub(s);
        setMultInput(parseFloat(s.multiplier).toString());
        setLimitInput(s.daily_loss_limit ?? "");
        setProfitInput(s.daily_profit_limit ?? "");
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
      notify.error(
        `Copy trading auto-paused — today's loss ($${e.todays_realized_pnl}) hit your daily limit ($${e.daily_loss_limit}).`,
        { autoClose: false }
      );
      api<SubscriberSettings>("/api/settings/subscriber").then(setSub);
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
      const body = { daily_loss_limit: trimmed === "" ? null : trimmed };
      const s = await api<SubscriberSettings>("/api/settings/subscriber/daily-loss-limit", {
        method: "PATCH", body: JSON.stringify(body),
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
  async function saveProfit() {
    setProfitBusy(true);
    try {
      const trimmed = profitInput.trim();
      const body = { daily_profit_limit: trimmed === "" ? null : trimmed };
      const s = await api<SubscriberSettings>("/api/settings/subscriber/daily-profit-limit", {
        method: "PATCH", body: JSON.stringify(body),
      });
      setSub(s);
      setProfitInput(s.daily_profit_limit ?? "");
      notify.success(s.daily_profit_limit ? `Daily profit limit set to $${s.daily_profit_limit}` : "Daily profit limit cleared");
    } catch (e) {
      notify.fromError(e, "Could not update daily profit limit");
    } finally {
      setProfitBusy(false);
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

  if (!user) return <p style={{color: "var(--muted)"}}>Loading…</p>;

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
  const limit = sub?.daily_loss_limit ? Number(sub.daily_loss_limit) : null;
  const headroom = limit !== null ? limit + todaysPnL : null;
  const limitPct = limit !== null && limit > 0 ? Math.min(100, Math.max(0, (-todaysPnL / limit) * 100)) : 0;
  const profitLimit = sub?.daily_profit_limit ? Number(sub.daily_profit_limit) : null;
  const profitHeadroom = profitLimit !== null ? profitLimit - Math.max(0, todaysPnL) : null;
  const profitPct = profitLimit !== null && profitLimit > 0
    ? Math.min(100, Math.max(0, (Math.max(0, todaysPnL) / profitLimit) * 100))
    : 0;

  const followedTrader = sub?.following_trader_id
    ? traders.find(t => t.id === sub.following_trader_id) ?? null
    : null;

  return (
    <div className="space-y-5 max-w-3xl pb-12">
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
                  <span className="text-xs tabular-nums whitespace-nowrap" style={{color: "var(--muted)"}}>
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

          {/* ── P&L Limit ───────────────────────────────────────────── */}
          {/* Two-column layout split by a vertical divider — no inner card
              borders so it reads as one cohesive section instead of cards
              inside a card. Stacks vertically on mobile with the divider
              becoming horizontal. */}
          <Card
            icon={<IconShield />}
            title="P&L Limit"
            hint="Copy turns OFF for the day when either limit is hit, then auto-resumes the next UTC day."
          >
            <div className="grid grid-cols-1 md:grid-cols-[1fr_1px_1fr] gap-4 md:gap-5">
              <PnLLimitPanel
                kind="loss"
                input={limitInput}
                onInput={setLimitInput}
                onSave={saveLimit}
                busy={limitBusy}
                current={sub.daily_loss_limit}
                todaysPnL={todaysPnL}
                limit={limit}
                pct={limitPct}
                headroom={headroom}
                fmt={fmt}
              />
              {/* Vertical divider on md+, horizontal on mobile */}
              <div
                aria-hidden
                className="hidden md:block w-px h-full"
                style={{background: "var(--border)"}}
              />
              <div
                aria-hidden
                className="md:hidden h-px w-full"
                style={{background: "var(--border)"}}
              />
              <PnLLimitPanel
                kind="profit"
                input={profitInput}
                onInput={setProfitInput}
                onSave={saveProfit}
                busy={profitBusy}
                current={sub.daily_profit_limit}
                todaysPnL={todaysPnL}
                limit={profitLimit}
                pct={profitPct}
                headroom={profitHeadroom}
                fmt={fmt}
              />
            </div>
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
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
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
          {hint && <p className="text-[11px] mt-1 leading-snug" style={{color: "var(--muted)"}}>{hint}</p>}
        </div>
      </header>
      <div className="px-4 py-3">{children}</div>
    </section>
  );
}

function Field({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-[10px] uppercase tracking-wider mb-1.5 font-medium" style={{color: "var(--muted)"}}>
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
      style={{borderColor: "var(--border)"}}
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
      style={{borderColor: "var(--border)"}}
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
        boxShadow: disabled ? "none" : "0 4px 14px -6px var(--accent)",
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
      <span style={{color: "var(--muted)"}}>{label}</span>
      {value !== undefined && (
        <strong style={{color: valueColor ?? "var(--text)"}}>{value}</strong>
      )}
    </span>
  );
}

function StatusItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-baseline gap-1.5 text-xs">
      <span className="text-[10px] uppercase tracking-wider" style={{color: "var(--muted)"}}>
        {label}
      </span>
      <span>{children}</span>
    </span>
  );
}

function Divider() {
  return <span className="h-3.5 w-px" style={{background: "var(--border)"}} aria-hidden />;
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wider font-medium" style={{color: "var(--muted)"}}>{label}</div>
      <div className="font-semibold mt-0.5 tabular-nums text-sm" style={{color: color ?? "var(--text)"}}>{value}</div>
    </div>
  );
}

/** One side of the P&L Limit card. No box around it — the parent card
 *  + a divider provide the visual grouping. The $-input is one cohesive
 *  control: a single bordered field with $ inline, focus ring on the
 *  whole thing (not just the inner input). */
function PnLLimitPanel({
  kind, input, onInput, onSave, busy, current, todaysPnL, limit, pct, headroom, fmt,
}: {
  kind: "loss" | "profit";
  input: string;
  onInput: (v: string) => void;
  onSave: () => void;
  busy: boolean;
  current: string | null;
  todaysPnL: number;
  limit: number | null;
  pct: number;
  headroom: number | null;
  fmt: (v: string | null | undefined) => string;
}) {
  const isLoss = kind === "loss";
  const title = isLoss ? "Daily loss limit" : "Daily profit limit";
  const subtitle = isLoss
    ? "Pause copy when today's loss reaches this."
    : "Pause copy when today's profit reaches this.";
  const barColor = pct >= 100 ? "var(--bad)" : pct >= 75 ? "#f59e0b" : "var(--good)";
  return (
    <div className="min-w-0 space-y-3">
      <div>
        <div className="text-sm font-semibold">{title}</div>
        <div className="text-[11px] mt-0.5" style={{color: "var(--muted)"}}>{subtitle}</div>
      </div>

      {/* One cohesive $-input: a single bordered field, with USD as a
          baked-in prefix and input as the body. Focus-within lights the
          whole shell. The inline style on <input> wipes the global
          `input[type="number"]` border + background from globals.css —
          without that override the inner input would render its OWN
          border inside the shell, creating a box-in-box look. */}
      <div className="flex items-center gap-2">
        <div
          className="flex-1 inline-flex items-center rounded-lg border overflow-hidden transition-colors focus-within:border-[var(--accent)]"
          style={{borderColor: "var(--border)", background: "rgba(0,0,0,0.18)"}}
        >
          <span
            className="px-3 py-2 text-xs font-medium border-r"
            style={{color: "var(--muted)", borderColor: "var(--border)"}}
          >
            USD
          </span>
          <input
            type="number" step="0.01" min="0" placeholder="no limit"
            className="flex-1 w-full px-3 py-2 text-sm tabular-nums"
            style={{
              border: "none",
              background: "transparent",
              outline: "none",
              borderRadius: 0,  // overrides globals.css var(--r-sm) inheritance
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

      <div className="grid grid-cols-3 gap-2">
        <Stat label="Today P&L" value={fmt(String(todaysPnL))}
              color={todaysPnL >= 0 ? "var(--good)" : "var(--bad)"} />
        <Stat label="Limit" value={fmt(current)} />
        <Stat
          label="Headroom"
          value={limit === null ? "—" : fmt(String(headroom))}
          color={(headroom ?? 1) > 0 ? "var(--text)" : "var(--bad)"}
        />
      </div>

      {limit !== null && (
        <div>
          <div className="h-1.5 rounded-full overflow-hidden" style={{background: "rgba(255,255,255,0.06)"}}>
            <div
              className="h-full rounded-full"
              style={{
                width: `${pct}%`,
                background: barColor,
                boxShadow: `0 0 8px -2px ${barColor}`,
                transition: "width 0.4s ease",
              }}
            />
          </div>
          <div className="text-[10px] mt-1 tabular-nums" style={{color: "var(--muted)"}}>
            {Math.round(pct)}% of limit
          </div>
        </div>
      )}
    </div>
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
          <p className="text-[11px] mt-0.5" style={{color: "var(--muted)"}}>{description}</p>
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
      style={{borderColor: "var(--border)", background: "rgba(0,0,0,0.15)"}}
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
            style={{color: "var(--text)", fontSize: "14px"}}
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
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
      <circle cx="9" cy="7" r="4"/>
      <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
      <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
    </svg>
  );
}
function IconShield() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
    </svg>
  );
}
function IconFilter() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>
    </svg>
  );
}
function IconRefresh() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="23 4 23 10 17 10"/>
      <polyline points="1 20 1 14 7 14"/>
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
    </svg>
  );
}
function IconPower() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18.36 6.64a9 9 0 1 1-12.73 0"/>
      <line x1="12" y1="2" x2="12" y2="12"/>
    </svg>
  );
}
