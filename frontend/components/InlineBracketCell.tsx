"use client";

/**
 * One TP or SL cell that flips into an input when clicked.
 *
 * Display mode is PERCENT-from-entry — the same shape the trader uses on
 * the Trade Panel to set the bracket. We store absolute prices on the
 * backend (the broker needs concrete prices), so this component converts
 * in both directions:
 *
 *   display:  abs price → % distance from entry on the trader's side
 *   save:     % the user typed → absolute price → PATCH /api/trades/{id}/bracket
 *
 * Direction convention (positive % = "good" side for the entry):
 *   buy  + tp: TP above entry  →  pct = +(p/e - 1) * 100
 *   buy  + sl: SL below entry  →  pct = +(1 - p/e) * 100
 *   sell + tp: TP below entry  →  pct = +(1 - p/e) * 100
 *   sell + sl: SL above entry  →  pct = +(p/e - 1) * 100
 *
 * When the entry price is unknown (e.g. a market order that hasn't
 * filled yet — there's no anchor) we fall back to showing/editing the
 * absolute price. Edit is gated by `canEdit`; the parent decides
 * eligibility based on order status.
 */
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import type { Order, OrderSide } from "@/lib/types";

interface Props {
  orderId: string | null;       // entry order ID — null disables edit (e.g. no parent found)
  leg: "tp" | "sl";
  value: string | null;          // current TP or SL ABSOLUTE price ("210.50" or null)
  /** Reference price for converting absolute ↔ percent. For filled orders
   *  this is the filled_avg_price; for open limit orders, the limit_price.
   *  Null means we couldn't determine a reference → fall back to abs-price
   *  display mode for this row. */
  entryPrice: string | null;
  /** Side of the parent ENTRY order (buy / sell). Drives which direction
   *  a positive percent points in. */
  side: OrderSide;
  canEdit: boolean;
  /** Called after a successful PATCH so the parent can refresh state. */
  onUpdated?: (updatedOrder: Order) => void;
}

/** Sign factor that makes a "correctly placed" leg yield a positive percent.
 *  +1 means the price is ABOVE entry, -1 means BELOW. */
function legDirection(side: OrderSide, leg: "tp" | "sl"): 1 | -1 {
  const buy = side === "buy";
  return (buy && leg === "tp") || (!buy && leg === "sl") ? 1 : -1;
}

function priceToPct(
  price: string | null,
  entry: string | null,
  side: OrderSide,
  leg: "tp" | "sl",
): number | null {
  if (!price || !entry) return null;
  const p = Number(price);
  const e = Number(entry);
  if (!Number.isFinite(p) || !Number.isFinite(e) || e <= 0) return null;
  return ((p - e) / e) * 100 * legDirection(side, leg);
}

function pctToPrice(
  pct: number,
  entry: string,
  side: OrderSide,
  leg: "tp" | "sl",
): number {
  const e = Number(entry);
  return e * (1 + (legDirection(side, leg) * pct) / 100);
}

function fmtPct(n: number): string {
  // Round to 2 dp so 8.137 reads as 8.14, but DROP trailing zeros so
  // 10.00 → 10 and 5.50 → 5.5. Going through Number() after toFixed
  // re-parses to a number, which naturally strips the trailing zeros.
  const rounded = Number(n.toFixed(2));
  return `${rounded}%`;
}

/** Same trim-trailing-zeros rule as fmtPct, but as a bare number (no "%"),
 *  for seeding the edit input. We don't want the user to start editing
 *  "10" and immediately see "10.00" in the field. */
function pctForInput(n: number): string {
  return String(Number(n.toFixed(2)));
}

