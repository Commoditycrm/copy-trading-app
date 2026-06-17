"use client";

import { ToastContainer } from "react-toastify";
import { useTheme } from "./ThemeProvider";

/** ToastContainer whose theme follows the app theme. Kept in its own client
 *  component so the root layout can stay a server component. */
export function ThemedToaster() {
  const { theme } = useTheme();
  return (
    <ToastContainer
      position="top-right"
      autoClose={3000}
      theme={theme}
      newestOnTop
      pauseOnFocusLoss={false}
    />
  );
}
