"use client";

/**
 * AI trimming — the alternative exit engine to the trim ladder.
 *
 * An OpenRouter model is asked about each Discord position when its price has
 * moved enough, and answers hold / trim / exit / raise the stop. The server
 * holds it to those four (it can't buy, oversell, or lower a stop) — see
 * backend/app/services/ai_trim.py. "Suggest" records each decision here for
 * approval; "Auto-execute" acts on it. Both are paper unless Discord live
 * trading is on.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Mic, Square } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { ConfirmModal } from "@/components/ConfirmModal";
import { SearchableSelect, type SelectOption } from "@/components/SearchableSelect";

type Engine = "ladder" | "ai";

type AiSettings = {
  engine: Engine;
  mode: "suggest" | "auto";
  model: string;
  move_pct: string;
  min_interval_s: number;
  instructions: string;
  key_configured: boolean;
  live_trading: boolean;
};

// The Web Speech API isn't in TypeScript's DOM lib; this is the slice we use.
type SpeechResultList = ArrayLike<{ isFinal: boolean; 0: { transcript: string } }>;
type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start: () => void;
  stop: () => void;
  onresult: ((e: { resultIndex: number; results: SpeechResultList }) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
};

function speechRecognition(): (new () => Recognition) | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as Record<string, unknown>;
  return (w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null) as (new () => Recognition) | null;
}

type AiModel = {
  id: string;
  name: string;
  prompt_per_m: string | null;
  completion_per_m: string | null;
};

function modelLabel(m: AiModel): string {
  const price =
    m.prompt_per_m != null && m.completion_per_m != null
      ? Number(m.prompt_per_m) === 0 && Number(m.completion_per_m) === 0
        ? " · free"
        : ` · $${m.prompt_per_m} / $${m.completion_per_m} per M tokens`
      : "";
  return `${m.name}${price}`;
}

type AiDecision = {
  id: string;
  contract: string;
  model: string;
  mode: string;
  mark: string;
  entry_price: string | null;
  held: string;
  action: "hold" | "trim" | "exit" | "raise_stop";
  sell_qty: string;
  new_stop_price: string | null;
  reason: string;
  notes: string | null;
  status: string;
  order_id: string | null;
  created_at: string;
};

const STATUS_TONE: Record<string, string> = {
  suggested: "var(--warn)",
  executed: "var(--good)",
  approved: "var(--good)",
  paper: "var(--accent)",
  error: "var(--bad)",
  hold: "var(--muted)",
  dismissed: "var(--muted)",
  superseded: "var(--muted)",
  expired: "var(--muted)",
};

function describe(d: AiDecision): string {
  const stop = d.new_stop_price ? ` · stop → ${d.new_stop_price}` : "";
  switch (d.action) {
    case "trim":
      return `Trim ${d.sell_qty} of ${d.held}${stop}`;
    case "exit":
      return `Exit all ${d.held}`;
    case "raise_stop":
      return `Raise stop to ${d.new_stop_price}`;
    default:
      return "Hold";
  }
}

function gain(d: AiDecision): string {
  if (!d.entry_price) return "";
  const g = ((Number(d.mark) - Number(d.entry_price)) / Number(d.entry_price)) * 100;
  return ` (${g >= 0 ? "+" : ""}${g.toFixed(1)}%)`;
}

export function AiTrimPanel({ onEngineChange }: { onEngineChange: (engine: Engine) => void }) {
  const [settings, setSettings] = useState<AiSettings | null>(null);
  const [draft, setDraft] = useState({ model: "", move_pct: "", min_interval_s: "", instructions: "" });
  const [busy, setBusy] = useState(false);
  const [decisions, setDecisions] = useState<AiDecision[]>([]);
  const [acting, setActing] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [confirmAuto, setConfirmAuto] = useState(false);
  const [models, setModels] = useState<AiModel[] | null>(null);
  // Dictation into "Your instructions". Support is decided after mount: the
  // server render has no window, and deciding during render would mismatch.
  const [canDictate, setCanDictate] = useState(false);
  const [listening, setListening] = useState(false);
  const recognition = useRef<Recognition | null>(null);

  useEffect(() => {
    setCanDictate(speechRecognition() !== null);
    return () => recognition.current?.stop();
  }, []);

  function toggleDictation() {
    if (listening) {
      recognition.current?.stop();
      return;
    }
    const Ctor = speechRecognition();
    if (!Ctor) return;
    const rec = new Ctor();
    rec.lang = navigator.language || "en-US";
    rec.continuous = true;
    rec.interimResults = true;
    // Speech is appended to what's already typed; interim words show live and
    // are replaced as the recogniser settles on them.
    const base = draft.instructions ? draft.instructions.replace(/\s*$/, " ") : "";
    let committed = "";
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) committed += r[0].transcript;
        else interim += r[0].transcript;
      }
      const text = (base + committed + interim).slice(0, 2000);
      setDraft((d) => ({ ...d, instructions: text }));
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        notify.error("Microphone access was blocked — allow it for this site to dictate.");
      } else if (e.error !== "aborted" && e.error !== "no-speech") {
        notify.error(`Dictation stopped: ${e.error}`);
      }
    };
    rec.onend = () => {
      setListening(false);
      recognition.current = null;
    };
    recognition.current = rec;
    rec.start();
    setListening(true);
  }

  const adopt = useCallback((s: AiSettings) => {
    setSettings(s);
    setDraft({
      model: s.model,
      move_pct: s.move_pct,
      min_interval_s: String(s.min_interval_s),
      instructions: s.instructions,
    });
    onEngineChange(s.engine);
  }, [onEngineChange]);

  const loadDecisions = useCallback(async () => {
    try {
      setDecisions(await api<AiDecision[]>("/api/discord-sources/ai-trim/decisions?limit=30"));
    } catch {
      /* the settings load reports errors; a missed refresh is not worth a toast */
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        adopt(await api<AiSettings>("/api/discord-sources/ai-trim"));
      } catch (e) {
        notify.fromError(e, "Could not load AI trimming settings");
      }
    })();
    void loadDecisions();
    (async () => {
      try {
        setModels(await api<AiModel[]>("/api/discord-sources/ai-trim/models"));
      } catch (e) {
        setModels([]);
        notify.fromError(e, "Could not load the model list");
      }
    })();
    // Decisions arrive on the worker's clock; polling keeps suggestions fresh.
    const t = setInterval(() => void loadDecisions(), 10_000);
    return () => clearInterval(t);
  }, [adopt, loadDecisions]);

  async function patch(body: Record<string, unknown>, done?: string) {
    setBusy(true);
    try {
      adopt(await api<AiSettings>("/api/discord-sources/ai-trim", {
        method: "PATCH",
        body: JSON.stringify(body),
      }));
      if (done) notify.success(done);
    } catch (e) {
      notify.fromError(e, "Could not save that");
    } finally {
      setBusy(false);
    }
  }

  async function act(id: string, verb: "approve" | "dismiss") {
    setActing(id);
    try {
      await api(`/api/discord-sources/ai-trim/decisions/${id}/${verb}`, { method: "POST" });
      notify.success(verb === "approve" ? "Approved — sent through the exit path" : "Dismissed");
    } catch (e) {
      notify.fromError(e, verb === "approve" ? "Could not approve" : "Could not dismiss");
    } finally {
      setActing(null);
      void loadDecisions();
    }
  }

  async function testConnection() {
    setTesting(true);
    try {
      const r = await api<{ ok: boolean; model: string; action?: string; reason?: string; error?: string }>(
        "/api/discord-sources/ai-trim/test",
        { method: "POST" },
      );
      if (r.ok) notify.success(`${r.model} answered: ${r.action} — ${r.reason ?? ""}`.slice(0, 220));
      else notify.error(r.error ?? "The model did not answer");
    } catch (e) {
      notify.fromError(e, "Test failed");
    } finally {
      setTesting(false);
    }
  }

  if (!settings) {
    return <div className="py-6 flex justify-center"><Spinner /></div>;
  }

  const on = settings.engine === "ai";
  const dirty =
    draft.model !== settings.model ||
    draft.move_pct !== settings.move_pct ||
    draft.min_interval_s !== String(settings.min_interval_s) ||
    draft.instructions !== settings.instructions;
  const input = "w-full rounded-lg border px-3 py-1.5 text-sm bg-transparent focus-ring";
  const modelOptions: SelectOption[] = (models ?? []).map((m) => ({ value: m.id, label: modelLabel(m) }));
  // Keep a saved model selectable even if OpenRouter no longer lists it.
  if (draft.model && !modelOptions.some((o) => o.value === draft.model)) {
    modelOptions.unshift({ value: draft.model, label: draft.model });
  }
  const inputStyle = { borderColor: "var(--border)", color: "var(--text)" };

  return (
    <div className="mt-2 space-y-3 text-[11px]">
      {/* Server + trading state — the two reasons nothing would happen. */}
      <div className="flex flex-wrap gap-2">
        <span
          className="rounded-md px-2 py-0.5"
          style={{
            border: `1px solid ${settings.key_configured ? "var(--good)" : "var(--warn)"}`,
            color: settings.key_configured ? "var(--good)" : "var(--warn)",
          }}
        >
          {settings.key_configured ? "OpenRouter key configured" : "OPENROUTER_API_KEY not set on the server"}
        </span>
        {!settings.live_trading && (
          <span className="rounded-md px-2 py-0.5" style={{ border: "1px solid var(--border)", color: "var(--muted)" }}>
            Paper — Discord live trading is off, so no AI order reaches the broker
          </span>
        )}
      </div>

      <label className="flex items-start gap-2 cursor-pointer select-none">
        <input
          type="checkbox"
          className="h-3.5 w-3.5 cursor-pointer mt-[1px]"
          style={{ accentColor: "var(--accent)" }}
          checked={on}
          disabled={busy}
          onChange={(e) =>
            void patch(
              { engine: e.target.checked ? "ai" : "ladder" },
              e.target.checked ? "AI trimming now manages exits" : "Back to the exit ladder",
            )
          }
        />
        <span style={{ color: "var(--text-2)" }}>
          Use AI trimming for exits
          <span style={{ color: "var(--muted)" }}>
            {" — "}replaces the ladder&apos;s automatic trims on Discord positions, so
            only one engine ever sells. Exit alerts your Discord author posts still
            run as before.
          </span>
        </span>
      </label>

      <div className="flex flex-wrap items-center gap-3">
        <span style={{ color: "var(--text-2)" }}>When it decides</span>
        <div className="flex rounded-lg overflow-hidden" style={{ border: "1px solid var(--border)" }}>
          {([
            ["suggest", "Suggest — I approve each"],
            ["auto", "Auto-execute"],
          ] as const).map(([value, label]) => (
            <button
              key={value}
              type="button"
              disabled={busy}
              onClick={() => {
                if (value === settings.mode) return;
                if (value === "auto") setConfirmAuto(true);
                else void patch({ mode: "suggest" }, "Suggest mode — nothing trades without your approval");
              }}
              className="px-3 py-1 disabled:opacity-40"
              style={{
                background: settings.mode === value ? "var(--accent)" : "transparent",
                color: settings.mode === value ? "#fff" : "var(--muted)",
              }}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
        {/* Full row: model names plus prices run long. */}
        <div style={{ gridColumn: "1 / -1" }}>
          <label className="block mb-1" style={{ color: "var(--text-2)" }}>Model</label>
          <SearchableSelect
            value={draft.model}
            options={modelOptions}
            loading={models === null}
            placeholder="Choose a model"
            onChange={(v) => setDraft((d) => ({ ...d, model: v }))}
            style={{ height: 34 }}
          />
        </div>
        <div>
          <label className="block mb-1" style={{ color: "var(--text-2)" }}>Ask again after a move of (%)</label>
          <input
            type="number" min="0.5" max="100" step="0.5"
            className={input}
            style={inputStyle}
            value={draft.move_pct}
            onChange={(e) => setDraft((d) => ({ ...d, move_pct: e.target.value }))}
          />
        </div>
        <div>
          <label className="block mb-1" style={{ color: "var(--text-2)" }}>At most once every (seconds)</label>
          <input
            type="number" min="15" max="3600" step="15"
            className={input}
            style={inputStyle}
            value={draft.min_interval_s}
            onChange={(e) => setDraft((d) => ({ ...d, min_interval_s: e.target.value }))}
          />
        </div>
      </div>

      <div>
        <label className="block mb-1" style={{ color: "var(--text-2)" }}>
          Your instructions <span style={{ color: "var(--muted)" }}>(optional — added to the model&apos;s brief)</span>
        </label>
        <div className="relative">
          <textarea
            rows={3}
            maxLength={2000}
            className={input}
            style={{ ...inputStyle, resize: "vertical", paddingRight: canDictate ? 40 : undefined }}
            placeholder="e.g. Take a third off at +30%. On 0DTE, be out by 3:30 ET. Keep the stop at break-even once up 25%."
            value={draft.instructions}
            onChange={(e) => setDraft((d) => ({ ...d, instructions: e.target.value }))}
          />
          {canDictate && (
            <button
              type="button"
              onClick={toggleDictation}
              aria-label={listening ? "Stop dictating" : "Dictate instructions"}
              aria-pressed={listening}
              title={listening ? "Stop dictating" : "Speak your instructions"}
              className="absolute right-2 top-2 rounded-full p-1.5 focus-ring"
              style={{
                background: listening ? "var(--bad)" : "var(--panel)",
                border: `1px solid ${listening ? "var(--bad)" : "var(--border)"}`,
                color: listening ? "#fff" : "var(--text-2)",
              }}
            >
              {listening ? <Square size={12} fill="currentColor" /> : <Mic size={14} />}
            </button>
          )}
        </div>
        {listening && (
          <p className="mt-1" style={{ color: "var(--bad)" }}>
            Listening — speak your instructions, then press stop. Remember to Save.
          </p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy || !dirty}
          onClick={() =>
            void patch(
              {
                model: draft.model.trim(),
                move_pct: draft.move_pct,
                min_interval_s: Number(draft.min_interval_s),
                instructions: draft.instructions,
              },
              "AI trimming settings saved",
            )
          }
          className="btn-primary px-3.5 py-1.5 text-[12px] disabled:opacity-40"
        >
          {busy ? <Spinner /> : "Save"}
        </button>
        <button
          type="button"
          disabled={testing || !settings.key_configured}
          onClick={() => void testConnection()}
          title={settings.key_configured ? "One call on a made-up position — records and trades nothing" : "Set OPENROUTER_API_KEY first"}
          className="btn-ghost px-3 py-1.5 text-[12px] disabled:opacity-40"
        >
          {testing ? <Spinner /> : "Test connection"}
        </button>
        <p className="leading-snug" style={{ color: "var(--muted)" }}>
          The model can only hold, trim, exit, or raise the stop. It can&apos;t buy, sell
          more than you hold, or lower a stop — anything else is treated as hold.
        </p>
      </div>

      {/* Decision log. */}
      <div className="pt-2" style={{ borderTop: "1px solid var(--border)" }}>
        <div className="flex items-baseline justify-between mb-1.5">
          <span className="font-medium" style={{ color: "var(--text-2)" }}>Recent AI decisions</span>
          <button type="button" onClick={() => void loadDecisions()} className="btn-ghost px-2 py-0.5 text-[11px]">
            Refresh
          </button>
        </div>
        {decisions.length === 0 ? (
          <p style={{ color: "var(--muted)" }}>
            None yet. With AI trimming on, the model is asked on each Discord position&apos;s
            next sweep, then whenever its price moves by your threshold.
          </p>
        ) : (
          <ul className="space-y-1.5">
            {decisions.map((d) => (
              <li
                key={d.id}
                className="rounded-lg px-2.5 py-1.5"
                style={{ border: "1px solid var(--border)" }}
              >
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="tabular-nums" style={{ color: "var(--muted)" }}>
                    {new Date(d.created_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
                  </span>
                  <span style={{ color: "var(--text)" }}>{d.contract}</span>
                  <span className="tabular-nums" style={{ color: "var(--muted)" }}>@ {d.mark}{gain(d)}</span>
                  <span className="font-medium" style={{ color: "var(--text)" }}>{describe(d)}</span>
                  <span
                    className="rounded px-1.5 uppercase tracking-wide text-[10px]"
                    style={{ color: STATUS_TONE[d.status] ?? "var(--muted)", border: "1px solid currentColor" }}
                  >
                    {d.status}
                  </span>
                  {d.status === "suggested" && (
                    <span className="ml-auto flex gap-1.5">
                      <button
                        type="button"
                        disabled={acting === d.id}
                        onClick={() => void act(d.id, "approve")}
                        className="btn-primary px-2.5 py-0.5 text-[11px] disabled:opacity-40"
                      >
                        Approve
                      </button>
                      <button
                        type="button"
                        disabled={acting === d.id}
                        onClick={() => void act(d.id, "dismiss")}
                        className="btn-ghost px-2 py-0.5 text-[11px] disabled:opacity-40"
                      >
                        Dismiss
                      </button>
                    </span>
                  )}
                </div>
                {d.reason && <div className="mt-0.5" style={{ color: "var(--text-2)" }}>{d.reason}</div>}
                {d.notes && <div className="mt-0.5" style={{ color: "var(--muted)" }}>{d.notes}</div>}
              </li>
            ))}
          </ul>
        )}
      </div>

      <ConfirmModal
        open={confirmAuto}
        title="Let the AI execute exits?"
        message={
          <>
            Every decision will be carried out as it arrives, without asking you.
            {settings.live_trading
              ? " Discord live trading is ON, so these are real orders at your broker."
              : " Discord live trading is off, so they stay paper until you turn it on."}
          </>
        }
        confirmLabel="Auto-execute"
        busy={busy}
        onCancel={() => setConfirmAuto(false)}
        onConfirm={() => {
          setConfirmAuto(false);
          void patch({ mode: "auto" }, "Auto-execute on — AI decisions act immediately");
        }}
      />
    </div>
  );
}
