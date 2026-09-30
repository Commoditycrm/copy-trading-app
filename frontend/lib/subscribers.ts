import { api } from "@/lib/api";
import type { Page, SubscriberSummary } from "@/lib/types";

/** Largest page /api/subscribers serves (the endpoint's `le=200`). */
const PAGE_SIZE = 200;

/** Every subscriber of the signed-in trader, fetched a page at a time.
 *
 *  /api/subscribers is paginated and caps `limit` at 200. Callers that need the
 *  whole roster (the calendar's subscriber picker, the dashboard's counts) used
 *  to ask for `limit=1000` — which the API rejects with a 422, so the calendar
 *  threw and the dashboard silently showed no subscribers. */
export async function fetchAllSubscribers(): Promise<SubscriberSummary[]> {
  const all: SubscriberSummary[] = [];
  for (let offset = 0; ; offset += PAGE_SIZE) {
    const page = await api<Page<SubscriberSummary>>(
      `/api/subscribers?limit=${PAGE_SIZE}&offset=${offset}`,
    );
    all.push(...page.items);
    if (page.items.length < PAGE_SIZE || all.length >= page.total) return all;
  }
}
