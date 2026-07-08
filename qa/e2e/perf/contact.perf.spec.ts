import { test, expect } from "@playwright/test";

// Local dev-server perf smoke for the public /contact page (heavy CSS gradients
// + backdrop-filter — worth watching paint). Warm first, then measure.
const BUDGETS = { ttfbMs: 2000, domContentLoadedMs: 5000, loadMs: 7000, lcpMs: 5000 };

test("perf: /contact within local budgets", async ({ page }, testInfo) => {
  await page.goto("/contact");
  await page.waitForLoadState("load");

  await page.goto("/contact", { waitUntil: "load" });
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
        setTimeout(() => resolve(-1), 4000);
      }),
  );

  const actual = { ...nav, lcp };
  await testInfo.attach("perf-contact.json", {
    body: JSON.stringify({ route: "/contact", actual, budgets: BUDGETS }, null, 2),
    contentType: "application/json",
  });
  // eslint-disable-next-line no-console
  console.log("PERF /contact:", JSON.stringify(actual));

  expect(nav.ttfb).toBeLessThan(BUDGETS.ttfbMs);
  expect(nav.domContentLoaded).toBeLessThan(BUDGETS.domContentLoadedMs);
  expect(nav.load).toBeLessThan(BUDGETS.loadMs);
  if (lcp > 0) expect(lcp).toBeLessThan(BUDGETS.lcpMs);
});
