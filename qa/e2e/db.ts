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
