"use client";

/**
 * One TP or SL cell that flips into an input when clicked.
 *
 * The cell shows the current price (or "—" if unset). Click → number input
 * + Save / Cancel buttons. Save calls PATCH /api/trades/{orderId}/bracket
 * with just the field for this cell ({ take_profit_price: ... } or
 * { stop_loss_price: ... }). Empty input on save → null → clears the leg.
 *
 * Editing is gated by `canEdit`: false → render the value as plain text,
 * no click-to-edit handler. The parent (positions / order history tables)
 * decides eligibility based on the order's status (open, or filled with
 * the position still alive).
 */
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import type { Order } from "@/lib/types";

interface Props {
  orderId: string | null;       // entry order ID — null disables edit (e.g. no parent found)
  leg: "tp" | "sl";
  value: string | null;          // current TP or SL price ("210.50" or null)
  canEdit: boolean;
  /** Called after a successful PATCH so the parent can refresh state. */
  onUpdated?: (updatedOrder: Order) => void;
}

function fmtPrice(s: string | null): string {
  if (s === null || s === undefined || s === "") return "—";
  const n = Number(s);
  if (!Number.isFinite(n)) return s;
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function InlineBracketCell({ orderId, leg, value, canEdit, onUpdated }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<string>(value ?? "");
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Re-sync draft if the prop changes while not editing (e.g. SSE update).
  useEffect(() => {
    if (!editing) setDraft(value ?? "");
  }, [value, editing]);

  // Focus the input the moment we enter edit mode so the user can type
  // immediately without an extra click.
  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const colorVar = leg === "tp" ? "var(--good)" : "var(--bad)";

  if (!editing) {
    const displayColor = value ? colorVar : "var(--faint)";
    const interactive = canEdit && !!orderId;
    return (
      <span
        className="num inline-flex items-center gap-1 px-1 py-0.5 rounded transition-colors"
        style={{
          color: displayColor,
          cursor: interactive ? "pointer" : "default",
          background: interactive ? "transparent" : "transparent",
        }}
        title={interactive ? `Click to edit ${leg.toUpperCase()}` : undefined}
        onClick={() => { if (interactive) setEditing(true); }}
      >
        {fmtPrice(value)}
      </span>
    );
  }

  async function save() {
    if (!orderId) return;
    setSaving(true);
    try {
      // Empty draft → clear the leg. Otherwise send the number as a string
      // (matches the Pydantic Decimal field's accepted forms).
      const trimmed = draft.trim();
      const fieldValue: string | null = trimmed === "" ? null : trimmed;
      if (fieldValue !== null) {
        const n = Number(fieldValue);
        if (!Number.isFinite(n) || n <= 0) {
          notify.warn("Enter a positive price (or leave empty to clear)");
          setSaving(false);
          return;
        }
      }
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
          : `${leg.toUpperCase()} set to ${fmtPrice(fieldValue)}`,
      );
      onUpdated?.(updated);
      setEditing(false);
    } catch (e) {
      // Translate the backend's geometry / state error codes into something
      // a human can act on.
      const detail = e instanceof ApiError ? String(e.detail ?? "") : "";
      const msg = detail.startsWith("buy_") || detail.startsWith("sell_")
        ? detail.replace(/_/g, " ")
        : detail || "Could not update bracket";
      notify.fromError(e, msg);
    } finally {
      setSaving(false);
    }
  }

  function cancel() {
    setDraft(value ?? "");
    setEditing(false);
  }

  return (
    <span className="inline-flex items-center gap-1">
      <input
        ref={inputRef}
        type="number"
        step="0.01"
        min="0.01"
        placeholder="—"
        value={draft}
        onChange={e => setDraft(e.target.value)}
        onKeyDown={e => {
          if (e.key === "Enter") { e.preventDefault(); save(); }
          if (e.key === "Escape") { e.preventDefault(); cancel(); }
        }}
        disabled={saving}
        className="w-20 px-1.5 py-0.5 text-xs tabular-nums border rounded outline-none"
        style={{
          background: "var(--bg)",
          borderColor: colorVar,
          color: "var(--text)",
        }}
      />
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
