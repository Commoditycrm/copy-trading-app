"use client";

/**
 * Trader Exit All modal: pick scope (mine only vs mine + every subscriber),
 * then confirm the "subscribers" path a second time before firing.
 * Subscribers don't use this — they get the simpler ConfirmModal.
 */
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Spinner } from "@/components/Spinner";

interface Props {
  open: boolean;
  busy?: boolean;
  /** Runs the close-all flow with `include_subscribers` set accordingly. */
  onConfirm: (includeSubscribers: boolean) => void;
  onCancel: () => void;
}

export function ExitAllModal({ open, busy = false, onConfirm, onCancel }: Props) {
  const [step, setStep] = useState<"scope" | "confirm-all">("scope");

  // Reset step whenever the modal opens fresh.
  useEffect(() => {
    if (open) setStep("scope");
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
        {step === "scope" ? (
          <>
            <h3 className="text-base font-semibold">Exit all positions?</h3>
            {/* <div className="text-sm" style={{ color: "var(--text-2)" }}>
              Choose whether to close only your own positions or also propagate
              the close to every subscriber following you.
            </div> */}
            <div className="flex flex-col gap-2 pt-1">
              <button
                type="button"
                disabled={busy}
                onClick={() => onConfirm(false)}
                className="btn-ghost px-4 py-3 text-sm text-left rounded border"
                style={{ borderColor: "var(--border)" }}
              >
                <div className="font-medium">Just me</div>
                <div className="text-xs" style={{ color: "var(--muted)" }}>
                  Close every position in your own broker accounts. Subscribers are not touched.
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
                  Close yours, then fan out a SELL to every subscriber's broker. Affects everyone copying you.
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
        ) : (
          <>
            <h3 className="text-base font-semibold">Confirm — exit for everyone?</h3>
            <div className="text-sm" style={{ color: "var(--text-2)" }}>
              This closes every open position in your brokers <strong>and</strong> places matching
              SELL orders in every subscriber's broker. The action cannot be undone.
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
                onClick={() => onConfirm(true)}
                className="btn-danger-soft px-4 py-2 text-sm inline-flex items-center gap-2"
              >
                <span>Yes, exit all</span>
                {busy && <Spinner />}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  ), document.body);
}
