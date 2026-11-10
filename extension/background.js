// Agent Tab Grouper — CDP-driven.
//
// A CDP client attaches to this service worker and calls self.groupTab /
// self.activateTab with Runtime.evaluate (awaitPromise: true).
// chrome.debugger.getTargets() bridges CDP targetIds to chrome.tabs.Tab ids.
// Nothing here is specific to any one driver — it's a plain tab grouper.
//
// THIS FILE IS NEVER A SOURCE OF TRUTH. Which tabs belong to which session is
// answered by the registry each session keeps on disk (~/.config/horse-browser/tabs/
// <BU_NAME>, written by helpers._hb_track and daemon._track_target), never by reading
// tab-group titles back out of the browser. The tab group RENDERS that registry: it is
// what the operator sees, not what the code decides on. Two consequences:
//   • groupTab is best-effort — its return value is ignored and a failure is not an
//     error. An unpainted tab is still fully owned by its session.
//   • nothing here answers "which tabs are mine" or "which are dead". Those have to work
//     on a browser with no extension at all, so they are answered from the registry over
//     plain CDP instead. See docs/attached-mode.md.

// MV3 service workers sleep when idle and then vanish from Target.getTargets
// until something wakes them — so a dormant SW would make groupTab
// (and the install smoke test) silently miss us. Two keepalives:
//   • a 30s no-op alarm resets the idle timer so we stay warm once running;
//   • waking on tab creation covers the gap before the first alarm tick — a
//     tab is created right before we're asked to group it, and at launch.
chrome.runtime.onInstalled.addListener(() => chrome.alarms.create("keepalive", { periodInMinutes: 0.5 }));
chrome.runtime.onStartup.addListener(() => chrome.alarms.create("keepalive", { periodInMinutes: 0.5 }));
chrome.alarms.onAlarm.addListener(() => {});
chrome.tabs.onCreated.addListener(() => {});

// ── realness, wire half ── the sec-ch-ua request header must carry the same major version
// as the UA string. A mismatch is a bot tell (Cloudflare Turnstile flags it and hard-loops),
// and it appears on its own: Chrome for Testing SELF-UPDATES, so any version we write down
// somewhere goes stale without anyone touching it.
//
// So don't write it down. Derive it from this browser's own userAgent at every service-worker
// start. The number and the browser are then the same fact, and cannot drift.
//
// This used to live in a static rules.json that bin/horse-browser rewrote on update
// (sync_realness_version) — which only held while OUR update path was the one that moved
// Chrome. A self-update left the file behind, silently.
const REALNESS_RULE_ID = 1001;
const REALNESS_RESOURCES = ["main_frame", "sub_frame", "stylesheet", "script", "image",
  "font", "object", "xmlhttprequest", "ping", "csp_report", "media", "websocket", "other"];
async function syncRealnessHeader() {
  const m = navigator.userAgent.match(/Chrome\/(\d+)/);
  if (!m) return;
  const v = m[1];
  await chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: [REALNESS_RULE_ID],           // replace, never accumulate
    addRules: [{
      id: REALNESS_RULE_ID,
      priority: 1,
      action: { type: "modifyHeaders", requestHeaders: [{
        header: "sec-ch-ua", operation: "set",
        value: `"Not;A=Brand";v="8", "Chromium";v="${v}", "Google Chrome";v="${v}"`,
      }] },
      condition: { urlFilter: "*", resourceTypes: REALNESS_RESOURCES },
    }],
  });
}
syncRealnessHeader();

const COLORS = ["blue", "cyan", "green", "yellow", "orange", "red", "pink", "purple"];

function colorForName(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return COLORS[h % COLORS.length];
}

// ── Session codename ── a stable identity (emoji + colour + last-4) derived from the
// session id, so any companion tool that renders the same code — a terminal statusline,
// a dashboard — matches this tab group. 48 emoji grouped 6-per-colour; colour group =
// slot / 6. The colours are Chrome's 8 tab-group colours (the binding constraint).
// Hash = FNV-1a + a murmur3 finalizer over the FULL session id; mirror this exact
// function if you render the codename in another tool, so they stay identical.
const CODE_EMOJI = ["🔥","🍎","🍓","🍒","🌹","🐞","🦊","🍊","🦁","🐯","🥕","🏀","🍋","🌻","⭐","🐝","🍌","🐥","🐸","🍀","🌵","🐢","🌲","🐍","🐬","🌊","💎","🧊","🐳","💧","🐧","🫐","🦋","🌀","🌐","🐟","🦄","🍇","🔮","🐙","🍆","👾","🌸","🐷","🦩","🍑","🌷","🌺"];
const CODE_ORDER = ["red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink"];

