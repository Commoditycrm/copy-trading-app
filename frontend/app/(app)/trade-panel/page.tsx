"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import { api, ApiError } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { PageLoading } from "@/components/PageLoading";
import { OpenPositionsTable, type OpenPositionsTableHandle } from "@/components/OpenPositionsTable";
import { BulkExitBar } from "@/components/BulkExitBar";
import { SearchableSelect } from "@/components/SearchableSelect";
import type { BrokerAccount, InstrumentType, Order, OrderSide, OrderType, OptionRight } from "@/lib/types";

/** Build OCC option symbol — ROOT + YYMMDD + C/P + strike*1000 (8 digits). */
function buildOccSymbol(
  symbol: string, expiryISO: string, strike: string, right: OptionRight
): string | null {
  if (!symbol || !expiryISO || !strike) return null;
  const d = new Date(expiryISO + "T00:00:00Z");
  if (Number.isNaN(d.getTime())) return null;
  const yy = String(d.getUTCFullYear() % 100).padStart(2, "0");
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const cp = right === "call" ? "C" : "P";
  const strikeNum = Number(strike);
  if (!Number.isFinite(strikeNum) || strikeNum <= 0) return null;
  const strikeInt = Math.round(strikeNum * 1000);
  return `${symbol.toUpperCase()}${yy}${mm}${dd}${cp}${String(strikeInt).padStart(8, "0")}`;
}

function fmtMoney(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  return n.toLocaleString(undefined, { style: "currency", currency: "USD" });
}

const POPULAR_SYMBOLS = [
  "AAPL", "NVDA", "TSLA", "AMZN", "MSFT", "META", "GOOGL", "AMD",
];

// ── shared visual primitives ─────────────────────────────────────────────────

/** Dark glass surface used by the Watchlist placeholder (keeps the
 *  original quiet look — it's a sidekick to the ticket, not the
 *  primary focus). Use `ticketStyle` for the actual trade form. */
const cardStyle: React.CSSProperties = {
  background: "var(--panel)",
  border: "1px solid var(--border)",
  borderRadius: "var(--r)",
  backdropFilter: "blur(10px)",
  WebkitBackdropFilter: "blur(10px)",
};

/** Trade-ticket surface — slightly brighter than `cardStyle` so the
 *  primary trade form holds its own contrast against the page bg.
 *  Aim is "clearly visible without screaming"; the previous 0.92/0.85
 *  read as washed-out, this 0.78/0.7 sits between the original (too
 *  dark) and the over-bright revision. */
const ticketStyle: React.CSSProperties = {
  background: "var(--panel)",
  border: "1px solid var(--border-strong)",
  borderRadius: "var(--r)",
  backdropFilter: "blur(10px)",
  WebkitBackdropFilter: "blur(10px)",
};

/** Inset input style — flat page-bg fill (#07090b) so every input and
 *  dropdown trigger reads at the same pitch. */
const inputStyle: React.CSSProperties = {
  background: "var(--bg-tint)",
  border: "1px solid var(--border)",
  borderRadius: 8,
};

// ── primitive components ─────────────────────────────────────────────────────

function TinyLabel({ children, hint }: { children: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between mb-1">
      <span className="text-[9px] uppercase tracking-[0.15em] font-semibold" style={{ color: "var(--text-2)" }}>
        {children}
      </span>
      {hint && <span className="text-[10px]" style={{ color: "var(--muted)" }}>{hint}</span>}
    </div>
  );
}

/** Tab-pill toggle — used for Instrument and Right. Inactive is flat
 *  ghost; active gets a subtle filled background + crisp text. */
function PillTab({
  active, onClick, children,
}: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex-1 inline-flex items-center justify-center h-full px-2 text-sm font-medium rounded-md transition-all"
      style={{
        background: active ? "var(--panel-2)" : "transparent",
        color: active ? "var(--text)" : "var(--muted)",
        boxShadow: "none",
      }}
    >
      {children}
    </button>
  );
}

/** Watchlist placeholder card — reserves the right-side column until
 *  the live-quotes module ships. Uses the same glass surface as the
 *  ticket so the two read as a paired layout. */
