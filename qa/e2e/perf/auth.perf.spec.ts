import { test, expect } from "@playwright/test";

// Local dev-server perf smoke (NOT a production Lighthouse audit — Next dev
// mode is uncompiled/slower). We warm the route first so on-demand compilation
// doesn't skew the measured navigation, then assert generous local budgets and
// record the actuals as an attachment. Tighten these against a production build.
const BUDGETS = { ttfbMs: 2000, domContentLoadedMs: 5000, loadMs: 7000, lcpMs: 5000 };

const ROUTES = ["/login", "/register"];

for (const route of ROUTES) {
  test(`perf: ${route} within local budgets`, async ({ page }, testInfo) => {
    await page.goto(route); // warm (triggers dev compile)
    await page.waitForLoadState("load");

    await page.goto(route, { waitUntil: "load" }); // measured pass (compiled)
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
    await testInfo.attach(`perf-${route.replace(/\W/g, "_")}.json`, {
      body: JSON.stringify({ route, actual, budgets: BUDGETS }, null, 2),
      contentType: "application/json",
    });
    // eslint-disable-next-line no-console
    console.log(`PERF ${route}:`, JSON.stringify(actual));

    expect(nav.ttfb).toBeLessThan(BUDGETS.ttfbMs);
    expect(nav.domContentLoaded).toBeLessThan(BUDGETS.domContentLoadedMs);
    expect(nav.load).toBeLessThan(BUDGETS.loadMs);
    if (lcp > 0) expect(lcp).toBeLessThan(BUDGETS.lcpMs);
  });
}
