"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { api, ApiError, clearTokens, getAccessToken } from "@/lib/api";
import type { User } from "@/lib/types";

// ── Icons ────────────────────────────────────────────────────────────────────
function IconGrid() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" />
      <rect x="14" y="14" width="7" height="7" /><rect x="3" y="14" width="7" height="7" />
    </svg>
  );
}
function IconUsers() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" />
      <path d="M23 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  );
}
function IconFlask() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M9 3h6m-3 0v7l-4 8h10l-4-8V3" />
    </svg>
  );
}
function IconLogOut() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <polyline points="16 17 21 12 16 7" /><line x1="21" y1="12" x2="9" y2="12" />
    </svg>
  );
}

const NAV = [
  { href: "/admin",            label: "Dashboard",    Icon: IconGrid },
  { href: "/admin/users",      label: "Users",        Icon: IconUsers },
  { href: "/admin/load-test",  label: "Load Test",    Icon: IconFlask },
];

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const router   = useRouter();
  const pathname = usePathname();
  const [user, setUser]       = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!getAccessToken()) { router.replace("/login"); return; }
    api<User>("/api/auth/me")
      .then((u) => {
        if (u.role !== "admin") {
          // Non-admins must not access /admin — redirect to their home
          router.replace("/");
          return;
        }
        setUser(u);
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) {
          clearTokens();
          router.replace("/login");
        }
      })
      .finally(() => setLoading(false));
  }, [router]);

  if (loading) {
    return (
      <div className="min-h-screen grid place-items-center text-sm" style={{ color: "var(--muted)" }}>
        Loading…
      </div>
    );
  }
  if (!user) return null;

  return (
    <div className="h-screen flex overflow-hidden">
      {/* ── Sidebar ──────────────────────────────────────────────────── */}
      <aside
        className="w-56 flex flex-col h-full shrink-0"
        style={{
          background: "linear-gradient(180deg,#0a0f0d 0%,#060809 100%)",
          borderRight: "1px solid var(--border)",
        }}
      >
        {/* Header */}
        <div className="px-5 pt-6 pb-5">
          <div
            className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-bold tracking-widest uppercase"
            style={{ background: "rgba(239,68,68,0.12)", color: "#ef4444", border: "1px solid rgba(239,68,68,0.25)" }}
          >
            ⚙ Admin Panel
          </div>
          <div className="mt-3 text-xs" style={{ color: "var(--muted)" }}>
            {user.email}
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-3 space-y-1">
          {NAV.map(({ href, label, Icon }) => {
            const active = href === "/admin"
              ? pathname === "/admin"
              : pathname?.startsWith(href);
            return (
              <a
                key={href}
                href={href}
                onClick={(e) => {
                  if (e.metaKey || e.ctrlKey || e.button !== 0) return;
                  e.preventDefault();
                  router.push(href);
                }}
                className="flex items-center gap-2.5 px-4 py-2.5 rounded-full text-sm no-underline transition-colors"
                style={{
                  background: active ? "rgba(239,68,68,0.10)" : "transparent",
                  color:      active ? "#ef4444" : "var(--text-2)",
                  fontWeight: active ? 600 : 500,
                  border:     active ? "1px solid rgba(239,68,68,0.25)" : "1px solid transparent",
                }}
              >
                <Icon />
                <span>{label}</span>
              </a>
            );
          })}
        </nav>

        {/* Footer */}
        <div className="p-3">
          <button
            onClick={() => { clearTokens(); router.replace("/login"); }}
            className="flex items-center gap-2 w-full px-3 py-2 rounded-lg text-sm transition-colors"
            style={{ color: "var(--text-2)" }}
          >
            <IconLogOut />
            Sign out
          </button>
        </div>
      </aside>

      {/* ── Main ─────────────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {/* Top bar */}
        <header
          className="flex items-center justify-between px-6 py-3 shrink-0"
          style={{
            borderBottom: "1px solid var(--border)",
            background: "rgba(7,9,10,0.6)",
            backdropFilter: "blur(8px)",
          }}
        >
          <h1 className="text-sm font-semibold" style={{ color: "var(--text-2)" }}>
            {NAV.find(n => n.href === "/admin" ? pathname === "/admin" : pathname?.startsWith(n.href))?.label ?? "Admin"}
          </h1>
          <span
            className="text-xs px-2 py-0.5 rounded-full font-medium"
            style={{ background: "rgba(239,68,68,0.12)", color: "#ef4444", border: "1px solid rgba(239,68,68,0.2)" }}
          >
            Internal — not visible to clients
          </span>
        </header>
        <main className="flex-1 overflow-y-auto p-6">{children}</main>
      </div>
    </div>
  );
}
