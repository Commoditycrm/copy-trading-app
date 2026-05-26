"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

interface LoadTestCount {
  seeded_users:         number;
  fake_broker_accounts: number;
  actively_following:   number;
}

interface SeedResult {
  created: number;
  skipped: number;
  total:   number;
}

interface CleanupResult {
  deleted: number;
}

const COUNT_PRESETS = [10, 25, 50, 100];

export default function LoadTestPage() {
  const [count, setCount]           = useState<LoadTestCount | null>(null);
  const [loadingCount, setLoadingCount] = useState(true);

  // Seed form state
  const [traderEmail, setTraderEmail] = useState("");
  const [seedCount, setSeedCount]     = useState(50);
  const [multiplier, setMultiplier]   = useState(1.0);
  const [seeding, setSeeding]         = useState(false);

  // Cleanup state
  const [cleanupEmail, setCleanupEmail] = useState("");
  const [cleaning, setCleaning]         = useState(false);
  const [confirmCleanup, setConfirmCleanup] = useState(false);

  async function loadCount() {
    setLoadingCount(true);
    try {
      const c = await api<LoadTestCount>("/api/admin/load-test/count");
      setCount(c);
    } catch (e) {
      notify.fromError(e, "Could not load count");
    } finally {
      setLoadingCount(false);
    }
  }

  useEffect(() => { loadCount(); }, []);

  async function handleSeed(e: React.FormEvent) {
    e.preventDefault();
    if (!traderEmail.trim()) { notify.warn("Enter the trader email"); return; }
    setSeeding(true);
    try {
      const result = await api<SeedResult>("/api/admin/load-test/seed", {
        method: "POST",
        body: JSON.stringify({ trader_email: traderEmail.trim(), count: seedCount, multiplier }),
      });
      notify.success(
        `Done: ${result.created} created, ${result.skipped} already existed`
      );
      await loadCount();
    } catch (e) {
      notify.fromError(e, "Seed failed");
    } finally {
      setSeeding(false);
    }
  }

  async function handleCleanup() {
    if (!confirmCleanup) { setConfirmCleanup(true); return; }
    setCleaning(true);
    setConfirmCleanup(false);
    try {
      const result = await api<CleanupResult>("/api/admin/load-test/cleanup", {
        method: "POST",
        body: JSON.stringify({ trader_email: cleanupEmail.trim() || null }),
      });
      notify.success(`Cleaned up ${result.deleted} fake subscribers`);
      await loadCount();
    } catch (e) {
      notify.fromError(e, "Cleanup failed");
    } finally {
      setCleaning(false);
    }
  }

  const hasSeeded = (count?.seeded_users ?? 0) > 0;

  return (
    <div className="space-y-6 max-w-2xl">
      <div>
        <h2 className="text-xl font-bold">Load Test</h2>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          Seed fake subscribers with a simulated broker (no real trades, no real money).
          Used to test the fanout pipeline speed without needing real Alpaca accounts.
        </p>
      </div>

      {/* Status card */}
      <div
        className="rounded-xl p-5 flex items-center justify-between"
        style={{
          background: hasSeeded
            ? "linear-gradient(135deg,rgba(250,204,21,0.08),rgba(250,204,21,0.02))"
            : "linear-gradient(135deg,rgba(14,20,17,0.7),rgba(7,9,10,0.5))",
          border: "1px solid " + (hasSeeded ? "rgba(250,204,21,0.25)" : "var(--border)"),
        }}
      >
        {loadingCount ? (
          <div style={{ color: "var(--muted)" }}>Loading…</div>
        ) : count ? (
          <>
            <div className="space-y-1">
              <div className="text-2xl font-bold" style={{ color: hasSeeded ? "#facc15" : "var(--text)" }}>
                {count.seeded_users} seeded users
              </div>
              <div className="text-sm" style={{ color: "var(--muted)" }}>
                {count.fake_broker_accounts} fake broker accounts ·{" "}
                {count.actively_following} actively following a trader
              </div>
            </div>
            <button
              onClick={loadCount}
              className="text-xs px-3 py-1.5 rounded-lg"
              style={{ background: "rgba(255,255,255,0.06)", border: "1px solid var(--border)", color: "var(--text-2)" }}
            >
              Refresh
            </button>
          </>
        ) : null}
      </div>

      {/* Seed form */}
      <div
        className="rounded-xl p-5 space-y-4"
        style={{ background: "rgba(14,20,17,0.5)", border: "1px solid var(--border)" }}
      >
        <div className="font-semibold">Seed Fake Subscribers</div>

        <form onSubmit={handleSeed} className="space-y-4">
          {/* Trader email */}
          <div className="space-y-1">
            <label className="text-xs font-medium" style={{ color: "var(--text-2)" }}>
              Trader Email
            </label>
            <input
              type="email"
              required
              placeholder="trader@example.com"
              value={traderEmail}
              onChange={e => setTraderEmail(e.target.value)}
              className="w-full text-sm px-3 py-2 rounded-lg"
              style={{
                background: "rgba(255,255,255,0.04)",
                border: "1px solid var(--border)",
                color: "var(--text)",
                outline: "none",
              }}
            />
            <div className="text-xs" style={{ color: "var(--muted)" }}>
              The trader these fake subscribers will follow.
            </div>
          </div>

          {/* Count presets */}
          <div className="space-y-1">
            <label className="text-xs font-medium" style={{ color: "var(--text-2)" }}>
              Number of Subscribers
            </label>
            <div className="flex gap-2 flex-wrap">
              {COUNT_PRESETS.map(n => (
                <button
                  key={n}
                  type="button"
                  onClick={() => setSeedCount(n)}
                  className="text-sm px-4 py-1.5 rounded-lg font-medium transition-colors"
                  style={{
                    background: seedCount === n ? "var(--accent)" : "rgba(255,255,255,0.05)",
                    color:      seedCount === n ? "var(--accent-ink)" : "var(--text-2)",
                    border:     "1px solid " + (seedCount === n ? "var(--accent)" : "var(--border)"),
                  }}
                >
                  {n}
                </button>
              ))}
              {/* Custom input */}
              <input
                type="number"
                min={1}
                max={500}
                value={COUNT_PRESETS.includes(seedCount) ? "" : seedCount}
                placeholder="Custom…"
                onChange={e => {
                  const v = parseInt(e.target.value, 10);
                  if (!isNaN(v) && v > 0) setSeedCount(v);
                }}
                className="text-sm px-3 py-1.5 rounded-lg w-28"
                style={{
                  background: "rgba(255,255,255,0.04)",
                  border: "1px solid var(--border)",
                  color: "var(--text)",
                  outline: "none",
                }}
              />
            </div>
            <div className="text-xs" style={{ color: "var(--muted)" }}>
              Idempotent — re-running skips already-seeded users.
            </div>
          </div>

          {/* Multiplier */}
          <div className="space-y-1">
            <label className="text-xs font-medium" style={{ color: "var(--text-2)" }}>
              Copy Multiplier
            </label>
            <div className="flex items-center gap-3">
              <input
                type="range"
                min={0.1}
                max={3}
                step={0.1}
                value={multiplier}
                onChange={e => setMultiplier(parseFloat(e.target.value))}
                className="flex-1"
              />
              <span className="text-sm font-mono w-10 text-right" style={{ color: "var(--text)" }}>
                {multiplier.toFixed(1)}×
              </span>
            </div>
          </div>

          <button
            type="submit"
            disabled={seeding}
            className="text-sm font-semibold px-5 py-2.5 rounded-lg w-full transition-opacity"
            style={{
              background: "var(--accent)",
              color: "var(--accent-ink)",
              opacity: seeding ? 0.6 : 1,
              cursor: seeding ? "not-allowed" : "pointer",
            }}
          >
            {seeding ? "Seeding…" : `Seed ${seedCount} Fake Subscribers`}
          </button>
        </form>
      </div>

      {/* Cleanup section */}
      {hasSeeded && (
        <div
          className="rounded-xl p-5 space-y-4"
          style={{
            background: "linear-gradient(135deg,rgba(239,68,68,0.06),rgba(239,68,68,0.02))",
            border: "1px solid rgba(239,68,68,0.2)",
          }}
        >
          <div>
            <div className="font-semibold" style={{ color: "#ef4444" }}>Cleanup</div>
            <div className="text-xs mt-1" style={{ color: "var(--muted)" }}>
              Permanently deletes all {count?.seeded_users} fake-load-test users and their orders.
              This cannot be undone.
            </div>
          </div>

          <div className="space-y-1">
            <label className="text-xs font-medium" style={{ color: "var(--text-2)" }}>
              Trader Email (optional — to bust Redis cache)
            </label>
            <input
              type="email"
              placeholder="trader@example.com"
              value={cleanupEmail}
              onChange={e => { setCleanupEmail(e.target.value); setConfirmCleanup(false); }}
              className="w-full text-sm px-3 py-2 rounded-lg"
              style={{
                background: "rgba(255,255,255,0.04)",
                border: "1px solid rgba(239,68,68,0.2)",
                color: "var(--text)",
                outline: "none",
              }}
            />
          </div>

          <button
            onClick={handleCleanup}
            disabled={cleaning}
            className="text-sm font-semibold px-5 py-2.5 rounded-lg w-full transition-all"
            style={{
              background: confirmCleanup ? "#ef4444" : "rgba(239,68,68,0.12)",
              color: confirmCleanup ? "#fff" : "#ef4444",
              border: "1px solid rgba(239,68,68,0.3)",
              opacity: cleaning ? 0.6 : 1,
              cursor: cleaning ? "not-allowed" : "pointer",
            }}
          >
            {cleaning
              ? "Cleaning up…"
              : confirmCleanup
              ? `⚠ Confirm — Delete ${count?.seeded_users} users`
              : `Cleanup All Fake Subscribers`}
          </button>
          {confirmCleanup && (
            <div className="text-xs text-center" style={{ color: "var(--muted)" }}>
              Click again to confirm. This will delete {count?.seeded_users} users and all their data.
            </div>
          )}
        </div>
      )}

      {/* Info box */}
      <div
        className="rounded-xl p-4 text-xs space-y-1"
        style={{ background: "rgba(255,255,255,0.03)", border: "1px solid var(--border)", color: "var(--muted)" }}
      >
        <div className="font-semibold text-sm" style={{ color: "var(--text-2)" }}>How this works</div>
        <div>• Fake subscribers use a simulated broker (FakeBrokerAdapter) — no real orders are placed.</div>
        <div>• Orders are processed and timed exactly like real subscribers — fanout pipeline, latency tracking, all included.</div>
        <div>• After seeding, place a trade as the trader and check the Performance page to see fanout timing across all subscribers.</div>
        <div>• Emails follow pattern: <code className="px-1 rounded" style={{ background: "rgba(255,255,255,0.06)" }}>fake-load-test-0001@example.invalid</code></div>
      </div>
    </div>
  );
}
