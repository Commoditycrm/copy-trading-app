"use client";

import { Fragment, FormEvent, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Hash, Radio, ScanLine, Receipt, ShieldCheck, Clock, Eye, PlugZap, Check, Trash2, X } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/toast";
import { Spinner } from "@/components/Spinner";
import { PageLoading } from "@/components/PageLoading";
import { AiTrimPanel } from "@/components/discord/AiTrimPanel";
import type { User } from "@/lib/types";

/**
 * Discord — INBOUND alert-copying (Step 2: connection + real-time listener).
 *
 * Kopyya monitors Discord Web as the trader's OWN logged-in account, reading
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

// releases/latest always resolves to the newest Connector build.
const CONNECTOR_RELEASES = "https://github.com/Commoditycrm/kopyya-connector/releases/latest/download";
const CONNECTOR_WINDOWS = `${CONNECTOR_RELEASES}/Kopyya-Connector-Windows.exe`;
const CONNECTOR_MAC = `${CONNECTOR_RELEASES}/Kopyya-Connector-macOS.zip`;

type ExitMode = "alerts" | "auto" | "orders" | "manual";

const EXIT_MODES: { value: ExitMode; label: string; detail: string; toast: string }[] = [
  { value: "alerts", label: "On alerts",
    detail: "Each trim waits for the channel's exit alert; its Profit target is the minimum that alert has to meet.",
    toast: "Exits on alerts — each trim waits for its Discord alert" },
  { value: "auto", label: "Auto trim",
    detail: "Each trim fires at its own Profit target, without waiting for an alert. A trim left at 0% keeps waiting.",
    toast: "Auto trim on — each trim fires at its own Profit target" },
  { value: "orders", label: "Take-profit orders",
    detail: "Each trim rests at the broker as a take-profit order, with its stop linked. Webull options; on other brokers it works like Auto trim.",
    toast: "Take-profit orders on — each trim rests at the broker at its Profit target" },
  { value: "manual", label: "Manual",
    detail: "Kopyya never sells. Exit alerts are recorded but not acted on — you close from Positions.",
    toast: "Manual exits — exit alerts are ignored; close from Positions" },
];

type DiscordSettings = {
  execution_mode: string;
  live_trading: boolean;
  /** Fire each ladder rung when its Min profit is reached, instead of
   *  waiting for that rung's Discord alert. */
  auto_trim: boolean;
  /** How a position leaves: on the channel's exit alerts, by auto-trim, or
   *  never on its own (the trader closes it). */
  exit_mode?: ExitMode;
  /** The whole exit ladder, in order — any number of trims. */
  trims?: TrimRow[];
  /** "On Fill" stop as a return from entry; null = no stop until the first trim. */
  fill_stop_pct?: string | null;
  /** The On Fill stop trails: fill_stop_pct is then a give-back from the high. */
  fill_stop_trail?: boolean;
  quantity_multiplier: number;
  /** "contracts" (quantity_multiplier per entry) or "dollars" (size_dollars per entry). */
  size_mode?: "contracts" | "dollars";
  size_dollars?: string | null;
  /** With contracts: the ONE cap that applies. Ignored with dollars. */
  size_cap?: "none" | "per_contract" | "per_order";
  max_per_contract: string | null;
  max_per_order: string | null;
  trail_percent: string;
  trim_profit_gate_pct: string;
  trim2_profit_gate_pct: string;
  trim2_stop_pct: string;
  trim3_profit_gate_pct: string;
  trim3_stop_pct: string;
  trim_stop_pct: string;
  trim_price_threshold: string;
  trim_trail_amount: string;
};

type LoginSession = {
  session_id: string;
  // pending | starting | awaiting_scan | scanned | complete | failed
  status: string;
  qr_image: string | null;
  error: string | null;
};

