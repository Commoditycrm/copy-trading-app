"use client";

import { motion } from "framer-motion";
import { CandlestickChart } from "lucide-react";
import { ThemeToggle } from "@/components/theme/ThemeToggle";

/**
 * Shared shell for every unauthenticated page (login, register, password reset,
 * email verification). Provides the brand mark, title/subtitle, an entrance
 * animation, a corner theme toggle (so logged-out users can switch light/dark),
 * and an elevated card. Purely presentational — pages keep all their own logic.
 */
export function AuthCard({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  return (
    <main className="min-h-screen grid place-items-center p-6 relative">
      <div className="absolute top-4 right-4">
        <ThemeToggle />
      </div>

      <motion.div
        initial={{ opacity: 0, y: 14, scale: 0.985 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
        className="w-full max-w-md"
      >
        {/* Brand mark */}
        <div className="flex flex-col items-center mb-6">
          <div
            className="grid place-items-center rounded-token"
            style={{
              width: 46,
              height: 46,
              background: "var(--grad-accent)",
              color: "var(--accent-ink)",
              boxShadow: "0 10px 24px -10px var(--accent-glow)",
            }}
            aria-hidden
          >
            <CandlestickChart size={24} />
          </div>
          <div className="mt-2.5 text-[13px] font-semibold tracking-[0.18em] uppercase" style={{ color: "var(--muted)" }}>
            Copy Trading
          </div>
        </div>

        <div className="card p-8 space-y-5" style={{ boxShadow: "var(--shadow-pop)" }}>
          <div className="text-center space-y-1.5">
            <h1 className="text-xl font-semibold tracking-tight" style={{ color: "var(--text)" }}>
              {title}
            </h1>
            {subtitle && (
              <p className="text-sm" style={{ color: "var(--muted)" }}>{subtitle}</p>
            )}
          </div>
          {children}
        </div>

        {footer && (
          <div className="text-center text-sm mt-5" style={{ color: "var(--muted)" }}>
            {footer}
          </div>
        )}
      </motion.div>
    </main>
  );
}
