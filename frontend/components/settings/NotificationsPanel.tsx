"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";

interface NotificationPrefs {
  email_enabled: boolean;
  sms_enabled: boolean;
  event_overrides: Record<string, { email?: boolean; sms?: boolean }>;
  phone_number: string | null;
  phone_verified: boolean;
  sms_available: boolean;
}

// Keep in sync with backend NOTIFY_EVENTS.
const EVENTS = [
  { key: "order.filled", label: "Order filled", hint: "A mirror order fills at your broker." },
  { key: "order.rejected", label: "Order rejected", hint: "An order is rejected (actionable failures)." },
] as const;

const E164 = /^\+\d{8,15}$/;

// ── Small UI atoms ──────────────────────────────────────────────────────────
function Switch({
  on, onToggle, disabled,
}: { on: boolean; onToggle: () => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={disabled}
      onClick={onToggle}
      className="relative inline-flex items-center rounded-full transition-colors focus-ring"
      style={{
        width: 38, height: 22,
        background: on ? "var(--accent)" : "var(--panel-2)",
        border: "1px solid " + (on ? "var(--accent)" : "var(--border)"),
        opacity: disabled ? 0.45 : 1,
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      <span
        className="inline-block rounded-full transition-transform"
        style={{
          width: 16, height: 16, background: "#fff",
          transform: on ? "translateX(18px)" : "translateX(3px)",
        }}
      />
    </button>
  );
}

function ChannelCheck({
  checked, disabled, onChange, title,
}: { checked: boolean; disabled?: boolean; onChange?: (v: boolean) => void; title?: string }) {
  return (
    <input
      type="checkbox"
      checked={checked}
      disabled={disabled}
      title={title}
      onChange={e => onChange?.(e.target.checked)}
      className="w-4 h-4 accent-[var(--accent)]"
      style={{ cursor: disabled ? "not-allowed" : "pointer" }}
    />
  );
}

export function NotificationsPanel() {
  const [prefs, setPrefs] = useState<NotificationPrefs | null>(null);
  const [loading, setLoading] = useState(true);
  const [phase, setPhase] = useState<"view" | "edit" | "code_sent">("edit");
  const [phoneInput, setPhoneInput] = useState("");
  const [codeInput, setCodeInput] = useState("");
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const p = await api<NotificationPrefs>("/api/settings/notifications");
        setPrefs(p);
        setPhoneInput(p.phone_number ?? "");
        setPhase(p.phone_verified ? "view" : "edit");
      } catch (e) {
        notify.fromError(e, "Could not load notification settings");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  async function patch(body: Record<string, unknown>) {
    try {
      const updated = await api<NotificationPrefs>("/api/settings/notifications", {
        method: "PATCH", body: JSON.stringify(body),
      });
      setPrefs(updated);
    } catch (e) {
      notify.fromError(e, "Could not update preferences");
    }
  }

  function chOn(ev: string, ch: "email" | "sms"): boolean {
    const o = prefs?.event_overrides?.[ev] ?? {};
    return o[ch] ?? true;
  }

  async function toggleEvent(ev: string, ch: "email" | "sms", value: boolean) {
    if (!prefs) return;
    const next = { ...(prefs.event_overrides ?? {}) };
    next[ev] = { ...(next[ev] ?? {}), [ch]: value };
    await patch({ event_overrides: next });
  }

  async function sendCode() {
    const phone = phoneInput.trim().replace(/\s/g, "");
    if (!E164.test(phone)) {
      notify.warn("Enter the number in E.164 format, e.g. +13412446121");
      return;
    }
    setSending(true);
    try {
      await api("/api/settings/phone", { method: "POST", body: JSON.stringify({ phone_number: phone }) });
      setPhase("code_sent");
      notify.success("Verification code sent by SMS");
    } catch (e) {
      notify.fromError(e, "Could not send the code");
    } finally {
      setSending(false);
    }
  }

  async function verify() {
    if (!codeInput.trim()) { notify.warn("Enter the code from the SMS"); return; }
    setVerifying(true);
    try {
      const updated = await api<NotificationPrefs>("/api/settings/phone/verify", {
        method: "POST", body: JSON.stringify({ code: codeInput.trim() }),
      });
      setPrefs(updated);
      setPhoneInput(updated.phone_number ?? "");
      setCodeInput("");
      setPhase("view");
      notify.success("Phone verified — SMS notifications are on");
    } catch (e) {
      notify.fromError(e, "That code didn't match");
    } finally {
      setVerifying(false);
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
            <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" />
          </svg>
        </span>
        <div className="min-w-0">
          <h2 className="text-sm font-semibold leading-tight">Notifications</h2>
          <p className="text-[11px] mt-1 leading-snug" style={{ color: "var(--muted)" }}>
            Choose how you hear about your orders. In-app alerts are always on.
          </p>
        </div>
      </header>

      <div className="px-4 py-3 space-y-4">
        {loading || !prefs ? (
          <div className="flex items-center gap-2 text-sm" style={{ color: "var(--muted)" }}>
            <Spinner /> Loading…
          </div>
        ) : (
          <>
            {/* Master channel switches */}
            <div className="space-y-2.5">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm font-medium">Email</div>
                  <div className="text-[11px]" style={{ color: "var(--muted)" }}>Alerts to your account email.</div>
                </div>
                <Switch on={prefs.email_enabled} onToggle={() => patch({ email_enabled: !prefs.email_enabled })} />
              </div>

              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm font-medium">SMS</div>
                  <div className="text-[11px]" style={{ color: "var(--muted)" }}>
                    {prefs.phone_verified ? "Text messages to your verified number." : "Verify a phone number below to enable."}
                  </div>
                </div>
                <Switch
                  on={prefs.sms_enabled}
                  disabled={!prefs.phone_verified}
                  onToggle={() => {
                    if (!prefs.phone_verified) { notify.warn("Verify your phone number first"); return; }
                    patch({ sms_enabled: !prefs.sms_enabled });
                  }}
                />
              </div>
            </div>

            {/* Phone verification */}
            {!prefs.sms_available ? (
              <div className="text-[11px] rounded-lg px-3 py-2" style={{ background: "rgba(250,204,21,0.10)", color: "#facc15" }}>
                SMS isn&apos;t configured on the server yet — email + in-app still work.
              </div>
            ) : (
              <div className="rounded-lg p-3" style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)" }}>
                <div className="text-[10px] uppercase tracking-wider mb-2 font-medium" style={{ color: "var(--muted)" }}>
                  Phone number
                </div>

                {phase === "view" ? (
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2 text-sm">
                      <span className="font-medium">{prefs.phone_number}</span>
                      <span className="text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider"
                        style={{ background: "rgba(34,197,94,0.12)", color: "#22c55e" }}>Verified</span>
                    </div>
                    <button onClick={() => setPhase("edit")} className="text-xs px-3 py-1.5 rounded-lg"
                      style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}>
                      Change
                    </button>
                  </div>
                ) : phase === "edit" ? (
                  <div className="flex items-center gap-2">
                    <input
                      type="tel" placeholder="+13412446121" value={phoneInput}
                      onChange={e => setPhoneInput(e.target.value)}
                      className="flex-1 text-sm px-3 py-1.5 rounded-lg"
                      style={{ background: "var(--bg)", border: "1px solid var(--border)", color: "var(--text)", outline: "none" }}
                    />
                    <button onClick={sendCode} disabled={sending}
                      className="text-sm px-3 py-1.5 rounded-lg inline-flex items-center gap-1.5 font-medium"
                      style={{ background: "var(--accent)", color: "var(--accent-ink)", opacity: sending ? 0.6 : 1 }}>
                      Send code {sending && <Spinner />}
                    </button>
                  </div>
                ) : (
                  <div className="space-y-2">
                    <div className="text-[11px]" style={{ color: "var(--muted)" }}>
                      Enter the code we texted to <span style={{ color: "var(--text)" }}>{phoneInput}</span>.
                    </div>
                    <div className="flex items-center gap-2">
                      <input
                        inputMode="numeric" placeholder="123456" value={codeInput}
                        onChange={e => setCodeInput(e.target.value)}
                        className="flex-1 text-sm px-3 py-1.5 rounded-lg tracking-widest"
                        style={{ background: "var(--bg)", border: "1px solid var(--border)", color: "var(--text)", outline: "none" }}
                      />
                      <button onClick={verify} disabled={verifying}
                        className="text-sm px-3 py-1.5 rounded-lg inline-flex items-center gap-1.5 font-medium"
                        style={{ background: "var(--accent)", color: "var(--accent-ink)", opacity: verifying ? 0.6 : 1 }}>
                        Verify {verifying && <Spinner />}
                      </button>
                      <button onClick={() => setPhase("edit")} className="text-xs px-2 py-1.5 rounded-lg"
                        style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}>
                        Back
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Per-event matrix */}
            <div>
              <div className="text-[10px] uppercase tracking-wider mb-2 font-medium" style={{ color: "var(--muted)" }}>
                Per-event
              </div>
              <div className="rounded-lg overflow-hidden" style={{ border: "1px solid var(--border)" }}>
                <div className="grid items-center px-3 py-2 text-[10px] uppercase tracking-wider"
                  style={{ gridTemplateColumns: "1fr 52px 52px 52px", background: "rgba(255,255,255,0.03)", color: "var(--muted)" }}>
                  <span>Event</span>
                  <span className="text-center">Email</span>
                  <span className="text-center">SMS</span>
                  <span className="text-center">In-app</span>
                </div>
                {EVENTS.map((ev, i) => (
                  <div key={ev.key} className="grid items-center px-3 py-2.5"
                    style={{ gridTemplateColumns: "1fr 52px 52px 52px", borderTop: i === 0 ? "none" : "1px solid var(--border)" }}>
                    <div>
                      <div className="text-sm font-medium">{ev.label}</div>
                      <div className="text-[10px]" style={{ color: "var(--muted)" }}>{ev.hint}</div>
                    </div>
                    <div className="text-center">
                      <ChannelCheck
                        checked={prefs.email_enabled && chOn(ev.key, "email")}
                        disabled={!prefs.email_enabled}
                        title={prefs.email_enabled ? undefined : "Turn on the Email channel above"}
                        onChange={v => toggleEvent(ev.key, "email", v)}
                      />
                    </div>
                    <div className="text-center">
                      <ChannelCheck
                        checked={prefs.sms_enabled && chOn(ev.key, "sms")}
                        disabled={!prefs.sms_enabled}
                        title={prefs.sms_enabled ? undefined : "Verify a phone + turn on SMS above"}
                        onChange={v => toggleEvent(ev.key, "sms", v)}
                      />
                    </div>
                    <div className="text-center">
                      <ChannelCheck checked disabled title="In-app is always on" />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
