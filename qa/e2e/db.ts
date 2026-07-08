/**
 * Direct e2e-DB access for arrange/assert steps that have no API path
 * (seeding notifications, seeding broker balances, reading read_at). Kept
 * separate from app code — this suite stays portable.
 */
import { Client } from "pg";

const CONN = process.env.E2E_DB_URL || "postgresql://trading:trading@localhost:5433/trading_app_e2e";

export async function withDb<T>(fn: (c: Client) => Promise<T>): Promise<T> {
  const c = new Client({ connectionString: CONN });
  await c.connect();
  try {
    return await fn(c);
  } finally {
    await c.end();
  }
}

/** Seed a connected broker row with a cached balance (GET /api/brokers reads
 *  these fields directly — no decrypt on list). */
export async function seedBroker(
  userId: string,
  opts: { equity: number; buyingPower?: number; cash?: number },
): Promise<void> {
  await withDb((c) =>
    c.query(
      `INSERT INTO broker_accounts
         (id,user_id,broker,label,is_paper,supports_fractional,encrypted_credentials,
          connection_status,total_equity,buying_power,cash,currency,created_at,updated_at)
       VALUES (gen_random_uuid(),$1,'alpaca','QA Seed',true,true,'seed',
          'connected',$2,$3,$4,'USD',now(),now())`,
      [userId, opts.equity, opts.buyingPower ?? 0, opts.cash ?? 0],
    ),
  );
}

/** Point a subscriber's settings at a trader and set copy on/off. */
export async function setFollowing(subUserId: string, traderId: string, copyEnabled: boolean): Promise<void> {
  await withDb((c) =>
    c.query(
      `UPDATE subscriber_settings SET following_trader_id=$2, copy_enabled=$3 WHERE user_id=$1`,
      [subUserId, traderId, copyEnabled],
    ),
  );
}

export async function seedNotification(
  userId: string,
  opts: { type?: string; message: string; metadata?: object | null; read?: boolean; ageSeconds?: number },
): Promise<string> {
  const created = opts.ageSeconds ? `now() - interval '${Math.floor(opts.ageSeconds)} seconds'` : "now()";
  return withDb(async (c) => {
    const r = await c.query(
      `INSERT INTO notifications (id,user_id,type,message,metadata_json,read_at,created_at)
       VALUES (gen_random_uuid(),$1,$2,$3,$4::jsonb,$5,${created}) RETURNING id`,
      [
        userId,
        opts.type ?? "copy.retry_failed",
        opts.message,
        opts.metadata ? JSON.stringify(opts.metadata) : null,
        opts.read ? new Date() : null,
      ],
    );
    return r.rows[0].id as string;
  });
}

export async function seedManyNotifications(userId: string, count: number, message = "Mirror order failed after retry"): Promise<void> {
  await withDb(async (c) => {
    for (let i = 0; i < count; i++) {
      await c.query(
        `INSERT INTO notifications (id,user_id,type,message,metadata_json,read_at,created_at)
         VALUES (gen_random_uuid(),$1,'copy.retry_failed',$2,NULL,NULL, now() - ($3 || ' seconds')::interval)`,
        [userId, `${message} #${i}`, String(i)],
      );
    }
  });
}

export async function getNotificationReadAt(id: string): Promise<string | null> {
  return withDb(async (c) => {
    const r = await c.query(`SELECT read_at FROM notifications WHERE id=$1`, [id]);
    return r.rows.length ? r.rows[0].read_at : null;
  });
}

export async function countUnread(userId: string): Promise<number> {
  return withDb(async (c) => {
    const r = await c.query(
      `SELECT count(*)::int AS n FROM notifications WHERE user_id=$1 AND read_at IS NULL`,
      [userId],
    );
    return r.rows[0].n as number;
  });
}

// ── Sprint 4: orders / fills / fake broker ──────────────────────────────────

export interface SeedOrderOpts {
  instrumentType?: "stock" | "option";
  symbol?: string;
  side?: "buy" | "sell";
  orderType?: "market" | "limit";
  quantity?: number;
  status?: string;
  filledQuantity?: number;
  filledAvgPrice?: number | null;
  limitPrice?: number | null;
  fannedOut?: boolean;
  bracketParentId?: string | null;
  bracketLeg?: "tp" | "sl" | null;
  ageSeconds?: number;
  optionExpiry?: string | null;
  optionStrike?: number | null;
  optionRight?: "call" | "put" | null;
}

