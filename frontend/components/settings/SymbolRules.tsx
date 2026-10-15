"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { PercentInput } from "@/components/PercentInput";
import { Spinner } from "@/components/Spinner";

/**
 * Per-symbol copy settings — "AAPL is different".
 *
 * A subscriber has one set of rules for everything they mirror. A symbol rule
 * overrides only what it names and inherits the rest, so the editor is built
 * around that: every field shows the inherited value as its placeholder, and
 * typing into it is what makes the field custom. Clearing it hands the field
 * back to the global settings.
 *
 * That is why the rows say "3 custom" rather than listing values — the useful
 * question is which symbols differ from the defaults, not what every number is.
 *
 * Scope note shown to the user: a rule is keyed on the underlying root, so
 * AAPL covers AAPL shares and every AAPL option.
 */

type SymbolRule = {
  symbol: string;
  enabled: boolean;
  overridden: string[];
  settings: Record<string, unknown>;
  effective: Record<string, unknown>;
  exit_mode: "alerts" | "ladder";
};

const BASE = "/api/settings/subscriber/symbols";

/** The common layer. A real ticker is never "ALL", so it doubles as the scope
 *  name and needs no separate storage. */
const ALL = "ALL";

/** The sizing fields, in the order they read. */
const SIZING: { key: string; label: string; hint: string; step: number }[] = [
  { key: "multiplier", label: "Multiplier", step: 0.05,
    hint: "Applies to the next new position — an open one keeps the multiplier it was opened at." },
  { key: "max_per_contract", label: "Max per contract ($)", step: 10,
    hint: "Options only. Skips a mirror whose premium exceeds this." },
  { key: "max_per_order", label: "Max per order ($)", step: 10,
    hint: "Shrinks an opening mirror to fit, or skips it." },
];

/** One trim's three numbers, in the order they read. */
const TRIMS = [
  { n: 1, gate: "discord_trim_profit_gate_pct", stop: "discord_trim_stop_pct", qty: "discord_trim_qty_pct" },
  { n: 2, gate: "discord_trim2_profit_gate_pct", stop: "discord_trim2_stop_pct", qty: "discord_trim2_qty_pct" },
  { n: 3, gate: "discord_trim3_profit_gate_pct", stop: "discord_trim3_stop_pct", qty: "discord_trim3_qty_pct" },
];

const LADDER_KEYS = [
  ...TRIMS.flatMap(t => [t.gate, t.stop, t.qty]),
  "discord_fill_stop_pct",
];

const AUTO_TRIM = "discord_auto_trim";

function asText(v: unknown): string {
  return v === null || v === undefined ? "" : String(v);
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <span className="text-[11px] self-center whitespace-nowrap" style={{ color: "var(--muted)" }}>
        {label}
      </span>
      {children}
    </>
  );
}


