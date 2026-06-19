"use client";

import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Moon, Sun } from "lucide-react";
import { useTheme } from "./ThemeProvider";

/**
 * Header theme switch. Animated sun/moon crossfade (Framer Motion). Renders a
 * static placeholder until mounted to avoid a hydration mismatch (the actual
 * theme is only known on the client).
 */
export function ThemeToggle({ className = "" }: { className?: string }) {
  const { theme, toggle } = useTheme();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const isDark = theme === "dark";
  const label = isDark ? "Switch to light mode" : "Switch to dark mode";

  return (
    <button
      type="button"
      onClick={toggle}
      title={label}
      aria-label={label}
      className={`relative grid place-items-center rounded-full transition-colors focus-ring overflow-hidden ${className}`}
      style={{
        width: 32,
        height: 32,
        background: "var(--chip-bg)",
        border: "1px solid var(--border)",
        color: "var(--text-2)",
      }}
    >
      <AnimatePresence initial={false} mode="wait">
        {mounted && (
          <motion.span
            key={isDark ? "moon" : "sun"}
            initial={{ y: 14, opacity: 0, rotate: -30 }}
            animate={{ y: 0, opacity: 1, rotate: 0 }}
            exit={{ y: -14, opacity: 0, rotate: 30 }}
            transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
            className="grid place-items-center"
          >
            {isDark ? <Moon size={16} /> : <Sun size={16} />}
          </motion.span>
        )}
      </AnimatePresence>
    </button>
  );
}
