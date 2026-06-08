"use client";

/**
 * Public contact page — sits OUTSIDE the (app) auth-gated route group so
 * anonymous visitors (SnapTrade reviewers, prospective subscribers) can
 * reach it without a login.
 *
 * Form submission notes
 * ---------------------
 * The form POSTs to `FORM_ENDPOINT`. Three options:
 *
 *  1. Formspree (free for 50 submissions/mo, ~5 min setup):
 *     - sign up at formspree.io → create a form → grab the form id
 *     - replace FORM_ENDPOINT below with `https://formspree.io/f/<id>`
 *
 *  2. A backend route of your own: add POST /api/contact in FastAPI
 *     that emails support@kopyya.com and forward to that.
 *
 *  3. Leave the placeholder — submission shows a friendly "email us
 *     directly" fallback that prefills the mailto link with whatever
 *     the user typed. Works without any 3rd-party service.
 */

import { useState, type FormEvent } from "react";

const SUPPORT_EMAIL = "support@kopyya.com";

// TODO: replace with your Formspree endpoint, or leave as "" to use the
// mailto fallback (still works for SnapTrade review — they just want a
// reachable contact path).
const FORM_ENDPOINT = "";

type Status = "idle" | "sending" | "sent" | "error";

export default function ContactPage() {
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);

    const form = e.currentTarget;
    const data = new FormData(form);

    // No 3rd-party endpoint configured → graceful mailto fallback so the
    // page still works end-to-end and SnapTrade reviewers can verify it.
    if (!FORM_ENDPOINT) {
      const name = String(data.get("name") ?? "");
      const email = String(data.get("email") ?? "");
      const message = String(data.get("message") ?? "");
      const subject = `Contact form — ${name || "Anonymous"}`;
      const body = `${message}\n\n— Sent via the contact form\nFrom: ${email}`;
      window.location.href =
        `mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(subject)}` +
        `&body=${encodeURIComponent(body)}`;
      setStatus("sent");
      form.reset();
      return;
    }

    setStatus("sending");
    try {
      const res = await fetch(FORM_ENDPOINT, {
        method: "POST",
        headers: { Accept: "application/json" },
        body: data,
      });
      if (!res.ok) throw new Error(`Server responded ${res.status}`);
      setStatus("sent");
      form.reset();
    } catch (err) {
      setStatus("error");
      setError(err instanceof Error ? err.message : "Could not send right now.");
    }
  }

  return (
    <main
      className="relative min-h-screen overflow-hidden"
      style={{ background: "var(--bg)", color: "var(--text)" }}
    >
      {/* ── Ambient background ──
          Two radial gradients sit behind everything, fading in and out
          gently to give the page a sense of depth without distracting
          from the form. Pure CSS, no JS animation. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(circle at 12% 18%, rgba(10,115,168,0.22), transparent 38%), " +
            "radial-gradient(circle at 88% 82%, rgba(44,147,197,0.18), transparent 44%)",
        }}
      />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-[0.06]"
        style={{
          backgroundImage:
            "linear-gradient(var(--border-strong) 1px, transparent 1px), " +
            "linear-gradient(90deg, var(--border-strong) 1px, transparent 1px)",
          backgroundSize: "44px 44px",
          maskImage: "radial-gradient(circle at 50% 50%, black 0%, transparent 75%)",
          WebkitMaskImage: "radial-gradient(circle at 50% 50%, black 0%, transparent 75%)",
        }}
      />

      {/* ── Top bar — minimal, just brand + back-to-home anchor ── */}
      <header className="relative z-10 px-6 sm:px-10 py-6 flex items-center justify-between">
        <a
          href="/"
          className="flex items-center gap-2.5 no-underline"
          style={{ color: "var(--text)" }}
        >
          {/* Logo image intentionally hidden — wordmark-only branding
              matches the new sidebar treatment. */}
          <span className="text-base font-semibold tracking-tight">
            ARK
          </span>
        </a>
        <a
          href="/"
          className="text-xs no-underline transition-colors"
          style={{ color: "var(--muted)" }}
        >
          ← Back to home
        </a>
      </header>

      {/* ── Body ── */}
      <section className="relative z-10 px-6 sm:px-10 pb-20">
        <div className="max-w-6xl mx-auto pt-10 sm:pt-16">
          {/* Two-column grid: copy + info cards on the left, form on the
              right. Both columns start at the same top edge (items-start
              + grid level) so the form aligns with the eyebrow chip
              rather than sitting below the headline.
              Column ratio 1fr_1.08fr = the form is ~10% narrower than
              its previous 1.2fr share; combined with the bigger gap
              (gap-20 on lg) the two cards breathe instead of crowding
              each other in the middle. */}
          <div className="grid grid-cols-1 lg:grid-cols-[1.1fr_0.95fr] gap-10 lg:gap-20 items-stretch">
            {/* ── Left column: headline + tagline + info cards ── */}
            <div>
              {/* Eyebrow chip — small, glowing, sets the mood */}
              <div
                className="inline-flex items-center gap-2 rounded-full px-3 py-1 text-[11px] uppercase tracking-widest"
                style={{
                  background: "rgba(10,115,168,0.10)",
                  border: "1px solid rgba(10,115,168,0.28)",
                  color: "var(--accent-2)",
                  boxShadow: "0 0 18px -6px var(--accent-glow)",
                }}
              >
                <span
                  className="inline-block rounded-full"
                  style={{
                    width: 6,
                    height: 6,
                    background: "var(--accent-2)",
                    boxShadow: "0 0 8px var(--accent-2)",
                  }}
                />
                We&apos;d love to hear from you
              </div>

              <h1
                className="mt-5 text-4xl sm:text-5xl font-semibold tracking-tight leading-[1.05]"
                style={{
                  backgroundImage:
                    "linear-gradient(180deg, #ffffff 0%, #b9c5d4 100%)",
                  WebkitBackgroundClip: "text",
                  backgroundClip: "text",
                  WebkitTextFillColor: "transparent",
                  color: "transparent",
                }}
              >
                Get in touch.
              </h1>
              <p
                className="mt-4 text-base sm:text-md leading-relaxed"
                style={{ color: "var(--text-2)" }}
              >
                Questions about copy trading, broker integrations, or your
                account? Drop us a note and a human gets back within one
                business day.
              </p>

              {/* Contact details — stacked cards under the copy */}
              <div className="mt-8 space-y-3.5">
                <InfoCard
                  icon={<IconMail />}
                  label="Email"
                  value={SUPPORT_EMAIL}
                  href={`mailto:${SUPPORT_EMAIL}`}
                />
                <InfoCard
                  icon={<IconClock />}
                  label="Response time"
                  value="Within 2 business days"
                  // hint="Mon–Fri · 9am–6pm ET"
                />
                <InfoCard
                  icon={<IconShield />}
                  label="Account help"
                  value="We never ask for passwords"
                  hint="Broker credentials are encrypted with Fernet (AES-128) and stored only on our backend."
                />
              </div>
            </div>

            {/* Right — the form, glassmorphic card. `flex flex-col h-full`
                lets us stretch to the height of the left column (via the
                grid's items-stretch) and use mt-auto on the bottom
                cluster to pin Send to the lower edge instead of letting
                it float just under the message box. */}
            <form
              onSubmit={onSubmit}
              className="relative rounded-[28px] overflow-hidden flex flex-col h-full"
              style={{
                background:
                  "linear-gradient(180deg, rgba(14,19,24,0.85) 0%, rgba(7,9,11,0.85) 100%)",
                border: "1px solid var(--border-strong)",
                backdropFilter: "blur(8px)",
                WebkitBackdropFilter: "blur(8px)",
                boxShadow:
                  "0 30px 60px -30px rgba(0,0,0,0.6), 0 0 1px rgba(255,255,255,0.05) inset",
              }}
            >
              <div className="p-6 sm:p-7 flex-1 flex flex-col">
                {/* Top cluster: title + 3 fields, naturally top-anchored. */}
                <div className="space-y-5">
                  <div className="flex items-baseline justify-between gap-3 flex-wrap">
                    <h2 className="text-xl font-semibold">Send us a message</h2>
                    <span
                      className="text-[11px]"
                      style={{ color: "var(--muted)" }}
                    >
                      * required
                    </span>
                  </div>

                  <Field label="Name *" htmlFor="name">
                    <input
                      id="name"
                      name="name"
                      required
                      autoComplete="name"
                      placeholder="Your full name"
                    />
                  </Field>

                  <Field label="Email *" htmlFor="email">
                    <input
                      id="email"
                      name="email"
                      type="email"
                      required
                      autoComplete="email"
                      placeholder="you@example.com"
                    />
                  </Field>

                  <Field label="Message *" htmlFor="message">
                    <textarea
                      id="message"
                      name="message"
                      required
                      rows={5}
                      placeholder="How can we help?"
                    />
                  </Field>
                </div>

                {/* Bottom cluster: pinned to bottom via mt-auto so the form
                    matches the left column's height without leaving a gap
                    between fields and Send when the column is short. */}
                <div className="mt-auto pt-6">
                  <button
                    type="submit"
                    disabled={status === "sending"}
                    className="group relative w-full rounded-xl px-5 py-3.5 font-semibold text-sm overflow-hidden transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                    style={{
                      background: "var(--grad-accent)",
                      color: "var(--accent-ink)",
                      boxShadow:
                        "0 12px 30px -10px var(--accent-glow), inset 0 1px 0 rgba(255,255,255,0.12)",
                    }}
                  >
                    {/* Animated shimmer on hover — pure CSS, premium feel */}
                    <span
                      aria-hidden
                      className="absolute inset-0 -translate-x-full bg-gradient-to-r from-transparent via-white/15 to-transparent transition-transform duration-700 group-hover:translate-x-full"
                    />
                    <span className="relative">
                      {status === "sending" ? "Sending…" : "Send message"}
                    </span>
                  </button>

                  {/* Status messages live in a fixed-height slot so the form
                      doesn't jump when they appear. */}
                  <div className="min-h-[20px] text-xs">
                  {status === "sent" && (
                    <p style={{ color: "var(--good)" }}>
                      Thanks — your message is on its way. We&apos;ll reply to{" "}
                      <strong>{SUPPORT_EMAIL}</strong>&apos;s thread within one
                      business day.
                    </p>
                  )}
                  {status === "error" && (
                    <p style={{ color: "var(--bad)" }}>
                      Couldn&apos;t send right now: {error}. Email us directly at{" "}
                      <a
                        href={`mailto:${SUPPORT_EMAIL}`}
                        className="underline"
                        style={{ color: "var(--bad)" }}
                      >
                        {SUPPORT_EMAIL}
                      </a>
                      .
                    </p>
                  )}
                  </div>
                </div>
              </div>
            </form>
          </div>

          {/* ── Footer — small print, trust signals ── */}
          <footer
            className="mt-20 pt-6 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 text-xs"
            style={{
              borderTop: "1px solid var(--border)",
              color: "var(--muted)",
            }}
          >
            <div>
              © {new Date().getFullYear()} ARK. All rights reserved.
            </div>
            <div className="flex items-center gap-4">
              <a
                href={`mailto:${SUPPORT_EMAIL}`}
                className="no-underline transition-colors hover:text-[var(--text-2)]"
                style={{ color: "var(--muted)" }}
              >
                {SUPPORT_EMAIL}
              </a>
              <span aria-hidden>·</span>
              <a
                href="/"
                className="no-underline transition-colors hover:text-[var(--text-2)]"
                style={{ color: "var(--muted)" }}
              >
                Home
              </a>
            </div>
          </footer>
        </div>
      </section>

      {/* Field-styling — kept in one place so the form reads cleanly */}
      <style jsx global>{`
        main input,
        main textarea {
          width: 100%;
          padding: 12px 14px;
          background: rgba(0, 0, 0, 0.30);
          border: 1px solid var(--border);
          border-radius: 10px;
          color: var(--text);
          font-size: 14px;
          line-height: 1.4;
          transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
        }
        main input::placeholder,
        main textarea::placeholder {
          color: var(--faint);
        }
        main input:focus,
        main textarea:focus {
          outline: none;
          border-color: var(--accent);
          box-shadow: 0 0 0 3px rgba(10, 115, 168, 0.18);
          background: rgba(0, 0, 0, 0.45);
        }
        main textarea {
          resize: vertical;
          min-height: 110px;
          font-family: inherit;
        }
      `}</style>
    </main>
  );
}

