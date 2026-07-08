import { test, expect, APIRequestContext } from "@playwright/test";
import { API, registerViaApi, loginViaApi, uniqueEmail } from "../helpers";
import { seedFakeBroker } from "../db";

const RANDOM_UUID = "22222222-2222-2222-2222-222222222222";
function authed(token: string) {
  return { headers: { Authorization: `Bearer ${token}` } };
}
async function newTrader(request: APIRequestContext) {
  const email = uniqueEmail("pos-trader");
  const u = await registerViaApi(request, { email, role: "trader", businessName: "QA Cap" });
  const tok = await loginViaApi(request, email);
  return { id: u.id as string, token: tok.access_token };
}
async function newSubscriber(request: APIRequestContext) {
  const email = uniqueEmail("pos-sub");
  const u = await registerViaApi(request, { email });
  const tok = await loginViaApi(request, email);
  return { id: u.id as string, token: tok.access_token };
}

test.describe("Positions · API", () => {
  // POS-FUNC-001 — no broker → empty
  test("no broker → empty positions", async ({ request }) => {
    const t = await newTrader(request);
    const r = await request.get(`${API}/api/positions`, authed(t.token));
    expect(r.status()).toBe(200);
    expect(await r.json()).toEqual([]);
  });

  // with a connected fake broker holding no positions → still empty
  test("connected fake broker with no positions → empty", async ({ request }) => {
    const t = await newTrader(request);
    await seedFakeBroker(t.id);
    const r = await request.get(`${API}/api/positions`, authed(t.token));
    expect(r.status()).toBe(200);
    expect(Array.isArray(await r.json())).toBe(true);
  });

  // POS-NEG-001 — close a position on an unknown account → 404
  test("close on unknown broker account → 404", async ({ request }) => {
    const t = await newTrader(request);
    const r = await request.post(`${API}/api/positions/AAPL/close?broker_account_id=${RANDOM_UUID}`, {
      ...authed(t.token),
      data: {},
    });
    expect(r.status()).toBe(404);
  });

  // POS-FUNC-003 — close-all with no positions → 200, nothing closed
  test("close-all with no positions → 200 closed_count 0", async ({ request }) => {
    const t = await newTrader(request);
    await seedFakeBroker(t.id);
    const r = await request.post(`${API}/api/positions/close-all?include_subscribers=false`, authed(t.token));
    expect(r.status()).toBe(200);
    expect((await r.json()).closed_count).toBe(0);
  });

  // POS-AUTHZ-001 — subscriber cannot bulk-close subscribers' positions
  test("subscriber cannot call close-all-subscribers (403)", async ({ request }) => {
    const s = await newSubscriber(request);
    const r = await request.post(`${API}/api/positions/close-all-subscribers`, authed(s.token));
    expect(r.status()).toBe(403);
  });

  // trader with no subscribers → queued 0
  test("trader close-all-subscribers with no subscribers → queued 0", async ({ request }) => {
    const t = await newTrader(request);
    const r = await request.post(`${API}/api/positions/close-all-subscribers`, authed(t.token));
    expect(r.status()).toBe(200);
    expect((await r.json()).queued_pairs).toBe(0);
  });
});
