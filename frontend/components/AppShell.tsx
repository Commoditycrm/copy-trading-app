"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError, clearTokens, getAccessToken } from "@/lib/api";
import type { User } from "@/lib/types";

const NAV_COMMON = [
  { href: "/brokers", label: "Brokers" },
  { href: "/trades", label: "Trades & P&L" },
  { href: "/calendar", label: "Calendar" },
  { href: "/settings", label: "Settings" },
];
const NAV_TRADER_EXTRA = [
  { href: "/trade-panel", label: "Trade Panel" },
  { href: "/subscribers", label: "Subscribers" },
];

export default function AppShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!getAccessToken()) {
      router.replace("/login");
      return;
    }
    api<User>("/api/auth/me")
      .then(setUser)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) {
          clearTokens();
          router.replace("/login");
        }
      })
      .finally(() => setLoading(false));
  }, [router]);

  if (loading) return <div className="p-8" style={{color: "var(--muted)"}}>Loading…</div>;
  if (!user) return null;

  const nav = user.role === "trader" ? [...NAV_COMMON, ...NAV_TRADER_EXTRA] : NAV_COMMON;

  return (
    <div className="min-h-screen flex">
      <aside className="w-60 border-r p-4 space-y-1" style={{borderColor: "var(--border)", background: "var(--panel)"}}>
        <div className="px-2 py-3">
          <div className="font-semibold">Copy Trading</div>
          <div className="text-xs" style={{color: "var(--muted)"}}>{user.email}</div>
          <div className="text-xs uppercase mt-1" style={{color: "var(--accent)"}}>{user.role}</div>
        </div>
        <nav className="space-y-1">
          {nav.map((item) => {
            const active = pathname?.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className="block px-3 py-2 rounded text-sm"
                style={{
                  background: active ? "rgba(78,161,255,0.12)" : "transparent",
                  color: active ? "var(--accent)" : "var(--text)",
                }}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        <button
          onClick={() => { clearTokens(); router.replace("/login"); }}
          className="mt-6 w-full px-3 py-2 text-sm rounded border"
          style={{borderColor: "var(--border)", color: "var(--muted)"}}
        >
          Sign out
        </button>
        <p className="text-[10px] mt-6 leading-snug" style={{color: "var(--muted)"}}>
          Educational software. Not investment advice. Copy trading involves substantial risk of loss.
        </p>
      </aside>
      <main className="flex-1 p-6 overflow-auto">{children}</main>
    </div>
  );
}
