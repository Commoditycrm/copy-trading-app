"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { OpenPositionsTable, type OpenPositionsTableHandle } from "@/components/OpenPositionsTable";
import { ConfirmModal } from "@/components/ConfirmModal";
import type { BrokerAccount, InstrumentType, Order, OrderSide, OrderType, OptionRight } from "@/lib/types";

function fmtNum(n: string | null | undefined, dp = 2): string {
  if (n === null || n === undefined || n === "") return "—";
  const v = Number(n);
  if (!Number.isFinite(v)) return String(n);
  return v.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

function fmtSignedMoney(n: string | null | undefined): { text: string; sign: 1 | -1 | 0 | null } {
  if (n === null || n === undefined || n === "") return { text: "—", sign: null };
  const v = Number(n);
  if (!Number.isFinite(v)) return { text: String(n), sign: null };
  return {
    text: v.toLocaleString(undefined, { style: "currency", currency: "USD" }),
    sign: v === 0 ? 0 : v > 0 ? 1 : -1,
  };
}

// ── small helpers ────────────────────────────────────────────────────────────

/** Build the standard OCC option symbol: ROOT + YYMMDD + C/P + strike*1000 (8 digits).
 *  Example: AAPL 2025-07-19 200 CALL → AAPL250719C00200000 */
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

// ── shared style helpers ─────────────────────────────────────────────────────

const sectionStyle: React.CSSProperties = {
  borderColor: "var(--border)",
  background: "var(--panel)",
};

const inputStyle: React.CSSProperties = {
  borderColor: "var(--border)",
  background: "var(--bg)",   // darker than the panel they sit on
};

function ChevronDown() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 20 20" aria-hidden>
      <path d="M0 0h20v20H0z" fill="none" />
      <path fill="currentColor" d="M10.103 12.778L16.81 6.08a.69.69 0 0 1 .99.012a.726.726 0 0 1-.012 1.012l-7.203 7.193a.69.69 0 0 1-.985-.006L2.205 6.72a.727.727 0 0 1 0-1.01a.69.69 0 0 1 .99 0z" />
    </svg>
  );
}

function ChevronUp() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 15 15" aria-hidden>
      <path d="M0 0h15v15H0z" fill="none" />
      <path fill="none" stroke="currentColor" strokeLinecap="square" d="m1 10l6.5-7l6.5 7" />
    </svg>
  );
}

function Label({ children, hint }: { children: React.ReactNode; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between mb-1">
      <label className="text-[11px] uppercase tracking-wider font-medium" style={{ color: "var(--muted)" }}>
        {children}
      </label>
      {hint && <span className="text-[10px]" style={{ color: "var(--muted)" }}>{hint}</span>}
    </div>
  );
}

function SegBtn({
  active, onClick, children, color,
}: {
  active: boolean; onClick: () => void; children: React.ReactNode;
  color?: "good" | "bad" | "accent";
}) {
  // Active state uses the matching gradient + a subtle inner highlight; inactive
  // is a quiet outlined chip. Border colour matches the gradient family for
  // a consistent edge.
  const grad =
    color === "bad" ? "var(--grad-bad)" :
      "var(--grad-accent)";   // good + accent both use lime gradient
  const edge =
    color === "bad" ? "rgba(255, 107, 107, 0.35)" :
      "rgba(10, 115, 168, 0.35)";
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex-1 px-3 py-2 rounded text-sm font-medium transition-all"
      style={{
        border: `1px solid ${active ? edge : "var(--border)"}`,
        background: active ? grad : "transparent",
        color: active ? "var(--accent-ink)" : "var(--text)",
        boxShadow: active
          ? "inset 0 1px 0 rgba(255,255,255,0.25), 0 6px 18px -8px " + (color === "bad" ? "rgba(255,107,107,0.35)" : "var(--accent-glow)")
          : "none",
      }}
    >
      {children}
    </button>
  );
}

// ── main ─────────────────────────────────────────────────────────────────────

