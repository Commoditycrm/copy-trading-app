# QA / Tester Guide — Changes for 21–23 Jul 2026

This document lists everything **fixed and implemented over the last 3 days** and,
for each item, **what changed, why, and how to test it**. It is written for the
QA/tester — no code knowledge required.

> **Legend**
> - 🐞 **Fix** — a bug that was resolved
> - ✨ **Feature** — new capability
> - 🎨 **UI** — visible interface change
>
> **Broker note:** many items behave differently on **Alpaca** (real-time) vs
> **Webull/SnapTrade** (updates arrive on a delay of seconds–minutes). Where it
> matters, test on **both** brokers.

---

## How to test in general

1. Have a **trader** account and at least one **subscriber** account connected
   (ideally one Alpaca and one Webull/SnapTrade).
2. Keep two browser windows open — trader and subscriber — plus the **admin
   Performance** page.
3. Most flows are exercised by the **trader placing/closing orders** and checking
   that the subscriber mirrors correctly, and that Order History reflects it.

---

## 1. Bug fixes

### 🐞 1.1 Subscriber close was sometimes skipped/cancelled (Webull)
**What:** When the trader closed a position, the subscriber's matching close was
occasionally dropped (shown as `CANCELED`) and the subscriber was left holding a
position the trader had already exited. Most visible during **fast buy→sell** or
**rapid cancel-and-replace** on Webull.

**Why:** Our system decided "nothing to close" from our own records, which lag
Webull's actual state. It now trusts the broker's real position instead of the
stale local count when the trader is genuinely closing.

**How to test:**
1. Trader **buys** an option, then within a few seconds **sells** to close (fast).
2. **Expected:** the subscriber's position **closes** (a filled SELL appears in the
   subscriber's Order History) — it is **not** left `CANCELED`/stranded.
3. Repeat on both Alpaca and Webull.
4. Edge case: trader places a SELL, cancels it, re-places, a few times, then lets it
   fill. Subscriber should still end up **flat**, not holding a stray position.

---

### 🐞 1.2 Option limit-price increment ("$0.05 / $0.10") rejections
**What:** Closing or setting TP/SL on an option sometimes failed with a broker
error like *"premium of $3 or more must be entered in $0.10 increments"* (or the
$0.05 version under $3).

**Why:** Option prices must be rounded to the exchange's tick: **under $3 → $0.05
(nickel)**, **$3 and above → $0.10 (dime)**. This is now enforced everywhere,
including a final safety net right before the order is sent to Webull/SnapTrade.

**How to test:**
1. Close an option trading around **$2.90–$2.99** — the close price should be a
   valid **nickel** (e.g. 2.95), **no rejection**.
2. Close an option trading around **$3.50–$3.60** — the close price should be a
   valid **dime** (e.g. 3.50 or 3.60), **no rejection**.
3. Set a **copied TP/SL** on an option in each range — the exit prices should also
   be valid ticks (no "increment" error).

---

