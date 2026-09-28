"use client";

/**
 * Configurable trim-ladder SIMULATION — self-contained, isolated tool page.
 *
 * Changes nothing in production. It mirrors the Discord exit-ladder behaviour
 * in the browser so a tester can define ladders PER TICKER and PER TRANSACTION
 * SIZE (e.g. AAPL vs everything else, split at a $ threshold), set a position's
 * ticker/entry/qty, and watch the resolved ladder trim the position in portions
 * while fixed/trailing stops manage the remainder.
 *
 * NOTE: the live backend currently uses ONE GLOBAL ladder per trader — per-ticker
 * / per-size ladders are modelled here for design only. The read-only "Import my
 * live settings" button pulls that single global ladder into the selected rule.
 *
 * Public route (outside the (app) auth group). The only network call is the
 * read-only GET /api/discord-sources/settings. Nothing here writes to the server.
 */

import { useCallback, useEffect, useRef, useState } from "react";

// ─────────────────────────── types ───────────────────────────
type Stage = {
  target: number; // profit target %, fires the stage
  sell: number; // % of the REMAINING position to sell
  stopOn: boolean;
  stopPct: number; // fixed stop, % vs entry (+ locks profit, 0 = break-even)
  trailOn: boolean;
  trailPct: number; // trailing stop, % below the highest price since it armed
};
type Ladder = { initOn: boolean; initPct: number; stages: Stage[] };
type Bucket = "under" | "over";
type Rule = { id: string; ticker: string; bucket: Bucket; ladder: Ladder };
type Position = { ticker: string; entry: number; qty: number };

type TrimRow = { i: number; target: number; sold: number; px: number; left: number; stop: number | null; trail: number | null };
type LogRow = { t: number; cls: string; m: string };
type Eng = {
  L: Ladder;
  ticker: string;
  dur: number;
  t: number;
  series: number[];
  entry: number;
  qty: number;
  remaining: number;
  high: number;
  cur: number;
  realized: number;
  closed: boolean;
  fixed: { on: boolean; price: number | null };
  trail: { on: boolean; pct: number };
  lastTrailLog: number;
  price: number;
  priceHist: number[];
  highHist: number[];
  fixedHist: (number | null)[];
  trailHist: (number | null)[];
  logs: LogRow[];
  trims: TrimRow[];
};

// ─────────────────────────── example config (editable, per screenshot) ─────────
const uid = () => Math.random().toString(36).slice(2, 9);
const mkStage = (target: number, sell: number, stopOn: boolean, stopPct: number): Stage => ({
  target,
  sell,
  stopOn,
  stopPct,
  trailOn: false,
  trailPct: 5,
});
const EXAMPLE_RULES = (): Rule[] => [
  { id: uid(), ticker: "AAPL", bucket: "under", ladder: { initOn: true, initPct: -10, stages: [mkStage(20, 50, true, 0), mkStage(40, 50, true, 10), mkStage(60, 100, false, 0)] } },
  { id: uid(), ticker: "AAPL", bucket: "over", ladder: { initOn: true, initPct: -10, stages: [mkStage(20, 30, true, 0), mkStage(50, 50, true, 20), mkStage(80, 100, false, 0)] } },
  { id: uid(), ticker: "ALL", bucket: "under", ladder: { initOn: false, initPct: -10, stages: [mkStage(15, 50, true, 0), mkStage(30, 50, true, 10), mkStage(50, 100, false, 0)] } },
  { id: uid(), ticker: "ALL", bucket: "over", ladder: { initOn: true, initPct: -15, stages: [mkStage(25, 20, true, 0), mkStage(50, 50, true, 15), mkStage(75, 100, false, 0)] } },
];

type Scenario = { label: string; dur: number; path: [number, number][] };
const SCENARIOS: Record<string, Scenario> = {
  climb_hold: { label: "Climb to +90%, hold", dur: 200, path: [[0, 0], [150, 90], [200, 86]] },
  climb_reverse: { label: "Climb +55%, reverse to +5%", dur: 200, path: [[0, 0], [95, 58], [135, 42], [200, 5]] },
  climb_crash: { label: "Climb +38% then crash to −18%", dur: 200, path: [[0, 0], [70, 40], [110, 26], [150, -15], [200, -18]] },
  drop: { label: "Straight drop −22% (initial-stop test)", dur: 140, path: [[0, 0], [100, -22], [140, -24]] },
  grind: { label: "Grind to +28%, chop", dur: 200, path: [[0, 0], [110, 30], [150, 19], [200, 26]] },
};

const TOKEN_KEY = "trading-app:access";
const f2 = (n: number) => n.toFixed(2);
const f0 = (n: number) => Math.round(n);

// ─────────────────────────── rule resolution ───────────────────────────
function resolveRule(rules: Rule[], ticker: string, size: number, threshold: number): { rule: Rule | null; via: "exact" | "all" | null; bucket: Bucket } {
  const bucket: Bucket = size < threshold ? "under" : "over";
  const tk = ticker.trim().toUpperCase();
  const exact = rules.find((r) => r.ticker.trim().toUpperCase() === tk && r.bucket === bucket);
  if (exact) return { rule: exact, via: "exact", bucket };
  const all = rules.find((r) => r.ticker.trim().toUpperCase() === "ALL" && r.bucket === bucket);
  if (all) return { rule: all, via: "all", bucket };
  return { rule: null, via: null, bucket };
}

