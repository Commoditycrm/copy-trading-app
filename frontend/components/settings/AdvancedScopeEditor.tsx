"use client";

import { useState } from "react";
import { PercentInput } from "@/components/PercentInput";

/**
 * Sizing + exit ladder for ONE scope — ALL Symbols, or a single symbol.
 *
 * Deliberately the same shape and the same stored fields as the Discord
 * channel settings, so a subscriber's rules are read by the very same
 * services: discord_channel_settings.active_sizing for the sizing contract and
 * discord_ladder for the rungs. Nothing here reinterprets what a setting
 * means; it only edits the values those functions read.
 *
 * Storage, matching discord_ladder:
 *   trims 1-3   named columns (discord_trim*_profit_gate_pct / _stop_pct / _qty_pct)
 *   trims 4+    discord_extra_trims: [{profit_gate_pct, stop_pct, qty_pct}]
 *   how many    discord_trim_count
 *   which trail discord_stop_trails: { fill: bool, trims: bool[] }
 *
 * Every box is sparse: empty means "inherit", which is what lets a symbol
 * override one value and take the rest from ALL Symbols.
 */

type Values = Record<string, unknown>;

const MAX_TRIMS = 6;
const CONTRACT_CHIPS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
const DEFAULT_SIZE_MODE = "dollars";

/** The three columns of trims 1-3, in order. */
const NAMED = [
  ["discord_trim_profit_gate_pct", "discord_trim_stop_pct", "discord_trim_qty_pct"],
  ["discord_trim2_profit_gate_pct", "discord_trim2_stop_pct", "discord_trim2_qty_pct"],
  ["discord_trim3_profit_gate_pct", "discord_trim3_stop_pct", "discord_trim3_qty_pct"],
];

type Trim = { gate: string; qty: string; stop: string; trail: boolean };

function text(v: unknown): string {
  return v === null || v === undefined ? "" : String(v);
}

/** The stored fields -> the rows the table edits. */
function toTrims(v: Values): Trim[] {
  const trails = (v.discord_stop_trails as { trims?: boolean[] } | undefined)?.trims ?? [];
  const rows: Trim[] = NAMED.map(([gate, stop, qty], i) => ({
    gate: text(v[gate]), stop: text(v[stop]), qty: text(v[qty]), trail: !!trails[i],
  }));
  for (const [i, extra] of ((v.discord_extra_trims as Values[]) ?? []).entries()) {
    rows.push({
      gate: text(extra?.profit_gate_pct), stop: text(extra?.stop_pct),
      qty: text(extra?.qty_pct), trail: !!trails[3 + i],
    });
  }
  const count = Number(v.discord_trim_count ?? 3) || 3;
  return rows.slice(0, Math.max(1, Math.min(MAX_TRIMS, count)));
}

/** The rows back into stored fields. Only non-empty values are written. */
function fromTrims(rows: Trim[], fillTrail: boolean): Values {
  const out: Values = { discord_trim_count: rows.length };
  rows.slice(0, 3).forEach((r, i) => {
    const [gate, stop, qty] = NAMED[i];
    if (r.gate.trim()) out[gate] = r.gate.trim();
    if (r.stop.trim()) out[stop] = r.stop.trim();
    if (r.qty.trim()) out[qty] = r.qty.trim();
  });
  if (rows.length > 3) {
    out.discord_extra_trims = rows.slice(3).map(r => ({
      profit_gate_pct: r.gate.trim() || null,
      stop_pct: r.stop.trim() || null,
      qty_pct: r.qty.trim() || null,
    }));
  }
  out.discord_stop_trails = { fill: fillTrail, trims: rows.map(r => r.trail) };
  return out;
}

function Segmented({ options, value, onChange, label }: {
  options: readonly (readonly [string, string])[];
  value: string;
  onChange: (v: string) => void;
  label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="flex rounded-lg border overflow-hidden"
         style={{ borderColor: "var(--border)" }}>
      {options.map(([v, t]) => (
        <button
          key={v}
          type="button"
          role="radio"
          aria-checked={value === v}
          onClick={() => onChange(v)}
          className="px-2.5 py-1 text-[11px] transition-colors"
          style={{
            background: value === v ? "var(--accent-glow)" : "transparent",
            color: value === v ? "var(--accent-2)" : "var(--muted)",
          }}
        >
          {t}
        </button>
      ))}
    </div>
  );
}

