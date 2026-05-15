"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api, setTokens } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    try {
      const res = await api<{ access_token: string; refresh_token: string }>(
        "/api/auth/login",
        { method: "POST", body: JSON.stringify({ email, password }), auth: false }
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
        <div className="flex items-center gap-3">
          <img
            src="/brand-icon.avif"
            alt="The Option Haven"
            width={45}
            height={45}
            style={{ width: 45, height: 45, borderRadius: 8, objectFit: "cover" }}
          />
          <div>
            <div style={{ fontWeight: 700, fontSize: 18, letterSpacing: "0.02em" }}>The Option Haven</div>
            <div className="text-xs" style={{ color: "var(--muted)" }}>Sign in to your account</div>
          </div>
        </div>

        <div className="space-y-3">
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>
              Email
            </label>
            <input
              className="w-full p-2.5"
              type="email" autoComplete="email" placeholder="you@example.com"
              value={email} onChange={(e) => setEmail(e.target.value)} required
            />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>
              Password
            </label>
            <input
              className="w-full p-2.5"
              type="password" autoComplete="current-password" placeholder="••••••••"
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
