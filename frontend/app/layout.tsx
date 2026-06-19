import "./globals.css";
import "react-toastify/dist/ReactToastify.css";
import type { Metadata } from "next";
import { ThemeProvider } from "@/components/theme/ThemeProvider";
import { ThemedToaster } from "@/components/theme/ThemedToaster";

export const metadata: Metadata = {
  title: "Copy Trading Platform",
  description: "Stock & options copy trading",
  // icons: {
  //   icon: "/brand-icon.avif",
  //   shortcut: "/brand-icon.avif",
  //   apple: "/brand-icon.avif",
  // },
};

// Runs before first paint to set the theme on <html>, so there's no flash of
// the wrong theme on load. Mirrors the logic in ThemeProvider (stored pref,
// else system, else dark). Kept tiny and dependency-free.
const NO_FLASH_THEME_SCRIPT = `(function(){try{var k='trading-app:theme';var t=localStorage.getItem(k);if(t!=='light'&&t!=='dark'){t=window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';}document.documentElement.setAttribute('data-theme',t);}catch(e){document.documentElement.setAttribute('data-theme','dark');}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: NO_FLASH_THEME_SCRIPT }} />
      </head>
      <body suppressHydrationWarning>
        <ThemeProvider>
          {children}
          <ThemedToaster />
        </ThemeProvider>
      </body>
    </html>
  );
}
