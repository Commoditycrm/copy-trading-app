"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { PageLoading } from "@/components/PageLoading";
import type { User } from "@/lib/types";

/**
 * Discord — INBOUND alert-copying (Step 2: connection + real-time listener).
 *
 * Kopyaa monitors Discord Web as the trader's OWN logged-in account, reading
 * only the channels that account can already legitimately open. The trader signs
 * in themselves with the login helper — their password and MFA code go straight
 * to Discord and never reach us — and uploads only the resulting session, which
 * we store encrypted.
 *
 * This replaces the earlier bot-token + Channel-Following model: a bot can only
 * read channels it was invited to, which is never true of the third-party alert
 * servers traders actually follow.
 *
 * Deliberately separate from the OUTBOUND webhook broadcast (Settings → Discord
 * alerts), which posts the trader's own fills TO a channel.
 */
type SessionInfo = {
  present: boolean;
  cookie_count: number;
  captured_at: string | null;
  age_days: number | null;
};

type Pairing = {
  code: string;
  // pending | claimed | complete | failed
  status: string;
  error: string | null;
};

type LoginSession = {
  session_id: string;
  // pending | starting | awaiting_scan | scanned | complete | failed
  status: string;
  qr_image: string | null;
  error: string | null;
};

type DiscordSource = {
  id: string;
  label: string;
  channel_id: string;
  channel_name: string | null;
  guild_id: string | null;
  guild_name: string | null;
  is_enabled: boolean;
  // needs_login | connecting | connected | disconnected | error
  status: string;
  last_error: string | null;
  last_heartbeat_at: string | null;
  last_message_at: string | null;
  last_seen_message_id: string | null;
  created_at: string;
  session: SessionInfo;
  // Active window: always | market | extended | custom
  schedule_mode: string;
  schedule_start: string | null;
  schedule_end: string | null;
  schedule_timezone: string | null;
  schedule_days: number[];
  schedule_summary: string;
};

const SCHEDULE_LABEL: Record<string, string> = {
  always: "Always on",
  market: "US market hours",
  extended: "US extended hours",
  custom: "Custom hours",
};

const STATUS_LABEL: Record<string, string> = {
  off_schedule: "Outside hours",
  needs_login: "Sign-in needed",
  connecting: "Connecting",
  connected: "Connected",
  disconnected: "Off",
  error: "Error",
};

function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${Math.floor(secs)}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

