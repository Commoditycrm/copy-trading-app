"use client";

import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { api } from "@/lib/api";
import { getSnapshot, setSnapshot, USER_SNAPSHOT_KEY } from "@/lib/swrCache";
import { BulkExitBar } from "@/components/BulkExitBar";
import { OpenPositionsTable, type OpenPositionsTableHandle } from "@/components/OpenPositionsTable";
import { PageLoading } from "@/components/PageLoading";
import type { User } from "@/lib/types";

export default function PositionsPage() {
  const tableRef = useRef<OpenPositionsTableHandle>(null);
  // Seed the user gate from the shared snapshot so return nav skips the
  // full-page loader; still revalidate below.
  const [user, setUser] = useState<User | null>(() => getSnapshot<User>(USER_SNAPSHOT_KEY) ?? null);

  useEffect(() => {
    api<User>("/api/auth/me").then((u) => { setUser(u); setSnapshot(USER_SNAPSHOT_KEY, u); }).catch(() => {});
  }, []);

  // Hold the page until `user` lands — the BulkExitBar gates which chips
  // render off role, and we don't want a brief flash of the "my-only" set
  // before the trader-targeted ones appear.
  if (!user) return <PageLoading />;

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex flex-col gap-4 flex-1 min-h-0">
        <BulkExitBar onActionComplete={() => tableRef.current?.refresh()} />
        <OpenPositionsTable ref={tableRef} fillHeight className="flex-1 min-h-0" />
      </div>
    </div>
  );
}
