"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import type { User } from "@/lib/types";

/** Self-service display-name editor. The name shows across the app (shell,
 *  follow lists, admin views); it updates everywhere on the next fetch after
 *  saving. */
export function ProfileNameCard() {
  const [user, setUser] = useState<User | null>(null);
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api<User>("/api/auth/me")
      .then(u => { setUser(u); setName(u.display_name ?? ""); })
      .catch(e => notify.fromError(e, "Could not load your profile"))
      .finally(() => setLoading(false));
  }, []);

  const dirty = user != null && name.trim() !== (user.display_name ?? "").trim();

  async function save() {
    const v = name.trim();
    if (!v) { notify.warn("Name can't be empty"); return; }
    setSaving(true);
    try {
      const updated = await api<User>("/api/auth/me", {
        method: "PATCH", body: JSON.stringify({ display_name: v }),
      });
      setUser(updated);
      setName(updated.display_name ?? "");
      notify.success("Name updated");
    } catch (e) {
      notify.fromError(e, "Could not update your name");
    } finally {
      setSaving(false);
    }
  }

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
        {loading || !user ? (
          <div className="flex items-center gap-2 text-sm" style={{ color: "var(--muted)" }}>
            <Spinner /> Loading…
          </div>
        ) : (
          <>
            <label className="block text-[10px] uppercase tracking-wider mb-1.5 font-medium" style={{ color: "var(--muted)" }}>
              Display name
            </label>
            <div className="flex items-center gap-2">
              <input
                value={name}
                maxLength={120}
                placeholder="Your name"
                onChange={e => setName(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter" && dirty && !saving) save(); }}
                className="flex-1 text-sm px-3 py-1.5 rounded-lg"
                style={{ background: "var(--bg)", border: "1px solid var(--border)", color: "var(--text)", outline: "none" }}
              />
              <button
                onClick={save}
                disabled={!dirty || saving}
                className="text-sm px-4 py-1.5 rounded-lg inline-flex items-center gap-1.5 font-medium"
                style={{
                  background: dirty ? "var(--accent)" : "var(--panel-2)",
                  color: dirty ? "var(--accent-ink)" : "var(--text-2)",
                  border: "1px solid " + (dirty ? "var(--accent)" : "var(--border)"),
                  opacity: saving ? 0.6 : 1,
                  cursor: !dirty || saving ? "not-allowed" : "pointer",
                }}
              >
                Save {saving && <Spinner />}
              </button>
            </div>
            <p className="text-[11px] mt-2" style={{ color: "var(--muted)" }}>
              Signed in as <span style={{ color: "var(--text-2)" }}>{user.email}</span>.
            </p>
          </>
        )}
      </div>
    </section>
  );
}