### 🐞 1.3 Trading-halt: market order rejected
**What:** A market order on a **halted** symbol was rejected (e.g. *"market order
rejected due to trading halt … place a limit order instead"*) and nothing was
placed.

**Why:** During a halt the broker refuses market orders. We now automatically
**retry as a limit order**, which the broker accepts; it rests and fills when the
halt lifts.

**How to test:** Hard to reproduce on demand (requires a live halt). If a halt
occurs, confirm the order becomes a **resting limit** instead of an outright
rejection. Otherwise verify normal (non-halted) market orders are unaffected.

---

### 🐞 1.4 Pre-market closes, order modifications & missing orders
**What:** Three related fixes:
- A **close placed pre-market** (before the entry had filled) used to reject; it now
  **waits** and fires the moment the entry fills.
- When the trader **modifies** a working order (e.g. changes the limit from 3.00 to
  3.20), the change now **propagates** to subscribers.
- Some trader orders that weren't being mirrored ("missing orders") are now picked up.

**How to test:**
1. **Pre-market:** trader places a buy pre-market, then a close, before fills happen.
   Subscriber's close should **not reject** — it stays pending and fills once the
   entry fills.
2. **Modify:** trader places a limit order, then edits the price. Subscriber's mirror
   should update to the **new price** (check Order History Expected Price).
3. Confirm no valid trader order is silently missed by subscribers.

---

### 🐞 1.5 SnapTrade P&L accuracy + no phantom short positions
**What:** Realized P&L for subscribers is now computed correctly (no double-counting)
and close-recovery no longer creates **phantom short** positions.

**Why:** P&L is de-duplicated by broker order id and computed directly from the
broker's activity feed; close recovery is aware of the broker's true net position.

**How to test:**
1. Do a few round-trip trades on a subscriber, then check the **Calendar / realized
   P&L** matches the broker's own statement for the day.
2. Confirm positions never show an **unexpected short** after a close.

---

## 2. New features

### ✨ 2.1 Daily Profit Target (NEW — please test thoroughly)
**What:** A configurable **daily profit target** as a **percentage of the previous
day's account value**. When the account's **current value** (including unrealized
gains on open positions) reaches *yesterday's value × (1 + target%)*, the system:
1. **Closes all open positions** once, to **book the profit**.
2. **Keeps copy trading ON.**
3. Does **not** immediately re-liquidate — so when the market dips back and the
   strategy re-enters, the position is re-bought normally.

It **resets each day** and re-arms off the new day's starting value.

**Example:** Yesterday's account value = **$1,000**, target = **20%** → target value
**$1,200**. When the account reaches **$1,200**, all positions close, ~**$200** is
booked, copy stays on.

**Where to configure:** Settings → risk limits → **"Daily profit target"** (enter a
%, e.g. 20). Leave blank/clear to disable.

**How to test:**
1. In Settings, set **Daily profit target = e.g. 5%** (small, so it's easy to hit).
2. With **open positions**, let the account's value rise past *start × 1.05*.
3. **Expected:**
   - All open positions **close** automatically (you'll see closing orders in Order
     History + a notification "Daily profit target hit…").
   - **Copy trading stays ON** (the toggle is *not* turned off — this is the key
     difference from the daily profit *limit*, which pauses copy).
   - The account does **not** keep re-closing every new position for the rest of the
     day.
4. If the trader re-enters later, the subscriber should **re-enter** normally.
5. **Next day:** the target re-arms based on the new starting value.
6. **Boundary:** with target 5% and start $1,000, it should fire at **~$1,050**, not
   before.
7. **Disable:** clear the field → no auto-close on profit.

**Notes for the tester:**
- It's an **account-value** target (includes unrealized), **not** realized-only.
- The booked amount is *approximately* the target — closing at market moves the
  number slightly.
- Applies to **subscribers** (each sets their own). Trader-side enforcement exists in
  the backend but the **trader configuration screen is a pending follow-up** — for
  now, test the **subscriber** path.
- **Requires the P&L poller** to be running (it checks account value every
  ~10s Alpaca / ~60s Webull).

---

### ✨ 2.2 Position reconciler (admin)
**What:** An admin tool that compares our recorded positions against the broker's
actual holdings, classifies each mismatch, and can write corrective closes for
**expired-worthless options**. Runs as a **dry-run by default** (writes nothing
unless explicitly applied).

**How to test (admin):**
1. Trigger the reconcile (dry-run) and confirm it **reports** divergences without
   changing anything.
2. Only when explicitly applied should it write the expired-worthless closes.

---

### ✨ 2.3 Calendar P&L from the broker (durable snapshots)
**What:** The realized-P&L Calendar is now computed **directly from the broker's
activity feed** and stored as **durable daily snapshots**, so it's accurate across
brokers and survives reconnects.

**How to test:**
1. Check the Calendar's daily realized P&L matches the broker for several days.
2. Reconnect a broker and confirm past days' P&L is **retained**.

---

## 3. UI changes

### 🎨 3.1 Order History — "Order Type" column
**What:** A new **Order Type** column (after Status) shows **Market / Limit / Stop /
Stop Limit** for both trader and subscriber orders.

**How to test:** Place a market order and a limit order; confirm the column shows the
correct type for each.

---

### 🎨 3.2 Order History — separate fill rows + frozen placement
**What:** When an order fills, the placement row **stays as "SUBMITTED"** (the order
event) and a **separate "FILL" row appears above it** showing the execution (filled
qty, price, time). Works for **both** brokers (Alpaca shows each execution; Webull
shows one consolidated fill).

**How to test:**
1. Place an order and let it fill.
2. **Expected:** the original row keeps showing **SUBMITTED** (with its Cancel button
   gone once filled), and a green **FILL** row appears **above** it.
3. A partially-filled order should keep its Cancel button (for the remaining qty).
4. Verify on both Alpaca and Webull subscribers.

---

### 🎨 3.3 Admin Performance table — several improvements
**What:**
- **Symbol** now shows the full contract descriptor, e.g. **`SPXW C $7510 22 Jul 26`**
  (like the trader panel), with no buy/sell tag crammed into it.
- New **Qty** column (clean integer — shows `1`, not `1.00000`).
- The old instrument **"Type"** column is replaced by a **Side** column (BUY green /
  SELL red).
- New **Order Type** column (Market/Limit) — also in the Excel export.
- New **Filled At** column for both the **trader** row and each **subscriber** in the
  expanded breakdown.
- Long headers (e.g. "Broker Accepted At") now render on a **single line**.

**How to test (admin Performance page):**
1. Confirm option rows read like `SPXW C $7510 22 Jul 26`; stock rows show the plain
   ticker.
2. Confirm **Qty** shows a clean number and **Side** shows BUY/SELL with color.
3. Confirm **Order Type** and **Filled At** are populated (Filled At blank until the
   order actually fills).
4. Expand a fanout row → each subscriber shows their own **Filled At**.
5. Confirm headers don't wrap to multiple lines.

---

## 4. Regression checklist (please re-verify these still work)

- [ ] Trader → subscriber copy of a normal **buy** and **sell** (Alpaca + Webull).
- [ ] **Options** close cleanly on both brokers (no market-order or increment errors).
- [ ] **Daily loss limit** and **daily profit limit** still **pause** copy as before
      (these are separate from the new profit *target*).
- [ ] **Auto-liquidation** (equity floor) still works and stays sticky.
- [ ] Order History **status tabs** (Working / Filled / Cancelled / Rejected) still
      filter correctly even with the new frozen-placement display.
- [ ] Settings **reset** clears the new profit-target field too.

---

## 5. Known gaps / notes for the tester

- **Historical rows don't change:** fixes only affect **new** activity. Orders that
  already cancelled/failed before deploy will stay as they are.
- **Webull delay:** Webull/SnapTrade reports fills and cancels on a delay, so some
  updates appear a little later than on Alpaca. This is expected.
- **Daily profit target — trader config UI** is not yet built (backend enforcement
  is). Test the **subscriber** side for now.
- **Alpaca can't trade index options** (e.g. NDXP) — orders for those on an Alpaca
  account are rejected by Alpaca as "asset not found." This is a broker limitation,
  not a bug.

---

*Prepared for QA — covering commits and changes dated 21–23 Jul 2026.*
