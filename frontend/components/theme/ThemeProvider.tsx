"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";

/**
 * Theme system — light / dark with system-preference fallback, persisted to
 * localStorage. No-flash is handled by an inline script in app/layout.tsx that
 * sets `data-theme` on <html> before first paint; this provider keeps React in
 * sync and exposes a toggle.
 *
 * Purely presentational — touches no API, auth, or business logic.
 */

export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "trading-app:theme";

interface ThemeContextValue {
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggle: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function systemTheme(): Theme {
  if (typeof window === "undefined") return "dark";
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function storedTheme(): Theme | null {
  if (typeof window === "undefined") return null;
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

function applyTheme(theme: Theme, animate: boolean) {
  const root = document.documentElement;
  // Briefly enable the cross-fade transition only on user-driven switches so
  // the initial load doesn't animate.
  if (animate) {
    root.classList.add("theme-anim");
    window.setTimeout(() => root.classList.remove("theme-anim"), 280);
  }
  root.setAttribute("data-theme", theme);
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  // Default matches the SSR/no-flash script default (dark) so the first client
  // render agrees with the server and the pre-paint script.
  const [theme, setThemeState] = useState<Theme>("dark");

  // Adopt whatever the no-flash script already applied (stored pref, else
  // system) once we're on the client.
  useEffect(() => {
    const initial = storedTheme() ?? systemTheme();
    setThemeState(initial);
    applyTheme(initial, false);
  }, []);

  // Follow OS changes only while the user hasn't pinned a preference.
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => {
      if (storedTheme() === null) {
        const next = systemTheme();
        setThemeState(next);
        applyTheme(next, true);
      }
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const setTheme = useCallback((t: Theme) => {
    setThemeState(t);
    applyTheme(t, true);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, t);
    } catch {
      /* storage disabled — theme still applies for this session */
    }
  }, []);

  const toggle = useCallback(() => {
    setTheme(theme === "dark" ? "light" : "dark");
  }, [theme, setTheme]);

  return (
    <ThemeContext.Provider value={{ theme, setTheme, toggle }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within <ThemeProvider>");
  return ctx;
}
