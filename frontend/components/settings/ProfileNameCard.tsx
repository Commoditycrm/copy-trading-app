"use client";

import { ProfileForm } from "@/components/profile/ProfileForm";

/** Profile section on the Settings page — wraps the shared ProfileForm in a
 *  card. The same form also lives in the avatar profile modal. */
export function ProfileNameCard() {
  return (
    <section
      className="rounded-xl border overflow-hidden"
      style={{
        borderColor: "var(--border)",
        background: "linear-gradient(180deg, var(--panel) 0%, rgba(0,0,0,0.18) 100%)",
        boxShadow: "0 1px 0 rgba(255,255,255,0.03) inset, 0 8px 24px -16px rgba(0,0,0,0.5)",
      }}
    >
      <header className="flex items-start gap-2.5 px-4 py-3 border-b" style={{ borderColor: "var(--border)" }}>
        <span className="grid place-items-center w-7 h-7 rounded-md shrink-0"
          style={{ background: "rgba(255,255,255,0.04)", color: "var(--accent)" }}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="4" />
          </svg>
        </span>
        <div className="min-w-0">
          <h2 className="text-sm font-semibold leading-tight">Profile</h2>
          <p className="text-[11px] mt-1 leading-snug" style={{ color: "var(--muted)" }}>
            Your display name — shown across the app.
          </p>
        </div>
      </header>

      <div className="px-4 py-3">
        <ProfileForm />
      </div>
    </section>
  );
}