export async function seedOrder(userId: string, opts: SeedOrderOpts = {}): Promise<string> {
  const status = opts.status ?? "filled";
  const qty = opts.quantity ?? 1;
  const filledQty = opts.filledQuantity ?? (status === "filled" ? qty : 0);
  const filledPx = opts.filledAvgPrice ?? (status === "filled" ? 100 : null);
  const ts = `now() - interval '${Math.floor(opts.ageSeconds ?? 0)} seconds'`;
  // The order enum columns store UPPERCASE member NAMES in Postgres (no
  // values_callable on the model), while the API I/O uses lowercase values.
  const up = (s?: string | null) => (s == null ? null : s.toUpperCase());
  return withDb(async (c) => {
    const r = await c.query(
      `INSERT INTO orders
        (id,user_id,broker_account_id,instrument_type,symbol,option_expiry,option_strike,option_right,
         side,order_type,quantity,limit_price,status,filled_quantity,filled_avg_price,
         bracket_parent_id,bracket_leg,fanned_out_to_subscribers,retry_count,is_closing,
         submitted_at,created_at,updated_at)
       VALUES (gen_random_uuid(),$1,NULL,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,0,false,
         ${ts},${ts},${ts})
       RETURNING id`,
      [
        userId,
        up(opts.instrumentType ?? "stock"),
        opts.symbol ?? "AAPL",
        opts.optionExpiry ?? null,
        opts.optionStrike ?? null,
        up(opts.optionRight ?? null),
        up(opts.side ?? "buy"),
        up(opts.orderType ?? "market"),
        qty,
        opts.limitPrice ?? null,
        up(status),
        filledQty,
        filledPx,
        opts.bracketParentId ?? null,
        opts.bracketLeg ?? null,
        opts.fannedOut ?? false,
      ],
    );
    return r.rows[0].id as string;
  });
}

export async function seedFill(
  orderId: string,
  opts: { quantity: number; price: number; fee?: number; ageSeconds?: number },
): Promise<void> {
  const ts = `now() - interval '${Math.floor(opts.ageSeconds ?? 0)} seconds'`;
  await withDb((c) =>
    c.query(
      `INSERT INTO fills (id,order_id,quantity,price,fee,filled_at)
       VALUES (gen_random_uuid(),$1,$2,$3,$4, ${ts})`,
      [orderId, opts.quantity, opts.price, opts.fee ?? 0],
    ),
  );
}

// A Fernet-encrypted dummy credential, valid for the local backend/.env
// CREDENTIAL_ENCRYPTION_KEY. The Fake adapter ignores the contents; it just
// needs to decrypt. Override via env if the local key rotates.
export const FAKE_BROKER_CREDS =
  process.env.E2E_FAKE_BROKER_CREDS ||
  "gAAAAABqTfJkJNBJkgFf9VvGu26Z5tDHy44WRpKncCwEasyVGs6HlT_uUB4Ks8_hROz9bPsvzjRnU5nb9megUM1WFkr76iaVuN4sVfj1jdUKzk9hhBEy5pc=";

/** Insert a FAKE broker with a Fernet-encrypted creds blob (connected by default). */
export async function seedFakeBroker(
  userId: string,
  connectionStatus: string = "connected",
  encryptedCreds: string = FAKE_BROKER_CREDS,
): Promise<string> {
  return withDb(async (c) => {
    const r = await c.query(
      `INSERT INTO broker_accounts
         (id,user_id,broker,label,is_paper,supports_fractional,encrypted_credentials,
          connection_status,created_at,updated_at)
       VALUES (gen_random_uuid(),$1,'fake','QA Fake',true,true,$3,$2,now(),now())
       RETURNING id`,
      [userId, connectionStatus, encryptedCreds],
    );
    return r.rows[0].id as string;
  });
}

export async function getOrderStatus(id: string): Promise<string | null> {
  return withDb(async (c) => {
    const r = await c.query(`SELECT status FROM orders WHERE id=$1`, [id]);
    return r.rows.length ? (r.rows[0].status as string) : null;
  });
}
