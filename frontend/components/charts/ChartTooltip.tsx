"use client";

/** Shared themed tooltip for the dashboard charts. Recharts injects
 *  `active`/`payload`/`label` at runtime (v3 drops them from the static
 *  TooltipProps type), so we type them loosely here. `valueFormat` turns the
 *  numeric payload into a display string (currency, count, …). */
interface ChartTooltipProps {
  active?: boolean;
  payload?: Array<{ value?: number | string }>;
  label?: string | number;
  valueFormat: (n: number) => string;
  labelFormat?: (l: string) => string;
}

export function ChartTooltip({
  active,
  payload,
  label,
  valueFormat,
  labelFormat,
}: ChartTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  const v = payload[0]?.value;
  return (
    <div
      className="rounded-token px-3 py-2 text-xs"
      style={{
        background: "var(--panel)",
        border: "1px solid var(--border-strong)",
        boxShadow: "var(--shadow-pop)",
        color: "var(--text)",
      }}
    >
      <div style={{ color: "var(--muted)" }} className="mb-0.5">
        {labelFormat ? labelFormat(String(label)) : String(label)}
      </div>
      <div className="num font-semibold">
        {typeof v === "number" ? valueFormat(v) : "—"}
      </div>
    </div>
  );
}
