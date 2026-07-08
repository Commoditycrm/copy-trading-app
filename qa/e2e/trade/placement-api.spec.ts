import { test, expect, APIRequestContext } from "@playwright/test";
import { API, registerViaApi, loginViaApi, uniqueEmail } from "../helpers";
import { seedFakeBroker } from "../db";

const RANDOM_UUID = "11111111-1111-1111-1111-111111111111";
const STOCK_MKT = { instrument_type: "stock", symbol: "AAPL", side: "buy", order_type: "market", quantity: "1" };

function authed(token: string) {
  return { headers: { Authorization: `Bearer ${token}` } };
}
async function newTrader(request: APIRequestContext) {
  const email = uniqueEmail("tp-trader");
  const u = await registerViaApi(request, { email, role: "trader", businessName: "QA Cap" });
  const tok = await loginViaApi(request, email);
  return { id: u.id as string, token: tok.access_token };
}
async function newSubscriber(request: APIRequestContext) {
  const email = uniqueEmail("tp-sub");
  const u = await registerViaApi(request, { email });
  const tok = await loginViaApi(request, email);
  return { id: u.id as string, token: tok.access_token };
}
function place(request: APIRequestContext, token: string | null, brokerId: string, body: object) {
  return request.post(`${API}/api/trades?broker_account_id=${brokerId}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    data: body,
  });
}

test.describe("Trade Panel · placement API", () => {
  // TP-AUTHZ-001
  test("subscriber cannot place an order (403)", async ({ request }) => {
    const s = await newSubscriber(request);
    const r = await place(request, s.token, RANDOM_UUID, STOCK_MKT);
    expect(r.status()).toBe(403);
  });

  // TP-AUTHZ-002
  test("unauthenticated placement is rejected (401)", async ({ request }) => {
    const r = await place(request, null, RANDOM_UUID, STOCK_MKT);
    expect(r.status()).toBe(401);
  });

  // TP-NEG-001
  test("trader + unknown broker account → 404", async ({ request }) => {
    const t = await newTrader(request);
    const r = await place(request, t.token, RANDOM_UUID, STOCK_MKT);
    expect(r.status()).toBe(404);
  });

  // TP-NEG-002
  test("disconnected broker → 409", async ({ request }) => {
    const t = await newTrader(request);
    const bid = await seedFakeBroker(t.id, "disconnected");
    const r = await place(request, t.token, bid, STOCK_MKT);
    expect(r.status()).toBe(409);
  });

  // TP-VAL-001..005 — schema validation (before broker lookup)
  for (const [name, body] of [
    ["option missing strike", { instrument_type: "option", symbol: "AAPL", side: "buy", order_type: "market", quantity: "1", option_expiry: "2026-12-18", option_right: "call" }],
    ["limit without limit_price", { instrument_type: "stock", symbol: "AAPL", side: "buy", order_type: "limit", quantity: "1" }],
    ["quantity ≤ 0", { ...STOCK_MKT, quantity: "0" }],
    ["buy bracket geometry invalid (TP<limit)", { instrument_type: "stock", symbol: "AAPL", side: "buy", order_type: "limit", quantity: "1", limit_price: "100", take_profit_price: "90", stop_loss_price: "95" }],
  ] as const) {
    test(`validation: ${name} → 4xx`, async ({ request }) => {
      const t = await newTrader(request);
      const r = await place(request, t.token, RANDOM_UUID, body);
      expect([400, 422]).toContain(r.status());
    });
  }

  // TP-FUNC-001 — real placement via the Fake adapter
  test("stock market buy places at the (fake) broker → 201", async ({ request }) => {
    const t = await newTrader(request);
    const bid = await seedFakeBroker(t.id);
    const r = await place(request, t.token, bid, STOCK_MKT);
    expect(r.status(), await r.text()).toBe(201);
    const o = await r.json();
    expect(o.symbol).toBe("AAPL");
    expect(o.broker_order_id).toMatch(/^fake-/);
    expect(["pending", "submitted", "accepted", "partially_filled", "filled"]).toContain(o.status);
  });

  // TP-FUNC-003 — option market buy
  test("option market buy places → 201 with option fields", async ({ request }) => {
    const t = await newTrader(request);
    const bid = await seedFakeBroker(t.id);
    const r = await place(request, t.token, bid, {
      instrument_type: "option", symbol: "NVDA", side: "buy", order_type: "market", quantity: "1",
      option_expiry: "2026-12-18", option_strike: "130", option_right: "call",
    });
    expect(r.status(), await r.text()).toBe(201);
    const o = await r.json();
    expect(o.instrument_type).toBe("option");
    expect(o.option_right).toBe("call");
    expect(o.broker_order_id).toMatch(/^fake-/);
  });

  // TP-FUNC-005 — duplicate suppression (OBS-TP-002): two identical concurrent POSTs → one order
  test("rapid identical submit is de-duplicated to one order", async ({ request }) => {
    const t = await newTrader(request);
    const bid = await seedFakeBroker(t.id);
    const body = { instrument_type: "stock", symbol: "TSLA", side: "buy", order_type: "market", quantity: "2" };
    const [r1, r2] = await Promise.all([place(request, t.token, bid, body), place(request, t.token, bid, body)]);
    expect(r1.status()).toBe(201);
    expect(r2.status()).toBe(201);
    const [o1, o2] = [await r1.json(), await r2.json()];
    expect(o1.id).toBe(o2.id); // same order returned twice (3s dedup window)
  });
});
