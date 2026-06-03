"use client";

import { usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, clearTokens, getAccessToken } from "@/lib/api";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";
import type { SubscriberSettings, User } from "@/lib/types";
import { ListenerPill } from "@/components/ListenerPill";

function IconBell() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
      <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
    </svg>
  );
}

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
function IconActivity() {
  // Activity / waveform — reads as "performance / latency".
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
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
  { href: "/performance", label: "Performance", Icon: IconActivity },
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

/** Small pill mirroring ListenerPill's visual language, but for the SSE
 *  connection itself rather than the broker stream. Hidden while
 *  everything's healthy so the header stays quiet during normal
 *  operation; surfaces during reconnect / unauthorized / cold connect. */
function SseStatusPill({ state, lastEventAt }: { state: import("@/lib/sse").SseState; lastEventAt: string | null }) {
  // Hide entirely once we're connected AND have recent traffic. Without
  // the lastEventAt check, the pill flashes "Connected" briefly on every
  // mount and adds visual noise.
  if (state === "connected") {
    const fresh = lastEventAt && (Date.now() - new Date(lastEventAt).getTime()) < 60_000;
    if (fresh) return null;
    // Connected but quiet — still don't show, the listener pill covers
    // "is anything live?" already. Keep this off unless something is wrong.
    return null;
  }
  if (state === "disconnected") return null;

  const { color, label, title } = (() => {
    switch (state) {
      case "connecting":
        return { color: "#94a3b8", label: "Connecting…", title: "Opening event stream" };
      case "reconnecting":
        return { color: "#facc15", label: "Reconnecting…", title: "Lost event stream — retrying" };
      case "unauthorized":
        return { color: "#ef4444", label: "Disconnected — please re-login", title: "Session expired" };
      default:
        return { color: "#94a3b8", label: state, title: state };
    }
  })();

  return (
    <div
      title={title}
      className="inline-flex items-center gap-1.5 px-2 py-1 rounded-full text-[10px] font-medium"
      style={{
        border: `1px solid ${color}55`,
        background: `${color}15`,
        color,
      }}
    >
      <span
        className="inline-block rounded-full"
        style={{
          width: 6, height: 6, background: color,
          boxShadow: state === "reconnecting" ? `0 0 6px ${color}` : "none",
        }}
      />
      <span className="whitespace-nowrap">{label}</span>
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
  // Trader-only master switch for copying to subscribers. `null` while
  // unloaded so we can hide the toggle until we know the state.
  const [bulkCopy, setBulkCopy] = useState<BulkCopyState | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  // Subscriber-only personal copy switch (same UX, different endpoint).
  const [subCopy, setSubCopy] = useState<SubscriberSettings | null>(null);
  const [subCopyBusy, setSubCopyBusy] = useState(false);
  // Bell badge. Hydrated from /unread-count, bumped by SSE on
  // notification.created, and refreshed on a 30s poll as a backstop
  // for SSE drops we didn't fully recover from.
  const [unreadCount, setUnreadCount] = useState<number>(0);

  async function refreshUnreadCount() {
    try {
      const r = await api<{ unread: number }>("/api/notifications/unread-count");
      setUnreadCount(r.unread);
    } catch { /* tolerate — bell just doesn't show a badge */ }
  }

  // Drive the bell badge via SSE. Also doubles as the AppShell's
  // canonical SseStatus source — the connection-status pill in the
  // header reads from this same return value so we don't open two
  // EventSources from the same component tree.
  const sseStatus = useEventStream((evt) => {
    if (evt.type === "notification.created") {
      setUnreadCount(c => c + 1);
      notify.warn(evt.notification.message, { autoClose: 8000 });
      return;
    }
    // The footer toggle binds to subCopy.copy_enabled, which is loaded
    // ONCE on mount and otherwise only mutated by the toggle handler.
    // When pnl_poller auto-pauses (loss/profit/pct limit hit) the
    // toggle stays visually ON until a manual reload — fix by listening
    // to the same events the Settings page does and keeping subCopy in
    // sync. eslint-disable: SSE union type doesn't carry these fields.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const e = evt as any;
    if (e?.type === "copy.auto_paused") {
      setSubCopy(prev => prev ? { ...prev, copy_enabled: false } : prev);
      return;
    }
    if (e?.type === "copy.auto_resumed") {
      setSubCopy(prev => prev ? { ...prev, copy_enabled: true } : prev);
      return;
    }
    if (e?.type === "pnl.tick") {
      if (typeof e.copy_enabled === "boolean") {
        setSubCopy(prev => prev ? { ...prev, copy_enabled: e.copy_enabled } : prev);
      }
      // Keep the Settings page's Risk Controls panel fresh even while
      // the user is on another page — write the tick fields it cares
      // about to sessionStorage so its mount-time hydration picks up
      // the latest values instead of showing "—" until the next tick.
      try {
        const raw = window.sessionStorage.getItem("trading-app:pnl-tick-cache");
        const current = raw ? JSON.parse(raw) : {};
        const next = {
          ...current,
          ...(typeof e.beginning_day_balance === "string" && { beginning_day_balance: e.beginning_day_balance }),
          ...(typeof e.todays_trading_value  === "string" && { todays_trading_value:  e.todays_trading_value }),
        };
        window.sessionStorage.setItem("trading-app:pnl-tick-cache", JSON.stringify(next));
      } catch { /* sessionStorage disabled / quota — silent */ }
    }
  });

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
        // Admins have their own panel — redirect immediately so they never
        // land on trader/subscriber routes (which would 403 on their API calls).
        if (u.role === "admin") {
          router.replace("/admin");
          return;
        }
        if (u.role === "trader") {
          api<BulkCopyState>("/api/subscribers/copy-state").then(setBulkCopy).catch(() => {});
        } else {
          api<SubscriberSettings>("/api/settings/subscriber").then(setSubCopy).catch(() => {});
        }
        // Hydrate bell badge for both roles. Today only subscribers
        // receive notifications (copy.retry_failed) but the table is
        // generic so future trader-side types work without UI changes.
        refreshUnreadCount();
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

  // Backstop poll for the bell badge. SSE keeps it fresh in real time,
  // but a missed event (during reconnect, brief 5xx, etc.) shouldn't
  // leave the count stale forever. Every 30s is cheap and matches what
  // users would manually do anyway.
  useEffect(() => {
    if (!user) return;
    const id = setInterval(refreshUnreadCount, 30_000);
    return () => clearInterval(id);
  }, [user]);

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

  // Sidebar collapse state, persisted across reloads so the user's pref
  // sticks. Initial render uses `false` to match SSR; the stored value is
  // applied in a layout-effect-ish useEffect (avoids hydration mismatch).
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => {
    try {
      const stored = localStorage.getItem("trading-app:sidebar-collapsed");
      if (stored === "1") setCollapsed(true);
    } catch { /* ignore */ }
  }, []);
  useEffect(() => {
    try {
      localStorage.setItem("trading-app:sidebar-collapsed", collapsed ? "1" : "0");
    } catch { /* ignore */ }
  }, [collapsed]);

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
  const SIDEBAR_W = collapsed ? 72 : 244;

  return (
    // Row: full-height sidebar on the left, then a column (navbar + main)
    // on the right. h-screen + overflow-hidden locks the outer frame; only
    // <main> scrolls internally.
    <div className="h-screen flex overflow-hidden relative">
      {/* Edge toggle — anchored to the seam between sidebar and main, rendered
          last in the outer container so it stacks above both. Vertically
          centered with the navbar strip. */}
      <button
        type="button"
        onClick={() => setCollapsed(c => !c)}
        title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        className="absolute grid place-items-center transition-[left] duration-200"
        style={{
          // Floating navbar starts at mt-3 (12px) with py-3 padding. A 28px
          // square button centered on the navbar's vertical midline:
          // 12 (offset) + (56-28)/2 = 26.
          top: 26,
          // Centered on the sidebar's right border — half the button sits
          // inside the sidebar, half outside, so it reads as a hinge.
          // Width trimmed another ~10% (24 → 22); height stays 28 so the
          // chevron icon doesn't crowd the button.
          left: SIDEBAR_W - 11,
          width: 22,
          height: 28,
          zIndex: 50,
          borderRadius: 6,
          border: "1px solid var(--border)",
          background: "var(--accent)",
          color: "var(--accent-ink)",
          boxShadow: "0 4px 12px -4px rgba(0,0,0,0.4)",
        }}
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
          style={{ transform: collapsed ? "rotate(180deg)" : "none", transition: "transform 200ms" }}
        >
          <polyline points="15 18 9 12 15 6" />
        </svg>
      </button>

      {/* ── Sidebar (full viewport height) ──────────────────────────────── */}
      <aside
        className="flex flex-col h-full shrink-0 transition-[width] duration-200 relative"
        style={{
          width: SIDEBAR_W,
          background: "linear-gradient(180deg, rgba(14,20,17,0.7) 0%, rgba(7,9,10,0.4) 100%)",
          borderRight: "1px solid var(--border)",
          backdropFilter: "blur(8px)",
        }}
      >
        {/* Logo (always visible — wordmark hidden when collapsed) */}
        <div className={`flex items-center gap-3 ${collapsed ? "px-4 justify-center" : "px-5"} pt-6 pb-7`}>
          <LogoMark />
          {!collapsed && (
            <div className="leading-tight">
              <div style={{ fontWeight: 700, fontSize: 15, letterSpacing: "0.02em" }}>The Option Haven</div>
            </div>
          )}
        </div>

        {/* Nav — scrolls within sidebar if it ever overflows */}
        <nav className="flex-1 min-h-0 overflow-y-auto px-3 space-y-1">
          {nav.map((item) => {
            const active = pathname?.startsWith(item.href);
            // Use Next.js <Link> with prefetch=true (the default) so the
            // RSC payload for each route is fetched on hover / when the
            // link scrolls into view. Without this, every navigation
            // pays the full RSC roundtrip (3-4s on slow links) before
            // the new page can render. We pair this with the route-
            // level `loading.tsx` so even a cold prefetch shows the
            // centered spinner instantly on click.
            //
            // (Previously this used router.push + an <a> to dodge an
            //  RSC auth-wall failure; the route-level loading file
            //  covers that case now — the user sees the spinner
            //  immediately rather than a stale page.)
            return (
              <Link
                key={item.href}
                href={item.href}
                prefetch
                title={collapsed ? item.label : undefined}
                className={`flex items-center gap-2.5 ${collapsed ? "justify-center px-2" : "px-4"} py-2.5 rounded-full text-sm transition-colors no-underline`}
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
                {!collapsed && <span>{item.label}</span>}
              </Link>
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
                className={`w-full flex items-center gap-2 rounded-lg border ${collapsed ? "justify-center px-2" : "justify-between px-3"} py-2`}
                style={{
                  borderColor: "var(--border)",
                  background: "rgba(255,255,255,0.02)",
                }}
              >
                {!collapsed && <div className="text-sm font-medium truncate">Copy trading</div>}
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
                className={`w-full flex items-center gap-2 rounded-lg border ${collapsed ? "justify-center px-2" : "justify-between px-3"} py-2`}
                style={{
                  borderColor: "var(--border)",
                  background: "rgba(255,255,255,0.02)",
                }}
              >
                {!collapsed && <div className="text-sm font-medium truncate">Copy trading</div>}
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
            title={collapsed ? "Sign out" : undefined}
            className={`btn-ghost w-full ${collapsed ? "px-2 justify-center" : "px-3"} py-2 text-sm flex items-center gap-2`}
          >
            {/* Door-arrow icon — always visible; label hidden in collapsed mode. */}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
              <polyline points="16 17 21 12 16 7" />
              <line x1="21" y1="12" x2="9" y2="12" />
            </svg>
            {!collapsed && <span>Sign out</span>}
          </button>
        </div>
      </aside>

      {/* ── Right column: navbar on top, scrollable main below ──────────── */}
      <div className="flex flex-col flex-1 min-w-0 h-full overflow-hidden">
        <header
          className="flex items-center justify-between px-5 py-3 shrink-0 mx-4 mt-3 rounded-xl"
          style={{
            background: "linear-gradient(180deg, rgba(14,20,17,0.75) 0%, rgba(7,9,10,0.5) 100%)",
            border: "1px solid var(--border)",
            backdropFilter: "blur(10px)",
            boxShadow: "0 10px 30px -10px rgba(0,0,0,0.55), 0 2px 6px -2px rgba(0,0,0,0.4)",
          }}
        >
          {/* Left: listener health pill + SSE connection-status pill.
              Margin-left clears the sidebar collapse tab so the pill
              doesn't sit underneath it. */}
          <div className="flex items-center gap-2">
            <ListenerPill role={user.role as "trader" | "subscriber"} />
            <SseStatusPill state={sseStatus.state} lastEventAt={sseStatus.lastEventAt} />
          </div>
          {/* Right: bell + who's signed in + role chip. */}
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => router.push("/notifications")}
              title={unreadCount > 0 ? `${unreadCount} unread notification(s)` : "Notifications"}
              aria-label="Notifications"
              className="relative grid place-items-center rounded-full transition-colors"
              style={{
                width: 32, height: 32,
                background: "linear-gradient(135deg,rgb(14, 31, 45) 0%,rgb(21, 28, 37) 100%)",
                border: "1px solid var(--border)",
                color: unreadCount > 0 ? "var(--accent)" : "var(--text-2)",
              }}
            >
              <IconBell />
              {unreadCount > 0 && (
                <span
                  className="absolute text-[10px] font-bold rounded-full grid place-items-center"
                  style={{
                    top: -4,
                    right: -4,
                    minWidth: 16,
                    height: 16,
                    padding: "0 4px",
                    background: "var(--bad)",
                    color: "#fff",
                    border: "1px solid var(--panel)",
                    lineHeight: 1,
                  }}
                >
                  {unreadCount > 99 ? "99+" : unreadCount}
                </span>
              )}
            </button>
            <div
              className="grid place-items-center rounded-full"
              style={{
                width: 32, height: 32,
                background: "linear-gradient(135deg,rgb(14, 31, 45) 0%,rgb(21, 28, 37) 100%)",
                border: "1px solid var(--border)",
                color: "var(--accent)",
                fontWeight: 700, fontSize: 14,
              }}
            >
              {initials(user.display_name, user.email)}
            </div>
            <div className="leading-tight text-right">
              <div className="text-sm" style={{ fontWeight: 600 }}>{displayName}</div>
              <div className="text-[10px] uppercase tracking-widest" style={{ color: "var(--muted)" }}>
                {user.role}
              </div>
            </div>
          </div>
        </header>

        <main className="flex-1 min-w-0 overflow-y-auto p-5">{children}</main>
      </div>
    </div>
  );
}
