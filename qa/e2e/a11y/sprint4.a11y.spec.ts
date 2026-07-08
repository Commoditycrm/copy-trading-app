import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { registerViaApi, loginViaApi, seedTokens, uniqueEmail } from "../helpers";
import { seedOrder, seedFakeBroker } from "../db";

// a11y for the trading-core pages (trader). Seed a broker + an order so the
// pages render populated states, not just empty ones.
const PAGES = ["/trade-panel", "/positions", "/trades", "/calendar"];

for (const path of PAGES) {
  test(`a11y: ${path} has no serious/critical axe violations`, async ({ page, request }, testInfo) => {
    const email = uniqueEmail(`a11y-s4`);
    const u = await registerViaApi(request, { email, role: "trader", businessName: "QA Cap" });
    await seedFakeBroker(u.id);
    await seedOrder(u.id, { symbol: "AXE", status: "filled", quantity: 2, filledAvgPrice: 100 });
    const tok = await loginViaApi(request, email);
    await seedTokens(page, tok.access_token, tok.refresh_token);

    await page.goto(path);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(1200); // let client render settle

    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();
    const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    await testInfo.attach("axe-violations.json", {
      body: JSON.stringify(results.violations, null, 2),
      contentType: "application/json",
    });
    const summary = blocking.map((v) => `${v.impact}: ${v.id} (${v.nodes.length}) — ${v.help}`).join("\n");
    expect(blocking, `Serious/critical a11y issues on ${path}:\n${summary}`).toEqual([]);
  });
}
