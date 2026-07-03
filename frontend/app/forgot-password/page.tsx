"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import { forgotPassword } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { AuthCard } from "@/components/auth/AuthCard";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [sent, setSent] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    try {
      await forgotPassword(email);
      // The API intentionally returns the same response whether or not the
      // email exists, so we always show the same confirmation.
      setSent(true);
    } catch (e) {
      notify.fromError(e, "could not send reset email");
    } finally {
      setLoading(false);
    }
  }

  return (
    <AuthCard
      title="Reset password"
      subtitle={sent ? undefined : "We'll email you a link to choose a new password"}
    >
      {sent ? (
        <div className="space-y-5">
          <p className="text-sm text-center" style={{ color: "var(--muted)" }}>
            If an account exists for <strong>{email}</strong>, we&rsquo;ve sent a
            reset link. Check your inbox (and spam) and follow the link to choose
            a new password.
          </p>
          <Link
            href="/login"
            className="btn-primary w-full py-2.5 text-sm inline-flex items-center justify-center"
          >
            Back to sign in
          </Link>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-5">
          <div>
            <label htmlFor="fp-email" className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>
              Email
            </label>
            <input
              id="fp-email"
              className="w-full p-2.5"
              type="email"
              autoComplete="email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </div>
          <button
            disabled={loading}
            className="btn-primary w-full py-2.5 text-sm inline-flex items-center justify-center gap-2"
          >
            <span>Send reset link</span>
            {loading && <Spinner />}
          </button>
          <div className="text-center text-sm" style={{ color: "var(--muted)" }}>
            Remembered it?{" "}
            <Link href="/login" className="underline" style={{ color: "var(--accent)" }}>
              Sign in
            </Link>
          </div>
        </form>
      )}
    </AuthCard>
  );
}
