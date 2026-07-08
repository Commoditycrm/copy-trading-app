import { test, expect } from "@playwright/test";
import { registerViaApi, uniqueEmail, STRONG_PW } from "../helpers";

test.describe("Auth · Register", () => {
  // AUTH-REG-FUNC-004 — subscriber happy path auto-logs-in and lands in the app
  test("register subscriber lands in the app", async ({ page }) => {
    await page.goto("/register");
    await page.locator('input[type="email"]').fill(uniqueEmail("reg"));
    await page.locator('input[type="password"]').fill(STRONG_PW);
    await page.getByRole("button", { name: /create account/i }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
  });

  // AUTH-REG-UX-001 — trader without business name is blocked client-side.
  // NOTE: the block is the field's native `required` attribute, which fires
  // before the JS `notify.error("Business name is required...")` guard — so
  // that custom toast is unreachable dead code (minor UX finding, OBS-E2E-001).
  test("trader requires a business name (native required guard)", async ({ page }) => {
    await page.goto("/register");
    await page.locator('input[type="email"]').fill(uniqueEmail("trader"));
    await page.locator('input[type="password"]').fill(STRONG_PW);
    await page.getByRole("button", { name: "Trader" }).click();
    const biz = page.locator('input[autocomplete="organization"]');
    await expect(biz).toBeVisible();
    await page.getByRole("button", { name: /create account/i }).click();
    // Native validation blocks submit → stays on /register, field is :invalid.
    await expect(page).toHaveURL(/\/register/);
    expect(await biz.evaluate((el: HTMLInputElement) => el.required && el.matches(":invalid"))).toBe(true);
  });

  // AUTH-REG-BND-003 — weak password passes client minLength but server rejects (422)
  test("all-lowercase password is rejected by the server", async ({ page }) => {
    await page.goto("/register");
    await page.locator('input[type="email"]').fill(uniqueEmail("weak"));
    await page.locator('input[type="password"]').fill("aaaaaaaa");
    await page.getByRole("button", { name: /create account/i }).click();
    await expect(page.locator(".Toastify__toast")).toBeVisible();
    await expect(page).toHaveURL(/\/register/);
  });

  // AUTH-REG-NEG-001 — duplicate email surfaces an error
  test("duplicate email shows an error toast", async ({ page, request }) => {
    const email = uniqueEmail("dup");
    await registerViaApi(request, { email });
    await page.goto("/register");
    await page.locator('input[type="email"]').fill(email);
    await page.locator('input[type="password"]').fill(STRONG_PW);
    await page.getByRole("button", { name: /create account/i }).click();
    await expect(page.locator(".Toastify__toast")).toBeVisible();
    await expect(page).toHaveURL(/\/register/);
  });
});
