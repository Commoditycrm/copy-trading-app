# User Manual — Login & Connect Your Alpaca Account

Welcome. This guide walks you through two things:

1. **Logging in to the app**
2. **Connecting your Alpaca brokerage account**

Total time: about 5 minutes. You'll need your login credentials (sent to
you separately) and a few minutes to generate an Alpaca API key.

---

## Part 1 — Log In

### Step 1.1 — Open the app

Go to the URL we sent you (for example, `https://app.yourdomain.com`).
You'll see the **Sign In** screen.

### Step 1.2 — Sign in

Enter:

- **Email** — the address we registered for your account
- **Password** — the password sent to you separately

Click **Sign in**.

If your password isn't working, contact support — do not reuse it
elsewhere.

### Step 1.3 — First-time landing

On a successful login, you'll land on your role's home page:

- **Traders** land on **Trade Panel** — this is where you place
  trades that get mirrored to your subscribers.
- **Subscribers** land on **Positions** — this is where you see what
  you currently hold, mirrored from the trader you follow.

The left sidebar shows your business name and navigation. The top bar
shows your broker connection status and a small bell for notifications.

> **If you don't have an account yet**, click **Create an account** at
> the bottom of the Sign In screen. Choose **Trader** or **Subscriber**.
> Traders must provide a **Business Name** — this is what your
> subscribers will see in their app.

---

## Part 2 — Connect Your Alpaca Account

The app needs an Alpaca API key to place and monitor trades on your
behalf. This is a one-time setup.

### Step 2.1 — Open your Alpaca dashboard

In a new browser tab, go to **<https://app.alpaca.markets>** and sign in
with your Alpaca account credentials.

> Don't have an Alpaca account yet? Sign up for free at
> **<https://alpaca.markets/>**. You can use a **paper** (fake-money)
> account for testing or a **live** account for real trading.

### Step 2.2 — Choose Paper or Live

In Alpaca's dashboard, the toggle at the **top-left** lets you switch
between:

- **Paper** — risk-free practice trading with fake money
- **Live** — real trading with real money

Pick whichever you want to connect. Each has its own set of API keys —
you cannot use a Paper key on a Live account or vice versa.

> **Recommendation:** start with **Paper** to confirm everything works
> before connecting a live account.

### Step 2.3 — Generate API keys

In your Alpaca dashboard:

1. Click your name (top-right) → **Account** → **Manage Accounts**.
2. Open your account and look for the **API Keys** section (under
   "Trading API" on the left).
3. Click **Generate New Key**.
4. Alpaca will show you two values:
   - **API Key ID** — looks like `PKxxxxxxxxxxxxxxxxxx`
   - **Secret Key** — looks like `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`

> **IMPORTANT:** the Secret Key is shown only once. Copy both values
> to a secure place immediately. If you lose the Secret Key, you'll
> need to regenerate (which invalidates the old one).

### Step 2.4 — Open the Brokers page in the app

Back in our app, in the left sidebar click **Broker**.

The Brokers page shows all your connected broker accounts. If this is
your first connection, the list will be empty.

### Step 2.5 — Fill in the Alpaca form

Find the **Alpaca** card. You'll need to fill in:

| Field        | What to enter                                                          |
| ------------ | ---------------------------------------------------------------------- |
| **Label**    | A friendly name you'll recognize. e.g. `My Alpaca Paper`               |
| **API Key**  | The **API Key ID** you copied from Alpaca                              |
| **Secret**   | The **Secret Key** you copied from Alpaca                              |
| **Mode**     | Select **Paper** if the keys are for paper trading, **Live** otherwise |

Click **Connect**.

### Step 2.6 — Confirm the connection worked

If everything is correct, within 1–2 seconds you'll see:

- A green **"Alpaca connected — balance fetched"** toast at the bottom
- Your account appearing in the connected-brokers list with its cash
  balance, buying power, and total equity displayed
- The top-bar pill switches to **"Broker live"** with a green dot

If the connection fails, the toast will tell you why. The most common
causes:

- **Wrong key/secret** — double-check that you copied both correctly
  (no extra spaces)
- **Paper/Live mismatch** — the key is for paper but you selected Live,
  or vice versa
- **Insufficient permissions** — make sure the key has trading
  permissions enabled in your Alpaca dashboard

### Step 2.7 — Verify the listener is running

Under your connected Alpaca card, you'll see three checkboxes:

- ✅ **Auto Pull Orders** — keep this ON
- ✅ **Bring Open Orders** — keep this ON
- ✅ **Bring Filled Orders** — keep this ON

These control which orders the app monitors. **All three should be on by
default.** If you turn any off, you'll miss those orders in your Order
History.

---

## Part 3 — You're Ready

Once connected, you can:

- **Traders:** go to **Trade Panel**, place a stock or option trade —
  it'll fan out to your subscribers automatically.
- **Subscribers:** go to **Settings** and configure your risk controls
  (TP/SL, daily loss limit, etc.). Then any trader you follow will
  copy their trades into your account.

---

## Troubleshooting

### "Sign in failed" / "Invalid credentials"

- Confirm the email is the one we registered for you (case-insensitive)
- Make sure caps lock is off when typing your password
- If you've recently been issued a new password, use the **most recent**
  one

### "Alpaca connected" but no orders show up

Check **the Broker page** — confirm all three boxes (Auto Pull / Open /
Filled) are checked. If they are, place a small test order on Alpaca's
own dashboard — it should appear in **Order History** within a few
seconds. If it doesn't, contact support and mention "listener not
firing" with your account email.

### "Connection failed: invalid credentials"

The API key or secret was wrong, or the Paper/Live toggle doesn't
match the keys' mode. Try regenerating new keys in Alpaca and
reconnecting.

### "Broker disconnected" warning

Sometimes Alpaca rotates connection state. Just click **Reconnect** on
your broker card and the same keys should work again.

### Need help?

Email **<support@yourdomain.com>** with:

- Your account email
- A screenshot of the issue
- What you were doing when it happened

---

## Quick Reference

| Action              | Where                                       |
| ------------------- | ------------------------------------------- |
| Sign in             | `/login`                                    |
| Connect Alpaca      | Sidebar → **Broker** → Alpaca card          |
| Place a trade       | Sidebar → **Trade Panel** (traders only)    |
| See your positions  | Sidebar → **Positions**                     |
| See order history   | Sidebar → **Order History**                 |
| Configure risk      | Sidebar → **Settings** (subscribers only)   |
| Sign out            | Sidebar bottom → **Sign out**               |
| Alpaca dashboard    | <https://app.alpaca.markets>                |
| Alpaca sign-up      | <https://alpaca.markets/>                   |

---

*Last updated: 2026-06-12*
