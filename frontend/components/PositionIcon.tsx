import { Briefcase, CircleArrowDown, CircleArrowUp, TrendingDown, TrendingUp } from "lucide-react";
import type { InstrumentType, OptionRight, OrderSide } from "@/lib/types";

/** What the icon conveys. Options are labelled by right (call/put) rather than
 *  direction: on an option row the contract type is the identifying fact, and
 *  direction is already carried by the adjacent Side column. */
export type PositionKind = "long" | "short" | "call" | "put" | "unknown";

const SPEC: Record<PositionKind, { Icon: typeof Briefcase; label: string; color: string }> = {
  long:    { Icon: TrendingUp,      label: "Long position",  color: "var(--good)" },
  short:   { Icon: TrendingDown,    label: "Short position", color: "var(--bad)" },
  call:    { Icon: CircleArrowUp,   label: "Call option",    color: "var(--good)" },
  put:     { Icon: CircleArrowDown, label: "Put option",     color: "var(--bad)" },
  unknown: { Icon: Briefcase,       label: "Position",       color: "var(--muted)" },
};

/**
 * Small glyph shown before a symbol so a row's type reads at a glance.
 *
 * Deliberately Lucide rather than emoji: emoji render per-platform in colours
 * we don't control, which fights the dark theme and nudges row height. These
 * inherit our --good/--bad/--muted tokens and stay crisp at 14px.
 *
 * Layout-inert — it sits inside the existing symbol cell and `shrink-0` stops
 * it collapsing in a narrow column, so sorting, filtering and column widths
 * are untouched.
 */
export function PositionIcon({
  kind,
  size = 14,
  className = "",
}: {
  kind: PositionKind;
  /** 14 matches our table text; bump to 16 for card headers. */
  size?: number;
  className?: string;
}) {
  const { Icon, label, color } = SPEC[kind] ?? SPEC.unknown;
  return (
    // The wrapper carries the tooltip + accessible name rather than the <svg>:
    // it gives one predictable hover target, and `title` on an svg is honoured
    // inconsistently across browsers.
    <span
      role="img"
      aria-label={label}
      title={label}
      className={`inline-flex items-center shrink-0 ${className}`}
      style={{ color }}
    >
      <Icon size={size} strokeWidth={2} aria-hidden />
    </span>
  );
}

type PositionLike = {
  instrument_type?: InstrumentType | null;
  option_right?: OptionRight | null;
  /** Signed — positive is long, negative is short (see lib/types Position). */
  quantity?: string | null;
};

/** Kind for an open POSITION. Long/short comes from the sign of quantity. */
export function positionKind(p: PositionLike): PositionKind {
  if (p.instrument_type === "option") {
    if (p.option_right === "call") return "call";
    if (p.option_right === "put") return "put";
    return "unknown";
  }
  const qty = Number(p.quantity ?? NaN);
  if (!Number.isFinite(qty) || qty === 0) return "unknown";
  return qty > 0 ? "long" : "short";
}

type OrderLike = {
  instrument_type?: InstrumentType | null;
  option_right?: OptionRight | null;
  side?: OrderSide | null;
};

/** Kind for an ORDER. An order isn't a position, so this reads the side as the
 *  direction it takes you — buy → long, sell → short. */
export function orderKind(o: OrderLike): PositionKind {
  if (o.instrument_type === "option") {
    if (o.option_right === "call") return "call";
    if (o.option_right === "put") return "put";
    return "unknown";
  }
  if (o.side === "buy") return "long";
  if (o.side === "sell") return "short";
  return "unknown";
}
