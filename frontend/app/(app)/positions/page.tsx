"use client";

import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { api } from "@/lib/api";
import { BulkExitBar } from "@/components/BulkExitBar";
import { OpenPositionsTable, type OpenPositionsTableHandle } from "@/components/OpenPositionsTable";
import { PageLoading } from "@/components/PageLoading";
import type { User } from "@/lib/types";

export default function PositionsPage() {
  const tableRef = useRef<OpenPositionsTableHandle>(null);
  const [user, setUser] = useState<User | null>(null);

  useEffect(() => {
    api<User>("/api/auth/me").then(setUser).catch(() => {});
  }, []);

  // Hold the page until `user` lands — the BulkExitBar gates which chips
  // render off role, and we don't want a brief flash of the "my-only" set
  // before the trader-targeted ones appear.
  if (!user) return <PageLoading />;

  return (
    <div className="max-w-[1400px] mx-auto">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        className="mb-5"
      >
        <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight" style={{ color: "var(--text)" }}>
          Positions
        </h1>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          Open positions across your connected brokers, updated in real time.
        </p>
      </motion.div>

      <div className="space-y-4">
        <BulkExitBar onActionComplete={() => tableRef.current?.refresh()} />
        <OpenPositionsTable ref={tableRef} />
      </div>
    </div>
  );
}
