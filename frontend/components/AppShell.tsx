"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError, clearTokens, getAccessToken } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import type { User } from "@/lib/types";

interface BulkCopyState { total: number; enabled: number; }

const USER_CACHE_KEY = "trading-app:user";

function loadCachedUser(): User | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(USER_CACHE_KEY);
    return raw ? JSON.parse(raw) as User : null;
  } catch { return null; }
}

const NAV_TRADER = [
  { href: "/trade-panel", label: "Trade Panel" },
  { href: "/trades", label: "Order History" },
  { href: "/calendar", label: "Calendar" },
  { href: "/subscribers", label: "Subscribers" },
  { href: "/brokers", label: "Broker" },
];
const NAV_SUBSCRIBER = [
  { href: "/trades", label: "Order History" },
  { href: "/calendar", label: "Calendar" },
  { href: "/brokers", label: "Broker" },
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
        background: "linear-gradient(135deg, var(--accent) 0%, #006fa3 100%)",
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
  const [user, setUser] = useState<User | null>(() => loadCachedUser());
  const [loading, setLoading] = useState(() => loadCachedUser() === null);
  // Trader-only master switch for copying to subscribers. `null` while
  // unloaded so we can hide the toggle until we know the state.
  const [bulkCopy, setBulkCopy] = useState<BulkCopyState | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);

  useEffect(() => {
    if (!getAccessToken()) { router.replace("/login"); return; }
    api<User>("/api/auth/me")
      .then((u) => {
        setUser(u);
        try { sessionStorage.setItem(USER_CACHE_KEY, JSON.stringify(u)); } catch {}
        if (u.role === "trader") {
          api<BulkCopyState>("/api/subscribers/copy-state").then(setBulkCopy).catch(() => {});
        }
      })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) {
          clearTokens();
          try { sessionStorage.removeItem(USER_CACHE_KEY); } catch {}
          router.replace("/login");
        }
      })
      .finally(() => setLoading(false));
  }, [router]);

  async function toggleBulkCopy() {
    if (!bulkCopy) return;
    // Mixed state and any-on collapse to "off". All-off → "on".
    const next = bulkCopy.enabled === 0;
    setBulkBusy(true);
    try {
      const res = await api<BulkCopyState>("/api/subscribers/copy-state", {
        method: "PATCH", body: JSON.stringify({ enabled: next }),
      });
      setBulkCopy(res);
      notify.success(
        next
          ? `Copy trading ON for ${res.enabled}/${res.total} subscribers`
          : `Copy trading OFF for all subscribers`
      );
    } catch (e) {
      notify.fromError(e, "Could not update copy trading");
    } finally {
      setBulkBusy(false);
    }
  }

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
          </div>
        </div>

        {/* User card */}
        <div className="mx-3 mb-4 card p-3 flex items-center gap-3">
          <div
            className="grid place-items-center rounded-full"
            style={{
              width: 36, height: 36,
              background: "linear-gradient(135deg,rgb(14, 31, 45) 0%,rgb(21, 28, 37) 100%)",
              border: "1px solid var(--border)",
              color: "var(--accent)",
              fontWeight: 700, fontSize: 17,
            }}
          >
            {initials(user.display_name, user.email)}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-sm truncate" style={{ fontWeight: 600 }}>{displayName}</div>
            <div className="text-[10px] uppercase tracking-widest" style={{ color: "var(--muted)" }}>
              {user.role}
            </div>
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
                    ? "linear-gradient(90deg, rgba(10,115,168,0.16), rgba(10,115,168,0.04))"
                    : "transparent",
                  color: active ? "var(--accent)" : "var(--text-2)",
                  fontWeight: active ? 600 : 500,
                  border: active ? "1px solid rgba(10,115,168,0.30)" : "1px solid transparent",
                  boxShadow: active ? "0 0 24px -6px var(--accent-glow)" : "none",
                }}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        {/* Footer — copy-trading master switch (trader only) + Exit All + Sign out */}
        <div className="p-3 space-y-2">
          {user.role === "trader" && bulkCopy && (() => {
            const allOff = bulkCopy.enabled === 0;
            const isOn = !allOff;   // any-on collapses to ON (next toggle turns all off)
            const disabled = bulkBusy || bulkCopy.total === 0;
            return (
              <div
                className="w-full flex items-center justify-between gap-2 rounded-lg border px-3 py-2"
                style={{
                  borderColor: "var(--border)",
                  background: "rgba(255,255,255,0.02)",
                }}
              >
                <div className="text-sm font-medium truncate">Copy trading</div>
                <button
                  type="button"
                  onClick={toggleBulkCopy}
                  disabled={disabled}
                  role="switch"
                  aria-checked={isOn}
                  title={
                    bulkCopy.total === 0
                      ? "No subscribers following you yet"
                      : isOn ? "Turn copy off for all subscribers"
                      : "Turn copy on for all subscribers"
                  }
                  className="relative shrink-0 rounded-full transition-colors"
                  style={{
                    width: 32, height: 18,
                    background: isOn ? "var(--good)" : "var(--border)",
                    opacity: disabled ? 0.5 : 1,
                    cursor: disabled ? "not-allowed" : "pointer",
                  }}
                >
                  <span
                    className="absolute top-0.5 inline-flex items-center justify-center rounded-full transition-all"
                    style={{
                      width: 14, height: 14,
                      left: isOn ? 16 : 2,
                      background: "#fff",
                      boxShadow: "0 1px 3px rgba(0,0,0,0.3)",
                    }}
                  >
                    {bulkBusy && (
                      <span style={{ color: "var(--text)", fontSize: 9, lineHeight: 1 }}>
                        <Spinner />
                      </span>
                    )}
                  </span>
                </button>
              </div>
            );
          })()}
          <button
            onClick={() => {
              clearTokens();
              try { sessionStorage.removeItem(USER_CACHE_KEY); } catch {}
              router.replace("/login");
            }}
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