function Box({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl px-4 py-3 flex-1 basis-full sm:basis-0 min-w-0"
         style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}>
      {children}
    </div>
  );
}

const NUM = "w-full px-2 py-1.5 text-sm rounded-lg bg-transparent border tabular-nums text-right focus:outline-none focus:border-[var(--accent)]";

export function AdvancedScopeEditor({ scope, settings, effective, busy, onSave }: {
  scope: string;
  settings: Values;
  effective: Values;
  busy: boolean;
  onSave: (next: Values) => void;
}) {
  const [v, setV] = useState<Values>({ ...settings });
  const [trims, setTrims] = useState<Trim[]>(() => toTrims(settings));
  const [fillTrail, setFillTrail] = useState<boolean>(
    () => !!(settings.discord_stop_trails as { fill?: boolean } | undefined)?.fill);

  // Dollars by default: an amount per entry is what most subscribers want, and
  // it is the one that does not need a contract count chosen first.
  const sizeMode = String(v.discord_size_mode ?? effective.discord_size_mode ?? DEFAULT_SIZE_MODE);
  const cap = String(v.discord_size_cap ?? effective.discord_size_cap ?? "none");
  const contracts = Number(v.discord_quantity_multiplier ?? effective.discord_quantity_multiplier ?? 1);


  function set(k: string, value: unknown) {
    setV(prev => ({ ...prev, [k]: value }));
  }

  function editTrim(i: number, patch: Partial<Trim>) {
    setTrims(rows => rows.map((r, n) => (n === i ? { ...r, ...patch } : r)));
  }

  function save() {
    const next: Values = {
      ...v,
      // Every advanced scope runs its own ladder — there is no mode to choose.
      // Harmless when no targets are set: a trim with a target of 0 never
      // fires, and the backend only lets a ladder take over the trader's exits
      // once it has a target that can.
      discord_auto_trim: true,
      // The mode shown is the mode stored. Leaving it out would save the
      // amount a subscriber typed under the OTHER mode, and the entry would be
      // sized by a contract count they never chose.
      discord_size_mode: sizeMode,
      ...fromTrims(trims, fillTrail),
    };
    // Strip anything the subscriber left empty — absent means "inherit".
    for (const [k, value] of Object.entries(next)) {
      if (value === "" || value === null || value === undefined) delete next[k];
    }
    onSave(next);
  }

  return (
    <div className="space-y-4">
      {/* ── SIZING ─────────────────────────────────────────────────────── */}
      <div>
        <div className="text-[10px] tracking-wide mb-2" style={{ color: "var(--muted)" }}>SIZING</div>
        <div className="flex flex-wrap gap-3">
          <Box>
            <div className="flex items-center justify-between gap-2">
              <label className="text-[11px] font-medium" style={{ color: "var(--text-2)" }}>
                Size each entry by
              </label>
              <Segmented
                label="Size each entry by"
                value={sizeMode}
                onChange={(m) => set("discord_size_mode", m)}
                options={[["contracts", "Contracts"], ["dollars", "Dollars"]]}
              />
            </div>
            {sizeMode === "contracts" ? (
              <>
                <div className="flex flex-wrap items-center gap-1.5 mt-2.5">
                  {CONTRACT_CHIPS.map(n => (
                    <button
                      key={n}
                      type="button"
                      onClick={() => set("discord_quantity_multiplier", n)}
                      className="w-8 h-8 rounded-lg text-xs tabular-nums border transition-colors"
                      style={{
                        borderColor: contracts === n ? "var(--accent)" : "var(--border)",
                        background: contracts === n ? "var(--accent-glow)" : "transparent",
                        color: contracts === n ? "var(--accent-2)" : "var(--text)",
                      }}
                    >
                      {n}
                    </button>
                  ))}
                  <span className="text-[11px] ml-1" style={{ color: "var(--accent-2)" }}>
                    {contracts} contract{contracts === 1 ? "" : "s"}
                  </span>
                </div>
                <p className="text-[11px] mt-2 leading-snug" style={{ color: "var(--muted)" }}>
                  Every entry buys exactly this many, whatever size the trader
                  took. A close always sells the position you hold.
                </p>
              </>
            ) : (
              <>
                <div className="flex items-center gap-2 mt-2.5">
                  <span className="text-xs" style={{ color: "var(--muted)" }}>$</span>
                  <PercentInput
                    step={50}
                    min={0}
                    value={text(v.discord_size_dollars)}
                    placeholder={text(effective.discord_size_dollars) || "amount per entry"}
                    aria-label={`${scope} dollars per entry`}
                    onChange={(e) => set("discord_size_dollars", e.target.value)}
                    className={NUM}
                    style={{ borderColor: "var(--border)" }}
                  />
                </div>
                <p className="text-[11px] mt-2 leading-snug" style={{ color: "var(--muted)" }}>
                  Buys as many whole contracts as fit in this amount at the price
                  the entry will pay.
                </p>
              </>
            )}
          </Box>

          <Box>
            <div className="flex items-center justify-between gap-2">
              <label className="text-[11px] font-medium" style={{ color: "var(--text-2)" }}>Cap</label>
              <Segmented
                label="Cap"
                value={cap}
                onChange={(c) => set("discord_size_cap", c)}
                options={[["none", "None"], ["per_contract", "Per contract"], ["per_order", "Per order"]]}
              />
            </div>
            {cap !== "none" && (
              <div className="flex items-center gap-2 mt-2.5">
                <span className="text-xs" style={{ color: "var(--muted)" }}>$</span>
                <PercentInput
                  step={100}
                  min={0}
                  value={text(v[cap === "per_contract" ? "discord_max_per_contract" : "discord_max_per_order"])}
                  placeholder={text(effective[cap === "per_contract" ? "discord_max_per_contract" : "discord_max_per_order"]) || "no limit"}
                  aria-label={`${scope} cap amount`}
                  onChange={(e) => set(cap === "per_contract" ? "discord_max_per_contract" : "discord_max_per_order", e.target.value)}
                  className={NUM}
                  style={{ borderColor: "var(--border)" }}
                />
              </div>
            )}
            <p className="text-[11px] mt-2 leading-snug" style={{ color: "var(--muted)" }}>
              {cap === "per_contract"
                ? "Skips an entry whose single contract costs more than this (premium × 100)."
                : cap === "per_order"
                ? "Cuts an entry to the most contracts that fit under this total; skips it if not even one fits. Closes always go through."
                : "No cap — the size above is what goes."}
            </p>
          </Box>
        </div>
      </div>

      {/* ── EXIT LADDER ────────────────────────────────────────────────── */}
      <div className="rounded-xl px-4 py-3"
           style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}>
        <div className="flex items-center justify-between gap-2 mb-2.5">
          <span className="text-xs font-semibold">Exit ladder</span>
          <span className="text-[10px]" style={{ color: "var(--muted)" }}>measured from entry price</span>
        </div>

        {/* The rungs. Column headings match what each number means. */}
        <div className="mt-3 overflow-x-auto">
          <div className="grid gap-2 min-w-[560px]"
               style={{ gridTemplateColumns: "72px 1fr 1fr 104px 1fr 28px" }}>
            <span />
            <span className="text-[10px] text-center" style={{ color: "var(--muted)" }}>PROFIT TARGET</span>
            <span className="text-[10px] text-center" style={{ color: "var(--muted)" }}>TRIM OF REM. QTY</span>
            <span />
            <span className="text-[10px] text-center" style={{ color: "var(--muted)" }}>STOP / TRAILING STOP</span>
            <span />

            {/* On Fill — a stop only: there is nothing to trim at the fill. */}
            <span className="text-[11px] self-center" style={{ color: "var(--muted)" }}>On Fill</span>
            <span /><span />
            <Segmented
              label="On fill stop kind"
              value={fillTrail ? "trail" : "stop"}
              onChange={(k) => setFillTrail(k === "trail")}
              options={[["stop", "Stop"], ["trail", "Trail"]]}
            />
            <PercentInput
              step={5}
              value={text(v.discord_fill_stop_pct)}
              placeholder={text(effective.discord_fill_stop_pct) || "none"}
              aria-label={`${scope} on fill stop`}
              onChange={(e) => set("discord_fill_stop_pct", e.target.value)}
              className={NUM}
              style={{ borderColor: "var(--border)" }}
            />
            <span />

            {trims.map((t, i) => (
              <Row key={i} index={i} trim={t} scope={scope}
                   onEdit={(patch) => editTrim(i, patch)}
                   onRemove={trims.length > 1 ? () => setTrims(rows => rows.filter((_, n) => n !== i)) : undefined} />
            ))}
          </div>
        </div>

        <div className="flex items-center gap-3 mt-2.5">
          <button
            type="button"
            disabled={trims.length >= MAX_TRIMS}
            onClick={() => setTrims(rows => [...rows, { gate: "", qty: "100", stop: "0", trail: false }])}
            className="rounded-md border px-2.5 py-1 text-[11px] disabled:opacity-40"
            style={{ borderColor: "var(--border)" }}
          >
            + Add trim
          </button>
          <span className="text-[10px]" style={{ color: "var(--muted)" }}>
            If the last trim is not 100% it rounds down and leaves a runner.
          </span>
        </div>
      </div>

      <div className="flex items-start gap-3">
        <button
          type="button"
          onClick={save}
          disabled={busy}
          className="rounded-md px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
          style={{ background: "var(--accent)" }}
        >
          {busy ? "Saving…" : "Save"}
        </button>
        <p className="text-[10px] leading-snug" style={{ color: "var(--muted)" }}>
          Trim of rem. qty is a share of what is STILL held, so 50 / 50 / 100
          takes a position of 4 down as 2, then 1, then 1. A trim only fires
          above its profit target; its stop applies to whatever is left after
          it. Stop is a level from entry; Trail follows the high and exits on a
          give-back of that %. Leave a box empty to inherit it.
        </p>
      </div>
    </div>
  );
}

