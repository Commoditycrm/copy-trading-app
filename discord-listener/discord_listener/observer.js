/*
 * In-page observer for a Discord Web channel.
 *
 * Runs inside the authenticated Discord tab and reports newly rendered messages
 * to the Python side through the exposed `__kopyaaEmit` binding.
 *
 * WHY EVENT-DRIVEN, NOT POLLING
 * -----------------------------
 * Re-reading the whole message list on a timer is both slow and lossy: Discord
 * virtualises the scroller, so a message can be rendered and scrolled out of the
 * DOM between two polls. A MutationObserver fires the moment React commits the
 * node, so we see every message exactly once, at render time, and the callback
 * is a cheap id-prefix check per added node.
 *
 * WHAT IT DOES NOT DO
 * -------------------
 * No network requests, no token access, no interaction with Discord's API, and
 * no writes to the page. It reads rendered text out of a session the user
 * authenticated themselves, which is the same content their eyes can see.
 *
 * BACKLOG HANDLING
 * ----------------
 * Opening a channel renders its recent history all at once. What we do with it
 * depends on whether this channel has ever been read before:
 *
 *   FIRST attach (no lastSeenMessageId) — emit NOTHING. Connecting a channel
 *     means "watch it from now on", not "import its history". The highest
 *     rendered snowflake is reported instead, so it becomes the baseline.
 *
 *   RECONNECT (lastSeenMessageId known) — emit exactly the backlog newer than
 *     it, so alerts posted while the listener was down aren't lost. Overlap is
 *     harmless: the backend de-duplicates on (source_id, message_id).
 */
