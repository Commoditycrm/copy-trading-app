"use client";

import { useEffect, useRef, useState } from "react";
import { animate, useMotionValue } from "framer-motion";

/**
 * Count-up animated number. Eases from its previous value to the new one and
 * renders through the provided formatter, so currency/percent stay consistent
 * with the rest of the app (lib/format). Respects reduced-motion by snapping.
 */
export function AnimatedNumber({
  value,
  format,
  className,
  duration = 0.9,
}: {
  value: number;
  format: (n: number) => string;
  className?: string;
  duration?: number;
}) {
  const mv = useMotionValue(value);
  const [display, setDisplay] = useState(() => format(value));
  const first = useRef(true);

  useEffect(() => {
    const reduce =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || first.current) {
      first.current = false;
      mv.set(value);
      setDisplay(format(value));
      if (reduce) return;
    }
    const controls = animate(mv, value, {
      duration,
      ease: [0.16, 1, 0.3, 1],
      onUpdate: (latest) => setDisplay(format(latest)),
    });
    return controls.stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, duration]);

  return <span className={className}>{display}</span>;
}
