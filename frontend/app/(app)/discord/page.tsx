"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { PageLoading } from "@/components/PageLoading";
import type { User } from "@/lib/types";

/**
 * Discord — INBOUND alert-copying (Step 1: connection only).
 *
 * A trader connects a Discord channel (via their own bot token) that Kopyaa will
 * later read trade alerts from. This page manages the connection lifecycle only:
 * add + verify, list, enable/disable, delete. No message reading or trading yet.
 *
 * Deliberately separate from the OUTBOUND webhook broadcast (Settings → Discord
 * alerts), which posts the trader's fills TO a channel.
 */
type DiscordSource = {
  id: string;
  label: string;
  channel_id: string;
  channel_name: string | null;
  guild_id: string | null;
  is_enabled: boolean;
  status: string;
  last_error: string | null;
  created_at: string;
};

export default function DiscordPage() {
  const [user, setUser] = useState<User | null>(null);
  const [sources, setSources] = useState<DiscordSource[]>([]);
  const [loading, setLoading] = useState(true);

  const [label, setLabel] = useState("");
  const [token, setToken] = useState("");
  const [channelId, setChannelId] = useState("");
  const [adding, setAdding] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

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

  async function addSource(e: FormEvent) {
    e.preventDefault();
    setAdding(true);
    try {
      await api("/api/discord-sources", {
        method: "POST",
        body: JSON.stringify({
          label: label.trim(),
          bot_token: token.trim(),
          channel_id: channelId.trim(),
        }),
      });
      setLabel(""); setToken(""); setChannelId("");
      notify.success("Channel connected");
      await load();
    } catch (e) {
      notify.fromError(e, "Could not connect — check the bot token and channel ID");
    } finally {
      setAdding(false);
    }
  }

  async function toggleEnabled(s: DiscordSource, next: boolean) {
    setBusyId(s.id);
    setSources((prev) => prev.map((x) => (x.id === s.id ? { ...x, is_enabled: next } : x)));
    try {
      const updated = await api<DiscordSource>(`/api/discord-sources/${s.id}`, {
        method: "PATCH", body: JSON.stringify({ is_enabled: next }),
      });
      setSources((prev) => prev.map((x) => (x.id === s.id ? updated : x)));
    } catch (e) {
      setSources((prev) => prev.map((x) => (x.id === s.id ? { ...x, is_enabled: !next } : x)));
      notify.fromError(e, "Could not update");
    } finally {
      setBusyId(null);
    }
  }

  async function verifySource(s: DiscordSource) {
    setBusyId(s.id);
    try {
      const updated = await api<DiscordSource>(`/api/discord-sources/${s.id}/verify`, { method: "POST" });
      setSources((prev) => prev.map((x) => (x.id === s.id ? updated : x)));
      notify.success(updated.status === "connected" ? "Connection OK" : "Connection failed — see details");
    } catch (e) {
      notify.fromError(e, "Verify failed");
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
      <div className="max-w-[760px] mx-auto">
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
    <div className="max-w-[760px] mx-auto space-y-4 pb-12">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight" style={{ color: "var(--text)" }}>
          Discord
        </h1>
        <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>
          Connect a Discord channel that posts trade alerts. Kopyaa will read those alerts and
          (in an upcoming release) place the matching trades on your connected broker.
        </p>
        <p className="text-xs mt-2" style={{ color: "var(--muted)" }}>
          This is separate from the alert <strong>broadcast</strong> (Settings), which posts <em>your</em> fills out to a channel.
        </p>
      </div>

      {/* Add source */}
      <form onSubmit={addSource} className="card p-5 space-y-3">
        <h3 className="text-sm font-semibold" style={{ color: "var(--text)" }}>Connect a channel</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label className="text-xs font-medium" style={{ color: "var(--muted)" }}>Label</label>
            <input className={`${inputCls} mt-1.5`} style={inputStyle} value={label}
              onChange={(e) => setLabel(e.target.value)} placeholder="e.g. My Alerts Room" required />
          </div>
          <div>
            <label className="text-xs font-medium" style={{ color: "var(--muted)" }}>Channel ID</label>
            <input className={`${inputCls} mt-1.5`} style={inputStyle} value={channelId}
              onChange={(e) => setChannelId(e.target.value)} placeholder="e.g. 123456789012345678"
              inputMode="numeric" required />
          </div>
        </div>
        <div>
          <label className="text-xs font-medium" style={{ color: "var(--muted)" }}>Bot token</label>
          <input className={`${inputCls} mt-1.5`} style={inputStyle} value={token} type="password"
            onChange={(e) => setToken(e.target.value)} placeholder="Your Discord bot token"
            autoComplete="off" required />
          <p className="text-[11px] mt-1" style={{ color: "var(--muted)" }}>
            Stored encrypted. We verify the bot can read the channel before saving. Never shown again.
          </p>
        </div>
        <div className="flex justify-end">
          <button type="submit" disabled={adding}
            className="btn-primary px-4 py-2 text-sm inline-flex items-center gap-1.5 disabled:opacity-60">
            {adding && <Spinner />} Connect channel
          </button>
        </div>
      </form>

      {/* Connected channels */}
      <div className="card p-5">
        <h3 className="text-sm font-semibold mb-3" style={{ color: "var(--text)" }}>
          Connected channels {sources.length > 0 && <span style={{ color: "var(--muted)" }}>({sources.length})</span>}
        </h3>
        {sources.length === 0 ? (
          <p className="text-sm py-2" style={{ color: "var(--muted)" }}>
            No channels connected yet. Add one above to get started.
          </p>
        ) : (
          <div className="flex flex-col divide-y" style={{ borderColor: "var(--border)" }}>
            {sources.map((s) => (
              <div key={s.id} className="py-3 first:pt-0 last:pb-0 flex items-center gap-3 flex-wrap">
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium truncate" style={{ color: "var(--text)" }}>{s.label}</div>
                  <div className="text-xs truncate" style={{ color: "var(--muted)" }}>
                    {s.channel_name ? `#${s.channel_name}` : `Channel ${s.channel_id}`}
                    {s.status === "error" && s.last_error ? ` · ${s.last_error}` : ""}
                  </div>
                </div>
                <span className={s.status === "connected" ? "chip chip-good" : s.status === "error" ? "chip chip-bad" : "chip"}>
                  <span className="inline-block rounded-full" style={{ width: 6, height: 6, background: "currentColor" }} />
                  {s.status}
                </span>
                <label className="flex items-center gap-2 text-xs cursor-pointer select-none" title="Enable/disable this source">
                  <input type="checkbox" className="h-4 w-4 cursor-pointer" style={{ accentColor: "var(--accent)" }}
                    checked={s.is_enabled} disabled={busyId === s.id}
                    onChange={(e) => toggleEnabled(s, e.target.checked)} />
                  <span style={{ color: "var(--text-2)" }}>{s.is_enabled ? "On" : "Off"}</span>
                </label>
                <button type="button" onClick={() => verifySource(s)} disabled={busyId === s.id}
                  className="btn-ghost px-3 py-1.5 text-xs disabled:opacity-60">
                  {busyId === s.id ? <Spinner /> : "Verify"}
                </button>
                <button type="button" onClick={() => deleteSource(s)} disabled={busyId === s.id}
                  className="btn-danger-soft px-3 py-1.5 text-xs disabled:opacity-60">
                  Remove
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Instructions */}
      <div className="card p-5">
        <h3 className="text-sm font-semibold mb-3" style={{ color: "var(--text)" }}>How to connect</h3>
        <ol className="text-sm space-y-2 list-decimal pl-5" style={{ color: "var(--text-2)" }}>
          <li>Create a bot at <strong>discord.com/developers</strong> → <strong>New Application</strong> → <strong>Bot</strong>, and copy its <strong>token</strong>.</li>
          <li>Under the bot's settings, enable the <strong>Message Content Intent</strong> (required to read messages).</li>
          <li>Invite the bot to the server that has the alerts channel, and give it permission to <strong>view</strong> that channel.</li>
          <li>In Discord, enable <strong>Developer Mode</strong> (User Settings → Advanced), then right-click the channel → <strong>Copy Channel ID</strong>.</li>
          <li>Paste the token + channel ID above and hit <strong>Connect</strong>.</li>
        </ol>
        <p className="text-[11px] mt-3" style={{ color: "var(--muted)" }}>
          Connecting only links the channel — reading alerts and placing trades will roll out in later updates.
        </p>
      </div>
    </div>
  );
}
