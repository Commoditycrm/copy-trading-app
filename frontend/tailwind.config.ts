import type { Config } from "tailwindcss";

/**
 * Design tokens are defined as CSS variables in app/globals.css (single source
 * of truth). Here we only EXPOSE them to Tailwind so components can use clean
 * utility classes (bg-panel, text-muted, border-line, animate-fade-up, …)
 * instead of inline styles. Everything is additive — no Tailwind defaults are
 * overridden, so existing markup keeps working unchanged.
 */
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "var(--bg)",
        "bg-tint": "var(--bg-tint)",
        panel: "var(--panel)",
        "panel-2": "var(--panel-2)",
        line: "var(--border)",
        "line-strong": "var(--border-strong)",
        ink: "var(--text)",
        "ink-2": "var(--text-2)",
        muted: "var(--muted)",
        faint: "var(--faint)",
        accent: "var(--accent)",
        "accent-2": "var(--accent-2)",
        good: "var(--good)",
        bad: "var(--bad)",
        warn: "var(--warn)",
      },
      // New radius keys (don't override Tailwind's sm/lg defaults).
      borderRadius: {
        chip: "var(--r-sm)",
        token: "var(--r)",
        card: "var(--r-lg)",
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(10px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in": { "0%": { opacity: "0" }, "100%": { opacity: "1" } },
        "scale-in": {
          "0%": { opacity: "0", transform: "scale(0.97)" },
          "100%": { opacity: "1", transform: "scale(1)" },
        },
        "slide-in-right": {
          "0%": { transform: "translateX(100%)" },
          "100%": { transform: "translateX(0)" },
        },
        shimmer: { "100%": { transform: "translateX(100%)" } },
      },
      animation: {
        "fade-up": "fade-up 0.4s cubic-bezier(0.16,1,0.3,1) both",
        "fade-in": "fade-in 0.2s ease-out both",
        "scale-in": "scale-in 0.16s cubic-bezier(0.16,1,0.3,1) both",
        "slide-in-right": "slide-in-right 0.25s cubic-bezier(0.16,1,0.3,1) both",
      },
    },
  },
  plugins: [],
};

export default config;
