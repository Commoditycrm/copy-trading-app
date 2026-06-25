"use client";

import { motion } from "framer-motion";
import type { LucideIcon } from "lucide-react";
import { AnimatedNumber } from "./AnimatedNumber";

export type KpiTone = "neutral" | "good" | "bad" | "accent";

export interface KpiCardProps {
  label: string;
  value: number;
  format: (n: number) => string;
  icon: LucideIcon;
  tone?: KpiTone;
  /** Small caption under the value (e.g. "vs. last 30 days"). */
  sub?: string;
  /** Optional delta pill, e.g. { text: "+4.2%", tone: "good" }. */
  delta?: { text: string; tone: "good" | "bad" | "flat" } | null;
  /** Stagger index for the entrance animation. */
  index?: number;
  /** Tighter padding + smaller value/icon for a slimmer card. */
  compact?: boolean;
}

const toneColor: Record<KpiTone, string> = {
  neutral: "var(--text)",
  good: "var(--good)",
  bad: "var(--bad)",
  accent: "var(--accent)",
};

const iconBg: Record<KpiTone, string> = {
  neutral: "var(--chip-bg)",
  good: "var(--good-soft)",
  bad: "var(--bad-soft)",
  accent: "var(--accent-glow)",
};

const deltaColor = { good: "var(--good)", bad: "var(--bad)", flat: "var(--muted)" };

export function KpiCard({
  label,
  value,
  format,
  icon: Icon,
  tone = "neutral",
  sub,
  delta,
  index = 0,
  compact = false,
}: KpiCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: index * 0.06, ease: [0.16, 1, 0.3, 1] }}
      className={`card hover-lift relative overflow-hidden ${compact ? "p-3.5" : "p-5"}`}
    >
      {/* faint accent wash in the corner */}
      <div
        aria-hidden
        className="pointer-events-none absolute -top-10 -right-10 h-28 w-28 rounded-full opacity-40 blur-2xl"
        style={{ background: tone === "neutral" ? "transparent" : iconBg[tone] }}
      />
      <div className="flex items-start justify-between gap-3">
        <span
          className="text-[11px] font-medium uppercase tracking-wider"
          style={{ color: "var(--muted)" }}
        >
          {label}
        </span>
        <span
          className="grid place-items-center rounded-token shrink-0"
          style={{
            width: compact ? 28 : 34,
            height: compact ? 28 : 34,
            background: iconBg[tone],
            border: "1px solid var(--border)",
            color: toneColor[tone],
          }}
        >
          <Icon size={compact ? 15 : 17} />
        </span>
      </div>

      <div className={`${compact ? "mt-2" : "mt-3"} flex items-end gap-2`}>
        <AnimatedNumber
          value={value}
          format={format}
          className={compact ? "num text-[19px] font-semibold leading-none" : "num num-lg"}
        />
        {delta && (
          <span
            className="chip mb-1"
            style={{
              color: deltaColor[delta.tone],
              background:
                delta.tone === "good"
                  ? "var(--good-soft)"
                  : delta.tone === "bad"
                  ? "var(--bad-soft)"
                  : "var(--panel)",
              borderColor: "var(--border)",
            }}
          >
            {delta.text}
          </span>
        )}
      </div>

      {sub && (
        <div className="mt-1 text-xs" style={{ color: "var(--muted)" }}>
          {sub}
        </div>
      )}
    </motion.div>
  );
}
