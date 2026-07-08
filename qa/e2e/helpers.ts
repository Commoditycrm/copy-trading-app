import { APIRequestContext, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

// Direct backend for arrange steps (bypasses the Next proxy — faster, no UI).
export const API = process.env.E2E_API_URL || "http://localhost:8000";

// Backend writes email links here (SENDGRID_API_KEY blank → log instead of send).
const BACKEND_LOG =
  process.env.E2E_BACKEND_LOG ||
  "C:/Users/IRFANH~1/AppData/Local/Temp/claude/C--Users-Irfan-H/34376392-3ac0-4b29-a06d-e9703e156ec5/scratchpad/e2e_backend.log";

// Meets register policy: 8+ chars, >=3 of {lower,upper,digit,symbol}.
export const STRONG_PW = "Str0ng!pw";

let seq = 0;
export function uniqueEmail(prefix = "u"): string {
  seq += 1;
  return `qa.${prefix}.${Date.now()}.${seq}@qatest.io`;
}

// Unique source IP per arrange-call so bulk test setup doesn't exhaust the
// real per-IP register/login rate limiters (backend trusts X-Forwarded-For).
function spoofIpHeader(): Record<string, string> {
  seq += 1;
  const a = 10 + (seq % 200);
  return { "X-Forwarded-For": `10.${a}.${(seq * 7) % 256}.${(seq * 13) % 256}` };
}

export async function registerViaApi(
  request: APIRequestContext,
  opts: { email: string; password?: string; role?: "subscriber" | "trader"; businessName?: string },
) {
  const body: Record<string, unknown> = {
    email: opts.email,
    password: opts.password ?? STRONG_PW,
    role: opts.role ?? "subscriber",
  };
  if ((opts.role ?? "subscriber") === "trader") body.business_name = opts.businessName ?? "QA Capital";
  const r = await request.post(`${API}/api/auth/register`, { data: body, headers: spoofIpHeader() });
  expect(r.status(), await r.text()).toBe(201);
  return r.json();
}

export async function loginViaApi(request: APIRequestContext, email: string, password = STRONG_PW) {
  const r = await request.post(`${API}/api/auth/login`, { data: { email, password }, headers: spoofIpHeader() });
  expect(r.status(), await r.text()).toBe(200);
  return r.json() as Promise<{ access_token: string; refresh_token: string }>;
}

/**
 * Pull the most recent reset/verify link for `email` out of the backend log.
 * The email service (keyless) logs `to=<email>` then `reset_link=`/`verify_link=`.
 * Polls because the send runs as a FastAPI BackgroundTask (fires after the response).
 */
export async function waitForEmailLink(
  email: string,
  kind: "reset_link" | "verify_link",
  timeoutMs = 8000,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  const re = new RegExp(`${kind}=(\\S+)`);
  while (Date.now() < deadline) {
    let text = "";
    try {
      text = readFileSync(BACKEND_LOG, "utf8");
    } catch {
      /* log may not be flushed yet */
    }
    // Find the last block whose `to=` matches this email, then its link line.
    const lines = text.split(/\r?\n/);
    let lastTo = "";
    let found = "";
    for (const line of lines) {
      const toM = line.match(/\bto=(\S+)/);
      if (toM) lastTo = toM[1];
      const linkM = line.match(re);
      if (linkM && lastTo.toLowerCase() === email.toLowerCase()) found = linkM[1];
    }
    if (found) return found;
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`No ${kind} logged for ${email} within ${timeoutMs}ms`);
}

/**
 * Seed localStorage tokens so a subsequent navigation loads authenticated.
 * One-time (not addInitScript): re-seeding on every load would fight the app's
 * own clearTokens() on a 401 and cause a redirect loop in the stale-token case.
 */
export async function seedTokens(page: import("@playwright/test").Page, access: string, refresh: string) {
  await page.goto("/login"); // any same-origin page so localStorage is available
  await page.evaluate(
    ([a, r]) => {
      localStorage.setItem("trading-app:access", a);
      localStorage.setItem("trading-app:refresh", r);
    },
    [access, refresh],
  );
}
