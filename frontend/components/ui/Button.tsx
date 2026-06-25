import { ButtonHTMLAttributes, forwardRef, ReactNode } from "react";
import { Spinner } from "@/components/Spinner";

type Variant = "primary" | "ghost" | "danger" | "danger-soft" | "accent";
type Size = "sm" | "md" | "lg";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  leftIcon?: ReactNode;
  rightIcon?: ReactNode;
  fullWidth?: boolean;
}

// Map to the existing globals.css button classes so visuals stay consistent
// with the rest of the app.
const VARIANT: Record<Variant, string> = {
  primary: "btn-primary",
  ghost: "btn-ghost",
  danger: "btn-danger",
  "danger-soft": "btn-danger-soft",
  accent: "btn-accent-solid",
};

const SIZE: Record<Size, string> = {
  sm: "px-3 py-1.5 text-xs",
  md: "px-4 py-2.5 text-sm",
  lg: "px-5 py-3 text-sm",
};

/**
 * Unified button with variants + built-in loading/disabled states.
 * `loading` disables the button and swaps the left content for a spinner,
 * and sets aria-busy for screen readers.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "primary",
    size = "md",
    loading = false,
    leftIcon,
    rightIcon,
    fullWidth,
    disabled,
    className = "",
    children,
    ...rest
  },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={`${VARIANT[variant]} ${SIZE[size]} ${fullWidth ? "w-full" : ""} inline-flex items-center justify-center gap-2 rounded-chip font-medium focus-ring disabled:cursor-not-allowed ${className}`}
      {...rest}
    >
      {loading ? <Spinner /> : leftIcon}
      {children != null && <span>{children}</span>}
      {!loading && rightIcon}
    </button>
  );
});
