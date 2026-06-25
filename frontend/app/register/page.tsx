"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, getAccessToken, setTokens } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { PasswordInput } from "@/components/PasswordInput";
import { AuthCard } from "@/components/auth/AuthCard";
import type { Role } from "@/lib/types";

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("subscriber");
  const [displayName, setDisplayName] = useState("");
  // Business name is REQUIRED when role=trader (enforced server-side too).
  // For subscribers we just don't send it.
  const [businessName, setBusinessName] = useState("");
  const [loading, setLoading] = useState(false);

  // Already signed in? Skip the form and bounce to the root, which
  // role-routes to the right landing page.
  useEffect(() => {
    if (getAccessToken()) router.replace("/");
  }, [router]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (role === "trader" && !businessName.trim()) {
      notify.error("Business name is required for traders");
      return;
    }
    setLoading(true);
    try {
      // Emails are treated case-insensitively — normalize once and use
      // the same value for both /register and the immediate /login that
      // follows so they can't drift apart. toLowerCase is also applied
      // on every keystroke in the input below; this is a belt-and-braces
      // safety net plus a trim for paste-from-clipboard whitespace.
      const normalizedEmail = email.trim().toLowerCase();
      await api("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({
          email: normalizedEmail,
          password,
          role,
          display_name: displayName || null,
          business_name: role === "trader" ? businessName.trim() : null,
        }),
        auth: false,
      });
      const tok = await api<{ access_token: string; refresh_token: string }>(
        "/api/auth/login",
        { method: "POST", body: JSON.stringify({ email: normalizedEmail, password }), auth: false }
      );
      setTokens(tok.access_token, tok.refresh_token);
      notify.success("Account created — check your email to verify it");
      router.replace("/");
    } catch (e) {
      notify.fromError(e, "registration failed");
    } finally {
      setLoading(false);
    }
  }

  const roleBtn = (value: Role, label: string) => {
    const active = role === value;
    return (
      <button
        type="button"
        onClick={() => setRole(value)}
        className="p-2.5 rounded-full text-sm transition-colors focus-ring"
        style={{
          border: `1px solid ${active ? "var(--accent)" : "var(--border)"}`,
          background: active ? "var(--nav-active-bg)" : "transparent",
          color: active ? "var(--accent)" : "var(--text-2)",
          fontWeight: active ? 600 : 500,
        }}
      >
        {label}
      </button>
    );
  };

  return (
    <AuthCard
      title="Create your account"
      subtitle="Start copying or sharing trades in minutes"
      footer={
        <>
          Have an account?{" "}
          <Link href="/login" className="underline" style={{ color: "var(--accent)" }}>
            Sign in
          </Link>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-5">
        <div className="space-y-3">
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Email</label>
            <input className="w-full p-2.5" type="email" autoComplete="email" placeholder="you@example.com"
              // Emails are case-insensitive — lowercase on every
              // keystroke so what the user sees is what we send.
              // inputMode/autoCapitalize/autoCorrect off prevent
              // mobile keyboards from inserting capitals or
              // suggesting corrections.
              value={email} onChange={(e) => setEmail(e.target.value.toLowerCase())} required
              inputMode="email" autoCapitalize="none" autoCorrect="off" spellCheck={false} />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Password</label>
            <PasswordInput className="w-full p-2.5" autoComplete="new-password" placeholder="8+ characters"
              value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Display name (optional)</label>
            <input className="w-full p-2.5" type="text" autoComplete="name"
              value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-2 block" style={{ color: "var(--muted)" }}>I am a</label>
            <div className="grid grid-cols-2 gap-2">
              {roleBtn("subscriber", "Subscriber")}
              {roleBtn("trader", "Trader")}
            </div>
          </div>
          {/* Trader-only: business / brand name. Required server-side too
              (RegisterIn validator) — this is the mandatory app name that
              gets shown to the trader and to every subscriber following
              them, replacing the default "ARK" wordmark. */}
          {role === "trader" && (
            <div>
              <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>
                Business name <span style={{ color: "var(--bad)" }}>*</span>
              </label>
              <input
                className="w-full p-2.5"
                type="text"
                autoComplete="organization"
                value={businessName}
                onChange={(e) => setBusinessName(e.target.value)}
                required
                maxLength={120}
              />
              <div className="text-[11px] mt-1" style={{ color: "var(--muted)" }}>
                Shown as your app name to you and every subscriber who follows you.
              </div>
            </div>
          )}
        </div>

        <button disabled={loading} className="btn-primary w-full py-2.5 text-sm inline-flex items-center justify-center gap-2">
          <span>Create account</span>
          {loading && <Spinner />}
        </button>
      </form>
    </AuthCard>
  );
}
