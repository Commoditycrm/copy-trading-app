import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Copy Trading Platform",
  description: "Stock & options copy trading",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body suppressHydrationWarning>{children}</body>
    </html>
  );
}
