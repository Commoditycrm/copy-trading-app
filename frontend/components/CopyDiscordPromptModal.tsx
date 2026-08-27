"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Bell, BellOff, X } from "lucide-react";
import { Spinner } from "@/components/Spinner";

/** Three-outcome prompt shown when a TRADER toggles copy trading in the sidebar:
 *  offer to flip their Discord alerts the same way. Two ACTIONS — "also toggle
 *  Discord" (primary) and "just copy" (secondary) — plus dismiss (Esc / X /
 *  click-outside) which ABORTS without changing anything. */
interface CopyDiscordPromptModalProps {
  open: boolean;
  /** true = resuming copy / offering alerts ON; false = pausing / offering OFF. */
  turningOn: boolean;
  title: string;
  message: React.ReactNode;
  alsoLabel: string;
  copyOnlyLabel: string;
  busy?: boolean;
  onAlso: () => void;
  onCopyOnly: () => void;
  onCancel: () => void;
}

// Discord "blurple" — themes the icon badge + primary button.
const BLURPLE = "#5865F2";

export function CopyDiscordPromptModal({
  open, turningOn, title, message, alsoLabel, copyOnlyLabel,
  busy = false, onAlso, onCopyOnly, onCancel,
}: CopyDiscordPromptModalProps) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);

  // Entrance transition (fade + scale) without any global CSS.
  const [show, setShow] = useState(false);
  useEffect(() => {
    if (!open) { setShow(false); return; }
    const id = requestAnimationFrame(() => setShow(true));
    return () => cancelAnimationFrame(id);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onCancel(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onCancel]);

  if (!open || !mounted) return null;

  const Icon = turningOn ? Bell : BellOff;

  return createPortal((
    <div
      className="fixed inset-0 z-50 grid place-items-center p-4"
      style={{
        background: "rgba(0,0,0,0.55)",
        backdropFilter: "blur(3px)",
        opacity: show ? 1 : 0,
        transition: "opacity 160ms ease",
      }}
      onClick={(e) => { if (e.target === e.currentTarget && !busy) onCancel(); }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="copy-discord-modal-title"
        className="card w-full max-w-sm p-6 relative"
        style={{
          background: "var(--panel)",
          borderColor: "var(--border)",
          boxShadow: "0 24px 60px -12px rgba(0,0,0,0.5)",
          opacity: show ? 1 : 0,
          transform: show ? "scale(1)" : "scale(0.96)",
          transition: "opacity 160ms ease, transform 160ms cubic-bezier(0.2,0.7,0.3,1)",
        }}
      >
        {/* Close */}
        <button
          type="button"
          aria-label="Close"
          disabled={busy}
          onClick={onCancel}
          className="absolute right-3 top-3 grid place-items-center rounded-md p-1.5 transition-colors disabled:opacity-40"
          style={{ color: "var(--muted)" }}
          onMouseEnter={(e) => { e.currentTarget.style.background = "var(--hover, rgba(127,127,127,0.12))"; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
        >
          <X size={16} />
        </button>

        {/* Icon badge */}
        <div
          className="grid place-items-center rounded-full mb-4"
          style={{ width: 48, height: 48, background: `${BLURPLE}1F`, color: BLURPLE }}
        >
          <Icon size={22} />
        </div>

        <h3 id="copy-discord-modal-title" className="text-lg font-semibold mb-1.5">{title}</h3>
        <div className="text-sm leading-relaxed" style={{ color: "var(--text-2)" }}>{message}</div>

        <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2 mt-6">
          <button
            type="button"
            disabled={busy}
            onClick={onCopyOnly}
            className="btn-ghost px-4 py-2.5 text-sm rounded-lg disabled:opacity-50"
          >
            {copyOnlyLabel}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={onAlso}
            className="px-4 py-2.5 text-sm font-medium rounded-lg inline-flex items-center justify-center gap-2 transition-opacity disabled:opacity-60"
            style={{ background: BLURPLE, color: "#fff" }}
          >
            {busy ? <Spinner /> : <Icon size={15} />}
            <span>{alsoLabel}</span>
          </button>
        </div>
      </div>
    </div>
  ), document.body);
}