// ─────────────────────────── engine ───────────────────────────
function makeSeries(path: [number, number][], dur: number, entry: number): number[] {
  const out: number[] = [];
  let seed = entry * 97;
  const rnd = () => {
    seed = (seed * 9301 + 49297) % 233280;
    return seed / 233280 - 0.5;
  };
  for (let t = 0; t <= dur; t++) {
    let a = path[0];
    let b = path[path.length - 1];
    for (let i = 0; i < path.length - 1; i++) {
      if (t >= path[i][0] && t <= path[i + 1][0]) {
        a = path[i];
        b = path[i + 1];
        break;
      }
    }
    const f = b[0] === a[0] ? 0 : (t - a[0]) / (b[0] - a[0]);
    const gain = a[1] + (b[1] - a[1]) * f;
    out.push(Math.max(0.01, entry * (1 + gain / 100) * (1 + rnd() * 0.003)));
  }
  return out;
}

function initEngine(ladder: Ladder, ticker: string, entry: number, qty: number, sc: Scenario): Eng {
  const eng: Eng = {
    L: JSON.parse(JSON.stringify(ladder)),
    ticker,
    dur: sc.dur,
    t: 0,
    series: makeSeries(sc.path, sc.dur, entry),
    entry,
    qty,
    remaining: qty,
    high: entry,
    cur: 0,
    realized: 0,
    closed: false,
    fixed: { on: false, price: null },
    trail: { on: false, pct: 0 },
    lastTrailLog: 0,
    price: entry,
    priceHist: [entry],
    highHist: [entry],
    fixedHist: [null],
    trailHist: [null],
    logs: [],
    trims: [],
  };
  if (ladder.initOn) eng.fixed = { on: true, price: +(entry * (1 + ladder.initPct / 100)).toFixed(4) };
  eng.logs.push({
    t: 0,
    cls: "buy",
    m: `ENTRY — <b>${ticker || "?"}</b> — ${qty} @ ${f2(entry)} — ${ladder.initOn ? `STOP ${f2(eng.fixed.price as number)} (${ladder.initPct >= 0 ? "+" : ""}${ladder.initPct}%)` : "NO STOP"}`,
  });
  return eng;
}

function sellRemaining(s: Eng, q: number, px: number, why: string, cls: string) {
  q = Math.min(q, s.remaining);
  if (q <= 0) return;
  s.remaining -= q;
  s.realized += (px - s.entry) * q;
  s.logs.push({ t: s.t, cls, m: `${why} — SELL <b>${q}</b> @ ${f2(px)} — ${s.remaining} LEFT` });
  if (s.remaining <= 0) s.closed = true;
}

function fireStage(s: Eng, stg: Stage, idx: number, px: number) {
  s.logs.push({ t: s.t, cls: "tgt", m: `TARGET ${idx + 1} REACHED — +${stg.target}%` });
  let q = stg.sell >= 100 ? s.remaining : Math.round((s.remaining * stg.sell) / 100);
  if (q < 1 && stg.sell > 0 && s.remaining > 0) q = 1;
  s.remaining -= q;
  s.realized += (px - s.entry) * q;
  s.logs.push({ t: s.t, cls: "trim", m: `TRIM ${idx + 1} — SOLD <b>${q}</b> @ ${f2(px)} — ${s.remaining} REMAINING` });
  let stopSet: number | null = null;
  if (stg.stopOn) {
    const sp = +(s.entry * (1 + stg.stopPct / 100)).toFixed(4);
    if (sp < px) {
      s.fixed = { on: true, price: sp };
      stopSet = sp;
      s.logs.push({ t: s.t, cls: "st", m: `STOP SET ${f2(sp)} (${stg.stopPct >= 0 ? "+" : ""}${stg.stopPct}% vs entry) on ${s.remaining}` });
    } else {
      s.logs.push({ t: s.t, cls: "st", m: `stop +${stg.stopPct}% is above price — not armed` });
    }
  } else {
    s.fixed = { on: false, price: null };
  }
  if (stg.trailOn && s.remaining > 0) {
    s.trail = { on: true, pct: stg.trailPct };
    s.high = Math.max(s.high, px);
    s.logs.push({ t: s.t, cls: "tr", m: `TRAILING ${stg.trailPct}% ACTIVATED (from high ${f2(s.high)})` });
  } else if (!stg.trailOn) {
    s.trail = { on: false, pct: 0 };
  }
  s.trims.push({ i: idx + 1, target: stg.target, sold: q, px, left: s.remaining, stop: stopSet, trail: stg.trailOn ? stg.trailPct : null });
  s.cur++;
  if (s.remaining <= 0) s.closed = true;
}

