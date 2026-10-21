"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

/** One tunable interval. `override` is null when no Redis override is set OR
 *  when the stored value is out of bounds — in that case the poller is using
 *  the default, and the UI says so rather than claiming a cadence nothing is
 *  running at. `min`/`max` are SECONDS, which is what the API stores.
 *
 *  `broker` + `row` are the grid coordinates; `broker` is null for a sweep
 *  that covers every connected account rather than one broker. */
interface IntervalKnob {
  name: string;
  group: string;
  broker: string | null;
  row: string;
  /** "seconds" is a duration: formatted as one, with a sec/min/hr picker.
   *  "count" and "rate" are plain numbers shown with `unit_label` as a
   *  suffix — a reads-per-placement of 2 is not "2s". */
  kind: "seconds" | "count" | "rate";
  unit_label: string;
  label: string;
  why: string;
  default: number;
  override: number | null;
  effective: number;
  min: number;
  max: number;
}

/** An on/off knob. Same override and coordinate semantics as above. */
interface SwitchKnob {
  name: string;
  group: string;
  broker: string | null;
  row: string;
  label: string;
  why: string;
  default: boolean;
  override: boolean | null;
  effective: boolean;
}

interface PollingConfig {
  intervals: IntervalKnob[];
  switches: SwitchKnob[];
  /** Render order, decided server-side so the page doesn't hardcode lists
   *  that drift from the registry. */
  groups: string[];
  brokers: { name: string; label: string }[];
  matrix_rows: { group: string; row: string }[];
  na_notes: { broker: string; row: string; note: string }[];
}

// ── Units ───────────────────────────────────────────────────────────────
// The API stores and validates SECONDS. Units exist only so nobody has to
// type 3600 for the hourly snapshot sweep; every value is converted back to
// seconds before it is sent, and the server re-checks the bounds regardless.

const UNITS = [
  { secs: 1, label: "sec" },
  { secs: 60, label: "min" },
  { secs: 3600, label: "hr" },
] as const;

/** Whole-number bounds for `knob` expressed in `unitSecs`. A unit is only
 *  offered when at least one whole number in it lands inside the knob's real
 *  range — otherwise picking "hr" on a 1–30s knob would offer no valid value. */
function boundsIn(knob: IntervalKnob, unitSecs: number): { lo: number; hi: number } | null {
  const lo = Math.ceil(knob.min / unitSecs);
  const hi = Math.floor(knob.max / unitSecs);
  return hi >= lo && hi >= 1 ? { lo, hi } : null;
}

function unitsFor(knob: IntervalKnob) {
  // A non-duration has no units to pick between.
  if (knob.kind !== "seconds") return [];
  return UNITS.filter((u) => boundsIn(knob, u.secs) !== null);
}

/** The largest offered unit that divides `secs` exactly, so a 900s default
 *  opens as "15 min" and a 45s one stays in seconds. */
function bestUnit(secs: number, knob: IntervalKnob): number {
  if (knob.kind !== "seconds") return 1;
  const usable = unitsFor(knob);
  for (let i = usable.length - 1; i >= 0; i--) {
    const u = usable[i];
    if (secs % u.secs === 0 && secs / u.secs >= 1) return u.secs;
  }
  return 1;
}

function fmtDur(secs: number): string {
  if (secs >= 3600 && secs % 3600 === 0) return `${secs / 3600} hr`;
  if (secs >= 60 && secs % 60 === 0) return `${secs / 60} min`;
  return `${secs}s`;
}

/** Read-only display for any knob: a duration for the seconds ones, a plain
 *  number plus its own suffix for a count or a rate. */
function fmtVal(knob: IntervalKnob, v: number): string {
  return knob.kind === "seconds" ? fmtDur(v) : `${v} ${knob.unit_label}`;
}

interface Draft {
  value: string;
  unit: number;   // seconds per unit
}

type Saver = (name: string, value: number | boolean | null, msg: string) => void;

/** Scoped so it can't leak to other number inputs in the admin area. */
const CONTROL_CSS = `
.knob-ctl { height: 30px; }
.knob-num {
  -moz-appearance: textfield;
  appearance: textfield;
}
.knob-num::-webkit-outer-spin-button,
.knob-num::-webkit-inner-spin-button {
  -webkit-appearance: none;
  margin: 0;
}
.knob-unit {
  -webkit-appearance: none;
  -moz-appearance: none;
  appearance: none;
}
`;

