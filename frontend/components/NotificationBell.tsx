"use client";

/**
 * Header notification bell + dropdown panel.
 *
 * Replaces the old "navigate to /notifications page" behaviour: the bell now
 * opens a dropdown anchored under it (Meetup-style) listing recent
 * notifications, with per-item mark-read, mark-all-read, and a "View all"
 * link to the full page (kept as a fallback / deep view).
 *
 * The unread BADGE count is owned by AppShell (hydrated + SSE + poll) and
 * passed in; after any mark-read action we call `onChanged()` so the parent
 * re-syncs the badge. The dropdown also listens to SSE itself so an OPEN
 * panel prepends new notifications live.
 */
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Bell, BellOff, Check, ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { useEventStream } from "@/lib/sse";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import type { AppNotification } from "@/lib/types";

function fmtRelative(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return "just now";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}

export function NotificationBell({
  unreadCount,
  onChanged,
}: {
  unreadCount: number;
  /** Called after a mark-read action so the parent re-syncs the badge. */
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<AppNotification[]>([]);
  const [loading, setLoading] = useState(false);
  const [markingAll, setMarkingAll] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  async function load() {
    setLoading(true);
    try {
      const r = await api<AppNotification[]>("/api/notifications?limit=20");
      setItems(r);
    } catch (e) {
      notify.fromError(e, "Could not load notifications");
    } finally {
      setLoading(false);
    }
  }

  // Fetch fresh on each open so the panel reflects current state.
  useEffect(() => {
    if (open) load();
  }, [open]);

  // Close on outside click + Escape.
  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Live: prepend newly-created notifications while the panel is open.
  useEventStream((evt) => {
    if (evt.type === "notification.created") {
      const n = evt.notification;
      setItems((cur) => [
        {
          id: n.id,
          type: n.type,
          message: n.message,
          metadata: n.metadata,
          read_at: null,
          created_at: n.created_at,
        },
        ...cur,
      ]);
    }
  });

  async function markRead(id: string) {
    const before = items;
    setItems((cur) =>
      cur.map((n) => (n.id === id ? { ...n, read_at: new Date().toISOString() } : n)),
    );
    try {
      await api(`/api/notifications/${id}/read`, { method: "POST" });
      onChanged();
    } catch (e) {
      setItems(before);
      notify.fromError(e, "Could not mark as read");
    }
  }

  async function markAllRead() {
    setMarkingAll(true);
    try {
      await api("/api/notifications/read-all", { method: "POST" });
      const now = new Date().toISOString();
      setItems((cur) => cur.map((n) => (n.read_at ? n : { ...n, read_at: now })));
      onChanged();
    } catch (e) {
      notify.fromError(e, "Could not mark all as read");
    } finally {
      setMarkingAll(false);
    }
  }

  return (
    <div className="relative" ref={wrapRef}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title={unreadCount > 0 ? `${unreadCount} unread notification(s)` : "Notifications"}
        aria-label={unreadCount > 0 ? `Notifications, ${unreadCount} unread` : "Notifications"}
        aria-haspopup="true"
        aria-expanded={open}
        className="relative grid place-items-center rounded-full transition-colors focus-ring"
        style={{
          width: 32,
          height: 32,
          background: "var(--chip-bg)",
          border: "1px solid var(--border)",
          color: open || unreadCount > 0 ? "var(--accent)" : "var(--text-2)",
        }}
      >
        <Bell size={16} />
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

      {open && (
        <div
          role="menu"
          className="absolute right-0 mt-2 rounded-2xl overflow-hidden z-50 animate-fade-in"
          style={{
            width: "min(360px, calc(100vw - 24px))",
            background: "var(--panel)",
            border: "1px solid var(--border)",
            boxShadow: "0 12px 40px rgba(0,0,0,0.35)",
          }}
        >
          {/* Header */}
          <div
            className="flex items-center justify-between gap-2 px-4 py-3"
            style={{ borderBottom: "1px solid var(--border)" }}
          >
            <div className="text-sm font-semibold" style={{ color: "var(--text)" }}>
              Notifications
              {unreadCount > 0 && (
                <span className="ml-1.5 text-xs font-normal" style={{ color: "var(--muted)" }}>
                  · {unreadCount} unread
                </span>
              )}
            </div>
            {items.some((n) => n.read_at === null) && (
              <button
                onClick={markAllRead}
                disabled={markingAll}
                className="text-xs inline-flex items-center gap-1 focus-ring rounded px-1.5 py-0.5"
                style={{ color: "var(--accent)" }}
              >
                {markingAll ? <Spinner /> : <Check size={12} />} Mark all read
              </button>
            )}
          </div>

          {/* Body */}
          <div className="max-h-[60vh] overflow-y-auto">
            {loading && (
              <div className="p-3 space-y-2">
                {Array.from({ length: 4 }).map((_, i) => (
                  <div key={i} className="skeleton h-12 rounded-lg" />
                ))}
              </div>
            )}

            {!loading && items.length === 0 && (
              <div
                className="flex flex-col items-center text-center gap-1.5 px-6 py-10"
                style={{ color: "var(--muted)" }}
              >
                <BellOff size={24} />
                <div className="text-sm" style={{ color: "var(--text)" }}>
                  You&rsquo;re all caught up
                </div>
                <div className="text-xs">New notifications show up here.</div>
              </div>
            )}

            {!loading &&
              items.map((n) => {
                const unread = n.read_at === null;
                return (
                  <button
                    key={n.id}
                    type="button"
                    onClick={() => unread && markRead(n.id)}
                    className="w-full text-left flex items-start gap-3 px-4 py-3 transition-colors hover:bg-[var(--panel-2)]"
                    style={{
                      borderBottom: "1px solid var(--border)",
                      background: unread ? "var(--bad-soft)" : undefined,
                      cursor: unread ? "pointer" : "default",
                    }}
                  >
                    {unread && (
                      <span
                        className="rounded-full shrink-0 mt-1.5"
                        style={{ width: 7, height: 7, background: "var(--bad)" }}
                        aria-hidden
                      />
                    )}
                    <span className="flex-1 min-w-0">
                      <span
                        className="block text-sm leading-snug"
                        style={{ color: "var(--text)", fontWeight: unread ? 600 : 400 }}
                      >
                        {n.message}
                      </span>
                      <span className="block text-xs mt-0.5" style={{ color: "var(--muted)" }}>
                        {fmtRelative(n.created_at)}
                      </span>
                    </span>
                  </button>
                );
              })}
          </div>

          {/* Footer */}
          <Link
            href="/notifications"
            onClick={() => setOpen(false)}
            className="flex items-center justify-center gap-1 px-4 py-2.5 text-xs font-medium no-underline focus-ring transition-colors hover:bg-[var(--panel-2)]"
            style={{ color: "var(--accent)", borderTop: "1px solid var(--border)" }}
          >
            View all notifications <ChevronRight size={13} />
          </Link>
        </div>
      )}
    </div>
  );
}