/** Form field with a small floating-style label sitting above the input. */
function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label
        htmlFor={htmlFor}
        className="block mb-1.5 text-[11px] uppercase tracking-widest font-medium"
        style={{ color: "var(--muted)" }}
      >
        {label}
      </label>
      {children}
    </div>
  );
}

/** Sidebar info card with an icon + label + value. Acts like a chip rather
 *  than a heavy panel so it complements (doesn't compete with) the form. */
function InfoCard({
  icon,
  label,
  value,
  hint,
  href,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  hint?: string;
  href?: string;
}) {
  const inner = (
    <div
      className="rounded-xl px-4 py-4 flex items-start gap-3.5 transition-colors"
      style={{
        background: "rgba(255,255,255,0.025)",
        border: "1px solid var(--border)",
      }}
    >
      <div
        className="shrink-0 rounded-lg p-2"
        style={{
          color: "var(--accent-2)",
          background: "rgba(10,115,168,0.10)",
          border: "1px solid rgba(10,115,168,0.22)",
        }}
      >
        {icon}
      </div>
      <div className="min-w-0">
        <div
          className="text-[10px] uppercase tracking-widest font-medium mb-1"
          style={{ color: "var(--muted)" }}
        >
          {label}
        </div>
        <div className="text-sm font-medium break-words" style={{ color: "var(--text)" }}>
          {value}
        </div>
        {hint && (
          <div className="text-[11px] mt-1 leading-snug" style={{ color: "var(--muted)" }}>
            {hint}
          </div>
        )}
      </div>
    </div>
  );
  return href ? (
    <a
      href={href}
      className="block no-underline focus:outline-none"
      style={{ color: "inherit" }}
    >
      {inner}
    </a>
  ) : (
    inner
  );
}

/* ── Inline SVG icons (no extra deps) ────────────────────────────────── */

function IconMail() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="2" y="4" width="20" height="16" rx="2" />
      <path d="m22 7-10 6L2 7" />
    </svg>
  );
}
function IconClock() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="9" />
      <polyline points="12 7 12 12 15 14" />
    </svg>
  );
}
function IconShield() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  );
}
