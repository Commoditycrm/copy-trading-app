import { PerformanceView } from "@/components/performance/PerformanceView";

// Trader's own Performance page — renders the shared view against their own
// fanouts. The admin per-trader page reuses the same component with an
// admin-scoped endpoint.
export default function PerformancePage() {
  return <PerformanceView />;
}