type DiscordSource = {
  /** A subscriber's copy of a trader channel — only the switch is theirs. */
  mirrored?: boolean;
  /** Pills beside the status: how entries go out, what drives exits. */
  entry_summary?: string | null;
  exit_summary?: string | null;
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

type LadderField = {
  key: string;
  label: string;
  step: string;
  prefix?: string;
  suffix?: string;
};


/** One trim of the exit ladder, as typed. ``stop_trail``: the stop trails — its
 *  value is a give-back from the high since this trim, not a return from entry.
 *  ``stop_alt`` is the other mode's value, kept while you flip between them
 *  (never sent). */
type TrimRow = {
  profit_gate_pct: string; qty_pct: string; stop_pct: string;
  stop_trail?: boolean; stop_alt?: string;
};

/** The ladder as a table — one ROW per stage, one COLUMN per setting:
 *
 *      On Fill   —              —                 stop
 *      Trim 1    profit target  trim of rem. qty  stop
 *      Trim 2    …              as many as the trader adds
 *
 *  Read across a row for what happens at that stage. On Fill has no target and
 *  no quantity — nothing is sold when the entry fills; it only places the stop.
 */
const TRIM_COLUMNS: { key: "profit_gate_pct" | "qty_pct" | "stop_pct"; label: string; hint: string; min: string; max?: string }[] = [
  { key: "profit_gate_pct", label: "Profit target", min: "0",
    hint: "minimum gain over entry before this trim sells (0 = no minimum)" },
  { key: "qty_pct", label: "Trim of rem. qty", min: "0", max: "100",
    hint: "share of what is STILL held, not of the original position" },
  { key: "stop_pct", label: "Stop / trailing stop", min: "-100",
    hint: "Stop: where the stop sits after this stage, as a return from entry (-25% is 25% below, "
      + "0% is break-even, +10% locks in profit). Trail: follows the high after this stage and "
      + "exits on a give-back of that %" },
];

const MAX_TRIMS = 10;
const DEFAULT_TRIMS: TrimRow[] = [
  { profit_gate_pct: "20", qty_pct: "50", stop_pct: "-25" },
  { profit_gate_pct: "0", qty_pct: "50", stop_pct: "0" },
  { profit_gate_pct: "0", qty_pct: "100", stop_pct: "0" },
];
/** What "Add trim" appends: sell the rest, stop at break-even. */
const NEW_TRIM: TrimRow = { profit_gate_pct: "0", qty_pct: "100", stop_pct: "0" };

/** A sizing card that isn't in use: greyed and not clickable, with why. */
function sizingCardStyle(inUse: boolean, minWidth: number): React.CSSProperties {
  return {
    background: "var(--panel-2)", border: "1px solid var(--border)", minWidth,
    opacity: inUse ? 1 : 0.45, pointerEvents: inUse ? undefined : "none",
  };
}

/** The ladder from a settings response (older responses carry only the three
 *  per-trim fields). */
function trimsFrom(s: Partial<DiscordSettings>): TrimRow[] {
  if (s.trims && s.trims.length > 0) return s.trims.map((t) => ({ ...t }));
  const v = s as Record<string, string | undefined>;
  return [
    { profit_gate_pct: v.trim_profit_gate_pct ?? "20", qty_pct: v.trim_qty_pct ?? "50", stop_pct: v.trim_stop_pct ?? "-25" },
    { profit_gate_pct: v.trim2_profit_gate_pct ?? "0", qty_pct: v.trim2_qty_pct ?? "50", stop_pct: v.trim2_stop_pct ?? "0" },
    { profit_gate_pct: v.trim3_profit_gate_pct ?? "0", qty_pct: v.trim3_qty_pct ?? "100", stop_pct: v.trim3_stop_pct ?? "0" },
  ];
}

/** The boxes on this page that hold typing until Save. */
type EditedField = "trims" | "fill" | "maxPerContract" | "maxPerOrder" | "sizeDollars";

const sameTrims = (a: TrimRow[], b: TrimRow[]) =>
  a.length === b.length && a.every((t, i) =>
    TRIM_COLUMNS.every((c) => t[c.key] === b[i][c.key]) && !!t.stop_trail === !!b[i].stop_trail);

/** What a fresh Trail starts at when there's nothing remembered for it. */
const DEFAULT_TRAIL = "10";

/** Flip a stop between a fixed level and a trailing give-back, keeping the
 *  value of the mode you're leaving so flipping back restores it. */
function flipStop<T extends { stop: string; trail: boolean; alt?: string }>(cur: T, trail: boolean): T {
  if (cur.trail === trail) return cur;
  return { ...cur, trail, stop: cur.alt ?? (trail ? DEFAULT_TRAIL : ""), alt: cur.stop };
}

/** Stop | Trail, in front of a stop box. */
function StopModeToggle({ trail, onChange, disabled, label }: {
  trail: boolean; onChange: (trail: boolean) => void; disabled?: boolean; label: string;
}) {
  return (
    <div role="radiogroup" aria-label={`${label}: stop or trailing stop`}
         className="flex shrink-0 rounded-lg border overflow-hidden"
         style={{ borderColor: "var(--border-strong)" }}>
      {([[false, "Stop"], [true, "Trail"]] as const).map(([value, text]) => (
        <button
          key={text}
          type="button"
          role="radio"
          aria-checked={trail === value}
          disabled={disabled}
          onClick={() => onChange(value)}
          title={value
            ? "Trailing stop: follows the high and exits on a give-back of this %"
            : "Fixed stop: a return from entry"}
          className="px-2 text-[11px] focus-ring disabled:opacity-50"
          style={{
            height: 30,
            background: trail === value ? "var(--accent-glow)" : "transparent",
            color: trail === value ? "var(--accent-2)" : "var(--muted)",
          }}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

export default function DiscordPage() {
  const router = useRouter();
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
  // Account-wide handling of parsed alerts. Manual until the server says
  // otherwise — never assume the permissive mode while loading.
  const [execMode, setExecMode] = useState<string>("manual");
  const [modeBusy, setModeBusy] = useState(false);
  // Paper until the server says otherwise — never show "live" optimistically.
  const [liveTrading, setLiveTrading] = useState(false);
  const [exitMode, setExitMode] = useState<ExitMode>("alerts");
  const [exitModeBusy, setExitModeBusy] = useState(false);
  // Which tab of the exits card is open, and which engine actually runs.
  const [exitTab, setExitTab] = useState<"ladder" | "ai">("ladder");
  const [exitEngine, setExitEngine] = useState<"ladder" | "ai">("ladder");
  const [qtyMultiplier, setQtyMultiplier] = useState(1);
  const [maxPerContract, setMaxPerContract] = useState("");
  // Sizing by contracts or by dollars. The toggle shows the choice before it's
  // saved (Dollars needs an amount first); sizeMode is what the server holds.
  const [sizeMode, setSizeMode] = useState<"contracts" | "dollars">("contracts");
  const [sizeModeUi, setSizeModeUi] = useState<"contracts" | "dollars">("contracts");
  const [sizeDollars, setSizeDollars] = useState("");
  const [sizeCap, setSizeCap] = useState<"none" | "per_contract" | "per_order">("none");
  const [savedSizeDollars, setSavedSizeDollars] = useState("");
  const [maxPerOrder, setMaxPerOrder] = useState("");
  // What the server currently holds, as distinct from what's in the box —
  // lets Save disable itself when nothing has changed.
  const [savedMaxPerContract, setSavedMaxPerContract] = useState("");
  const [savedMaxPerOrder, setSavedMaxPerOrder] = useState("");
  // The exit ladder: the On Fill stop, then one row per trim.
  const [trims, setTrims] = useState<TrimRow[]>(DEFAULT_TRIMS);
  const [savedTrims, setSavedTrims] = useState<TrimRow[]>(DEFAULT_TRIMS);
  const [fillStop, setFillStop] = useState("");
  const [savedFillStop, setSavedFillStop] = useState("");
  // On Fill's Stop | Trail, and the other mode's value while flipping.
  const [fillTrail, setFillTrail] = useState(false);
  const [savedFillTrail, setSavedFillTrail] = useState(false);
  const [fillAlt, setFillAlt] = useState<string | undefined>(undefined);
  // What the server last said for every field you type into, as a ref: the
  // 15s background reload runs a stale closure, so state can't tell it whether
  // a box holds an unsaved edit. It used to overwrite them all — an edited
  // ladder reverted 15s later unless Save had been clicked by then.
  const savedRef = useRef({
    trims: DEFAULT_TRIMS, fill: "", fillTrail: false,
    maxPerContract: "", maxPerOrder: "", sizeDollars: "",
  });
  const [pairFor, setPairFor] = useState<DiscordSource | null>(null);
  // Which settings the Alert handling card edits: null = the account's, else a
  // channel's. A channel follows the account until "Use account settings" is
  // turned off; it also has its own Market / Limit for entries.
  const [scope, setScope] = useState<string | null>(null);
  const scopeRef = useRef<string | null>(null);
  const [followsAccount, setFollowsAccount] = useState(true);
  const [entryType, setEntryType] = useState<"limit" | "market">("limit");
  const settingsUrl = (id: string | null = scopeRef.current) =>
    id ? `/api/discord-sources/${id}/settings` : "/api/discord-sources/settings";
  const [pair, setPair] = useState<Pairing | null>(null);

  // One hidden file input, retargeted at whichever source is uploading.
  const fileRef = useRef<HTMLInputElement | null>(null);
  const uploadTargetRef = useRef<string | null>(null);

  async function load() {
    try {
      // Channels and the account-wide alert-handling mode are fetched together:
      // the mode is part of the page's state, and loading it separately left the
      // card showing the default until something else happened to refresh it.
      const [list, settings, ai] = await Promise.all([
        api<DiscordSource[]>("/api/discord-sources"),
        api<DiscordSettings & { use_account_settings?: boolean; entry_order_type?: string }>(settingsUrl()),
        // Only for which engine is active; the AI tab loads its own settings.
        api<{ engine: "ladder" | "ai" }>("/api/discord-sources/ai-trim").catch(() => null),
      ]);
      setSources(list);
      if (ai) setExitEngine(ai.engine);
      applySettings(settings);
    } catch (e) {
      notify.fromError(e, "Failed to load Discord channels");
    }
  }

  /** Show the server's settings. ``force`` replaces what you typed: ``true``
   *  for everything (switching which settings are shown), or just the fields a
   *  save sent. Otherwise — the background reload — a box you have edited and
   *  not saved keeps your edit, and only boxes you haven't touched follow the
   *  server. */
  function applySettings(
    settings: DiscordSettings & { use_account_settings?: boolean; entry_order_type?: string },
    force: boolean | EditedField[] = false,
  ) {
      const forced = (f: EditedField) => force === true || (Array.isArray(force) && force.includes(f));
      setFollowsAccount(settings.use_account_settings ?? true);
      setEntryType(settings.entry_order_type === "market" ? "market" : "limit");
      setExecMode(settings.execution_mode);
      setLiveTrading(!!settings.live_trading);
      setExitMode(settings.exit_mode ?? (settings.auto_trim ? "auto" : "alerts"));
      setQtyMultiplier(settings.quantity_multiplier || 1);

      const prev = savedRef.current;
      const next = {
        trims: trimsFrom(settings),
        fill: settings.fill_stop_pct ?? "",
        fillTrail: !!settings.fill_stop_trail,
        maxPerContract: settings.max_per_contract ?? "",
        sizeDollars: settings.size_dollars ?? "",
        maxPerOrder: settings.max_per_order ?? "",
      };
      savedRef.current = next;
      setTrims((cur) => (forced("trims") || sameTrims(cur, prev.trims) ? next.trims : cur));
      setSavedTrims(next.trims);
      setFillStop((cur) => (forced("fill") || cur === prev.fill ? next.fill : cur));
      setSavedFillStop(next.fill);
      setFillTrail((cur) => (forced("fill") || cur === prev.fillTrail ? next.fillTrail : cur));
      setSavedFillTrail(next.fillTrail);
      setMaxPerContract((cur) => (forced("maxPerContract") || cur === prev.maxPerContract ? next.maxPerContract : cur));
      setSizeDollars((cur) => (forced("sizeDollars") || cur === prev.sizeDollars ? next.sizeDollars : cur));
      setSavedSizeDollars(next.sizeDollars);
      setSizeMode(settings.size_mode ?? "contracts");
      setSizeModeUi(settings.size_mode ?? "contracts");
      setSizeCap(settings.size_cap ?? "none");
      setSavedMaxPerContract(next.maxPerContract);
      setMaxPerOrder((cur) => (forced("maxPerOrder") || cur === prev.maxPerOrder ? next.maxPerOrder : cur));
      setSavedMaxPerOrder(next.maxPerOrder);
  }

  // The channel list alone — so the Entry / Exit pills reflect a settings
  // change at once, without reloading (and overwriting) the settings in view.
  async function refreshSources() {
    try {
      setSources(await api<DiscordSource[]>("/api/discord-sources"));
    } catch {
      /* the 15s refresh will catch up */
    }
  }

  async function changeScope(id: string | null) {
    scopeRef.current = id;
    setScope(id);
    if (id !== null) setExitTab("ladder");          // the AI tab is account-only
    try {
      applySettings(await api(settingsUrl(id)), true);    // other settings: replace everything
    } catch (e) {
      notify.fromError(e, "Could not load those settings");
    }
  }

  async function patchChannel(body: Record<string, unknown>, done: string) {
    if (!scope) return;
    setModeBusy(true);
    try {
      applySettings(await api(settingsUrl(scope), { method: "PATCH", body: JSON.stringify(body) }), true);
      notify.success(done);
      void refreshSources();
    } catch (e) {
      notify.fromError(e, "Could not save that");
    } finally {
      setModeBusy(false);
    }
  }

  useEffect(() => {
    (async () => {
      try {
        const u = await api<User>("/api/auth/me");
        // Discord is an opt-in, admin-enabled trader feature — and a subscriber
        // of such a trader gets it too, to trade the trader's channels on their
        // own settings. Anyone else is bounced to the dashboard so typing
        // /discord directly can't reach the page. The nav entry is hidden the
        // same way in AppShell.
        if (!u.discord_available) {
          router.replace("/dashboard");
          return;
        }
        setUser(u);
        await load();
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
    if (!user) return;
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
      setSources((prev) => prev.map((x) => (x.id === s.id
        ? { ...updated, entry_summary: updated.entry_summary ?? x.entry_summary,
            exit_summary: updated.exit_summary ?? x.exit_summary }
        : x)));
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

  async function setExecutionMode(mode: string) {
    setModeBusy(true);
    try {
      const next = await api<{ execution_mode: string; live_trading: boolean }>(
        settingsUrl(),
        { method: "PATCH", body: JSON.stringify({ execution_mode: mode }) }
      );
      setExecMode(next.execution_mode);
      setLiveTrading(!!next.live_trading);
      void refreshSources();
      notify.success(
        mode === "auto"
          ? "Parsed alerts will be approved automatically"
          : "You'll review each alert before it's approved"
      );
    } catch (e) {
      notify.fromError(e, "Could not change the mode");
    } finally {
      setModeBusy(false);
    }
  }

  const ladderDirty = !sameTrims(trims, savedTrims) || fillStop !== savedFillStop
    || fillTrail !== savedFillTrail;
  /** Everything the exit ladder's Save sends: the whole ladder at once. */
  const ladderPatch = () => ({
    trims: trims.map(({ stop_alt: _alt, ...t }) => ({ ...t, stop_trail: !!t.stop_trail })),
    fill_stop_pct: fillStop.trim(),
    fill_stop_trail: fillTrail,
  });
  const lastTrimLeavesRunner = Number(trims[trims.length - 1]?.qty_pct) < 100;

  async function saveSizing(patch: Record<string, unknown>) {
    setModeBusy(true);
    try {
      const r = await api<DiscordSettings>(settingsUrl(), {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      // What was saved IS now the server's value: those boxes take it. Anything
      // else being edited (the ladder, while saving Max per order) is left be.
      const sent: EditedField[] = [];
      if ("trims" in patch) sent.push("trims");
      if ("fill_stop_pct" in patch) sent.push("fill");
      if ("max_per_contract" in patch) sent.push("maxPerContract");
      if ("size_dollars" in patch) sent.push("sizeDollars");
      if ("max_per_order" in patch) sent.push("maxPerOrder");
      applySettings(r, sent);
      notify.success("Saved");
      void refreshSources();
    } catch (e) {
      notify.fromError(e, "Could not save that setting");
    } finally {
      setModeBusy(false);
    }
  }

  async function setLiveTrading_(next: boolean) {
    // Turning this ON starts spending real money, so it asks first. Turning it
    // OFF is always safe and never prompts.
    if (next && !window.confirm(
      "Enable live trading?\n\nApproved Discord alerts will place REAL orders on your " +
      "connected broker. Make sure the parser is behaving the way you expect in paper first."
    )) return;
    setModeBusy(true);
    try {
      const r = await api<{ execution_mode: string; live_trading: boolean }>(
        settingsUrl(),
        { method: "PATCH", body: JSON.stringify({ live_trading: next }) }
      );
      setLiveTrading(!!r.live_trading);
      notify.success(next ? "Live trading enabled" : "Back to paper — nothing reaches your broker");
      void refreshSources();
    } catch (e) {
      notify.fromError(e, "Could not change that");
    } finally {
      setModeBusy(false);
    }
  }

  async function changeExitMode(next: ExitMode) {
    if (next === exitMode) return;
    setExitModeBusy(true);
    const prev = exitMode;
    setExitMode(next);
    try {
      const r = await api<{ exit_mode?: ExitMode }>(
        settingsUrl(),
        { method: "PATCH", body: JSON.stringify({ exit_mode: next }) }
      );
      setExitMode(r.exit_mode ?? next);
      void refreshSources();
      notify.success(EXIT_MODES.find((m) => m.value === next)!.toast);
    } catch (e) {
      setExitMode(prev);
      notify.fromError(e, "Could not change that");
    } finally {
      setExitModeBusy(false);
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

  // A subscriber sees the channels of the trader they follow, and trades them
  // on their own alert-handling settings below. The channels themselves are
  // the trader's: the only thing a subscriber changes on one is the switch.
  const isSub = user.role !== "trader";

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
            Kopyya watches an alert channel you already follow, reads each message the moment
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
      <div
        className={
          isSub
            ? "grid grid-cols-1 gap-4 items-start"
            : "grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_340px] gap-4 items-start"
        }
      >
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
            {isSub && (
              <span className="text-[11px] ml-auto mr-2" style={{ color: "var(--muted)" }}>
                Your trader&apos;s channels — off skips new entries; exits still go through
              </span>
            )}
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
                {isSub
                  ? "Your trader hasn't added a Discord channel yet."
                  : "Add one on the right, then connect your Discord account."}
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
                          {/* How this channel trades: its own settings, or the
                              account's while it follows them. */}
                          {([["Entry", s.entry_summary], ["Exit", s.exit_summary]] as const).map(
                            ([name, value]) => value ? (
                              <span
                                key={name}
                                className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full"
                                style={{
                                  color: "var(--text-2)",
                                  background: "var(--panel-2)",
                                  border: "1px solid var(--border)",
                                }}
                                title={name === "Entry"
                                  ? "How entries from this channel are placed"
                                  : "What closes positions this channel opened"}
                              >
                                <span style={{ color: "var(--muted)" }}>{name}:</span> {value}
                              </span>
                            ) : null,
                          )}
                        </div>
                        <div className="text-[12px] truncate mt-0.5" style={{ color: "var(--muted)" }}>
                          {s.guild_name ? `${s.guild_name} · ` : ""}
                          {s.channel_name ? `#${s.channel_name}` : `Channel ${s.channel_id}`}
                        </div>
                      </div>

                      {/* On/off for this channel */}
                      <label
                        className="flex items-center gap-2 text-[11px] cursor-pointer select-none shrink-0"
                        title={
                          isSub
                            ? s.is_enabled
                              ? "On — new entries from this channel are traded on your account"
                              : "Off — new entries are skipped; exits for positions you hold still go through"
                            : s.is_enabled ? "Monitoring on" : "Monitoring off"
                        }
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
                        than hidden behind a dropdown. The trader's to set. */}
                    {!isSub && (
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
                    )}

                    {/* Actions — the trader's; a subscriber only has the switch. */}
                    {!isSub && (
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
                    )}

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
                        {isSub
                          ? "Your trader's Discord is signed out — alerts resume once they reconnect."
                          : "Waiting for a Discord sign-in — connect once on the right."}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Alert handling — what happens to an alert once it's parsed. The
            account's settings, or one channel's ("Settings for"): a channel
            follows the account until it is given its own. Below the channel
            list because it needs the width for three side-by-side decisions. */}
        <div className="card overflow-hidden">
          <div
            className="flex items-center justify-between px-5 py-3.5"
            style={{ borderBottom: "1px solid var(--border)" }}
          >
            <div className="flex items-center gap-2">
              <ScanLine size={15} style={{ color: "var(--accent-2)" }} />
              <h3 className="text-sm font-semibold" style={{ color: "var(--text)" }}>
                Alert handling
              </h3>
            </div>
            <span
              className="text-[11px] px-2 py-0.5 rounded-full"
              style={{
                background: liveTrading ? "var(--bad-soft)" : "var(--panel-2)",
                color: liveTrading ? "var(--bad)" : "var(--muted)",
              }}
            >
              {liveTrading ? "LIVE" : "Test mode"}
            </span>
          </div>

          <div className="p-5 space-y-5">
            {/* Whose settings these are: the account's, or one channel's. */}
            <div
              className="rounded-xl px-4 py-3 flex items-center gap-x-5 gap-y-2.5 flex-wrap"
              style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}
            >
              <label className="flex items-center gap-2 text-[12px]" style={{ color: "var(--text-2)" }}>
                Settings for
                <select
                  value={scope ?? ""}
                  onChange={(e) => void changeScope(e.target.value || null)}
                  className="rounded-md border px-2 py-1 text-[12px] bg-transparent focus-ring"
                  style={{ borderColor: "var(--border)", color: "var(--text)" }}
                >
                  <option value="">Account (every channel that follows it)</option>
                  {sources.map((s) => (
                    <option key={s.id} value={s.id}>{s.label}</option>
                  ))}
                </select>
              </label>
              {scope && (
                <>
                  <label className="flex items-center gap-2 text-[12px] cursor-pointer select-none" style={{ color: "var(--text-2)" }}>
                    <input
                      type="checkbox"
                      className="h-3.5 w-3.5 cursor-pointer"
                      style={{ accentColor: "var(--accent)" }}
                      checked={followsAccount}
                      disabled={modeBusy}
                      onChange={(e) => void patchChannel(
                        { use_account_settings: e.target.checked },
                        e.target.checked
                          ? "This channel follows the account settings again"
                          : "This channel now has its own settings, starting from the account's",
                      )}
                    />
                    Use account settings
                  </label>
                  <div className="flex items-center gap-1.5 text-[12px]" style={{ color: "var(--text-2)" }}>
                    Entries
                    {(["limit", "market"] as const).map((t) => (
                      <button
                        key={t}
                        type="button"
                        disabled={modeBusy}
                        onClick={() => void patchChannel(
                          { entry_order_type: t },
                          t === "market" ? "Entries from this channel go at market"
                            : "Entries from this channel use the alert's price",
                        )}
                        className="px-2.5 py-0.5 text-[11px] font-medium rounded-full disabled:opacity-60"
                        style={{
                          background: entryType === t ? "var(--accent-glow)" : "transparent",
                          border: `1px solid ${entryType === t ? "rgba(44,147,197,0.45)" : "var(--border)"}`,
                          color: entryType === t ? "var(--accent-2)" : "var(--muted)",
                        }}
                        title={t === "market"
                          ? "Buy at market in the regular session (outside it, the alert's limit is kept). Exits still follow the exit ladder."
                          : "Buy at the alert's price, as before"}
                      >
                        {t === "limit" ? "Limit" : "Market"}
                      </button>
                    ))}
                  </div>
                  <span className="text-[11px] w-full" style={{ color: "var(--muted)" }}>
                    {followsAccount
                      ? "Following the account settings below. Turn the switch off to give this channel its own."
                      : "This channel's own settings — changes here affect only this channel. AI trimming stays account-wide."}
                  </span>
                </>
              )}
            </div>

            <fieldset
              disabled={!!scope && followsAccount}
              className="space-y-5 disabled:opacity-60"
              style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}
            >
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
            {/* 1 — does it spend money (Execution comes first so Approval,
                which is only meaningful in Live, reads as the follow-on step) */}
            <div className="space-y-2.5">
              <div className="text-[11px] font-semibold uppercase tracking-wide"
                   style={{ color: "var(--muted)" }}>
                Execution
              </div>

              {[
                { live: false, label: "Test mode", detail: "Validated and recorded. Nothing reaches your broker." },
                { live: true, label: "Live", detail: "Approved alerts place REAL orders." },
              ].map(({ live, label, detail }) => {
                const active = liveTrading === live;
                const danger = live && active;
                return (
                  <button
                    key={label}
                    type="button"
                    disabled={modeBusy}
                    onClick={() => setLiveTrading_(live)}
                    className="w-full text-left rounded-xl px-3.5 py-2.5 transition-colors disabled:opacity-60"
                    style={{
                      background: danger
                        ? "var(--bad-soft)"
                        : active ? "var(--accent-glow)" : "var(--panel-2)",
                      border: `1px solid ${
                        danger ? "rgba(255,107,107,0.35)"
                        : active ? "rgba(44,147,197,0.45)" : "var(--border)"
                      }`,
                    }}
                  >
                    <div className="flex items-center gap-2">
                      <RadioDot active={active} tone={danger ? "var(--bad)" : undefined} />
                      <span className="text-[12.5px] font-semibold"
                            style={{ color: danger ? "var(--bad)" : active ? "var(--accent-2)" : "var(--text)" }}>
                        {label}
                      </span>
                    </div>
                    <p className="text-[11px] mt-0.5 leading-snug pl-[22px]"
                       style={{ color: "var(--muted)" }}>
                      {detail}
                    </p>
                  </button>
                );
              })}

              <p className="text-[11px] leading-snug" style={{ color: "var(--muted)" }}>
                {scope ? "Applies to this channel, from now on." : "Applies to every channel that follows the account, from now on."}
              </p>
            </div>

            {/* 2 — who approves. Only meaningful when Execution is Live; in
                Paper mode nothing reaches a broker, so the approval choice is
                moot and both options are disabled. */}
            <div className="space-y-2.5"
                 style={{ borderLeft: "1px solid var(--border)", paddingLeft: 20 }}>
              <div className="text-[11px] font-semibold uppercase tracking-wide"
                   style={{ color: "var(--muted)" }}>
                Approval
              </div>
              {[
                { value: "manual", label: "Review each alert",
                  detail: "Accept or reject in Order History." },
                { value: "auto", label: "Auto-approve",
                  detail: "Parsed alerts go straight through." },
              ].map(({ value, label, detail }) => {
                const active = execMode === value;
                return (
                  <button
                    key={value}
                    type="button"
                    disabled={modeBusy || !liveTrading}
                    onClick={() => setExecutionMode(value)}
                    className="w-full text-left rounded-xl px-3.5 py-2.5 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                    style={{
                      background: active ? "var(--accent-glow)" : "var(--panel-2)",
                      border: `1px solid ${active ? "rgba(44,147,197,0.45)" : "var(--border)"}`,
                    }}
                  >
                    <div className="flex items-center gap-2">
                      <RadioDot active={active} />
                      <span className="text-[12.5px] font-semibold"
                            style={{ color: active ? "var(--accent-2)" : "var(--text)" }}>
                        {label}
                      </span>
                    </div>
                    <p className="text-[11px] mt-0.5 leading-snug pl-[22px]"
                       style={{ color: "var(--muted)" }}>
                      {detail}
                    </p>
                  </button>
                );
              })}

              {!liveTrading && (
                <p className="text-[11px] leading-snug" style={{ color: "var(--muted)" }}>
                  Available once Execution is set to Live.
                </p>
              )}
            </div>
            </div>

            {/* Sizing spans the full width: the multiplier is ten options and
                needs the room, and it applies to whatever the two choices above
                decide rather than being a third peer to them. */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 18 }}>
            {/* 2 — how big. Both controls on one row: they answer the same
                question (how much exposure per alert) and reading them together
                is how you judge whether the pair is sane. */}
            <div className="space-y-3">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: "var(--muted)" }}>
                  Sizing
                </div>
                {/* ONE cap at a time, and only with Contracts — Dollars is its own rule. */}
                <div className="flex items-center gap-2 text-[11px]" style={{ color: "var(--muted)" }}>
                  <span>Cap</span>
                  <div role="radiogroup" aria-label="Which cap applies" className="flex rounded-lg border overflow-hidden"
                       style={{ borderColor: "var(--border-strong)", opacity: sizeModeUi === "dollars" ? 0.45 : 1 }}
                       title={sizeModeUi === "dollars" ? "Not used while sizing by dollars — the amount is the rule" : "Only one cap applies at a time"}>
                    {([["none", "None"], ["per_contract", "Max per contract"], ["per_order", "Max per order"]] as const).map(([value, text]) => (
                      <button
                        key={value}
                        type="button"
                        role="radio"
                        aria-checked={sizeCap === value}
                        disabled={modeBusy || sizeModeUi === "dollars"}
                        onClick={() => { setSizeCap(value); saveSizing({ size_cap: value }); }}
                        className="px-2.5 py-0.5 disabled:cursor-not-allowed"
                        style={{
                          background: sizeCap === value ? "var(--accent-glow)" : "transparent",
                          color: sizeCap === value ? "var(--accent-2)" : "var(--muted)",
                        }}
                      >
                        {text}
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              <div className="flex gap-4 flex-wrap items-stretch">
                {/* Size each entry: by contracts, or by dollars */}
                <div
                  className="rounded-xl px-4 py-3 flex-1"
                  style={{ background: "var(--panel-2)", border: "1px solid var(--border)", minWidth: 330 }}
                >
                  <div className="flex items-center justify-between gap-2">
                    <label className="text-[11px] font-medium" style={{ color: "var(--text-2)" }}>
                      Size each entry by
                    </label>
                    <div role="radiogroup" aria-label="Size each entry by" className="flex rounded-lg border overflow-hidden"
                         style={{ borderColor: "var(--border-strong)" }}>
                      {([["contracts", "Contracts"], ["dollars", "Dollars"]] as const).map(([value, text]) => (
                        <button
                          key={value}
                          type="button"
                          role="radio"
                          aria-checked={sizeModeUi === value}
                          disabled={modeBusy}
                          onClick={() => {
                            setSizeModeUi(value);
                            // Contracts can switch at once; Dollars waits for an amount.
                            if (value === "contracts" && sizeMode !== "contracts") saveSizing({ size_mode: "contracts" });
                            if (value === "dollars" && sizeMode !== "dollars" && savedSizeDollars) saveSizing({ size_mode: "dollars" });
                          }}
                          className="px-2.5 py-0.5 text-[11px] disabled:opacity-60"
                          style={{
                            background: sizeModeUi === value ? "var(--accent-glow)" : "transparent",
                            color: sizeModeUi === value ? "var(--accent-2)" : "var(--muted)",
                          }}
                        >
                          {text}
                        </button>
                      ))}
                    </div>
                  </div>

                  {sizeModeUi === "contracts" ? (
                    <>
                      <div className="flex gap-1 mt-2 flex-wrap items-center">
                        {Array.from({ length: 10 }, (_, i) => i + 1).map((n) => {
                          const active = qtyMultiplier === n;
                          return (
                            <button
                              key={n}
                              type="button"
                              disabled={modeBusy}
                              onClick={() => saveSizing({ quantity_multiplier: n })}
                              className="text-[11px] font-semibold rounded-lg transition-colors disabled:opacity-60"
                              style={{
                                width: 28, height: 28,
                                background: active ? "var(--accent-glow)" : "transparent",
                                border: `1px solid ${active ? "rgba(44,147,197,0.5)" : "var(--border)"}`,
                                color: active ? "var(--accent-2)" : "var(--muted)",
                              }}
                            >
                              {n}
                            </button>
                          );
                        })}
                        <span className="text-[11px] ml-1 tabular-nums" style={{ color: "var(--accent-2)" }}>
                          {qtyMultiplier} {qtyMultiplier === 1 ? "contract" : "contracts"}
                        </span>
                      </div>
                      <p className="text-[11px] mt-2 leading-snug" style={{ color: "var(--muted)" }}>
                        Every entry buys exactly this many, whatever size the alert says. A close always sells the position you hold.
                      </p>
                    </>
                  ) : (
                    <>
                      <div className="flex gap-2 mt-2">
                        <div className="relative flex-1">
                          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm" style={{ color: "var(--muted)" }}>$</span>
                          <input
                            type="number"
                            min="1"
                            step="50"
                            inputMode="decimal"
                            placeholder="500"
                            aria-label="Dollars per entry"
                            value={sizeDollars}
                            disabled={modeBusy}
                            onChange={(e) => setSizeDollars(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter" && sizeDollars.trim()) saveSizing({ size_dollars: sizeDollars, size_mode: "dollars" });
                            }}
                            className="w-full rounded-lg border py-1.5 text-sm bg-transparent focus-ring"
                            style={{ borderColor: "var(--border-strong)", color: "var(--text)", paddingLeft: 22, paddingRight: 12 }}
                          />
                        </div>
                        <button
                          type="button"
                          disabled={modeBusy || !sizeDollars.trim() || (sizeDollars === savedSizeDollars && sizeMode === "dollars")}
                          onClick={() => saveSizing({ size_dollars: sizeDollars, size_mode: "dollars" })}
                          className="btn-primary px-3.5 py-1.5 text-[12px] disabled:opacity-40"
                        >
                          Save
                        </button>
                      </div>
                      <p className="text-[11px] mt-2 leading-snug" style={{ color: "var(--muted)" }}>
                        Each entry buys as many whole contracts as fit in this amount, at the price it will pay — the live
                        price for a market entry. Light alerts spend half; if one contract costs more, the entry is skipped.
                        Averaging down still doubles what you hold. A close always sells the position you hold.
                      </p>
                    </>
                  )}
                </div>

                {/* Max per contract — in use only with Contracts, as the chosen cap */}
                <div
                  className="rounded-xl px-4 py-3 flex-1"
                  aria-disabled={!(sizeModeUi === "contracts" && sizeCap === "per_contract")}
                  title={sizeModeUi === "dollars" ? "Not used while sizing by dollars"
                    : sizeCap !== "per_contract" ? "Not in use — choose Max per contract as the cap above" : undefined}
                  style={sizingCardStyle(sizeModeUi === "contracts" && sizeCap === "per_contract", 300)}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <label className="text-[11px] font-medium" style={{ color: "var(--text-2)" }}>
                      Max per contract
                    </label>
                    <span className="text-[11px] tabular-nums" style={{ color: "var(--muted)" }}>
                      {savedMaxPerContract ? `$${savedMaxPerContract}` : "No limit"}
                    </span>
                  </div>
                  <div className="flex gap-2 mt-2">
                    <div className="relative flex-1">
                      <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm"
                            style={{ color: "var(--muted)" }}>$</span>
                      <input
                        type="number"
                        min="1"
                        step="50"
                        placeholder="No limit"
                        value={maxPerContract}
                        disabled={modeBusy}
                        onChange={(e) => setMaxPerContract(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") saveSizing({ max_per_contract: maxPerContract });
                        }}
                        className="w-full rounded-lg border pl-7 pr-3 py-1.5 text-sm bg-transparent focus-ring"
                        style={{ borderColor: "var(--border)", color: "var(--text)" }}
                      />
                    </div>
                    {/* Explicit save: typing a number shouldn't commit a risk
                        limit the moment focus moves. */}
                    <button
                      type="button"
                      disabled={modeBusy || maxPerContract === savedMaxPerContract}
                      onClick={() => saveSizing({ max_per_contract: maxPerContract })}
                      className="btn-primary px-3.5 py-1.5 text-[12px] disabled:opacity-40"
                    >
                      {modeBusy ? <Spinner /> : "Save"}
                    </button>
                  </div>
                  <p className="text-[11px] mt-2 leading-snug" style={{ color: "var(--muted)" }}>
                    Skips an entry when one contract&apos;s value (premium × 100) is above this — at the live price for a market entry.
                    Options only — closes always go through.
                  </p>
                </div>

                {/* Max per order — the whole order's value, not one contract's.
                    Kept as a separate limit rather than folded into the one
                    above: they answer different questions, and an alert can
                    pass either and fail the other. */}
                <div
                  className="rounded-xl px-4 py-3 flex-1"
                  aria-disabled={!(sizeModeUi === "contracts" && sizeCap === "per_order")}
                  title={sizeModeUi === "dollars" ? "Not used while sizing by dollars"
                    : sizeCap !== "per_order" ? "Not in use — choose Max per order as the cap above" : undefined}
                  style={sizingCardStyle(sizeModeUi === "contracts" && sizeCap === "per_order", 300)}
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <label className="text-[11px] font-medium" style={{ color: "var(--text-2)" }}>
                      Max per order
                    </label>
                    <span className="text-[11px] tabular-nums" style={{ color: "var(--muted)" }}>
                      {savedMaxPerOrder ? `$${savedMaxPerOrder}` : "No limit"}
                    </span>
                  </div>
                  <div className="flex gap-2 mt-2">
                    <div className="relative flex-1">
                      <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm"
                            style={{ color: "var(--muted)" }}>$</span>
                      <input
                        type="number"
                        min="1"
                        step="100"
                        placeholder="No limit"
                        value={maxPerOrder}
                        disabled={modeBusy}
                        onChange={(e) => setMaxPerOrder(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") saveSizing({ max_per_order: maxPerOrder });
                        }}
                        className="w-full rounded-lg border pl-7 pr-3 py-1.5 text-sm bg-transparent focus-ring"
                        style={{ borderColor: "var(--border)", color: "var(--text)" }}
                      />
                    </div>
                    {/* Explicit save, same as the limit above: typing a number
                        shouldn't commit a risk limit when focus moves. */}
                    <button
                      type="button"
                      disabled={modeBusy || maxPerOrder === savedMaxPerOrder}
                      onClick={() => saveSizing({ max_per_order: maxPerOrder })}
                      className="btn-primary px-3.5 py-1.5 text-[12px] disabled:opacity-40"
                    >
                      {modeBusy ? <Spinner /> : "Save"}
                    </button>
                  </div>
                  <p className="text-[11px] mt-2 leading-snug" style={{ color: "var(--muted)" }}>
                    Cuts an entry to the most contracts that fit under this total (quantity × price, × 100 for options — the live price for a market entry); skips it only if not even one fits. Closes always go through.
                  </p>
                </div>
              </div>

              {/* The exit ladder, below the sizing cards and full width: the
                  grid is three columns of inputs, and squeezed into a flex row
                  beside them it was the one card that always wrapped.
                  Every level is measured from the position's ENTRY price, so
                  they are fixed the moment it opens. */}
              <div
                className="rounded-xl px-4 py-3 w-full mt-4"
                style={{ background: "var(--panel-2)", border: "1px solid var(--border)" }}
              >
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <div role="tablist" aria-label="Exit engine" className="flex gap-1">
                      {(scope
                        ? ([["ladder", "Exit ladder"]] as const)
                        : ([["ladder", "Exit ladder"], ["ai", "AI trimming"]] as const)
                      ).map(([value, label]) => (
                        <button
                          key={value}
                          type="button"
                          role="tab"
                          aria-selected={exitTab === value}
                          onClick={() => setExitTab(value)}
                          className="text-[11px] font-medium px-2.5 py-1 rounded-md"
                          style={{
                            color: exitTab === value ? "var(--text)" : "var(--muted)",
                            background: exitTab === value ? "var(--accent-glow)" : "transparent",
                            border: `1px solid ${exitTab === value ? "rgba(44,147,197,0.5)" : "transparent"}`,
                          }}
                        >
                          {label}
                          {exitEngine === value && (
                            <span className="ml-1.5 text-[9px] uppercase tracking-wide" style={{ color: "var(--good)" }}>
                              active
                            </span>
                          )}
                        </button>
                      ))}
                    </div>
                    <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                      {exitTab === "ladder" ? "measured from entry price" : "OpenRouter decides each exit"}
                    </span>
                  </div>

                  {exitTab === "ai" ? (
                    <AiTrimPanel onEngineChange={setExitEngine} />
                  ) : (
                  <>
                  {exitEngine === "ai" && (
                    <p className="mt-2 text-[11px] rounded-md px-2 py-1" style={{ color: "var(--warn)", border: "1px solid var(--warn)" }}>
                      AI trimming is managing exits, so Auto trim is paused. These rungs still apply
                      to exit alerts your Discord author posts.
                    </p>
                  )}

                  {/* What makes a position leave. On alerts: a rung waits for
                      its Discord alert and the Profit target below is the
                      condition that alert has to meet. Auto trim: no alert to
                      wait for — the rung fires the moment its target is
                      reached. Manual: nothing sells on its own. */}
                  <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-4" role="radiogroup" aria-label="Exits">
                    {EXIT_MODES.map(({ value, label, detail }) => {
                      const active = exitMode === value;
                      return (
                        <button
                          key={value}
                          type="button"
                          role="radio"
                          aria-checked={active}
                          disabled={exitModeBusy}
                          onClick={() => void changeExitMode(value)}
                          className="text-left rounded-lg px-3 py-2 transition-colors disabled:opacity-60"
                          style={{
                            background: active ? "var(--accent-glow)" : "transparent",
                            border: `1px solid ${active ? "rgba(44,147,197,0.45)" : "var(--border)"}`,
                          }}
                        >
                          <div className="flex items-center gap-2">
                            <RadioDot active={active} />
                            <span className="text-[12px] font-semibold"
                                  style={{ color: active ? "var(--accent-2)" : "var(--text)" }}>
                              {label}
                            </span>
                          </div>
                          <p className="text-[11px] mt-0.5 leading-snug pl-[22px]" style={{ color: "var(--muted)" }}>
                            {detail}
                          </p>
                        </button>
                      );
                    })}
                  </div>
                  {exitMode === "manual" && (
                    <p className="mt-2 text-[11px] leading-snug" style={{ color: "var(--muted)" }}>
                      The trims below are not used while exits are Manual — except for an exit you
                      type into the alert composer yourself, which still runs on them.
                    </p>
                  )}

                  {/* One ROW per stage, one COLUMN per setting — read across a
                      row for what happens at that stage.

                      No overflow wrapper: "overflow-x-auto" makes an element a
                      scroll container in BOTH axes, and .focus-ring draws its
                      outline 2px OUTSIDE the input, so the ring was clipped on
                      every cell. The columns are minmax(0,1fr) and shrink on
                      their own, so nothing needed to scroll. */}
                  <div className="mt-3 grid gap-x-2 gap-y-2 items-center"
                       style={{ gridTemplateColumns: "64px repeat(3, minmax(0, 1fr)) 22px" }}>
                    {/* Header: the settings. */}
                    <span />
                    {TRIM_COLUMNS.map((c) => (
                      <span
                        key={c.key}
                        className="text-[10px] font-medium uppercase tracking-wide text-center"
                        style={{ color: "var(--text-2)" }}
                        title={c.hint}
                      >
                        {c.label}
                      </span>
                    ))}
                    <span />

                    {/* On Fill: nothing is sold when the entry fills, so the
                        target and quantity are blank, with no box to type in.
                        Only the stop is set here. */}
                    <span className="text-[11px]" style={{ color: "var(--muted)" }}
                          title="When the entry fills: the stop that goes on straight away">
                      On Fill
                    </span>
                    <span aria-hidden="true" />
                    <span aria-hidden="true" />
                    <div className="flex items-center gap-1.5 min-w-0">
                      <StopModeToggle
                        label="On Fill"
                        trail={fillTrail}
                        disabled={modeBusy}
                        onChange={(trail) => {
                          const next = flipStop({ stop: fillStop, trail: fillTrail, alt: fillAlt }, trail);
                          setFillTrail(next.trail);
                          setFillStop(next.stop);
                          setFillAlt(next.alt);
                        }}
                      />
                      <div className="relative flex-1 min-w-0">
                        <input
                          id="ladder-fill-stop"
                          aria-label={fillTrail ? "Trailing stop, On Fill" : "Stop, On Fill"}
                          type="number"
                          min={fillTrail ? "1" : "-99"}
                          max={fillTrail ? "99" : "-1"}
                          step="5"
                          placeholder={fillTrail ? "give-back" : "none"}
                          value={fillStop}
                          disabled={modeBusy}
                          onChange={(e) => setFillStop(e.target.value)}
                          onKeyDown={(e) => { if (e.key === "Enter") saveSizing(ladderPatch()); }}
                          title={fillTrail
                            ? "Trailing stop from the fill: 20 exits on a 20% give-back from the best price since the entry filled."
                            : "Stop placed as soon as the entry fills, as a return from entry: -25 is 25% below. Leave empty for no stop until the first trim."}
                          className="w-full rounded-lg border py-1.5 text-[13px] bg-transparent focus-ring text-right"
                          style={{ borderColor: "var(--border-strong)", color: "var(--text)", paddingLeft: 8, paddingRight: 20 }}
                        />
                        <span className="absolute right-2 top-1/2 -translate-y-1/2 text-[13px]" style={{ color: "var(--muted)" }}>%</span>
                      </div>
                    </div>
                    <span />

                    {trims.map((t, i) => (
                      <Fragment key={i}>
                        <span className="text-[11px]" style={{ color: "var(--muted)" }}>Trim {i + 1}</span>
                        {TRIM_COLUMNS.filter((c) => c.key !== "stop_pct").map((c) => (
                          <div key={c.key} className="relative">
                            <input
                              id={`ladder-trim${i + 1}-${c.key}`}
                              aria-label={`${c.label}, Trim ${i + 1}`}
                              type="number"
                              min={c.min}
                              max={c.max}
                              step="5"
                              value={t[c.key]}
                              disabled={modeBusy}
                              onChange={(e) =>
                                setTrims((rows) => rows.map((r, j) => (j === i ? { ...r, [c.key]: e.target.value } : r)))
                              }
                              onKeyDown={(e) => { if (e.key === "Enter") saveSizing(ladderPatch()); }}
                              className="w-full rounded-lg border py-1.5 text-[13px] bg-transparent focus-ring text-right"
                              style={{
                                // --border is 6% white in dark, which on a panel
                                // reads as no edge at all. An input people are
                                // meant to type into needs the stronger token.
                                borderColor: "var(--border-strong)",
                                color: "var(--text)",
                                paddingLeft: 8,
                                paddingRight: 20,
                              }}
                            />
                            <span className="absolute right-2 top-1/2 -translate-y-1/2 text-[13px]" style={{ color: "var(--muted)" }}>%</span>
                          </div>
                        ))}
                        <div className="flex items-center gap-1.5 min-w-0">
                          <StopModeToggle
                            label={`Trim ${i + 1}`}
                            trail={!!t.stop_trail}
                            disabled={modeBusy}
                            onChange={(trail) =>
                              setTrims((rows) => rows.map((r, j) => {
                                if (j !== i) return r;
                                const next = flipStop({ stop: r.stop_pct, trail: !!r.stop_trail, alt: r.stop_alt }, trail);
                                return { ...r, stop_pct: next.stop, stop_trail: next.trail, stop_alt: next.alt };
                              }))
                            }
                          />
                          <div className="relative flex-1 min-w-0">
                            <input
                              id={`ladder-trim${i + 1}-stop_pct`}
                              aria-label={`${t.stop_trail ? "Trailing stop" : "Stop"}, Trim ${i + 1}`}
                              type="number"
                              min={t.stop_trail ? "1" : "-100"}
                              max={t.stop_trail ? "99" : undefined}
                              step="5"
                              placeholder={t.stop_trail ? "give-back" : undefined}
                              value={t.stop_pct}
                              disabled={modeBusy}
                              onChange={(e) =>
                                setTrims((rows) => rows.map((r, j) => (j === i ? { ...r, stop_pct: e.target.value } : r)))
                              }
                              onKeyDown={(e) => { if (e.key === "Enter") saveSizing(ladderPatch()); }}
                              title={t.stop_trail
                                ? "Trailing stop: follows the high after this trim and exits on a give-back of this %"
                                : "Where the stop sits after this trim, as a return from entry"}
                              className="w-full rounded-lg border py-1.5 text-[13px] bg-transparent focus-ring text-right"
                              style={{ borderColor: "var(--border-strong)", color: "var(--text)", paddingLeft: 8, paddingRight: 20 }}
                            />
                            <span className="absolute right-2 top-1/2 -translate-y-1/2 text-[13px]" style={{ color: "var(--muted)" }}>%</span>
                          </div>
                        </div>
                        {trims.length > 1 ? (
                          <button
                            type="button"
                            disabled={modeBusy}
                            onClick={() => setTrims((rows) => rows.filter((_, j) => j !== i))}
                            aria-label={`Remove Trim ${i + 1}`}
                            title={`Remove Trim ${i + 1}`}
                            className="focus-ring rounded p-0.5 opacity-60 hover:opacity-100 disabled:opacity-30"
                            style={{ color: "var(--muted)" }}
                          >
                            <X size={13} />
                          </button>
                        ) : <span />}
                      </Fragment>
                    ))}
                  </div>

                  <div className="mt-2 flex items-center gap-3 flex-wrap">
                    <button
                      type="button"
                      disabled={modeBusy || trims.length >= MAX_TRIMS}
                      onClick={() => setTrims((rows) => [...rows, { ...NEW_TRIM }])}
                      className="btn-ghost px-2.5 py-1 text-[11px] disabled:opacity-40"
                      title={trims.length >= MAX_TRIMS ? `A ladder can have up to ${MAX_TRIMS} trims` : "Add another trim after the last one"}
                    >
                      + Add trim
                    </button>
                    <p className="text-[11px] leading-snug"
                       style={{ color: lastTrimLeavesRunner ? "var(--warn, #b45309)" : "var(--muted)" }}>
                      If the last trim is not 100% then it will round down and leave runners.
                    </p>
                  </div>

                  <div className="flex items-center gap-2 mt-3">
                    <button
                      type="button"
                      disabled={modeBusy || !ladderDirty}
                      onClick={() => saveSizing(ladderPatch())}
                      className="btn-primary px-3.5 py-1.5 text-[12px] disabled:opacity-40"
                    >
                      {modeBusy ? <Spinner /> : "Save"}
                    </button>
                    <p className="text-[11px] leading-snug" style={{ color: "var(--muted)" }}>
                      Trim of rem. qty is a share of what is STILL held, so 50 / 50 / 100 works a
                      position of 4 down as 2, then 1, then 1. A trim only fires above
                      its profit target; its stop applies to whatever is left after it.
                      Stop is a level from entry; Trail follows the high and exits on a
                      give-back of that %.
                    </p>
                  </div>
                  </>
                  )}
              </div>
            </div>
            </div>
          </fieldset>
          </div>
        </div>
        </div>

        {/* Sidebar: the two things you DO, in order. Sticky so they stay put
            while a long channel list scrolls. Trader-only: a subscriber's
            channels are the trader's. */}
        {!isSub && (
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
            <p className="text-sm mt-1" style={{ color: "var(--muted)" }}>{pairFor.label}</p>

            <ol className="text-sm mt-5 space-y-2 list-decimal pl-5 text-left" style={{ color: "var(--text-2)" }}>
              <li>
                Open the <strong>Kopyya Connector</strong> app on your computer.
                <div className="mt-2 flex flex-wrap gap-2">
                  <a href={CONNECTOR_WINDOWS} target="_blank" rel="noopener noreferrer" className="btn-primary px-3 py-1.5 text-xs">
                    Download for Windows
                  </a>
                  <a href={CONNECTOR_MAC} target="_blank" rel="noopener noreferrer" className="btn-ghost px-3 py-1.5 text-xs">
                    Download for Mac
                  </a>
                </div>
                <p className="text-[11px] mt-1.5" style={{ color: "var(--muted)" }}>
                  Needs Google Chrome. If Windows says &ldquo;Windows protected your PC&rdquo;, click
                  More info &rarr; Run anyway. On a Mac, right-click the app and choose Open.
                </p>
              </li>
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
              You sign in on your own computer, in your own browser. Kopyya never sees your
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
              You sign in on your own device. Kopyya never sees your Discord password or
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
        className="inline-flex items-center gap-1.5 text-sm font-semibold"
        style={{ color: "var(--text)" }}
      >
        <span style={{ color: "var(--accent-2)" }}>{icon}</span>
        {title}
      </span>
    </div>
  );
}


/** Radio glyph used by the Alert handling choices. Named RadioDot to
 *  avoid colliding with lucide's Radio icon, used in the intro card. */
function RadioDot({ active, tone }: { active: boolean; tone?: string }) {
  const color = tone ?? "var(--accent-2)";
  return (
    <span
      className="inline-flex items-center justify-center rounded-full shrink-0"
      style={{
        width: 14,
        height: 14,
        border: `1px solid ${active ? color : "var(--border-strong)"}`,
        background: active ? color : "transparent",
      }}
    >
      {active && (
        <span
          className="inline-block rounded-full"
          style={{ width: 5, height: 5, background: "var(--accent-ink)" }}
        />
      )}
    </span>
  );
}
