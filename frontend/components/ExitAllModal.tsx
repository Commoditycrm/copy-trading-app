"use client";

/**
 * Trader Exit All modal — three-step flow:
 *
 *   1. Action     — cancel open orders, or close open positions?
 *   2. Scope      — just me, or me + every subscriber?
 *   3. Confirm    — only shown when scope = "subscribers" (extra friction
 *                   on the destructive path).
 *
 * Subscribers don't use this modal — they get the simpler ConfirmModal
 * because they have no downstream to choose. (See positions/page.tsx.)
 *
 * The parent component receives both choices in `onConfirm` and routes
 * to the right backend endpoint:
 *
 *   action="orders"    → POST /api/trades/cancel-all-open?include_subscribers=…
 *   action="positions" → POST /api/positions/close-all?include_subscribers=…
 */
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Spinner } from "@/components/Spinner";

export type ExitAction = "orders" | "positions";

interface Props {
  open: boolean;
  busy?: boolean;
  /** Runs the close-all/cancel-all flow with the chosen action + scope. */
  onConfirm: (action: ExitAction, includeSubscribers: boolean) => void;
  onCancel: () => void;
}

type Step = "action" | "scope" | "confirm-all";

export function ExitAllModal({ open, busy = false, onConfirm, onCancel }: Props) {
  const [step, setStep] = useState<Step>("action");
  const [action, setAction] = useState<ExitAction>("positions");

  // Reset to step 1 every time the modal is reopened — don't surprise
  // the user with stale state from the previous open.
  useEffect(() => {
    if (open) {
      setStep("action");
      setAction("positions");
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onCancel]);

  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);
  if (!open || !mounted) return null;

  // Pre-compute the strings that change with the chosen action so the
  // scope + confirm screens read naturally without nested ternaries.
  const noun = action === "orders" ? "open orders" : "open positions";
  const verb = action === "orders" ? "Cancel"      : "Exit";
  const verbLower = action === "orders" ? "cancel" : "exit";
  const fanoutDescription =
    action === "orders"
      ? "Cancel yours, then cancel every matching mirror order in every subscriber's broker. Subscribers with copy-on lose those pending fills."
      : "Close yours, then fan out a SELL to every subscriber's broker. Affects everyone copying you.";
  const confirmBody =
    action === "orders"
      ? "This cancels every open order in your brokers AND every still-open mirror order across every subscriber. Filled orders are unaffected. The action cannot be undone."
      : "This closes every open position in your brokers AND places matching SELL orders in every subscriber's broker. The action cannot be undone.";

  return createPortal((
    <div
      className="fixed inset-0 z-50 grid place-items-center p-4"
      style={{ background: "rgba(0,0,0,0.55)", backdropFilter: "blur(2px)" }}
      onClick={(e) => { if (e.target === e.currentTarget && !busy) onCancel(); }}
    >
      <div
        role="dialog"
        aria-modal="true"
        className="card p-5 w-full max-w-md space-y-4"
        style={{ background: "var(--panel)", borderColor: "var(--border)" }}
      >
        {step === "action" && (
          <>
            <h3 className="text-base font-semibold">What do you want to exit?</h3>
            <div className="flex flex-col gap-2 pt-1">
              <button
                type="button"
                disabled={busy}
                onClick={() => { setAction("orders"); setStep("scope"); }}
                className="btn-ghost px-4 py-3 text-sm text-left rounded border"
                style={{ borderColor: "var(--border)" }}
              >
                <div className="font-medium">All open orders</div>
                <div className="text-xs" style={{ color: "var(--muted)" }}>
                  Cancel every order still working at the broker — Pending, Submitted,
                  Accepted, or Partially Filled. Filled orders + held positions are
                  left alone.
                </div>
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => { setAction("positions"); setStep("scope"); }}
                className="btn-ghost px-4 py-3 text-sm text-left rounded border"
                style={{ borderColor: "var(--border)" }}
              >
                <div className="font-medium">All open positions</div>
                <div className="text-xs" style={{ color: "var(--muted)" }}>
                  Place a market reverse order for every position held in your
                  connected brokers. Pending orders are not touched.
                </div>
              </button>
            </div>
            <div className="flex justify-end pt-1">
              <button
                type="button"
                disabled={busy}
                onClick={onCancel}
                className="btn-ghost px-4 py-2 text-sm"
              >
                Cancel
              </button>
            </div>
          </>
        )}

        {step === "scope" && (
          <>
            <h3 className="text-base font-semibold">{verb} {noun} — for whom?</h3>
            <div className="flex flex-col gap-2 pt-1">
              <button
                type="button"
                disabled={busy}
                onClick={() => onConfirm(action, false)}
                className="btn-ghost px-4 py-3 text-sm text-left rounded border"
                style={{ borderColor: "var(--border)" }}
              >
                <div className="font-medium">Just me</div>
                <div className="text-xs" style={{ color: "var(--muted)" }}>
                  {action === "orders"
                    ? "Cancel every open order in your own broker accounts. Subscribers' mirror orders are not touched."
                    : "Close every position in your own broker accounts. Subscribers are not touched."}
                </div>
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => setStep("confirm-all")}
                className="btn-ghost px-4 py-3 text-sm text-left rounded border"
                style={{ borderColor: "var(--border)" }}
              >
                <div className="font-medium" style={{ color: "var(--bad)" }}>Me + all subscribers</div>
                <div className="text-xs" style={{ color: "var(--muted)" }}>
                  {fanoutDescription}
                </div>
              </button>
            </div>
            <div className="flex justify-between pt-1">
              <button
                type="button"
                disabled={busy}
                onClick={() => setStep("action")}
                className="btn-ghost px-4 py-2 text-sm"
              >
                Back
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={onCancel}
                className="btn-ghost px-4 py-2 text-sm"
              >
                Cancel
              </button>
            </div>
          </>
        )}

        {step === "confirm-all" && (
          <>
            <h3 className="text-base font-semibold">Confirm — {verbLower} for everyone?</h3>
            <div className="text-sm" style={{ color: "var(--text-2)" }}>
              {confirmBody}
            </div>
            <div className="flex justify-end gap-2 pt-1">
              <button
                type="button"
                disabled={busy}
                onClick={() => setStep("scope")}
                className="btn-ghost px-4 py-2 text-sm"
              >
                Back
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => onConfirm(action, true)}
                className="btn-danger-soft px-4 py-2 text-sm inline-flex items-center gap-2"
              >
                <span>Yes, {verbLower} all</span>
                {busy && <Spinner />}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  ), document.body);
}