export default function TradePanelPage() {
  const [accts, setAccts] = useState<BrokerAccount[]>([]);
  const [acctId, setAcctId] = useState<string>("");
  const [instrument, setInstrument] = useState<InstrumentType>("option");
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<OrderSide>("buy");
  // Default to "limit" so the Custom order section opens ready for a limit order
  // (the express BUY/SELL @ MARKET buttons override this anyway via placeOrder).
  const [orderType, setOrderType] = useState<OrderType>("limit");
  const [qty, setQty] = useState("1");
  const [limit, setLimit] = useState("");
  const [stop, setStop] = useState("");
  const [expiry, setExpiry] = useState("");
  const [strike, setStrike] = useState("");
  const [right, setRight] = useState<OptionRight>("call");
  const [submitting, setSubmitting] = useState(false);
  const [last, setLast] = useState<Order | null>(null);
  const [summaryOpen, setSummaryOpen] = useState(true);

  // Open positions table — owned by the shared component. We keep a ref so
  // we can ask it to refresh after a fresh order or exit-all.
  const positionsRef = useRef<OpenPositionsTableHandle>(null);
  const [exitBusy, setExitBusy] = useState(false);
  const [exitConfirmOpen, setExitConfirmOpen] = useState(false);

  // Expiries fetched per (symbol, account). Cached client-side
  // so retyping the same symbol doesn't trigger a re-fetch.
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiriesLoading, setExpiriesLoading] = useState(false);
  const [expiriesErr, setExpiriesErr] = useState<string | null>(null);
  const [expiriesFor, setExpiriesFor] = useState<string>("");  // "<acctId>:<SYMBOL>"

  useEffect(() => {
    api<BrokerAccount[]>("/api/brokers").then(a => {
      setAccts(a);
      if (a.length && !acctId) setAcctId(a[0].id);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fetch option expiries from SnapTrade when (symbol, account) change.
  // Debounced 500ms so typing "AAPL" doesn't fire 4 requests.
  useEffect(() => {
    if (instrument !== "option") return;
    const sym = symbol.trim().toUpperCase();
    if (!sym || !acctId) {
      setExpiries([]); setExpiriesErr(null); setExpiriesFor("");
      return;
    }
    const cacheKey = `${acctId}:${sym}`;
    if (cacheKey === expiriesFor) return;  // already fetched / fetching

    const t = setTimeout(async () => {
      setExpiriesLoading(true);
      setExpiriesErr(null);
      try {
        const res = await api<{ symbol: string; expiries: string[] }>(
          `/api/options/expiries?account_id=${acctId}&symbol=${encodeURIComponent(sym)}`
        );
        setExpiries(res.expiries);
        setExpiriesFor(cacheKey);
        // Auto-pick the soonest expiry: keep the user's existing choice if
        // it's still valid, otherwise default to the first (closest) date.
        if (res.expiries.length === 0) {
          setExpiry("");
        } else if (!expiry || !res.expiries.includes(expiry)) {
          setExpiry(res.expiries[0]);
        }
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

  const selectedAcct = useMemo(() => accts.find(a => a.id === acctId), [accts, acctId]);

  async function doExitAll() {
    setExitBusy(true);
    try {
      const res = await api<{ closed_count: number; failed_count: number; failed: { symbol: string | null; error: string }[] }>(
        "/api/positions/close-all", { method: "POST" }
      );
      if (res.closed_count === 0 && res.failed_count === 0) {
        notify.info("No open positions to close.");
      } else if (res.failed_count === 0) {
        notify.success(`Exited ${res.closed_count} position${res.closed_count === 1 ? "" : "s"} at market`);
      } else {
        notify.warn(`Exited ${res.closed_count}; ${res.failed_count} failed — check Trades for details`);
      }
      positionsRef.current?.refresh();
      setExitConfirmOpen(false);
    } catch (e) {
      notify.fromError(e, "Exit all failed");
    } finally {
      setExitBusy(false);
    }
  }

  // OCC symbol preview for the right-side summary panel.
  const occ = useMemo(
    () => instrument === "option" ? buildOccSymbol(symbol, expiry, strike, right) : null,
    [instrument, symbol, expiry, strike, right]
  );

  // Estimated cost for the summary panel. Options multiplier is 100 shares/contract.
  const estCost = useMemo(() => {
    const q = Number(qty);
    if (!Number.isFinite(q) || q <= 0) return null;
    if (instrument === "option") {
      if (orderType === "market") return null;     // unknown until fill
      const px = Number(limit);
      if (!Number.isFinite(px) || px <= 0) return null;
      return q * px * 100;
    }
    if (orderType === "market") return null;
    const px = Number(limit);
    if (!Number.isFinite(px) || px <= 0) return null;
    return q * px;
  }, [instrument, orderType, qty, limit]);

  /**
   * Single placement path used by:
   *   - Express "Buy/Sell at Market" buttons (overrideSide + overrideType="market")
   *   - Custom-order submit (no overrides — uses form state)
   */
  async function placeOrder(opts: {
    overrideSide?: OrderSide;
    overrideType?: OrderType;
  } = {}) {
    if (!acctId) { notify.warn("Connect a broker first"); return; }
    if (!symbol.trim()) { notify.warn("Enter a symbol"); return; }
    if (!qty || Number(qty) <= 0) { notify.warn("Enter a quantity"); return; }

    const useSide = opts.overrideSide ?? side;
    const useType = opts.overrideType ?? orderType;

    setSubmitting(true);
    try {
      const body: Record<string, unknown> = {
        instrument_type: instrument,
        symbol: symbol.toUpperCase(),
        side: useSide,
        order_type: useType,
        quantity: qty,
      };
      if (useType === "limit" || useType === "stop_limit") body.limit_price = limit;
      if (useType === "stop" || useType === "stop_limit") body.stop_price = stop;
      if (instrument === "option") {
        if (!expiry || !strike) {
          notify.warn("Option requires expiry and strike");
          setSubmitting(false);
          return;
        }
        body.option_expiry = expiry;
        body.option_strike = strike;
        body.option_right = right;
      }
      const res = await api<Order>(`/api/trades?broker_account_id=${acctId}`, {
        method: "POST", body: JSON.stringify(body),
      });
      setLast(res);
      notify.success(
        `${useSide.toUpperCase()} ${qty} ${symbol.toUpperCase()} (${useType.replace("_", "-")}) — ${res.status}`
      );
      // Refresh the embedded positions table so a new fill appears below.
      positionsRef.current?.refresh();
    } catch (e) {
      notify.fromError(e, "Order placement failed");
    } finally {
      setSubmitting(false);
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    placeOrder();   // uses current side + orderType state (custom path)
  }

  // The form is the same content in both stock and option mode — only the
  // wrapper layout changes (single-col vs two-col with summary on the right).
  const formBody = (
    <form onSubmit={submit} className="space-y-5 p-5 rounded border" style={sectionStyle}>

      {/* Account */}
      {/* <div>
        <Label>Broker account</Label>
        <select
          value={acctId} onChange={e => setAcctId(e.target.value)} required
          className="w-full p-2 rounded bg-transparent border" style={inputStyle}
        >
          {accts.length === 0 && <option value="">— connect a broker first —</option>}
          {accts.map(a => (
            <option key={a.id} value={a.id}>
              {a.broker} · {a.label}{a.is_paper ? " (paper)" : ""}
            </option>
          ))}
        </select>
        {selectedAcct && (
          <div className="mt-1 text-[11px]" style={{ color: "var(--muted)" }}>
            Buying power: {fmtMoney(selectedAcct.buying_power ? Number(selectedAcct.buying_power) : null)}
            {" · "}
            Cash: {fmtMoney(selectedAcct.cash ? Number(selectedAcct.cash) : null)}
          </div>
        )}
      </div> */}

      {/* Instrument toggle */}
      <div>
        <Label>Instrument</Label>
        <div className="flex gap-2">
          <SegBtn active={instrument === "option"} onClick={() => setInstrument("option")}>Options</SegBtn>
          <SegBtn active={instrument === "stock"} onClick={() => setInstrument("stock")}>Stocks</SegBtn>
        </div>
      </div>

      {/* Symbol + Quantity in one row — the two fields you always need */}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <Label>Symbol</Label>
          <input
            className="w-full p-2 rounded bg-transparent border uppercase tracking-wide font-medium" style={inputStyle}
            placeholder="AAPL" value={symbol}
            onChange={e => setSymbol(e.target.value)} required
          />
        </div>
        <div>
          <Label>Quantity</Label>
          <input
            type="number" step="1" min="1"
            className="w-full p-2 rounded bg-transparent border" style={inputStyle}
            placeholder="1" value={qty} onChange={e => setQty(e.target.value)} required
          />
        </div>
      </div>

      {/* Option contract fields */}
      {instrument === "option" && (
        <div className="space-y-3 p-3 rounded" style={{ background: "rgba(10,115,168,0.05)", border: "1px dashed var(--border)" }}>
          <div className="text-[11px] uppercase tracking-wider font-medium" style={{ color: "var(--muted)" }}>
            Contract
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <Label hint={expiriesLoading ? "loading…" : (expiries.length ? `${expiries.length} available` : undefined)}>
                Expiry
              </Label>
              {expiriesErr ? (
                <>
                  <input
                    type="date" className="w-full p-2 rounded bg-transparent border" style={inputStyle}
                    value={expiry} onChange={e => setExpiry(e.target.value)} required
                  />
                  <div className="text-[10px] mt-1" style={{ color: "var(--bad)" }}>
                    {expiriesErr} — pick a date manually
                  </div>
                </>
              ) : (
                <select
                  className="w-full p-2 rounded bg-transparent border" style={inputStyle}
                  value={expiry} onChange={e => setExpiry(e.target.value)}
                  required disabled={expiriesLoading || expiries.length === 0}
                >
                  <option value="">
                    {expiriesLoading
                      ? "loading…"
                      : !symbol
                        ? "Enter symbol first"
                        : expiries.length === 0
                          ? "no expiries"
                          : "— select —"}
                  </option>
                  {expiries.map(e => (
                    <option key={e} value={e}>{fmtDate(e)}</option>
                  ))}
                </select>
              )}
            </div>
            <div>
              <Label>Strike</Label>
              <input
                type="number" step="0.01" min="0.01"
                className="w-full p-2 rounded bg-transparent border" style={inputStyle}
                placeholder="200" value={strike} onChange={e => setStrike(e.target.value)} required
              />
            </div>
            <div>
              <Label>Right</Label>
              <div className="flex gap-2">
                <SegBtn active={right === "call"} onClick={() => setRight("call")}>Call</SegBtn>
                <SegBtn active={right === "put"} onClick={() => setRight("put")}>Put</SegBtn>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Express market buttons — primary path: 1 click after Symbol+Qty */}
      <div className="grid grid-cols-2 gap-3">
        <button
          type="button"
          disabled={submitting || !acctId}
          onClick={() => placeOrder({ overrideSide: "buy", overrideType: "market" })}
          className="btn-primary w-full p-2.5 text-sm inline-flex items-center justify-center gap-2"
        >
          <span>BUY at MARKET</span>
          {submitting && <Spinner />}
        </button>
        <button
          type="button"
          disabled={submitting || !acctId}
          onClick={() => placeOrder({ overrideSide: "sell", overrideType: "market" })}
          className="btn-danger w-full p-2.5 text-sm inline-flex items-center justify-center gap-2"
        >
          <span>SELL at MARKET</span>
          {submitting && <Spinner />}
        </button>
      </div>

      {/* ── Custom order (limit / stop / stop-limit) ───────────────────── */}
      <div className="pt-4 border-t" style={{ borderColor: "var(--border)" }}>
        <div className="text-xs uppercase tracking-wider mb-3" style={{ color: "var(--muted)" }}>
          Custom order
        </div>

        <div className="p-4 rounded space-y-4" style={{ background: "rgba(255,255,255,0.02)", border: "1px solid var(--border)" }}>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Side</Label>
              <div className="flex gap-2">
                <SegBtn color="good" active={side === "buy"} onClick={() => setSide("buy")}>Buy</SegBtn>
                <SegBtn color="bad" active={side === "sell"} onClick={() => setSide("sell")}>Sell</SegBtn>
              </div>
            </div>
            <div>
              <Label>Order type</Label>
              <select
                value={orderType} onChange={e => setOrderType(e.target.value as OrderType)}
                className="w-full p-2 rounded bg-transparent border" style={inputStyle}
              >
                <option value="market">Market</option>
                <option value="limit">Limit</option>
                <option value="stop">Stop</option>
                <option value="stop_limit">Stop-limit</option>
              </select>
            </div>
          </div>

          {(orderType === "limit" || orderType === "stop_limit") && (
            <div>
              <Label>Limit price</Label>
              <input
                type="number" step="0.01" min="0.01"
                className="w-full p-2 rounded bg-transparent border" style={inputStyle}
                placeholder="200.00" value={limit} onChange={e => setLimit(e.target.value)} required
              />
            </div>
          )}
          {(orderType === "stop" || orderType === "stop_limit") && (
            <div>
              <Label>Stop price</Label>
              <input
                type="number" step="0.01" min="0.01"
                className="w-full p-2 rounded bg-transparent border" style={inputStyle}
                placeholder="195.00" value={stop} onChange={e => setStop(e.target.value)} required
              />
            </div>
          )}

          <button
            type="submit"
            disabled={submitting || !acctId}
            className={(side === "buy" ? "btn-primary" : "btn-danger") + " w-full p-2.5 text-sm capitalize inline-flex items-center justify-center gap-2"}
          >
            <span>{`${side === "buy" ? "Buy" : "Sell"} ${orderType.replace("_", "-")} order`}</span>
            {submitting && <Spinner />}
          </button>
        </div>
      </div>
    </form>
  );

  // ── Order summary — collapsible accordion ────────────────────────────────
  const isOption = instrument === "option";
  const summaryHeadline = qty && symbol
    ? `${side.toUpperCase()} ${qty} ${symbol.toUpperCase()}${isOption ? " (option)" : ""} · ${orderType.replace("_", "-")}`
    : "Fill in symbol & quantity";

  const summaryCard = (
    <div className="rounded border sticky top-4 overflow-hidden" style={sectionStyle}>
      {/* Accordion header — always visible, click to toggle */}
      <button
        type="button"
        onClick={() => setSummaryOpen(v => !v)}
        className="w-full flex items-center justify-between gap-3 p-4 text-left transition-colors hover:opacity-90"
      >
        <div className="min-w-0 flex-1">
          <div className="font-semibold text-sm">Order summary</div>
          <div className="text-[11px] mt-0.5 truncate" style={{ color: "var(--muted)" }}>
            {summaryHeadline}
          </div>
        </div>
        <span className="text-base shrink-0" style={{ color: "var(--muted)" }}>
          {summaryOpen ? <ChevronUp /> : <ChevronDown />}
        </span>
      </button>

      {/* Body */}
      {summaryOpen && (
        <div className="px-5 pb-5 pt-1 space-y-4 border-t" style={{ borderColor: "var(--border)" }}>
          <div className="pt-3">
            <div className="text-[11px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>
              {isOption ? "OCC symbol" : "Symbol"}
            </div>
            <div className="font-mono text-sm break-all p-2 mt-1 rounded" style={{ background: "rgba(255,255,255,0.03)" }}>
              {isOption
                ? (occ ?? <span style={{ color: "var(--muted)" }}>fill in expiry, strike & right</span>)
                : (symbol ? symbol.toUpperCase() : <span style={{ color: "var(--muted)" }}>enter a ticker</span>)
              }
            </div>
          </div>

          {isOption && (
            <div className="border-t pt-3" style={{ borderColor: "var(--border)" }}>
              <div className="text-[11px] uppercase tracking-wider mb-2" style={{ color: "var(--muted)" }}>Contract</div>
              <dl className="space-y-1.5 text-sm">
                <Row label="Underlying" value={symbol ? symbol.toUpperCase() : "—"} />
                <Row label="Expiry" value={fmtDate(expiry)} />
                <Row label="Strike" value={strike ? fmtMoney(Number(strike)) : "—"} />
                <Row label="Type" value={right.toUpperCase()} valueColor={right === "call" ? "var(--good)" : "var(--bad)"} />
              </dl>
            </div>
          )}

          <div className="border-t pt-3" style={{ borderColor: "var(--border)" }}>
            <div className="text-[11px] uppercase tracking-wider mb-2" style={{ color: "var(--muted)" }}>Order</div>
            <dl className="space-y-1.5 text-sm">
              <Row label="Side" value={side.toUpperCase()} valueColor={side === "buy" ? "var(--good)" : "var(--bad)"} />
              <Row
                label="Quantity"
                value={qty
                  ? `${qty} ${isOption ? `contract${Number(qty) === 1 ? "" : "s"}` : `share${Number(qty) === 1 ? "" : "s"}`}`
                  : "—"}
              />
              <Row label="Order type" value={orderType.replace("_", "-")} />
              {(orderType === "limit" || orderType === "stop_limit") && (
                <Row label="Limit" value={limit ? fmtMoney(Number(limit)) : "—"} />
              )}
              {(orderType === "stop" || orderType === "stop_limit") && (
                <Row label="Stop" value={stop ? fmtMoney(Number(stop)) : "—"} />
              )}
              <Row label="Time in force" value="Day" />
            </dl>
          </div>

          <div className="border-t pt-3" style={{ borderColor: "var(--border)" }}>
            <div className="text-[11px] uppercase tracking-wider mb-2" style={{ color: "var(--muted)" }}>
              {side === "buy" ? "Estimated cost" : "Estimated proceeds"}
            </div>
            {orderType === "market" ? (
              <div className="text-sm" style={{ color: "var(--muted)" }}>
                Computed at fill — depends on market price.
              </div>
            ) : (
              <>
                <div className="text-2xl font-semibold">{fmtMoney(estCost)}</div>
                <div className="text-[11px] mt-1" style={{ color: "var(--muted)" }}>
                  {qty || "—"} × {limit ? fmtMoney(Number(limit)) : "—"}
                  {isOption ? " × 100 (contract multiplier)" : ""}
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Trade panel</h1>
          <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
            Orders placed here mirror to all subscribers who have copy trading on, scaled by their multiplier.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setExitConfirmOpen(true)}
          disabled={exitBusy}
          title="Close every open position at market across all connected brokers"
          className="btn-danger-soft shrink-0 px-3 py-2 text-sm font-medium inline-flex items-center gap-2"
        >
          <span>Exit All Positions</span>
          {exitBusy && <Spinner />}
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[640px_304px] gap-5 items-start">
        <div>{formBody}</div>
        <div>{summaryCard}</div>
      </div>


      <OpenPositionsTable ref={positionsRef} className="pt-2" />
      <ConfirmModal
        open={exitConfirmOpen}
        title="Exit all positions?"
        message="This will close every open position at market across every connected broker. The action cannot be undone."
        confirmLabel="Exit all at market"
        variant="danger"
        busy={exitBusy}
        onConfirm={doExitAll}
        onCancel={() => setExitConfirmOpen(false)}
      />
    </div>
  );
}

// Small reusable label/value row for the summary card.
function Row({
  label, value, valueColor,
}: {
  label: string; value: React.ReactNode; valueColor?: string;
}) {
  return (
    <div className="flex justify-between gap-2">
      <dt style={{ color: "var(--muted)" }}>{label}</dt>
      <dd className="font-medium text-right" style={valueColor ? { color: valueColor } : undefined}>
        {value}
      </dd>
    </div>
  );
}
