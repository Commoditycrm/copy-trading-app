"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError, clearTokens, getAccessToken } from "@/lib/api";
import type { User } from "@/lib/types";

/**
 * Root landing route. Decides where to send the user based on auth + role:
 *   - not logged in → /login
 *   - admin         → /admin      (their own panel)
 *   - everyone else → /dashboard  (role-aware overview)
 */
export default function Home() {
  const router = useRouter();
  useEffect(() => {
    if (!getAccessToken()) {
      router.replace("/login");
      return;
    }
    api<User>("/api/auth/me")
      .then((u) => {
        router.replace(u.role === "admin" ? "/admin" : "/dashboard");
      })
      .catch((e) => {
        // Stale/invalid token — clear and bounce to login.
        if (e instanceof ApiError && e.status === 401) clearTokens();
        router.replace("/login");
      });
  }, [router]);
  return null;
}
