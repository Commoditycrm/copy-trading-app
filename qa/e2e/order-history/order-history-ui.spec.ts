import { test, expect } from "@playwright/test";
import { registerViaApi, loginViaApi, seedTokens, uniqueEmail } from "../helpers";
import { seedOrder } from "../db";

test.describe("Order History · UI", () => {
  // OH-UI-001 — renders summary tiles + seeded orders
  test("renders orders and the Filled notional summary", async ({ page, request }) => {
    const email = uniqueEmail("ohui");
    const u = await registerViaApi(request, { email, role: "trader", businessName: "QA Cap" });
    await seedOrder(u.id, { symbol: "ZLAB", status: "filled", quantity: 3, filledAvgPrice: 100 });
    const tok = await loginViaApi(request, email);
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/trades");
    await expect(page.getByText(/filled notional/i)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("ZLAB").first()).toBeVisible();
  });
});
