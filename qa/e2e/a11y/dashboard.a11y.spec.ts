import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { registerViaApi, loginViaApi, seedTokens, uniqueEmail } from "../helpers";
import { seedBroker } from "../db";

// DASH-A11Y-001 — Recharts SVG + KPI cards; watch contrast + chart roles.
test("a11y: /dashboard (trader) has no serious/critical axe violations", async ({ page, request }, testInfo) => {
  const email = uniqueEmail("a11y-dash");
  const user = await registerViaApi(request, { email, role: "trader", businessName: "QA Capital" });
  await seedBroker(user.id, { equity: 1234 });
  const tok = await loginViaApi(request, email);
  await seedTokens(page, tok.access_token, tok.refresh_token);
  await page.goto("/dashboard");
  await page.getByText(/trader overview/i).waitFor({ timeout: 15_000 });
  await page.waitForLoadState("networkidle");

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  await testInfo.attach("axe-violations.json", {
    body: JSON.stringify(results.violations, null, 2),
    contentType: "application/json",
  });
  const summary = blocking.map((v) => `${v.impact}: ${v.id} (${v.nodes.length}) — ${v.help}`).join("\n");
  expect(blocking, `Serious/critical a11y issues on /dashboard:\n${summary}`).toEqual([]);
});
