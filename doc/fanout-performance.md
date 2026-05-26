# Fanout performance — why it was slow and how we fixed it

**Author:** Irfan · **Date:** 2026-05-26 · **PR:** [irfan/fanout-perf-batch-pnl](https://github.com/Commoditycrm/copy-trading-app/pull/new/irfan/fanout-perf-batch-pnl)

---

## TL;DR

On a fanout to 91 subscribers, `pick_lag_ms` for the **last** subscriber was climbing to ~9 seconds. The PR linked above brings that tail down to a few hundred milliseconds with two compounding changes — **batch P&L** and **deferred flush** — both confined to the Phase-1 DB work.

## Symptom (what you'd see on the Performance page)

Expand any fanout row and look at the per-subscriber timeline. The `pick_lag_ms` column would climb linearly across subscribers:

```
subscriber 1   →   pick_lag    2ms
subscriber 2   →   pick_lag  150ms
subscriber 3   →   pick_lag  300ms
…
subscriber 91  →   pick_lag ~9000ms   ← the 9-second tail
```

Cards above the table reflected the same — `AVG FANOUT` in the seconds, `MAX FANOUT` near 10 s.

## Background: `pick_lag_ms` is

> `subscriber_picked_at` − `parent.created_at`

i.e., **how long after our backend recorded your trade did the copy engine get around to looking at this specific subscriber.** A linear ramp across rows means Phase 1 of the engine is processing subscribers serially.

## Root cause

`copy_engine.fanout_async` has three phases. Phase 2 (broker calls) is already parallel via `asyncio.gather`; Phase 3 (apply responses) is fine. The culprit was **Phase 1**:

```python
for sub in subs:                         # 91 iterations
    if sub.daily_loss_limit is not None:
        todays_pnl = today_realized_pnl(db, sub.user_id)   # ← per-sub, expensive
    ...
    db.add(child)
    db.flush()                            # ← per-sub round-trip to Postgres
```

Two compounding problems:

### 1. `today_realized_pnl` was a full-history walk *per subscriber*

Every call did this (`backend/app/services/pnl.py`):
- `SELECT * FROM orders WHERE user_id = ... AND filled_quantity > 0`
- `SELECT * FROM fills WHERE order_id IN (those)`
- Walked FIFO lot matching from the beginning of the user's trading history

With 91 subscribers each carrying ~hundreds of historical orders, that's **5–15 seconds of pure DB + CPU work before Phase 2 even starts** — and almost all of it happens before the *last* subscriber's pick.

### 2. `db.flush()` after every child INSERT

Each child Order INSERT was flushed individually — ~91 separate Postgres round-trips inside the Phase-1 loop, vs. one batched INSERT-many transaction.

Combined, these two account for the entire pick_lag ramp.

## Fix

### `today_realized_pnl_bulk` — one batched P&L call (in `pnl.py`)

New helper that returns `dict[user_id, today_pnl]` for many users in **two SELECTs total** (orders, then fills), partitioned in memory per-user, then the same FIFO walk runs in-process. Each user-walk also short-circuits the moment we step past today (we don't care about future fills for the daily-loss-limit check).

```python
# pnl.py
def today_realized_pnl_bulk(
    db: Session,
    user_ids: list[uuid.UUID],
    tz_name: str | None = None,
) -> dict[uuid.UUID, Decimal]:
    ...
```

Users with no fills (or no closing trades today) map to `Decimal(0)`. Same FIFO semantics as `realized_pnl_by_day` — just batched.

### Pre-Phase-1 batch in `fanout_async` (in `copy_engine.py`)

Compute P&L for all subscribers who have `daily_loss_limit` set, in one shot, before entering the loop:

```python
sub_ids_with_limit = [s.user_id for s in subs if s.daily_loss_limit is not None]
pnl_by_user = (
    await asyncio.to_thread(today_realized_pnl_bulk, db, sub_ids_with_limit)
    if sub_ids_with_limit else {}
)

for sub in subs:
    if sub.daily_loss_limit is not None:
        todays_pnl = pnl_by_user.get(sub.user_id, Decimal(0))
        ...
```

Subscribers without a limit set don't appear in the pre-batch and short-circuit cheaply inside the loop. The lifecycle timestamps (`subscriber_picked_at`, `subscriber_accepted_at`) are still stamped inside the loop, so per-row pick_lag still reflects engine throughput — it just isn't dominated by the P&L cost anymore.

### Single batched flush at end of Phase 1

`Order.id` has a Python-side `default=uuid.uuid4`, so `child.id` is populated by `Order(...)` itself — no round-trip to Postgres needed to read it. We can defer the flush:

```python
# Inside the loop
db.add(child)
# (no db.flush() here)
...
pending.append(_PendingMirror(child_order_id=child.id, ...))

# After the loop, just before Phase 2:
if pending:
    db.flush()
```

Postgres batches all ~91 INSERTs into one transactional round-trip.

## Phase 2 / Phase 3 (no change)

- **Phase 2** — broker calls — was already concurrent via `asyncio.gather` with per-broker semaphore (default 200 for Alpaca). Untouched.
- **Phase 3** — apply broker responses, audit-log, set `redis_published_at`. Untouched.

## Expected impact

For a 91-subscriber fanout with daily-loss-limit widely set:

| | DB queries | History walks | Flushes | Approx. Phase-1 time |
|---|---|---|---|---|
| **Before** | ~182 | 91 (per-user) | 91 | ~9 s tail |
| **After** | 2 | 91 (in memory) | 1 | hundreds of ms |

Phase 2 / Phase 3 unchanged in this PR, so the overall `total_ms` improves by approximately the Phase-1 saving.

## How to verify after merge + deploy

1. Auto-deploy to Lightsail picks up the change on PR merge to `main`.
2. Place a small market trade as the trader (any qty, any symbol).
3. Open `/performance` → click the new fanout row.
4. Scan the **pick_lag_ms** column across all subscribers:
   - **Before** — climbs linearly from ~2 ms to several seconds across rows
   - **After** — roughly flat across rows (~50–300 ms range)
5. Cards above (`AVG FANOUT`, `MAX FANOUT`) reflect the same drop.

## What this PR does NOT do (deliberate scope-cuts for follow-ups)

- **Caching P&L across consecutive fanouts.** A trader placing 10 trades in a row still re-batches P&L 10 times. Adding a short-TTL Redis cache (5–10 s) keyed on `user_id` would help. Not in this PR — single-fanout improvement is already large.
- **Async-as-completed for streaming.** `asyncio.gather` is fine for total wall-clock time. `as_completed` would let us push SSE events to subscribers as their individual mirrors land rather than waiting for the slowest one. UX-level improvement, separate change.
- **Webull / SnapTrade lifecycle parity.** Phase 1 lifecycle stamps were already correct here; just flagging for completeness.

## Files changed

```
backend/app/services/pnl.py            +130 lines  (new today_realized_pnl_bulk helper)
backend/app/services/copy_engine.py    + ~30 lines (batch call + deferred flush)
```

No DB migration. No schema change. No frontend change. No API contract change.

## Links

- **PR:** [`irfan/fanout-perf-batch-pnl`](https://github.com/Commoditycrm/copy-trading-app/pull/new/irfan/fanout-perf-batch-pnl)
- **Code path:**
  - [`backend/app/services/copy_engine.py`](../backend/app/services/copy_engine.py) — `fanout_async`
  - [`backend/app/services/pnl.py`](../backend/app/services/pnl.py) — `today_realized_pnl_bulk`
