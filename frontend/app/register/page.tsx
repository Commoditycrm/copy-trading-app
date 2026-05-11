"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, ApiError, setTokens } from "@/lib/api";
import type { Role } from "@/lib/types";

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("subscriber");
  const [displayName, setDisplayName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    setLoading(true);
    try {
      await api("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({ email, password, role, display_name: displayName || null }),
        auth: false,
      });
      const tok = await api<{ access_token: string; refresh_token: string }>(
        "/api/auth/login",
        { method: "POST", body: JSON.stringify({ email, password }), auth: false }
      );
      setTokens(tok.access_token, tok.refresh_token);
      router.replace("/brokers");
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : "registration failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen grid place-items-center p-6">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 p-6 rounded-lg" style={{background: "var(--panel)", border: "1px solid var(--border)"}}>
        <h1 className="text-xl font-semibold">Create account</h1>
        <input className="w-full p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}} type="email" placeholder="Email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        <input className="w-full p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}} type="password" placeholder="Password (8+ chars)" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} />
        <input className="w-full p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}} type="text" placeholder="Display name (optional)" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        <div className="space-y-2">
          <label className="text-sm" style={{color: "var(--muted)"}}>I am a:</label>
          <div className="flex gap-2">
            <button type="button" onClick={() => setRole("subscriber")} className="flex-1 p-2 rounded border" style={{borderColor: role === "subscriber" ? "var(--accent)" : "var(--border)"}}>Subscriber</button>
            <button type="button" onClick={() => setRole("trader")} className="flex-1 p-2 rounded border" style={{borderColor: role === "trader" ? "var(--accent)" : "var(--border)"}}>Trader</button>
          </div>
        </div>
        {err && <p className="text-sm" style={{color: "var(--bad)"}}>{err}</p>}
        <button disabled={loading} className="w-full p-2 rounded font-medium" style={{background: "var(--accent)", color: "#06121f"}}>
          {loading ? "Creating…" : "Create account"}
        </button>
        <p className="text-sm" style={{color: "var(--muted)"}}>
          Have an account? <Link href="/login" className="underline">Sign in</Link>
        </p>
      </form>
    </main>
  );
}
