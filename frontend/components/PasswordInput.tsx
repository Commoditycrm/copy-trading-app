"use client";

/**
 * Password input with a built-in show/hide toggle.
 *
 * Drop-in replacement for ``<input type="password" ... />``. The toggle
 * lives inside the input's right edge so the field's overall width stays
 * the same. Tab order skips the toggle (``tabIndex={-1}``) — typical
 * sign-in flow is type-password → Enter, and stealing focus would just
 * add a Tab press for no benefit. The toggle is still reachable via
 * mouse / touch / screen reader (it carries an ``aria-label``).
 *
 * Inline SVG icons (no extra deps). Stroke pulls from ``currentColor``
 * so the icon adopts whatever ``color`` the surrounding context sets.
 */
import { InputHTMLAttributes, forwardRef, useState } from "react";

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, "type">;

export const PasswordInput = forwardRef<HTMLInputElement, Props>(
  function PasswordInput({ className = "", ...rest }, ref) {
    const [show, setShow] = useState(false);
    return (
      <div className="relative">
        <input
          ref={ref}
          type={show ? "text" : "password"}
          // Reserve right padding so typed characters don't slide under
          // the toggle button. 2.5rem ≈ icon width + comfortable gap.
          className={`${className} pr-10`}
          {...rest}
        />
        <button
          type="button"
          onClick={() => setShow(s => !s)}
          // Don't grab focus when tabbing through the form — password
          // managers + Enter-to-submit are the common path; a focusable
          // toggle would add friction. Mouse / a11y users still get it.
          tabIndex={-1}
          aria-label={show ? "Hide password" : "Show password"}
          title={show ? "Hide password" : "Show password"}
          className="absolute inset-y-0 right-0 flex items-center justify-center px-2.5"
          style={{ color: "var(--muted)", background: "transparent", border: "none" }}
        >
          {show ? <EyeOffIcon /> : <EyeIcon />}
        </button>
      </div>
    );
  },
);

function EyeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      aria-hidden="true">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      aria-hidden="true">
      <path d="M17.94 17.94A10.94 10.94 0 0 1 12 20c-7 0-11-8-11-8a19.77 19.77 0 0 1 4.22-5.06" />
      <path d="M9.9 4.24A10.94 10.94 0 0 1 12 4c7 0 11 8 11 8a19.86 19.86 0 0 1-3.17 4.19" />
      <path d="M14.12 14.12a3 3 0 1 1-4.24-4.24" />
      <line x1="1" y1="1" x2="23" y2="23" />
    </svg>
  );
}
