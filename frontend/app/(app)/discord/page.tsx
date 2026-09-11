"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { Hash, Radio, ScanLine, Receipt, ShieldCheck, Clock, Eye, PlugZap, Check, Trash2 } from "lucide-react";
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

const STATUS_TONE: Record<string, string> = {
  connected: "var(--good)",
  connecting: "var(--accent-2)",
  needs_login: "var(--warn, #b45309)",
  error: "var(--bad)",
  off_schedule: "var(--muted)",
  disconnected: "var(--muted)",
};

const STATUS_LABEL: Record<string, string> = {
  off_schedule: "Outside hours",
  needs_login: "Sign-in needed",
  connecting: "Connecting",
  connected: "Connected",
  disconnected: "Off",
  error: "Error",
};

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
      <div className="max-w-[820px]">
        <div className="card p-8 text-center text-sm" style={{ color: "var(--muted)" }}>
          Connecting a Discord alert channel is a trader feature — it reads a channel you connect and
          (later) places its alerts as trades on your account.
        </div>
      </div>
    );
  }

  // The Connect card acts on the first channel still waiting for a sign-in, so
  // the button always has an unambiguous target rather than asking the trader
  // to pick one.
  const needsConnect = sources.find((x) => !x.session.present) ?? null;
  // The session lives on the Discord ACCOUNT, so any channel reporting one
  // means the account is connected and every channel is covered.
  const accountConnected = sources.some((x) => x.session.present);

  const inputCls = "w-full rounded-lg border px-3 py-2 text-sm bg-transparent focus-ring";
  const inputStyle = { borderColor: "var(--border)", color: "var(--text)" } as const;

  return (
    <div className="max-w-[1200px] space-y-4 pb-12">
      <input
        ref={fileRef}
        type="file"
        accept="application/json,.json"
        className="hidden"
        onChange={onSessionFile}
      />

      {/* ── Feature introduction ───────────────────────────────────────────
          Traders arrive here with no idea what this does. Lead with the whole
          pipeline in one glance, then the guarantees, before asking them to
          connect anything. */}
      <div className="relative overflow-hidden card p-5">
        {/* Soft accent wash — depth without a heavy gradient behind text. */}
        <div
          aria-hidden
          className="absolute -top-20 -right-14 w-60 h-60 rounded-full pointer-events-none"
          style={{ background: "var(--accent-glow)", filter: "blur(48px)", opacity: 0.55 }}
        />
        <div className="relative">
          <div className="flex items-center gap-2">
            <span
              className="inline-flex items-center gap-1.5 text-[11px] font-medium px-2 py-1 rounded-full"
              style={{
                background: "var(--panel-2)",
                color: "var(--accent-2)",
                border: "1px solid var(--border)",
              }}
            >
              <Radio size={11} /> Live alert copying
            </span>
          </div>

          <h1
            className="text-[21px] leading-tight font-semibold tracking-tight mt-2.5"
            style={{ color: "var(--text)" }}
          >
            Turn a Discord channel into trade signals
          </h1>
          <p className="text-[13px] leading-relaxed mt-1.5 max-w-[680px]" style={{ color: "var(--text-2)" }}>
            Kopyaa watches an alert channel you already follow, reads each message the moment
            it&apos;s posted, and turns the ones that describe a trade into structured signals
            you can review in Order History.
          </p>

          {/* The pipeline, in one line. */}
          <div className="mt-4 flex items-stretch gap-2 flex-wrap">
            <PipelineStep
              icon={<Hash size={15} />}
              title="Discord channel"
              detail="Any channel your Discord account can open"
            />
            <PipelineArrow />
            <PipelineStep
              icon={<Radio size={15} />}
              title="Read live"
              detail="Detected the instant it renders"
            />
            <PipelineArrow />
            <PipelineStep
              icon={<ScanLine size={15} />}
              title="Parsed"
              detail="Symbol, strike, expiry, size, price"
            />
            <PipelineArrow />
            <PipelineStep
              icon={<Receipt size={15} />}
              title="Place orders"
              detail="On your connected broker"
              accent
              soon
            />
          </div>
        </div>
      </div>

      {/* Channel list beside the actions that feed it. */}
      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_340px] gap-4 items-start">
        <div className="min-w-0 space-y-4">
        {/* Watched channels — one row per source. Everything about a channel
            lives in its row: identity, live state, when it runs, and what you
            can do to it. */}
        <div className="card overflow-hidden">
          <div
            className="flex items-center justify-between px-5 py-3.5"
            style={{ borderBottom: "1px solid var(--border)" }}
          >
            <h3 className="text-sm font-semibold" style={{ color: "var(--text)" }}>
              Watched channels
            </h3>
            {sources.length > 0 && (
              <span
                className="text-[11px] px-2 py-0.5 rounded-full"
                style={{ background: "var(--panel-2)", color: "var(--muted)" }}
              >
                {sources.length}
              </span>
            )}
          </div>

          {sources.length === 0 ? (
            <div className="px-5 py-10 text-center">
              <div
                className="mx-auto flex items-center justify-center rounded-xl mb-3"
                style={{
                  width: 40, height: 40,
                  background: "var(--panel-2)", border: "1px solid var(--border)",
                }}
              >
                <Hash size={18} style={{ color: "var(--muted)" }} />
              </div>
              <p className="text-sm" style={{ color: "var(--text-2)" }}>No channels yet</p>
              <p className="text-[12px] mt-1" style={{ color: "var(--muted)" }}>
                Add one on the right, then connect your Discord account.
              </p>
            </div>
          ) : (
            <div className="flex flex-col">
              {sources.map((s, i) => {
                const tone = STATUS_TONE[s.status] ?? "var(--muted)";
                return (
                  <div
                    key={s.id}
                    className="px-5 py-4 space-y-3"
                    style={{ borderTop: i === 0 ? "none" : "1px solid var(--border)" }}
                  >
                    {/* Identity + live state */}
                    <div className="flex items-start gap-3">
                      <div
                        className="flex items-center justify-center rounded-xl shrink-0"
                        style={{
                          width: 34, height: 34,
                          background: "var(--panel-2)", border: "1px solid var(--border)",
                        }}
                      >
                        <Hash size={15} style={{ color: tone }} />
                      </div>

                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span
                            className="text-sm font-semibold truncate"
                            style={{ color: "var(--text)" }}
                          >
                            {s.label}
                          </span>
                          <span
                            className="inline-flex items-center gap-1.5 text-[11px] px-2 py-0.5 rounded-full"
                            style={{
                              color: tone,
                              background: "var(--panel-2)",
                              border: "1px solid var(--border)",
                            }}
                          >
                            <span
                              className="inline-block rounded-full"
                              style={{ width: 5, height: 5, background: tone }}
                            />
                            {STATUS_LABEL[s.status] ?? s.status}
                          </span>
                        </div>
                        <div className="text-[12px] truncate mt-0.5" style={{ color: "var(--muted)" }}>
                          {s.guild_name ? `${s.guild_name} · ` : ""}
                          {s.channel_name ? `#${s.channel_name}` : `Channel ${s.channel_id}`}
                        </div>
                      </div>

                      {/* On/off for this channel */}
                      <label
                        className="flex items-center gap-2 text-[11px] cursor-pointer select-none shrink-0"
                        title={s.is_enabled ? "Monitoring on" : "Monitoring off"}
                      >
                        <input
                          type="checkbox"
                          className="h-3.5 w-3.5 cursor-pointer"
                          style={{ accentColor: "var(--accent)" }}
                          checked={s.is_enabled}
                          disabled={busyId === s.id}
                          onChange={(e) => toggleEnabled(s, e.target.checked)}
                        />
                        <span style={{ color: "var(--text-2)" }}>{s.is_enabled ? "On" : "Off"}</span>
                      </label>
                    </div>

                    {/* When to watch — pills, so the choice is visible rather
                        than hidden behind a dropdown. */}
                    <div className="flex items-center gap-2 flex-wrap pl-[46px]">
                      <span className="text-[11px]" style={{ color: "var(--muted)" }}>Watch</span>
                      {/* Separate pills rather than one segmented block — each
                          option reads as its own choice, and the selected one
                          stands alone instead of being a lighter patch inside a
                          shared container. */}
                      {Object.entries(SCHEDULE_LABEL).map(([value, label]) => {
                        const active = s.schedule_mode === value;
                        return (
                          <button
                            key={value}
                            type="button"
                            disabled={busyId === s.id}
                            onClick={() => setSchedule(s, value)}
                            className="px-3 py-1 text-[11px] font-medium rounded-full transition-colors disabled:opacity-60"
                            style={{
                              background: active ? "var(--accent-glow)" : "transparent",
                              border: `1px solid ${active ? "rgba(44,147,197,0.45)" : "var(--border)"}`,
                              color: active ? "var(--accent-2)" : "var(--muted)",
                            }}
                          >
                            {label}
                          </button>
                        );
                      })}

                      {s.schedule_mode === "custom" && (
                        <>
                          <input
                            type="time"
                            defaultValue={(s.schedule_start || "09:30:00").slice(0, 5)}
                            disabled={busyId === s.id}
                            onBlur={(e) => setCustomWindow(s, "start", e.target.value)}
                            className="rounded-md border px-2 py-1 text-[11px] bg-transparent focus-ring"
                            style={{ borderColor: "var(--border)", color: "var(--text)" }}
                          />
                          <span className="text-[11px]" style={{ color: "var(--muted)" }}>to</span>
                          <input
                            type="time"
                            defaultValue={(s.schedule_end || "16:00:00").slice(0, 5)}
                            disabled={busyId === s.id}
                            onBlur={(e) => setCustomWindow(s, "end", e.target.value)}
                            className="rounded-md border px-2 py-1 text-[11px] bg-transparent focus-ring"
                            style={{ borderColor: "var(--border)", color: "var(--text)" }}
                          />
                          {/* <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                            {s.schedule_timezone || "America/New_York"}
                          </span> */}
                        </>
                      )}

                      {/* {s.schedule_mode !== "always" && (
                        <span className="text-[11px]" style={{ color: "var(--warn, #b45309)" }}>
                          alerts outside these hours aren&apos;t received
                        </span>
                      )} */}
                    </div>

                    {/* Actions */}
                    <div className="flex items-center gap-1.5 flex-wrap pl-[46px]">
                      <button
                        type="button"
                        onClick={() => { setEditingId(editingId === s.id ? null : s.id); setEditUrl(""); }}
                        disabled={busyId === s.id}
                        className="btn-ghost px-2.5 py-1 text-[11px] disabled:opacity-60"
                        title="Point this source at a different channel — keeps your Discord session"
                      >
                        Change channel
                      </button>
                      <button
                        type="button"
                        onClick={() => startPairing(s)}
                        disabled={busyId === s.id}
                        className="btn-ghost px-2.5 py-1 text-[11px] disabled:opacity-60"
                      >
                        {s.session.present ? "Reconnect" : "Connect Discord"}
                      </button>
                      {s.session.present && (
                        <button
                          type="button"
                          onClick={() => clearSession(s)}
                          disabled={busyId === s.id}
                          className="btn-ghost px-2.5 py-1 text-[11px] disabled:opacity-60"
                          title="Sign this Discord account out — affects every channel it reads"
                        >
                          Sign out
                        </button>
                      )}
                      {/* Icon only — it sits apart from the safe actions, and a
                          word-sized "Remove" gave a destructive action the same
                          visual weight as everything else in the row. */}
                      <button
                        type="button"
                        onClick={() => deleteSource(s)}
                        disabled={busyId === s.id}
                        className="btn-danger-soft p-1.5 rounded-md disabled:opacity-60 ml-auto inline-flex items-center"
                        title="Remove this channel and its stored messages"
                        aria-label="Remove channel"
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>

                    {/* Inline channel repoint */}
                    {editingId === s.id && (
                      <div className="flex items-center gap-2 flex-wrap pl-[46px]">
                        <input
                          className={inputCls}
                          style={{ ...inputStyle, maxWidth: 360 }}
                          value={editUrl}
                          onChange={(e) => setEditUrl(e.target.value)}
                          placeholder="https://discord.com/channels/…/…"
                          autoFocus
                        />
                        <button
                          type="button"
                          onClick={() => saveChannel(s)}
                          disabled={busyId === s.id || !editUrl.trim()}
                          className="btn-primary px-3 py-1.5 text-[11px] disabled:opacity-60"
                        >
                          {busyId === s.id ? <Spinner /> : "Save"}
                        </button>
                        <button
                          type="button"
                          onClick={() => setEditingId(null)}
                          className="btn-ghost px-3 py-1.5 text-[11px]"
                        >
                          Cancel
                        </button>
                      </div>
                    )}

                    {/* Anything that needs the trader's attention */}
                    {s.status === "error" && s.last_error && (
                      <div className="text-[11px] pl-[46px]" style={{ color: "var(--bad)" }}>
                        {s.last_error}
                      </div>
                    )}
                    {s.status === "needs_login" && (
                      <div className="text-[11px] pl-[46px]" style={{ color: "var(--warn, #b45309)" }}>
                        Waiting for a Discord sign-in — connect once on the right.
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
        </div>

        {/* Sidebar: the two things you DO, in order. Sticky so they stay put
            while a long channel list scrolls. */}
        <aside className="space-y-4 lg:sticky lg:top-4">
          {/* Step 1 — add a channel */}
          <form onSubmit={addSource} className="card p-5 space-y-3.5">
            <StepHeader n={1} icon={<Hash size={14} />} title="Add a channel" />
            <div className="space-y-3">
              <div>
                <label className="text-[11px] font-medium" style={{ color: "var(--muted)" }}>
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
                <label className="text-[11px] font-medium" style={{ color: "var(--muted)" }}>
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
                <p className="text-[11px] mt-1.5 leading-snug" style={{ color: "var(--muted)" }}>
                  Open the channel in Discord and copy the address from your browser.
                </p>
              </div>
            </div>
            <button
              type="submit"
              disabled={adding}
              className="btn-primary w-full py-2 text-sm inline-flex items-center justify-center gap-1.5 disabled:opacity-60"
            >
              {adding && <Spinner />} Add channel
            </button>
          </form>

        
        </aside>
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
            <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>{pairFor.label}</p>

            <ol className="text-sm mt-5 space-y-2 list-decimal pl-5 text-left" style={{ color: "var(--text-2)" }}>
              <li>Open the <strong>Kopyaa Connector</strong> app on your computer.</li>
              <li>Enter this code:</li>
            </ol>

            <div
              className="mt-3 mx-auto rounded-xl py-4 font-mono tracking-[0.2em] text-2xl"
              style={{ background: "var(--panel-2)", color: "var(--text)" }}
            >
              {pair ? pair.code : "…"}
            </div>
            <p className="text-[11px] mt-2" style={{ color: "var(--muted)" }}>
              Expires in 10 minutes · single use
            </p>

            <ol className="text-sm mt-4 space-y-2 list-decimal pl-5 text-left" style={{ color: "var(--text-2)" }} start={3}>
              <li>Sign in to Discord in the browser window it opens.</li>
            </ol>

            <div className="mt-4 text-sm" style={{ color: "var(--text-2)" }}>
              {pair?.status === "claimed" ? (
                <strong>Connector found — waiting for you to sign in…</strong>
              ) : pair?.status === "failed" ? (
                <span style={{ color: "var(--bad)" }}>{pair.error || "Connection failed."}</span>
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
                <button type="button" onClick={() => startPairing(pairFor)} className="btn-primary px-4 py-2 text-sm">
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

      {/* QR sign-in dialog — fallback when the Connector isn't available */}
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
            <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>{loginFor.label}</p>

            <div
              className="mt-5 mx-auto flex items-center justify-center rounded-xl"
              style={{ width: 220, height: 220, background: "#fff" }}
            >
              {login?.qr_image ? (
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
                <span style={{ color: "var(--bad)" }}>{login.error || "Sign-in failed."}</span>
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
                <button type="button" onClick={() => startLogin(loginFor)} className="btn-primary px-4 py-2 text-sm">
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
    </div>
  );
}


/** One stage of the pipeline strip. */
function PipelineStep({
  icon,
  title,
  detail,
  accent = false,
  soon = false,
}: {
  icon: React.ReactNode;
  title: string;
  detail: string;
  accent?: boolean;
  /** Marks a stage that isn't live yet. The strip describes the finished
   *  pipeline, and a trader who assumes their alerts already reach their broker
   *  would stop watching them — so an unbuilt stage has to say so. */
  soon?: boolean;
}) {
  return (
    <div
      className="flex-1 min-w-[132px] rounded-xl px-3 py-2.5"
      style={{
        background: accent ? "var(--accent-glow)" : "var(--panel-2)",
        border: `1px solid ${accent ? "rgba(44,147,197,0.35)" : "var(--border)"}`,
      }}
    >
      <div
        className="flex items-center gap-1.5 text-[13px] font-medium"
        style={{ color: accent ? "var(--accent-2)" : "var(--text)" }}
      >
        {icon}
        {title}
        {soon && (
          <span
            className="text-[9px] font-semibold px-1.5 py-0.5 rounded-full tracking-wide"
            style={{ background: "var(--panel-2)", color: "var(--muted)" }}
          >
            SOON
          </span>
        )}
      </div>
      <div className="text-[10.5px] mt-0.5 leading-snug" style={{ color: "var(--muted)" }}>
        {detail}
      </div>
    </div>
  );
}

/** Chevron between pipeline stages. Hidden once the strip wraps, where a
 *  horizontal arrow would point at the wrong thing. */
function PipelineArrow() {
  return (
    <div
      aria-hidden
      className="hidden sm:flex items-center self-center"
      style={{ color: "var(--muted)" }}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M5 12h14M13 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );
}

/** One reassurance tile. */
function Assurance({
  icon,
  title,
  body,
}: {
  icon: React.ReactNode;
  title: string;
  body: string;
}) {
  return (
    <div
      className="rounded-xl px-3 py-2.5"
      style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}
    >
      <div
        className="flex items-center gap-1.5 text-[12px] font-semibold"
        style={{ color: "var(--text)" }}
      >
        <span style={{ color: "var(--accent-2)" }}>{icon}</span>
        {title}
      </div>
      <p className="text-[11px] mt-1 leading-snug" style={{ color: "var(--muted)" }}>
        {body}
      </p>
    </div>
  );
}


/** Numbered heading tying the sidebar cards into an ordered flow. */
function StepHeader({ n, icon, title }: { n: number; icon: React.ReactNode; title: string }) {
  return (
    <div className="flex items-center gap-2.5">
      <span
        className="inline-flex items-center justify-center rounded-lg text-[11px] font-semibold"
        style={{
          width: 22,
          height: 22,
          background: "var(--accent-glow)",
          color: "var(--accent-2)",
          border: "1px solid rgba(44,147,197,0.35)",
        }}
      >
        {n}
      </span>
      <span
        className="inline-flex items-center gap-1.5 text-sm font-semibold"
        style={{ color: "var(--text)" }}
      >
        <span style={{ color: "var(--accent-2)" }}>{icon}</span>
        {title}
      </span>
    </div>
  );
}
