"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Spinner } from "@/components/Spinner";

interface ConfirmModalProps {
  open: boolean;
  title: string;
  message: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Visual treatment of the confirm button. */
  variant?: "danger" | "primary";
  /** Disables both buttons while async work is in flight; spinner shown on confirm. */
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmModal({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "danger",
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  // Esc closes (unless an async confirm is in flight).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onCancel]);

  // Portal target: render into document.body so the fixed-positioned overlay
  // isn't clipped by an ancestor with overflow/transform/filter.
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);

  if (!open || !mounted) return null;

  const confirmClass = variant === "danger" ? "btn-danger-soft" : "btn-accent-solid";

  return createPortal((
    <div
      className="fixed inset-0 z-50 grid place-items-center p-4"
      style={{ background: "rgba(0,0,0,0.55)", backdropFilter: "blur(2px)" }}
      onClick={(e) => {
        // Click outside the card closes — but only when nothing's running.
        if (e.target === e.currentTarget && !busy) onCancel();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-modal-title"
        className="card p-5 w-full max-w-md space-y-4"
        style={{ background: "var(--panel)", borderColor: "var(--border)" }}
      >
        <h3 id="confirm-modal-title" className="text-base font-semibold">{title}</h3>
        <div className="text-sm" style={{ color: "var(--text-2)" }}>{message}</div>
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            disabled={busy}
            onClick={onCancel}
            className="btn-ghost px-4 py-2 text-sm"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onConfirm}
            className={`${confirmClass} px-4 py-2 text-sm inline-flex items-center gap-2`}
          >
            <span>{confirmLabel}</span>
            {busy && <Spinner />}
          </button>
        </div>
      </div>
    </div>
  ), document.body);
}
