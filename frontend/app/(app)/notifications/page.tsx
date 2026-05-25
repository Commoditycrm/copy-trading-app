"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
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

function fmtAbsolute(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", year: "numeric",
    hour: "2-digit", minute: "2-digit",
    hour12: false,
  });
}

export default function NotificationsPage() {
  const [items, setItems] = useState<AppNotification[]>([]);
  const [loading, setLoading] = useState(true);
  const [markingAll, setMarkingAll] = useState(false);

  async function load() {
    try {
      const r = await api<AppNotification[]>("/api/notifications?limit=50");
      setItems(r);
    } catch (e) { notify.fromError(e, "Could not load notifications"); }
    finally { setLoading(false); }
  }

  useEffect(() => { load(); }, []);

  // Live update — when SSE fires, prepend to the list without a refresh.
  useEventStream((evt) => {
    if (evt.type === "notification.created") {
      const n = evt.notification;
      setItems(cur => [
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
    // Optimistic update — flip locally, fire request; revert on error.
    const before = items;
    setItems(cur => cur.map(n => n.id === id ? { ...n, read_at: new Date().toISOString() } : n));
    try {
      await api(`/api/notifications/${id}/read`, { method: "POST" });
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
      setItems(cur => cur.map(n => n.read_at ? n : { ...n, read_at: now }));
      notify.success("All notifications marked as read");
    } catch (e) {
      notify.fromError(e, "Could not mark all as read");
    } finally {
      setMarkingAll(false);
    }
  }

  const unreadCount = items.filter(n => n.read_at === null).length;

  return (
    <div className="flex flex-col h-full max-w-4xl space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Notifications</h1>
          <p className="text-sm" style={{ color: "var(--muted)" }}>
            {unreadCount > 0
              ? `${unreadCount} unread`
              : "All caught up."}
            {" · "}Auto-deleted after 30 days.
          </p>
        </div>
        {unreadCount > 0 && (
          <button
            onClick={markAllRead}
            disabled={markingAll}
            className="px-3 py-2 text-sm rounded border inline-flex items-center gap-2"
            style={{ borderColor: "var(--border)", color: "var(--text-2)" }}
          >
            <span>Mark all read</span>
            {markingAll && <Spinner />}
          </button>
        )}
      </div>

      {loading && (
        <div className="text-sm" style={{ color: "var(--muted)" }}>
          <span className="inline-flex items-center gap-2">
            <Spinner />
            <span>Loading…</span>
          </span>
        </div>
      )}

      {!loading && items.length === 0 && (
        <div
          className="p-8 rounded border text-center"
          style={{ borderColor: "var(--border)", background: "var(--panel)", color: "var(--muted)" }}
        >
          No notifications yet. You&rsquo;ll see one here if a mirror order fails after retry.
        </div>
      )}

      <div className="space-y-2">
        {items.map(n => {
          const unread = n.read_at === null;
          const childOrderId = n.metadata?.["child_order_id"] as string | undefined;
          return (
            <div
              key={n.id}
              className="p-4 rounded border flex items-start gap-4"
              style={{
                borderColor: unread ? "rgba(220, 38, 38, 0.4)" : "var(--border)",
                background: unread ? "rgba(220, 38, 38, 0.04)" : "var(--panel)",
              }}
            >
              <div
                className="mt-1 w-2 h-2 rounded-full shrink-0"
                style={{ background: unread ? "var(--bad)" : "transparent" }}
                aria-label={unread ? "Unread" : "Read"}
              />
              <div className="flex-1 space-y-1">
                <div className="text-sm" style={{ fontWeight: unread ? 500 : 400 }}>
                  {n.message}
                </div>
                <div className="text-xs flex items-center gap-3" style={{ color: "var(--muted)" }}>
                  <span title={fmtAbsolute(n.created_at)}>{fmtRelative(n.created_at)}</span>
                  {childOrderId && (
                    <Link
                      href="/trades"
                      className="underline"
                      style={{ color: "var(--accent)" }}
                    >
                      View in Order History →
                    </Link>
                  )}
                </div>
              </div>
              {unread && (
                <button
                  onClick={() => markRead(n.id)}
                  className="text-xs px-2 py-1 rounded border shrink-0"
                  style={{ borderColor: "var(--border)", color: "var(--muted)" }}
                >
                  Mark read
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
