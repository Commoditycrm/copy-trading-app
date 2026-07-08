import { test, expect } from "@playwright/test";
import { registerViaApi, loginViaApi, seedTokens, uniqueEmail, STRONG_PW } from "../helpers";

test.describe("Auth · Login", () => {
  // AUTH-LOGIN-UI-001 — form renders its key controls
  test("renders email, password and the forgot/register links", async ({ page }) => {
    await page.goto("/login");
    await expect(page.locator('input[type="email"]')).toBeVisible();
    await expect(page.locator('input[type="password"]')).toBeVisible();
    await expect(page.getByRole("link", { name: /forgot password/i })).toBeVisible();
    await expect(page.getByRole("link", { name: /create an account/i })).toBeVisible();
  });

  // AUTH-LOGIN-UX-001 — invalid credentials surface an error toast (no silent fail)
  test("invalid credentials show an error toast and stay on /login", async ({ page }) => {
    await page.goto("/login");
    await page.locator('input[type="email"]').fill("nobody@qatest.io");
    await page.locator('input[type="password"]').fill("WrongPw!123");
    await page.getByRole("button", { name: /sign in/i }).click();
    await expect(page.locator(".Toastify__toast")).toBeVisible();
    await expect(page).toHaveURL(/\/login/);
  });

  // AUTH-LOGIN-FUNC-004 — valid login routes to the role landing (/dashboard)
  test("valid subscriber login lands on /dashboard", async ({ page, request }) => {
    const email = uniqueEmail("login");
    await registerViaApi(request, { email });
    await page.goto("/login");
    await page.locator('input[type="email"]').fill(email);
    await page.locator('input[type="password"]').fill(STRONG_PW);
    await page.getByRole("button", { name: /sign in/i }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  // AUTH-LOGIN-SESS-001 — an already-authenticated visitor is bounced off /login
  test("already logged-in visitor is redirected away from /login", async ({ page, request }) => {
    const email = uniqueEmail("sess");
    await registerViaApi(request, { email });
    const tok = await loginViaApi(request, email);
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/login");
    await expect(page).not.toHaveURL(/\/login$/, { timeout: 15_000 });
  });
});
