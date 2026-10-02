"use client";

/**
 * The Discord alert composer: a Discord button and the "Submit an alert" popup
 * it opens. Used in Order History and on the Positions screen.
 *
 * Paste (or type) an alert and it is replayed through the normal Discord
 * pipeline — same parser, same rules, same execution; the backend has no second
 * path, which is the whole point. Deliberately plain: a channel, one textarea
 * and one button. Anything cleverer — a symbol picker, a side toggle — would be
 * a second way to describe a trade.
 */
import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";

/** What the backend says happened to a hand-submitted alert — the stored
 *  message's own verdict, so the toast can be specific instead of saying "sent"
 *  and leaving the trader to go hunting in another tab. */
type SelfAlertResult = {
  id: string;
  content: string;
  status: string;
  status_reason: string | null;
  decision: string | null;
  order_id: string | null;
  parsed_signal: Record<string, unknown> | null;
};

/** Discord's mark. Inline rather than an icon-set import because lucide has no
 *  Discord glyph and this button has to be recognisable at 14px. */
export function DiscordGlyph({ size = 15 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M20.317 4.369a19.79 19.79 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.249a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.036A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128c.126-.094.252-.192.372-.291a.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.009c.12.099.246.198.373.292a.077.077 0 0 1-.006.127 12.3 12.3 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.331c-1.182 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z" />
    </svg>
  );
}

/** ``onSent`` runs after the backend has answered (whatever its verdict), so
 *  the host can refresh its own list. Show this only to accounts with Discord
 *  trading — the same gate as the Channel column. */
export function DiscordAlertComposer({ onSent }: { onSent?: () => unknown }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  // Which channel the alert is handled AS: "" is the trader's own Self channel,
  // otherwise one of their channels — its sizing, entry type and exit settings
  // then apply and the position shows that channel.
  const [channel, setChannel] = useState("");
  const [channels, setChannels] = useState<{ id: string; label: string }[]>([]);
  useEffect(() => {
    if (!open) return;
    api<{ id: string; label: string | null; channel_name?: string | null }[]>("/api/discord-sources")
      .then((rows) => setChannels(rows.map((r) => ({ id: r.id, label: r.label?.trim() || r.channel_name?.trim() || "Channel" }))))
      .catch(() => { /* Self alone still works */ });
  }, [open]);

  const send = useCallback(async () => {
    const content = text.trim();
    if (!content || busy) return;
    setBusy(true);
    try {
      const r = await api<SelfAlertResult>("/api/discord-sources/self/alert", {
        method: "POST",
        body: JSON.stringify(channel ? { content, source_id: channel } : { content }),
      });
      // The alert's own verdict decides the tone. A parse that produced no
      // trade, or an order the executor refused, is NOT a success — saying
      // "sent" for those is how a missed alert stays missed. The toast carries
      // the reason, so the modal doesn't have to stay open to show it.
      if (r.status === "order_created") {
        notify.success("Alert placed");
      } else if (r.status === "order_failed" || r.status === "invalid") {
        notify.warn(r.status_reason || "The alert could not be placed");
      } else if (r.status === "ignored") {
        notify.warn(r.status_reason || "Not read as a trade alert");
      } else {
        notify.success("Alert queued for your approval");
      }
      // Answered, whatever the verdict — close and clear, so the next paste
      // starts from an empty box rather than the previous alert.
      setText("");
      setOpen(false);
      await onSent?.();
    } catch (e) {
      // Left OPEN on purpose: nothing reached the backend, so closing would
      // throw away the text with no record of it anywhere. The trader can
      // retry without re-pasting.
      notify.fromError(e, "could not submit the alert");
    } finally {
      setBusy(false);
    }
  }, [text, busy, channel, onSent]);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-label="Submit a Discord alert manually"
        title="Paste a Discord alert the system missed"
        className="focus-ring rounded px-2 py-1.5 border transition-colors"
        // Discord's own blurple. --muted made a 15px glyph read as a disabled
        // control next to the search box; the brand colour is both brighter and
        // self-explanatory, and it carries its own contrast in light and dark
        // rather than tracking the theme's text tokens.
        style={{ borderColor: "rgba(88,101,242,0.45)", color: "#5865F2", background: "rgba(88,101,242,0.10)" }}
        onMouseEnter={(e) => { e.currentTarget.style.background = "rgba(88,101,242,0.20)"; }}
        onMouseLeave={(e) => { e.currentTarget.style.background = "rgba(88,101,242,0.10)"; }}
      >
        <DiscordGlyph />
      </button>

      {/* In a portal: the popup must cover the page whatever container the
          button sits in (a table toolbar clips and stacks differently). */}
      {open && createPortal(
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center p-4"
          style={{ background: "rgba(0,0,0,0.45)" }}
          onClick={() => !busy && setOpen(false)}
          role="presentation"
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Submit a Discord alert"
            className="card w-full max-w-lg p-5 flex flex-col gap-3"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2" style={{ color: "var(--text)" }}>
                <DiscordGlyph size={17} />
                <span className="font-medium">Submit an alert</span>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                disabled={busy}
                aria-label="Close"
                className="focus-ring rounded"
                style={{ color: "var(--muted)" }}
              >
                <X size={16} />
              </button>
            </div>

            <p className="text-xs" style={{ color: "var(--muted)" }}>
              Paste an alert the system missed. It is read by the same parser as
              a live channel and follows your Discord settings — so in manual
              mode it waits for your approval rather than placing.
            </p>

            <label className="flex items-center gap-2 text-xs" style={{ color: "var(--text-2)" }}>
              Channel
              <select
                value={channel}
                onChange={(e) => setChannel(e.target.value)}
                disabled={busy}
                aria-label="Channel to handle this alert as"
                title="The alert is handled as this channel's: its contracts, entry type and exit settings apply, and the position shows this channel"
                className="rounded-md border px-2 py-1 text-xs bg-transparent focus-ring"
                style={{ borderColor: "var(--border)", color: "var(--text)" }}
              >
                <option value="">Self</option>
                {channels.map((c) => (
                  <option key={c.id} value={c.id}>{c.label}</option>
                ))}
              </select>
            </label>

            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                // Enter alone must NOT send: a multi-line message is one alert
                // and people paste them.
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  void send();
                }
              }}
              rows={4}
              autoFocus
              spellCheck={false}
              placeholder="$SPY 771 CALL 0DTE @0.68"
              aria-label="Discord alert text"
              className="w-full px-3 py-2 text-sm font-mono resize-y"
            />

            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => setOpen(false)}
                disabled={busy}
                className="btn-ghost px-3 py-1.5 text-sm"
              >
                Close
              </button>
              <button
                type="button"
                onClick={() => void send()}
                disabled={busy || !text.trim()}
                className="btn-primary px-4 py-1.5 text-sm"
              >
                {busy ? "Sending…" : "Send"}
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
