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

/** Pull a readable message out of whatever was thrown — ApiError detail,
 *  Error.message, or a string. */
export function describeError(e: unknown, fallback = "Something went wrong"): string {
  if (e instanceof ApiError) return String(e.detail ?? fallback);
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
