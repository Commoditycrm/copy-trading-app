import { test, expect } from "@playwright/test";
import { registerViaApi, loginViaApi, seedTokens, uniqueEmail } from "../helpers";

async function traderPage(page: any, request: any, prefix: string) {
  const email = uniqueEmail(prefix);
  await registerViaApi(request, { email, role: "trader", businessName: "QA Cap" });
  const tok = await loginViaApi(request, email);
  await seedTokens(page, tok.access_token, tok.refresh_token);
}

test.describe("Trade Panel · UI", () => {
  // TP-UI-001 — renders the ticket controls (toggle names are lowercase)
  test("renders instrument toggle + Buy/Sell CTAs", async ({ page, request }) => {
    await traderPage(page, request, "tpui");
    await page.goto("/trade-panel");
    await expect(page.getByRole("button", { name: /^options$/i })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("button", { name: /^stocks$/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /buy/i }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: /sell/i }).first()).toBeVisible();
  });

  // TP-UI-002 — with no broker connected, the CTAs are disabled (title explains why)
  test("CTAs are disabled with 'Connect a broker first' when no broker", async ({ page, request }) => {
    await traderPage(page, request, "tpui-nobroker");
    await page.goto("/trade-panel");
    const buy = page.getByRole("button", { name: /buy/i }).first();
    await expect(buy).toBeDisabled({ timeout: 15_000 });
    await expect(buy).toHaveAttribute("title", /connect a broker/i);
  });
});
