/** Route-segment loading UI for the (app) group.
 *
 *  Next.js App Router renders this automatically while the segment's
 *  RSC payload is in flight after a client-side navigation. We use the
 *  same centered spinner the per-page `PageLoading` early-returns use,
 *  so the user gets *instant* feedback the moment they click a sidebar
 *  item — instead of the previous "click → stare at the old page for
 *  3-4s while the RSC fetch returns" behaviour.
 *
 *  This file MUST be a server component (no "use client"). Next.js
 *  streams the loading UI immediately and swaps in the real page when
 *  it's ready; client components can't be the streaming boundary. */
import { PageLoading } from "@/components/PageLoading";

export default function Loading() {
  return <PageLoading />;
}
