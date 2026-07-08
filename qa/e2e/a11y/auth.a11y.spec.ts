import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// WCAG 2.0/2.1 A + AA. We fail on serious/critical only — moderate/minor are
// reported (attached) but don't block, matching the Phase-2 a11y strategy.
const PAGES = [
  { name: "login", path: "/login" },
  { name: "register", path: "/register" },
  { name: "forgot-password", path: "/forgot-password" },
  { name: "reset-password (no token)", path: "/reset-password" },
  { name: "verify-email (no token)", path: "/verify-email" },
];

for (const p of PAGES) {
  test(`a11y: ${p.name} has no serious/critical axe violations`, async ({ page }, testInfo) => {
    await page.goto(p.path);
    await page.waitForLoadState("networkidle");
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();

    const blocking = results.violations.filter(
      (v) => v.impact === "serious" || v.impact === "critical",
    );
    // Attach the full findings (all impacts) for the report.
    await testInfo.attach("axe-violations.json", {
      body: JSON.stringify(results.violations, null, 2),
      contentType: "application/json",
    });
    const summary = blocking
      .map((v) => `${v.impact}: ${v.id} (${v.nodes.length}) — ${v.help}`)
      .join("\n");
    expect(blocking, `Serious/critical a11y issues on ${p.path}:\n${summary}`).toEqual([]);
  });
}