function stepEngine(s: Eng): boolean {
  const px = s.series[s.t];
  s.price = px;
  if (px > s.high) s.high = px;
  if (!s.closed) {
    while (s.cur < s.L.stages.length && s.remaining > 0) {
      const stg = s.L.stages[s.cur];
      const gain = ((px - s.entry) / s.entry) * 100;
      if (gain >= stg.target) fireStage(s, stg, s.cur, px);
      else break;
    }
    if (s.remaining > 0) {
      const stops: { v: number; kind: string }[] = [];
      if (s.fixed.on && s.fixed.price != null) stops.push({ v: s.fixed.price, kind: "FIXED STOP" });
      if (s.trail.on) stops.push({ v: +(s.high * (1 - s.trail.pct / 100)).toFixed(4), kind: "TRAILING STOP" });
      if (stops.length) {
        const bind = stops.reduce((a, b) => (b.v > a.v ? b : a));
        if (px <= bind.v) sellRemaining(s, s.remaining, px, `${bind.kind} BREACH @ ${f2(px)}`, "brk");
      }
      if (!s.closed && s.trail.on) {
        const ts = s.high * (1 - s.trail.pct / 100);
        if (ts - s.lastTrailLog > s.entry * 0.03) {
          s.lastTrailLog = ts;
          s.logs.push({ t: s.t, cls: "tr", m: `NEW HIGH ${f2(s.high)} — TRAIL ↑ ${f2(ts)}` });
        }
      }
    }
  }
  s.priceHist.push(px);
  s.highHist.push(s.high);
  s.fixedHist.push(s.closed ? null : s.fixed.on ? s.fixed.price : null);
  s.trailHist.push(s.closed || !s.trail.on ? null : +(s.high * (1 - s.trail.pct / 100)).toFixed(4));
  s.t++;
  if (s.t > s.dur) {
    s.logs.push({ t: s.t - 1, cls: "", m: s.remaining <= 0 ? "— position flat —" : `— ${s.remaining} still held at end —` });
    return true;
  }
  return false;
}

