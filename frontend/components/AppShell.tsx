"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError, clearTokens, getAccessToken } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import type { SubscriberSettings, User } from "@/lib/types";

interface BulkCopyState { total: number; enabled: number; paused: boolean; }

const USER_CACHE_KEY = "trading-app:user";

function loadCachedUser(): User | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(USER_CACHE_KEY);
    return raw ? JSON.parse(raw) as User : null;
  } catch { return null; }
}

// Inline SVG icons — all share the same stroke style so the sidebar reads
// consistently. 16×16 viewBox, stroke=currentColor so they inherit nav color.
function IconBolt() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    </svg>
  );
}
function IconLayers() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <polygon points="12 2 2 7 12 12 22 7 12 2" />
      <polyline points="2 17 12 22 22 17" />
      <polyline points="2 12 12 17 22 12" />
    </svg>
  );
}
function IconList() {
  // Clipboard with lines — reads as "orders / records list".
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
      <rect x="8" y="2" width="8" height="4" rx="1" ry="1" />
      <line x1="9" y1="12" x2="15" y2="12" />
      <line x1="9" y1="16" x2="15" y2="16" />
    </svg>
  );
}
function IconCalendar() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
      <line x1="16" y1="2" x2="16" y2="6" />
      <line x1="8" y1="2" x2="8" y2="6" />
      <line x1="3" y1="10" x2="21" y2="10" />
    </svg>
  );
}
function IconUsers() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  );
}
function IconLink() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </svg>
  );
}
function IconSettings() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

const NAV_TRADER = [
  { href: "/trade-panel", label: "Trade Panel", Icon: IconBolt },
  { href: "/positions", label: "Positions", Icon: IconLayers },
  { href: "/trades", label: "Order History", Icon: IconList },
  { href: "/calendar", label: "P&L", Icon: IconCalendar },
  { href: "/subscribers", label: "Subscribers", Icon: IconUsers },
  { href: "/brokers", label: "Broker", Icon: IconLink },
];
const NAV_SUBSCRIBER = [
  { href: "/positions", label: "Positions", Icon: IconLayers },
  { href: "/trades", label: "Order History", Icon: IconList },
  { href: "/calendar", label: "P&L", Icon: IconCalendar },
  { href: "/brokers", label: "Broker", Icon: IconLink },
  { href: "/settings", label: "Settings", Icon: IconSettings },
];

/** Brand mark — uses the uploaded icon from /public. */
function LogoMark({ size = 40 }: { size?: number }) {
  return (
    <img
      src="/brand-icon.avif"
      alt="The Option Haven"
      width={size}
      height={size}
      style={{ width: size, height: size, borderRadius: 8, objectFit: "cover" }}
    />
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
  // Trader-only master switch for copying to subscribers. `null` while
  // unloaded so we can hide the toggle until we know the state.
  const [bulkCopy, setBulkCopy] = useState<BulkCopyState | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  // Subscriber-only personal copy switch (same UX, different endpoint).
  const [subCopy, setSubCopy] = useState<SubscriberSettings | null>(null);
  const [subCopyBusy, setSubCopyBusy] = useState(false);

  useEffect(() => {
    if (!getAccessToken()) { router.replace("/login"); return; }
    // Hydrate from cache first so a remount (or hard refresh) renders the
    // shell instantly instead of flashing "Loading…". Then revalidate.
    const cached = loadCachedUser();
    if (cached) {
      setUser(cached);
      setLoading(false);
    }
    api<User>("/api/auth/me")
      .then((u) => {
        setUser(u);
        try { sessionStorage.setItem(USER_CACHE_KEY, JSON.stringify(u)); } catch {}
        if (u.role === "trader") {
          api<BulkCopyState>("/api/subscribers/copy-state").then(setBulkCopy).catch(() => {});
        } else {
          api<SubscriberSettings>("/api/settings/subscriber").then(setSubCopy).catch(() => {});
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

  async function toggleSubscriberCopy() {
    if (!subCopy) return;
    const next = !subCopy.copy_enabled;
    setSubCopyBusy(true);
    try {
      const updated = await api<SubscriberSettings>("/api/settings/subscriber/copy", {
        method: "PATCH", body: JSON.stringify({ copy_enabled: next }),
      });
      setSubCopy(updated);
      notify.success(next ? "Copy trading ON" : "Copy trading OFF");
    } catch (e) {
      notify.fromError(e, "Could not update copy trading");
    } finally {
      setSubCopyBusy(false);
    }
  }

  async function toggleBulkCopy() {
    if (!bulkCopy) return;
    // Toggle master pause. `enabled` in the payload means "fanout enabled" —
    // resume when currently paused, pause when currently running.
    const next = bulkCopy.paused;
    setBulkBusy(true);
    try {
      const res = await api<BulkCopyState>("/api/subscribers/copy-state", {
        method: "PATCH", body: JSON.stringify({ enabled: next }),
      });
      setBulkCopy(res);
      notify.success(
        next
          ? "Copy trading resumed for subscribers"
          : "Copy trading paused — subscribers will not receive new trades"
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
            <div style={{ fontWeight: 700, fontSize: 15, letterSpacing: "0.02em" }}>The Option Haven</div>
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
            // Use programmatic router.push instead of <Link>. <Link>'s built-in
            // navigation can fall back to a hard reload on Vercel when the
            // RSC payload fetch returns an unexpected shape (auth wall, CDN
            // weirdness). router.push goes strictly through the client router
            // — no prefetch, no MPA fallback.
            return (
              <a
                key={item.href}
                href={item.href}
                onClick={(e) => {
                  // Let modified clicks (cmd/ctrl/middle) open in a new tab
                  // as usual; intercept only the plain click.
                  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
                  e.preventDefault();
                  if (item.href !== pathname) router.push(item.href);
                }}
                className="flex items-center gap-2.5 px-4 py-2.5 rounded-full text-sm transition-colors no-underline"
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
                <item.Icon />
                <span>{item.label}</span>
              </a>
            );
          })}
        </nav>

        {/* Footer — copy-trading switch (trader: master; subscriber: own) + Sign out */}
        <div className="p-3 space-y-2">
          {user.role === "subscriber" && subCopy && (() => {
            const isOn = subCopy.copy_enabled;
            const disabled = subCopyBusy;
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
                  onClick={toggleSubscriberCopy}
                  disabled={disabled}
                  role="switch"
                  aria-checked={isOn}
                  title={isOn ? "Turn copy off" : "Turn copy on"}
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
                    {subCopyBusy && (
                      <span style={{ color: "var(--text)", fontSize: 9, lineHeight: 1 }}>
                        <Spinner />
                      </span>
                    )}
                  </span>
                </button>
              </div>
            );
          })()}
          {user.role === "trader" && bulkCopy && (() => {
            // The toggle reflects the trader-side master fanout gate, not
            // subscribers' individual flags. ON = fanout active, OFF = paused.
            const isOn = !bulkCopy.paused;
            const disabled = bulkBusy;
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
                  title={isOn ? "Pause copy trading" : "Resume copy trading"}
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
