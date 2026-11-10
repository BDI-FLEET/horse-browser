# horse-browser — manual

The full reference for driving the dedicated agent browser. The always-on rule
(`~/.claude/rules/horse-browser.md`) is the short version; this is everything else,
on demand. Read the section you need — you don't need to read it front to back.

## What it is

A dedicated **Chrome for Testing** (its own profile, coexists with your daily browser)
that you drive over the Chrome DevTools Protocol through the `horse-browser` command.
It's **shared**: every agent session — and every subagent — drives its own tabs in the
same browser, dropped into its own colour group by a bundled extension, and your operator
is signed into real accounts here. The substrate keeps that shared use coherent (below);
you drive with raw CDP plus a small paved-path helper set.

## Driving it

```bash
horse-browser <<'PY'
tid = open_tab("https://example.com")   # your tab, your colour group, no focus steal
wait_for_load()
print(page_info())
PY
```

- Helpers are pre-imported; the daemon auto-starts and connects. You never set
  `BU_CDP_URL` or name a port — horse-browser owns the endpoint and wires it internally.
- Bare `horse-browser` (no script) just ensures the browser is up (launch if down, heal a
  GPU wedge after sleep, no-op if healthy) and exits.
- Flags forward: `horse-browser --doctor`, `horse-browser --reload` (drop the daemon so the
  next call reloads code), `horse-browser skill` (print this manual).
- **Never launch Chrome yourself** — don't `open` Chrome/Brave, don't spawn your own
  Chromium. Only `horse-browser`; it runs a dedicated browser that won't collide with the
  operator's daily one. If `horse-browser` isn't on PATH, setup hasn't run — tell the
  operator to run the repo's `./install.sh`; don't attempt setup yourself.

## When not to use a browser

A plain fetch of public info needs no browser — use `curl`/`http_get(url)` for a public
page, an API, or docs. Reach for the browser when the task needs interaction (click, type,
navigate), the operator's logged-in session, JS rendering, or a bot-protected page. If a
direct fetch returns a shell/blocked page, then escalate to the browser.

## The CDP contract — what the substrate changes, and nothing else

Raw CDP behaves exactly as you know it, with **two** deliberate exceptions that keep a
shared browser coherent. Both honour what the call meant and drop only a shared-browser
side effect; nothing is blocked, wrapped, or refused.

1. **`Target.activateTarget` is fulfilled focus-safe.** The target tab still becomes the
   active tab in its window — but the `[NSApp activate]` side effect (which yanks the
   browser in front of whatever app your operator is using) is dropped. The daemon routes
   it through the extension's `activateTab` (`chrome.tabs.update`, which does not raise the
   window). If the extension is unreachable it falls back to the native call and logs that
   it stole focus. To *deliberately* raise the window over the operator's app (rare), do it
   explicitly with a `chrome.windows.update({focused:true})` via the extension.
2. **Your session is pinned to its own tab.** The daemon binds your session to the tab you
   drive; a stale target-destroyed event or a foreign first page can never drift you onto
   another agent's tab. So `goto_url`, `js`, `page_info`, `capture_screenshot` always act on
   *your* tab. (This is why the old "never bare `goto_url`" warning is gone — it can no
   longer clobber a neighbour.)

One quirk to know: the tab you are driving carries a leading `🐴 ` marker in its title (added
so the operator can see which tab an agent is working). If you read `document.title` and get
`🐴 Example Domain`, the horse + space is the marker, not the page.

The horse comes and goes — it marks the **foreground lease** (below), so it appears on the
tab you're driving and disappears once you stop, rather than sitting on every tab you have
ever touched. A tab with no horse is one no agent is working right now.

## Verbs — the paved path, and the CDP they run

Helpers are worked examples of the right CDP idiom, not a wall over it. Each shows the raw
calls it makes, so when you outgrow it you already know the idiom to compose your own.

**Tabs**
- `open_tab(url)` → reuse a blank tab in your group or `Target.createTarget(background=True)`,
  group it via the extension, switch to it, navigate, `wait_for_load()`. Returns targetId.