export default function AdminApiPage() {
  const [cfg, setCfg] = useState<PollingConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [savingName, setSavingName] = useState<string | null>(null);

  const apply = useCallback((c: PollingConfig) => {
    setCfg(c);
    setDrafts(
      Object.fromEntries(
        c.intervals.map((k) => {
          const unit = bestUnit(k.effective, k);
          return [k.name, { value: String(k.effective / unit), unit }];
        }),
      ),
    );
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      apply(await api<PollingConfig>("/api/admin/config/polling"));
    } catch (e) {
      notify.fromError(e, "Could not load polling config");
    } finally {
      setLoading(false);
    }
  }, [apply]);

  useEffect(() => { void load(); }, [load]);

  const save = useCallback<Saver>(
    async (name, value, msg) => {
      setSavingName(name);
      try {
        const c = await api<PollingConfig>("/api/admin/config/polling", {
          method: "PATCH",
          body: JSON.stringify({ name, value }),
        });
        apply(c);
        notify.success(msg);
      } catch (e) {
        notify.fromError(e, "Could not update polling config");
      } finally {
        setSavingName(null);
      }
    },
    [apply],
  );

  if (loading || !cfg) {
    return <div style={{ color: "var(--muted)" }}>Loading…</div>;
  }

  const overrides =
    cfg.intervals.filter((k) => k.override !== null).length +
    cfg.switches.filter((k) => k.override !== null).length;

  /** The knob in a given cell, if any. */
  const cellKnob = (broker: string, row: string) => {
    const sw = cfg.switches.find((k) => k.broker === broker && k.row === row);
    if (sw) return { kind: "switch" as const, knob: sw };
    const iv = cfg.intervals.find((k) => k.broker === broker && k.row === row);
    if (iv) return { kind: "interval" as const, knob: iv };
    return null;
  };

  const naNote = (broker: string, row: string) =>
    cfg.na_notes.find((n) => n.broker === broker && n.row === row)?.note
    ?? "Not applicable for this broker — there is no such loop.";

  const sections = cfg.groups
    .map((group) => ({
      group,
      rows: cfg.matrix_rows.filter((r) => r.group === group).map((r) => r.row),
    }))
    .filter((s) => s.rows.length > 0);

  const platformWide = cfg.intervals.filter((k) => k.broker === null);

  return (
    <div className="space-y-6 max-w-6xl">
      <style>{CONTROL_CSS}</style>
      <div>
        <h2 className="text-xl font-bold">API Configuration</h2>
        <p className="text-sm mt-0.5" style={{ color: "var(--muted)" }}>
          How often each broker is polled. Every value is read on the poller&apos;s
          next tick, so changes apply immediately — no backend restart.
          {overrides > 0 && (
            <>
              {" "}
              <span style={{ color: "#f59e0b" }}>
                {overrides} setting{overrides === 1 ? "" : "s"} overridden.
              </span>
            </>
          )}
        </p>
      </div>

      <div
        className="rounded-xl overflow-x-auto"
        style={{ border: "1px solid var(--border)" }}
      >
        <table className="w-full text-sm" style={{ borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ background: "var(--panel-2)" }}>
              <th
                className="px-3 py-2 text-left text-[10px] uppercase tracking-widest font-semibold"
                style={{ color: "var(--muted)", minWidth: "17rem" }}
              >
                Setting
              </th>
              {cfg.brokers.map((b) => (
                <th
                  key={b.name}
                  className="px-3 py-2 text-left text-[11px] font-semibold"
                  style={{
                    color: "var(--text)",
                    minWidth: "11rem",
                    borderLeft: "1px solid var(--border)",
                  }}
                >
                  {b.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sections.map(({ group, rows }) => (
              <Section key={group} group={group} span={cfg.brokers.length + 1}>
                {rows.map((row) => (
                  <tr key={row} style={{ borderTop: "1px solid var(--border)" }}>
                    <td className="px-3 py-2 align-middle">
                      <RowLabel row={row} cfg={cfg} />
                    </td>
                    {cfg.brokers.map((b) => {
                      const cell = cellKnob(b.name, row);
                      return (
                        <td
                          key={b.name}
                          className="px-3 py-2 align-middle"
                          style={{ borderLeft: "1px solid var(--border)" }}
                        >
                          {cell === null ? (
                            <span
                              title={naNote(b.name, row)}
                              className="cursor-help text-sm"
                              style={{ color: "var(--muted)", opacity: 0.45 }}
                            >
                              —
                            </span>
                          ) : cell.kind === "switch" ? (
                            <SwitchCell
                              knob={cell.knob}
                              saving={savingName === cell.knob.name}
                              save={save}
                            />
                          ) : (
                            <IntervalCell
                              knob={cell.knob}
                              draft={drafts[cell.knob.name] ?? { value: "", unit: 1 }}
                              onDraft={(d) =>
                                setDrafts((p) => ({ ...p, [cell.knob.name]: d }))
                              }
                              saving={savingName === cell.knob.name}
                              save={save}
                            />
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </Section>
            ))}
          </tbody>
        </table>
      </div>

      <div>
        <h3 className="text-sm font-semibold">All brokers</h3>
        <p className="text-[11px] mt-0.5 mb-2" style={{ color: "var(--muted)" }}>
          One sweep covers every connected account, so these aren&apos;t set per
          broker.
        </p>
        <div
          className="rounded-xl overflow-x-auto"
          style={{ border: "1px solid var(--border)" }}
        >
          <table className="w-full text-sm" style={{ borderCollapse: "collapse" }}>
            <tbody>
              {platformWide.map((knob, i) => (
                <tr
                  key={knob.name}
                  style={{ borderTop: i === 0 ? undefined : "1px solid var(--border)" }}
                >
                  <td className="px-3 py-2 align-middle" style={{ minWidth: "17rem" }}>
                    <span
                      title={knob.why}
                      className="cursor-help border-b border-dotted"
                      style={{ borderColor: "var(--border)" }}
                    >
                      {knob.row}
                    </span>
                  </td>
                  <td
                    className="px-3 py-2 align-middle"
                    style={{ borderLeft: "1px solid var(--border)", width: "11rem" }}
                  >
                    <IntervalCell
                      knob={knob}
                      draft={drafts[knob.name] ?? { value: "", unit: 1 }}
                      onDraft={(d) => setDrafts((p) => ({ ...p, [knob.name]: d }))}
                      saving={savingName === knob.name}
                      save={save}
                    />
                  </td>
                  <td />
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <p className="text-[11px]" style={{ color: "var(--muted)" }}>
        Hover a setting&apos;s name — or a blank cell — for what it controls and
        why. Durations are stored in seconds; minutes and hours are just for
        entry, and the server re-checks the range whichever unit you use.
      </p>
    </div>
  );
}

function Section({
  group, span, children,
}: {
  group: string;
  span: number;
  children: React.ReactNode;
}) {
  return (
    <>
      <tr>
        <td
          colSpan={span}
          className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-widest"
          style={{
            background: "var(--bg-tint)",
            color: "var(--muted)",
            borderTop: "1px solid var(--border)",
            borderBottom: "1px solid var(--border)",
          }}
        >
          {group}
        </td>
      </tr>
      {children}
    </>
  );
}

/** A row's tooltip comes from whichever knob sits in it — they all explain the
 *  same mechanism, so the first one found is representative. */
function RowLabel({ row, cfg }: { row: string; cfg: PollingConfig }) {
  const any =
    cfg.switches.find((k) => k.row === row) ??
    cfg.intervals.find((k) => k.row === row);
  return (
    <span
      title={any?.why}
      className="cursor-help border-b border-dotted"
      style={{ borderColor: "var(--border)" }}
    >
      {row}
    </span>
  );
}

function IntervalCell({
  knob, draft, onDraft, saving, save,
}: {
  knob: IntervalKnob;
  draft: Draft;
  onDraft: (d: Draft) => void;
  saving: boolean;
  save: Saver;
}) {
  const units = unitsFor(knob);
  const n = Number(draft.value);
  const secs = n * draft.unit;
  const valid =
    draft.value !== "" &&
    Number.isFinite(n) &&
    Number.isInteger(n) &&
    secs >= knob.min &&
    secs <= knob.max;
  const changed = valid && secs !== knob.effective;
  const usingOverride = knob.override !== null;
  const b =
    knob.kind === "seconds"
      ? boundsIn(knob, draft.unit)
      : { lo: knob.min, hi: knob.max };

  const borderColor =
    !valid && draft.value !== ""
      ? "var(--bad)"
      : usingOverride
        ? "rgba(245,158,11,0.45)"
        : "var(--border)";

  /** Switching unit keeps the same real duration where it converts exactly,
   *  so flipping sec→min on 900s shows 15 rather than resetting the field. */
  function changeUnit(nextUnit: number) {
    const current = valid ? secs : knob.effective;
    const converted = current / nextUnit;
    const nb = boundsIn(knob, nextUnit);
    onDraft({
      value: Number.isInteger(converted)
        ? String(converted)
        : String(Math.min(nb?.hi ?? 1, Math.max(nb?.lo ?? 1, Math.round(converted)))),
      unit: nextUnit,
    });
  }

  return (
    <div className="flex flex-col gap-1 min-h-[3rem] justify-center">
      <div className="flex items-center gap-1.5">
        <div
          className="knob-ctl inline-flex items-stretch rounded-lg overflow-hidden transition-colors"
          style={{
            border: `1px solid ${borderColor}`,
            background: "var(--bg-tint)",
          }}
        >
          <input
            type="number"
            step={1}
            min={b?.lo}
            max={b?.hi}
            value={draft.value}
            onChange={(e) => onDraft({ ...draft, value: e.target.value })}
            aria-label={knob.label}
            className="knob-num w-[3.25rem] px-2 text-sm tabular-nums text-right"
            style={{
              border: "none",
              background: "transparent",
              outline: "none",
              color: usingOverride ? "#f59e0b" : "var(--text)",
            }}
          />
          {units.length > 1 ? (
            <div
              className="relative inline-flex items-center"
              style={{ borderLeft: "1px solid var(--border)" }}
            >
              <select
                value={draft.unit}
                onChange={(e) => changeUnit(Number(e.target.value))}
                aria-label={`${knob.label} unit`}
                className="knob-unit h-full pl-2 pr-5 text-[11px] cursor-pointer"
                style={{
                  border: "none",
                  background: "transparent",
                  outline: "none",
                  color: "var(--text-2)",
                }}
              >
                {units.map((u) => (
                  <option key={u.secs} value={u.secs}>{u.label}</option>
                ))}
              </select>
              <span
                aria-hidden
                className="pointer-events-none absolute right-1.5 text-[8px]"
                style={{ color: "var(--muted)" }}
              >
                ▼
              </span>
            </div>
          ) : (
            // Same divider and width as the picker, so a count or rate cell
            // lines up with the duration cells in the column above it.
            <span
              className="inline-flex items-center px-2 text-[11px] whitespace-nowrap"
              style={{
                borderLeft: "1px solid var(--border)",
                color: "var(--muted)",
              }}
            >
              {knob.kind === "seconds" ? "sec" : knob.unit_label}
            </span>
          )}
        </div>
        {changed && (
          <button
            disabled={saving}
            onClick={() =>
              save(knob.name, secs, `${knob.label} set to ${fmtVal(knob, secs)}`)
            }
            className="knob-ctl px-2 text-[10px] rounded-lg font-semibold transition-all disabled:opacity-30 hover:enabled:brightness-110"
            style={{ background: "var(--accent)", color: "var(--accent-ink)" }}
          >
            Save
          </button>
        )}
      </div>
      <div
        className="text-[10px] leading-none flex items-center gap-1.5"
        style={{ color: "var(--muted)" }}
      >
        {!valid && draft.value !== "" ? (
          <span style={{ color: "var(--bad)" }}>
            {b ? `${b.lo}–${b.hi}` : "out of range"}
          </span>
        ) : (
          <span>default {fmtVal(knob, knob.default)}</span>
        )}
        {usingOverride && (
          <button
            disabled={saving}
            onClick={() =>
              save(
                knob.name,
                null,
                `${knob.label} back to the default ${fmtVal(knob, knob.default)}`,
              )
            }
            title="Clear the override and go back to the default"
            className="underline disabled:opacity-30"
            style={{ color: "#f59e0b" }}
          >
            reset
          </button>
        )}
      </div>
    </div>
  );
}

function SwitchCell({
  knob, saving, save,
}: {
  knob: SwitchKnob;
  saving: boolean;
  save: Saver;
}) {
  const usingOverride = knob.override !== null;
  return (
    <div className="flex flex-col gap-1 min-h-[3rem] justify-center">
      <button
        disabled={saving}
        onClick={() =>
          save(
            knob.name,
            !knob.effective,
            `${knob.label} turned ${!knob.effective ? "on" : "off"}`,
          )
        }
        title={`Click to turn ${knob.effective ? "off" : "on"}`}
        className="self-start px-2 py-0.5 text-[10px] uppercase tracking-wider rounded-full font-semibold disabled:opacity-30"
        style={{
          background: knob.effective ? "rgba(34,197,94,0.12)" : "rgba(148,163,184,0.12)",
          color: knob.effective ? "#22c55e" : "var(--muted)",
          border: knob.effective
            ? "1px solid rgba(34,197,94,0.25)"
            : "1px solid var(--border)",
        }}
      >
        {knob.effective ? "On" : "Off"}
      </button>
      <div
        className="text-[10px] leading-none flex items-center gap-1.5"
        style={{ color: "var(--muted)" }}
      >
        <span>default {knob.default ? "on" : "off"}</span>
        {usingOverride && (
          <button
            disabled={saving}
            onClick={() =>
              save(
                knob.name,
                null,
                `${knob.label} back to the default (${knob.default ? "on" : "off"})`,
              )
            }
            title="Clear the override and go back to the default"
            className="underline disabled:opacity-30"
            style={{ color: "#f59e0b" }}
          >
            reset
          </button>
        )}
      </div>
    </div>
  );
}
