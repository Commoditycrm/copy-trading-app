"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
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

const ORDER_TYPE_OPTIONS = [
  { value: "market", label: "Market" },
  { value: "limit", label: "Limit" },
  { value: "stop", label: "Stop" },
  { value: "stop_limit", label: "Stop-limit" },
];

// ── shared visual primitives ─────────────────────────────────────────────────

/** Tinted glass-card surface used by the ticket + watchlist panels.
 *  Subtle vertical gradient + 1px border + soft backdrop blur. */
const cardStyle: React.CSSProperties = {
  background:
    "linear-gradient(180deg, rgba(20,26,32,0.55) 0%, rgba(10,14,18,0.35) 100%)",
  border: "1px solid var(--border)",
  borderRadius: "var(--r)",
  backdropFilter: "blur(10px)",
  WebkitBackdropFilter: "blur(10px)",
};

/** Inset input style — slightly darker than the card surface so they
 *  read as wells the user types into. Smaller radius for a tighter look. */
const inputStyle: React.CSSProperties = {
  background: "rgba(7,10,14,0.7)",
  border: "1px solid var(--border)",
  borderRadius: 8,
};

// ── primitive components ─────────────────────────────────────────────────────

function TinyLabel({ children, hint }: { children: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between mb-1">
      <span className="text-[9px] uppercase tracking-[0.15em] font-semibold" style={{ color: "var(--muted)" }}>
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
        background: active ? "rgba(255,255,255,0.06)" : "transparent",
        color: active ? "var(--text)" : "var(--muted)",
        boxShadow: active ? "inset 0 1px 0 rgba(255,255,255,0.06)" : "none",
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
  const [orderType, setOrderType] = useState<OrderType>("market");
  const [qty, setQty] = useState("1");
  const [limit, setLimit] = useState("");
  const [stop, setStop] = useState("");
  const [expiry, setExpiry] = useState("");
  const [strike, setStrike] = useState("");
  const [right, setRight] = useState<OptionRight>("call");
  const [submitting, setSubmitting] = useState(false);

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
        // Pick the strike NEAREST to the underlying's current price (ATM).
        // The backend ships a `underlying_price` alongside the strikes; we
        // pick `argmin(|strike - underlying|)`. If the lookup failed (null)
        // we fall back to the chain median — a coarse ATM approximation,
        // but better than picking the first strike.
        if (res.strikes.length === 0) {
          setStrike("");
        } else if (res.underlying_price && res.underlying_price > 0) {
          const target = res.underlying_price;
          const nearest = res.strikes.reduce((best, s) =>
            Math.abs(s - target) < Math.abs(best - target) ? s : best
          );
          setStrike(String(nearest));
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


  const isOption = instrument === "option";
  const isBuy = side === "buy";

  const occ = useMemo(
    () => isOption ? buildOccSymbol(symbol, expiry, strike, right) : null,
    [isOption, symbol, expiry, strike, right]
  );

  const bracketCompatible = orderType === "market" || orderType === "limit";

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
    if (!bracketCompatible) return null;
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
  }, [bracketCompatible, stopLoss, takeProfit, isBuy, refPrice, orderType]);

  // Estimated cost for the live preview footer.
  const estCost = useMemo(() => {
    const q = Number(qty);
    if (!Number.isFinite(q) || q <= 0) return null;
    if (orderType === "market") return null;
    const px = Number(limit);
    if (!Number.isFinite(px) || px <= 0) return null;
    return q * px * (isOption ? 100 : 1);
  }, [orderType, qty, limit, isOption]);

  /** Build the bracket TP/SL absolute prices for a specific side. Used
   *  inside placeOrder so the actual prices match the button the user
   *  clicked, even though the preview state defaults to "buy". Returns
   *  null for legs that don't have a usable percentage or ref price. */
  function bracketFor(forSide: OrderSide): { tp: number | null; sl: number | null } {
    if (!bracketCompatible || !refPrice) return { tp: null, sl: null };
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

  async function placeOrder(forSide: OrderSide) {
    if (!acctId) { notify.warn("Connect a broker first"); return; }
    if (!symbol.trim()) { notify.warn("Enter a symbol"); return; }
    if (!qty || Number(qty) <= 0) { notify.warn("Enter a quantity"); return; }
    if (bracketGeometryError) { notify.warn(bracketGeometryError); return; }

    setSide(forSide);
    setSubmitting(true);
    try {
      const body: Record<string, unknown> = {
        instrument_type: instrument,
        symbol: symbol.toUpperCase(),
        side: forSide,
        order_type: orderType,
        quantity: qty,
      };
      if (orderType === "limit" || orderType === "stop_limit") body.limit_price = limit;
      if (orderType === "stop" || orderType === "stop_limit") body.stop_price = stop;
      // Convert percentage TP/SL → absolute prices for the broker. We
      // recompute here for the *clicked* side (not the previewed side)
      // and round to 4 decimals to match the orders.Numeric(18,4) column.
      const { tp, sl } = bracketFor(forSide);
      if (tp !== null) body.take_profit_price = tp.toFixed(4);
      if (sl !== null) body.stop_loss_price = sl.toFixed(4);
      if (isOption) {
        if (!expiry || !strike) { notify.warn("Option requires expiry and strike"); setSubmitting(false); return; }
        body.option_expiry = expiry;
        body.option_strike = strike;
        body.option_right = right;
      }
      const res = await api<Order>(`/api/trades?broker_account_id=${acctId}`, {
        method: "POST", body: JSON.stringify(body),
      });
      const tag = (tp !== null || sl !== null) ? " · bracket" : "";
      notify.success(`${forSide.toUpperCase()} ${qty} ${symbol.toUpperCase()} (${orderType.replace("_", "-")})${tag} — ${res.status}`);
      positionsRef.current?.refresh();
    } catch (e) {
      notify.fromError(e, "Order placement failed");
    } finally {
      setSubmitting(false);
    }
  }

  function submit(e: FormEvent) {
    // Enter-key submit defaults to the side currently highlighted in
    // the preview (initially "buy"). The two CTA buttons override.
    e.preventDefault();
    placeOrder(side);
  }

  if (acctsLoading) return <PageLoading />;

  // Order-type descriptor used inside both CTA button labels so the
  // trader sees the exact action: "Buy · Market", "Sell · Limit @ $200".
  const typeLabel = orderType === "market" ? "Market"
    : orderType === "limit" ? `Limit @ ${limit ? fmtMoney(Number(limit)) : "—"}`
    : orderType === "stop" ? `Stop @ ${stop ? fmtMoney(Number(stop)) : "—"}`
    : "Stop-Limit";

  return (
    <div className="space-y-4">
      {/* ── Top row: ticket + watchlist placeholder, side-by-side ─────── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 items-stretch">

        {/* ────────────── TRADE TICKET ────────────── */}
        <form
          onSubmit={submit}
          className="overflow-hidden flex flex-col"
          style={cardStyle}
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
              style={{ background: "rgba(0,0,0,0.35)", border: "1px solid var(--border)" }}
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
                            background: "rgba(255,255,255,0.08)",
                            color: "var(--text)",
                            boxShadow: "inset 0 1px 0 rgba(255,255,255,0.12)",
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

                {/* Row B: Right · Order type — same 34px height so the
                    segmented pill matches the select beside it. */}
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <TinyLabel>Right</TinyLabel>
                    <div
                      className="flex p-0.5"
                      style={{
                        background: "rgba(0,0,0,0.3)",
                        border: "1px solid var(--border)",
                        borderRadius: 8,
                        height: 34,
                      }}
                    >
                      <PillTab active={right === "call"} onClick={() => setRight("call")}>Call</PillTab>
                      <PillTab active={right === "put"} onClick={() => setRight("put")}>Put</PillTab>
                    </div>
                  </div>
                  <div>
                    <TinyLabel>Order type</TinyLabel>
                    <SearchableSelect
                      value={orderType}
                      options={ORDER_TYPE_OPTIONS}
                      onChange={v => setOrderType(v as OrderType)}
                      searchable={false}
                      style={{ height: 34 }}
                    />
                  </div>
                </div>
              </>
            ) : (
              // Stocks: just Qty + Order type. Same 34px row height.
              <div className="grid grid-cols-2 gap-2">
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
                  <TinyLabel>Order type</TinyLabel>
                  <SearchableSelect
                    value={orderType}
                    options={ORDER_TYPE_OPTIONS}
                    onChange={v => setOrderType(v as OrderType)}
                    searchable={false}
                    style={{ height: 34 }}
                  />
                </div>
              </div>
            )}

            {/* Price inputs — only render the ones the current order type
                needs, so the form doesn't grow when MARKET is selected. */}
            {(orderType === "limit" || orderType === "stop_limit" || orderType === "stop") && (
              <div className="grid grid-cols-2 gap-2">
                {(orderType === "limit" || orderType === "stop_limit") && (
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
                )}
                {(orderType === "stop" || orderType === "stop_limit") && (
                  <div>
                    <TinyLabel>Stop price</TinyLabel>
                    <input
                      type="number" step="0.01" min="0.01"
                      className="w-full px-2.5 py-1.5 text-sm tabular-nums outline-none"
                      style={inputStyle}
                      placeholder="195.00"
                      value={stop}
                      onChange={e => setStop(e.target.value)}
                    />
                  </div>
                )}
              </div>
            )}

            {/* Bracket — PERCENTAGE inputs, side-by-side. The number entered
                is interpreted as % away from the reference price (limit for
                limit/stop-limit; the small "Ref price" input below for
                market orders). Computed absolute price shows under each
                input so the trader sees what's going to the broker. */}
            <div>
              <div className="flex items-center justify-between mb-1">
                {!bracketCompatible && (
                  <span className="text-[10px]" style={{ color: "var(--muted)" }}>
                    market/limit only
                  </span>
                )}
              </div>
              <div className="grid grid-cols-2 gap-2">
                {/* Take profit % — single flat input. The "%" lives in the
                    label only; no inline suffix, no wrapper div. */}
                <div>
                  <TinyLabel>Take profit %</TinyLabel>
                  <input
                    type="number" step="0.01" min="0.01"
                    disabled={!bracketCompatible}
                    className="w-full px-2.5 py-1.5 text-sm tabular-nums outline-none disabled:opacity-50"
                    style={{
                      ...inputStyle,
                      borderColor: takeProfit && bracketCompatible
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
                    disabled={!bracketCompatible}
                    className="w-full px-2.5 py-1.5 text-sm tabular-nums outline-none disabled:opacity-50"
                    style={{
                      ...inputStyle,
                      borderColor: stopLoss && bracketCompatible
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

          {/* Footer — TWO CTAs (Buy + Sell) side-by-side, no shared toggle.
              Each button is its own commit point: click Buy → places buy
              order; click Sell → places sell. The bracket TP/SL prices are
              recomputed per-side at click time inside placeOrder, so the
              correct geometry goes to the broker regardless of which side
              the preview was rendered for. */}
          <div
            className="px-4 py-3 space-y-2"
            style={{ borderTop: "1px solid var(--border)", background: "rgba(0,0,0,0.2)" }}
          >
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => placeOrder("buy")}
                disabled={submitting || !acctId}
                className="w-full px-4 py-3 text-sm font-bold tracking-wide rounded-lg inline-flex items-center justify-center gap-2 transition-all"
                style={{
                  background: "linear-gradient(135deg, #2dd66b 0%, #16a34a 50%, #15803d 100%)",
                  color: "#06210f",
                  opacity: submitting || !acctId ? 0.6 : 1,
                  cursor: submitting || !acctId ? "not-allowed" : "pointer",
                }}
              >
                {submitting && side === "buy" && <Spinner />}
                <span>Buy · {typeLabel}</span>
              </button>
              <button
                type="button"
                onClick={() => placeOrder("sell")}
                disabled={submitting || !acctId}
                className="w-full px-4 py-3 text-sm font-bold tracking-wide rounded-lg inline-flex items-center justify-center gap-2 transition-all"
                style={{
                  background: "linear-gradient(135deg, #fb7474 0%, #dc2626 50%, #b91c1c 100%)",
                  color: "#1a0606",
                  opacity: submitting || !acctId ? 0.6 : 1,
                  cursor: submitting || !acctId ? "not-allowed" : "pointer",
                }}
              >
                {submitting && side === "sell" && <Spinner />}
                <span>Sell · {typeLabel}</span>
              </button>
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
                  ? <span>≈ {fmtMoney(estCost)}</span>
                  : orderType === "market"
                    ? <span>market price</span>
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
