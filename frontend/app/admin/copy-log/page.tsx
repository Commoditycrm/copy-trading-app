"use client";

/**
 * Admin Copy Log — a timeline of every copy-trading ON/OFF change: when, the
 * new state, why, and who did it (the user manually, the trader's master
 * switch, or the system via a daily limit / auto-liquidation). Read straight
 * from the audit log. Filter by user email.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

interface CopyEvent {
  at: string;
  user_id: string | null;
  user_email: string | null;
  state: "ON" | "OFF";
  source: "User" | "System" | "Trader";
  reason: string;
  detail: string | null;
}

/** ISO → "Sep 16, 7:18 PM ET" (market time, like the rest of the app). */
function fmt(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-US", {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    timeZone: "America/New_York",
  }) + " ET";
}

export default function CopyLogPage() {
  const [events, setEvents] = useState<CopyEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [email, setEmail] = useState("");
  const [applied, setApplied] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const qs = applied.trim() ? `?email=${encodeURIComponent(applied.trim())}` : "";
      const r = await api<{ events: CopyEvent[] }>(`/api/admin/copy-log${qs}`);
      setEvents(r.events);
    } catch (e) {
      notify.fromError(e, "Could not load copy log");
    } finally {
      setLoading(false);
    }
  }, [applied]);
  useEffect(() => { load(); }, [load]);

  const th = "px-4 py-3 text-xs font-semibold whitespace-nowrap";
  const td = "px-4 py-3 text-sm whitespace-nowrap";

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-bold">Copy Log</h2>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          Every time copy trading turned ON or OFF — with why, and who did it (the user, the trader&apos;s master
          switch, or the system via a daily limit). Newest first.
        </p>
      </div>

      {/* Filter by user email */}
      <form onSubmit={(e) => { e.preventDefault(); setApplied(email); }}
            className="flex items-center gap-2 flex-wrap">
        <input value={email} onChange={(e) => setEmail(e.target.value)}
               placeholder="Filter by user email…"
               className="text-sm px-3 py-1.5 rounded-lg outline-none w-64"
               style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text)" }} />
        <button type="submit"
                className="px-3 py-1.5 rounded-lg text-xs font-semibold"
                style={{ background: "var(--accent)", color: "var(--accent-ink)", border: "1px solid var(--accent)" }}>
          Filter
        </button>
        {applied && (
          <button type="button" onClick={() => { setEmail(""); setApplied(""); }}
                  className="px-3 py-1.5 rounded-lg text-xs font-semibold"
                  style={{ background: "var(--panel-2)", color: "var(--text)", border: "1px solid var(--border)" }}>
            Clear
          </button>
        )}
      </form>

      {loading ? (
        <div style={{ color: "var(--muted)" }}>Loading…</div>
      ) : events.length === 0 ? (
        <div className="rounded-xl p-10 text-center" style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
          No copy on/off events{applied ? ` for “${applied}”` : ""} yet.
        </div>
      ) : (
        <div className="rounded-xl overflow-hidden" style={{ border: "1px solid var(--border)" }}>
          <div className="overflow-auto" style={{ maxHeight: "70vh" }}>
            <table className="w-full">
              <thead className="sticky top-0 z-10" style={{ background: "var(--panel)" }}>
                <tr style={{ borderBottom: "1px solid var(--border)" }}>
                  <th className={`${th} text-left`} style={{ color: "var(--muted)" }}>When (ET)</th>
                  <th className={`${th} text-left`} style={{ color: "var(--muted)" }}>User</th>
                  <th className={`${th} text-left`} style={{ color: "var(--muted)" }}>State</th>
                  <th className={`${th} text-left`} style={{ color: "var(--muted)" }}>Who</th>
                  <th className={`${th} text-left`} style={{ color: "var(--muted)" }}>Reason</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e, i) => {
                  const on = e.state === "ON";
                  return (
                    <tr key={i} style={{ borderBottom: "1px solid var(--border)" }}>
                      <td className={`${td}`} style={{ color: "var(--text-2)" }}>{fmt(e.at)}</td>
                      <td className={`${td}`}>{e.user_email ?? <span style={{ color: "var(--muted)" }}>—</span>}</td>
                      <td className={td}>
                        <span className="text-xs px-2 py-0.5 rounded-full font-semibold"
                              style={{
                                background: on ? "var(--good-soft)" : "rgba(239,68,68,0.12)",
                                color: on ? "var(--good)" : "var(--bad)",
                              }}>
                          {on ? "ON" : "OFF"}
                        </span>
                      </td>
                      <td className={td} style={{ color: "var(--text-2)" }}>{e.source}</td>
                      <td className={td} style={{ color: "var(--text-2)" }}>
                        {e.reason}
                        {e.detail && <span style={{ color: "var(--muted)" }}> · {e.detail}</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
