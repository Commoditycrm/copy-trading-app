"use client";

const TOKEN_KEY = "trading-app:access";
const REFRESH_KEY = "trading-app:refresh";

export function setTokens(access: string, refresh: string) {
  localStorage.setItem(TOKEN_KEY, access);
  localStorage.setItem(REFRESH_KEY, refresh);
}

export function clearTokens() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(REFRESH_KEY);
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

// Coalesce concurrent refreshes so a burst of 401s only triggers one /refresh call.
let refreshInFlight: Promise<boolean> | null = null;

async function tryRefresh(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  const refresh = getRefreshToken();
  if (!refresh) return false;
  refreshInFlight = (async () => {
    try {
      const r = await fetch(
        `/api/auth/refresh`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refresh }),
        }
      );
      if (!r.ok) { clearTokens(); return false; }
      const data = await r.json() as { access_token: string; refresh_token: string };
      setTokens(data.access_token, data.refresh_token);
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

export async function api<T>(
  path: string,
  init: RequestInit & { auth?: boolean } = {}
): Promise<T> {
  const { auth = true, headers, ...rest } = init;

  const send = async () => {
    const h = new Headers(headers);
    if (!h.has("Content-Type") && rest.body) h.set("Content-Type", "application/json");
    if (auth) {
      const tok = getAccessToken();
      if (tok) h.set("Authorization", `Bearer ${tok}`);
    }
    return fetch(path, { ...rest, headers: h });
  };

  let r = await send();
  if (r.status === 401 && auth && getRefreshToken() && path !== "/api/auth/refresh") {
    if (await tryRefresh()) r = await send();
  }
  if (r.status === 204) return undefined as T;
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new ApiError(r.status, data?.detail ?? data ?? r.statusText);
  return data as T;
}

// ── Password reset ──────────────────────────────────────────────────────────

/** Request a reset link. Always resolves (the API never reveals whether the
 * email exists), so the caller can show the same confirmation regardless. */
export async function forgotPassword(email: string): Promise<{ detail: string }> {
  return api("/api/auth/forgot-password", {
    method: "POST",
    body: JSON.stringify({ email }),
    auth: false,
  });
}

/** Complete a reset with the token from the emailed link + a new password. */
export async function resetPassword(
  token: string,
  newPassword: string,
): Promise<{ detail: string }> {
  return api("/api/auth/reset-password", {
    method: "POST",
    body: JSON.stringify({ token, new_password: newPassword }),
    auth: false,
  });
}

// ── Email verification ──────────────────────────────────────────────────────

/** Confirm an email address using the token from the verification link. */
export async function verifyEmail(token: string): Promise<{ detail: string }> {
  return api("/api/auth/verify-email", {
    method: "POST",
    body: JSON.stringify({ token }),
    auth: false,
  });
}

/** Re-send the verification email. Always resolves (no account enumeration). */
export async function resendVerification(email: string): Promise<{ detail: string }> {
  return api("/api/auth/resend-verification", {
    method: "POST",
    body: JSON.stringify({ email }),
    auth: false,
  });
}
