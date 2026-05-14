"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api, setTokens } from "@/lib/api";
import { notify } from "@/lib/toast";

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
      notify.success("Welcome back");
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
          <div
            className="grid place-items-center"
            style={{
              width: 40, height: 40,
              clipPath: "polygon(25% 5%, 75% 5%, 100% 50%, 75% 95%, 25% 95%, 0% 50%)",
              background: "linear-gradient(135deg, var(--accent) 0%, #006fa3 100%)",
            }}
          >
            <span style={{ color: "var(--accent-ink)", fontWeight: 800 }}>Ƈ</span>
          </div>
          <div>
            <div style={{ fontWeight: 700, fontSize: 18, letterSpacing: "0.02em" }}>COPYTRADE</div>
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
          className="btn-primary w-full py-2.5 text-sm"
        >
          {loading ? "Signing in…" : "Sign in"}
        </button>

        <div className="text-center text-sm" style={{ color: "var(--muted)" }}>
          New here? <Link href="/register" className="underline" style={{ color: "var(--accent)" }}>Create an account</Link>
        </div>
      </form>
    </main>
  );
}
