"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Spinner } from "@/components/Spinner";

/** Three-outcome prompt shown when a TRADER toggles copy trading in the sidebar:
 *  offer to flip their Discord alerts the same way. Unlike ConfirmModal (two
 *  buttons), this has two ACTIONS — "also toggle Discord" and "just copy" — plus
 *  dismiss (Esc / click-outside) which ABORTS without changing anything. */
interface CopyDiscordPromptModalProps {
  open: boolean;
  title: string;
  message: React.ReactNode;
  /** Primary button — apply the copy toggle AND flip Discord alerts. */
  alsoLabel: string;
  /** Secondary button — apply the copy toggle only, leave Discord as-is. */
  copyOnlyLabel: string;
  busy?: boolean;
  onAlso: () => void;
  onCopyOnly: () => void;
  onCancel: () => void;
}

export function CopyDiscordPromptModal({
  open, title, message, alsoLabel, copyOnlyLabel,
  busy = false, onAlso, onCopyOnly, onCancel,
}: CopyDiscordPromptModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onCancel(); };
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
        aria-labelledby="copy-discord-modal-title"
        className="card p-5 w-full max-w-md space-y-4"
        style={{ background: "var(--panel)", borderColor: "var(--border)" }}
      >
        <h3 id="copy-discord-modal-title" className="text-base font-semibold">{title}</h3>
        <div className="text-sm" style={{ color: "var(--text-2)" }}>{message}</div>
        <div className="flex flex-col sm:flex-row sm:justify-end gap-2 pt-1">
          <button
            type="button"
            disabled={busy}
            onClick={onCopyOnly}
            className="btn-ghost px-4 py-2 text-sm"
          >
            {copyOnlyLabel}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onAlso}
            className="btn-accent-solid px-4 py-2 text-sm inline-flex items-center justify-center gap-2"
          >
            <span>{alsoLabel}</span>
            {busy && <Spinner />}
          </button>
        </div>
      </div>
    </div>
  ), document.body);
}
