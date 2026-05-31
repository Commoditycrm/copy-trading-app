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

  // Auto-pause SSE → refresh sub.
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

  // Symbol-filter PATCH used by both chip lists. Optimistic update +
  // revert on error so the chip vanishes/reappears instantly.
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

  return (
    <div className="space-y-4 max-w-3xl">
      <h1 className="text-xl font-semibold">Settings</h1>

      {user.role === "subscriber" && sub && (
        <>
          {/* ── Following trader (inline) ─────────────────────────── */}
          <Card title="Following trader">
            <select
              value={sub.following_trader_id ?? ""}
              onChange={e => follow(e.target.value || null)}
              className="w-full px-2 py-1.5 text-sm rounded bg-transparent border"
              style={{borderColor: "var(--border)"}}
            >
              <option value="">— not following anyone —</option>
              {traders.map(t => (
                <option key={t.id} value={t.id}>{t.display_name ?? t.email}</option>
              ))}
            </select>
          </Card>

          {/* ── Multiplier + Daily Loss Limit (2-up) ────────────────── */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <Card title="Trade multiplier" hint="trader_qty × this. 0.1–10.">
              <div className="flex items-center gap-2">
                <input
                  type="number" step="0.1" min="0.1" max="10"
                  className="w-24 px-2 py-1.5 text-sm rounded bg-transparent border tabular-nums"
                  style={{borderColor: "var(--border)"}}
                  value={multInput}
                  onChange={(e) => setMultInput(e.target.value)}
                />
                <span className="text-xs" style={{color: "var(--muted)"}}>
                  current ×{fmtMultiplier(sub.multiplier)}
                </span>
                <button
                  onClick={saveMultiplier}
                  disabled={multBusy || parseFloat(multInput) === parseFloat(sub.multiplier)}
                  className="ml-auto px-3 py-1.5 text-xs rounded font-medium inline-flex items-center gap-1.5 disabled:opacity-40"
                  style={{background: "var(--accent)", color: "#06121f"}}
                >
                  {multBusy && <Spinner />}
                  Save
                </button>
              </div>
            </Card>

            <Card title="Daily loss limit" hint="copy turns OFF when hit. blank = disabled.">
              <div className="flex items-center gap-2">
                <span className="text-sm" style={{color: "var(--muted)"}}>$</span>
                <input
                  type="number" step="0.01" min="0" placeholder="no limit"
                  className="w-28 px-2 py-1.5 text-sm rounded bg-transparent border tabular-nums"
                  style={{borderColor: "var(--border)"}}
                  value={limitInput}
                  onChange={(e) => setLimitInput(e.target.value)}
                />
                <button
                  onClick={saveLimit}
                  disabled={limitBusy || limitInput === (sub.daily_loss_limit ?? "")}
                  className="ml-auto px-3 py-1.5 text-xs rounded font-medium inline-flex items-center gap-1.5 disabled:opacity-40"
                  style={{background: "var(--accent)", color: "#06121f"}}
                >
                  {limitBusy && <Spinner />}
                  Save
                </button>
              </div>

              {/* compact metric row: pnl / limit / headroom */}
              <div className="grid grid-cols-3 gap-2 mt-2 text-xs">
                <Stat label="Today P&L" value={fmt(sub.todays_realized_pnl)}
                      color={todaysPnL >= 0 ? "var(--good)" : "var(--bad)"} />
                <Stat label="Limit" value={fmt(sub.daily_loss_limit)} />
                <Stat label="Headroom" value={limit === null ? "—" : fmt(String(headroom))}
                      color={(headroom ?? 1) > 0 ? "var(--text)" : "var(--bad)"} />
              </div>
              {limit !== null && (
                <div className="h-1 mt-2 rounded overflow-hidden" style={{background: "var(--border)"}}>
                  <div style={{
                    width: `${limitPct}%`, height: "100%",
                    background: limitPct >= 100 ? "var(--bad)" : limitPct >= 75 ? "#f59e0b" : "var(--good)",
                    transition: "width 0.3s ease",
                  }} />
                </div>
              )}
            </Card>
          </div>

          {/* ── Retry policy (2-up, inline) ─────────────────────────── */}
          <Card title="Retry mirror orders on broker errors" hint="for transient (network/5xx/rate-limit) failures only.">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <RetrySelect
                label="Opening positions"
                value={sub.retry_interval_open}
                onChange={(v) => setRetryInterval("open", v)}
              />
              <RetrySelect
                label="Closing positions"
                value={sub.retry_interval_close}
                onChange={(v) => setRetryInterval("close", v)}
              />
            </div>
          </Card>

          {/* ── Symbol filters (the new feature) ────────────────────── */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <Card
              title="Trade exclusion list"
              hint="trades on these symbols are NOT copied to you."
            >
              <ChipInput
                symbols={sub.symbol_exclusion_list}
                onChange={(next) => saveSymbolFilter("symbol_exclusion_list", next)}
                placeholder="e.g. TSLA — Enter or comma to add"
                accent="var(--bad)"
              />
            </Card>
            <Card
              title="Trade inclusion list"
              hint="when non-empty, ONLY these symbols are copied. Empty = copy everything."
            >
              <ChipInput
                symbols={sub.symbol_inclusion_list}
                onChange={(next) => saveSymbolFilter("symbol_inclusion_list", next)}
                placeholder="e.g. AAPL — Enter or comma to add"
                accent="var(--good)"
              />
            </Card>
          </div>
        </>
      )}

      {user.role === "trader" && trd && (
        <Card title="Master trading switch"
              hint="when OFF, the platform refuses to place new orders (yours and any subscriber mirrors).">
          <div className="flex items-center justify-between">
            <div className="text-xs" style={{color: "var(--muted)"}}>
              State: <strong style={{color: trd.trading_enabled ? "var(--good)" : "var(--bad)"}}>
                {trd.trading_enabled ? "ON" : "OFF"}
              </strong>
            </div>
            <button
              onClick={() => toggleTrading(!trd.trading_enabled)}
              className="px-3 py-1.5 text-xs rounded font-medium"
              style={{background: trd.trading_enabled ? "var(--good)" : "var(--border)", color: trd.trading_enabled ? "#06121f" : "var(--text)"}}
            >
              {trd.trading_enabled ? "Turn OFF" : "Turn ON"}
            </button>
          </div>
        </Card>
      )}
    </div>
  );
}

// ── Compact reusable building blocks ────────────────────────────────────

function Card({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section
      className="p-3 rounded-lg border space-y-2"
      style={{borderColor: "var(--border)", background: "var(--panel)"}}
    >
      <header>
        <h2 className="text-sm font-semibold leading-tight">{title}</h2>
        {hint && <p className="text-[11px] mt-0.5" style={{color: "var(--muted)"}}>{hint}</p>}
      </header>
      {children}
    </section>
  );
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wider" style={{color: "var(--muted)"}}>{label}</div>
      <div className="font-medium mt-0.5 tabular-nums" style={{color: color ?? "var(--text)"}}>{value}</div>
    </div>
  );
}

