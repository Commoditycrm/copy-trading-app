"use client";

/**
 * Small status pill that surfaces the Alpaca trade_updates listener health:
 *
 *   🟢 Live           ← connected and receiving
 *   🟡 Reconnecting   ← drop detected, retrying
 *   🔴 Offline        ← disconnected (or credentials missing)
 *   ⚪ No broker      ← no broker connected
 *
 * BOTH roles see the status of THEIR OWN broker ("Broker live"): a trader sees
 * their live listener; a subscriber sees whether their own broker is connected
 * (subscribers have no listener of their own, and deliberately do NOT see the
 * trader's broker state). The trader's listener updates live via the SSE
 * `listener.state_changed` event; the subscriber's own-broker state refreshes on
 * mount + the periodic poll (that SSE is about a trader's listener, so it never
 * drives a subscriber's pill).
 */
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useEventStream, type ListenerStatus } from "@/lib/sse";
import { onBrokerChanged } from "@/lib/traderSync";

interface StatusPayload extends ListenerStatus {
  trader_id: string | null;
  viewer: "trader" | "subscriber";
}

interface Props {
  /** Role of the current user — used to choose the wording. */
  role: "trader" | "subscriber";
}

const STATE_LABEL: Record<ListenerStatus["state"], string> = {
  connecting: "Connecting…",
  connected: "Live",
  reconnecting: "Reconnecting…",
  disconnected: "Offline",
  credentials_invalid: "Broker disconnected",
  no_trader: "No trader followed",
  no_broker: "No broker",
};

const STATE_COLOR: Record<ListenerStatus["state"], string> = {
  connecting: "#facc15",
  connected: "#22c55e",
  reconnecting: "#facc15",
  disconnected: "#ef4444",
  credentials_invalid: "#ef4444",
  no_trader: "#94a3b8",
  no_broker: "#94a3b8",
};

function fmtRel(iso: string | null): string {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return `${Math.max(0, Math.floor(ms / 1000))}s ago`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}

export function ListenerPill({ role }: Props) {
  const [status, setStatus] = useState<StatusPayload | null>(null);

  // Fetch helper used by mount, SSE-reconnect re-sync, and the polling backstop.
  const refetch = () => {
    api<StatusPayload>("/api/listener/status")
      .then(s => setStatus(s))
      .catch(() => { /* ignore — pill keeps last-known state */ });
  };

  // Initial fetch on mount + periodic backstop poll. The poll catches state
  // changes that fire while our SSE subscription is briefly down (Redis
  // pub/sub is fire-and-forget — no replay on reconnect).
  useEffect(() => {
    refetch();
    const id = setInterval(refetch, 30_000);
    // Instant refresh when the user connects/disconnects a broker (else the
    // pill looks stuck until the next 30s poll or a manual page refresh).
    const offBroker = onBrokerChanged(refetch);
    return () => { clearInterval(id); offBroker(); };
  }, []);

  // Live updates via SSE.
  const sse = useEventStream((evt) => {
    if (evt.type !== "listener.state_changed") return;
    // This SSE is about a TRADER's listener. A subscriber's pill shows their own
    // broker (refreshed by fetch/poll), so never let a trader's event drive it.
    if (role !== "trader") return;
    setStatus((prev) => ({
      // Preserve trader_id + viewer; only the inner status changes.
      trader_id: prev?.trader_id ?? evt.trader_id,
      viewer: prev?.viewer ?? role,
      ...evt.status,
    }));
  });

  // Whenever the SSE socket (re)connects, re-sync from the source of truth
  // in case we missed a state_changed event during the disconnected window.
  useEffect(() => {
    if (sse.state === "connected") refetch();
  }, [sse.state]);

  const s = status?.state ?? "disconnected";
  const color = STATE_COLOR[s];
  const label = STATE_LABEL[s];
  // Both roles now describe THEIR OWN broker.
  const prefix = "Broker";

  const tooltip = (() => {
    if (s === "no_broker") return "Connect a broker on the Brokers page";
    if (s === "no_trader") return "Pick a trader to follow on the Settings page";
    if (s === "credentials_invalid") return "Broker credentials missing or revoked";
    const last = status?.last_event_at ? `last event ${fmtRel(status.last_event_at)}` : "no events yet";
    return `${prefix} ${label.toLowerCase()} · ${last}`;
  })();

  // Visible text — avoid prepending the "broker" prefix to states whose label
  // already stands alone (otherwise: "Trader's broker no trader followed" and
  // "Trader's broker broker disconnected").
  const display = (() => {
    // Subscribers see a simple binary for THEIR OWN broker: Online when
    // connected, Offline for anything else (no broker, disconnected, creds bad).
    if (role !== "trader") {
      return s === "connected" ? "Broker Online" : "Broker Offline";
    }
    if (s === "no_broker") return "No broker";
    if (s === "no_trader") return "No trader followed";
    if (s === "credentials_invalid") return `${prefix} disconnected`;
    return `${prefix} ${label.toLowerCase()}`;
  })();

  return (
    <div
      title={tooltip}
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
          width: 6,
          height: 6,
          background: color,
          boxShadow: s === "connected" ? `0 0 6px ${color}` : "none",
        }}
      />
      <span className="whitespace-nowrap">{display}</span>
    </div>
  );
}
