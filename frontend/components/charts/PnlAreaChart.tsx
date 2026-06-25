"use client";

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fmtSignedUsd } from "@/lib/format";
import { ChartTooltip } from "./ChartTooltip";

export interface PnlPoint {
  day: string; // YYYY-MM-DD
  value: number; // cumulative realized P&L
}

const tickDay = (d: string) => {
  const parts = d.split("-");
  return parts.length === 3 ? `${parts[1]}/${parts[2]}` : d;
};

/** Cumulative realized-P&L area chart. Color follows the ending sign. */
export function PnlAreaChart({ data }: { data: PnlPoint[] }) {
  const last = data.length ? data[data.length - 1].value : 0;
  const positive = last >= 0;
  const stroke = positive ? "var(--good)" : "var(--bad)";

  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 4 }}>
        <defs>
          <linearGradient id="pnlArea" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity={0.28} />
            <stop offset="100%" stopColor={stroke} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid
          vertical={false}
          stroke="var(--border)"
          strokeDasharray="3 3"
        />
        <XAxis
          dataKey="day"
          tickFormatter={tickDay}
          tick={{ fill: "var(--muted)", fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          minTickGap={28}
        />
        <YAxis
          tick={{ fill: "var(--muted)", fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          width={52}
          tickFormatter={(v) => fmtSignedUsd(v)}
        />
        <Tooltip
          content={
            <ChartTooltip
              valueFormat={fmtSignedUsd}
              labelFormat={(l) => l}
            />
          }
          cursor={{ stroke: "var(--border-strong)", strokeWidth: 1 }}
        />
        <Area
          type="monotone"
          dataKey="value"
          stroke={stroke}
          strokeWidth={2}
          fill="url(#pnlArea)"
          animationDuration={900}
          animationEasing="ease-out"
          dot={false}
          activeDot={{ r: 4, strokeWidth: 0, fill: stroke }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
