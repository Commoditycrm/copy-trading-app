"use client";

import { useEffect, useRef } from "react";
import { getAccessToken } from "@/lib/api";

export type AppEvent =
  | { type: "order.placed"; order: OrderEventPayload }
  | { type: "order.copy_submitted"; order: OrderEventPayload }
  | { type: "order.copy_failed"; order: OrderEventPayload }
  | { type: "order.cancelled"; order: OrderEventPayload };

export interface OrderEventPayload {
  id: string;
  parent_order_id: string | null;
  broker_account_id: string;
  symbol: string;
  side: string;
  order_type: string;
  quantity: string;
  filled_quantity: string;
  filled_avg_price: string | null;
  status: string;
  broker_order_id: string | null;
  instrument_type: string;
  created_at: string | null;
  reject_reason: string | null;
}

/**
 * Subscribe to the server's per-user SSE stream.
 * `onEvent` is called on every push (re-renders are the caller's responsibility).
 *
 * Auth via query-param token because EventSource can't set headers.
 */
export function useEventStream(onEvent: (e: AppEvent) => void): void {
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    const token = getAccessToken();
    if (!token) return;

    const url = `/api/events?token=${encodeURIComponent(token)}`;
    const es = new EventSource(url);

    es.onmessage = (msg) => {
      try {
        const evt = JSON.parse(msg.data) as AppEvent;
        handlerRef.current(evt);
      } catch {
        /* ignore malformed events */
      }
    };

    es.onerror = () => {
      // Browser auto-reconnects on transient errors. We only need to bail on
      // hard failures (e.g. 401), which the browser surfaces by closing the
      // stream after a few retries.
    };

    return () => es.close();
  }, []);
}
