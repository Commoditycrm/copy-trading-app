"use client";

import { usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, clearTokens, getAccessToken, resendVerification } from "@/lib/api";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";
import type { SubscriberSettings, User } from "@/lib/types";
import { ListenerPill } from "@/components/ListenerPill";
import { ThemeToggle } from "@/components/theme/ThemeToggle";
import { ChevronsLeft, ChevronsRight } from "lucide-react";

function IconGrid() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
    </svg>
  );
}

function IconBell() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
      <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
    </svg>
  );
}

function IconMenu() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <line x1="3" y1="6" x2="21" y2="6" />
      <line x1="3" y1="12" x2="21" y2="12" />
      <line x1="3" y1="18" x2="21" y2="18" />
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
  { href: "/dashboard", label: "Dashboard", Icon: IconGrid },
  { href: "/trade-panel", label: "Trade Panel", Icon: IconBolt },
  { href: "/positions", label: "Positions", Icon: IconLayers },
  { href: "/trades", label: "Order History", Icon: IconList },
  { href: "/calendar", label: "P&L", Icon: IconCalendar },
  { href: "/subscribers", label: "Subscribers", Icon: IconUsers },
  { href: "/performance", label: "Performance", Icon: IconActivity },
  { href: "/brokers", label: "Broker", Icon: IconLink },
];
const NAV_SUBSCRIBER = [
  { href: "/dashboard", label: "Dashboard", Icon: IconGrid },
  { href: "/positions", label: "Positions", Icon: IconLayers },
  { href: "/trades", label: "Order History", Icon: IconList },
  { href: "/calendar", label: "P&L", Icon: IconCalendar },
  { href: "/brokers", label: "Broker", Icon: IconLink },
  { href: "/settings", label: "Settings", Icon: IconSettings },
];

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

/** Copy-trading on/off toggle used in the sidebar footer (trader master +
 *  subscriber personal share the same visual). Logic is unchanged — this is
 *  just the presentational switch. */
