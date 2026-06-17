"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api, getAccessToken, setTokens } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { PasswordInput } from "@/components/PasswordInput";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);

  // Already signed in? Don't show the form — bounce to the root, which
  // role-routes to the right landing page. Guards against an authenticated
  // user landing back on /login via the URL, back button, or a stale link.
  useEffect(() => {
    if (getAccessToken()) router.replace("/");
  }, [router]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    try {
      // Emails are treated as case-insensitive identifiers — normalize
      // here so "User@Example.com" matches the stored "user@example.com".
      // toLowerCase is also applied on every keystroke below, so this
      // is a belt-and-braces safety net (and the trim catches stray
      // whitespace from paste).
      const normalizedEmail = email.trim().toLowerCase();
      const res = await api<{ access_token: string; refresh_token: string }>(
        "/api/auth/login",
        { method: "POST", body: JSON.stringify({ email: normalizedEmail, password }), auth: false }
      );
      setTokens(res.access_token, res.refresh_token);
      // Root page handles role-aware landing (trader → /trade-panel, subscriber → /trades).
      router.replace("/");
    } catch (e) {
      notify.fromError(e, "login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen grid place-items-center p-6">
      <form
        onSubmit={submit}
        className="card w-full max-w-md p-8 space-y-5"
      >
        <div className="text-center">
          <div style={{ fontWeight: 700, fontSize: 24, letterSpacing: "0.02em" }}>Sign In</div>
        </div>

        <div className="space-y-3">
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>
              Email
            </label>
            <input
              className="w-full p-2.5"
              type="email" autoComplete="email" placeholder="you@example.com"
              // Emails are case-insensitive — store and display the
              // lowercase form so what the user sees is what we send,
              // and they can't end up with a "User@" stored on the
              // server that won't match a "user@" sign-in.
              value={email} onChange={(e) => setEmail(e.target.value.toLowerCase())} required
              inputMode="email"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
            />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>
              Password
            </label>
            <PasswordInput
              className="w-full p-2.5"
              autoComplete="current-password" placeholder="••••••••"
              value={password} onChange={(e) => setPassword(e.target.value)} required
            />
          </div>
        </div>

        <button
          disabled={loading}
          className="btn-primary w-full py-2.5 text-sm inline-flex items-center justify-center gap-2"
        >
          <span>Sign in</span>
          {loading && <Spinner />}
        </button>

        <div className="text-center text-sm" style={{ color: "var(--muted)" }}>
          New here? <Link href="/register" className="underline" style={{ color: "var(--accent)" }}>Create an account</Link>
        </div>
      </form>
    </main>
  );
}
