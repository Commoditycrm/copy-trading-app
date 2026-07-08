"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { CheckCircle2, AlertTriangle } from "lucide-react";
import { verifyEmail, verifyEmailChange, getAccessToken } from "@/lib/api";
import { Spinner } from "@/components/Spinner";
import { AuthCard } from "@/components/auth/AuthCard";

// Keep in sync with AppShell's USER_CACHE_KEY — busting it forces a fresh
// /api/auth/me so the "verify your email" banner clears after verifying.
const USER_CACHE_KEY = "trading-app:user";

type State = "verifying" | "success" | "error";

function VerifyEmailInner() {
  const params = useSearchParams();
  const token = params.get("token") ?? "";
  // ?change=1 → this link confirms an email *change* (a different endpoint +
  // copy) rather than an initial signup verification.
  const isChange = params.get("change") === "1";
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
    (isChange ? verifyEmailChange(token) : verifyEmail(token))
      .then((r) => {
        setState("success");
        setMessage(r.detail || (isChange ? "Your email has been updated." : "Your email has been verified."));
        // Bust the cached user so the app shell re-fetches /me — drops the
        // "verify your email" banner, and reflects a changed email.
        try { sessionStorage.removeItem(USER_CACHE_KEY); } catch {}
      })
      .catch(() => {
        setState("error");
        setMessage("This link is invalid or has expired.");
      });
  }, [token, isChange]);

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
          <div className="flex justify-center" style={{ color: "var(--good)" }}><CheckCircle2 size={40} /></div>
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
          <div className="flex justify-center" style={{ color: "var(--warn)" }}><AlertTriangle size={40} /></div>
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
    <AuthCard title="Email verification">
      <Suspense fallback={<div className="text-center"><Spinner /></div>}>
        <VerifyEmailInner />
      </Suspense>
    </AuthCard>
  );
}
