"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, setTokens } from "@/lib/api";
import { notify } from "@/lib/toast";
import type { Role } from "@/lib/types";

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("subscriber");
  const [displayName, setDisplayName] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
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
      notify.success("Account created");
      router.replace("/");
    } catch (e) {
      notify.fromError(e, "registration failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen grid place-items-center p-6">
      <form onSubmit={submit} className="card w-full max-w-md p-8 space-y-5">
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
            <div className="text-xs" style={{ color: "var(--muted)" }}>Create an account</div>
          </div>
        </div>

        <div className="space-y-3">
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Email</label>
            <input className="w-full p-2.5" type="email" autoComplete="email" placeholder="you@example.com"
              value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          <div>
            <label className="text-[11px] uppercase tracking-wider mb-1 block" style={{ color: "var(--muted)" }}>Password</label>
            <input className="w-full p-2.5" type="password" autoComplete="new-password" placeholder="8+ characters"
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
              <button type="button" onClick={() => setRole("subscriber")}
                className="p-2.5 rounded-full text-sm transition-colors"
                style={{
                  border: `1px solid ${role === "subscriber" ? "var(--accent)" : "var(--border)"}`,
                  background: role === "subscriber" ? "rgba(10,115,168,0.12)" : "transparent",
                  color: role === "subscriber" ? "var(--accent)" : "var(--text-2)",
                  fontWeight: role === "subscriber" ? 600 : 500,
                }}
              >Subscriber</button>
              <button type="button" onClick={() => setRole("trader")}
                className="p-2.5 rounded-full text-sm transition-colors"
                style={{
                  border: `1px solid ${role === "trader" ? "var(--accent)" : "var(--border)"}`,
                  background: role === "trader" ? "rgba(10,115,168,0.12)" : "transparent",
                  color: role === "trader" ? "var(--accent)" : "var(--text-2)",
                  fontWeight: role === "trader" ? 600 : 500,
                }}
              >Trader</button>
            </div>
          </div>
        </div>

        <button disabled={loading} className="btn-primary w-full py-2.5 text-sm">
          {loading ? "Creating…" : "Create account"}
        </button>

        <div className="text-center text-sm" style={{ color: "var(--muted)" }}>
          Have an account? <Link href="/login" className="underline" style={{ color: "var(--accent)" }}>Sign in</Link>
        </div>
      </form>
    </main>
  );
}
