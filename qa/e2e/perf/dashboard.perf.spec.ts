import { test, expect } from "@playwright/test";
import { registerViaApi, loginViaApi, seedTokens, uniqueEmail } from "../helpers";
import { seedBroker } from "../db";

// DASH-PERF-001 — authed dashboard load (client-side data fetch + charts).
// Generous local-dev budgets; records actuals. Not a production Lighthouse audit.
const BUDGETS = { ttfbMs: 3000, domContentLoadedMs: 8000, loadMs: 12000, lcpMs: 9000 };

test("perf: /dashboard within local budgets", async ({ page, request }, testInfo) => {
  const email = uniqueEmail("perf-dash");
  const user = await registerViaApi(request, { email, role: "trader", businessName: "QA Capital" });
  await seedBroker(user.id, { equity: 1000 });
  const tok = await loginViaApi(request, email);
  await seedTokens(page, tok.access_token, tok.refresh_token);

  await page.goto("/dashboard"); // warm (dev compile)
  await page.getByText(/trader overview/i).waitFor({ timeout: 20_000 });

  await page.goto("/dashboard", { waitUntil: "load" });
  const nav = await page.evaluate(() => {
    const n = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming;
    return {
      ttfb: n.responseStart - n.requestStart,
      domContentLoaded: n.domContentLoadedEventEnd - n.startTime,
      load: n.loadEventEnd - n.startTime,
    };
  });
  const lcp = await page.evaluate(
    () =>
      new Promise<number>((resolve) => {
        new PerformanceObserver((list) => {
          const e = list.getEntries();
          resolve(e[e.length - 1].startTime);
        }).observe({ type: "largest-contentful-paint", buffered: true });
        setTimeout(() => resolve(-1), 5000);
      }),
  );

  const actual = { ...nav, lcp };
  await testInfo.attach("perf-dashboard.json", {
    body: JSON.stringify({ route: "/dashboard", actual, budgets: BUDGETS }, null, 2),
    contentType: "application/json",
  });
  // eslint-disable-next-line no-console
  console.log("PERF /dashboard:", JSON.stringify(actual));

  expect(nav.ttfb).toBeLessThan(BUDGETS.ttfbMs);
  expect(nav.load).toBeLessThan(BUDGETS.loadMs);
  if (lcp > 0) expect(lcp).toBeLessThan(BUDGETS.lcpMs);
});
