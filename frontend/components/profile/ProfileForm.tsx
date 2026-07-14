"use client";

import { useEffect, useState } from "react";
import { api, changeEmail } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import type { User } from "@/lib/types";

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

/** Self-service profile editor — display name + email change. Wrapper-free so
 *  it can drop into either the Settings card or the avatar modal. `onUpdated`
 *  fires after a successful save so the app shell can refresh the shown name. */
export function ProfileForm({ onUpdated }: { onUpdated?: (u: User) => void } = {}) {
  const [user, setUser] = useState<User | null>(null);
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [emailOpen, setEmailOpen] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [pwd, setPwd] = useState("");
  const [sendingEmail, setSendingEmail] = useState(false);
  const [phone, setPhone] = useState("");
  const [sms, setSms] = useState(false);
  const [savingSms, setSavingSms] = useState(false);

  useEffect(() => {
    api<User>("/api/auth/me")
      .then(u => {
        setUser(u); setName(u.display_name ?? "");
        setPhone(u.phone ?? ""); setSms(u.sms_notifications_enabled);
      })
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
      onUpdated?.(updated);
      notify.success("Name updated");
    } catch (e) {
      notify.fromError(e, "Could not update your name");
    } finally {
      setSaving(false);
    }
  }

  const smsDirty = user != null
    && (phone.trim() !== (user.phone ?? "") || sms !== user.sms_notifications_enabled);

  async function saveSms() {
    // Accept any format/country: strip spaces/dashes/parens, 00 -> +.
    const p = phone.trim().replace(/[\s\-().]/g, "").replace(/^00/, "+");
    if (p && !/^\+[1-9]\d{6,14}$/.test(p)) {
      notify.warn("Enter your number with country code, e.g. +91 98765 43210");
      return;
    }
    if (sms && !p) { notify.warn("Add a phone number to receive SMS"); return; }
    setSavingSms(true);
    try {
      const updated = await api<User>("/api/auth/me", {
        method: "PATCH",
        body: JSON.stringify({ phone: p, sms_notifications_enabled: sms }),
      });
      setUser(updated);
      setPhone(updated.phone ?? "");
      setSms(updated.sms_notifications_enabled);
      onUpdated?.(updated);
      notify.success("SMS settings saved");
    } catch (e) {
      notify.fromError(e, "Could not save your SMS settings");
    } finally {
      setSavingSms(false);
    }
  }

  async function submitEmailChange() {
    const e = newEmail.trim().toLowerCase();
    if (!EMAIL_RE.test(e)) { notify.warn("Enter a valid email address"); return; }
    if (user && e === user.email.toLowerCase()) { notify.warn("That's already your email"); return; }
    if (!pwd) { notify.warn("Enter your current password to confirm"); return; }
    setSendingEmail(true);
    try {
      const r = await changeEmail(e, pwd);
      notify.success(r.detail || `Confirmation link sent to ${e}`);
      setEmailOpen(false); setNewEmail(""); setPwd("");
    } catch (err) {
      notify.fromError(err, "Could not start the email change");
    } finally {
      setSendingEmail(false);
    }
  }

  if (loading || !user) {
    return (
      <div className="flex items-center gap-2 text-sm" style={{ color: "var(--muted)" }}>
        <Spinner /> Loading…
      </div>
    );
  }

  return (
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

      {/* Email */}
      <div className="mt-4 pt-3" style={{ borderTop: "1px solid var(--border)" }}>
        <label className="block text-[10px] uppercase tracking-wider mb-1.5 font-medium" style={{ color: "var(--muted)" }}>
          Email
        </label>
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm min-w-0">
            <span className="truncate">{user.email}</span>
            {user.email_verified ? (
              <span className="text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider shrink-0"
                style={{ background: "rgba(34,197,94,0.12)", color: "#22c55e" }}>Verified</span>
            ) : (
              <span className="text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider shrink-0"
                style={{ background: "rgba(250,204,21,0.12)", color: "#facc15" }}>Unverified</span>
            )}
          </div>
          <button
            onClick={() => { setEmailOpen(o => !o); setNewEmail(""); setPwd(""); }}
            className="text-xs px-3 py-1.5 rounded-lg shrink-0"
            style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}
          >
            {emailOpen ? "Cancel" : "Change"}
          </button>
        </div>

        {emailOpen && (
          <div className="mt-2 space-y-2 rounded-lg p-3" style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
            <input
              type="email" placeholder="New email address" value={newEmail}
              onChange={e => setNewEmail(e.target.value)}
              className="w-full text-sm px-3 py-1.5 rounded-lg"
              style={{ background: "var(--bg)", border: "1px solid var(--border)", color: "var(--text)", outline: "none" }}
            />
            <input
              type="password" placeholder="Current password" value={pwd}
              onChange={e => setPwd(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter" && !sendingEmail) submitEmailChange(); }}
              className="w-full text-sm px-3 py-1.5 rounded-lg"
              style={{ background: "var(--bg)", border: "1px solid var(--border)", color: "var(--text)", outline: "none" }}
            />
            <button
              onClick={submitEmailChange}
              disabled={sendingEmail}
              className="text-sm px-4 py-1.5 rounded-lg inline-flex items-center gap-1.5 font-medium"
              style={{ background: "var(--accent)", color: "var(--accent-ink)", opacity: sendingEmail ? 0.6 : 1 }}
            >
              Send confirmation link {sendingEmail && <Spinner />}
            </button>
            <p className="text-[11px]" style={{ color: "var(--muted)" }}>
              We&apos;ll email a confirmation link to the new address — the change takes effect once you click it.
            </p>
          </div>
        )}
      </div>

      {/* SMS notifications */}
      <div className="mt-4 pt-3" style={{ borderTop: "1px solid var(--border)" }}>
        <label className="block text-[10px] uppercase tracking-wider mb-1.5 font-medium" style={{ color: "var(--muted)" }}>
          SMS notifications
        </label>
        <div className="flex items-center gap-2">
          <input
            type="tel" value={phone} maxLength={24} placeholder="+91 98765 43210"
            onChange={e => setPhone(e.target.value)}
            className="flex-1 text-sm px-3 py-1.5 rounded-lg"
            style={{ background: "var(--bg)", border: "1px solid var(--border)", color: "var(--text)", outline: "none" }}
          />
          <button
            onClick={saveSms}
            disabled={!smsDirty || savingSms}
            className="text-sm px-4 py-1.5 rounded-lg inline-flex items-center gap-1.5 font-medium"
            style={{
              background: smsDirty ? "var(--accent)" : "var(--panel-2)",
              color: smsDirty ? "var(--accent-ink)" : "var(--text-2)",
              border: "1px solid " + (smsDirty ? "var(--accent)" : "var(--border)"),
              opacity: savingSms ? 0.6 : 1,
              cursor: !smsDirty || savingSms ? "not-allowed" : "pointer",
            }}
          >
            Save {savingSms && <Spinner />}
          </button>
        </div>
        <label className="flex items-center gap-2 mt-2.5 text-sm cursor-pointer select-none">
          <input type="checkbox" checked={sms} onChange={e => setSms(e.target.checked)} />
          <span>Text me my notifications</span>
        </label>
        <p className="text-[11px] mt-1.5" style={{ color: "var(--muted)" }}>
          Include your country code (any country), e.g. +91 98765 43210 or +1 555 123 4567. Standard message rates may apply.
        </p>
      </div>
    </>
  );
}
