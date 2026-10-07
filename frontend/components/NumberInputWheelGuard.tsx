"use client";

import { useEffect } from "react";

/**
 * Stops the mouse wheel from editing `<input type="number">`.
 *
 * A focused number input treats a wheel tick as a step: scroll the page with
 * the pointer over one you have just typed into, and the value silently
 * changes under you. On a settings form that is a quiet data-loss bug — a
 * profit target or a stop percentage can move without anyone touching it, and
 * nothing on screen says it happened.
 *
 * Mounted once at the ROOT layout rather than fixed per input. There are 26 of
 * these across the app and the admin pages do not render AppShell, so anything
 * lower would have to be repeated and would still miss some.
 *
 * ── Why it is scoped this tightly ───────────────────────────────────────────
 * The listener only exists while a number input holds focus, and it only
 * cancels the event when the pointer is over THAT input. So:
 *
 *   - scrolling anywhere else on the page works normally, even mid-edit
 *   - a permanently non-passive wheel listener on `document` never exists,
 *     which is what would cost scroll performance everywhere
 *
 * The one deliberate trade-off: while a number input is focused you cannot
 * scroll the page with the pointer resting on that input. Moving the pointer a
 * few pixels away, or clicking off the field, restores it — and that is far
 * cheaper than an unnoticed value change.
 *
 * `blur()` alone is the usual one-liner for this, but it is not reliable: it
 * depends on the browser re-checking focus before applying the step, and it
 * also throws away the caret mid-edit. preventDefault is unambiguous.
 */
export function NumberInputWheelGuard() {
  useEffect(() => {
    let focused: HTMLInputElement | null = null;

    const block = (e: WheelEvent) => {
      // Only when the wheel is actually over the focused field. The browser
      // only steps the value in that case, so cancelling anything else would
      // break ordinary page scrolling for no reason.
      if (focused && e.target instanceof Node && focused.contains(e.target)) {
        e.preventDefault();
      }
    };

    const onFocusIn = (e: FocusEvent) => {
      const el = e.target;
      if (el instanceof HTMLInputElement && el.type === "number") {
        focused = el;
        // passive: false is required — a passive listener cannot preventDefault,
        // and the step IS the default action.
        document.addEventListener("wheel", block, { passive: false });
      }
    };

    const onFocusOut = () => {
      if (!focused) return;
      document.removeEventListener("wheel", block);
      focused = null;
    };

    document.addEventListener("focusin", onFocusIn);
    document.addEventListener("focusout", onFocusOut);
    return () => {
      document.removeEventListener("focusin", onFocusIn);
      document.removeEventListener("focusout", onFocusOut);
      document.removeEventListener("wheel", block);
    };
  }, []);

  return null;
}
