"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api, ApiError, setTokens } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    setLoading(true);
    try {
      const res = await api<{ access_token: string; refresh_token: string }>(
        "/api/auth/login",
        { method: "POST", body: JSON.stringify({ email, password }), auth: false }
      );
      setTokens(res.access_token, res.refresh_token);
      router.replace("/brokers");
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : "login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen grid place-items-center p-6">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 p-6 rounded-lg" style={{background: "var(--panel)", border: "1px solid var(--border)"}}>
        <h1 className="text-xl font-semibold">Sign in</h1>
        <input
          className="w-full p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}}
          type="email" placeholder="Email" value={email}
          onChange={(e) => setEmail(e.target.value)} required
        />
        <input
          className="w-full p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}}
          type="password" placeholder="Password" value={password}
          onChange={(e) => setPassword(e.target.value)} required
        />
        {err && <p className="text-sm" style={{color: "var(--bad)"}}>{err}</p>}
        <button
          disabled={loading}
          className="w-full p-2 rounded font-medium"
          style={{background: "var(--accent)", color: "#06121f"}}
        >
          {loading ? "Signing in…" : "Sign in"}
        </button>
        <p className="text-sm" style={{color: "var(--muted)"}}>
          New here? <Link href="/register" className="underline">Create an account</Link>
        </p>
      </form>
    </main>
  );
}
