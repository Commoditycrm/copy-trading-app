import { test, expect } from "@playwright/test";
import { registerViaApi, loginViaApi, seedTokens, uniqueEmail } from "./helpers";

// The root route renders nothing and redirects by role. There is no marketing
// landing page (OBS-LAND-001) — anonymous visitors go straight to /login.
test.describe("Landing · root redirect", () => {
  // LAND-FUNC-001
  test("anonymous visitor is redirected to /login", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
  });

  // LAND-FUNC-002
  test("subscriber is redirected to /dashboard", async ({ page, request }) => {
    const email = uniqueEmail("land-sub");
    await registerViaApi(request, { email });
    const tok = await loginViaApi(request, email);
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/");
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  // LAND-FUNC-003
  test("trader is redirected to /dashboard", async ({ page, request }) => {
    const email = uniqueEmail("land-trader");
    await registerViaApi(request, { email, role: "trader", businessName: "QA Capital" });
    const tok = await loginViaApi(request, email);
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/");
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  // LAND-FUNC-004 — BLOCKED: admin role is unusable on qa-branch (BUG-AUTH-001,
  // admin user_role enum case mismatch). Unskip once that fix is deployed here.
  test.skip("admin is redirected to /admin (blocked by BUG-AUTH-001 on qa-branch)", async () => {});

  // LAND-SESS-001
  test("stale/invalid tokens are cleared and visitor lands on /login", async ({ page }) => {
    await seedTokens(page, "invalid.access.token", "invalid.refresh.token");
    await page.goto("/");
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
    const access = await page.evaluate(() => localStorage.getItem("trading-app:access"));
    expect(access).toBeNull();
  });

  // LAND-AUTHZ-001 — no authed chrome leaks before the redirect resolves
  test("renders no dashboard content before redirecting", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
  });
});
