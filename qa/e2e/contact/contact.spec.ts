import { test, expect } from "@playwright/test";

// Public, backend-less page. FORM_ENDPOINT="" → mailto fallback (no network).
test.describe("Contact", () => {
  // CONTACT-FUNC-001 / SMOKE-001
  test("loads publicly (no auth) with the form", async ({ page }) => {
    await page.goto("/contact");
    await expect(page.getByRole("heading", { name: /get in touch/i })).toBeVisible();
    await expect(page.locator("#name")).toBeVisible();
    await expect(page.locator("#email")).toBeVisible();
    await expect(page.locator("#message")).toBeVisible();
    await expect(page.getByRole("button", { name: /send message/i })).toBeVisible();
  });

  // CONTACT-UI-001 — key layout pieces + brand
  test("renders info cards, brand and footer", async ({ page }) => {
    await page.goto("/contact");
    await expect(page.getByText(/within 2 business days/i)).toBeVisible();
    await expect(page.getByText(/we never ask for passwords/i)).toBeVisible();
    await expect(page.locator("header").getByText("ARK")).toBeVisible();
  });

  // CONTACT-FUNC-002 — valid submit → mailto path → sent + reset
  test("valid submit shows 'sent' and resets the form", async ({ page }) => {
    await page.goto("/contact");
    await page.locator("#name").fill("QA Tester");
    await page.locator("#email").fill("qa@qatest.io");
    await page.locator("#message").fill("Hello from E2E");
    await page.getByRole("button", { name: /send message/i }).click();
    await expect(page.getByText(/your message is on its way/i)).toBeVisible();
    await expect(page.locator("#name")).toHaveValue("");
    await expect(page.locator("#message")).toHaveValue("");
  });

  // CONTACT-BIZ-001 / SEC-003 — mailto fallback makes no external network call
  test("submit makes no external network request (mailto fallback)", async ({ page }) => {
    const external: string[] = [];
    page.on("request", (r) => {
      const u = r.url();
      if (!u.includes("localhost") && !u.startsWith("data:") && !u.startsWith("mailto:")) external.push(u);
    });
    await page.goto("/contact");
    await page.locator("#name").fill("QA");
    await page.locator("#email").fill("qa@qatest.io");
    await page.locator("#message").fill("hi");
    await page.getByRole("button", { name: /send message/i }).click();
    await expect(page.getByText(/your message is on its way/i)).toBeVisible();
    expect(external).toEqual([]);
  });

  // CONTACT-VAL-001 — empty required fields blocked by native validation
  test("empty required fields block submit", async ({ page }) => {
    await page.goto("/contact");
    await page.getByRole("button", { name: /send message/i }).click();
    await expect(page).toHaveURL(/\/contact/);
    expect(await page.locator("#name").evaluate((el: HTMLInputElement) => el.matches(":invalid"))).toBe(true);
  });

  // CONTACT-VAL-002 — invalid email format blocked
  test("invalid email format is blocked", async ({ page }) => {
    await page.goto("/contact");
    await page.locator("#name").fill("QA");
    await page.locator("#email").fill("not-an-email");
    await page.locator("#message").fill("hi");
    await page.getByRole("button", { name: /send message/i }).click();
    expect(await page.locator("#email").evaluate((el: HTMLInputElement) => el.matches(":invalid"))).toBe(true);
  });

  // CONTACT-FUNC-003 — support mailto + home links present
  test("support email and home links resolve", async ({ page }) => {
    await page.goto("/contact");
    await expect(page.locator('a[href^="mailto:support@kopyya.com"]').first()).toBeVisible();
    await expect(page.locator('a[href="/"]').first()).toBeVisible();
  });

  // CONTACT-SEC-001 — field payloads are not executed / reflected (they only
  // ever enter an encoded mailto, never the DOM)
  test("XSS payload in fields does not execute or inject", async ({ page }) => {
    let dialog = false;
    page.on("dialog", (d) => {
      dialog = true;
      d.dismiss().catch(() => {});
    });
    await page.goto("/contact");
    await page.locator("#name").fill('"><img src=x onerror=alert(1)>');
    await page.locator("#email").fill("qa@qatest.io");
    await page.locator("#message").fill("<script>alert(1)</script>");
    await page.getByRole("button", { name: /send message/i }).click();
    await expect(page.getByText(/your message is on its way/i)).toBeVisible();
    expect(dialog).toBe(false);
    expect(await page.locator('img[src="x"]').count()).toBe(0);
  });
});
