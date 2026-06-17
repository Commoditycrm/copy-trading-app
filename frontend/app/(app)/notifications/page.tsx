"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { motion } from "framer-motion";
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

function fmtAbsolute(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString("en-US", {
    timeZone: "America/New_York", timeZoneName: "short",
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
    <div className="max-w-3xl mx-auto">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        className="flex items-start justify-between gap-3 mb-5"
      >
        <div>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight" style={{ color: "var(--text)" }}>
            Notifications
          </h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            {unreadCount > 0 ? `${unreadCount} unread` : "All caught up."}
            {" · "}Auto-deleted after 30 days.
          </p>
        </div>
        {unreadCount > 0 && (
          <button
            onClick={markAllRead}
            disabled={markingAll}
            className="btn-ghost px-3 py-2 text-sm inline-flex items-center gap-2 shrink-0"
          >
            <Check size={14} />
            <span>Mark all read</span>
            {markingAll && <Spinner />}
          </button>
        )}
      </motion.div>

      {loading && (
        <div className="space-y-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="skeleton h-[72px] rounded-card" />
          ))}
        </div>
      )}

      {!loading && items.length === 0 && (
        <div className="card p-10 flex flex-col items-center text-center gap-2" style={{ color: "var(--muted)" }}>
          <BellOff size={28} />
          <div className="text-sm" style={{ color: "var(--text)" }}>You&rsquo;re all caught up</div>
          <div className="text-xs">A notification appears here if a mirror order fails after retry.</div>
        </div>
      )}

      <div className="space-y-2">
        {items.map((n, i) => {
          const unread = n.read_at === null;
          const childOrderId = n.metadata?.["child_order_id"] as string | undefined;
          return (
            <motion.div
              key={n.id}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.28, delay: Math.min(i * 0.03, 0.2) }}
              className="card p-4 flex items-start gap-3.5"
              style={unread ? { borderColor: "rgba(239,68,68,0.35)", background: "var(--bad-soft)" } : undefined}
            >
              <span
                className="grid place-items-center rounded-full shrink-0 mt-0.5"
                style={{
                  width: 32, height: 32,
                  background: unread ? "var(--bad-soft)" : "var(--panel-2)",
                  color: unread ? "var(--bad)" : "var(--muted)",
                }}
              >
                <Bell size={16} />
              </span>
              <div className="flex-1 min-w-0 space-y-1">
                <div className="text-sm" style={{ color: "var(--text)", fontWeight: unread ? 600 : 400 }}>
                  {n.message}
                </div>
                <div className="text-xs flex items-center gap-3 flex-wrap" style={{ color: "var(--muted)" }}>
                  <span title={fmtAbsolute(n.created_at)}>{fmtRelative(n.created_at)}</span>
                  {childOrderId && (
                    <Link href="/trades" className="inline-flex items-center gap-0.5 no-underline focus-ring rounded" style={{ color: "var(--accent)" }}>
                      View in Order History <ChevronRight size={12} />
                    </Link>
                  )}
                </div>
              </div>
              {unread && (
                <button
                  onClick={() => markRead(n.id)}
                  className="btn-ghost text-xs px-2.5 py-1 shrink-0 inline-flex items-center gap-1"
                >
                  <Check size={12} /> Mark read
                </button>
              )}
            </motion.div>
          );
        })}
      </div>
    </div>
  );
}