function RetrySelect({ label, value, onChange }: {
  label: string; value: RetryInterval; onChange: (v: RetryInterval) => void;
}) {
  return (
    <div>
      <label className="block text-[11px] mb-1" style={{color: "var(--muted)"}}>{label}</label>
      <select
        className="w-full px-2 py-1.5 text-sm rounded border bg-transparent"
        style={{borderColor: "var(--border)"}}
        value={value}
        onChange={(e) => onChange(e.target.value as RetryInterval)}
      >
        {RETRY_OPTIONS.map(opt => (
          <option key={opt.value} value={opt.value}>{opt.label}</option>
        ))}
      </select>
    </div>
  );
}

/** Chip-style symbol input. Add via Enter or comma. Backspace on empty
 *  input removes the last chip. PATCH-on-every-mutation through the
 *  onChange callback (parent does the API call + revert-on-error). */
function ChipInput({ symbols, onChange, placeholder, accent }: {
  symbols: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  accent: string;
}) {
  const [draft, setDraft] = useState("");

  // Dedupe + uppercase on entry so we never persist garbage chips.
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

  return (
    <div
      className="rounded border px-1.5 py-1 flex flex-wrap items-center gap-1 min-h-[34px]"
      style={{borderColor: "var(--border)", background: "rgba(255,255,255,0.02)"}}
      onClick={(e) => {
        // Click anywhere in the box → focus the input.
        const inp = (e.currentTarget.querySelector("input") as HTMLInputElement | null);
        inp?.focus();
      }}
    >
      {symbols.map(sym => (
        <span
          key={sym}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[11px] rounded"
          style={{
            background: "rgba(255,255,255,0.06)",
            border: `1px solid ${accent}`,
            color: accent,
            fontWeight: 600,
          }}
        >
          {sym}
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); remove(sym); }}
            aria-label={`Remove ${sym}`}
            className="opacity-70 hover:opacity-100"
            style={{color: accent}}
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
          // Auto-commit when user types a delimiter so chips form as you type.
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
        className="flex-1 min-w-[120px] px-1.5 py-1 text-xs bg-transparent outline-none"
        style={{color: "var(--text)"}}
      />
    </div>
  );
}
