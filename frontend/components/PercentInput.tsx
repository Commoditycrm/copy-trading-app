"use client";

import { forwardRef } from "react";

/**
 * A number input for values that can never be negative — percentages above all,
 * but also prices/qtys/thresholds. Drop-in for `<input type="number">`: same
 * props, same event-based `onChange`. It blocks the sign/exponent keys and
 * strips a leading "-" that sneaks in via paste or the spinner. Positive values
 * are passed through untouched — we never clamp a number the user typed (so 900
 * stays 900), we only stop negatives.
 */
type Props = React.InputHTMLAttributes<HTMLInputElement>;

export const PercentInput = forwardRef<HTMLInputElement, Props>(
  function PercentInput({ onChange, onKeyDown, min, ...rest }, ref) {
    return (
      <input
        ref={ref}
        {...rest}
        type="number"
        min={min ?? 0}
        onKeyDown={(e) => {
          onKeyDown?.(e);
          // "-"/"+" would make it negative/signed; "e"/"E" is exponent notation.
          if (!e.defaultPrevented && ["-", "+", "e", "E"].includes(e.key)) {
            e.preventDefault();
          }
        }}
        onChange={(e) => {
          if (e.target.value.startsWith("-")) {
            e.target.value = e.target.value.replace(/^-+/, "");
          }
          onChange?.(e);
        }}
      />
    );
  },
);
