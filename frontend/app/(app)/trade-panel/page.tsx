"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { BrokerAccount, InstrumentType, Order, OrderSide, OrderType, OptionRight } from "@/lib/types";

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

function fmtDate(iso: string): string {
  if (!iso) return "—";
  try {
    return new Date(iso + "T00:00:00Z").toLocaleDateString(undefined, {
      month: "short", day: "numeric", year: "numeric", timeZone: "UTC",
    });
  } catch {
    return iso;
  }
}

// ── shared style helpers ─────────────────────────────────────────────────────

const sectionStyle: React.CSSProperties = {
  borderColor: "var(--border)",
  background: "var(--panel)",
};

const inputStyle: React.CSSProperties = { borderColor: "var(--border)" };

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
    "rgba(182, 255, 60, 0.35)";
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
  const [orderType, setOrderType] = useState<OrderType>("market");
  const [qty, setQty] = useState("1");
  const [limit, setLimit] = useState("");
  const [stop, setStop] = useState("");
  const [expiry, setExpiry] = useState("");
  const [strike, setStrike] = useState("");
  const [right, setRight] = useState<OptionRight>("call");
  const [submitting, setSubmitting] = useState(false);
  const [last, setLast] = useState<Order | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Expiries fetched from SnapTrade per (symbol, account). Cached client-side
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
        // If current selection is no longer valid, clear it.
        if (expiry && !res.expiries.includes(expiry)) setExpiry("");
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

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    setSubmitting(true);
    try {
      const body: Record<string, unknown> = {
        instrument_type: instrument,
        symbol: symbol.toUpperCase(),
        side,
        order_type: orderType,
        quantity: qty,
      };
      if (orderType === "limit" || orderType === "stop_limit") body.limit_price = limit;
      if (orderType === "stop" || orderType === "stop_limit") body.stop_price = stop;
      if (instrument === "option") {
        body.option_expiry = expiry;
        body.option_strike = strike;
        body.option_right = right;
      }
      const res = await api<Order>(`/api/trades?broker_account_id=${acctId}`, {
        method: "POST", body: JSON.stringify(body),
      });
      setLast(res);
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  // The form is the same content in both stock and option mode — only the
  // wrapper layout changes (single-col vs two-col with summary on the right).
  const formBody = (
    <form onSubmit={submit} className="space-y-5 p-5 rounded border" style={sectionStyle}>

      {/* Account */}
      <div>
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
      </div>

      {/* Instrument toggle */}
      <div>
        <Label>Instrument</Label>
        <div className="flex gap-2">
          <SegBtn active={instrument === "option"} onClick={() => setInstrument("option")}>Options</SegBtn>
          <SegBtn active={instrument === "stock"} onClick={() => setInstrument("stock")}>Stocks</SegBtn>
        </div>
      </div>

      {/* Symbol */}
      <div>
        <Label hint="e.g. AAPL, TSLA, NVDA">Symbol</Label>
        <input
          className="w-full p-2 rounded bg-transparent border uppercase tracking-wide font-medium" style={inputStyle}
          placeholder="AAPL" value={symbol}
          onChange={e => setSymbol(e.target.value)} required
        />
      </div>

      {/* Option contract fields */}
      {instrument === "option" && (
        <div className="space-y-3 p-3 rounded" style={{ background: "rgba(78,161,255,0.05)", border: "1px dashed var(--border)" }}>
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
                        ? "enter symbol first"
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

      {/* Side */}
      <div>
        <Label>Side</Label>
        <div className="flex gap-2">
          <SegBtn color="good" active={side === "buy"} onClick={() => setSide("buy")}>Buy</SegBtn>
          <SegBtn color="bad" active={side === "sell"} onClick={() => setSide("sell")}>Sell</SegBtn>
        </div>
      </div>

      {/* Order type + qty */}
      <div className="grid grid-cols-2 gap-3">
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
        <div>
          <Label hint={instrument === "option" ? "contracts" : "shares"}>Quantity</Label>
          <input
            type="number" step="1" min="1"
            className="w-full p-2 rounded bg-transparent border" style={inputStyle}
            placeholder="1" value={qty} onChange={e => setQty(e.target.value)} required
          />
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

      {err && (
        <div className="p-3 rounded text-sm" style={{ background: "rgba(239,68,68,0.08)", color: "var(--bad)" }}>
          {err}
        </div>
      )}

      <button
        disabled={submitting || !acctId}
        className={side === "buy" ? "btn-primary w-full p-3 text-base" : "btn-danger w-full p-3 text-base"}
      >
        {submitting
          ? "Placing…"
          : `${side === "buy" ? "Buy" : "Sell"} ${instrument === "option" ? "contracts" : symbol || "stock"}${" — mirror to subscribers"}`}
      </button>
    </form>
  );

  // ── Order summary card (always shown; contract block is options-only) ─────
  const isOption = instrument === "option";
  const summaryCard = (
    <div className="p-5 rounded border space-y-4 sticky top-4" style={sectionStyle}>
      <h2 className="font-semibold">Order summary</h2>

      <div className="text-[11px] uppercase tracking-wider" style={{ color: "var(--muted)" }}>
        {isOption ? "OCC symbol" : "Symbol"}
      </div>
      <div className="font-mono text-sm break-all p-2 rounded" style={{ background: "rgba(255,255,255,0.03)" }}>
        {isOption
          ? (occ ?? <span style={{ color: "var(--muted)" }}>fill in expiry, strike & right</span>)
          : (symbol ? symbol.toUpperCase() : <span style={{ color: "var(--muted)" }}>enter a ticker</span>)
        }
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
  );

  // ── Last submitted order toast ─────────────────────────────────────────────
  const lastSubmitted = last && (
    <div className="p-3 rounded border text-sm" style={sectionStyle}>
      <div className="flex items-baseline gap-2">
        <span className="font-medium">Last order</span>
        <span className="text-xs uppercase px-2 py-0.5 rounded" style={{
          background: last.status === "rejected" ? "rgba(239,68,68,0.15)" : "rgba(34,197,94,0.15)",
          color: last.status === "rejected" ? "var(--bad)" : "var(--good)",
        }}>
          {last.status}
        </span>
      </div>
      <div className="mt-1 text-xs" style={{ color: "var(--muted)" }}>
        Broker order id: <span className="font-mono">{last.broker_order_id ?? "—"}</span>
      </div>
      {last.reject_reason && (
        <div className="mt-1 text-xs" style={{ color: "var(--bad)" }}>{last.reject_reason}</div>
      )}
    </div>
  );

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">Trade panel</h1>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          Orders placed here mirror to all subscribers who have copy trading on, scaled by their multiplier.
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_360px] gap-5 items-start">
        <div>{formBody}</div>
        <div>{summaryCard}</div>
      </div>

      <div className="lg:max-w-[calc(100%-380px)]">
        {lastSubmitted}
      </div>
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
