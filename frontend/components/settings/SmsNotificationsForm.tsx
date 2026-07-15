"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { PhoneInput } from "@/components/PhoneInput";
import type { User } from "@/lib/types";

/** The only notification categories that can send SMS. Anything else is in-app
 *  only — our A2P 10DLC campaign is registered with sample messages covering
 *  just these three, and carriers audit live traffic against what's on file.
 *  Adding a category here means filing a new sample with Twilio first. Keep in
 *  sync with _SMS_PREF_EXACT / _SMS_PREF_PREFIX in services/notifications.py. */
type SmsCats = Pick<User, "sms_on_trade_rejected" | "sms_on_auto_actions" | "sms_on_broker_connection">;

const DEFAULT_CATS: SmsCats = {
  sms_on_trade_rejected: true,
  sms_on_auto_actions: true,
  sms_on_broker_connection: true,
};

const SMS_CATEGORIES: { key: keyof SmsCats; label: string; hint: string }[] = [
  { key: "sms_on_trade_rejected", label: "Rejected trades",
    hint: "An order, or your copy of one, was rejected by the broker." },
  { key: "sms_on_auto_actions", label: "Auto liquidation & pauses",
    hint: "A position was auto-liquidated or closed, or copying paused on a daily limit." },
  { key: "sms_on_broker_connection", label: "Broker connection",
    hint: "A broker disconnected and needs reconnecting to resume copying." },
];

const pickCats = (u: SmsCats): SmsCats => ({
  sms_on_trade_rejected: u.sms_on_trade_rejected,
  sms_on_auto_actions: u.sms_on_auto_actions,
  sms_on_broker_connection: u.sms_on_broker_connection,
});

/** SMS notification preferences — phone, consent, and per-category toggles.
 *  Lives on the Settings page (a preference, not an identity detail), so the
 *  avatar profile modal stays a short identity editor. Applies to traders and
 *  subscribers alike, so it renders outside the role-gated sections. */
export function SmsNotificationsForm({ onUpdated }: { onUpdated?: (u: User) => void } = {}) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [phone, setPhone] = useState("");
  const [sms, setSms] = useState(false);
  const [cats, setCats] = useState<SmsCats>(DEFAULT_CATS);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api<User>("/api/auth/me")
      .then(u => {
        setUser(u);
        setPhone(u.phone ?? "");
        setSms(u.sms_notifications_enabled);
        setCats(pickCats(u));
      })
      .catch(e => notify.fromError(e, "Could not load your SMS settings"))
      .finally(() => setLoading(false));
  }, []);

  const dirty = user != null && (
    phone.trim() !== (user.phone ?? "")
    || sms !== user.sms_notifications_enabled
    || SMS_CATEGORIES.some(c => cats[c.key] !== user[c.key])
  );

  async function save() {
    // Accept any format/country: strip spaces/dashes/parens, 00 -> +.
    const p = phone.trim().replace(/[\s\-().]/g, "").replace(/^00/, "+");
    if (p && !/^\+[1-9]\d{6,14}$/.test(p)) {
      notify.warn("Enter your number with country code, e.g. +91 98765 43210");
      return;
    }
    if (sms && !p) { notify.warn("Add a phone number to receive SMS"); return; }
    setSaving(true);
    try {
      const updated = await api<User>("/api/auth/me", {
        method: "PATCH",
        body: JSON.stringify({ phone: p, sms_notifications_enabled: sms, ...cats }),
      });
      setUser(updated);
      setPhone(updated.phone ?? "");
      setSms(updated.sms_notifications_enabled);
      setCats(pickCats(updated));
      onUpdated?.(updated);
      notify.success("SMS settings saved");
    } catch (e) {
      notify.fromError(e, "Could not save your SMS settings");
    } finally {
      setSaving(false);
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
      <div className="flex items-center gap-2">
        <div className="flex-1 min-w-0">
          <PhoneInput value={phone} onChange={setPhone} />
        </div>
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
      <p className="text-[11px] mt-1.5" style={{ color: "var(--muted)" }}>
        Include your country code (any country), e.g. +91 98765 43210 or +1 555 123 4567.
      </p>

      {/* This wording is what our A2P 10DLC campaign registration declares as the
          consent mechanism, and a screenshot of it is filed with the carriers —
          brand, frequency, rates and opt-out have to stay on the checkbox itself.
          Keep it in sync with services/sms.py compose(). */}
      <label className="flex items-start gap-2 mt-3 text-sm cursor-pointer select-none">
        <input
          type="checkbox"
          checked={sms}
          onChange={e => setSms(e.target.checked)}
          className="mt-0.5 shrink-0"
        />
        <span>
          I agree to receive SMS notifications from Kopyya about my account activity.
          Msg frequency varies. Msg &amp; data rates may apply. Reply STOP to opt out,
          HELP for help. See our{" "}
          {/* stopPropagation: without it, clicking the link also toggles the box. */}
          <a
            href="/terms" target="_blank" rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            style={{ color: "var(--accent)", textDecoration: "underline" }}
          >Terms</a>{" "}and{" "}
          <a
            href="/privacy" target="_blank" rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            style={{ color: "var(--accent)", textDecoration: "underline" }}
          >Privacy Policy</a>.
        </span>
      </label>

      {/* Greyed out rather than hidden when SMS is off, so the user can see what
          they'd be signing up for before opting in. */}
      <div
        className="mt-3 pt-3"
        style={{ borderTop: "1px dashed var(--border)", opacity: sms ? 1 : 0.5 }}
      >
        <div className="text-[10px] uppercase tracking-wider mb-2 font-medium" style={{ color: "var(--muted)" }}>
          Text me about
        </div>
        <div className="grid gap-2 sm:grid-cols-3">
          {SMS_CATEGORIES.map(c => (
            <label
              key={c.key}
              className="flex items-start gap-2 text-sm select-none rounded-lg p-2.5"
              style={{
                cursor: sms ? "pointer" : "not-allowed",
                background: "rgba(255,255,255,0.02)",
                border: "1px solid var(--border)",
              }}
            >
              <input
                type="checkbox"
                disabled={!sms}
                checked={cats[c.key]}
                onChange={e => setCats(p => ({ ...p, [c.key]: e.target.checked }))}
                className="mt-0.5 shrink-0"
              />
              <span>
                {c.label}
                <span className="block text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>{c.hint}</span>
              </span>
            </label>
          ))}
        </div>
        <p className="text-[11px] mt-2" style={{ color: "var(--muted)" }}>
          Everything else — follow requests, filled orders — stays in the app only.
        </p>
      </div>
    </>
  );
}
