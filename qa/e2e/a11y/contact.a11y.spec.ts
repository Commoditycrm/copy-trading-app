import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// CONTACT-A11Y-001 — labels are associated via <Field htmlFor>, so this should
// pass; the ambient gradient background makes contrast worth checking too.
test("a11y: /contact has no serious/critical axe violations", async ({ page }, testInfo) => {
  await page.goto("/contact");
  await page.waitForLoadState("networkidle");
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const blocking = results.violations.filter(
    (v) => v.impact === "serious" || v.impact === "critical",
  );
  await testInfo.attach("axe-violations.json", {
    body: JSON.stringify(results.violations, null, 2),
    contentType: "application/json",
  });
  const summary = blocking.map((v) => `${v.impact}: ${v.id} (${v.nodes.length}) — ${v.help}`).join("\n");
  expect(blocking, `Serious/critical a11y issues on /contact:\n${summary}`).toEqual([]);
});
