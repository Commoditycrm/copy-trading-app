"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError, clearTokens, getAccessToken } from "@/lib/api";
import type { User } from "@/lib/types";

const NAV_TRADER = [
  { href: "/trade-panel", label: "Trade Panel" },
  { href: "/trades",      label: "Order History" },
  { href: "/calendar",    label: "Calendar" },
  { href: "/subscribers", label: "Subscribers" },
  { href: "/brokers",     label: "Brokers" },
];
const NAV_SUBSCRIBER = [
  { href: "/trades",   label: "Order History" },
  { href: "/calendar", label: "Calendar" },
  { href: "/brokers",  label: "Brokers" },
  { href: "/settings", label: "Settings" },
];

/** Hex-shaped logo mark — drawn inline so we don't need an asset. */
function LogoMark() {
  return (
    <div
      className="grid place-items-center"
      style={{
        width: 36, height: 36,
        clipPath: "polygon(25% 5%, 75% 5%, 100% 50%, 75% 95%, 25% 95%, 0% 50%)",
        background: "linear-gradient(135deg, var(--accent) 0%, #6fd920 100%)",
      }}
    >
      <span style={{ color: "var(--accent-ink)", fontWeight: 800, fontSize: 16, letterSpacing: "-0.02em" }}>
        Ƈ
      </span>
    </div>
  );
}

function initials(s: string | null | undefined, fallback: string) {
  const t = (s || fallback).trim();
  if (!t) return "·";
  const parts = t.split(/[\s@.]+/).filter(Boolean);
  return (parts[0]?.[0] ?? "?").concat(parts[1]?.[0] ?? "").toUpperCase();
}

export default function AppShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!getAccessToken()) { router.replace("/login"); return; }
    api<User>("/api/auth/me")
      .then(setUser)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) {
          clearTokens(); router.replace("/login");
        }
      })
      .finally(() => setLoading(false));
  }, [router]);

  if (loading) {
    return (
      <div className="min-h-screen grid place-items-center" style={{ color: "var(--muted)" }}>
        Loading…
      </div>
    );
  }
  if (!user) return null;

  const nav = user.role === "trader" ? NAV_TRADER : NAV_SUBSCRIBER;
  const displayName = user.display_name || user.email.split("@")[0];

  return (
    // h-screen + overflow-hidden lock the outer frame to viewport height.
    // The sidebar fills it; only <main> scrolls internally when content overflows.
    <div className="h-screen flex overflow-hidden">
      {/* ── Sidebar ─────────────────────────────────────────────────────── */}
      <aside
        className="flex flex-col h-full shrink-0"
        style={{
          width: 244,
          background: "linear-gradient(180deg, rgba(14,20,17,0.7) 0%, rgba(7,9,10,0.4) 100%)",
          borderRight: "1px solid var(--border)",
          backdropFilter: "blur(8px)",
        }}
      >
        {/* Logo */}
        <div className="flex items-center gap-3 px-5 pt-6 pb-7">
          <LogoMark />
          <div className="leading-tight">
            <div style={{ fontWeight: 700, fontSize: 15, letterSpacing: "0.02em" }}>COPYTRADE</div>
            <div className="text-[10px] uppercase tracking-widest" style={{ color: "var(--muted)" }}>
              {user.role}
            </div>
          </div>
        </div>

        {/* User card */}
        <div className="mx-3 mb-4 card p-3 flex items-center gap-3">
          <div
            className="grid place-items-center rounded-full"
            style={{
              width: 36, height: 36,
              background: "linear-gradient(135deg, #1f2a23 0%, #0e1411 100%)",
              border: "1px solid var(--border)",
              color: "var(--accent)",
              fontWeight: 700, fontSize: 17,
            }}
          >
            {initials(user.display_name, user.email)}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-sm truncate" style={{ fontWeight: 600 }}>{displayName}</div>
            <div className="text-[11px] truncate" style={{ color: "var(--muted)" }}>{user.email}</div>
          </div>
        </div>

        {/* Nav — scrolls within sidebar if it ever overflows */}
        <nav className="flex-1 min-h-0 overflow-y-auto px-3 space-y-1">
          {nav.map((item) => {
            const active = pathname?.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className="block px-4 py-2.5 rounded-full text-sm transition-colors"
                style={{
                  background: active
                    ? "linear-gradient(90deg, rgba(182,255,60,0.14), rgba(182,255,60,0.04))"
                    : "transparent",
                  color: active ? "var(--accent)" : "var(--text-2)",
                  fontWeight: active ? 600 : 500,
                  border: active ? "1px solid rgba(182,255,60,0.25)" : "1px solid transparent",
                  boxShadow: active ? "0 0 24px -6px var(--accent-glow)" : "none",
                }}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        {/* Footer — disclaimer + sign out */}
        <div className="p-3 space-y-3">
          <button
            onClick={() => { clearTokens(); router.replace("/login"); }}
            className="btn-ghost w-full px-3 py-2 text-sm"
          >
            Sign out
          </button>
        </div>
      </aside>

      {/* ── Main ────────────────────────────────────────────────────────── */}
      <main className="flex-1 min-w-0 h-full overflow-y-auto p-8">{children}</main>
    </div>
  );
}
