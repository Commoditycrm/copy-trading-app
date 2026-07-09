"use client";

import { Suspense } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { PerformanceView } from "@/components/performance/PerformanceView";

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
