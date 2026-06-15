"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { verifyEmail } from "@/lib/api";
import { Spinner } from "@/components/Spinner";

type State = "verifying" | "success" | "error";

function VerifyEmailInner() {
  const token = useSearchParams().get("token") ?? "";
  const [state, setState] = useState<State>(token ? "verifying" : "error");
  const [message, setMessage] = useState<string>(
    token ? "" : "This verification link is invalid or incomplete.",
  );
  // Guard against React 18 StrictMode double-invoke in dev.
  const ran = useRef(false);

  useEffect(() => {
    if (!token || ran.current) return;
    ran.current = true;
    verifyEmail(token)
      .then((r) => {
        setState("success");
        setMessage(r.detail || "Your email has been verified.");
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
            href="/login"
            className="btn-primary w-full py-2.5 text-sm inline-flex items-center justify-center"
          >
            Continue to sign in
          </Link>
        </>
      )}

      {state === "error" && (
        <>
          <div style={{ fontSize: 40 }}>⚠️</div>
          <p className="text-sm" style={{ color: "var(--muted)" }}>{message}</p>
          <p className="text-sm" style={{ color: "var(--muted)" }}>
            Sign in and use the “Resend” option on the banner to get a fresh link.
          </p>
          <Link
            href="/login"
            className="btn-primary w-full py-2.5 text-sm inline-flex items-center justify-center"
          >
            Back to sign in
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
