"use client";

import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
} from "recharts";
import { fmtSignedUsd } from "@/lib/format";
import { ChartTooltip } from "./ChartTooltip";

export interface DailyBar {
  day: string; // YYYY-MM-DD
  value: number; // realized P&L for the day
}

const tickDay = (d: string) => {
  const parts = d.split("-");
  return parts.length === 3 ? `${parts[1]}/${parts[2]}` : d;
};

/** Per-day realized-P&L bars, green for gains / red for losses. */
export function DailyPnlBars({ data }: { data: DailyBar[] }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 8, right: 4, bottom: 0, left: 4 }}>
        <XAxis
          dataKey="day"
          tickFormatter={tickDay}
          tick={{ fill: "var(--muted)", fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          minTickGap={20}
        />
        <Tooltip
          content={<ChartTooltip valueFormat={fmtSignedUsd} />}
          cursor={{ fill: "var(--border)" }}
        />
        <Bar dataKey="value" radius={[3, 3, 0, 0]} animationDuration={800}>
          {data.map((d, i) => (
            <Cell
              key={i}
              fill={d.value >= 0 ? "var(--good)" : "var(--bad)"}
              fillOpacity={0.85}
            />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
