"use client";

/**
 * Tiny in-memory stale-while-revalidate store. Module state survives
 * client-side route changes (so navigating back to a page paints its last
 * snapshot instantly) but NOT a full reload — a hard refresh always re-fetches.
 *
 * Only for read snapshots that a page revalidates on mount. Never a source of
 * truth: the fetch that follows always overwrites it. Cleared wholesale on
 * logout / 401 (see clearTokens) so one user's data can't flash for the next.
 */
const store = new Map<string, unknown>();

export function getSnapshot<T>(key: string): T | undefined {
  return store.get(key) as T | undefined;
}

export function setSnapshot<T>(key: string, data: T): void {
  store.set(key, data);
}

export function clearSnapshots(): void {
  store.clear();
}
