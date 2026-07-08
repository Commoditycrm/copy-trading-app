import { test, expect } from "@playwright/test";
import { API, registerViaApi, loginViaApi, waitForEmailLink, uniqueEmail, STRONG_PW } from "../helpers";

test.describe("Auth · Reset Password", () => {
  // AUTH-RESET-UI-001 — no token → invalid-link screen
  test("missing token shows the invalid-link screen", async ({ page }) => {
    await page.goto("/reset-password");
    await expect(page.getByText(/invalid or incomplete/i)).toBeVisible();
    await expect(page.getByRole("link", { name: /request a new link/i })).toBeVisible();
  });

  // AUTH-RESET-VAL-001 — mismatched confirmation is blocked client-side
  test("password mismatch shows a client toast", async ({ page }) => {
    await page.goto("/reset-password?token=dummy-token");
    const pw = page.locator('input[type="password"]');
    await pw.nth(0).fill(STRONG_PW);
    await pw.nth(1).fill("Different!9");
    await page.getByRole("button", { name: /reset password/i }).click();
    await expect(page.locator(".Toastify__toast")).toContainText(/don.?t match/i);
  });

  // AUTH-RESET-FUNC-001 — full flow: forgot → capture token → reset → login with new pw
  test("full reset with a real token, then login with the new password", async ({ page, request }) => {
    const email = uniqueEmail("reset");
    await registerViaApi(request, { email });
    // Trigger the reset email (logged, not sent).
    await request.post(`${API}/api/auth/forgot-password`, { data: { email } });
    const link = await waitForEmailLink(email, "reset_link");
    const token = new URL(link).searchParams.get("token")!;
    expect(token).toBeTruthy();

    const newPw = "Fresh!Pw99";
    await page.goto(`/reset-password?token=${encodeURIComponent(token)}`);
    const pw = page.locator('input[type="password"]');
    await pw.nth(0).fill(newPw);
    await pw.nth(1).fill(newPw);
    await page.getByRole("button", { name: /reset password/i }).click();
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });

    // New password authenticates; old one no longer does.
    await loginViaApi(request, email, newPw);
    const old = await request.post(`${API}/api/auth/login`, { data: { email, password: STRONG_PW } });
    expect(old.status()).toBe(401);
  });
});