function WatchlistPlaceholder() {
  return (
    <div
      className="overflow-hidden flex flex-col h-full"
      style={cardStyle}
    >
      <div
        className="flex items-center justify-between px-4 py-3"
        style={{ borderBottom: "1px solid var(--border)" }}
      >
        <div className="flex items-center gap-2">
          <span
            className="w-1.5 h-1.5 rounded-full"
            style={{ background: "var(--accent-2)" }}
          />
          <span className="text-[10px] uppercase tracking-[0.2em] font-semibold" style={{ color: "var(--text-2)" }}>
            Watchlist
          </span>
        </div>
        <span className="text-[10px]" style={{ color: "var(--faint)" }}>
          coming soon
        </span>
      </div>

      <div className="flex-1 grid place-items-center p-6">
        <div className="text-center max-w-[280px]">
          <div
            className="mx-auto mb-3 inline-flex items-center justify-center w-12 h-12 rounded-full"
            style={{
              background: "linear-gradient(135deg, rgba(10,115,168,0.25), rgba(10,115,168,0.05))",
              border: "1px solid var(--border)",
            }}
          >
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color: "var(--accent-2)" }}>
              <path d="M3 3v18h18" />
              <path d="M7 14l4-4 4 4 5-5" />
            </svg>
          </div>
          <div className="text-sm font-medium mb-1.5" style={{ color: "var(--text)" }}>
            Live quotes panel
          </div>
          <div className="text-[11px] leading-relaxed" style={{ color: "var(--muted)" }}>
            Pin symbols here for streaming prices, intraday charts, and one-click trade entry.
          </div>
        </div>
      </div>
    </div>
  );
}

// ── main ─────────────────────────────────────────────────────────────────────

