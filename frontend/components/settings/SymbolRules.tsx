"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { AdvancedScopeEditor } from "@/components/settings/AdvancedScopeEditor";
import { Spinner } from "@/components/Spinner";
import { ConfirmModal } from "@/components/ConfirmModal";

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


export function SymbolRules({ advanced }: { advanced: boolean }) {
  const [rules, setRules] = useState<SymbolRule[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [adding, setAdding] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);
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
    // The editor seeds itself from the rule's OWN values, so opening a row is
    // all this does. An inherited field stays empty there and shows what it
    // falls back to as its placeholder.
    setOpen(open === rule.symbol ? null : rule.symbol);
  }

  async function save(symbol: string, settings: Record<string, unknown>) {
    setBusy(symbol);
    try {
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
    } catch (e) {
      notify.fromError(e, `Could not add ${symbol}`);
    } finally {
      setBusy(null);
    }
  }

  async function remove(symbol: string) {
    setConfirming(symbol);
  }

  async function doRemove() {
    const symbol = confirming;
    if (!symbol) return;
    setBusy(symbol);
    try {
      await api(`${BASE}/${encodeURIComponent(symbol)}`, { method: "DELETE" });
      notify.success(symbol === ALL
        ? "Common settings removed"
        : `${symbol} is back on the ALL Symbols settings`);
      if (open === symbol) setOpen(null);
      setConfirming(null);
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

  async function addCommon() {
    setBusy(ALL);
    try {
      await api(`${BASE}/${ALL}`, { method: "PUT", body: JSON.stringify({ settings: {} }) });
      await load();
      setOpen(ALL);
    } catch (e) {
      notify.fromError(e, "Could not create the common settings");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-3">
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
        {[
          // The common layer first when it exists: it is what everything else
          // inherits from, so it reads top-down.
          ...(common ? [common] : []),
          ...rules.filter(r => r.symbol !== ALL),
        ].map(rule => {
          const custom = rule.overridden.length;
          return (
            <li
              key={rule.symbol}
              className="rounded-lg border overflow-hidden"
              style={{ borderColor: "var(--border)" }}
            >
              <div className="flex items-center gap-2 px-3 py-2">
                <span className="font-semibold text-sm tabular-nums">
                  {rule.symbol === ALL ? "ALL Symbols" : rule.symbol}
                </span>
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
                  className="px-3 py-3 border-t"
                  style={{ borderColor: "var(--border)", background: "rgba(0,0,0,0.12)" }}
                >
                  <AdvancedScopeEditor
                    scope={rule.symbol === ALL ? "ALL Symbols" : rule.symbol}
                    settings={rule.settings}
                    effective={rule.effective}
                    busy={busy === rule.symbol}
                    onSave={(next) => void save(rule.symbol, next)}
                  />
                </div>
              )}
            </li>
          );
        })}
      </ul>

      <ConfirmModal
        open={confirming !== null}
        busy={busy === confirming}
        title={confirming === ALL ? "Remove the common settings?" : `Remove ${confirming}?`}
        message={confirming === ALL
          // Removing the common layer is the one action here that reaches
          // every symbol, so it says so rather than naming one.
          ? "Every symbol without its own value goes back to your standard settings."
          : `${confirming} goes back to the ALL Symbols settings.`}
        confirmLabel="Remove"
        onConfirm={() => void doRemove()}
        onCancel={() => setConfirming(null)}
      />

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
