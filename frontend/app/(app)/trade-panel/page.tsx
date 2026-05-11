"use client";

import { FormEvent, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { BrokerAccount, InstrumentType, Order, OrderSide, OrderType, OptionRight } from "@/lib/types";

export default function TradePanelPage() {
  const [accts, setAccts] = useState<BrokerAccount[]>([]);
  const [acctId, setAcctId] = useState<string>("");
  const [instrument, setInstrument] = useState<InstrumentType>("stock");
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

  useEffect(() => {
    api<BrokerAccount[]>("/api/brokers").then(a => {
      setAccts(a);
      if (a.length && !acctId) setAcctId(a[0].id);
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  return (
    <div className="space-y-6 max-w-2xl">
      <h1 className="text-2xl font-semibold">Trade panel</h1>
      <p className="text-sm" style={{color: "var(--muted)"}}>
        Orders placed here are mirrored to all subscribers who have copy trading turned ON.
      </p>

      <form onSubmit={submit} className="space-y-3 p-4 rounded border" style={{borderColor: "var(--border)", background: "var(--panel)"}}>
        <div>
          <label className="text-xs uppercase block mb-1" style={{color: "var(--muted)"}}>Account</label>
          <select value={acctId} onChange={e => setAcctId(e.target.value)} required className="w-full p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}}>
            {accts.length === 0 && <option value="">— connect a broker first —</option>}
            {accts.map(a => (
              <option key={a.id} value={a.id}>{a.broker} · {a.label}{a.is_paper ? " (paper)" : ""}</option>
            ))}
          </select>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <button type="button" onClick={() => setInstrument("stock")} className="p-2 rounded border" style={{borderColor: instrument === "stock" ? "var(--accent)" : "var(--border)"}}>Stock</button>
          <button type="button" onClick={() => setInstrument("option")} className="p-2 rounded border" style={{borderColor: instrument === "option" ? "var(--accent)" : "var(--border)"}}>Option</button>
        </div>

        <input className="w-full p-2 rounded bg-transparent border uppercase" style={{borderColor: "var(--border)"}} placeholder="Symbol (e.g. AAPL)" value={symbol} onChange={e => setSymbol(e.target.value)} required />

        {instrument === "option" && (
          <div className="grid grid-cols-3 gap-2">
            <input type="date" className="p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}} value={expiry} onChange={e => setExpiry(e.target.value)} required />
            <input className="p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}} placeholder="Strike" value={strike} onChange={e => setStrike(e.target.value)} required />
            <select value={right} onChange={e => setRight(e.target.value as OptionRight)} className="p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}}>
              <option value="call">Call</option>
              <option value="put">Put</option>
            </select>
          </div>
        )}

        <div className="grid grid-cols-2 gap-2">
          <button type="button" onClick={() => setSide("buy")} className="p-2 rounded font-medium" style={{background: side === "buy" ? "var(--good)" : "transparent", border: side === "buy" ? "none" : "1px solid var(--border)", color: side === "buy" ? "#06121f" : "var(--text)"}}>Buy</button>
          <button type="button" onClick={() => setSide("sell")} className="p-2 rounded font-medium" style={{background: side === "sell" ? "var(--bad)" : "transparent", border: side === "sell" ? "none" : "1px solid var(--border)", color: side === "sell" ? "#06121f" : "var(--text)"}}>Sell</button>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <select value={orderType} onChange={e => setOrderType(e.target.value as OrderType)} className="p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}}>
            <option value="market">Market</option>
            <option value="limit">Limit</option>
            <option value="stop">Stop</option>
            <option value="stop_limit">Stop-limit</option>
          </select>
          <input className="p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}} placeholder="Quantity" value={qty} onChange={e => setQty(e.target.value)} required />
        </div>

        {(orderType === "limit" || orderType === "stop_limit") && (
          <input className="w-full p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}} placeholder="Limit price" value={limit} onChange={e => setLimit(e.target.value)} required />
        )}
        {(orderType === "stop" || orderType === "stop_limit") && (
          <input className="w-full p-2 rounded bg-transparent border" style={{borderColor: "var(--border)"}} placeholder="Stop price" value={stop} onChange={e => setStop(e.target.value)} required />
        )}

        {err && <p className="text-sm" style={{color: "var(--bad)"}}>{err}</p>}
        <button disabled={submitting || !acctId} className="w-full p-2 rounded font-medium" style={{background: "var(--accent)", color: "#06121f"}}>
          {submitting ? "Placing…" : "Place order (and mirror to subscribers)"}
        </button>
      </form>

      {last && (
        <div className="p-3 rounded border text-sm" style={{borderColor: "var(--border)", background: "var(--panel)"}}>
          <div>Status: <strong>{last.status}</strong></div>
          <div>Broker order id: {last.broker_order_id ?? "—"}</div>
          {last.reject_reason && <div style={{color: "var(--bad)"}}>{last.reject_reason}</div>}
        </div>
      )}
    </div>
  );
}
