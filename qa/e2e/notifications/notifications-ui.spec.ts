import { test, expect } from "@playwright/test";
import { registerViaApi, loginViaApi, seedTokens, uniqueEmail } from "../helpers";
import { seedNotification, seedManyNotifications, getNotificationReadAt, countUnread } from "../db";

async function arrange(page: any, request: any, prefix: string) {
  const email = uniqueEmail(prefix);
  const user = await registerViaApi(request, { email });
  const tok = await loginViaApi(request, email);
  return { user, tok };
}

test.describe("Notifications · UI", () => {
  // NOTIF-FUNC-002 — empty state
  test("page shows the empty state", async ({ page, request }) => {
    const { tok } = await arrange(page, request, "nui-empty");
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/notifications");
    await expect(page.getByText(/all caught up/i)).toBeVisible({ timeout: 15_000 });
  });

  // NOTIF-FUNC-001/003/004 — newest-first, unread styled, mark-read persists
  test("lists newest-first and mark-read persists to DB", async ({ page, request }) => {
    const { user, tok } = await arrange(page, request, "nui-list");
    await seedNotification(user.id, { message: "Older alert", ageSeconds: 300 });
    const newerId = await seedNotification(user.id, { message: "Newer alert", ageSeconds: 5 });
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/notifications");
    await expect(page.getByText("Newer alert")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("Older alert")).toBeVisible();
    // first (newest) unread → mark read
    await page.getByRole("button", { name: /mark read/i }).first().click();
    await expect.poll(async () => await getNotificationReadAt(newerId), { timeout: 10_000 }).not.toBeNull();
  });

  // NOTIF-FUNC-005 — "View in Order History" link when metadata has child_order_id
  test("shows Order History link when notification has a child_order_id", async ({ page, request }) => {
    const { user, tok } = await arrange(page, request, "nui-link");
    await seedNotification(user.id, {
      message: "Mirror failed",
      metadata: { child_order_id: "abc-123" },
    });
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/notifications");
    await expect(page.getByRole("link", { name: /view in order history/i })).toBeVisible({ timeout: 15_000 });
  });

  // NOTIF-FUNC-006/008 — header bell badge + mark-all-read clears it
  test("header bell shows unread badge and mark-all clears it", async ({ page, request }) => {
    const { user, tok } = await arrange(page, request, "nui-badge");
    await seedManyNotifications(user.id, 3);
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/notifications");
    const bell = page.getByRole("button", { name: /notifications/i });
    await expect(bell).toHaveAttribute("aria-label", /3 unread/, { timeout: 15_000 });
    await bell.click();
    await expect(page.getByRole("menu")).toBeVisible();
    await page.getByRole("button", { name: /mark all read/i }).click();
    await expect(bell).toHaveAttribute("aria-label", /^Notifications$/, { timeout: 10_000 });
    expect(await countUnread(user.id)).toBe(0);
  });

  // NOTIF-FUNC-009 — bell dropdown closes on Escape
  test("bell dropdown closes on Escape", async ({ page, request }) => {
    const { user, tok } = await arrange(page, request, "nui-esc");
    await seedManyNotifications(user.id, 2);
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/notifications");
    const bell = page.getByRole("button", { name: /notifications/i });
    await bell.click();
    await expect(page.getByRole("menu")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu")).toBeHidden();
  });
});
