import { test, expect, APIRequestContext } from "@playwright/test";
import { API, registerViaApi, loginViaApi, uniqueEmail } from "../helpers";
import { seedOrder, seedFill } from "../db";

const WIDE = "from=2020-01-01&to=2030-01-01&tz=America/New_York";
function authed(token: string) {
  return { headers: { Authorization: `Bearer ${token}` } };
}
async function newTrader(request: APIRequestContext) {
  const email = uniqueEmail("cal-trader");
  const u = await registerViaApi(request, { email, role: "trader", businessName: "QA Cap" });
  const tok = await loginViaApi(request, email);
  return { id: u.id as string, token: tok.access_token };
}
async function newSubscriber(request: APIRequestContext) {
  const email = uniqueEmail("cal-sub");
  const u = await registerViaApi(request, { email });
  const tok = await loginViaApi(request, email);
  return { id: u.id as string, token: tok.access_token };
}

test.describe("Calendar · P&L API", () => {
  // CAL-FUNC-001 — no fills → empty list
  test("empty range returns []", async ({ request }) => {
    const t = await newTrader(request);
    const r = await request.get(`${API}/api/calendar/pnl?${WIDE}`, authed(t.token));
    expect(r.status()).toBe(200);
    expect(await r.json()).toEqual([]);
  });

  // CAL-VAL-001 — from > to → 422
  test("from > to is rejected (422)", async ({ request }) => {
    const t = await newTrader(request);
    const r = await request.get(`${API}/api/calendar/pnl?from=2030-01-01&to=2020-01-01&tz=America/New_York`, authed(t.token));
    expect(r.status()).toBe(422);
  });

  // CAL-SEC-001 — non-trader view-as another user → 403
  test("subscriber cannot view another user's P&L (403)", async ({ request }) => {
    const s = await newSubscriber(request);
    const victim = await newTrader(request);
    const r = await request.get(`${API}/api/calendar/pnl?${WIDE}&user_id=${victim.id}`, authed(s.token));
    expect(r.status()).toBe(403);
  });

  // CAL-SEC-002 — trader view-as a non-follower → 404
  test("trader viewing a non-follower's P&L → 404", async ({ request }) => {
    const t = await newTrader(request);
    const stranger = await newSubscriber(request);
    const r = await request.get(`${API}/api/calendar/pnl?${WIDE}&user_id=${stranger.id}`, authed(t.token));
    expect(r.status()).toBe(404);
  });

  // CAL-FUNC-002 — realized P&L from a seeded round-trip (buy then sell higher)
  test("round-trip fills produce realized P&L", async ({ request }) => {
    const t = await newTrader(request);
    const buy = await seedOrder(t.id, { symbol: "RT", side: "buy", status: "filled", quantity: 10, filledAvgPrice: 100, ageSeconds: 120 });
    await seedFill(buy, { quantity: 10, price: 100, ageSeconds: 120 });
    const sell = await seedOrder(t.id, { symbol: "RT", side: "sell", status: "filled", quantity: 10, filledAvgPrice: 110, ageSeconds: 30 });
    await seedFill(sell, { quantity: 10, price: 110, ageSeconds: 30 });
    const rows = await (await request.get(`${API}/api/calendar/pnl?${WIDE}`, authed(t.token))).json();
    const totalPnl = rows.reduce((a: number, d: any) => a + Number(d.realized_pnl), 0);
    expect(totalPnl).toBeCloseTo(100, 2); // +$100 realized
  });
});