export default function TradePanelPage() {
  const [accts, setAccts] = useState<BrokerAccount[]>([]);
  const [acctsLoading, setAcctsLoading] = useState(true);
  const [acctId, setAcctId] = useState<string>("");
  const [instrument, setInstrument] = useState<InstrumentType>("option");
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<OrderSide>("buy");
  const [qty, setQty] = useState("1");
  const [limit, setLimit] = useState("");
  const [expiry, setExpiry] = useState("");
  const [strike, setStrike] = useState("");
  const [right, setRight] = useState<OptionRight>("call");
  const [submitting, setSubmitting] = useState(false);
  // Tracks which CTA is mid-flight so only that button's spinner spins
  // (we have 4 CTAs now — side alone can't disambiguate Buy MKT vs Buy LMT).
  const [submittingType, setSubmittingType] = useState<OrderType | null>(null);
  // Synchronous in-flight mutex. We can't rely on the `submitting` state
  // alone because state updates only take effect after the next render
  // — if two clicks land in the same render cycle (rapid double-tap,
  // bubbling event, browser quirk), both pass the `disabled={submitting}`
  // gate and the API gets hit twice. A ref updates immediately, so the
  // second invocation bails out before sending a request.
  const submittingRef = useRef(false);

  // Bracket exit legs — entered as PERCENTAGES off the reference price.
  // Reference is the limit price for limit/stop_limit orders, or a small
  // user-supplied "ref price" input that appears for market orders. We
  // convert to absolute prices at submit time so the backend / adapter
  // contract stays unchanged (Alpaca's bracket wants absolute prices).
  const [stopLoss, setStopLoss] = useState("");     // percent, e.g. "5" = 5%
  const [takeProfit, setTakeProfit] = useState(""); // percent, e.g. "10" = 10%

  const positionsRef = useRef<OpenPositionsTableHandle>(null);

  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiriesLoading, setExpiriesLoading] = useState(false);
  const [expiriesErr, setExpiriesErr] = useState<string | null>(null);
  const [expiriesFor, setExpiriesFor] = useState<string>("");

  // Live option quote for the currently-selected contract. Populated by
  // an effect that fires when (symbol, expiry, strike, right) all hold a
  // value. `bid`/`ask` are nullable — illiquid contracts or out-of-RTH
  // sessions can return 0/None and we don't want to lie to the user.
  const [optionQuote, setOptionQuote] = useState<{ bid: number | null; ask: number | null; mid: number | null } | null>(null);
  const [quoteFor, setQuoteFor] = useState<string>("");

  const [strikes, setStrikes] = useState<number[]>([]);
  const [strikesLoading, setStrikesLoading] = useState(false);
  const [strikesErr, setStrikesErr] = useState<string | null>(null);
  const [strikesFor, setStrikesFor] = useState<string>("");

  useEffect(() => {
    setStrikes([]); setStrike(""); setStrikesFor(""); setStrikesErr(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol]);

  useEffect(() => {
    api<BrokerAccount[]>("/api/brokers")
      .then(a => {
        setAccts(a);
        if (a.length && !acctId) setAcctId(a[0].id);
      })
      .finally(() => setAcctsLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (instrument !== "option") return;
    const sym = symbol.trim().toUpperCase();
    if (!sym || !acctId) {
      setExpiries([]); setExpiriesErr(null); setExpiriesFor("");
      return;
    }
    const cacheKey = `${acctId}:${sym}`;
    if (cacheKey === expiriesFor) return;
    const t = setTimeout(async () => {
      setExpiriesLoading(true); setExpiriesErr(null);
      try {
        const res = await api<{ symbol: string; expiries: string[] }>(
          `/api/options/expiries?account_id=${acctId}&symbol=${encodeURIComponent(sym)}`
        );
        setExpiries(res.expiries);
        setExpiriesFor(cacheKey);
        if (res.expiries.length === 0) setExpiry("");
        else if (!expiry || !res.expiries.includes(expiry)) setExpiry(res.expiries[0]);
      } catch (e) {
        setExpiries([]);
        setExpiriesErr(e instanceof ApiError ? String(e.detail) : "could not load expiries");
        setExpiriesFor(cacheKey);
        setExpiry("");
      } finally {
        setExpiriesLoading(false);
      }
    }, 500);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instrument, symbol, acctId]);

  useEffect(() => {
    if (instrument !== "option") return;
    const sym = symbol.trim().toUpperCase();
    if (!sym || !acctId || !expiry || !right) {
      setStrikes([]); setStrikesErr(null); setStrikesFor("");
      return;
    }
    const cacheKey = `${acctId}:${sym}:${expiry}:${right}`;
    if (cacheKey === strikesFor) return;
    const t = setTimeout(async () => {
      setStrikesLoading(true); setStrikesErr(null);
      try {
        const res = await api<{ symbol: string; expiry: string; right: string; strikes: number[]; underlying_price: number | null }>(
          `/api/options/strikes?account_id=${acctId}&symbol=${encodeURIComponent(sym)}&expiry=${expiry}&right=${right}`
        );
        setStrikes(res.strikes);
        setStrikesFor(cacheKey);
        // Pick the FIRST OTM (out-of-the-money) strike relative to the
        // underlying. Matches what most trading apps default to:
        //   call → smallest strike >= underlying_price (just above)
        //   put  → largest  strike <= underlying_price (just below)
        // Example: TSLA at 420.26, calls → 422.50 (not 420, which would
        // be slightly ITM). Falls back to absolute-nearest if no strike
        // sits on the OTM side, and to the chain median if there's no
        // underlying_price at all.
        if (res.strikes.length === 0) {
          setStrike("");
        } else if (res.underlying_price && res.underlying_price > 0) {
          const target = res.underlying_price;
          const sorted = [...res.strikes].sort((a, b) => a - b);
          let pick: number | undefined;
          if (right === "call") {
            pick = sorted.find(s => s >= target);
          } else {
            // For puts walk from the top down so we land on the largest
            // strike that's still <= underlying.
            for (let i = sorted.length - 1; i >= 0; i--) {
              if (sorted[i] <= target) { pick = sorted[i]; break; }
            }
          }
          // No OTM strike on this side of the chain — fall back to the
          // absolute-nearest strike so we still pick something sensible.
          if (pick === undefined) {
            pick = sorted.reduce((best, s) =>
              Math.abs(s - target) < Math.abs(best - target) ? s : best
            );
          }
          setStrike(String(pick));
        } else {
          setStrike(String(res.strikes[Math.floor(res.strikes.length / 2)]));
        }
      } catch (e) {
        setStrikes([]);
        setStrikesErr(e instanceof ApiError ? String(e.detail) : "could not load strikes");
        setStrikesFor(cacheKey);
        setStrike("");
      } finally {
        setStrikesLoading(false);
      }
    }, 500);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instrument, symbol, acctId, expiry, right]);

  // Fetch the live option quote (bid/ask) when the contract is fully
  // specified. Debounced 400ms so the user can quickly switch between
  // strikes without triggering a request for each intermediate state.
  // On contract change we also seed the Limit field with the side-natural
  // execution price — ASK for buy (cross the spread, fill instantly),
  // BID for sell (hit the bid). Falls back to MID, then the opposite leg
  // when one side of the quote is missing (illiquid contracts).
  // `side` is intentionally NOT in the deps below — we only re-seed when
  // the contract changes; toggling Buy/Sell after the fact must not
  // overwrite a user-edited price.
  useEffect(() => {
    if (instrument !== "option") {
      setOptionQuote(null);
      setQuoteFor("");
      return;
    }
    const sym = symbol.trim().toUpperCase();
    if (!sym || !acctId || !expiry || !strike || !right) {
      setOptionQuote(null);
      setQuoteFor("");
      return;
    }
    const cacheKey = `${acctId}:${sym}:${expiry}:${strike}:${right}`;
    if (cacheKey === quoteFor) return;
    const t = setTimeout(async () => {
      try {
        const res = await api<{ bid: number | null; ask: number | null; mid: number | null }>(
          `/api/options/quote?account_id=${acctId}&symbol=${encodeURIComponent(sym)}&expiry=${expiry}&strike=${strike}&right=${right}`
        );
        setOptionQuote({ bid: res.bid, ask: res.ask, mid: res.mid });
        setQuoteFor(cacheKey);
        // Default the Limit field to the side-natural execution price:
        // buy → ASK, sell → BID. Falls back through MID then the
        // opposite leg when one side of the quote is missing. We always
        // overwrite — a limit price from a different contract would be
        // misleading.
        const seed = side === "buy"
          ? (res.ask ?? res.mid ?? res.bid)
          : (res.bid ?? res.mid ?? res.ask);
        if (seed && seed > 0) {
          setLimit(seed.toFixed(2));
        }
      } catch {
        setOptionQuote(null);
        setQuoteFor(cacheKey);
      }
    }, 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instrument, symbol, acctId, expiry, strike, right]);


  const isOption = instrument === "option";
  const isBuy = side === "buy";

  const occ = useMemo(
    () => isOption ? buildOccSymbol(symbol, expiry, strike, right) : null,
    [isOption, symbol, expiry, strike, right]
  );

  // Reference price for converting TP/SL percentages → absolute prices.
  // We use the order's limit price as the implicit reference. Market
  // orders have no upfront price → an inline hint asks the trader to
  // switch to limit if they want %-based SL/TP.
  const refPrice = useMemo(() => {
    const n = limit ? Number(limit) : NaN;
    return Number.isFinite(n) && n > 0 ? n : null;
  }, [limit]);

  /** Apply a positive percentage to the reference price on the trader's
   *  side. Buy: TP is above entry, SL is below. Sell: reversed. */
  function pctToPrice(pct: string, leg: "tp" | "sl"): number | null {
    if (!refPrice) return null;
    const p = Number(pct);
    if (!Number.isFinite(p) || p <= 0) return null;
    const sign = (isBuy && leg === "tp") || (!isBuy && leg === "sl") ? 1 : -1;
    return refPrice * (1 + (sign * p) / 100);
  }

  const tpAbsPrice = useMemo(() => pctToPrice(takeProfit, "tp"),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [refPrice, takeProfit, isBuy]);
  const slAbsPrice = useMemo(() => pctToPrice(stopLoss, "sl"),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [refPrice, stopLoss, isBuy]);

  const bracketGeometryError = useMemo(() => {
    const sl = stopLoss ? Number(stopLoss) : null;
    const tp = takeProfit ? Number(takeProfit) : null;
    if (!sl && !tp) return null;
    // Percentages must be positive (direction is implied by side).
    if (sl !== null && sl <= 0) return "Stop loss % must be > 0";
    if (tp !== null && tp <= 0) return "Take profit % must be > 0";
    // Sanity: a 100%+ stop loss on a long would push the SL price to ≤0.
    if (isBuy && sl !== null && sl >= 100) return "Stop loss % must be < 100";
    if (!isBuy && tp !== null && tp >= 100) return "Take profit % must be < 100";
    // Need a reference price to actually convert to absolute.
    if ((sl || tp) && !refPrice) {
      return "Enter a limit price for % SL/TP";
    }
    return null;
  }, [stopLoss, takeProfit, isBuy, refPrice]);

  // Estimated cost for the live preview footer — uses the Limit price as
  // the reference. With no limit filled in we just show "—" (market price
  // is unknowable at preview time).
  const estCost = useMemo(() => {
    const q = Number(qty);
    if (!Number.isFinite(q) || q <= 0) return null;
    const px = Number(limit);
    if (!Number.isFinite(px) || px <= 0) return null;
    return q * px * (isOption ? 100 : 1);
  }, [qty, limit, isOption]);

  /** Build the bracket TP/SL absolute prices for a specific side. Used
   *  inside placeOrder so the actual prices match the button the user
   *  clicked, even though the preview state defaults to "buy". Returns
   *  null for legs that don't have a usable percentage or ref price. */
  function bracketFor(forSide: OrderSide): { tp: number | null; sl: number | null } {
    if (!refPrice) return { tp: null, sl: null };
    const buy = forSide === "buy";
    const tpPct = Number(takeProfit);
    const slPct = Number(stopLoss);
    const tp = takeProfit && Number.isFinite(tpPct) && tpPct > 0
      ? refPrice * (1 + (buy ? 1 : -1) * tpPct / 100)
      : null;
    const sl = stopLoss && Number.isFinite(slPct) && slPct > 0
      ? refPrice * (1 + (buy ? -1 : 1) * slPct / 100)
      : null;
    return { tp, sl };
  }

  async function placeOrder(forSide: OrderSide, forType: OrderType) {
    // Synchronous in-flight guard. Fires BEFORE the state-based
    // disabled prop on the button can save us — protects against
    // rapid double-clicks, form-submit-plus-button races, and any
    // browser event quirk that could call this twice in the same tick.
    if (submittingRef.current) return;
    submittingRef.current = true;
    if (!acctId) { notify.warn("Connect a broker first"); submittingRef.current = false; return; }
    if (!symbol.trim()) { notify.warn("Enter a symbol"); submittingRef.current = false; return; }
    if (!qty || Number(qty) <= 0) { notify.warn("Enter a quantity"); submittingRef.current = false; return; }
    if (forType === "limit" && (!limit || Number(limit) <= 0)) {
      notify.warn("Enter a limit price"); submittingRef.current = false; return;
    }
    // Bracket only applies to limit orders (market has no entry-price anchor
    // to convert the % against). Skip the geometry check for market clicks.
    if (forType === "limit" && bracketGeometryError) {
      notify.warn(bracketGeometryError); submittingRef.current = false; return;
    }

    setSide(forSide);
    setSubmittingType(forType);
    setSubmitting(true);
    try {
      const body: Record<string, unknown> = {
        instrument_type: instrument,
        symbol: symbol.toUpperCase(),
        side: forSide,
        order_type: forType,
        quantity: qty,
      };
      if (forType === "limit") body.limit_price = limit;
      // Convert percentage TP/SL → absolute prices for the broker. Only
      // attached for limit clicks (market has no reference anchor). We
      // recompute here for the *clicked* side (not the previewed side)
      // and round to 4 decimals to match the orders.Numeric(18,4) column.
      if (forType === "limit") {
        const { tp, sl } = bracketFor(forSide);
        if (tp !== null) body.take_profit_price = tp.toFixed(4);
        if (sl !== null) body.stop_loss_price = sl.toFixed(4);
      }
      if (isOption) {
        if (!expiry || !strike) {
          notify.warn("Option requires expiry and strike");
          setSubmitting(false); setSubmittingType(null); submittingRef.current = false; return;
        }
        body.option_expiry = expiry;
        body.option_strike = strike;
        body.option_right = right;
      }
      const res = await api<Order>(`/api/trades?broker_account_id=${acctId}`, {
        method: "POST", body: JSON.stringify(body),
      });
      const hasBracket = forType === "limit" && (body.take_profit_price || body.stop_loss_price);
      const tag = hasBracket ? " · bracket" : "";
      notify.success(`${forSide.toUpperCase()} ${qty} ${symbol.toUpperCase()} (${forType})${tag} — ${res.status}`);
      positionsRef.current?.refresh();
    } catch (e) {
      notify.fromError(e, "Order placement failed");
    } finally {
      setSubmitting(false);
      setSubmittingType(null);
      submittingRef.current = false;
    }
  }

  function submit(e: FormEvent) {
    // Each CTA explicitly chooses (side, type), so Enter has no implicit
    // action — preventDefault stops the browser from doing anything weird.
    e.preventDefault();
  }

  if (acctsLoading) return <PageLoading />;

  return (
    <div className="space-y-4 max-w-[1400px] mx-auto">
      {/* ── Top row: ticket + watchlist placeholder, side-by-side ─────── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 items-stretch">

        {/* ────────────── TRADE TICKET ────────────── */}
        <form
          onSubmit={submit}
          className="overflow-hidden flex flex-col"
          style={ticketStyle}
        >
          {/* Header — two matching pill toggles. Both use the same
              rounded-full track + identical inner-button geometry so they
              read as a balanced pair. Options/Stocks is the neutral one
              (subtle white-tint active state); Buy/Sell is the loud one
              (colored gradient active state). */}
          <div
            className="px-4 py-3"
            style={{ borderBottom: "1px solid var(--border)" }}
          >
            {/* Instrument switch — Options / Stocks. Full-width segmented
                control with `rounded-lg` to match the radius of the Buy /
                Sell CTA buttons at the footer. Each button flexes equally
                to fill half the row. */}
            <div
              className="flex w-full p-0.5 rounded-lg"
              style={{ background: "var(--bg-tint)", border: "1px solid var(--border)" }}
            >
              {(["option", "stock"] as const).map(kind => {
                const active = instrument === kind;
                return (
                  <button
                    key={kind}
                    type="button"
                    onClick={() => setInstrument(kind)}
                    className="flex-1 px-3.5 py-2 text-[11px] font-bold uppercase tracking-wider rounded-md transition-all"
                    style={
                      active
                        ? {
                            background: "var(--panel-2)",
                            color: "var(--text)",
                          }
                        : { color: "var(--muted)" }
                    }
                  >
                    {kind === "option" ? "Options" : "Stocks"}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Body — all input rows */}
          <div className="p-4 space-y-3.5">
            {/* Symbol — the visual focal point. Large, bold, monospace. */}
            <div>
              <TinyLabel>
                Symbol
              </TinyLabel>
              <input
                className="w-full px-3 text-[17px] font-bold tracking-tight uppercase outline-none transition-colors"
                style={{
                  ...inputStyle,
                  fontFamily: "ui-monospace, 'SF Mono', Menlo, monospace",
                  height: 42,
                }}
                placeholder="AAPL"
                value={symbol}
                onChange={e => setSymbol(e.target.value)}
                required
              />
              <div className="flex flex-wrap gap-1.5 mt-2">
                {POPULAR_SYMBOLS.map(tk => {
                  const selected = symbol.trim().toUpperCase() === tk;
                  return (
                    <button
                      key={tk}
                      type="button"
                      onClick={() => setSymbol(tk)}
                      className="px-[9.2px] py-[2.3px] text-[11.5px] font-medium rounded-md transition-all"
                      style={{
                        border: `1px solid ${selected ? "rgba(10,115,168,0.45)" : "var(--border)"}`,
                        background: selected
                          ? "linear-gradient(180deg, rgba(10,115,168,0.25), rgba(10,115,168,0.1))"
                          : "transparent",
                        color: selected ? "var(--text)" : "var(--muted)",
                      }}
                    >
                      {tk}
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Field rows — layout depends on instrument.
                Options: row A is [Qty | Expiry | Strike] · row B is [Right | Order type].
                Stocks:  single row [Qty | Order type] (no contract fields).
                All inputs share the same height (px-2.5 py-1.5 text-sm) so
                the rows read as a clean grid. The Right toggle's inner pill
                buttons use the same vertical padding to line up with the
                neighboring select / input boxes. */}
            {isOption ? (
              <>
                {/* Row A: Qty · Expiry · Strike — grouped inside a tinted
                    dashed wrapper to visually separate the "contract" inputs
                    from the rest of the form (same treatment we had before
                    the layout flatten). Inner inputs still pin at 34px so
                    the row reads as a single aligned strip. */}
                <div
                  className="p-2.5 rounded-lg"
                  style={{
                    background: "linear-gradient(180deg, rgba(10,115,168,0.06), rgba(10,115,168,0.02))",
                    border: "1px dashed rgba(10,115,168,0.25)",
                  }}
                >
                <div className="grid grid-cols-3 gap-2">
                  <div>
                    <TinyLabel>Qty</TinyLabel>
                    <input
                      type="number" step="1" min="1"
                      className="w-full px-2.5 text-sm tabular-nums outline-none"
                      style={{ ...inputStyle, height: 34 }}
                      value={qty}
                      onChange={e => setQty(e.target.value)}
                      required
                    />
                  </div>
                  <div>
                    <TinyLabel hint={expiriesLoading ? "…" : (expiries.length ? `${expiries.length}` : undefined)}>
                      Expiry
                    </TinyLabel>
                    {expiriesErr ? (
                      <input
                        type="date"
                        className="w-full px-2.5 text-sm outline-none"
                        style={{ ...inputStyle, height: 34 }}
                        value={expiry}
                        onChange={e => setExpiry(e.target.value)}
                        required
                      />
                    ) : (
                      <SearchableSelect
                        value={expiry}
                        options={expiries.map(e => ({ value: e, label: fmtDate(e) }))}
                        onChange={setExpiry}
                        loading={expiriesLoading}
                        disabled={expiriesLoading || expiries.length === 0}
                        placeholder={!symbol ? "—" : expiries.length === 0 ? "none" : "Select"}
                        style={{ height: 34 }}
                      />
                    )}
                  </div>
                  <div>
                    <TinyLabel>Strike</TinyLabel>
                    {strikesErr ? (
                      <input
                        type="number" step="0.01" min="0.01"
                        className="w-full px-2.5 text-sm tabular-nums outline-none"
                        style={{ ...inputStyle, height: 34 }}
                        placeholder="200"
                        value={strike}
                        onChange={e => setStrike(e.target.value)}
                        required
                      />
                    ) : (
                      <SearchableSelect
                        value={strike}
                        options={strikes.map(s => ({
                          value: String(s),
                          label: s.toLocaleString(undefined, { minimumFractionDigits: 2 }),
                        }))}
                        onChange={setStrike}
                        loading={strikesLoading}
                        disabled={strikesLoading || strikes.length === 0}
                        placeholder={!expiry ? "—" : strikes.length === 0 ? "none" : "Select"}
                        style={{ height: 34 }}
                      />
                    )}
                  </div>
                </div>
                </div>

                {/* Row B: Right toggle — full-width segmented pill now
                    that the Order-type select is gone (the 4 CTA buttons
                    encode order type instead). Same 34px height. */}
                <div>
                  <TinyLabel>Right</TinyLabel>
                  <div
                    className="flex p-0.5"
                    style={{
                      background: "var(--bg-tint)",
                      border: "1px solid var(--border)",
                      borderRadius: 8,
                      height: 34,
                    }}
                  >
                    <PillTab active={right === "call"} onClick={() => setRight("call")}>Call</PillTab>
                    <PillTab active={right === "put"} onClick={() => setRight("put")}>Put</PillTab>
                  </div>
                </div>
              </>
            ) : (
              // Stocks: just Qty (Order type is gone — the CTAs encode it).
              <div>
                <TinyLabel>Qty</TinyLabel>
                <input
                  type="number" step="1" min="1"
                  className="w-full px-2.5 text-sm tabular-nums outline-none"
                  style={{ ...inputStyle, height: 34 }}
                  value={qty}
                  onChange={e => setQty(e.target.value)}
                  required
                />
              </div>
            )}

            {/* Limit price — always visible. Used only when the user clicks
                a "Buy LMT" / "Sell LMT" CTA; ignored on Market clicks. We
                split the row 50/50: input on the left, three Bid / Mid / Ask
                pills on the right (options only). Each pill is a button that
                seeds the limit with that price; values are plain numbers
                (no $ or currency code) so they read at a glance and don't
                crowd the 50% column. Full width when no quote exists. */}
            {(() => {
              const hasQuote =
                isOption && optionQuote &&
                (optionQuote.bid !== null || optionQuote.ask !== null || optionQuote.mid !== null);
              const fmtPx = (n: number) => n.toFixed(2);
              return (
                <div
                  className={
                    hasQuote ? "grid grid-cols-2 gap-2" : "grid grid-cols-1 gap-2"
                  }
                >
                  <div>
                    <TinyLabel>Limit price</TinyLabel>
                    <input
                      type="number" step="0.01" min="0.01"
                      className="w-full px-2.5 py-1.5 text-sm tabular-nums outline-none"
                      style={inputStyle}
                      placeholder="200.00"
                      value={limit}
                      onChange={e => setLimit(e.target.value)}
                    />
                  </div>

                  {hasQuote && (
                    <div>
                      <TinyLabel>Quote</TinyLabel>
                      {/* Three pills side-by-side, equal width, height
                          matches the limit input (34px) so the row reads
                          as one aligned strip. Each pill is a button —
                          click seeds the limit with that price. */}
                      <div className="flex gap-1.5" style={{ height: 34 }}>
                        {(["bid", "mid", "ask"] as const).map(side => {
                          const val =
                            side === "bid" ? optionQuote!.bid :
                            side === "mid" ? optionQuote!.mid :
                            optionQuote!.ask;
                          const color =
                            side === "bid" ? "var(--bad)" :
                            side === "mid" ? "var(--text)" :
                            "var(--good)";
                          const disabled = val === null;
                          return (
                            <button
                              key={side}
                              type="button"
                              disabled={disabled}
                              onClick={() => !disabled && setLimit(fmtPx(val!))}
                              className="flex-1 flex flex-col items-center justify-center rounded-md tabular-nums transition-colors hover:bg-[var(--panel-2)] disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                              style={inputStyle}
                              title={disabled ? "no quote" : `Use ${side} as limit`}
                            >
                              <span
                                className="text-[8px] uppercase tracking-[0.15em] leading-none"
                                style={{ color: "var(--muted)" }}
                              >
                                {side}
                              </span>
                              <span
                                className="text-[12px] font-semibold leading-tight mt-0.5"
                                style={{ color }}
                              >
                                {val !== null ? fmtPx(val) : "—"}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
              );
            })()}

            {/* Bracket — PERCENTAGE inputs, side-by-side. Anchored on the
                Limit price, so they're only applied to "Buy LMT" / "Sell LMT"
                clicks. Filling them with no limit price triggers the geometry
                error below at submit time. */}
            <div>
                <div className="grid grid-cols-2 gap-2">
                  {/* Take profit % — single flat input. The "%" lives in the
                      label only; no inline suffix, no wrapper div. */}
                  <div>
                    <TinyLabel>Take profit %</TinyLabel>
                    <input
                      type="number" step="0.01" min="0.01"
                      className="w-full px-2.5 py-1.5 text-sm tabular-nums outline-none"
                      style={{
                        ...inputStyle,
                        borderColor: takeProfit
                          ? "rgba(34,197,94,0.45)"
                          : "var(--border)",
                      }}
                      placeholder="10"
                      value={takeProfit}
                      onChange={e => setTakeProfit(e.target.value)}
                    />
                    {tpAbsPrice !== null && (
                      <div className="mt-1 text-[10px] tabular-nums" style={{ color: "var(--muted)" }}>
                        ≈ <span style={{ color: "var(--good)" }}>{fmtMoney(tpAbsPrice)}</span>
                      </div>
                    )}
                  </div>

                  {/* Stop loss % — same shape, red accent when filled. */}
                  <div>
                    <TinyLabel>Stop loss %</TinyLabel>
                    <input
                      type="number" step="0.01" min="0.01"
                      className="w-full px-2.5 py-1.5 text-sm tabular-nums outline-none"
                      style={{
                        ...inputStyle,
                        borderColor: stopLoss
                          ? "rgba(239,68,68,0.45)"
                          : "var(--border)",
                      }}
                      placeholder="5"
                      value={stopLoss}
                      onChange={e => setStopLoss(e.target.value)}
                    />
                    {slAbsPrice !== null && (
                      <div className="mt-1 text-[10px] tabular-nums" style={{ color: "var(--muted)" }}>
                        ≈ <span style={{ color: "var(--bad)" }}>{fmtMoney(slAbsPrice)}</span>
                      </div>
                    )}
                  </div>

                  {bracketGeometryError && (
                    <div className="col-span-2 text-[10px]" style={{ color: "var(--warn)" }}>
                      {bracketGeometryError}
                    </div>
                  )}
                </div>
              </div>
          </div>

          {/* Footer — FOUR CTAs in a single row. Each button picks both
              side AND order type at the moment of click, so the trader
              never has to touch an Order-type dropdown first. Layout is
              [Buy MKT | Sell MKT | Buy LMT | Sell LMT]; the Market pair is
              outlined (secondary look — fires instantly, no price needed),
              the Limit pair is filled gradient (primary — uses the Limit
              price + bracket TP/SL). The bracket TP/SL prices are recomputed
              per-side at click time inside placeOrder, so the correct
              geometry goes to the broker regardless of which side the
              preview was rendered for. Spinner only spins on the specific
              button that's mid-flight. */}
          <div
            className="px-4 py-3 space-y-2"
            style={{ borderTop: "1px solid var(--border)", background: "var(--panel-2)" }}
          >
            <div className="grid grid-cols-4 gap-2">
              {([
                // Order: Market pair on the left, Limit pair on the right.
                // Within each pair: Buy then Sell. Greens for buy, reds for
                // sell — same hues as qa's old single Buy / Sell CTAs.
                // Market = outlined (secondary look) — fires instantly.
                // Limit  = filled gradient (primary look) — uses the Limit
                // price + bracket TP/SL.
                { side: "buy",  type: "market", primary: "Buy",  sub: "Market",
                  variant: "outline",
                  border: "rgba(34,197,94,0.55)",
                  tint:   "rgba(34,197,94,0.08)",
                  grad:   "",
                  text:   "#4ade80" },
                { side: "sell", type: "market", primary: "Sell", sub: "Market",
                  variant: "outline",
                  border: "rgba(239,68,68,0.55)",
                  tint:   "rgba(239,68,68,0.08)",
                  grad:   "",
                  text:   "#f87171" },
                { side: "buy",  type: "limit",  primary: "Buy",  sub: "Limit",
                  variant: "filled",
                  border: "",
                  tint:   "",
                  grad: "linear-gradient(135deg, #2dd66b 0%, #16a34a 50%, #15803d 100%)",
                  text: "#06210f" },
                { side: "sell", type: "limit",  primary: "Sell", sub: "Limit",
                  variant: "filled",
                  border: "",
                  tint:   "",
                  grad: "linear-gradient(135deg, #fb7474 0%, #dc2626 50%, #b91c1c 100%)",
                  text: "#1a0606" },
              ] as const).map(b => {
                const spinning = submitting && side === b.side && submittingType === b.type;
                const outlined = b.variant === "outline";
                return (
                  <button
                    key={`${b.side}-${b.type}`}
                    type="button"
                    onClick={() => placeOrder(b.side, b.type)}
                    disabled={submitting || !acctId}
                    className="w-full px-2 py-2.5 rounded-lg flex flex-col items-center justify-center gap-0.5 transition-all"
                    style={{
                      background: outlined ? b.tint : b.grad,
                      border: outlined ? `1px solid ${b.border}` : "1px solid transparent",
                      color: b.text,
                      opacity: submitting || !acctId ? 0.6 : 1,
                      cursor: submitting || !acctId ? "not-allowed" : "pointer",
                    }}
                  >
                    <span className="text-[13px] font-bold tracking-wide leading-none inline-flex items-center gap-1.5">
                      {spinning && <Spinner />}
                      {b.primary}
                    </span>
                    <span className="text-[10px] font-semibold uppercase tracking-[0.12em] leading-none opacity-80">
                      {b.sub}
                    </span>
                  </button>
                );
              })}
            </div>
            {/* Live preview footer — OCC symbol (options) + cost estimate. */}
            <div
              className="flex items-center justify-between text-[10px] tabular-nums"
              style={{ color: "var(--muted)" }}
            >
              <div className="truncate">
                {isOption
                  ? (occ ? <span className="font-mono">{occ}</span> : <span>fill expiry · strike · right</span>)
                  : <span>{symbol ? symbol.toUpperCase() : "—"} · stock</span>
                }
              </div>
              <div className="shrink-0">
                {estCost !== null
                  ? <span>≈ {fmtMoney(estCost)} <span style={{ color: "var(--faint)" }}>at limit</span></span>
                  : <span>—</span>}
              </div>
            </div>
          </div>
        </form>

        {/* ────────────── WATCHLIST PLACEHOLDER ────────────── */}
        <WatchlistPlaceholder />
      </div>

      {/* ── Divider, bulk-exit bar, then the open positions table ──────── */}
      <hr className="my-2" style={{ borderColor: "var(--border)" }} />
      <BulkExitBar onActionComplete={() => positionsRef.current?.refresh()} />
      <OpenPositionsTable ref={positionsRef} />
    </div>
  );
}