function hash32(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  h ^= h >>> 16; h = Math.imul(h, 0x7feb352d); h ^= h >>> 15; h = Math.imul(h, 0x846ca68b); h ^= h >>> 16;
  return h >>> 0;
}

// id → { title: "🐍 0FDA", color: "green" }. A bare ≤4-char label (no session id)
// falls back to the old plain-label group, so nothing breaks if the caller is older.
function codename(id) {
  id = id || "";
  if (id.length <= 4) return { title: id, color: colorForName(id) };
  const slot = hash32(id) % CODE_EMOJI.length;
  return { title: CODE_EMOJI[slot] + " " + id.slice(-4).toUpperCase(), color: CODE_ORDER[Math.floor(slot / 6)] };
}

async function tabIdForTargetId(targetId) {
  const targets = await chrome.debugger.getTargets();
  const t = targets.find(x => x.id === targetId);
  if (!t || !t.tabId) throw new Error(`no tab for CDP target ${targetId}`);
  return t.tabId;
}

// `session` is the FULL session id (the caller passes CLAUDE_CODE_SESSION_ID); the
// codename (emoji + colour + last-4) is derived here so the browser owns its render.
// Best-effort by contract — see the header. Callers ignore what this returns; the tab
// is theirs because their registry says so, not because this succeeded.
self.groupTab = async (targetId, session) => {
  const tabId = await tabIdForTargetId(targetId);
  const tab = await chrome.tabs.get(tabId);
  const { title, color } = codename(session);
  const existing = await chrome.tabGroups.query({ windowId: tab.windowId, title });
  if (existing.length) {
    await chrome.tabs.group({ tabIds: [tabId], groupId: existing[0].id });
  } else {
    const gid = await chrome.tabs.group({
      tabIds: [tabId],
      createProperties: { windowId: tab.windowId },
    });
    await chrome.tabGroups.update(gid, { title, color, collapsed: false });
  }
  return tabId;
};

// chrome.tabs.update({active:true}) swaps the browser's visible tab without raising
// the app — replaces CDP Target.activateTarget, which calls [NSApp activate]
// and steals macOS focus while the agent works.
self.activateTab = async (targetId) => {
  const tabId = await tabIdForTargetId(targetId);
  await chrome.tabs.update(tabId, { active: true });
  return tabId;
};

// CDP targetId of the tab currently visible in each browser window. No CDP equivalent
// exists — targets carry no "visible" flag — which is why this stays even though nothing
// in the harness calls it: tests/e2e.sh uses it to prove a screenshot of a background tab
// leaves the viewer's visible tab untouched, and there is no other way to observe that.
self.activeTabTargets = async () => {
  const tabs = await chrome.tabs.query({ active: true });
  const targets = await chrome.debugger.getTargets();
  const idByTab = new Map(targets.filter(t => t.tabId).map(t => [t.tabId, t.id]));
  return tabs.map(t => idByTab.get(t.id)).filter(Boolean);
};

// Close STRAY tabs — about:blank that no session's registry claims.
// This is janitorial, NOT ownership: a session's dead tabs are closed from its registry
// over plain CDP (bin/horse-browser reap_orphan_tabs), which works with or without this
// extension. A stray by definition appears in no registry, and finding one needs
// chrome.tabs, so this sweep is the one piece of cleanup that stays here.
//
// `claimed` is every targetId any registry claims — live sessions and dead alike. This
// function may never touch one. That guard is what keeps the two rules from colliding:
// groupTab is best-effort, so a session's tab CAN end up ungrouped, and without the
// guard this would read that tab as a stray and close a live session's work.
//
// A tab group is no claim. The browser restores a relaunched window's tabs with their
// groups and with NEW target ids, so every restored blank is grouped and in no registry;
// reading the group as ownership kept each of them open for good.
//
// `claimed` is a snapshot, and a tab is minted a moment before its registry line is
// written. `minAgeMs` leaves a tab that young alone; the caller sets it.
const strayTabs = async (claimed, minAgeMs) => {
  const known = new Set(Array.isArray(claimed) ? claimed : []);
  const tabs = await chrome.tabs.query({});
  const targets = await chrome.debugger.getTargets();
  const idByTab = new Map(targets.filter(t => t.tabId).map(t => [t.tabId, t.id]));
  const now = Date.now();
  return tabs.filter(t =>
    !t.pinned &&                                                  // never the pinned Monitor
    ((t.url || "") === "" || t.url === "about:blank") &&
    now - (t.lastAccessed || 0) >= minAgeMs &&
    !known.has(idByTab.get(t.id)));                               // claimed ⇒ not ours to judge
};
self.sweepStrayTabs = async (claimed, minAgeMs = 0) => {
  const stray = await strayTabs(claimed, minAgeMs);
  for (const t of stray) { try { await chrome.tabs.remove(t.id); } catch {} }
  return { closed: stray.length };
};
// What sweepStrayTabs would close, closing nothing — the launcher's dry run.
self.countStrayTabs = async (claimed, minAgeMs = 0) =>
  ({ closed: (await strayTabs(claimed, minAgeMs)).length });

