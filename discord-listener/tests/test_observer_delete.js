/* Deletion detection against a simulated Discord message list.
 *
 * A removed message node is a deletion only when it looks like nothing else:
 * Discord also removes nodes when it trims the oldest from the top of its
 * virtualised scroller, and every node on a re-render or channel switch. Those
 * must never be reported — the backend cancels the entry a deleted alert placed.
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const CH = "555";
const dom = new JSDOM(`<!doctype html><body><ol id="list"></ol></body>`, { url: `https://discord.com/channels/1/${CH}`, runScripts: "outside-only" });
const { window } = dom;
global.window = window;
global.document = window.document;
Object.defineProperty(window.HTMLElement.prototype, "innerText", { get() { return this.textContent; } });

const list = document.getElementById("list");
function msg(id, text) {
  const li = document.createElement("li");
  li.id = `chat-messages-${CH}-${id}`;
  li.innerHTML = `<div id="message-content-${id}">${text}</div>`;
  return li;
}
for (const id of ["101", "102", "103", "104"]) list.appendChild(msg(id, `alert ${id}`));

const batches = [];
window.__kopyaaEmit = (payload) => batches.push(...JSON.parse(payload));
const observer = window.eval(fs.readFileSync(path.join(__dirname, "../discord_listener/observer.js"), "utf8"));
observer({ channelId: CH, lastSeenMessageId: "104", flushMs: 10 });

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const deletes = () => batches.filter((m) => m.is_delete).map((m) => m.message_id);
const node = (id) => document.getElementById(`chat-messages-${CH}-${id}`);

(async () => {
  // A message in the middle deleted: reported.
  node("102").remove();
  await wait(1700);
  assert.deepStrictEqual(deletes(), ["102"]);

  // The NEWEST message deleted — the commonest case (post, then delete): reported.
  node("104").remove();
  await wait(1700);
  assert.deepStrictEqual(deletes(), ["102", "104"]);

  // The topmost trimmed by virtualisation: nothing above it, not a deletion.
  node("101").remove();
  await wait(1700);
  assert.deepStrictEqual(deletes(), ["102", "104"]);

  // A re-render that removes and puts back the same node: not a deletion.
  for (const id of ["105", "106", "107"]) list.appendChild(msg(id, `alert ${id}`));
  await wait(50);
  const n106 = node("106");
  n106.remove();
  await wait(100);
  list.insertBefore(msg("106", "alert 106"), node("107"));
  await wait(1700);
  assert.deepStrictEqual(deletes(), ["102", "104"]);

  // The whole list swapped out (channel switch / reload): nothing reported.
  list.replaceChildren();
  await wait(1700);
  assert.deepStrictEqual(deletes(), ["102", "104"]);

  // A deletion carries no content and is reported once.
  const d = batches.find((m) => m.is_delete);
  assert.strictEqual(d.content, "");
  assert.strictEqual(d.channel_id, CH);
  assert.strictEqual(batches.filter((m) => m.is_delete && m.message_id === "102").length, 1);

  window.__kopyaaObserver.disconnect();
  console.log("observer delete tests passed");
})().catch((e) => { console.error(e); process.exit(1); });