export default function DiscordPage() {
  const [user, setUser] = useState<User | null>(null);
  const [sources, setSources] = useState<DiscordSource[]>([]);
  const [loading, setLoading] = useState(true);

  const [label, setLabel] = useState("");
  const [channelUrl, setChannelUrl] = useState("");
  const [adding, setAdding] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  // Which source is having its channel repointed, and to what.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editUrl, setEditUrl] = useState("");
  // QR login dialog: which source is connecting, and the live session.
  const [loginFor, setLoginFor] = useState<DiscordSource | null>(null);
  const [login, setLogin] = useState<LoginSession | null>(null);
  // Desktop Connector pairing — the primary way to connect a Discord account.
  const [pairFor, setPairFor] = useState<DiscordSource | null>(null);
  const [pair, setPair] = useState<Pairing | null>(null);

  // One hidden file input, retargeted at whichever source is uploading.
  const fileRef = useRef<HTMLInputElement | null>(null);
  const uploadTargetRef = useRef<string | null>(null);

  async function load() {
    try {
      setSources(await api<DiscordSource[]>("/api/discord-sources"));
    } catch (e) {
      notify.fromError(e, "Failed to load Discord channels");
    }
  }

  useEffect(() => {
    (async () => {
      try {
        const u = await api<User>("/api/auth/me");
        setUser(u);
        if (u.role === "trader") await load();
      } catch (e) {
        notify.fromError(e, "Failed to load");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // The listener reports status out-of-band, so poll while the page is open —
  // a source can go connecting → connected without any action from the user.
  useEffect(() => {
    if (!user || user.role !== "trader") return;
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [user]);

  async function addSource(e: FormEvent) {
    e.preventDefault();
    setAdding(true);
    try {
      const created = await api<DiscordSource>("/api/discord-sources", {
        method: "POST",
        body: JSON.stringify({ label: label.trim(), channel_url: channelUrl.trim() }),
      });
      setLabel("");
      setChannelUrl("");
      setSources((prev) => [created, ...prev]);
      notify.success("Channel added — now connect your Discord session below");
    } catch (e) {
      notify.fromError(e, "Could not add that channel — check the link");
    } finally {
      setAdding(false);
    }
  }

  async function toggleEnabled(s: DiscordSource, next: boolean) {
    setBusyId(s.id);
    setSources((prev) => prev.map((x) => (x.id === s.id ? { ...x, is_enabled: next } : x)));
    try {
      const updated = await api<DiscordSource>(`/api/discord-sources/${s.id}`, {
        method: "PATCH",
        body: JSON.stringify({ is_enabled: next }),
      });
      setSources((prev) => prev.map((x) => (x.id === s.id ? updated : x)));
    } catch (e) {
      setSources((prev) => prev.map((x) => (x.id === s.id ? { ...x, is_enabled: !next } : x)));
      notify.fromError(e, "Could not update");
    } finally {
      setBusyId(null);
    }
  }

  async function setSchedule(s: DiscordSource, mode: string) {
    setBusyId(s.id);
    try {
      const body: Record<string, unknown> = { schedule_mode: mode };
      if (mode === "custom" && !s.schedule_start) {
        // Seed a sane first window so "custom" is never a schedule that
        // matches nothing the moment it's selected.
        body.schedule_start = "09:30:00";
        body.schedule_end = "16:00:00";
        body.schedule_timezone = "America/New_York";
      }
      const updated = await api<DiscordSource>(`/api/discord-sources/${s.id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      setSources((prev) => prev.map((x) => (x.id === s.id ? updated : x)));
    } catch (e) {
      notify.fromError(e, "Could not update the schedule");
    } finally {
      setBusyId(null);
    }
  }

  async function setCustomWindow(s: DiscordSource, field: "start" | "end", value: string) {
    setBusyId(s.id);
    try {
      const updated = await api<DiscordSource>(`/api/discord-sources/${s.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          [`schedule_${field}`]: `${value}:00`,
          schedule_timezone: s.schedule_timezone || "America/New_York",
        }),
      });
      setSources((prev) => prev.map((x) => (x.id === s.id ? updated : x)));
    } catch (e) {
      notify.fromError(e, "Could not update the window");
    } finally {
      setBusyId(null);
    }
  }

  async function saveChannel(s: DiscordSource) {
    setBusyId(s.id);
    try {
      const updated = await api<DiscordSource>(`/api/discord-sources/${s.id}`, {
        method: "PATCH",
        body: JSON.stringify({ channel_url: editUrl.trim() }),
      });
      setSources((prev) => prev.map((x) => (x.id === s.id ? updated : x)));
      setEditingId(null);
      setEditUrl("");
      notify.success("Channel updated — your Discord session is unchanged");
    } catch (e) {
      notify.fromError(e, "Could not update the channel — check the link");
    } finally {
      setBusyId(null);
    }
  }

  async function startPairing(src: DiscordSource) {
    setPairFor(src);
    setPair(null);
    try {
      setPair(await api<Pairing>(`/api/discord-sources/${src.id}/pair`, { method: "POST" }));
    } catch (e) {
      notify.fromError(e, "Could not start the connection");
      setPairFor(null);
    }
  }

  // Poll until the Connector finishes. The trader is signing in on their own
  // machine, so this is the only way we learn it worked.
  useEffect(() => {
    if (!pairFor || !pair) return;
    if (pair.status === "complete" || pair.status === "failed") return;

    const t = setInterval(async () => {
      try {
        const next = await api<Pairing>(
          `/api/discord-sources/${pairFor.id}/pair/${pair.code}`
        );
        setPair(next);
        if (next.status === "complete") {
          notify.success("Discord connected — the listener is starting up");
          setPairFor(null);
          setPair(null);
          load();
        }
      } catch {
        /* transient, or the code expired — the next tick retries */
      }
    }, 2500);
    return () => clearInterval(t);
  }, [pairFor, pair]);

  async function startLogin(src: DiscordSource) {
    setLoginFor(src);
    setLogin(null);
    try {
      setLogin(
        await api<LoginSession>(`/api/discord-sources/${src.id}/login`, { method: "POST" })
      );
    } catch (e) {
      notify.fromError(e, "Could not start Discord sign-in");
      setLoginFor(null);
    }
  }

  async function cancelLogin() {
    const src = loginFor;
    const session = login;
    setLoginFor(null);
    setLogin(null);
    // Close the attempt server-side too — otherwise a live QR sits in Redis
    // waiting to be scanned after the trader has walked away.
    if (src && session) {
      try {
        await api(`/api/discord-sources/${src.id}/login/${session.session_id}`, {
          method: "DELETE",
        });
      } catch {
        /* best effort — it expires on its own */
      }
    }
  }

  // Poll the login attempt while the dialog is open. The QR rotates, so this
  // also keeps the displayed code current rather than letting it expire.
  useEffect(() => {
    if (!loginFor || !login) return;
    if (login.status === "complete" || login.status === "failed") return;

    const t = setInterval(async () => {
      try {
        const next = await api<LoginSession>(
          `/api/discord-sources/${loginFor.id}/login/${login.session_id}`
        );
        setLogin(next);
        if (next.status === "complete") {
          notify.success("Discord connected — the listener is starting up");
          setLoginFor(null);
          setLogin(null);
          load();
        }
      } catch {
        /* transient — the next tick retries */
      }
    }, 2000);
    return () => clearInterval(t);
  }, [loginFor, login]);

  function pickSessionFile(sourceId: string) {
    uploadTargetRef.current = sourceId;
    fileRef.current?.click();
  }

  async function onSessionFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    const sourceId = uploadTargetRef.current;
    // Reset immediately so re-picking the same file still fires a change event.
    e.target.value = "";
    if (!file || !sourceId) return;

    setBusyId(sourceId);
    try {
      const storage_state = JSON.parse(await file.text());
      const updated = await api<DiscordSource>(`/api/discord-sources/${sourceId}/session`, {
        method: "PUT",
        body: JSON.stringify({ storage_state }),
      });
      setSources((prev) => prev.map((x) => (x.id === sourceId ? updated : x)));
      notify.success("Discord session connected — the listener is starting up");
    } catch (err) {
      notify.fromError(err, "That session file couldn't be used — re-run the login helper");
    } finally {
      setBusyId(null);
      uploadTargetRef.current = null;
    }
  }

  async function clearSession(s: DiscordSource) {
    setBusyId(s.id);
    try {
      const updated = await api<DiscordSource>(`/api/discord-sources/${s.id}/session`, {
        method: "DELETE",
      });
      setSources((prev) => prev.map((x) => (x.id === s.id ? updated : x)));
      notify.success("Discord session removed");
    } catch (e) {
      notify.fromError(e, "Could not remove the session");
    } finally {
      setBusyId(null);
    }
  }

  async function deleteSource(s: DiscordSource) {
    setBusyId(s.id);
    try {
      await api(`/api/discord-sources/${s.id}`, { method: "DELETE" });
      setSources((prev) => prev.filter((x) => x.id !== s.id));
      notify.success("Removed");
    } catch (e) {
      notify.fromError(e, "Could not remove");
    } finally {
      setBusyId(null);
    }
  }

  if (loading || !user) return <PageLoading />;

  if (user.role !== "trader") {
    return (
      <div className="max-w-[820px] mx-auto">
        <div className="card p-8 text-center text-sm" style={{ color: "var(--muted)" }}>
          Connecting a Discord alert channel is a trader feature — it reads a channel you connect and
          (later) places its alerts as trades on your account.
        </div>
      </div>
    );
  }

  const inputCls = "w-full rounded-lg border px-3 py-2 text-sm bg-transparent focus-ring";
  const inputStyle = { borderColor: "var(--border)", color: "var(--text)" } as const;

  return (
    <div className="max-w-[820px] mx-auto space-y-4 pb-12">
      <input
        ref={fileRef}
        type="file"
        accept="application/json,.json"
        className="hidden"
        onChange={onSessionFile}
      />

      <div>
        <h1 className="text-2xl font-semibold tracking-tight" style={{ color: "var(--text)" }}>
          Discord
        </h1>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          Watch a Discord channel for trade alerts and (in an upcoming release) place the matching
          trades on your connected broker. Kopyaa reads the channel as <strong>you</strong> — only
          channels your own Discord account can already open.
        </p>
        <p className="text-xs mt-2" style={{ color: "var(--muted)" }}>
          This is separate from the alert <strong>broadcast</strong> (Settings), which posts{" "}
          <em>your</em> fills out to a channel.
        </p>
      </div>

      {/* How it works — lead with where the credentials go, since that's the question */}
      <div className="card p-5" style={{ background: "var(--surface-2, transparent)" }}>
        <h3 className="text-sm font-semibold mb-2" style={{ color: "var(--text)" }}>
          How it works
        </h3>
        <p className="text-sm" style={{ color: "var(--text-2)" }}>
          You sign in to Discord yourself, in a real browser window on your own machine. Your password
          and 2FA code go straight to Discord — Kopyaa never sees them. You then hand us only the
          resulting <strong>session</strong>, which we store encrypted and use to keep the channel open
          and watch for new messages in real time.
        </p>
        <div
          className="text-xs mt-3 flex items-center gap-2 flex-wrap"
          style={{ color: "var(--muted)" }}
        >
          <span className="chip">You sign in to Discord</span>
          <span aria-hidden>──session──▶</span>
          <span className="chip">Kopyaa (encrypted)</span>
          <span aria-hidden>──watches──▶</span>
          <span className="chip">#channel</span>
        </div>
      </div>

      {/* Add form */}
      <form onSubmit={addSource} className="card p-5 space-y-3">
        <h3 className="text-sm font-semibold" style={{ color: "var(--text)" }}>
          Add a channel to watch
        </h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label className="text-xs font-medium" style={{ color: "var(--muted)" }}>
              Name this source
            </label>
            <input
              className={`${inputCls} mt-1.5`}
              style={inputStyle}
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="e.g. OptionHaven Alerts"
              required
            />
          </div>
          <div>
            <label className="text-xs font-medium" style={{ color: "var(--muted)" }}>
              Channel link
            </label>
            <input
              className={`${inputCls} mt-1.5`}
              style={inputStyle}
              value={channelUrl}
              onChange={(e) => setChannelUrl(e.target.value)}
              placeholder="https://discord.com/channels/…/…"
              required
            />
          </div>
        </div>
        <p className="text-[11px]" style={{ color: "var(--muted)" }}>
          Open the channel in Discord and copy the address from your browser&apos;s address bar.
        </p>
        <div className="flex justify-end">
          <button
            type="submit"
            disabled={adding}
            className="btn-primary px-4 py-2 text-sm inline-flex items-center gap-1.5 disabled:opacity-60"
          >
            {adding && <Spinner />} Add channel
          </button>
        </div>
      </form>

      {/* Connected channels */}
      <div className="card p-5">
        <h3 className="text-sm font-semibold mb-3" style={{ color: "var(--text)" }}>
          Watched channels{" "}
          {sources.length > 0 && <span style={{ color: "var(--muted)" }}>({sources.length})</span>}
        </h3>
        {sources.length === 0 ? (
          <p className="text-sm py-2" style={{ color: "var(--muted)" }}>
            No channels yet. Add one above, then connect your Discord session.
          </p>
        ) : (
          <div className="flex flex-col divide-y" style={{ borderColor: "var(--border)" }}>
            {sources.map((s) => (
              <div key={s.id} className="py-3 first:pt-0 last:pb-0 space-y-2">
                <div className="flex items-center gap-3 flex-wrap">
                  <div className="min-w-0 flex-1">
                    <div
                      className="text-sm font-medium truncate"
                      style={{ color: "var(--text)" }}
                    >
                      {s.label}
                    </div>
                    <div className="text-xs truncate" style={{ color: "var(--muted)" }}>
                      {s.guild_name ? `${s.guild_name} · ` : ""}
                      {s.channel_name ? `#${s.channel_name}` : `Channel ${s.channel_id}`}
                    </div>
                  </div>

                  <span
                    className={
                      s.status === "connected"
                        ? "chip chip-good"
                        : s.status === "error"
                          ? "chip chip-bad"
                          : "chip"
                    }
                  >
                    <span
                      className="inline-block rounded-full"
                      style={{ width: 6, height: 6, background: "currentColor" }}
                    />
                    {STATUS_LABEL[s.status] ?? s.status}
                  </span>

                  <label
                    className="flex items-center gap-2 text-xs cursor-pointer select-none"
                    title="Enable/disable this source"
                  >
                    <input
                      type="checkbox"
                      className="h-4 w-4 cursor-pointer"
                      style={{ accentColor: "var(--accent)" }}
                      checked={s.is_enabled}
                      disabled={busyId === s.id}
                      onChange={(e) => toggleEnabled(s, e.target.checked)}
                    />
                    <span style={{ color: "var(--text-2)" }}>{s.is_enabled ? "On" : "Off"}</span>
                  </label>

                  <button
                    type="button"
                    onClick={() => {
                      setEditingId(editingId === s.id ? null : s.id);
                      setEditUrl("");
                    }}
                    disabled={busyId === s.id}
                    className="btn-ghost px-3 py-1.5 text-xs disabled:opacity-60"
                    title="Point this source at a different channel — keeps your Discord session"
                  >
                    Change channel
                  </button>
                  <button
                    type="button"
                    onClick={() => startPairing(s)}
                    disabled={busyId === s.id}
                    className={`${s.session.present ? "btn-ghost" : "btn-primary"} px-3 py-1.5 text-xs disabled:opacity-60`}
                  >
                    {s.session.present ? "Reconnect Discord" : "Connect Discord"}
                  </button>
                  {s.session.present && (
                    <button
                      type="button"
                      onClick={() => clearSession(s)}
                      disabled={busyId === s.id}
                      className="btn-ghost px-3 py-1.5 text-xs disabled:opacity-60"
                      title="Forget the stored Discord session but keep this channel configured"
                    >
                      Sign out
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => deleteSource(s)}
                    disabled={busyId === s.id}
                    className="btn-danger-soft px-3 py-1.5 text-xs disabled:opacity-60"
                  >
                    Remove
                  </button>
                </div>

                {editingId === s.id && (
                  <div className="flex items-center gap-2 flex-wrap">
                    <input
                      className={inputCls}
                      style={{ ...inputStyle, maxWidth: 420 }}
                      value={editUrl}
                      onChange={(e) => setEditUrl(e.target.value)}
                      placeholder="https://discord.com/channels/…/…"
                      autoFocus
                    />
                    <button
                      type="button"
                      onClick={() => saveChannel(s)}
                      disabled={busyId === s.id || !editUrl.trim()}
                      className="btn-primary px-3 py-1.5 text-xs disabled:opacity-60"
                    >
                      {busyId === s.id ? <Spinner /> : "Save"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setEditingId(null)}
                      className="btn-ghost px-3 py-1.5 text-xs"
                    >
                      Cancel
                    </button>
                  </div>
                )}

                {/* Active window — outside it, no session is held open */}
                <div className="flex items-center gap-2 flex-wrap text-xs">
                  <span style={{ color: "var(--muted)" }}>Watch:</span>
                  <select
                    value={s.schedule_mode}
                    disabled={busyId === s.id}
                    onChange={(e) => setSchedule(s, e.target.value)}
                    className="rounded-md border px-2 py-1 bg-transparent focus-ring"
                    style={{ borderColor: "var(--border)", color: "var(--text)" }}
                  >
                    {Object.entries(SCHEDULE_LABEL).map(([v, label]) => (
                      <option key={v} value={v} style={{ background: "var(--surface, #111)" }}>
                        {label}
                      </option>
                    ))}
                  </select>

                  {s.schedule_mode === "custom" && (
                    <>
                      <input
                        type="time"
                        defaultValue={(s.schedule_start || "09:30:00").slice(0, 5)}
                        disabled={busyId === s.id}
                        onBlur={(e) => setCustomWindow(s, "start", e.target.value)}
                        className="rounded-md border px-2 py-1 bg-transparent focus-ring"
                        style={{ borderColor: "var(--border)", color: "var(--text)" }}
                      />
                      <span style={{ color: "var(--muted)" }}>to</span>
                      <input
                        type="time"
                        defaultValue={(s.schedule_end || "16:00:00").slice(0, 5)}
                        disabled={busyId === s.id}
                        onBlur={(e) => setCustomWindow(s, "end", e.target.value)}
                        className="rounded-md border px-2 py-1 bg-transparent focus-ring"
                        style={{ borderColor: "var(--border)", color: "var(--text)" }}
                      />
                      <span style={{ color: "var(--muted)" }}>
                        {s.schedule_timezone || "America/New_York"}
                      </span>
                    </>
                  )}

                  {s.schedule_mode !== "always" && (
                    <span style={{ color: "var(--warn, #b45309)" }}>
                      · alerts posted outside these hours are not received
                    </span>
                  )}
                </div>

                <div className="text-xs flex gap-4 flex-wrap" style={{ color: "var(--muted)" }}>
                  <span>Last message: {relativeTime(s.last_message_at)}</span>
                  <span>Heartbeat: {relativeTime(s.last_heartbeat_at)}</span>
                  <span>
                    Session:{" "}
                    {s.session.present
                      ? `active${s.session.age_days !== null ? ` · ${s.session.age_days}d old` : ""}`
                      : "not connected"}
                  </span>
                </div>

                {s.status === "error" && s.last_error && (
                  <div className="text-xs" style={{ color: "var(--danger, #b91c1c)" }}>
                    {s.last_error}
                  </div>
                )}
                {s.status === "needs_login" && (
                  <div className="text-xs" style={{ color: "var(--warn, #b45309)" }}>
                    Discord needs you to sign in again — run the login helper and upload the new
                    session.
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Connector pairing dialog — the primary way to connect Discord */}
      {pairFor && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: "rgba(0,0,0,0.6)" }}
          onClick={() => { setPairFor(null); setPair(null); }}
        >
          <div
            className="card p-6 max-w-[460px] w-full text-center"
            style={{ background: "var(--surface, #111)" }}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold" style={{ color: "var(--text)" }}>
              Connect Discord
            </h3>
            <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
              {pairFor.label}
            </p>

            <ol
              className="text-sm mt-5 space-y-2 list-decimal pl-5 text-left"
              style={{ color: "var(--text-2)" }}
            >
              <li>
                Open the <strong>Kopyaa Connector</strong> app on your computer.
              </li>
              <li>Enter this code:</li>
            </ol>

            <div
              className="mt-3 mx-auto rounded-xl py-4 font-mono tracking-[0.2em] text-2xl"
              style={{ background: "var(--surface-2, #1a1d23)", color: "var(--text)" }}
            >
              {pair ? pair.code : "…"}
            </div>
            <p className="text-[11px] mt-2" style={{ color: "var(--muted)" }}>
              Expires in 10 minutes · single use
            </p>

            <ol
              className="text-sm mt-4 space-y-2 list-decimal pl-5 text-left"
              style={{ color: "var(--text-2)" }}
              start={3}
            >
              <li>Sign in to Discord in the browser window it opens.</li>
            </ol>

            <div className="mt-4 text-sm" style={{ color: "var(--text-2)" }}>
              {pair?.status === "claimed" ? (
                <strong>Connector found — waiting for you to sign in…</strong>
              ) : pair?.status === "failed" ? (
                <span style={{ color: "var(--danger, #b91c1c)" }}>
                  {pair.error || "Connection failed."}
                </span>
              ) : (
                <span style={{ color: "var(--muted)" }}>Waiting for the Connector…</span>
              )}
            </div>

            <p className="text-[11px] mt-3" style={{ color: "var(--muted)" }}>
              You sign in on your own computer, in your own browser. Kopyaa never sees your
              Discord password or 2FA code — only the resulting session, stored encrypted.
            </p>

            <div className="mt-5 flex justify-center gap-2">
              <button
                type="button"
                onClick={() => { setPairFor(null); setPair(null); }}
                className="btn-ghost px-4 py-2 text-sm"
              >
                Cancel
              </button>
              {pair?.status === "failed" && (
                <button
                  type="button"
                  onClick={() => startPairing(pairFor)}
                  className="btn-primary px-4 py-2 text-sm"
                >
                  New code
                </button>
              )}
            </div>

            <button
              type="button"
              onClick={() => {
                const src = pairFor;
                setPairFor(null);
                setPair(null);
                startLogin(src);
              }}
              className="mt-4 text-[11px] underline"
              style={{ color: "var(--muted)" }}
            >
              No Connector? Try scanning a QR code instead
            </button>
          </div>
        </div>
      )}

      {/* QR sign-in dialog */}
      {loginFor && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: "rgba(0,0,0,0.6)" }}
          onClick={cancelLogin}
        >
          <div
            className="card p-6 max-w-[420px] w-full text-center"
            style={{ background: "var(--surface, #111)" }}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold" style={{ color: "var(--text)" }}>
              Connect Discord
            </h3>
            <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
              {loginFor.label}
            </p>

            <div
              className="mt-5 mx-auto flex items-center justify-center rounded-xl"
              style={{ width: 220, height: 220, background: "#fff" }}
            >
              {login?.qr_image ? (
                // Discord rotates this code; the poll above swaps in a fresh one.
                <img
                  src={login.qr_image}
                  alt="Discord login QR code"
                  style={{ width: 200, height: 200, objectFit: "contain" }}
                />
              ) : (
                <div style={{ color: "#666" }} className="text-xs flex flex-col items-center gap-2">
                  <Spinner />
                  Opening Discord…
                </div>
              )}
            </div>

            <div className="mt-4 text-sm" style={{ color: "var(--text-2)" }}>
              {login?.status === "scanned" ? (
                <strong>Scanned — now approve the sign-in on your phone.</strong>
              ) : login?.status === "failed" ? (
                <span style={{ color: "var(--danger, #b91c1c)" }}>
                  {login.error || "Sign-in failed."}
                </span>
              ) : (
                <>
                  Open <strong>Discord on your phone</strong> → tap your avatar →{" "}
                  <strong>Scan QR Code</strong>, and point it at this code.
                </>
              )}
            </div>

            <p className="text-[11px] mt-3" style={{ color: "var(--muted)" }}>
              You sign in on your own device. Kopyaa never sees your Discord password or
              2FA code — only the resulting session, stored encrypted.
            </p>

            <div className="mt-5 flex justify-center gap-2">
              <button type="button" onClick={cancelLogin} className="btn-ghost px-4 py-2 text-sm">
                Cancel
              </button>
              {login?.status === "failed" && (
                <button
                  type="button"
                  onClick={() => startLogin(loginFor)}
                  className="btn-primary px-4 py-2 text-sm"
                >
                  Try again
                </button>
              )}
            </div>

            <button
              type="button"
              onClick={() => {
                const id = loginFor.id;
                cancelLogin();
                pickSessionFile(id);
              }}
              className="mt-4 text-[11px] underline"
              style={{ color: "var(--muted)" }}
            >
              Can&apos;t scan? Upload a session file instead
            </button>
          </div>
        </div>
      )}

      {/* Setup instructions */}
      <div className="card p-5">
        <h3 className="text-sm font-semibold mb-3" style={{ color: "var(--text)" }}>
          Connecting your Discord session
        </h3>
        <ol className="text-sm space-y-2 list-decimal pl-5" style={{ color: "var(--text-2)" }}>
          <li>
            Open the alert channel in Discord and copy the link from your address bar, then
            add it above.
          </li>
          <li>
            Click <strong>Connect Discord</strong>. Kopyaa shows a pairing code.
          </li>
          <li>
            Open the <strong>Kopyaa Connector</strong> app and enter that code.
          </li>
          <li>
            Sign in to Discord in the browser window it opens — on your own computer, as
            you normally would.
          </li>
          <li>That&apos;s it. Monitoring starts automatically.</li>
        </ol>
        <p className="text-[11px] mt-3" style={{ color: "var(--muted)" }}>
          Kopyaa only reads channels your Discord account can already open, and never posts or
          interacts as you. You can revoke access at any time with <strong>Sign out</strong> here, or
          by logging out of that session in Discord. Reading alerts and placing trades roll out in
          later updates.
        </p>
      </div>
    </div>
  );
}
