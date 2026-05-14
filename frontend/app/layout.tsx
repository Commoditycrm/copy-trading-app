import "./globals.css";
import "react-toastify/dist/ReactToastify.css";
import type { Metadata } from "next";
import { ToastContainer } from "react-toastify";

export const metadata: Metadata = {
  title: "Copy Trading Platform",
  description: "Stock & options copy trading",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body suppressHydrationWarning>
        {children}
        <ToastContainer
          position="top-right"
          autoClose={3000}
          theme="dark"
          newestOnTop
          pauseOnFocusLoss={false}
        />
      </body>
    </html>
  );
}
