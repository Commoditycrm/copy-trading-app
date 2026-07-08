import { test, expect } from "@playwright/test";
import { registerViaApi, waitForEmailLink, uniqueEmail } from "../helpers";

test.describe("Auth · Verify Email", () => {
  // AUTH-VERIFY-NEG-005 — no token → immediate error state
  test("missing token shows the error state", async ({ page }) => {
    await page.goto("/verify-email");
    await expect(page.getByText(/invalid or incomplete/i)).toBeVisible();
  });

  // AUTH-VERIFY-NEG-001 — garbage token → error state
  test("invalid token shows the error state", async ({ page }) => {
    await page.goto("/verify-email?token=not-a-real-token");
    await expect(page.getByText(/invalid or has expired/i)).toBeVisible({ timeout: 10_000 });
  });

  // AUTH-VERIFY-FUNC-002 — full flow: register → capture verify token → success state
  test("valid token verifies the email", async ({ page, request }) => {
    const email = uniqueEmail("verify");
    await registerViaApi(request, { email }); // registration queues the verification email
    const link = await waitForEmailLink(email, "verify_link");
    const token = new URL(link).searchParams.get("token")!;
    expect(token).toBeTruthy();
    await page.goto(`/verify-email?token=${encodeURIComponent(token)}`);
    await expect(page.getByText(/your email has been verified/i)).toBeVisible({ timeout: 10_000 });
  });
});