function CopySwitch({
  on,
  busy,
  collapsed,
  onToggle,
  title,
}: {
  on: boolean;
  busy: boolean;
  collapsed: boolean;
  onToggle: () => void;
  title: string;
}) {
  return (
    <div
      className={`w-full flex items-center gap-2 rounded-token border ${collapsed ? "justify-center px-2" : "justify-between px-3"} py-2`}
      style={{ borderColor: "var(--border)", background: "rgba(255,255,255,0.02)" }}
    >
      {!collapsed && (
        <div className="flex items-center gap-2 min-w-0">
          <span
            className={on ? "pulse-dot" : ""}
            style={{ width: 7, height: 7, borderRadius: 9999, background: on ? "var(--good)" : "var(--muted)", color: "var(--good)", display: "inline-block", flexShrink: 0 }}
            aria-hidden
          />
          <span className="text-sm font-medium truncate">Copy trading</span>
        </div>
      )}
      <button
        type="button"
        onClick={onToggle}
        disabled={busy}
        role="switch"
        aria-checked={on}
        aria-label={title}
        title={title}
        className="relative shrink-0 rounded-full transition-colors focus-ring"
        style={{
          width: 32, height: 18,
          background: on ? "var(--good)" : "var(--border)",
          opacity: busy ? 0.5 : 1,
          cursor: busy ? "not-allowed" : "pointer",
        }}
      >
        <span
          className="absolute top-0.5 inline-flex items-center justify-center rounded-full transition-all"
          style={{ width: 14, height: 14, left: on ? 16 : 2, background: "#fff", boxShadow: "0 1px 3px rgba(0,0,0,0.3)" }}
        >
          {busy && (
            <span style={{ color: "var(--text)", fontSize: 9, lineHeight: 1 }}>
              <Spinner />
            </span>
          )}
        </span>
      </button>
    </div>
  );
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
  // Email-verification banner state (soft enforcement — nag, don't block).
  const [resendBusy, setResendBusy] = useState(false);
  const [resendDone, setResendDone] = useState(false);

  async function resendVerify() {
    if (!user) return;
    setResendBusy(true);
    try {
      await resendVerification(user.email);
      setResendDone(true);
      notify.success("Verification email sent — check your inbox.");
    } catch (e) {
      notify.fromError(e, "Could not resend verification email");
    } finally {
      setResendBusy(false);
    }
  }

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

  // Mobile drawer: below lg the sidebar slides in over an overlay. Default
  // false (matches SSR). Track desktop so the collapse feature only applies
  // on large screens; the mobile drawer always shows the full sidebar.
  const [mobileOpen, setMobileOpen] = useState(false);
  const [isDesktop, setIsDesktop] = useState(true);
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1024px)");
    const update = () => setIsDesktop(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  // Close the drawer on navigation.
  useEffect(() => { setMobileOpen(false); }, [pathname]);

  if (loading) {
    return (
      <div className="min-h-screen grid place-items-center" style={{ color: "var(--muted)" }}>
        <Spinner />
      </div>
    );
  }
  if (!user) return null;

  const nav = user.role === "trader" ? NAV_TRADER : NAV_SUBSCRIBER;
  const displayName = user.display_name || user.email.split("@")[0];
  // App wordmark: trader sees their own business_name; subscriber sees
  // the business_name of the trader they follow (loaded via /api/settings/subscriber
  // into subCopy). Falls back to "ARK" when the value isn't available —
  // e.g. legacy traders that pre-date business_name, or a subscriber who
  // hasn't picked a trader yet.
  const brandName =
    (user.role === "trader"
      ? user.business_name
      : subCopy?.following_trader_business_name) || "ARK";
  // The collapse feature is desktop-only; the mobile drawer always shows the
  // expanded sidebar (labels visible).
  const navCollapsed = isDesktop && collapsed;
  const brandShort = brandName.trim().charAt(0).toUpperCase() || "A";
  const SIDEBAR_W = navCollapsed ? 72 : 244;

  return (
    // Row: full-height sidebar on the left, then a column (navbar + main)
    // on the right. h-screen + overflow-hidden locks the outer frame; only
    // <main> scrolls internally.
    <div className="h-screen flex overflow-hidden relative">
      {/* Mobile overlay — tap to close the drawer. Desktop never shows it. */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 lg:hidden animate-fade-in"
          style={{ background: "var(--overlay)", backdropFilter: "blur(2px)" }}
          onClick={() => setMobileOpen(false)}
          aria-hidden
        />
      )}

      {/* Desktop collapse hinge — floats on the sidebar's right edge near the
          top (half over the bar). Hidden on mobile (drawer + hamburger). */}
      <button
        type="button"
        onClick={() => setCollapsed(c => !c)}
        title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        className="absolute hidden lg:grid place-items-center transition-[left,transform] duration-200 focus-ring hover:scale-110"
        style={{
          top: 24,
          left: SIDEBAR_W - 14,
          width: 28,
          height: 28,
          zIndex: 60,
          borderRadius: 9999,
          border: "1px solid var(--border-strong)",
          background: "var(--accent)",
          color: "var(--accent-ink)",
          boxShadow: "var(--shadow-card)",
        }}
      >
        {collapsed
          ? <ChevronsRight size={16} strokeWidth={2.5} />
          : <ChevronsLeft size={16} strokeWidth={2.5} />}
      </button>

      {/* ── Sidebar — static column on desktop, slide-in drawer on mobile ── */}
      <aside
        className={`flex flex-col h-full shrink-0 z-50 fixed inset-y-0 left-0 lg:static transition-[transform,width] duration-200 ${mobileOpen ? "translate-x-0" : "-translate-x-full"} lg:translate-x-0`}
        style={{
          width: SIDEBAR_W,
          background: "var(--sidebar-bg)",
          borderRight: "1px solid var(--border)",
          backdropFilter: "blur(8px)",
        }}
      >
        {/* Wordmark — derived per role (trader's own business_name, or the
            followed trader's for subscribers). First-letter fallback when
            collapsed so the chrome still reads as branded at narrow widths. */}
        <div className={`flex items-center ${navCollapsed ? "px-4 justify-center" : "px-5"} pt-6 pb-7`}>
          <div className="flex items-center gap-2.5 min-w-0">
            <div
              className="grid place-items-center shrink-0 rounded-token"
              style={{
                width: 30, height: 30,
                background: "var(--grad-accent)",
                color: "var(--accent-ink)",
                fontWeight: 800, fontSize: 15,
                boxShadow: "0 6px 16px -8px var(--accent-glow)",
              }}
              aria-hidden
            >
              {brandShort}
            </div>
            {!navCollapsed && (
              <div
                title={brandName}
                style={{
                  fontWeight: 700, fontSize: 19, letterSpacing: "0.03em",
                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}
              >
                {brandName}
              </div>
            )}
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
                prefetch
                onClick={() => setMobileOpen(false)}
                title={navCollapsed ? item.label : undefined}
                aria-current={active ? "page" : undefined}
                className={`flex items-center gap-2.5 ${navCollapsed ? "justify-center px-2" : "px-4"} py-2.5 rounded-full text-sm transition-colors no-underline focus-ring`}
                style={{
                  background: active ? "var(--nav-active-bg)" : "transparent",
                  color: active ? "var(--accent)" : "var(--text-2)",
                  fontWeight: active ? 600 : 500,
                  border: active ? "1px solid rgba(10,115,168,0.30)" : "1px solid transparent",
                  boxShadow: active ? "0 0 24px -6px var(--accent-glow)" : "none",
                }}
              >
                <item.Icon />
                {!navCollapsed && <span>{item.label}</span>}
              </Link>
            );
          })}
        </nav>

        {/* Footer — copy-trading switch (trader: master; subscriber: own) + Sign out */}
        <div className="p-3 space-y-2">
          {user.role === "subscriber" && subCopy && (
            <CopySwitch
              on={subCopy.copy_enabled}
              busy={subCopyBusy}
              collapsed={navCollapsed}
              onToggle={toggleSubscriberCopy}
              title={subCopy.copy_enabled ? "Turn copy off" : "Turn copy on"}
            />
          )}
          {user.role === "trader" && bulkCopy && (
            <CopySwitch
              on={!bulkCopy.paused}
              busy={bulkBusy}
              collapsed={navCollapsed}
              onToggle={toggleBulkCopy}
              title={!bulkCopy.paused ? "Pause copy trading" : "Resume copy trading"}
            />
          )}
          <button
            onClick={() => {
              clearTokens();
              try { sessionStorage.removeItem(USER_CACHE_KEY); } catch {}
              router.replace("/login");
            }}
            title={navCollapsed ? "Sign out" : undefined}
            className={`btn-ghost w-full ${navCollapsed ? "px-2 justify-center" : "px-3"} py-2 text-sm flex items-center gap-2 focus-ring`}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
              <polyline points="16 17 21 12 16 7" />
              <line x1="21" y1="12" x2="9" y2="12" />
            </svg>
            {!navCollapsed && <span>Sign out</span>}
          </button>
        </div>
      </aside>

      {/* ── Right column: navbar on top, scrollable main below ──────────── */}
      <div className="flex flex-col flex-1 min-w-0 h-full overflow-hidden">
        <header
          className="flex items-center justify-between gap-3 px-4 sm:px-5 py-3 shrink-0 mx-3 sm:mx-4 mt-3 rounded-xl"
          style={{
            background: "var(--header-bg)",
            border: "1px solid var(--border)",
            backdropFilter: "blur(10px)",
            boxShadow: "var(--shadow-card)",
          }}
        >
          {/* Left: hamburger (mobile only) + listener/SSE status pills. */}
          <div className="flex items-center gap-2 min-w-0">
            <button
              type="button"
              onClick={() => setMobileOpen(true)}
              aria-label="Open navigation menu"
              className="lg:hidden grid place-items-center rounded-token shrink-0 focus-ring"
              style={{ width: 36, height: 36, border: "1px solid var(--border)", background: "rgba(255,255,255,0.02)", color: "var(--text-2)" }}
            >
              <IconMenu />
            </button>
            <ListenerPill role={user.role as "trader" | "subscriber"} />
            <div className="hidden sm:block">
              <SseStatusPill state={sseStatus.state} lastEventAt={sseStatus.lastEventAt} />
            </div>
          </div>
          {/* Right: theme toggle + bell + who's signed in + role chip. */}
          <div className="flex items-center gap-2 sm:gap-3">
            <ThemeToggle />
            <button
              type="button"
              onClick={() => router.push("/notifications")}
              title={unreadCount > 0 ? `${unreadCount} unread notification(s)` : "Notifications"}
              aria-label={unreadCount > 0 ? `Notifications, ${unreadCount} unread` : "Notifications"}
              className="relative grid place-items-center rounded-full transition-colors focus-ring"
              style={{
                width: 32, height: 32,
                background: "var(--chip-bg)",
                border: "1px solid var(--border)",
                color: unreadCount > 0 ? "var(--accent)" : "var(--text-2)",
              }}
            >
              <IconBell />
              {unreadCount > 0 && (
                <span
                  className="absolute text-[10px] font-bold rounded-full grid place-items-center"
                  style={{
                    top: -4, right: -4, minWidth: 16, height: 16, padding: "0 4px",
                    background: "var(--bad)", color: "#fff",
                    border: "1px solid var(--panel)", lineHeight: 1,
                  }}
                >
                  {unreadCount > 99 ? "99+" : unreadCount}
                </span>
              )}
            </button>
            <div
              className="grid place-items-center rounded-full shrink-0"
              style={{
                width: 32, height: 32,
                background: "var(--chip-bg)",
                border: "1px solid var(--border)",
                color: "var(--accent)", fontWeight: 700, fontSize: 14,
              }}
            >
              {initials(user.display_name, user.email)}
            </div>
            <div className="leading-tight text-right hidden sm:block">
              <div className="text-sm" style={{ fontWeight: 600 }}>{displayName}</div>
              <div className="text-[10px] uppercase tracking-widest" style={{ color: "var(--muted)" }}>
                {user.role}
              </div>
            </div>
          </div>
        </header>

        {!user.email_verified && (
          <div
            className="flex items-center justify-between gap-3 mx-3 sm:mx-4 mt-3 px-4 py-2.5 rounded-xl text-sm animate-fade-in"
            style={{
              background: "rgba(250,204,21,0.10)",
              border: "1px solid rgba(250,204,21,0.35)",
              color: "#facc15",
            }}
          >
            <span className="min-w-0">
              📧 Please verify your email <strong>{user.email}</strong> to secure your account.
            </span>
            <button
              type="button"
              onClick={resendVerify}
              disabled={resendBusy || resendDone}
              className="btn-ghost px-3 py-1 text-xs whitespace-nowrap shrink-0 focus-ring"
            >
              {resendDone ? "Sent ✓" : resendBusy ? "Sending…" : "Resend email"}
            </button>
          </div>
        )}

        <main className="flex-1 min-w-0 overflow-y-auto p-4 sm:p-5">{children}</main>
      </div>
    </div>
  );
}