export function SymbolRules({ advanced, onModeChange }: {
  advanced: boolean;
  onModeChange: (next: boolean) => void;
}) {
  const [rules, setRules] = useState<SymbolRule[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [exitMode, setExitMode] = useState<"alerts" | "ladder">("alerts");
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  async function load() {
    try {
      const r = await api<{ symbols: SymbolRule[] }>(BASE);
      setRules(r.symbols);
    } catch (e) {
      notify.fromError(e, "Could not load symbol settings");
      setRules([]);
    }
  }

  useEffect(() => { void load(); }, []);

  function edit(rule: SymbolRule) {
    if (open === rule.symbol) { setOpen(null); return; }
    // Seed from the OVERRIDES, not the effective values: an inherited field
    // must start empty so its placeholder shows what it falls back to.
    const next: Record<string, string> = {};
    for (const f of SIZING) next[f.key] = asText(rule.settings[f.key]);
    for (const k of LADDER_KEYS) next[k] = asText(rule.settings[k]);
    setDraft(next);
    setExitMode(rule.exit_mode);
    setOpen(rule.symbol);
  }

  async function save(symbol: string) {
    setBusy(symbol);
    try {
      // Only non-empty fields are sent. An empty box means "inherit", and the
      // whole-settings PUT is what lets that clear a previous override.
      const settings: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(draft)) {
        if (v.trim() !== "") settings[k] = v.trim();
      }
      // Only stored when the symbol runs its own ladder. Absent means "On
      // Alerts", which is also what every symbol without a rule does — so the
      // default costs no stored value.
      if (exitMode === "ladder") settings[AUTO_TRIM] = true;
      await api(`${BASE}/${encodeURIComponent(symbol)}`, {
        method: "PUT",
        body: JSON.stringify({ settings }),
      });
      notify.success(`${symbol} settings saved`);
      setOpen(null);
      await load();
    } catch (e) {
      notify.fromError(e, `Could not save ${symbol}`);
    } finally {
      setBusy(null);
    }
  }

  async function add() {
    const symbol = adding.trim().toUpperCase();
    if (!symbol) return;
    if (rules?.some(r => r.symbol === symbol)) {
      notify.info(`${symbol} already has settings`);
      setAdding("");
      return;
    }
    setBusy(symbol);
    try {
      await api(`${BASE}/${encodeURIComponent(symbol)}`, {
        method: "PUT",
        body: JSON.stringify({ settings: {} }),
      });
      setAdding("");
      await load();
      setOpen(symbol);
      setDraft(Object.fromEntries(SIZING.map(f => [f.key, ""])));
    } catch (e) {
      notify.fromError(e, `Could not add ${symbol}`);
    } finally {
      setBusy(null);
    }
  }

  async function remove(symbol: string) {
    if (!confirm(`Remove the custom settings for ${symbol}? It goes back to your default settings.`)) return;
    setBusy(symbol);
    try {
      await api(`${BASE}/${encodeURIComponent(symbol)}`, { method: "DELETE" });
      notify.success(`${symbol} is back on your default settings`);
      if (open === symbol) setOpen(null);
      await load();
    } catch (e) {
      notify.fromError(e, `Could not remove ${symbol}`);
    } finally {
      setBusy(null);
    }
  }

  if (rules === null) {
    return <div className="py-6 grid place-items-center"><Spinner /></div>;
  }

  const common = rules.find(r => r.symbol === ALL) ?? null;

  async function setConfigMode(next: boolean) {
    setBusy("__mode__");
    try {
      await api(`${BASE}/mode`, { method: "PATCH", body: JSON.stringify({ enabled: next }) });
      onModeChange(next);
      notify.success(next ? "Advanced settings on" : "Back to risk controls");
      await load();
    } catch (e) {
      notify.fromError(e, "Could not switch mode");
    } finally {
      setBusy(null);
    }
  }

  async function addCommon() {
    setBusy(ALL);
    try {
      await api(`${BASE}/${ALL}`, { method: "PUT", body: JSON.stringify({ settings: {} }) });
      await load();
      setOpen(ALL);
      setDraft({});
      setExitMode("alerts");
    } catch (e) {
      notify.fromError(e, "Could not create the common settings");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-3">
      {/* ── Which mode is live ───────────────────────────────────────────
          One or the other, never both. The inactive one says WHY it is
          inactive rather than just greying out. */}
      <div className="flex flex-col gap-1.5">
        {([
          [false, "Risk controls",
           "Your settings above decide the size of every copied trade."],
          [true, "Advanced settings",
           "Per-symbol rules decide the size instead, and can run their own exit ladder. Your multiplier and the two caps above stop applying; daily limits and auto-liquidation keep working."],
        ] as const).map(([value, label, hint]) => (
          <label key={String(value)} className="flex items-start gap-2 cursor-pointer">
            <input
              type="radio"
              name="subscriber-settings-mode"
              checked={advanced === value}
              disabled={busy === "__mode__"}
              onChange={() => void setConfigMode(value)}
              className="mt-0.5"
            />
            <span>
              <span className="text-xs block">{label}</span>
              <span className="text-[10px] block leading-snug" style={{ color: "var(--muted)" }}>
                {hint}
              </span>
            </span>
          </label>
        ))}
      </div>

      {!advanced && (
        <div
          className="rounded-lg border border-dashed px-3 py-3 text-[11px] leading-snug"
          style={{ borderColor: "var(--border)", color: "var(--muted)" }}
        >
          Advanced settings are off, so nothing here applies — your risk
          controls above are what run.
          {rules.length > 0 && " Your saved symbol rules are kept and will apply again if you turn this back on."}
        </div>
      )}

      {advanced && (
        <>
          <p className="text-[11px] leading-snug" style={{ color: "var(--muted)" }}>
            <strong style={{ color: "var(--text)" }}>ALL Symbols</strong> is the
            common configuration — it applies to everything you copy. Add an
            individual symbol only where it should differ; it inherits anything
            it does not set. A symbol covers its stock and every option on it.
          </p>

          {/* The common layer, always first — it is what most subscribers
              will set and the thing everything else inherits from. */}
          {common === null ? (
            <button
              onClick={() => void addCommon()}
              disabled={busy === ALL}
              className="w-full rounded-lg border border-dashed px-3 py-3 text-xs"
              style={{ borderColor: "var(--border)", color: "var(--muted)" }}
            >
              + Set common settings for ALL symbols
            </button>
          ) : null}
        </>
      )}

      {rules.filter(r => r.symbol !== ALL).length === 0 && (
        <div
          className="rounded-lg border border-dashed px-3 py-4 text-xs text-center"
          style={{ borderColor: "var(--border)", color: "var(--muted)" }}
        >
          No individual symbols yet — they all follow ALL Symbols.
        </div>
      )}

      <ul className="space-y-2">
        {rules.filter(r => r.symbol !== ALL).map(rule => {
          const custom = rule.overridden.length;
          return (
            <li
              key={rule.symbol}
              className="rounded-lg border overflow-hidden"
              style={{ borderColor: "var(--border)" }}
            >
              <div className="flex items-center gap-2 px-3 py-2">
                <span className="font-semibold text-sm tabular-nums">{rule.symbol}</span>
                <span
                  className="text-[10px] px-1.5 py-0.5 rounded"
                  style={{
                    background: custom ? "rgba(255,255,255,0.06)" : "transparent",
                    color: custom ? "var(--text)" : "var(--muted)",
                    border: custom ? "none" : "1px solid var(--border)",
                  }}
                >
                  {custom ? `${custom} custom` : "using defaults"}
                </span>
                <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                  {rule.exit_mode === "ladder" ? "own exit ladder" : "exits follow the trader"}
                </span>
                <span className="flex-1" />
                <button
                  onClick={() => edit(rule)}
                  className="text-xs px-2 py-1 rounded-md border"
                  style={{ borderColor: "var(--border)" }}
                >
                  {open === rule.symbol ? "Close" : "Edit"}
                </button>
                <button
                  onClick={() => void remove(rule.symbol)}
                  disabled={busy === rule.symbol}
                  className="btn-danger-soft text-xs px-2 py-1"
                >
                  Remove
                </button>
              </div>

              {open === rule.symbol && (
                <div
                  className="px-3 py-3 border-t grid gap-3 sm:grid-cols-3"
                  style={{ borderColor: "var(--border)", background: "rgba(0,0,0,0.12)" }}
                >
                  {SIZING.map(f => (
                    <label key={f.key} className="block">
                      <span className="text-[11px] block mb-1" style={{ color: "var(--muted)" }}>
                        {f.label}
                      </span>
                      <PercentInput
                        step={f.step}
                        min={0}
                        value={draft[f.key] ?? ""}
                        // The inherited value as the placeholder: the box reads
                        // as "this is what you get unless you type something".
                        placeholder={asText(rule.effective[f.key]) || "default"}
                        aria-label={`${rule.symbol} ${f.label}`}
                        onChange={(e) => setDraft(d => ({ ...d, [f.key]: e.target.value }))}
                        className="w-full px-3 py-2 text-sm rounded-lg bg-transparent border tabular-nums focus:outline-none focus:border-[var(--accent)]"
                        style={{ borderColor: "var(--border)" }}
                      />
                      <span className="text-[10px] block mt-1 leading-snug" style={{ color: "var(--muted)" }}>
                        {f.hint}
                      </span>
                    </label>
                  ))}
                  {/* ── How this symbol gets out ─────────────────────────
                      Two choices, and the ladder only matters under the
                      second — so the rungs are hidden until it is picked,
                      rather than shown greyed out and ignored. */}
                  <div className="sm:col-span-3 pt-1 border-t" style={{ borderColor: "var(--border)" }}>
                    <span className="text-[11px] block mt-2 mb-1.5" style={{ color: "var(--muted)" }}>
                      Exits for {rule.symbol}
                    </span>
                    <div className="flex flex-col gap-1.5">
                      {([
                        ["alerts", "On alerts",
                         "Sell when the trader sells — what every symbol does by default."],
                        ["ladder", "Pre-defined ladder targets",
                         "This symbol's own targets fire on their own. The trader's trims are not copied for it; their full close still is."],
                      ] as const).map(([value, label, hint]) => (
                        <label key={value} className="flex items-start gap-2 cursor-pointer">
                          <input
                            type="radio"
                            name={`exit-${rule.symbol}`}
                            checked={exitMode === value}
                            onChange={() => setExitMode(value)}
                            className="mt-0.5"
                          />
                          <span>
                            <span className="text-xs block">{label}</span>
                            <span className="text-[10px] block leading-snug" style={{ color: "var(--muted)" }}>
                              {hint}
                            </span>
                          </span>
                        </label>
                      ))}
                    </div>
                  </div>

                  {exitMode === "ladder" && (
                    <div className="sm:col-span-3">
                      <div className="grid gap-2" style={{ gridTemplateColumns: "auto 1fr 1fr 1fr" }}>
                        <span />
                        <span className="text-[10px]" style={{ color: "var(--muted)" }}>Target %</span>
                        <span className="text-[10px]" style={{ color: "var(--muted)" }}>Stop %</span>
                        <span className="text-[10px]" style={{ color: "var(--muted)" }}>Sell %</span>
                        {TRIMS.map(t => (
                          <Row key={t.n} label={`Trim ${t.n}`}>
                            {[t.gate, t.stop, t.qty].map(key => (
                              <PercentInput
                                key={key}
                                step={5}
                                value={draft[key] ?? ""}
                                placeholder={asText(rule.effective[key]) || "—"}
                                aria-label={`${rule.symbol} trim ${t.n} ${key}`}
                                onChange={(e) => setDraft(d => ({ ...d, [key]: e.target.value }))}
                                className="w-full px-2 py-1.5 text-sm rounded-lg bg-transparent border tabular-nums focus:outline-none focus:border-[var(--accent)]"
                                style={{ borderColor: "var(--border)" }}
                              />
                            ))}
                          </Row>
                        ))}
                        <Row label="On fill stop">
                          <PercentInput
                            step={5}
                            value={draft["discord_fill_stop_pct"] ?? ""}
                            placeholder={asText(rule.effective["discord_fill_stop_pct"]) || "none"}
                            aria-label={`${rule.symbol} on fill stop`}
                            onChange={(e) => setDraft(d => ({ ...d, discord_fill_stop_pct: e.target.value }))}
                            className="w-full px-2 py-1.5 text-sm rounded-lg bg-transparent border tabular-nums focus:outline-none focus:border-[var(--accent)]"
                            style={{ borderColor: "var(--border)" }}
                          />
                          <span className="text-[10px] self-center sm:col-span-2" style={{ color: "var(--muted)" }}>
                            A stop set the moment the position fills. Negative is below your cost — e.g. -25.
                          </span>
                        </Row>
                      </div>
                      <p className="text-[10px] mt-2 leading-snug" style={{ color: "var(--muted)" }}>
                        Target is the profit that fires the trim; stop is where the
                        rest sits afterwards (0 is break-even); sell is how much of
                        what is left goes. Blank uses the default.
                      </p>
                    </div>
                  )}

                  <div className="sm:col-span-3 flex items-center gap-2">
                    <button
                      onClick={() => void save(rule.symbol)}
                      disabled={busy === rule.symbol}
                      className="rounded-md px-3 py-1.5 text-xs font-semibold text-white"
                      style={{ background: "var(--accent)" }}
                    >
                      {busy === rule.symbol ? "Saving…" : "Save"}
                    </button>
                    <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                      Clear a box to go back to your default for that field.
                    </span>
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>

      <div className="flex items-center gap-2 pt-1">
        <input
          value={adding}
          onChange={(e) => setAdding(e.target.value.toUpperCase())}
          onKeyDown={(e) => { if (e.key === "Enter") void add(); }}
          placeholder="Add a symbol — e.g. AAPL"
          aria-label="Add a symbol"
          maxLength={16}
          className="flex-1 min-w-0 px-3 py-2 text-sm rounded-lg bg-transparent border uppercase focus:outline-none focus:border-[var(--accent)]"
          style={{ borderColor: "var(--border)" }}
        />
        <button
          onClick={() => void add()}
          disabled={!adding.trim() || busy !== null}
          className="rounded-md px-3 py-2 text-xs font-semibold text-white disabled:opacity-50"
          style={{ background: "var(--accent)" }}
        >
          Add
        </button>
      </div>
    </div>
  );
}