// The Agent Monitor — a CCTV grid of every session's live tabs. It's a normal extension
// page that does its own CDP work as a second client on this browser's debug port (which the
// launcher seeds into chrome.storage.local — see monitor.js). We keep exactly one, pinned
// as the first tab, "permanently": Chrome already shields pinned tabs from "Close other
// tabs", and showMonitor() reopens it if it's closed any other way. Defined as a const arrow
// (not a function declaration) right after MONITOR_URL — both bind reliably in the worker.
const MONITOR_URL = chrome.runtime.getURL("monitor.html");
const showMonitor = async (focus) => {
  const tabs = await chrome.tabs.query({ url: MONITOR_URL });
  if (tabs.length === 0) {
    await chrome.tabs.create({ url: MONITOR_URL, pinned: true, active: !!focus, index: 0 });
    return;
  }
  if (!tabs[0].pinned) await chrome.tabs.update(tabs[0].id, { pinned: true });
  if (focus) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    await chrome.windows.update(tabs[0].windowId, { focused: true });
  }
  // de-dup: keep one Monitor (a closure burst can briefly mint two before either query returns)
  for (const t of tabs.slice(1)) { try { await chrome.tabs.remove(t.id); } catch (e) {} }
};

// ── which browser is this? — the toolbar badge answers it ───────────────────
// Several horse-browsers are open at once, one per agent, and they look identical: the Monitor
// is a PINNED tab, and a pinned tab shows only its favicon, so there is nowhere else on screen
// that names the instance. The badge carries the debug port the launcher seeded (see
// bin/horse-browser: seed_instance_env), which is the one number that identifies a browser —
// the tooltip spells it out for ports too long to fit. Colour is derived from the port, so two
// windows side by side differ at a glance rather than by reading digits.
const BADGE_COLORS = ["#1a73e8", "#12a150", "#c5221f", "#8430ce", "#b06000", "#00838f"];
async function showInstanceBadge() {
  const { hbCdpPort: port } = await chrome.storage.local.get("hbCdpPort");
  if (!port) {   // not seeded (yet) — say nothing rather than guess
    await chrome.action.setBadgeText({ text: "" });
    await chrome.action.setTitle({ title: "Open Agent Monitor" });
    return;
  }
  await chrome.action.setBadgeText({ text: String(port) });
  await chrome.action.setBadgeBackgroundColor({ color: BADGE_COLORS[port % BADGE_COLORS.length] });
  try { await chrome.action.setBadgeTextColor({ color: "#ffffff" }); } catch (e) {}  // Chrome ≥110
  await chrome.action.setTitle({ title: `Agent Monitor — this browser is CDP :${port}` });
}
showInstanceBadge();                                              // every worker start
chrome.storage.onChanged.addListener((changes, area) => {         // …and whenever the port lands
  if (area === "local" && changes.hbCdpPort) showInstanceBadge();
});

chrome.action.onClicked.addListener(() => showMonitor(true));     // toolbar button → focus it
chrome.runtime.onStartup.addListener(() => showMonitor(false));   // profile start → ensure pinned
// Closed any other way (explicit close, etc.) → reopen, unless the window itself is going away.
// Called directly (no setTimeout): chrome.tabs.* keeps the MV3 worker alive to finish; a timer wouldn't.
chrome.tabs.onRemoved.addListener((_id, info) => { if (!info.isWindowClosing) showMonitor(false); });
// install/update → ensure the Monitor is pinned. No welcome tab: the Monitor's own empty
// state IS the welcome (monitor.html), so a fresh profile opens ONE tab that explains
// itself and becomes the wall as soon as an agent opens something.
chrome.runtime.onInstalled.addListener(() => showMonitor(false));


// The window's real geometry, for winbounds.js. A backgrounded tab reports outerWidth,
// outerHeight, screenX and screenY as 0; only the service worker can ask chrome.windows what
// the window actually is. Returns undefined rather than guessing if the lookup fails — the page
// side falls back to something self-consistent, and a wrong number is worse than none.
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg || msg.hb !== 'winbounds' || !sender.tab) return;
  chrome.windows.get(sender.tab.windowId, (w) => {
    if (chrome.runtime.lastError || !w) { reply(null); return; }
    reply({ left: w.left, top: w.top, width: w.width, height: w.height });
  });
  return true;                      // reply arrives asynchronously
});

