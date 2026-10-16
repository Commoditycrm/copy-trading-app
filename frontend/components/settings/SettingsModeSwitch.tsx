"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

/**
 * Standard or Advanced — which set of settings is live.
 *
 * At the top of the page because it changes what everything below MEANS. Only
 * position SIZING moves between the two: on Advanced, the multiplier and the
 * two per-trade caps come from the per-symbol rules instead. Daily limits,
 * auto-liquidation and the master switch are account-level safety nets with no
 * per-symbol equivalent, so they keep working either way and are never
 * disabled.
 *
 * Switching is not destructive in either direction: the per-symbol rules stay
 * in the database while Standard is active, and come back when Advanced is
 * chosen again.
 */
export function SettingsModeSwitch({ advanced, onChange }: {
  advanced: boolean;
  onChange: (next: boolean) => void;
}) {
  const [busy, setBusy] = useState(false);

  async function pick(next: boolean) {
    if (next === advanced || busy) return;
    setBusy(true);
    try {
      await api("/api/settings/subscriber/symbols/mode", {
        method: "PATCH",
        body: JSON.stringify({ enabled: next }),
      });
      onChange(next);
      notify.success(next ? "Advanced settings are now live" : "Back to standard settings");
    } catch (e) {
      notify.fromError(e, "Could not switch settings mode");
    } finally {
      setBusy(false);
    }
  }

  const OPTIONS = [
    {
      value: false,
      label: "Standard settings",
      hint: "One multiplier and one set of limits for everything you copy.",
    },
    {
      value: true,
      label: "Advanced settings",
      hint: "Size and exits per symbol. Your daily limits and auto-liquidation still apply.",
    },
  ];

  return (
    <section
      className="rounded-xl border overflow-hidden"
      style={{
        borderColor: "var(--border)",
        background: "linear-gradient(180deg, var(--panel) 0%, rgba(0,0,0,0.18) 100%)",
      }}
    >
      <div className="px-4 py-3 grid gap-2 sm:grid-cols-2">
        {OPTIONS.map(o => {
          const on = advanced === o.value;
          return (
            <label
              key={String(o.value)}
              className="flex items-start gap-2.5 rounded-lg border px-3 py-2.5 cursor-pointer transition-colors"
              style={{
                borderColor: on ? "var(--accent)" : "var(--border)",
                background: on ? "rgba(255,255,255,0.04)" : "transparent",
              }}
            >
              <input
                type="radio"
                name="settings-mode"
                checked={on}
                disabled={busy}
                onChange={() => void pick(o.value)}
                className="mt-0.5"
              />
              <span className="min-w-0">
                <span className="text-sm font-semibold block leading-tight">{o.label}</span>
                <span className="text-[11px] block mt-0.5 leading-snug" style={{ color: "var(--muted)" }}>
                  {o.hint}
                </span>
              </span>
            </label>
          );
        })}
      </div>
    </section>
  );
}
