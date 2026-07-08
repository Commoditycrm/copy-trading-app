import { test, expect, APIRequestContext } from "@playwright/test";
import { API, registerViaApi, loginViaApi, uniqueEmail } from "../helpers";
import { seedNotification, getNotificationReadAt, countUnread } from "../db";

function authed(token: string) {
  return { headers: { Authorization: `Bearer ${token}` } };
}

async function newUserWithToken(request: APIRequestContext, prefix: string) {
  const email = uniqueEmail(prefix);
  const user = await registerViaApi(request, { email });
  const tok = await loginViaApi(request, email);
  return { id: user.id as string, token: tok.access_token, email };
}

test.describe("Notifications · API", () => {
  // NOTIF-API-001 / NOTIF-FUNC-001
  test("list is caller-scoped and newest-first", async ({ request }) => {
    const u = await newUserWithToken(request, "napi");
    await seedNotification(u.id, { message: "oldest", ageSeconds: 300 });
    await seedNotification(u.id, { message: "newest", ageSeconds: 5 });
    const r = await request.get(`${API}/api/notifications`, authed(u.token));
    expect(r.status()).toBe(200);
    const rows = await r.json();
    expect(rows.length).toBe(2);
    expect(rows[0].message).toBe("newest");
    expect(rows[1].message).toBe("oldest");
  });

  // NOTIF-API-001 — unread_only + limit clamp
  test("unread_only filter and limit>200 rejected", async ({ request }) => {
    const u = await newUserWithToken(request, "nfilter");
    await seedNotification(u.id, { message: "unread-one" });
    await seedNotification(u.id, { message: "read-one", read: true });
    const r = await request.get(`${API}/api/notifications?unread_only=true`, authed(u.token));
    const rows = await r.json();
    expect(rows.map((n: any) => n.message)).toEqual(["unread-one"]);
    const over = await request.get(`${API}/api/notifications?limit=201`, authed(u.token));
    expect(over.status()).toBe(422);
  });

  // NOTIF-API-002 — unread-count matches DB
  test("unread-count matches DB", async ({ request }) => {
    const u = await newUserWithToken(request, "ncount");
    await seedNotification(u.id, { message: "a" });
    await seedNotification(u.id, { message: "b" });
    await seedNotification(u.id, { message: "c", read: true });
    const r = await request.get(`${API}/api/notifications/unread-count`, authed(u.token));
    expect((await r.json()).unread).toBe(2);
    expect(await countUnread(u.id)).toBe(2);
  });

  // NOTIF-FUNC-004 / NOTIF-API-003 — mark read sets read_at, idempotent
  test("mark read sets read_at and is idempotent", async ({ request }) => {
    const u = await newUserWithToken(request, "nread");
    const id = await seedNotification(u.id, { message: "mark me" });
    expect(await getNotificationReadAt(id)).toBeNull();
    const r1 = await request.post(`${API}/api/notifications/${id}/read`, authed(u.token));
    expect(r1.status()).toBe(200);
    const first = await getNotificationReadAt(id);
    expect(first).not.toBeNull();
    // second call: still ok, timestamp unchanged
    await request.post(`${API}/api/notifications/${id}/read`, authed(u.token));
    expect(await getNotificationReadAt(id)).toEqual(first);
  });

  // NOTIF-API-004 — read-all returns the count flipped
  test("read-all marks all and returns count", async ({ request }) => {
    const u = await newUserWithToken(request, "nall");
    await seedNotification(u.id, { message: "x" });
    await seedNotification(u.id, { message: "y" });
    const r = await request.post(`${API}/api/notifications/read-all`, authed(u.token));
    const body = await r.json();
    expect(body.ok).toBe(true);
    expect(body.count).toBe(2);
    expect(await countUnread(u.id)).toBe(0);
  });

  // NOTIF-SEC-001 — IDOR: cannot mark another user's notification (404)
  test("cannot mark another user's notification (404)", async ({ request }) => {
    const a = await newUserWithToken(request, "victim");
    const b = await newUserWithToken(request, "attacker");
    const victimNotif = await seedNotification(a.id, { message: "victim's secret" });
    const r = await request.post(`${API}/api/notifications/${victimNotif}/read`, authed(b.token));
    expect(r.status()).toBe(404);
    // and it stays unread for the victim
    expect(await getNotificationReadAt(victimNotif)).toBeNull();
  });

  // NOTIF-SEC-002 — list never leaks another user's notifications
  test("list does not leak other users' notifications", async ({ request }) => {
    const a = await newUserWithToken(request, "userA");
    const b = await newUserWithToken(request, "userB");
    await seedNotification(a.id, { message: "A-only" });
    const rb = await request.get(`${API}/api/notifications`, authed(b.token));
    expect((await rb.json())).toEqual([]);
  });

  // NOTIF-NEG-001 / NOTIF-NEG-002
  test("non-existent id → 404; unauthenticated → 401", async ({ request }) => {
    const u = await newUserWithToken(request, "nneg");
    const missing = await request.post(
      `${API}/api/notifications/00000000-0000-0000-0000-000000000000/read`,
      authed(u.token),
    );
    expect(missing.status()).toBe(404);
    const noauth = await request.get(`${API}/api/notifications`);
    expect(noauth.status()).toBe(401);
  });
});
