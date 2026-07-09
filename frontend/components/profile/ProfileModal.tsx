"use client";

import { useEffect } from "react";
import { ProfileForm } from "@/components/profile/ProfileForm";
import type { User } from "@/lib/types";

function initials(name: string | null, email: string): string {
  const base = (name ?? email).trim();
  const parts = base.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return base.slice(0, 2).toUpperCase();
}

/** Profile popover from the avatar — display name + email editing for ANY role,
 *  plus sign out. Same editor (ProfileForm) as the Settings page, so no trip to
 *  Settings is needed. */
export function ProfileModal({
  open, user, onClose, onSignOut, onUpdated,
}: {
  open: boolean;
  user: User;
  onClose: () => void;
  onSignOut: () => void;
  onUpdated?: (u: User) => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  const displayName = user.display_name || user.email.split("@")[0];

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-end p-4"
      style={{ background: "rgba(0,0,0,0.4)" }}
      onClick={onClose}
    >
      <div
        onClick={e => e.stopPropagation()}
        className="mt-14 w-[360px] max-w-[92vw] rounded-xl overflow-hidden"
        style={{ background: "var(--panel)", border: "1px solid var(--border)", boxShadow: "0 20px 50px -12px rgba(0,0,0,0.6)" }}
      >
        {/* Identity header */}
        <div className="flex items-center gap-3 px-4 py-3 border-b" style={{ borderColor: "var(--border)" }}>
          <div className="grid place-items-center w-9 h-9 rounded-full shrink-0"
            style={{ background: "var(--chip-bg)", border: "1px solid var(--border)", color: "var(--accent)", fontWeight: 700, fontSize: 14 }}>
            {initials(user.display_name, user.email)}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold truncate">{displayName}</div>
            <div className="text-[11px] truncate uppercase tracking-widest" style={{ color: "var(--muted)" }}>{user.role}</div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="grid place-items-center w-7 h-7 rounded-lg shrink-0"
            style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}
          >
            ✕
          </button>
        </div>

        {/* Editor */}
        <div className="px-4 py-3">
          <ProfileForm onUpdated={onUpdated} />
        </div>

        {/* Sign out */}
        <div className="px-4 py-3 border-t" style={{ borderColor: "var(--border)" }}>
          <button
            onClick={onSignOut}
            className="w-full text-sm px-3 py-2 rounded-lg inline-flex items-center justify-center gap-2"
            style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
              <polyline points="16 17 21 12 16 7" /><line x1="21" y1="12" x2="9" y2="12" />
            </svg>
            Sign out
          </button>
        </div>
      </div>
    </div>
  );
}
