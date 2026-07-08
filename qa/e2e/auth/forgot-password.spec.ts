import { test, expect } from "@playwright/test";
import { registerViaApi, uniqueEmail } from "../helpers";

test.describe("Auth · Forgot Password", () => {
  // AUTH-FORGOT-UI-001 — form renders
  test("renders the email form", async ({ page }) => {
    await page.goto("/forgot-password");
    await expect(page.locator('input[type="email"]')).toBeVisible();
    await expect(page.getByRole("button", { name: /send reset link/i })).toBeVisible();
  });

  // AUTH-FORGOT-FUNC-002 — existing user: generic confirmation screen after submit
  test("known email shows the generic confirmation screen", async ({ page, request }) => {
    const email = uniqueEmail("forgot");
    await registerViaApi(request, { email });
    await page.goto("/forgot-password");
    await page.locator('input[type="email"]').fill(email);
    await page.getByRole("button", { name: /send reset link/i }).click();
    await expect(page.getByText(/sent a\s+reset link/i)).toBeVisible({ timeout: 10_000 });
  });

  // AUTH-FORGOT-BIZ-001 — unknown email shows the SAME confirmation (anti-enumeration)
  test("unknown email shows the same confirmation (no enumeration)", async ({ page }) => {
    await page.goto("/forgot-password");
    await page.locator('input[type="email"]').fill(uniqueEmail("ghost"));
    await page.getByRole("button", { name: /send reset link/i }).click();
    await expect(page.getByText(/sent a\s+reset link/i)).toBeVisible({ timeout: 10_000 });
  });
});