// ─────────────────────────── component ───────────────────────────
export default function SimulationPage() {
  const [rules, setRules] = useState<Rule[]>(EXAMPLE_RULES);
  const [threshold, setThreshold] = useState<number>(500);
  const [pos, setPos] = useState<Position>({ ticker: "AAPL", entry: 4.2, qty: 100 });
  const [scenarioKey, setScenarioKey] = useState<string>("climb_reverse");
  const [speed, setSpeed] = useState<number>(4);
  const [running, setRunning] = useState<boolean>(false);
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const [, setFrame] = useState<number>(0);

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const engRef = useRef<Eng | null>(null);
  const timerRef = useRef<number | null>(null);
  const speedRef = useRef<number>(speed);
  speedRef.current = speed;

  const bump = () => setFrame((f) => f + 1);

  const size = pos.entry * pos.qty;
  const resolved = resolveRule(rules, pos.ticker, size, threshold);

  const draw = useCallback(() => {
    const s = engRef.current;
    const cv = canvasRef.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth || 600;
    const H = 290;
    cv.width = W * dpr;
    cv.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!s) return;
    const padL = 48, padR = 10, padT = 12, padB = 20, iw = W - padL - padR, ih = H - padT - padB;
    let lo = Infinity, hi = -Infinity;
    const push = (v: number | null) => {
      if (v != null) {
        lo = Math.min(lo, v);
        hi = Math.max(hi, v);
      }
    };
    s.priceHist.forEach(push);
    s.fixedHist.forEach(push);
    s.trailHist.forEach(push);
    s.L.stages.forEach((st) => push(s.entry * (1 + st.target / 100)));
    push(s.entry);
    if (!isFinite(lo)) {
      lo = s.entry * 0.9;
      hi = s.entry * 1.1;
    }
    const pad = (hi - lo) * 0.1 || 1;
    lo -= pad;
    hi += pad;
    const X = (t: number) => padL + (iw * t) / s.dur;
    const Y = (v: number) => padT + (ih * (hi - v)) / (hi - lo);
    const C = (name: string) => COLORS[name];
    ctx.font = "10px ui-monospace, monospace";
    ctx.lineWidth = 1;
    ctx.strokeStyle = C("line");
    ctx.fillStyle = C("faint");
    for (let g = 0; g <= 4; g++) {
      const v = lo + ((hi - lo) * g) / 4;
      const y = Y(v);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
      ctx.stroke();
      ctx.textAlign = "right";
      ctx.fillText(v.toFixed(2), padL - 6, y + 3);
    }
    s.L.stages.forEach((st, i) => {
      const y = Y(s.entry * (1 + st.target / 100));
      ctx.strokeStyle = C("neutral");
      ctx.setLineDash([2, 4]);
      ctx.globalAlpha = i < s.cur ? 0.45 : 0.9;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.setLineDash([]);
      ctx.fillStyle = C("faint");
      ctx.textAlign = "left";
      ctx.fillText("T" + (i + 1) + " +" + st.target + "%", padL + 2, y - 3);
    });
    ctx.strokeStyle = C("neutral");
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(padL, Y(s.entry));
    ctx.lineTo(W - padR, Y(s.entry));
    ctx.stroke();
    ctx.setLineDash([]);
    const line = (arr: (number | null)[], color: string, w: number, dash?: number[]) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = w;
      ctx.setLineDash(dash || []);
      ctx.beginPath();
      let started = false;
      for (let t = 0; t < arr.length; t++) {
        const v = arr[t];
        if (v == null) {
          started = false;
          continue;
        }
        const x = X(t), y = Y(v);
        if (started) ctx.lineTo(x, y);
        else ctx.moveTo(x, y);
        started = true;
      }
      ctx.stroke();
      ctx.setLineDash([]);
    };
    line(s.highHist, C("high"), 1, [1, 3]);
    line(s.fixedHist, C("stop"), 1.4, [4, 4]);
    line(s.trailHist, C("trail"), 1.4, [4, 4]);
    line(s.priceHist, C("accent"), 2);
    const n = s.priceHist.length - 1;
    ctx.fillStyle = s.closed ? C("down") : C("accent");
    ctx.beginPath();
    ctx.arc(X(n), Y(s.price), s.closed ? 4 : 3, 0, 7);
    ctx.fill();
  }, []);

  const stopTimer = () => {
    if (timerRef.current != null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const reset = useCallback(() => {
    stopTimer();
    setRunning(false);
    const r = resolveRule(rules, pos.ticker, pos.entry * pos.qty, threshold);
    engRef.current = r.rule ? initEngine(r.rule.ladder, pos.ticker, pos.entry, pos.qty, SCENARIOS[scenarioKey]) : null;
    draw();
    bump();
  }, [rules, pos, threshold, scenarioKey, draw]);

  useEffect(() => {
    reset();
    return stopTimer;
  }, [reset]);

  const loop = useCallback(() => {
    const s = engRef.current;
    if (!s) return;
    const done = stepEngine(s);
    draw();
    bump();
    if (done) {
      setRunning(false);
      stopTimer();
      return;
    }
    timerRef.current = window.setTimeout(loop, 1000 / speedRef.current);
  }, [draw]);

  const run = () => {
    const s = engRef.current;
    if (!s) return;
    if (s.t > s.dur || s.closed) reset();
    setRunning(true);
    timerRef.current = window.setTimeout(loop, 1000 / speedRef.current);
  };

  // ── editing helpers ──
  const setRuleMeta = (id: string, patch: Partial<Pick<Rule, "ticker" | "bucket">>) =>
    setRules((rs) => rs.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  const setRuleLadder = (id: string, patch: Partial<Ladder>) =>
    setRules((rs) => rs.map((r) => (r.id === id ? { ...r, ladder: { ...r.ladder, ...patch } } : r)));
  const setRuleStage = (id: string, i: number, patch: Partial<Stage>) =>
    setRules((rs) => rs.map((r) => (r.id === id ? { ...r, ladder: { ...r.ladder, stages: r.ladder.stages.map((s, j) => (j === i ? { ...s, ...patch } : s)) } } : r)));
  const addRuleStage = (id: string) =>
    setRules((rs) =>
      rs.map((r) => {
        if (r.id !== id) return r;
        const last = r.ladder.stages[r.ladder.stages.length - 1];
        return { ...r, ladder: { ...r.ladder, stages: [...r.ladder.stages, mkStage(last ? last.target + 20 : 20, 50, false, 0)] } };
      }),
    );
  const removeRuleStage = (id: string, i: number) =>
    setRules((rs) => rs.map((r) => (r.id === id ? { ...r, ladder: { ...r.ladder, stages: r.ladder.stages.filter((_, j) => j !== i) } } : r)));
  const addRule = () => setRules((rs) => [...rs, { id: uid(), ticker: "ALL", bucket: "under", ladder: { initOn: false, initPct: -10, stages: [mkStage(20, 50, true, 0)] } }]);
  const removeRule = (id: string) => setRules((rs) => rs.filter((r) => r.id !== id));
  const resetExample = () => setRules(EXAMPLE_RULES());

  const importSettings = async () => {
    if (!resolved.rule) {
      setImportMsg("No rule resolves for this ticker/size — add one first.");
      return;
    }
    const targetId = resolved.rule.id;
    setImportMsg("Loading your live settings…");
    try {
      const token = typeof window !== "undefined" ? window.localStorage.getItem(TOKEN_KEY) : null;
      const res = await fetch("/api/discord-sources/settings", { headers: token ? { Authorization: `Bearer ${token}` } : {} });
      if (!res.ok) {
        setImportMsg(res.status === 401 || res.status === 403 ? "Log in as the trader first — live settings need authentication." : `Couldn't load settings (HTTP ${res.status}).`);
        return;
      }
      const s = await res.json();
      const num = (v: unknown, d: number) => {
        const n = parseFloat(String(v));
        return isFinite(n) ? n : d;
      };
      const stages: Stage[] = [
        mkStage(num(s.trim_profit_gate_pct, 20), 50, true, num(s.trim_stop_pct, 25)),
        mkStage(num(s.trim2_profit_gate_pct, 0), 50, true, num(s.trim2_stop_pct, 0)),
        mkStage(num(s.trim3_profit_gate_pct, 0), 100, false, num(s.trim3_stop_pct, 0)),
      ];
      setRules((rs) => rs.map((r) => (r.id === targetId ? { ...r, ladder: { initOn: false, initPct: -10, stages } } : r)));
      setImportMsg(`Imported the live global ladder into rule ${resolved.rule.ticker}/${resolved.bucket === "under" ? "<" : "≥"}$${threshold}. Sell % set 50/50/100; live trailing was a $${s.trim_trail_amount ?? "?"} give-back — set % here to model it.`);
    } catch {
      setImportMsg("Import failed — are you logged in on this domain?");
    }
  };

  // ── readouts ──
  const s = engRef.current;
  const gain = s ? ((s.price - s.entry) / s.entry) * 100 : 0;
  const stats: [string, string, string][] = s
    ? [
        ["Ticker", s.ticker || "?", "sm"],
        ["Price", f2(s.price), s.price >= s.entry ? "pos" : "neg"],
        ["Profit", (gain >= 0 ? "+" : "") + gain.toFixed(1) + "%", gain >= 0 ? "pos" : "neg"],
        ["Remaining", `${s.remaining} / ${s.qty}`, ""],
        ["Highest", f2(s.high), ""],
        ["Realized", (s.realized >= 0 ? "+" : "") + "$" + f0(s.realized), s.realized >= 0 ? "pos" : "neg"],
        ["Stage", s.closed ? "done" : s.cur >= s.L.stages.length ? "all fired" : "next T" + (s.cur + 1), "sm"],
        ["Fixed stop", s.closed ? "—" : s.fixed.on && s.fixed.price != null ? f2(s.fixed.price) : "OFF", "sm"],
        ["Trailing", s.closed ? "—" : s.trail.on ? f2(s.high * (1 - s.trail.pct / 100)) : "OFF", "sm"],
      ]
    : [];

  const stageEditor = (r: Rule) => (
    <div className="stages">
      {r.ladder.stages.map((st, i) => (
        <div className="stagerow" key={i}>
          <span className="sidx">S{i + 1}</span>
          <label>target +%<input type="number" step="1" value={st.target} onChange={(e) => setRuleStage(r.id, i, { target: parseFloat(e.target.value) || 0 })} /></label>
          <label>sell %<input type="number" step="5" min="0" max="100" value={st.sell} onChange={(e) => setRuleStage(r.id, i, { sell: parseFloat(e.target.value) || 0 })} /></label>
          <label className="chk"><input type="checkbox" checked={st.stopOn} onChange={(e) => setRuleStage(r.id, i, { stopOn: e.target.checked })} />stop</label>
          <label>@%<input type="number" step="5" value={st.stopPct} disabled={!st.stopOn} onChange={(e) => setRuleStage(r.id, i, { stopPct: parseFloat(e.target.value) || 0 })} /></label>
          <label className="chk"><input type="checkbox" checked={st.trailOn} onChange={(e) => setRuleStage(r.id, i, { trailOn: e.target.checked })} />trail</label>
          <label>%<input type="number" step="1" min="0" value={st.trailPct} disabled={!st.trailOn} onChange={(e) => setRuleStage(r.id, i, { trailPct: parseFloat(e.target.value) || 0 })} /></label>
          <button className="rm" title="remove stage" onClick={() => removeRuleStage(r.id, i)}>✕</button>
        </div>
      ))}
      <button className="btn tiny" onClick={() => addRuleStage(r.id)}>+ stage</button>
    </div>
  );

  return (
    <div className="simx">
      <style dangerouslySetInnerHTML={{ __html: CSS }} />
      <div className="wrap">
        <header>
          <div className="eyebrow">Kopyya · Internal Tool</div>
          <h1>Trim-Ladder Simulation <span className="tag-demo">SIMULATION</span></h1>
          <p>
            Define ladders <b>per ticker</b> and <b>per transaction size</b>, set a position, and watch the resolved ladder trim it in
            portions while fixed/trailing stops manage the remainder. It changes nothing in the live system.{" "}
            <b>Note:</b> the live backend uses one global ladder today — per-ticker/size ladders are modelled here for design.
          </p>
        </header>

        <div className="cfgwrap">
          <div className="panel">
            <h3>Position <span className="tag">what to simulate</span></h3>
            <div className="posrow">
              <div className="f"><label>Ticker</label><input className="txt" type="text" value={pos.ticker} onChange={(e) => setPos({ ...pos, ticker: e.target.value })} /></div>
              <div className="f"><label>Entry price</label><input type="number" step="0.1" min="0.01" value={pos.entry} onChange={(e) => setPos({ ...pos, entry: parseFloat(e.target.value) || 0.01 })} /></div>
              <div className="f"><label>Quantity</label><input type="number" step="1" min="1" value={pos.qty} onChange={(e) => setPos({ ...pos, qty: Math.max(1, parseInt(e.target.value) || 1) })} /></div>
              <div className="f"><label>Size threshold $</label><input type="number" step="50" min="0" value={threshold} onChange={(e) => setThreshold(parseFloat(e.target.value) || 0)} /></div>
              <div className="divider" />
              <div className="resolved">
                <div className="rsize">size = entry × qty = <b>${f0(size)}</b> → {resolved.bucket === "under" ? `< $${threshold}` : `≥ $${threshold}`}</div>
                {resolved.rule ? (
                  <div className="rbadge ok">
                    ▶ ladder: <b>{resolved.rule.ticker}</b> / {resolved.bucket === "under" ? `<$${threshold}` : `≥$${threshold}`}
                    {resolved.via === "all" && <span className="via"> (via ALL — no {pos.ticker.toUpperCase()} rule)</span>}
                  </div>
                ) : (
                  <div className="rbadge bad">✗ no rule matches {pos.ticker.toUpperCase()} / {resolved.bucket} — add one</div>
                )}
              </div>
              <button className="btn primary block" onClick={importSettings}>⭳ Import live settings → this rule</button>
              {importMsg && <div className="importmsg">{importMsg}</div>}
            </div>
          </div>

          <div className="panel">
            <h3>Ladder rules <span className="tag">per ticker · per size — add / remove</span></h3>
            <div className="rules">
              {rules.map((r) => {
                const active = resolved.rule?.id === r.id;
                return (
                  <div className={"rule" + (active ? " active" : "")} key={r.id}>
                    <div className="rulehead">
                      {active && <span className="live">● active</span>}
                      <label className="rl">ticker<input className="txt sm" type="text" value={r.ticker} onChange={(e) => setRuleMeta(r.id, { ticker: e.target.value })} /></label>
                      <label className="rl">size<select value={r.bucket} onChange={(e) => setRuleMeta(r.id, { bucket: e.target.value as Bucket })}><option value="under">&lt; threshold</option><option value="over">≥ threshold</option></select></label>
                      <label className="rl chk2"><input type="checkbox" checked={r.ladder.initOn} onChange={(e) => setRuleLadder(r.id, { initOn: e.target.checked })} />init stop</label>
                      <label className="rl">@%<input type="number" step="1" value={r.ladder.initPct} disabled={!r.ladder.initOn} onChange={(e) => setRuleLadder(r.id, { initPct: parseFloat(e.target.value) || 0 })} /></label>
                      <button className="rm big" title="remove rule" onClick={() => removeRule(r.id)}>✕</button>
                    </div>
                    {stageEditor(r)}
                  </div>
                );
              })}
            </div>
            <button className="btn tiny" onClick={addRule}>+ Add rule</button>
            <button className="btn tiny" style={{ marginLeft: 6 }} onClick={resetExample}>↺ example (from the table)</button>
            <div className="note">A position resolves to the rule matching its <b>ticker</b> + <b>size bucket</b>; if no exact ticker rule exists it falls back to <b>ALL</b>. Each stage fires at its <b>target %</b>, sells that <b>% of remaining</b>, then sets an optional fixed <b>stop</b> and/or <b>trailing</b> stop. 100% sell exits fully.</div>
          </div>
        </div>

        <div className="controls">
          <div><label className="cl">Price path</label><select value={scenarioKey} onChange={(e) => setScenarioKey(e.target.value)}>{Object.entries(SCENARIOS).map(([k, v]) => (<option key={k} value={k}>{v.label}</option>))}</select></div>
          <div><label className="cl">Speed</label><div className="seg">{[1, 4, 20].map((sp) => (<button key={sp} className={speed === sp ? "on" : ""} onClick={() => setSpeed(sp)}>{sp}×</button>))}</div></div>
          <div><label className="cl">&nbsp;</label><button className="btn primary" onClick={run} disabled={running || !resolved.rule}>{running ? "● Running…" : "▶ Run"}</button></div>
          <div><label className="cl">&nbsp;</label><button className="btn" onClick={reset}>↺ Reset</button></div>
          <div className="clock">t = <b>{s ? s.t : 0}s</b>/{s ? s.dur : 0}s</div>
        </div>

        <div className="stats">
          {stats.length ? (
            stats.map((c, i) => (
              <div className="stat" key={i}><div className="k">{c[0]}</div><div className={"v " + (c[2] === "pos" || c[2] === "neg" ? c[2] : "") + (c[2] === "sm" ? " sm" : "")}>{c[1]}</div></div>
            ))
          ) : (
            <div className="stat" style={{ gridColumn: "1 / -1", color: COLORS.down }}><div className="k">No ladder</div><div className="v sm">Add a rule that matches the position, then Run.</div></div>
          )}
        </div>

        <div className="grid">
          <div className="panel">
            <h3>Price · stops · targets <span className="tag">{s ? s.dur : 0}s · 1s ticks</span></h3>
            <canvas ref={canvasRef} />
            <div className="legend">
              <span><span className="sw" style={{ background: COLORS.accent }} />price</span>
              <span><span className="sw" style={{ background: COLORS.high }} />highest</span>
              <span><span className="sw d" style={{ borderColor: COLORS.stop }} />fixed stop</span>
              <span><span className="sw d" style={{ borderColor: COLORS.trail }} />trailing</span>
              <span><span className="sw d" style={{ borderColor: COLORS.neutral }} />targets</span>
            </div>
          </div>
          <div className="panel">
            <h3>Trim history <span className="tag">what sold, when</span></h3>
            <div className="tblwrap">
              <table>
                <thead><tr><th>Stage</th><th>Target</th><th>Sold</th><th>@</th><th>Left</th><th>Stop</th><th>Trail</th></tr></thead>
                <tbody>
                  {s && s.trims.length ? (
                    s.trims.map((r, i) => (
                      <tr key={i}><td>T{r.i}</td><td className="m">+{r.target}%</td><td className="m">{r.sold}</td><td className="m">{f2(r.px)}</td><td className="m">{r.left}</td><td className="m" style={{ color: COLORS.stop }}>{r.stop != null ? f2(r.stop) : "—"}</td><td className="m" style={{ color: COLORS.trail }}>{r.trail != null ? r.trail + "%" : "—"}</td></tr>
                    ))
                  ) : (
                    <tr><td colSpan={7} className="empty">no trims yet</td></tr>
                  )}
                </tbody>
              </table>
            </div>
            <h3 style={{ marginTop: 14 }}>Event log</h3>
            <div className="log">
              {s ? s.logs.slice(-100).map((e, i) => (<div className="row" key={i}><span className="lt">{String(e.t).padStart(3, "0")}s</span><span className={"lm " + (e.cls || "")} dangerouslySetInnerHTML={{ __html: e.m }} /></div>)) : null}
            </div>
          </div>
        </div>

        <p className="foot"><b>Simulation only.</b> Mirrors the exit-ladder logic in the browser to test per-ticker / per-size configurations — it reads and writes no live position and changes no production behaviour. The only server call is the read-only <b>Import live settings</b> button (the live backend has one global ladder). Trailing never moves down; a stop closes only the remaining quantity, never what was already trimmed.</p>
      </div>
    </div>
  );
}

// ─────────────────────────── palette + styles (self-contained, dark) ───────────────────────────
const COLORS: Record<string, string> = {
  accent: "#2dd4bf", up: "#34d399", down: "#f87171", stop: "#fbbf24", trail: "#a78bfa",
  high: "#38bdf8", neutral: "#7c8b98", line: "#222c34", faint: "#657481",
};

const CSS = `
.simx{min-height:100vh;background:#0c1014;color:#e8edf1;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.5}
.simx *{box-sizing:border-box}
.simx .wrap{max-width:1160px;margin:0 auto;padding:26px 18px 70px}
.simx h1,.simx h3{margin:0;font-weight:800;letter-spacing:-.01em}
.simx .eyebrow{font-family:ui-monospace,monospace;font-size:11.5px;letter-spacing:.18em;text-transform:uppercase;color:#2dd4bf;font-weight:600}
.simx h1{font-size:clamp(23px,3.6vw,31px);margin:6px 0 6px}
.simx header p{color:#9aa7b2;font-size:14px;margin:0;max-width:92ch}
.simx b{color:#e8edf1}
.simx .tag-demo{font-family:ui-monospace,monospace;font-size:10.5px;color:#fbbf24;border:1px solid #fbbf24;border-radius:5px;padding:1px 6px;margin-left:6px;vertical-align:middle}
.simx .panel{background:#141a20;border:1px solid #222c34;border-radius:12px;padding:14px 15px}
.simx .panel h3{font-size:13px;font-weight:700;display:flex;align-items:center;gap:8px;margin-bottom:11px}
.simx .panel h3 .tag{font-family:ui-monospace,monospace;font-size:10px;color:#657481;font-weight:500;letter-spacing:.05em;text-transform:uppercase}
.simx .cfgwrap{display:grid;grid-template-columns:310px 1fr;gap:14px;margin:16px 0}
@media(max-width:860px){.simx .cfgwrap{grid-template-columns:1fr}}
.simx label{font-size:12px;color:#9aa7b2}
.simx input[type=number],.simx input.txt{font-family:ui-monospace,monospace;font-size:12px;padding:4px 6px;border-radius:6px;border:1px solid #31404a;background:#0c1014;color:#e8edf1;text-align:right;width:96px}
.simx input.txt{text-align:left}.simx input.txt.sm{width:74px}
.simx input:disabled{opacity:.4}
.simx input[type=checkbox]{accent-color:#2dd4bf;vertical-align:-1px}
.simx select{font-family:inherit;font-size:12px;border-radius:6px;border:1px solid #31404a;background:#0c1014;color:#e8edf1;padding:4px 6px}
.simx .btn{font-family:inherit;font-size:13px;border-radius:8px;border:1px solid #31404a;background:#0f151a;color:#e8edf1;padding:7px 12px;cursor:pointer}
.simx .btn.primary{background:#2dd4bf;color:#04211d;border-color:transparent;font-weight:700}
.simx .btn.tiny{font-size:11px;padding:4px 9px}
.simx .btn.block{width:100%}
.simx .btn:disabled{opacity:.5;cursor:not-allowed}
.simx .posrow{display:flex;flex-direction:column;gap:9px}
.simx .posrow .f{display:flex;justify-content:space-between;align-items:center;gap:8px}
.simx .divider{height:1px;background:#222c34;margin:2px 0}
.simx .resolved{font-size:12px;display:flex;flex-direction:column;gap:6px}
.simx .rsize{color:#9aa7b2;font-family:ui-monospace,monospace;font-size:11.5px}
.simx .rbadge{font-family:ui-monospace,monospace;font-size:12px;padding:6px 9px;border-radius:7px}
.simx .rbadge.ok{background:#0f2e2b;color:#2dd4bf}
.simx .rbadge.bad{background:#2a1413;color:#f87171}
.simx .rbadge .via{color:#657481;font-size:11px}
.simx .importmsg{font-size:11.5px;color:#38bdf8;background:#0f151a;border:1px solid #222c34;border-radius:7px;padding:7px 9px;line-height:1.5}
.simx .rules{display:flex;flex-direction:column;gap:8px}
.simx .rule{border:1px solid #222c34;border-radius:10px;background:#0f151a;padding:9px 10px}
.simx .rule.active{border-color:#2dd4bf;box-shadow:0 0 0 1px #2dd4bf inset}
.simx .rulehead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.simx .rulehead .live{font-family:ui-monospace,monospace;font-size:10px;color:#2dd4bf;font-weight:700}
.simx .rl{display:flex;align-items:center;gap:5px;font-family:ui-monospace,monospace;font-size:10px;letter-spacing:.03em;text-transform:uppercase;color:#657481}
.simx .rl.chk2{text-transform:none;font-size:11px;color:#9aa7b2}
.simx .rl input[type=number]{width:56px}
.simx .rm{border:none;background:none;color:#657481;font-size:13px;cursor:pointer;padding:2px}
.simx .rm:hover{color:#f87171}.simx .rm.big{margin-left:auto;font-size:15px}
.simx .stages{display:flex;flex-direction:column;gap:6px}
.simx .stagerow{display:grid;grid-template-columns:26px 1fr 1fr auto auto auto auto 22px;gap:6px;align-items:center;padding:6px;border:1px solid #222c34;border-radius:8px;background:#141a20}
.simx .stagerow .sidx{font-family:ui-monospace,monospace;font-size:11px;font-weight:700;color:#2dd4bf}
.simx .stagerow label{display:flex;flex-direction:column;gap:2px;font-size:9px;font-family:ui-monospace,monospace;letter-spacing:.03em;text-transform:uppercase;color:#657481}
.simx .stagerow label.chk{flex-direction:row;align-items:center;gap:4px;text-transform:none;font-size:11px;color:#9aa7b2}
.simx .stagerow input[type=number]{width:100%}
.simx .controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:2px 0 14px}
.simx .controls .cl{font-family:ui-monospace,monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#657481;display:block;margin-bottom:4px}
.simx .controls select{font-size:13.5px;padding:8px 12px;min-width:230px}
.simx .seg{display:inline-flex;border:1px solid #31404a;border-radius:8px;overflow:hidden}
.simx .seg button{border:none;border-left:1px solid #31404a;padding:8px 11px;background:#0f151a;color:#e8edf1;cursor:pointer;font-family:inherit;font-size:13px}
.simx .seg button:first-child{border-left:none}
.simx .seg button.on{background:#0f2e2b;color:#2dd4bf;font-weight:700}
.simx .clock{margin-left:auto;font-family:ui-monospace,monospace;font-size:13px;color:#9aa7b2}
.simx .clock b{color:#e8edf1}
.simx .stats{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:#222c34;border:1px solid #222c34;border-radius:11px;overflow:hidden;margin-bottom:14px}
@media(max-width:760px){.simx .stats{grid-template-columns:repeat(3,1fr)}}
.simx .stat{background:#141a20;padding:10px 12px}
.simx .stat .k{font-family:ui-monospace,monospace;font-size:9px;letter-spacing:.06em;text-transform:uppercase;color:#657481}
.simx .stat .v{font-family:ui-monospace,monospace;font-size:17px;font-weight:600;margin-top:3px}
.simx .stat .v.sm{font-size:13px}
.simx .pos{color:#34d399}.simx .neg{color:#f87171}
.simx .grid{display:grid;grid-template-columns:1.5fr 1fr;gap:14px}
@media(max-width:860px){.simx .grid{grid-template-columns:1fr}}
.simx canvas{width:100%;height:290px;display:block}
.simx .legend{display:flex;flex-wrap:wrap;gap:12px;margin-top:8px;font-family:ui-monospace,monospace;font-size:11px;color:#9aa7b2}
.simx .legend span{display:inline-flex;align-items:center;gap:5px}
.simx .sw{width:11px;height:3px;border-radius:2px;display:inline-block}
.simx .sw.d{height:0;border-top:2px dashed}
.simx .tblwrap{overflow-x:auto}
.simx table{width:100%;border-collapse:collapse;font-size:12px}
.simx th,.simx td{text-align:right;padding:5px 7px;border-top:1px solid #222c34}
.simx th:first-child,.simx td:first-child{text-align:left}
.simx thead th{color:#657481;font-family:ui-monospace,monospace;font-size:9px;letter-spacing:.05em;text-transform:uppercase;border-top:none;font-weight:600}
.simx td.m{font-family:ui-monospace,monospace}
.simx td.empty{color:#657481;text-align:center;padding:10px}
.simx .log{height:168px;overflow-y:auto;font-family:ui-monospace,monospace;font-size:11.5px;line-height:1.7}
.simx .log .row{display:flex;gap:9px;padding:1px 0;border-bottom:1px solid #0f151a}
.simx .log .lt{color:#657481;flex:none}
.simx .log .lm{color:#9aa7b2}
.simx .log .lm.buy{color:#38bdf8}.simx .log .lm.tgt{color:#2dd4bf}.simx .log .lm.trim{color:#34d399}.simx .log .lm.st{color:#fbbf24}.simx .log .lm.tr{color:#a78bfa}.simx .log .lm.brk{color:#f87171}
.simx .foot{margin-top:22px;font-size:12px;color:#657481;border-top:1px solid #222c34;padding-top:14px;max-width:92ch}
`;
