/* messageText() against the DOM Discord actually renders.
 *
 * The case that matters is a REPLY: traders post an exit as a reply to their
 * own entry, so the quoted entry sits inside the same message element. Left in,
 * the parser reads the entry first and places a duplicate BUY while the exit is
 * lost — silently, because an order does appear.
 */
const assert = require("assert");
const fs = require("fs");
const { JSDOM } = require("jsdom");

const dom = new JSDOM("<!doctype html><body></body>");
global.window = dom.window;
global.document = dom.window.document;

/* innerText is a layout property jsdom doesn't compute; textContent is what the
 * production helper falls back to, so route to it. */
Object.defineProperty(dom.window.HTMLElement.prototype, "innerText", {
  get() { return this.textContent; },
});

/* observer.js is one arrow function Playwright calls with a config, so its
 * helpers aren't exported. Lift just messageText out by brace-matching — the
 * alternative is duplicating it here, which would let the copy drift from the
 * real one and quietly stop testing anything. */
const src = fs.readFileSync(
  require("path").join(__dirname, "../discord_listener/observer.js"), "utf8"
);
const start = src.indexOf("function messageText");
assert.ok(start !== -1, "messageText not found in observer.js");
let depth = 0, end = start;
for (let i = src.indexOf("{", start); i < src.length; i++) {
  if (src[i] === "{") depth++;
  else if (src[i] === "}" && --depth === 0) { end = i + 1; break; }
}
const messageText = new Function(`${src.slice(start, end)}; return messageText;`)();

function li(html) {
  const el = document.createElement("li");
  el.innerHTML = html;
  return el;
}

// ── a plain message is untouched ────────────────────────────────────────────
assert.strictEqual(
  messageText(li(`<div>$SPY 762 CALL 0DTE @0.87</div>`)),
  "$SPY 762 CALL 0DTE @0.87"
);

// ── emoji come back from their alt text ─────────────────────────────────────
assert.strictEqual(
  messageText(li(`<div><img alt="✂️"> $SPY 762c +20%</div>`)),
  "✂️ $SPY 762c +20%"
);

// ── a reply drops the quoted entry, keeping only what was typed ─────────────
for (const quoted of [
  `<div id="message-reply-context-123">Clint $SPY 762 CALL 0DTE @0.87</div>`,
  `<div class="repliedMessage_abc123">Clint $SPY 762 CALL 0DTE @0.87</div>`,
]) {
  const got = messageText(li(`${quoted}<div><img alt="✂️"> $SPY 762c +20%</div>`));
  assert.strictEqual(got, "✂️ $SPY 762c +20%", `reply not stripped: ${got}`);
  assert.ok(!got.includes("0.87"), "the quoted entry leaked into the exit");
}

// ── timestamps stay out ─────────────────────────────────────────────────────
assert.strictEqual(
  messageText(li(`<time>7:44 PM</time><div>$SPY 761 CALL 0DTE @1.05</div>`)),
  "$SPY 761 CALL 0DTE @1.05"
);

console.log("observer messageText: 5 assertions passed");
