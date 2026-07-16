"use client";

import { useState } from "react";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { apiBlob } from "@/lib/api";

/** Downloads an .xlsx from an authed API endpoint.
 *
 *  Can't use a plain <a href> — these routes need the bearer token, and a link
 *  navigation won't carry it. So fetch via apiBlob (which shares api()'s
 *  401→refresh retry), then hand the blob to a throwaway anchor. The filename
 *  comes from the server's Content-Disposition, so it matches what the backend
 *  recorded in the audit log.
 */
export function ExportButton({
  path,
  label = "Export",
  fallbackName = "export.xlsx",
  disabled,
}: {
  /** API path incl. query string, e.g. "/api/trades/export?status=filled". */
  path: string;
  label?: string;
  fallbackName?: string;
  disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    try {
      const res = await apiBlob(path);
      const cd = res.headers.get("Content-Disposition") || "";
      const name = /filename="?([^";]+)"?/.exec(cd)?.[1] || fallbackName;

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Revoking immediately can cancel the download in some browsers; a tick
      // is enough for the click to have been handed off.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      notify.success(`Downloaded ${name}`);
    } catch (e) {
      notify.fromError(e, "Could not export");
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      onClick={run}
      disabled={busy || disabled}
      title="Download the rows matching the current filters as an Excel file"
      className="text-xs px-3 py-1.5 rounded-lg inline-flex items-center gap-1.5 font-medium whitespace-nowrap"
      style={{
        background: "var(--panel-2)",
        border: "1px solid var(--border)",
        color: "var(--text-2)",
        opacity: busy || disabled ? 0.6 : 1,
        cursor: busy || disabled ? "not-allowed" : "pointer",
      }}
    >
      {busy ? <Spinner /> : (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <polyline points="7 10 12 15 17 10" />
          <line x1="12" y1="15" x2="12" y2="3" />
        </svg>
      )}
      {busy ? "Exporting…" : label}
    </button>
  );
}