(config) => {
  const { channelId, lastSeenMessageId, flushMs } = config;

  // Guard against a double-injection (navigation re-fires the init).
  if (window.__kopyaaObserver) {
    window.__kopyaaObserver.disconnect();
  }

  const MESSAGE_ID_PREFIX = "chat-messages-";
  const lastSeen = lastSeenMessageId ? BigInt(lastSeenMessageId) : null;

  // Message ids already emitted from THIS page instance. Bounded below so a
  // long-lived tab can't grow it without limit; the backend's Redis claim and
  // (from step 3) unique constraint are the real duplicate guards.
  const emitted = new Set();
  const EMITTED_CAP = 5000;

  // Highest snowflake seen in the rendered history. On a first attach this
  // becomes the channel's starting point, so the next reconnect resumes from
  // here rather than re-suppressing (and therefore missing) anything posted in
  // between.
  let highestBacklog = null;

  // Content we last saw per message, so an in-place edit can be told apart from
  // a re-render of identical text. Discord alert channels edit posts in place
  // ("filled", "closed" appended), which the parser will need to know about.
  const contentSeen = new Map();

  const pending = new Map();
  let flushTimer = null;

  function remember(id) {
    emitted.add(id);
    if (emitted.size > EMITTED_CAP) {
      // Drop the oldest half. Set preserves insertion order.
      const drop = Math.floor(EMITTED_CAP / 2);
      let i = 0;
      for (const key of emitted) {
        emitted.delete(key);
        contentSeen.delete(key);
        if (++i >= drop) break;
      }
    }
  }

  function text(el) {
    return el ? (el.innerText || el.textContent || "").trim() : "";
  }

  /* Message text with Discord's own chrome stripped out.
   *
   * innerText on a message node picks up more than the author typed: system and
   * join messages render an inline <time> (which expands to "9/3/26, 5:17 AM /
   * Thursday, September 3, 2026..."), and edited posts append an "(edited)"
   * marker. Feeding that into the trade parser would mean matching a price or a
   * date against Discord's furniture, so remove it here — at the point we know
   * which nodes are chrome — rather than trying to regex it back out later.
   *
   * Reads from a clone so the real page is never mutated. */
  function messageText(el) {
    if (!el) return "";
    const clone = el.cloneNode(true);
    clone.querySelectorAll('time, [class*="timestamp"], [class*="edited"]').forEach((n) => n.remove());
    return (clone.innerText || clone.textContent || "").trim();
  }

  /* Discord groups consecutive messages from one author and renders the
   * username only on the first of the group. Walking back to the nearest
   * preceding message that has one recovers the author for the rest. */
  function findAuthor(li) {
    let node = li;
    for (let hops = 0; node && hops < 50; hops++) {
      const el = node.querySelector('[id^="message-username-"]');
      if (el) {
        const name = el.querySelector('[class*="username"]') || el;
        /* NOT from el.id — that element is `message-username-<MESSAGE id>`, so
         * reading it back yields the message id relabelled as a user id, which
         * is worse than having none. The avatar URL is the one place the real
         * user id appears in the DOM: /avatars/<userId>/<hash>.png */
        const avatar = node.querySelector('img[src*="/avatars/"]');
        const src = avatar ? avatar.getAttribute("src") || "" : "";
        return {
          author: text(name).slice(0, 200) || null,
          authorId: (src.match(/\/avatars\/(\d+)\//) || [])[1] || null,
        };
      }
      node = node.previousElementSibling;
    }
    return { author: null, authorId: null };
  }

  function extractAttachments(root) {
    if (!root) return [];
    const out = [];
    root.querySelectorAll('a[class*="originalLink"], a[data-role="img"], a[href]').forEach((a) => {
      const href = a.getAttribute("href") || "";
      // Only real uploads; message links and mentions are not attachments.
      if (/(cdn|media)\.discordapp\.(com|net)/.test(href)) {
        out.push({ url: href, filename: (href.split("/").pop() || "").split("?")[0] });
      }
    });
    root.querySelectorAll("img[src]").forEach((img) => {
      const src = img.getAttribute("src") || "";
      if (/(cdn|media)\.discordapp\.(com|net)/.test(src) && !out.some((a) => a.url === src)) {
        out.push({ url: src, filename: (src.split("/").pop() || "").split("?")[0] });
      }
    });
    return out.slice(0, 10);
  }

  /* Class names in Discord's bundle are hashed (embedTitle_b0068a), so match on
   * a substring rather than an exact class — the stable part is the prefix. */
  function extractEmbeds(root) {
    if (!root) return [];
    const out = [];
    root.querySelectorAll('[class*="embedWrapper"], article[class*="embed"]').forEach((node) => {
      const fields = [];
      /* Anchor on the NAME node and walk up to its wrapper. Selecting the
       * wrapper directly with [class*="embedField"] also matches its own
       * embedFieldName_/embedFieldValue_ children (same substring), which
       * yielded every field two or three times plus empty rows. */
      node.querySelectorAll('[class*="embedFieldName"]').forEach((nameEl) => {
        const wrap = nameEl.parentElement || nameEl;
        const name = text(nameEl);
        const value = text(wrap.querySelector('[class*="embedFieldValue"]'));
        if (name || value) fields.push({ name: name, value: value });
      });
      out.push({
        title: text(node.querySelector('[class*="embedTitle"]')) || null,
        description: text(node.querySelector('[class*="embedDescription"]')) || null,
        author: text(node.querySelector('[class*="embedAuthorName"]')) || null,
        footer: text(node.querySelector('[class*="embedFooterText"]')) || null,
        fields: fields.slice(0, 25),
      });
    });
    return out.slice(0, 5);
  }

  function extract(li) {
    // id shape: chat-messages-<channelId>-<messageId>
    const parts = (li.id || "").slice(MESSAGE_ID_PREFIX.length).split("-");
    if (parts.length < 2) return null;
    const messageId = parts[parts.length - 1];
    const liChannelId = parts.slice(0, -1).join("-");
    if (!/^\d+$/.test(messageId)) return null;

    const contentEl = li.querySelector(`#message-content-${messageId}`);
    const accessories = li.querySelector(`#message-accessories-${messageId}`);
    const timeEl = li.querySelector("time[datetime]");
    const { author, authorId } = findAuthor(li);

    return {
      message_id: messageId,
      channel_id: /^\d+$/.test(liChannelId) ? liChannelId : channelId,
      server_id: (location.pathname.match(/\/channels\/(\d+)\//) || [])[1] || null,
      author: author,
      author_id: authorId,
      content: messageText(contentEl).slice(0, 8000),
      timestamp: timeEl ? timeEl.getAttribute("datetime") : null,
      attachments: extractAttachments(accessories || li),
      embeds: extractEmbeds(accessories || li),
      is_edit: false,
    };
  }

  function flush() {
    flushTimer = null;
    if (!pending.size) return;
    const batch = Array.from(pending.values());
    pending.clear();
    try {
      window.__kopyaaEmit(JSON.stringify(batch));
    } catch (err) {
      // The binding is gone (page navigating / context tearing down). Dropping
      // is correct: the Python side re-attaches and replays from last_seen.
    }
  }

  function queue(msg) {
    pending.set(msg.message_id, msg);
    if (!flushTimer) flushTimer = setTimeout(flush, flushMs);
  }

  function consider(li, { fromBacklog = false } = {}) {
    if (!li || !li.id || !li.id.startsWith(MESSAGE_ID_PREFIX)) return;
    const msg = extract(li);
    if (!msg) return;

    const already = emitted.has(msg.message_id);
    if (already) {
      // Same message, different text ⇒ the author edited it in place.
      const prev = contentSeen.get(msg.message_id);
      if (prev !== undefined && prev !== msg.content) {
        contentSeen.set(msg.message_id, msg.content);
        msg.is_edit = true;
        queue(msg);
      }
      return;
    }

    if (fromBacklog) {
      let snowflake = null;
      try {
        snowflake = BigInt(msg.message_id);
      } catch (err) {
        return;
      }
      // Track the newest thing already on screen, whether or not we emit it —
      // this is what the baseline is read from.
      if (highestBacklog === null || snowflake > highestBacklog) {
        highestBacklog = snowflake;
      }
      // Never read before: the whole visible history predates the connection,
      // so none of it is ours to ingest.
      // Read before: only the part newer than what the backend already has.
      if (lastSeen === null || snowflake <= lastSeen) {
        remember(msg.message_id);
        contentSeen.set(msg.message_id, msg.content);
        return;
      }
    }

    remember(msg.message_id);
    contentSeen.set(msg.message_id, msg.content);
    queue(msg);
  }

  function scanSubtree(node, opts) {
    if (node.nodeType !== 1) return;
    if (node.id && node.id.startsWith(MESSAGE_ID_PREFIX)) {
      consider(node, opts);
      return;
    }
    const found = node.querySelectorAll
      ? node.querySelectorAll(`li[id^="${MESSAGE_ID_PREFIX}"]`)
      : [];
    found.forEach((li) => consider(li, opts));
  }

  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      m.addedNodes.forEach((n) => scanSubtree(n, { fromBacklog: false }));
      // An edit changes text inside an existing node rather than adding one.
      if (m.type === "characterData" && m.target.parentElement) {
        const li = m.target.parentElement.closest(`li[id^="${MESSAGE_ID_PREFIX}"]`);
        if (li) consider(li, { fromBacklog: false });
      }
    }
  });

  /* Observe the document rather than the message list itself. Discord swaps the
   * scroller out on channel switches and virtualisation, so an observer bound to
   * the list dies silently the first time that happens — the classic "listener
   * looks connected but sees nothing" failure. Filtering is a cheap id-prefix
   * check, so the wider scope costs little. */
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
  });
  window.__kopyaaObserver = observer;

  // Seed from what's already rendered, replaying only past `lastSeenMessageId`.
  document
    .querySelectorAll(`li[id^="${MESSAGE_ID_PREFIX}"]`)
    .forEach((li) => consider(li, { fromBacklog: true }));
  flush();

  // Report liveness: the presence of the message list is what "connected"
  // means. Read by the Python side, which turns it into a heartbeat.
  // Newest message already on screen when we attached. Read once by the Python
  // side to set a channel's starting point.
  window.__kopyaaBaseline = () => (highestBacklog === null ? null : highestBacklog.toString());

  window.__kopyaaHealth = () => ({
    attached: !!window.__kopyaaObserver,
    messageNodes: document.querySelectorAll(`li[id^="${MESSAGE_ID_PREFIX}"]`).length,
    href: location.href,
  });

  return true;
};