- `list_tabs()` → your session's registry (`~/.config/horse-browser/tabs/$BU_NAME`), filtered
  to targets still open: `targetId`/`url`/`title`/`lastAccessed`. The registry is the truth
  about which tabs are yours — the coloured tab group renders it, never sources it, so this
  answers identically on a browser with no extension. `tabId`/`discarded`/`audible`/`active`
  come back `None`: only `chrome.tabs` knows them, and nothing may depend on the extension
  being there. See `docs/attached-mode.md`.
- `switch_tab(tid)` → attach the target (`Target.attachToTarget flatten`) and tell the daemon
  to bind this session to it, which takes the foreground lease on the new tab and releases it
  on the old one. No `activateTarget`.
- `goto_url(url)` = `Page.navigate` on your bound tab. `current_tab()` / `all_tabs()` /
  `close_tab(t)` as named.

**Read**
- `page_info()` → `{url,title,w,h,sx,sy,pw,ph}` (viewport + scroll + page size), or
  `{dialog:…}` if a native dialog is open (the JS thread is frozen until it's handled).
- `js(expr, target_id=None)` = `Runtime.evaluate(returnByValue, awaitPromise)`; retries once
  wrapped in a function if Chrome reports an illegal top-level `return`. Pass `target_id` (from
  `iframe_target(substr)`) to run inside an iframe.
- `http_get(url, headers=?)` — pure HTTP, no browser.

**Input — trusted, real events (see the section below)**
- `click(css)` / `click_xy(x,y)`, `type_into(css,text, clear=?, enter=?)`, `type_text(text)`,
  `press(name)`, `press_hold(css_or_xy, s)`, `drag(css_or_xy, to=/dx=)`.
- For a listener-free `<textarea>` where speed beats fidelity: `cdp("Input.insertText",
  text=…)` directly — it fires **no key events** (that's the point), so never use it on a
  field the page listens to.

**Visual**
- `capture_screenshot(path=None, full=False, max_dim=None)` → attaches a **fresh** session to
  your target and runs `Page.captureScreenshot(fromSurface=True)` on it, retrying a few times.
  That fresh-session + fromSurface path is why it shoots a backgrounded or occluded tab
  reliably where a raw capture on the shared daemon session hangs or returns a 2×2. Writes a
  per-call unique PNG (never a shared `shot.png`) and returns the path.

**Sync**
- `wait_for_load(timeout=15)` polls `document.readyState=='complete'`.
- `wait_for_element(css, visible=?)` for SPAs (the doc is 'complete' before the framework
  renders). `wait_for_network_idle()` after submits/XHR. `wait(s)` sleeps.

**Escape hatch**
- `cdp(method, session_id=None, **params)` — raw CDP for everything the helpers don't cover.
  This is the point: compose freely.

## Page workflow

- Prefer the **accessibility tree** to screenshots for finding elements:
  `cdp("Accessibility.getFullAXTree")["nodes"]` has every element's role, name, and
  `backendDOMNodeId` (filter in Python first — it's thousands of nodes). Coordinates:
  `q = cdp("DOM.getBoxModel", backendNodeId=n)["model"]["content"]; x,y = sum(q[0::2])/4, sum(q[1::2])/4`
  (viewport px, ready for `click_xy`; negative/oversized → scroll first).
- Clicking: AX node → box centre → `click_xy(x,y)` → verify with a targeted `js(...)`/`page_info()`.
- Fall back to raw HTML via `js(...)` only when the AX tree lacks the element (canvas, exotic
  widgets); screenshot when layout or imagery matters.
- After navigation, `wait_for_load()`. If the current tab is stale/internal, `ensure_real_tab()`.
- **Login walls:** stop and ask. Exception: use available SSO automatically when Chrome is
  already signed in; still stop for passwords, MFA, consent, or ambiguous account choice.

## Input — use trusted, real events

`click` / `type_into` / `press` fire the same key/mouse events a real browser generates, so
the page's `keyup`/`input`/`mousedown` listeners actually run — submit buttons enable,
autocompletes fire, React/Vue/Svelte controlled state updates, menus open. **Never drive a
form with `el.value=…` or `el.click()` in `js(...)`, and don't use `Input.insertText` for a
listened field:** those fire *no* (or partial) events, so the value/click *looks* applied
while the page's logic never ran — disabled submit, dead dropdown, stale state. This is
**correctness, not just bot-evasion** — plain sites break too; the anti-detection win rides
along free.

- `click(css)` — trusted mousedown→mouseup→click (+pointer). `click_xy(x,y)` for coords,
  shadow DOM, or cross-iframe (CDP input passes through iframes at the compositor level).
- `type_into(css, text, clear=?, enter=?)` — real per-char keys, fast. `type_text(text)` types
  the focused element. `press("Enter"|"Tab"|"Escape"|"Arrow…")` for a named key. Shifted
  characters carry a real Shift keydown, so `event.shiftKey` is true as on a keyboard.
- Fast untrusted (`js("el.click()")`) is fine on trivial internal/dev pages, but **always** use
  trusted input on login / signup / checkout, anything behind a bot vendor (Akamai /
  PerimeterX / DataDome / Cloudflare / hCaptcha / reCAPTCHA), or after any challenge appeared.

## Challenges — solve the easy ones, don't halt, don't reload

Many "captchas" are just a gesture — click a checkbox, press-&-hold, slide-to-verify. Do
them. With a real fingerprint (always on) plus a trusted gesture, the easy ones usually clear.

- `solve_challenge()` — classifies, then solves a same-document gesture (a Press & Hold on
  `#px-captcha`, a slider it can select) and verifies it. A **perception** challenge (identify
  images, read distorted text, rotate, audio) returns `escalate:<why>` — only those go to the
  operator. Gesture verbs take a CSS selector **or an `(x,y)`** you read off a screenshot:
  `press_hold(css_or_xy, s)`, `drag(css_or_xy, to=(x,y)/dx=…)`, `click_xy(x,y)`.
  Both gestures pace themselves against the clock, so a busy machine costs them samples rather
  than seconds — the shape and duration are fitted to recorded human traces and hold under
  load. Don't wrap them in sleeps of your own to "slow them down": that lands the gesture
  outside the band it was fitted to, which is the thing being scored.
- **Cross-origin iframe challenges → vision is primary.** A challenge sealed in a cross-origin
  iframe (Turnstile, DataDome, hCaptcha) can't be reached by `querySelector`, but CDP input
  still lands on the pixel. `solve_challenge()` hands these back as `vision:<vendor> …
  Screenshot:<path>`: read the screenshot, act by coordinate, then confirm with
  `challenge_cleared()` (it reads the top-document side effects a solve leaves — token input
  populated, iframe removed). Didn't clear → screenshot again and adjust. No hardcoded widget
  offsets — vendors redesign; vision + verify self-corrects.
- **Solve, don't reload.** A challenge is a reputation checkpoint. Solving deposits credit at
  three levels — a long-lived trust cookie, the fingerprint, and (the big one) the whole
  **IP/network**: one human-grade solve lifts the IP's reputation for everyone on it (observed
  live — a DataDome solve made a fresh cookieless browser on the same IP sail through).
  Reloading to reroll banks nothing and a streak of unsolved challenges reads bot-like and can
  *lower* the score. Spend the few seconds to solve.

## The extension — per-session tab groups

Each session (and each subagent) gets its own colour group automatically; you never manage
this. `chrome.tabGroups` is extension-only (no CDP equivalent), which is why an extension
exists. The service worker exposes three async functions on `self`, reachable by attaching to
its service_worker target and `Runtime.evaluate` (this is what `ext_call` / `bh_*` do):

```js
self.groupTab(targetId, label)  -> tabId       // paint the tab into `label`'s group (best-effort)
self.activateTab(targetId)      -> tabId       // make it the visible tab WITHOUT raising the window
self.activeTabTargets()         -> targetId[]  // the visible tab of each window (no CDP equivalent)
self.sweepStrayTabs(claimed, minAgeMs) -> {closed}  // close about:blank no registry claims
self.countStrayTabs(claimed, minAgeMs) -> {closed}  // the same count, closing nothing
```

None of these is a source of truth. Which tabs belong to a session is answered by that
session's registry over plain CDP — `groupTab` only paints, and its result is ignored.
Asking the browser instead would give a second answer that can disagree with the first.

Focus-safety mechanics: (1) `Target.createTarget(background=True)` + `chrome.tabs.update({active:true})`
instead of `Target.activateTarget` — the latter calls `[NSApp activate]`; `chrome.tabs.update`
only changes which tab is visible. (2) `Emulation.setFocusEmulationEnabled` per session makes
the renderer treat the page as focused (`document.hasFocus()` true, rAF at full rate),
so a backgrounded tab still paints and fires events. Native popovers (autofill, password save,
translate) are *not* gated by focus emulation — if you see typing-time focus theft, disable
those at the profile level rather than chasing them through CDP.

**The foreground lease.** (2) is held as a lease, not set once. The daemon takes it on the
first call that drives a tab, renews it on every call after, and drops it — along with the 🐴 —
after `HORSE_BROWSER_FOCUS_TTL` seconds of silence (default 60); the next call takes it back
before the page is touched, so nothing an agent does can land on a page that still thinks it
is hidden. It is per-CDP-session, so it also dies with the daemon.

Why it must lapse: a page that believes it is visible *paints*. Held forever, every tab any
agent had ever touched kept compositing for the daemon's whole life — measured at ~40% of a
core for two idle dashboards, indefinitely, on a browser window that wasn't even focused. The
lease is also the more honest signal: a real tab loses focus when its user looks elsewhere,
and one that reports focus for eight unbroken hours does not look like a person.

The cost to know: if you leave a page unattended for longer than the TTL — a bare `sleep`
rather than `wait_for_load()`, which renews — it goes hidden and throttles its timers while
you wait. Poll, or raise `HORSE_BROWSER_FOCUS_TTL`, when a page must keep working untouched.

Several browsers run side by side (one per agent, each its own `HORSE_BROWSER_PORT` +
`HORSE_BROWSER_PROFILE`), so nothing in the extension may assume a port. An extension can't
read argv, so the launcher writes its own into that profile's storage at launch
(`chrome.storage.local.hbCdpPort`); the Monitor page — the one part that needs a raw CDP
socket — reads it and still proves the port is its own (it tags its URL with a nonce and
looks for that URL in the target list) before attaching. `tools/sw_eval.py` is the launcher's
side of that channel: `sw_eval.py <port> <expression> [wait_s]` evaluates anything in the
service worker.

If `open_tab` is undefined, the vendored harness didn't load — run `horse-browser harness-setup`
(or re-run `install.sh`); don't hand-roll your own, the focus-safe behaviour is subtle.

## Tab discipline

Glance at `list_tabs()` before opening many tabs; close stale ones via raw CDP
(`cdp("Target.closeTarget", targetId=t["targetId"])`). Close the rest when a task ends —
Chrome removes empty groups, so a disciplined session leaves zero clutter. (A daemon whose
session dies also self-reaps its group, so this is politeness, not a leak backstop.)

A session that is still open but silent gives back its blank tabs on its own: after
`HORSE_BROWSER_BLANK_TTL` seconds without a call (default 600; `0` keeps them) its daemon
closes every tab its registry claims whose address is still `about:blank`. A page written
into such a tab without navigating it reads as blank too, so navigate what must last. The next
call re-attaches as a first call does — to one of the session's remaining tabs, or a new one.

## Diagnostics & gotchas

- `horse-browser --doctor` — endpoint, daemon, and extension state. `horse-browser harness-setup`
  — (re)build the harness venv.
- `horse-browser instances` — every browser on this machine with a debugging port: pid, port,
  uptime, live tab count, profile. Anything not on a profile this machine is configured for is
  marked `<- stray`. A throwaway browser that outlives the script which started it (an
  interrupted test, a timed-out one-liner) leaves no trace anywhere else, and stays up holding
  memory and a port. It never kills anything: some strays legitimately belong to another tool.
- Omnibox popups are not real work tabs. CDP target order is not the visible tab-strip order.
- `page_info()` returning `{dialog:…}` means a native alert/confirm/prompt froze the JS thread;
  handle the dialog (`cdp("Page.handleJavaScriptDialog", accept=True)`) before anything else.
- A workspace `agent_helpers.py` (`~/.config/browser-harness/agent-workspace/`) still loads on
  top of the built-ins for your own additions; its private (`_`-prefixed) helpers must stay
  prefixed so they don't shadow the harness's.
