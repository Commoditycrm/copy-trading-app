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

const NAV_TRADER = [
  { href: "/trade-panel", label: "Trade Panel" },
  { href: "/positions", label: "Positions" },
  { href: "/trades", label: "Order History" },
  { href: "/calendar", label: "Calendar" },
  { href: "/subscribers", label: "Subscribers" },
  { href: "/brokers", label: "Broker" },
];
const NAV_SUBSCRIBER = [
  { href: "/positions", label: "Positions" },
  { href: "/trades", label: "Order History" },
  { href: "/calendar", label: "Calendar" },
  { href: "/brokers", label: "Broker" },
  { href: "/settings", label: "Settings" },
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
                className="block px-4 py-2.5 rounded-full text-sm transition-colors no-underline"
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
