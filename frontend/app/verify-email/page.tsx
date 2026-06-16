"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { verifyEmail, getAccessToken } from "@/lib/api";
import { Spinner } from "@/components/Spinner";

// Keep in sync with AppShell's USER_CACHE_KEY — busting it forces a fresh
// /api/auth/me so the "verify your email" banner clears after verifying.
const USER_CACHE_KEY = "trading-app:user";

type State = "verifying" | "success" | "error";

function VerifyEmailInner() {
  const token = useSearchParams().get("token") ?? "";
  const [state, setState] = useState<State>(token ? "verifying" : "error");
  const [message, setMessage] = useState<string>(
    token ? "" : "This verification link is invalid or incomplete.",
  );
  // Whether the visitor already has a session — drives where "Continue" goes
  // (back into the app vs. to the login form). Read after mount to avoid an
  // SSR/hydration mismatch.
  const [loggedIn, setLoggedIn] = useState(false);
  useEffect(() => { setLoggedIn(!!getAccessToken()); }, []);

  // Guard against React 18 StrictMode double-invoke in dev.
  const ran = useRef(false);

  useEffect(() => {
    if (!token || ran.current) return;
    ran.current = true;
    verifyEmail(token)
      .then((r) => {
        setState("success");
        setMessage(r.detail || "Your email has been verified.");
        // Bust the cached user so the app shell re-fetches /me and drops the
        // "verify your email" banner the next time they land in the app.
        try { sessionStorage.removeItem(USER_CACHE_KEY); } catch {}
      })
      .catch(() => {
        setState("error");
        setMessage("This verification link is invalid or has expired.");
      });
  }, [token]);

  return (
    <div className="space-y-5 text-center">
      {state === "verifying" && (
        <>
          <div className="flex justify-center"><Spinner /></div>
          <p className="text-sm" style={{ color: "var(--muted)" }}>Verifying your email…</p>
        </>
      )}

      {state === "success" && (
        <>
          <div style={{ fontSize: 40 }}>✅</div>
          <p className="text-sm" style={{ color: "var(--muted)" }}>{message}</p>
          <Link
            href={loggedIn ? "/" : "/login"}
            className="btn-primary w-full py-2.5 text-sm inline-flex items-center justify-center"
          >
            {loggedIn ? "Continue to app" : "Continue to sign in"}
          </Link>
        </>
      )}

      {state === "error" && (
        <>
          <div style={{ fontSize: 40 }}>⚠️</div>
          <p className="text-sm" style={{ color: "var(--muted)" }}>{message}</p>
          <p className="text-sm" style={{ color: "var(--muted)" }}>
            {loggedIn
              ? "Use the “Resend” option on the banner to get a fresh link."
              : "Sign in and use the “Resend” option on the banner to get a fresh link."}
          </p>
          <Link
            href={loggedIn ? "/" : "/login"}
            className="btn-primary w-full py-2.5 text-sm inline-flex items-center justify-center"
          >
            {loggedIn ? "Back to app" : "Back to sign in"}
          </Link>
        </>
      )}
    </div>
  );
}

export default function VerifyEmailPage() {
  return (
    <main className="min-h-screen grid place-items-center p-6">
      <div className="card w-full max-w-md p-8 space-y-5">
        <div className="text-center">
          <div style={{ fontWeight: 700, fontSize: 24, letterSpacing: "0.02em" }}>
            Email verification
          </div>
        </div>
        <Suspense fallback={<div className="text-center"><Spinner /></div>}>
          <VerifyEmailInner />
        </Suspense>
      </div>
    </main>
  );
}
