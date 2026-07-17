"use client";

import { Suspense } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { PerformanceView } from "@/components/performance/PerformanceView";
import { ExportButton } from "@/components/ExportButton";

function Inner() {
  const params = useParams<{ trader_id: string }>();
  const traderId = params?.trader_id ?? "";
  const name = useSearchParams().get("name");

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link
          href="/admin/traders"
          className="text-sm px-3 py-1.5 rounded-lg no-underline"
          style={{ background: "var(--panel-2)", border: "1px solid var(--border)", color: "var(--text-2)" }}
        >
          ← All traders
        </Link>
        <h2 className="text-xl font-bold">{name || "Trader"} — Performance</h2>
        {/* Two different questions, so two exports:
            - Trades  = this trader's COMPLETE order history, including orders
              that never fanned out (Just-me scope, copy paused, no subscribers).
            - Fanouts = only the copy-traded ones, one row per subscriber mirror. */}
        <div className="ml-auto flex items-center gap-2">
          <ExportButton
            path={`/api/trades/export?user_id=${traderId}`}
            label="Export trades"
            fallbackName="kopyya-trades.xlsx"
          />
          <ExportButton
            path={`/api/admin/performance/export?trader_id=${traderId}`}
            label="Export fanouts"
            fallbackName="kopyya-fanouts.xlsx"
          />
        </div>
      </div>
      {/* Same view the trader sees, scoped to this trader via the admin endpoint. */}
      <PerformanceView endpoint={`/api/admin/performance/fanouts?trader_id=${traderId}&limit=50`} />
    </div>
  );
}

export default function AdminTraderPerformancePage() {
  return (
    <Suspense fallback={null}>
      <Inner />
    </Suspense>
  );
}
