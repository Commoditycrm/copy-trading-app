"use client";

import { FormEvent, Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { resetPassword } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { PasswordInput } from "@/components/PasswordInput";

const MIN_LEN = 8;

function ResetPasswordForm() {
  const router = useRouter();
  const token = useSearchParams().get("token") ?? "";

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (password.length < MIN_LEN) {
      notify.error(`Password must be at least ${MIN_LEN} characters.`);
      return;
    }
    if (password !== confirm) {
      notify.error("Passwords don't match.");
      return;
    }
    setLoading(true);
    try {
      await resetPassword(token, password);
      notify.success("Password reset. Please sign in.");
      router.replace("/login");
    } catch (e) {
      notify.fromError(e, "could not reset password");
    } finally {
      setLoading(false);
    }
  }

  // No token in the URL → the link is malformed or was opened directly.
  if (!token) {
    return (
      <div className="space-y-5">
        <p className="text-sm text-center" style={{ color: "var(--muted)" }}>
          This reset link is invalid or incomplete. Please request a new one.
        </p>
        <Link
          href="/forgot-password"
          className="btn-primary w-full py-2.5 text-sm inline-flex items-center justify-center"
        >
          Request a new link
        </Link>
      </div>
    );
  }

  return (
    <form onSubmit={submit} className="space-y-5">
      <p className="text-sm text-center" style={{ color: "var(--muted)" }}>
        Choose a new password for your account.
      </p>
      <div>
        <label
          className="text-[11px] uppercase tracking-wider mb-1 block"
          style={{ color: "var(--muted)" }}
        >
          New password
        </label>
        <PasswordInput
          className="w-full p-2.5"
          autoComplete="new-password"
          placeholder="••••••••"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          minLength={MIN_LEN}
          required
        />
      </div>
      <div>
        <label
          className="text-[11px] uppercase tracking-wider mb-1 block"
          style={{ color: "var(--muted)" }}
        >
          Confirm new password
        </label>
        <PasswordInput
          className="w-full p-2.5"
          autoComplete="new-password"
          placeholder="••••••••"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          minLength={MIN_LEN}
          required
        />
      </div>
      <button
        disabled={loading}
        className="btn-primary w-full py-2.5 text-sm inline-flex items-center justify-center gap-2"
      >
        <span>Reset password</span>
        {loading && <Spinner />}
      </button>
      <div className="text-center text-sm" style={{ color: "var(--muted)" }}>
        <Link href="/login" className="underline" style={{ color: "var(--accent)" }}>
          Back to sign in
        </Link>
      </div>
    </form>
  );
}

export default function ResetPasswordPage() {
  return (
    <main className="min-h-screen grid place-items-center p-6">
      <div className="card w-full max-w-md p-8 space-y-5">
        <div className="text-center">
          <div style={{ fontWeight: 700, fontSize: 24, letterSpacing: "0.02em" }}>
            Set a new password
          </div>
        </div>
        <Suspense fallback={<div className="text-center"><Spinner /></div>}>
          <ResetPasswordForm />
        </Suspense>
      </div>
    </main>
  );
}