function fmtPrice(s: string | null): string {
  if (s === null || s === undefined || s === "") return "—";
  const n = Number(s);
  if (!Number.isFinite(n)) return s;
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function InlineBracketCell({ orderId, leg, value, entryPrice, side, canEdit, onUpdated }: Props) {
  // Percent mode requires a usable entry-price anchor. Without it (e.g.
  // unfilled market orders) we silently fall back to absolute-price
  // display + edit so the row is still useful.
  const inPercentMode = !!entryPrice && Number(entryPrice) > 0;
  const currentPct = inPercentMode ? priceToPct(value, entryPrice, side, leg) : null;

  // Initial draft = the percent the cell currently shows (or empty if no
  // value). In abs-price mode the draft is just the raw price. We use
  // pctForInput so the field opens as "10" not "10.00".
  const initialDraft = inPercentMode
    ? (currentPct !== null ? pctForInput(currentPct) : "")
    : (value ?? "");

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<string>(initialDraft);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Re-sync draft if the value / entry / mode changes while not editing
  // (e.g. SSE update changed take_profit_price, or filled_avg_price
  // landed for a previously-unfilled order).
  useEffect(() => {
    if (!editing) setDraft(initialDraft);
  }, [initialDraft, editing]);

  // Focus the input the moment we enter edit mode so the user can type
  // immediately without an extra click.
  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const colorVar = leg === "tp" ? "var(--good)" : "var(--bad)";

  if (!editing) {
    const displayColor = value ? colorVar : "var(--faint)";
    const interactive = canEdit && !!orderId;
    const display = inPercentMode
      ? (currentPct !== null ? fmtPct(currentPct) : "—")
      : fmtPrice(value);
    return (
      <span
        className="num inline-flex items-center gap-1 px-1 py-0.5 rounded transition-colors tabular-nums"
        style={{
          color: displayColor,
          cursor: interactive ? "pointer" : "default",
        }}
        title={
          interactive
            ? (inPercentMode
                ? `Click to edit ${leg.toUpperCase()} (% of entry)`
                : `Click to edit ${leg.toUpperCase()}`)
            : undefined
        }
        onClick={() => { if (interactive) setEditing(true); }}
      >
        {display}
      </span>
    );
  }

  async function save() {
    if (!orderId) return;
    setSaving(true);
    try {
      const trimmed = draft.trim();
      // Empty → clear the leg (send null).
      if (trimmed === "") {
        await sendPatch(null);
        return;
      }
      const n = Number(trimmed);
      if (!Number.isFinite(n) || n <= 0) {
        notify.warn(
          inPercentMode
            ? "Enter a positive percent (or leave empty to clear)"
            : "Enter a positive price (or leave empty to clear)",
        );
        setSaving(false);
        return;
      }
      // In percent mode, convert the typed % back to an absolute price
      // using the entry-side direction. We send the price (the backend
      // contract is absolute decimals). Round to 4 dp to match
      // orders.Numeric(18,4).
      let absolutePrice: string;
      if (inPercentMode) {
        // entryPrice was non-null when we computed inPercentMode above.
        const px = pctToPrice(n, entryPrice as string, side, leg);
        if (!Number.isFinite(px) || px <= 0) {
          notify.warn("Resulting price isn't positive — check the percent");
          setSaving(false);
          return;
        }
        absolutePrice = px.toFixed(4);
      } else {
        absolutePrice = trimmed;
      }
      await sendPatch(absolutePrice, n);
    } catch (e) {
      handleSaveError(e);
    } finally {
      setSaving(false);
    }
  }

  /** Single shared send/notify path used by both clear and set. The
   *  `pctEcho` is only for the success toast when we want to show what
   *  the trader typed in percent mode. */
  async function sendPatch(fieldValue: string | null, pctEcho?: number) {
    const body = leg === "tp"
      ? { take_profit_price: fieldValue }
      : { stop_loss_price: fieldValue };
    const updated = await api<Order>(
      `/api/trades/${orderId}/bracket`,
      { method: "PATCH", body: JSON.stringify(body) },
    );
    notify.success(
      fieldValue === null
        ? `${leg.toUpperCase()} cleared`
        : (pctEcho !== undefined
            ? `${leg.toUpperCase()} set to ${fmtPct(pctEcho)}`
            : `${leg.toUpperCase()} set to ${fmtPrice(fieldValue)}`),
    );
    onUpdated?.(updated);
    setEditing(false);
  }

  function handleSaveError(e: unknown) {
    const detail = e instanceof ApiError ? String(e.detail ?? "") : "";
    const msg = detail.startsWith("buy_") || detail.startsWith("sell_")
      ? detail.replace(/_/g, " ")
      : detail || "Could not update bracket";
    notify.fromError(e, msg);
  }

  function cancel() {
    setDraft(initialDraft);
    setEditing(false);
  }

  // Wider input for percent mode (allows "10.50") + a "%" suffix glyph so
  // the unit is unambiguous even mid-edit.
  return (
    <span className="inline-flex items-center gap-1">
      <span className="inline-flex items-stretch border rounded overflow-hidden"
        style={{ borderColor: colorVar }}
      >
        <input
          ref={inputRef}
          type="number"
          step={inPercentMode ? "0.01" : "0.01"}
          min="0.01"
          placeholder={inPercentMode ? "—" : "—"}
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => {
            if (e.key === "Enter") { e.preventDefault(); save(); }
            if (e.key === "Escape") { e.preventDefault(); cancel(); }
          }}
          disabled={saving}
          className="w-16 px-1.5 py-0.5 text-xs tabular-nums outline-none border-0"
          style={{
            background: "var(--bg)",
            color: "var(--text)",
          }}
        />
        {inPercentMode && (
          <span
            className="px-1.5 grid place-items-center text-[11px] font-medium"
            style={{ color: "var(--muted)", background: "var(--bg)" }}
            aria-hidden
          >
            %
          </span>
        )}
      </span>
      <button
        type="button"
        onClick={save}
        disabled={saving}
        className="px-1.5 py-0.5 text-[10px] font-semibold rounded inline-flex items-center gap-1"
        style={{
          background: colorVar,
          color: leg === "tp" ? "#06210f" : "#1a0606",
        }}
        title="Save (Enter)"
      >
        {saving && <Spinner />}
        Save
      </button>
      <button
        type="button"
        onClick={cancel}
        disabled={saving}
        className="px-1.5 py-0.5 text-[10px] font-medium rounded border"
        style={{ borderColor: "var(--border)", color: "var(--muted)" }}
        title="Cancel (Esc)"
      >
        ✕
      </button>
    </span>
  );
}
