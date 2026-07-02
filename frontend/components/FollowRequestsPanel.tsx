"use client";

import { useCallback, useEffect, useState } from "react";
import { UserPlus } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { useEventStream } from "@/lib/sse";
import { Spinner } from "@/components/Spinner";
import type { FollowRequest } from "@/lib/types";

/** Trader-facing pending follow-request approvals. Self-contained: fetches
 *  its own list, live-refreshes on any follow.* notification (SSE), and
 *  renders nothing when there's nothing pending. Used on both the Subscribers
 *  page and the trader's Settings page. */
export function FollowRequestsPanel(
  { className = "", onDecision }: { className?: string; onDecision?: () => void },
) {
  const [items, setItems] = useState<FollowRequest[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(() => {
    api<FollowRequest[]>("/api/follow-requests/incoming").then(setItems).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  // Live-refresh when a follow.requested (new) / follow.* notification lands.
  useEventStream((evt) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const e = evt as any;
    if (e?.type === "notification.created" && typeof e.notification?.type === "string"
        && e.notification.type.startsWith("follow.")) {
      load();
    }
  });

  async function decide(id: string, approve: boolean) {
    setBusyId(id);
    try {
      await api(`/api/follow-requests/${id}/${approve ? "approve" : "reject"}`, { method: "POST" });
      notify.success(approve ? "Approved — they're now following you" : "Request declined");
      load();
      onDecision?.();  // let the parent (e.g. subscribers table) refresh
    } catch (e) {
      notify.fromError(e, "Could not update request");
    } finally {
      setBusyId(null);
    }
  }

  if (items.length === 0) return null;

  return (
    <div className={`card p-4 ${className}`}>
      <div className="flex items-center gap-2 mb-3">
        <UserPlus size={16} style={{ color: "var(--accent)" }} />
        <h2 className="text-sm font-semibold">Follow requests</h2>
        <span className="chip">{items.length}</span>
      </div>
      <div className="space-y-2">
        {items.map(r => {
          const busy = busyId === r.id;
          return (
            <div
              key={r.id}
              className="flex items-center justify-between gap-3 rounded-lg border p-3"
              style={{ borderColor: "var(--border)" }}
            >
              <div className="min-w-0">
                <div className="text-sm font-medium truncate">
                  {r.subscriber_name || r.subscriber_email || "A subscriber"}
                </div>
                {r.subscriber_name && r.subscriber_email && (
                  <div className="text-xs truncate" style={{ color: "var(--muted)" }}>
                    {r.subscriber_email}
                  </div>
                )}
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <button
                  onClick={() => decide(r.id, false)}
                  disabled={busy}
                  className="btn-ghost px-3 py-1.5 text-xs"
                >
                  Decline
                </button>
                <button
                  onClick={() => decide(r.id, true)}
                  disabled={busy}
                  className="px-4 py-1.5 text-xs rounded-lg font-semibold inline-flex items-center gap-1.5 disabled:opacity-50"
                  style={{ background: "var(--accent)", color: "#06121f" }}
                >
                  {busy && <Spinner />}
                  Approve
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
