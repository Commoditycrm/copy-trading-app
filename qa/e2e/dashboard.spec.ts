import { test, expect } from "@playwright/test";
import { registerViaApi, loginViaApi, seedTokens, uniqueEmail } from "./helpers";
import { seedBroker, setFollowing } from "./db";

async function trader(page: any, request: any, prefix: string) {
  const email = uniqueEmail(prefix);
  const user = await registerViaApi(request, { email, role: "trader", businessName: "QA Capital" });
  const tok = await loginViaApi(request, email);
  return { user, tok };
}

test.describe("Trader Dashboard", () => {
  // DASH-FUNC-001 — loads with trader chrome
  test("loads for a trader", async ({ page, request }) => {
    const { tok } = await trader(page, request, "dash-load");
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/dashboard");
    await expect(page.getByText(/trader overview/i)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/total equity/i)).toBeVisible();
    await expect(page.getByText(/active subscribers/i)).toBeVisible();
  });

  // DASH-BND-001 — zero brokers
  test("zero brokers → $0.00 across 0 brokers", async ({ page, request }) => {
    const { tok } = await trader(page, request, "dash-zero");
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/dashboard");
    await expect(page.getByText(/across 0 brokers/i)).toBeVisible({ timeout: 15_000 });
  });

  // DASH-FUNC-002 — total equity = Σ broker equity
  test("total equity sums seeded broker balances", async ({ page, request }) => {
    const { user, tok } = await trader(page, request, "dash-equity");
    await seedBroker(user.id, { equity: 1000, buyingPower: 500 });
    await seedBroker(user.id, { equity: 2500, buyingPower: 1000 });
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/dashboard");
    await expect(page.getByText(/across 2 brokers/i)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/\$?3,500/)).toBeVisible({ timeout: 10_000 });
  });

  // DASH-FUNC-004 — active subscribers KPI
  test("active subscribers reflects copy-enabled followers", async ({ page, request }) => {
    const { user, tok } = await trader(page, request, "dash-subs");
    for (let i = 0; i < 3; i++) {
      const s = await registerViaApi(request, { email: uniqueEmail(`dash-sub-${i}`) });
      await setFollowing(s.id, user.id, i < 2); // 2 copying, 1 not
    }
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.goto("/dashboard");
    await expect(page.getByText(/2\/3 copying/i)).toBeVisible({ timeout: 15_000 });
  });

  // DASH-RECOV-001 — a failing sub-fetch degrades gracefully (page still renders)
  test("degrades gracefully when /positions fails", async ({ page, request }) => {
    const { tok } = await trader(page, request, "dash-degrade");
    await seedTokens(page, tok.access_token, tok.refresh_token);
    await page.route("**/api/positions", (route) =>
      route.fulfill({ status: 500, contentType: "application/json", body: "{}" }),
    );
    await page.goto("/dashboard");
    // Not the error card — shell + KPIs still render; positions just 0.
    await expect(page.getByText(/trader overview/i)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/open positions/i)).toBeVisible();
    await expect(page.getByText(/could not load dashboard data/i)).toHaveCount(0);
  });
});
