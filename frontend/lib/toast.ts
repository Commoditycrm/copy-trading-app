"use client";

/**
 * Thin wrapper over react-toastify so the rest of the app uses one
 * consistent API + theming.
 *
 * Use:
 *   import { notify } from "@/lib/toast";
 *   notify.success("Order placed");
 *   notify.error("Broker rejected: insufficient buying power");
 *   notify.info("Synced 4 fills");
 *   notify.warn("Daily loss limit at 80%");
 */
import { ApiError } from "@/lib/api";
import { toast, ToastOptions } from "react-toastify";

const base: ToastOptions = {
  position: "top-right",
  autoClose: 3000,
  hideProgressBar: false,
  closeOnClick: true,
  pauseOnHover: true,
  draggable: true,
  theme: "dark",
};

// Backend uses short machine-readable codes in HTTPException(detail=...).
// This map turns each one into something a trader would actually read.
const ERROR_MESSAGES: Record<string, string> = {
  // auth
  email_taken: "An account with that email already exists.",
  invalid_credentials: "Email or password is incorrect.",
  invalid_token: "Your session has expired. Please sign in again.",
  wrong_token: "Your session is invalid. Please sign in again.",
  wrong_token_type: "Your session is invalid. Please sign in again.",
  missing_token: "Please sign in to continue.",
  user_inactive: "This account is disabled. Contact support.",
  trader_already_exists: "A trader account already exists. Only one trader is allowed.",

  // role gates
  trader_only: "Only the trader can do this.",
  subscriber_only: "Only subscribers can do this.",

  // trading kill-switch
  trading_disabled: "Trading is turned off. Enable it before placing orders.",

  // brokers
  broker_account_not_found: "We can't find that broker account.",
  broker_account_missing: "This order has no broker account attached.",
  broker_not_connected: "That broker isn't connected. Reconnect it before trading.",
  "alpaca credentials required": "Please enter your Alpaca API key and secret.",
  "unknown broker": "That broker isn't supported yet.",
  "options chain only implemented for alpaca": "Options are only supported on Alpaca for now.",

  // orders
  not_found: "We couldn't find that record.",
  cannot_close_mirror: "Mirrored orders can't be closed directly — close the original.",
  quantity_must_be_positive: "Quantity must be greater than zero.",
  quantity_exceeds_original_filled: "You can't close more than the original filled quantity.",
  quantity_exceeds_position: "You can't close more than you currently hold.",
  position_not_found: "We couldn't find that position at your broker.",

  // settings / subscribers
  settings_missing: "Your settings haven't been initialized yet.",
  trader_not_found: "That trader doesn't exist.",
  subscriber_not_found: "That subscriber doesn't exist.",
  not_a_subscriber: "That user isn't a subscriber of yours.",

  // misc
  "from must be <= to": "The start date must be on or before the end date.",
};

// Known Alpaca error codes that benefit from a hand-written explanation.
// Falls through to the broker's own `message` field if we don't have one.
const ALPACA_CODE_MESSAGES: Record<number, string> = {
  40310000: "Order rejected: this would be a wash trade. Cancel the opposite-side order first or use a complex order.",
  40010001: "Order rejected: insufficient buying power.",
  40010002: "Order rejected: insufficient shares to sell.",
  42210000: "Order rejected: market is closed.",
};

/** Brokers (especially Alpaca) often return JSON-encoded errors. Pull out the
 *  human bits — `message`, `reject_reason`, or a known code — and present them
 *  cleanly. Falls back to the raw string if it isn't JSON. */
function formatBrokerError(rest: string): string {
  if (!rest) return "Your broker rejected the order.";

  // Some adapters wrap the JSON in a prefix like "400 - {...}". Find the JSON
  // by hunting for the first '{'.
  const braceIdx = rest.indexOf("{");
  const jsonPart = braceIdx >= 0 ? rest.slice(braceIdx) : rest;
  try {
    const obj = JSON.parse(jsonPart) as {
      code?: number;
      message?: string;
      reject_reason?: string;
    };
    if (typeof obj.code === "number" && ALPACA_CODE_MESSAGES[obj.code]) {
      return ALPACA_CODE_MESSAGES[obj.code];
    }
    const pieces = [obj.message, obj.reject_reason]
      .filter((s): s is string => typeof s === "string" && s.trim().length > 0)
      .map(s => s.trim().replace(/^./, c => c.toUpperCase()));
    if (pieces.length > 0) return `Order rejected: ${pieces.join(" — ")}.`;
  } catch {
    // Not JSON, fall through.
  }
  return `Your broker rejected the order: ${rest}`;
}

/** Translate a raw backend detail (string, list, or unknown) into a friendly
 *  message. Falls back to `fallback` if nothing matches. */
function humanize(detail: unknown, status: number | undefined, fallback: string): string {
  // FastAPI validation errors arrive as an array of {loc, msg, type}.
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: string; loc?: unknown[] };
    if (first?.msg) {
      // Strip Pydantic's "Value error, " prefix if present.
      return first.msg.replace(/^Value error, /, "");
    }
  }

  if (typeof detail === "string") {
    const raw = detail.trim();

    // Exact match against the known-codes table.
    if (ERROR_MESSAGES[raw]) return ERROR_MESSAGES[raw];

    // Prefixed dynamic codes from the backend.
    if (raw.startsWith("broker_error:")) {
      const rest = raw.slice("broker_error:".length).trim();
      return formatBrokerError(rest);
    }
    if (raw.startsWith("not_cancellable:")) {
      return "This order can't be cancelled in its current state.";
    }
    if (raw.startsWith("not_closeable:")) {
      return "This order can't be closed in its current state.";
    }

    // Server-side issues (5xx) get a generic friendly message —
    // technical detail isn't useful to a trader.
    if (status && status >= 500) {
      return "Something went wrong on our end. Please try again in a moment.";
    }

    // Otherwise: if it looks like a snake_case code we didn't map, prettify it
    // (better than showing the raw identifier).
    if (/^[a-z][a-z0-9_]*$/.test(raw)) {
      return raw.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) + ".";
    }

    return raw;
  }

  return fallback;
}

/** Pull a readable message out of whatever was thrown — ApiError detail,
 *  Error.message, or a string. */
export function describeError(e: unknown, fallback = "Something went wrong"): string {
  if (e instanceof ApiError) {
    // Network/CORS issues come through as ApiError with no real detail.
    if (e.status === 0) return "Can't reach the server. Check your connection and try again.";
    return humanize(e.detail, e.status, fallback);
  }
  if (e instanceof Error) return e.message || fallback;
  if (typeof e === "string") return e;
  return fallback;
}

export const notify = {
  success: (msg: string, opts?: ToastOptions) =>
    toast.success(msg, { ...base, ...opts }),
  error: (msg: string, opts?: ToastOptions) =>
    toast.error(msg, { ...base, ...opts }),
  info: (msg: string, opts?: ToastOptions) =>
    toast.info(msg, { ...base, ...opts }),
  warn: (msg: string, opts?: ToastOptions) =>
    toast.warn(msg, { ...base, ...opts }),

  /** Convenience: surface a thrown error as a toast. */
  fromError: (e: unknown, fallback?: string) =>
    toast.error(describeError(e, fallback), base),
};
