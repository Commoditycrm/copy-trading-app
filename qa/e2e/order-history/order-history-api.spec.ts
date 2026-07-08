import { test, expect, APIRequestContext } from "@playwright/test";
import { API, registerViaApi, loginViaApi, uniqueEmail } from "../helpers";
import { seedOrder, seedFill, seedFakeBroker } from "../db";

function authed(token: string) {
  return { headers: { Authorization: `Bearer ${token}` } };
}
async function newTrader(request: APIRequestContext) {
  const email = uniqueEmail("oh-trader");
  const u = await registerViaApi(request, { email, role: "trader", businessName: "QA Cap" });
  const tok = await loginViaApi(request, email);
  return { id: u.id as string, token: tok.access_token };
}

test.describe("Order History · API", () => {
  // OH-FUNC-001
  test("list is newest-first and caller-scoped", async ({ request }) => {
    const t = await newTrader(request);
    await seedOrder(t.id, { symbol: "OLD", ageSeconds: 600 });
    await seedOrder(t.id, { symbol: "NEW", ageSeconds: 5 });
    const r = await request.get(`${API}/api/trades`, authed(t.token));
    expect(r.status()).toBe(200);
    const rows = await r.json();
    expect(rows.length).toBe(2);
    expect(rows[0].symbol).toBe("NEW");
    expect(rows[1].symbol).toBe("OLD");
  });

  // OH-SEC-001
  test("history does not leak other users' orders", async ({ request }) => {
    const a = await newTrader(request);
    const b = await newTrader(request);
    await seedOrder(a.id, { symbol: "AONLY" });
    const rb = await request.get(`${API}/api/trades`, authed(b.token));
    expect(await rb.json()).toEqual([]);
  });

  // OH-FUNC-003 — stats: all vs mine, notional (stock ×1, option ×100), filled only
  test("stats compute all/mine totals and notional", async ({ request }) => {
    const t = await newTrader(request);
    await seedOrder(t.id, { symbol: "MINE", status: "filled", quantity: 10, filledAvgPrice: 100, fannedOut: false }); // notional 1000, in mine+all
    await seedOrder(t.id, { symbol: "FAN", status: "filled", quantity: 5, filledAvgPrice: 100, fannedOut: true }); // notional 500, all only
    await seedOrder(t.id, { symbol: "WORK", status: "submitted", quantity: 3, fannedOut: false }); // working, notional 0
    const r = await request.get(`${API}/api/trades/stats`, authed(t.token));
    const s = await r.json();
    expect(s.all.total).toBe(3);
    expect(s.all.filled).toBe(2);
    expect(s.all.working).toBe(1);
    expect(Number(s.all.notional)).toBe(1500);
    expect(s.mine.total).toBe(2); // not-fanned-out only
    expect(Number(s.mine.notional)).toBe(1000);
  });

  // OH-FUNC-004 — STATS excludes non-filled bracket legs from counts; the LIST
  // endpoint returns them all (the UI hides resting legs client-side).
  test("stats exclude resting bracket legs; list returns them all", async ({ request }) => {
    const t = await newTrader(request);
    const entry = await seedOrder(t.id, { symbol: "BRK", status: "filled" });
    await seedOrder(t.id, { symbol: "BRK", status: "submitted", bracketParentId: entry, bracketLeg: "tp" }); // resting
    await seedOrder(t.id, { symbol: "BRK", status: "filled", bracketParentId: entry, bracketLeg: "sl" }); // filled
    // list is unfiltered (client hides resting legs)
    const rows = await (await request.get(`${API}/api/trades`, authed(t.token))).json();
    expect(rows.length).toBe(3);
    // stats: entry (no parent) + filled sl are counted; resting tp is excluded
    const s = await (await request.get(`${API}/api/trades/stats`, authed(t.token))).json();
    expect(s.all.total).toBe(2);
  });

  // OH-FUNC-005 — detail + fills; 404 for non-owner
  test("order detail returns fills; non-owner gets 404", async ({ request }) => {
    const t = await newTrader(request);
    const other = await newTrader(request);
    const oid = await seedOrder(t.id, { symbol: "DET", status: "filled", quantity: 4, filledAvgPrice: 50 });
    await seedFill(oid, { quantity: 4, price: 50 });
    const ok = await request.get(`${API}/api/trades/${oid}`, authed(t.token));
    expect(ok.status()).toBe(200);
    expect((await ok.json()).fills.length).toBe(1);
    const denied = await request.get(`${API}/api/trades/${oid}`, authed(other.token));
    expect(denied.status()).toBe(404);
  });

  // OH-FUNC-006 — cancel terminal order → 409
  test("cancelling a FILLED order returns 409", async ({ request }) => {
    const t = await newTrader(request);
    const oid = await seedOrder(t.id, { symbol: "FILLED", status: "filled" });
    const r = await request.post(`${API}/api/trades/${oid}/cancel`, authed(t.token));
    expect(r.status()).toBe(409);
  });

  // OH-FUNC-007 — cancel non-owner → 404
  test("cancelling another user's order → 404", async ({ request }) => {
    const t = await newTrader(request);
    const other = await newTrader(request);
    const oid = await seedOrder(t.id, { symbol: "X", status: "submitted" });
    const r = await request.post(`${API}/api/trades/${oid}/cancel`, authed(other.token));
    expect(r.status()).toBe(404);
  });

  // OH-FUNC-008 — cancel a real (fake-broker) open order succeeds
  test("cancel a live fake-broker order succeeds", async ({ request }) => {
    const t = await newTrader(request);
    const bid = await seedFakeBroker(t.id);
    const placed = await request.post(`${API}/api/trades?broker_account_id=${bid}`, {
      headers: { Authorization: `Bearer ${t.token}` },
      data: { instrument_type: "stock", symbol: "CANC", side: "buy", order_type: "limit", quantity: "1", limit_price: "10" },
    });
    expect(placed.status()).toBe(201);
    const oid = (await placed.json()).id;
    const r = await request.post(`${API}/api/trades/${oid}/cancel`, authed(t.token));
    // fake adapter accepts the cancel → canceled; tolerate 200 (canceled) result
    expect([200]).toContain(r.status());
    expect((await r.json()).status).toBe("canceled");
  });
});