function Row({ index, trim, scope, onEdit, onRemove }: {
  index: number;
  trim: Trim;
  scope: string;
  onEdit: (patch: Partial<Trim>) => void;
  onRemove?: () => void;
}) {
  return (
    <>
      <span className="text-[11px] self-center" style={{ color: "var(--muted)" }}>Trim {index + 1}</span>
      <PercentInput
        step={5} value={trim.gate} placeholder="—"
        aria-label={`${scope} trim ${index + 1} profit target`}
        onChange={(e) => onEdit({ gate: e.target.value })}
        className={NUM} style={{ borderColor: "var(--border)" }}
      />
      <PercentInput
        step={5} value={trim.qty} placeholder="—"
        aria-label={`${scope} trim ${index + 1} share`}
        onChange={(e) => onEdit({ qty: e.target.value })}
        className={NUM} style={{ borderColor: "var(--border)" }}
      />
      <Segmented
        label={`Trim ${index + 1} stop kind`}
        value={trim.trail ? "trail" : "stop"}
        onChange={(k) => onEdit({ trail: k === "trail" })}
        options={[["stop", "Stop"], ["trail", "Trail"]]}
      />
      <PercentInput
        step={5} value={trim.stop} placeholder="—"
        aria-label={`${scope} trim ${index + 1} stop`}
        onChange={(e) => onEdit({ stop: e.target.value })}
        className={NUM} style={{ borderColor: "var(--border)" }}
      />
      {onRemove ? (
        <button type="button" onClick={onRemove} aria-label={`Remove trim ${index + 1}`}
                className="text-sm self-center" style={{ color: "var(--muted)" }}>
          ×
        </button>
      ) : <span />}
    </>
  );
}
