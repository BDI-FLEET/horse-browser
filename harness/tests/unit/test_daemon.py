import asyncio
import json

from horse_harness import daemon


class _FakeCDP:
    """Records send_raw calls so tests can assert which CDP methods fired."""

    def __init__(self):
        self.calls = []  # list of (method, params, session_id)

    async def send_raw(self, method, params=None, session_id=None):
        self.calls.append((method, params, session_id))
        # Set-session/initial-attach paths only need a benign response.
        return {}


def _fresh_daemon():
    d = daemon.Daemon()
    d.cdp = _FakeCDP()
    return d


def test_set_session_enables_all_default_domains_on_new_session():
    """Regression: switch_tab() / open_tab() in helpers.py route through the
    `set_session` IPC, which previously only enabled Page on the new
    session. With Network disabled, wait_for_network_idle() silently stops
    receiving events after a tab switch. Initial attach enables all of the
    (Page, DOM, Runtime, Network); set_session must enable the same set."""
    d = _fresh_daemon()
    new_session = "session-AFTER-switch"

    asyncio.run(d.handle({
        "meta": "set_session",
        "session_id": new_session,
        "target_id": "target-2",
    }))

    enabled_on_new = [
        method for (method, _params, sid) in d.cdp.calls
        if sid == new_session and method.endswith(".enable")
    ]
    assert set(enabled_on_new) == {"Page.enable", "DOM.enable", "Network.enable"}, (
        f"set_session must enable Page/DOM/Runtime/Network on the new session "
        f"(parity with initial attach). Got: {enabled_on_new}"
    )
    assert d.session == new_session
    assert d.target_id == "target-2"


def test_set_session_falls_back_to_existing_target_id_when_not_provided():
    """If a caller forgets target_id (passes None), the daemon should keep its
    existing target_id rather than overwriting it with None — otherwise
    subsequent calls that depend on self.target_id would break."""
    d = _fresh_daemon()
    d.target_id = "original-target"

    asyncio.run(d.handle({
        "meta": "set_session",
        "session_id": "session-AFTER",
        "target_id": None,
    }))

    assert d.target_id == "original-target"
    assert d.session == "session-AFTER"


def test_enable_default_domains_swallows_errors_per_domain():
    """A single domain failing to enable must not prevent the others from
    being attempted — that would leave the daemon in a partially-configured
    state. Each Domain.enable call has its own try/except inside the helper."""
    class _PartialFailureCDP(_FakeCDP):
        async def send_raw(self, method, params=None, session_id=None):
            self.calls.append((method, params, session_id))
            if method == "DOM.enable":
                raise RuntimeError("simulated DOM failure")
            return {}

    d = daemon.Daemon()
    d.cdp = _PartialFailureCDP()

    asyncio.run(d._enable_default_domains("session-X"))

    attempted = [m for (m, _p, _s) in d.cdp.calls]
    assert "Page.enable" in attempted
    assert "DOM.enable" in attempted  # attempted, but raised
    # Runtime is deliberately NOT enabled — it is detectable and we consume none of its
    # events. This asserts the absence so nobody restores it for symmetry.
    assert "Runtime.enable" not in attempted
    assert "Network.enable" in attempted


def test_set_session_disables_network_on_old_session_before_enabling_new():
    """When switching tabs, the previous session's Network domain must be
    disabled so background tabs (polling, SSE, etc.) stop emitting events
    into the global buffer that wait_for_network_idle reads. Initial attach
    has no `old_session` so this disable doesn't fire then."""
    d = _fresh_daemon()
    d.session = "session-OLD"
    d.target_id = "target-OLD"

    asyncio.run(d.handle({
        "meta": "set_session",
        "session_id": "session-NEW",
        "target_id": "target-NEW",
    }))

    disabled = [
        (method, sid) for (method, _params, sid) in d.cdp.calls
        if method == "Network.disable"
    ]
    assert disabled == [("Network.disable", "session-OLD")], (
        f"Network.disable must fire on the old session before re-enabling on "
        f"the new one. Got: {disabled}"
    )

    # Sanity: the new session still gets Network.enable.
    enabled_on_new = {
        method for (method, _p, sid) in d.cdp.calls
        if sid == "session-NEW" and method.endswith(".enable")
    }
    assert "Network.enable" in enabled_on_new


def test_set_session_does_not_disable_network_when_no_previous_session():
    """First set_session call (e.g. very early in startup before any attach)
    has no old_session — the Network.disable path must be skipped."""
    d = _fresh_daemon()
    d.session = None  # no prior attach

    asyncio.run(d.handle({
        "meta": "set_session",
        "session_id": "session-FIRST",
        "target_id": "target-FIRST",
    }))

    disables = [m for (m, _p, _s) in d.cdp.calls if m == "Network.disable"]
    assert disables == [], (
        f"Network.disable must not fire when there's no previous session "
        f"to disable. Got: {disables}"
    )


def test_set_session_runs_disable_and_enables_in_parallel():
    """The Domain.enable calls (plus Network.disable on the old session)
    must run concurrently via asyncio.gather, not sequentially. With the old
    sequential code, helpers.switch_tab() would block in _send() for up to
    ~22s on a slow/remote daemon while the helper's IPC socket has a 5s
    read timeout, causing client-side socket timeouts. Verifying that all
    all of them reach send_raw before any returns proves parallelization."""
    class _ConcurrencyProbeCDP:
        def __init__(self):
            self.calls = []
            self.in_flight = 0
            self.max_concurrent = 0
            self.release = None  # asyncio.Event, set inside the test loop

        async def send_raw(self, method, params=None, session_id=None):
            self.calls.append((method, params, session_id))
            self.in_flight += 1
            self.max_concurrent = max(self.max_concurrent, self.in_flight)
            try:
                await self.release.wait()
            finally:
                self.in_flight -= 1
            return {}

    async def run():
        d = daemon.Daemon()
        d.cdp = _ConcurrencyProbeCDP()
        d.session = "session-OLD"  # ensures Network.disable on old fires
        d.cdp.release = asyncio.Event()

        handle_task = asyncio.create_task(d.handle({
            "meta": "set_session",
            "session_id": "session-NEW",
            "target_id": "target-NEW",
        }))
        # Yield enough times that everything which is going to start has started.
        # Deliberately no early break on a specific count: asserting an exact number
        # of calls made this test a tripwire for any change to set_session's payload
        # (it broke when Runtime.enable was dropped, and again when the foreground
        # lease was added) while never testing the property it exists to protect.
        for _ in range(50):
            await asyncio.sleep(0)
        peak = d.cdp.max_concurrent
        d.cdp.release.set()
        await handle_task
        return peak, d.cdp.calls

    peak, calls = asyncio.run(run())
    # A LOWER bound, not an exact count: the gather's branches (Network.disable on OLD
    # + the enables on NEW) must all be in flight together, and sequential code would
    # peak at 1. Stated as ">=" so that adding work to set_session — the lease did —
    # can only satisfy it further, while a regression to sequential awaits still fails.
    # Not `== len(calls)`: _enable_default_domains legitimately runs realness AFTER its
    # enables, so some calls are correctly not concurrent with the rest.
    assert peak >= 4, (
        f"set_session must issue Network.disable and the enables concurrently via gather "
        f"(observed peak in-flight = {peak} across {len(calls)} calls). "
        f"Sequential await would peak at 1."
    )
    # Sanity: the right calls were made.
    methods = sorted({m for (m, _p, _s) in calls})
    assert "Network.disable" in methods
    assert {"Page.enable", "DOM.enable", "Network.enable"}.issubset(methods)
    # The new session takes the foreground lease as part of the same gather.
    assert ("Emulation.setFocusEmulationEnabled", {"enabled": True}, "session-NEW") in calls


def test_set_session_first_attach_runs_enables_in_parallel():
    """When there's no previous session, the disable path is skipped — only
    the enables run, still in parallel."""
    class _ConcurrencyProbeCDP:
        def __init__(self):
            self.calls = []
            self.in_flight = 0
            self.max_concurrent = 0
            self.release = None

        async def send_raw(self, method, params=None, session_id=None):
            self.calls.append((method, params, session_id))
            self.in_flight += 1
            self.max_concurrent = max(self.max_concurrent, self.in_flight)
            try:
                await self.release.wait()
            finally:
                self.in_flight -= 1
            return {}

    async def run():
        d = daemon.Daemon()
        d.cdp = _ConcurrencyProbeCDP()
        d.session = None  # no previous session
        d.cdp.release = asyncio.Event()

        handle_task = asyncio.create_task(d.handle({
            "meta": "set_session",
            "session_id": "session-FIRST",
            "target_id": "target-FIRST",
        }))
        for _ in range(50):
            await asyncio.sleep(0)
        peak = d.cdp.max_concurrent
        d.cdp.release.set()
        await handle_task
        return peak, d.cdp.calls

    peak, calls = asyncio.run(run())
    assert peak >= 3, (   # lower bound: the enables overlap; sequential would peak at 1
        f"first set_session must run its enables concurrently "
        f"(observed peak = {peak} across {len(calls)} calls)."
    )
    assert "Network.disable" not in {m for (m, _p, _s) in calls}, (
        "no previous session — nothing to disable"
    )


def test_current_tab_meta_passes_attached_target_id():
    """Regression for issue #304: helpers.current_tab() previously sent
    Target.getTargetInfo with no targetId. The daemon strips session_id for
    Target.* methods, so the call hit the browser-level connection with empty
    params, and Chrome returned info about the *browser* target (empty
    url/title) instead of the attached page. The daemon now resolves this
    server-side using its tracked target_id."""
    class _TargetInfoCDP(_FakeCDP):
        async def send_raw(self, method, params=None, session_id=None):
            self.calls.append((method, params, session_id))
            if method == "Target.getTargetInfo":
                return {"targetInfo": {
                    "targetId": params["targetId"],
                    "url": "https://example.com/",
                    "title": "Example Domain",
                    "type": "page",
                }}
            return {}

    d = daemon.Daemon()
    d.cdp = _TargetInfoCDP()
    d.target_id = "page-target-abc"

    result = asyncio.run(d.handle({"meta": "current_tab"}))

    assert result == {
        "targetId": "page-target-abc",
        "url": "https://example.com/",
        "title": "Example Domain",
    }
    # The targetId must be passed through — that's the whole point of the fix.
    get_info_calls = [(p, s) for (m, p, s) in d.cdp.calls if m == "Target.getTargetInfo"]
    assert get_info_calls == [({"targetId": "page-target-abc"}, None)]


def test_current_tab_meta_returns_not_attached_when_no_target_id():
    """Without an attached page, current_tab() has no meaningful answer.
    Returning {error: not_attached} causes _send() to raise in helpers, which
    is the right signal for callers like ensure_real_tab() that wrap the call
    in try/except."""
    d = _fresh_daemon()
    d.target_id = None

    result = asyncio.run(d.handle({"meta": "current_tab"}))

    assert result == {"error": "not_attached"}
    # No CDP call should have been issued.
    assert d.cdp.calls == []


class _ScriptedCDP:
    """send_raw fake with per-method canned responses, recording every call."""

    def __init__(self, responses=None):
        self.calls = []  # list of (method, params, session_id)
        self.responses = responses or {}

    async def send_raw(self, method, params=None, session_id=None):
        self.calls.append((method, params, session_id))
        r = self.responses.get(method, {})
        return r(params, session_id) if callable(r) else r


_SW_TARGETS = {"targetInfos": [
    {"targetId": "PAGE1", "type": "page", "url": "https://example.com"},
    {"targetId": "SW1", "type": "service_worker", "url": "chrome-extension://abc/background.js"},
]}


def test_activate_target_routes_through_extension_never_native():
    """THE invariant: Target.activateTarget must never reach Chrome natively
    when the tab-grouper extension is present — the native call fires
    [NSApp activate] on macOS and steals the operator's focus. The daemon
    rewrites it to the extension's activateTab (chrome.tabs.update)."""
    d = daemon.Daemon()
    d.cdp = _ScriptedCDP({
        "Target.getTargets": _SW_TARGETS,
        "Target.attachToTarget": {"sessionId": "sw-session"},
        "Runtime.evaluate": {"result": {"value": 42}},
    })

    resp = asyncio.run(d.handle({"method": "Target.activateTarget", "params": {"targetId": "PAGE1"}}))

    assert resp == {"result": {}}
    methods = [m for (m, _p, _s) in d.cdp.calls]
    assert "Target.activateTarget" not in methods, (
        f"native activateTarget must not fire when the extension is live. Calls: {methods}"
    )
    evals = [(p, s) for (m, p, s) in d.cdp.calls if m == "Runtime.evaluate"]
    assert evals and evals[0][1] == "sw-session"
    assert 'self.activateTab("PAGE1")' in evals[0][0]["expression"]
    assert ("Target.detachFromTarget", {"sessionId": "sw-session"}, None) in d.cdp.calls


def test_activate_target_surfaces_extension_error_without_native_fallback():
    """If the extension answers but refuses (no live tab for the target id),
    the daemon must return that error — not fall through to the native,
    focus-stealing call."""
    d = daemon.Daemon()
    d.cdp = _ScriptedCDP({
        "Target.getTargets": _SW_TARGETS,
        "Target.attachToTarget": {"sessionId": "sw-session"},
        "Runtime.evaluate": {"result": {}, "exceptionDetails": {
            "exception": {"description": "Error: no tab for CDP target DEAD1"},
        }},
    })

    resp = asyncio.run(d.handle({"method": "Target.activateTarget", "params": {"targetId": "DEAD1"}}))

    assert "error" in resp and "no tab for CDP target" in resp["error"]
    methods = [m for (m, _p, _s) in d.cdp.calls]
    assert "Target.activateTarget" not in methods


def test_activate_target_falls_back_to_native_without_extension():
    """On a browser without the tab-grouper extension the raw capability stays
    alive: the daemon forwards the native call (and logs the focus steal)."""
    d = daemon.Daemon()
    d.cdp = _ScriptedCDP({
        "Target.getTargets": {"targetInfos": [
            {"targetId": "PAGE1", "type": "page", "url": "https://example.com"},
        ]},
    })

    resp = asyncio.run(d.handle({"method": "Target.activateTarget", "params": {"targetId": "PAGE1"}}))

    assert resp == {"result": {}}
    assert ("Target.activateTarget", {"targetId": "PAGE1"}, None) in d.cdp.calls


def _scripted_for_attach(targets, monkeypatch, bound=None, label="", tracked=(), claimed=None):
    monkeypatch.setattr(daemon, "_bound_target", lambda: bound)
    monkeypatch.setattr(daemon, "_session_label", lambda: label)
    monkeypatch.setattr(daemon, "_tracked_tabs", lambda: list(tracked))
    monkeypatch.setattr(daemon, "_remember_target", lambda tid: remembered.append(tid))
    # attach_first_page's mint path calls _track_target directly. Unpatched, that writes the
    # DEVELOPER's ~/.config/horse-browser/tabs/default — a real registry the reaper then acts
    # on. Caught by running the suite on a clean machine, where the junk was suddenly visible.
    if claimed is None:
        monkeypatch.setattr(daemon, "_track_target", lambda tid: None)
        monkeypatch.setattr(daemon, "_all_tracked", set)
    remembered.clear()
    d = daemon.Daemon()
    # start() probes this; these tests call attach_first_page directly. Default to a WATCHED
    # browser (extension present) because that is what most of them are about — the unattended
    # case gets its own test below.
    d.has_extension = True
    created = {"n": 0}

    def create(_params, _sid):
        created["n"] += 1
        return {"targetId": "NEW-BLANK"}
    d.cdp = _ScriptedCDP({
        "Target.getTargets": {"targetInfos": targets},
        "Target.createTarget": create,
        "Target.attachToTarget": {"sessionId": "attach-session"},
        "Runtime.evaluate": {"result": {"value": 1}},
    })
    return d, created


remembered = []

_FOREIGN_PAGE = {"targetId": "NEIGHBOUR", "type": "page", "url": "https://neighbour.example"}


def test_attach_prefers_bound_tab(monkeypatch):
    """With a persisted binding alive, the daemon re-attaches THAT tab — never a guess."""
    own = {"targetId": "MYTAB", "type": "page", "url": "https://mine.example"}
    d, created = _scripted_for_attach([_FOREIGN_PAGE, own, _SW_TARGETS["targetInfos"][1]],
                                      monkeypatch, bound="MYTAB", label="sess-1")

    pick = asyncio.run(d.attach_first_page())

    assert pick["targetId"] == "MYTAB"
    assert d.target_id == "MYTAB"
    assert created["n"] == 0
    assert remembered == ["MYTAB"]


def test_attach_with_identity_never_grabs_foreign_tab(monkeypatch):
    """THE isolation invariant: a session with identity whose bound tab is gone must
    mint (and group) its own about:blank — never attach a neighbour's page."""
    d, created = _scripted_for_attach([_FOREIGN_PAGE, _SW_TARGETS["targetInfos"][1]],
                                      monkeypatch, bound="GONE", label="sess-1")

    pick = asyncio.run(d.attach_first_page())

    assert pick["targetId"] == "NEW-BLANK"
    assert created["n"] == 1
    attaches = [p for (m, p, _s) in d.cdp.calls if m == "Target.attachToTarget"]
    assert {"targetId": "NEIGHBOUR", "flatten": True} not in attaches
    evals = [p["expression"] for (m, p, _s) in d.cdp.calls if m == "Runtime.evaluate"]
    assert any("groupTab" in e and "NEW-BLANK" in e and "sess-1" in e for e in evals), evals
    creates = [p for (m, p, _s) in d.cdp.calls if m == "Target.createTarget"]
    assert creates == [{"url": "about:blank", "background": True}]


def test_attach_reuses_an_own_registry_tab_before_minting(monkeypatch):
    """The blank-tab-pile bug: a dead binding must not mint while the session still
    owns a live tab. Every closed tab (operator, reap race, crash) used to become a
    fresh about:blank that no reaper could attribute to anyone."""
    mine = {"targetId": "MINE-2", "type": "page", "url": "about:blank"}
    d, created = _scripted_for_attach(
        [_FOREIGN_PAGE, mine, _SW_TARGETS["targetInfos"][1]],
        monkeypatch, bound="GONE", label="sess-1", tracked=["MINE-1", "MINE-2"])

    pick = asyncio.run(d.attach_first_page())

    assert pick["targetId"] == "MINE-2"          # newest live tab in the registry
    assert created["n"] == 0                     # …and nothing minted
    assert remembered == ["MINE-2"]


def test_attach_still_mints_when_every_registry_tab_is_dead(monkeypatch):
    """Reuse must not weaken isolation: with no live tab of ours, mint — never adopt
    the neighbour's page that happens to be sitting there."""
    d, created = _scripted_for_attach([_FOREIGN_PAGE, _SW_TARGETS["targetInfos"][1]],
                                      monkeypatch, bound="GONE", label="sess-1",
                                      tracked=["DEAD-1", "DEAD-2"])

    pick = asyncio.run(d.attach_first_page())

    assert pick["targetId"] == "NEW-BLANK"
    assert created["n"] == 1


def test_attach_never_reuses_a_registry_tab_that_is_not_a_page(monkeypatch):
    """A stale registry entry that now names a worker/iframe target must not be attached
    to as if it were the session's page."""
    worker = {"targetId": "MINE-2", "type": "service_worker", "url": "chrome-extension://x/sw.js"}
    d, created = _scripted_for_attach([worker, _SW_TARGETS["targetInfos"][1]],
                                      monkeypatch, bound="GONE", label="sess-1",
                                      tracked=["MINE-2"])

    pick = asyncio.run(d.attach_first_page())

    assert pick["targetId"] == "NEW-BLANK"
    assert created["n"] == 1


def test_attach_adopts_the_new_tab_page_instead_of_minting(tmp_path, monkeypatch):
    """Chrome opens its own New Tab Page at launch. Minting beside it means the browser
    sits at TWO idle tabs — the dead one and ours. Take it over instead."""
    ntp = {"targetId": "NTP", "type": "page", "url": "chrome://newtab/"}
    _registry(tmp_path, monkeypatch, [])
    d, created = _scripted_for_attach([ntp, _SW_TARGETS["targetInfos"][1]],
                                      monkeypatch, bound=None, label="sess-1", claimed=True)

    pick = asyncio.run(d.attach_first_page())

    assert pick["targetId"] == "NTP"
    assert created["n"] == 0
    evals = [p["expression"] for (m, p, _s) in d.cdp.calls if m == "Runtime.evaluate"]
    assert any("groupTab" in e and "NTP" in e for e in evals), evals   # painted into our group


def test_adopted_new_tab_page_is_blanked(tmp_path, monkeypatch):
    """A New Tab Page is not inert — it keeps fetching (one-google-bar and friends). Adopting
    one without blanking hands the session a tab busier than the about:blank it replaced,
    which showed up as extra flake in the multi-instance suite."""
    ntp = {"targetId": "NTP", "type": "page", "url": "chrome://newtab/"}
    _registry(tmp_path, monkeypatch, [])
    d, _created = _scripted_for_attach([ntp, _SW_TARGETS["targetInfos"][1]],
                                       monkeypatch, bound=None, label="sess-1", claimed=True)

    asyncio.run(d.attach_first_page())

    navs = [p for (m, p, _s) in d.cdp.calls if m == "Page.navigate"]
    assert navs == [{"url": "about:blank"}], navs


def test_minted_tab_is_not_navigated(tmp_path, monkeypatch):
    """The mint path already creates about:blank — a second navigation would be a wasted
    round trip on the hot startup path."""
    _registry(tmp_path, monkeypatch, [])
    d, created = _scripted_for_attach([_FOREIGN_PAGE, _SW_TARGETS["targetInfos"][1]],
                                      monkeypatch, bound="GONE", label="sess-1", claimed=True)

    asyncio.run(d.attach_first_page())

    assert created["n"] == 1
    assert [m for (m, _p, _s) in d.cdp.calls if m == "Page.navigate"] == []


def test_attach_never_adopts_a_new_tab_page_another_session_claimed(tmp_path, monkeypatch):
    """Adoption must not take a tab someone else is driving — the registry is the check."""
    ntp = {"targetId": "NTP", "type": "page", "url": "chrome://new-tab-page/"}
    _registry(tmp_path, monkeypatch, [])
    (tmp_path / "neighbour").write_text(json.dumps(["NTP"]))     # another session owns it
    d, created = _scripted_for_attach([ntp, _SW_TARGETS["targetInfos"][1]],
                                      monkeypatch, bound=None, label="sess-1", claimed=True)

    pick = asyncio.run(d.attach_first_page())

    assert pick["targetId"] == "NEW-BLANK"
    assert created["n"] == 1


def test_unattended_browser_is_not_asked_to_paint_a_tab_group(tmp_path, monkeypatch):
    """No extension means no group to paint into. Asking anyway costs four CDP round trips
    discovering the absence, then logs a failure — on every tab minted, for every session.
    The tab is ours either way: the registry said so, and a group only RENDERS that."""
    _registry(tmp_path, monkeypatch, [])
    d, created = _scripted_for_attach([_FOREIGN_PAGE, _SW_TARGETS["targetInfos"][1]],
                                      monkeypatch, bound="GONE", label="sess-1", claimed=True)
    d.has_extension = False                     # unattended, or a browser we did not launch

    pick = asyncio.run(d.attach_first_page())

    assert created["n"] == 1                    # the tab is still minted…
    assert remembered == [pick["targetId"]]     # …and still claimed in the registry
    evals = [p["expression"] for (m, p, _s) in d.cdp.calls if m == "Runtime.evaluate"]
    assert not [e for e in evals if "groupTab" in e], evals


def test_attach_without_identity_keeps_legacy_first_page(monkeypatch):
    d, created = _scripted_for_attach([_FOREIGN_PAGE], monkeypatch, bound=None, label="")

    pick = asyncio.run(d.attach_first_page())

    assert pick["targetId"] == "NEIGHBOUR"
    assert created["n"] == 0


def test_anchor_alive_true_for_live_pid_and_false_for_dead(monkeypatch):
    import os
    monkeypatch.setattr(daemon, "ANCHOR_PID", str(os.getpid()))
    monkeypatch.setattr(daemon, "ANCHOR_START", "")
    assert daemon._anchor_alive() is True

    # find a PID that doesn't exist
    pid = 99999
    while True:
        try:
            os.kill(pid, 0)
            pid -= 1
        except ProcessLookupError:
            break
        except OSError:
            pid -= 1
    monkeypatch.setattr(daemon, "ANCHOR_PID", str(pid))
    assert daemon._anchor_alive() is False


def _tagged_start(pid):
    """BH_ANCHOR_START exactly as detect.sh writes it on this platform."""
    import sys as _s
    from horse_harness.lifecycle import _process_start_time
    return ("linux:" if _s.platform.startswith("linux") else "darwin:") + str(_process_start_time(pid))


def test_anchor_alive_accepts_its_own_platform_fingerprint(monkeypatch):
    """THE Linux data-loss bug: detect.sh wrote `ps -o lstart=` on every platform, while
    _process_start_time returns /proc start-ticks on Linux. The two could never be equal, so
    every live Linux session looked PID-reused and the watchdog closed its tabs ~32s in."""
    import os
    monkeypatch.setattr(daemon, "ANCHOR_PID", str(os.getpid()))
    monkeypatch.setattr(daemon, "ANCHOR_START", _tagged_start(os.getpid()))
    assert daemon._anchor_alive() is True


def test_anchor_alive_treats_an_incomparable_fingerprint_as_no_evidence(monkeypatch):
    """An untagged value (an older install's exported env surviving an upgrade) or one from
    the other platform must mean "cannot tell", never "dead". This check exists to catch PID
    reuse — rare and recoverable — so "I cannot compare these" must not cost a live agent
    its tabs."""
    import os
    monkeypatch.setattr(daemon, "ANCHOR_PID", str(os.getpid()))
    for stale in ("Sun Aug  3 04:11:22 2026",          # legacy, untagged
                  "linux:14454660" if not __import__("sys").platform.startswith("linux")
                  else "darwin:Sun Aug  3 04:11:22 2026"):   # the other platform's dialect
        monkeypatch.setattr(daemon, "ANCHOR_START", stale)
        assert daemon._anchor_alive() is True, stale


def test_anchor_alive_still_detects_pid_reuse_within_one_dialect(monkeypatch):
    """The fix must not blunt the check it guards: same platform tag, genuinely different
    value, still means the PID was recycled."""
    import os, sys as _s
    monkeypatch.setattr(daemon, "ANCHOR_PID", str(os.getpid()))
    tag = "linux:" if _s.platform.startswith("linux") else "darwin:"
    monkeypatch.setattr(daemon, "ANCHOR_START", tag + "definitely-not-this-process")
    assert daemon._anchor_alive() is False


def test_anchor_alive_unverifiable_defaults_to_alive(monkeypatch):
    monkeypatch.setattr(daemon, "ANCHOR_PID", None)
    assert daemon._anchor_alive() is True
    monkeypatch.setattr(daemon, "ANCHOR_PID", "not-a-pid")
    assert daemon._anchor_alive() is True


def test_reap_own_tabs_never_asks_the_extension(tmp_path, monkeypatch):
    # Self-reap runs when things have already gone wrong (the anchor died). Routing it
    # through the extension made the cleanup you most need depend on a service worker
    # being awake and an earlier groupTab having worked. It reads the registry now, and
    # a live, answering extension must not change the outcome by one tab.
    monkeypatch.setattr(daemon, "_session_label", lambda: "sess-1")
    _registry(tmp_path, monkeypatch, ["T1", "T2"])
    d = daemon.Daemon()
    d.cdp = _ScriptedCDP({
        "Target.getTargets": _SW_TARGETS,
        "Target.attachToTarget": {"sessionId": "sw-session"},
        "Runtime.evaluate": {"result": {"value": [{"targetId": "FROM-EXT"}]}},
    })

    asyncio.run(d._reap_own_tabs())

    closed = [p for (m, p, _s) in d.cdp.calls if m == "Target.closeTarget"]
    assert closed == [{"targetId": "T1"}, {"targetId": "T2"}]
    assert not [m for (m, _p, _s) in d.cdp.calls if m == "Runtime.evaluate"]


# ── the blank sweep ── an idle session gives back the tabs that hold nothing ────────


_OWN_AND_FOREIGN = {"targetInfos": [
    {"targetId": "MINE-BLANK", "type": "page", "url": "about:blank"},
    {"targetId": "MINE-WORK", "type": "page", "url": "https://example.com/"},
    {"targetId": "BOUND-BLANK", "type": "page", "url": ""},
    {"targetId": "THEIRS-BLANK", "type": "page", "url": "about:blank"},
    {"targetId": "SW1", "type": "service_worker", "url": ""},
]}


def test_blank_sweep_closes_only_the_sessions_own_blank_tabs(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, ["MINE-BLANK", "MINE-WORK", "ALREADY-GONE"])
    d = daemon.Daemon()
    d.target_id = "BOUND-BLANK"
    d.cdp = _ScriptedCDP({"Target.getTargets": _OWN_AND_FOREIGN})

    closed = asyncio.run(d._close_own_blank_tabs())

    assert sorted(closed) == ["BOUND-BLANK", "MINE-BLANK"]
    assert [p["targetId"] for (m, p, _s) in d.cdp.calls if m == "Target.closeTarget"] == closed


def test_blank_sweep_reports_only_what_actually_closed(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, ["MINE-BLANK"])
    d = daemon.Daemon()

    def refuse(_params, _sid):
        raise RuntimeError("No target with given id found")

    d.cdp = _ScriptedCDP({"Target.getTargets": _OWN_AND_FOREIGN, "Target.closeTarget": refuse})

    assert asyncio.run(d._close_own_blank_tabs()) == []


def _run_sweeper(d, monkeypatch, ttl, idle_for):
    swept = []

    async def record():
        swept.append(True)
        d.stop.set()

    async def go():
        d.stop = asyncio.Event()
        d.last_request = daemon.time.monotonic() - idle_for
        d._close_own_blank_tabs = record
        try:
            await asyncio.wait_for(d._blank_sweeper(interval=0.01), timeout=0.2)
        except asyncio.TimeoutError:
            pass

    monkeypatch.setattr(daemon, "BLANK_TTL", ttl)
    asyncio.run(go())
    return bool(swept)


def test_blank_sweeper_waits_for_the_session_to_fall_silent(monkeypatch):
    assert _run_sweeper(daemon.Daemon(), monkeypatch, ttl=60, idle_for=5) is False


def test_blank_sweeper_sweeps_once_the_session_has_been_silent_for_the_ttl(monkeypatch):
    assert _run_sweeper(daemon.Daemon(), monkeypatch, ttl=60, idle_for=61) is True


def test_a_ping_does_not_count_as_the_session_calling(monkeypatch):
    d = _fresh_daemon()
    d.last_request = 1.0

    asyncio.run(d.handle({"meta": "ping"}))
    assert d.last_request == 1.0

    asyncio.run(d.handle({"meta": "current_tab"}))
    assert d.last_request > 1.0


# ── the tab registry ── the daemon mints tabs of its own, so it records them too ────


def _registry(tmp_path, monkeypatch, tracked=None):
    f = tmp_path / "tabs"
    if tracked is not None:
        f.write_text(json.dumps(tracked))
    monkeypatch.setattr(daemon, "_tabs_file", lambda: f)
    monkeypatch.setattr(daemon, "_bound_file", lambda: tmp_path / "current")
    return f


def test_remember_target_also_records_the_tab_in_the_registry(tmp_path, monkeypatch):
    # attach_first_page mints an about:blank and used to call only _remember_target, so
    # that tab existed ONLY in the extension's tab group — invisible, and leaked, the
    # moment there is no extension to ask.
    f = _registry(tmp_path, monkeypatch)
    daemon._remember_target("T-minted")
    assert json.loads(f.read_text()) == ["T-minted"]
    assert (tmp_path / "current").read_text() == "T-minted"     # binding still written


def test_watchdog_keeps_the_registry_when_the_reap_failed(tmp_path, monkeypatch):
    """The registry is the ONLY record of a session's tabs. Deleting it after a failed
    close leaves those tabs alive and unattributable — an orphan no sweep can reach."""
    f = _registry(tmp_path, monkeypatch, ["T1", "T2"])
    monkeypatch.setattr(daemon, "_anchor_alive", lambda: False)
    monkeypatch.setattr(daemon, "ANCHOR_PID", "999999")
    d = daemon.Daemon()

    async def boom():
        raise RuntimeError("browser unreachable")
    d._reap_own_tabs = boom

    async def go():
        d.stop = asyncio.Event()
        await d._watchdog(interval=0)
    asyncio.run(go())

    assert json.loads(f.read_text()) == ["T1", "T2"]        # kept for the launcher's sweep
    assert not (tmp_path / "current").exists()              # the binding still dies
    assert d.stop.is_set()


def test_watchdog_drops_the_registry_once_the_tabs_are_actually_closed(tmp_path, monkeypatch):
    f = _registry(tmp_path, monkeypatch, ["T1", "T2"])
    monkeypatch.setattr(daemon, "_anchor_alive", lambda: False)
    monkeypatch.setattr(daemon, "ANCHOR_PID", "999999")
    d = daemon.Daemon()

    async def ok():
        return None
    d._reap_own_tabs = ok

    async def go():
        d.stop = asyncio.Event()
        await d._watchdog(interval=0)
    asyncio.run(go())

    assert not f.exists()


def test_track_target_writes_atomically(tmp_path, monkeypatch):
    """Both the daemon and every client process write this file. A non-atomic write lets
    a reader catch it half-written, parse nothing, and collapse the registry on its own
    next write — silently orphaning every tab it listed."""
    f = _registry(tmp_path, monkeypatch, ["a"])
    daemon._track_target("b")
    assert json.loads(f.read_text()) == ["a", "b"]
    assert list(tmp_path.glob("*.tmp")) == []               # no temp file left behind


def test_track_target_moves_a_retouched_tab_to_the_end(tmp_path, monkeypatch):
    f = _registry(tmp_path, monkeypatch, ["a", "b", "c"])
    daemon._track_target("a")
    assert json.loads(f.read_text()) == ["b", "c", "a"]         # order stands in for recency


def test_reap_own_tabs_falls_back_to_the_registry_with_no_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_session_label", lambda: "sess-1")
    _registry(tmp_path, monkeypatch, ["T1", "T2"])
    d = daemon.Daemon()
    d.target_id = "T3"                                          # bound tab, not yet tracked
    d.cdp = _ScriptedCDP({"Target.getTargets": {"targetInfos": []}})   # no SW → unreachable

    asyncio.run(d._reap_own_tabs())

    closed = [p for (m, p, _s) in d.cdp.calls if m == "Target.closeTarget"]
    assert closed == [{"targetId": "T1"}, {"targetId": "T2"}, {"targetId": "T3"}]


def test_reap_own_tabs_closes_nothing_when_nothing_is_known(tmp_path, monkeypatch):
    # The safety stop: no extension AND an empty registry must not fall through to
    # something broader (every page target, say) — an attached browser's other tabs
    # are not ours to close.
    monkeypatch.setattr(daemon, "_session_label", lambda: "sess-1")
    _registry(tmp_path, monkeypatch, [])
    d = daemon.Daemon()
    d.cdp = _ScriptedCDP({"Target.getTargets": {"targetInfos": [
        {"targetId": "SOMEONE-ELSE", "type": "page", "url": "https://example.com"},
    ]}})

    asyncio.run(d._reap_own_tabs())

    assert [p for (m, p, _s) in d.cdp.calls if m == "Target.closeTarget"] == []


# ── realness: applied over CDP only where there is no extension to do it ────────────


def _realness_daemon(has_extension):
    d = daemon.Daemon()
    d.has_extension = has_extension
    d.cdp = _ScriptedCDP({"Browser.getVersion": {"userAgent": "Mozilla/5.0 Chrome/151.0.0.0"}})
    asyncio.run(d._apply_realness("sess-x"))
    return [m for (m, _p, _s) in d.cdp.calls]


def test_realness_is_not_applied_when_the_extension_is_there():
    # The extension masks browser-wide, with zero clients attached, including the operator's
    # own tabs. Layering a per-session override on top would add a second applier for one
    # effect and buy nothing.
    assert _realness_daemon(True) == []


def test_realness_is_applied_over_cdp_when_there_is_no_extension():
    calls = _realness_daemon(False)
    assert "Page.addScriptToEvaluateOnNewDocument" in calls   # JS half
    assert "Network.setExtraHTTPHeaders" in calls             # wire half


def test_realness_wire_half_derives_the_version_from_the_live_browser():
    d = daemon.Daemon()
    d.has_extension = False
    d.cdp = _ScriptedCDP({"Browser.getVersion": {"userAgent": "Mozilla/5.0 Chrome/168.0.7.9"}})
    asyncio.run(d._apply_realness("sess-x"))
    hdr = next(p for (m, p, _s) in d.cdp.calls if m == "Network.setExtraHTTPHeaders")
    v = hdr["headers"]["sec-ch-ua"]
    assert '"Google Chrome";v="168"' in v and '"Chromium";v="168"' in v, v
    assert "151" not in v, "version must come from the browser, never a stored constant"


def test_realness_injects_the_same_source_the_extension_uses():
    # One implementation of the masking logic, two delivery mechanisms. If this ever reads a
    # Python-side copy instead, the two will drift and only one of them gets fixed.
    d = daemon.Daemon()
    d.has_extension = False
    d.cdp = _ScriptedCDP({"Browser.getVersion": {"userAgent": "Chrome/151.0.0.0"}})
    asyncio.run(d._apply_realness("sess-x"))
    src = next(p for (m, p, _s) in d.cdp.calls if m == "Page.addScriptToEvaluateOnNewDocument")
    assert "userAgentData" in src["source"] and "Google Chrome" in src["source"]
    assert src["runImmediately"] is True      # also cover an already-loaded tab


# ── the extension we talk to must be OURS ───────────────────────────────────────────
# Every Chrome runs component extensions of its own: a freshly created, empty profile
# already has Google's (nkeimhogjdpnpccoofpliimaahmaaome) with a live service worker.
# "The first chrome-extension:// worker" therefore finds a stranger's at least as often
# as ours — which read as "the extension refused", and, once the realness probe existed,
# as "an extension is present" on browsers carrying none of ours. Found by pointing the
# harness at a bare Chrome for the first time (tests/attached-mode.sh).

_STRANGER = {"targetId": "SW-GOOGLE", "type": "service_worker",
             "url": "chrome-extension://nkeimhogjdpnpccoofpliimaahmaaome/thunk.js"}
_OURS = {"targetId": "SW-OURS", "type": "service_worker",
         "url": "chrome-extension://ourgrouper/background.js"}


def _sw_cdp(targets, ours_session="sess-SW-OURS", seen=None):
    def attach(params, _sid):
        return {"sessionId": "sess-" + params["targetId"]}

    def evaluate(params, sid):
        if seen is not None:
            seen.append((sid, params["expression"]))
        if sid == ours_session:
            return {"result": {"value": "REAL-ANSWER"}}
        return {"result": {"value": daemon._EXT_NOT_MINE}}

    return _ScriptedCDP({"Target.getTargets": {"targetInfos": targets},
                         "Target.attachToTarget": attach,
                         "Runtime.evaluate": evaluate})


def test_ext_eval_walks_past_a_stranger_extension_to_ours():
    seen = []
    d = daemon.Daemon()
    d.cdp = _sw_cdp([_STRANGER, _OURS], seen=seen)

    assert asyncio.run(d._ext_eval("self.groupTab('t','s')")) == ("ok", "REAL-ANSWER")
    assert [s for (s, _e) in seen] == ["sess-SW-GOOGLE", "sess-SW-OURS"]


def test_ext_eval_never_runs_the_expression_in_a_stranger():
    # The guard has to be in the SAME evaluate as the call, not a probe beforehand:
    # a two-step check would still run groupTab in whichever worker answered first.
    seen = []
    d = daemon.Daemon()
    d.cdp = _sw_cdp([_STRANGER, _OURS], seen=seen)
    asyncio.run(d._ext_eval("self.groupTab('t','s')"))

    for _sid, expr in seen:
        assert expr.startswith("(typeof self.groupTab==="), expr
        assert daemon._EXT_NOT_MINE in expr, "no sentinel branch — expression runs unguarded"


def test_ext_eval_is_unreachable_when_only_strangers_are_present():
    # THE attached-mode case. Returning "ok" here is what made the daemon believe an
    # extension was present on a bare browser and skip the realness mask entirely.
    d = daemon.Daemon()
    d.cdp = _sw_cdp([_STRANGER])
    status, _why = asyncio.run(d._ext_eval("self.groupTab('t','s')"))
    assert status == "unreachable"


def test_ext_eval_is_unreachable_with_no_workers_at_all():
    d = daemon.Daemon()
    d.cdp = _sw_cdp([])
    assert asyncio.run(d._ext_eval("1"))[0] == "unreachable"


def test_self_reap_reports_failure_when_closes_fail(tmp_path, monkeypatch):
    """The real orphan path: the anchor dies while CDP is already disconnected, so every
    closeTarget raises. _silent() used to swallow them all, _reap_own_tabs returned normally,
    and the watchdog then DELETED the registry — leaving those tabs open and unattributable,
    which is the one thing the registry exists to prevent. The existing watchdog test patches
    _reap_own_tabs itself to raise, so it never exercised this."""
    _registry(tmp_path, monkeypatch, ["T1", "T2"])
    monkeypatch.setattr(daemon, "_session_label", lambda: "sess-1")

    class _DeadCDP:
        calls = []
        async def send_raw(self, method, params=None, session_id=None):
            self.calls.append(method)
            raise RuntimeError("CDP socket closed")

    d = daemon.Daemon(); d.cdp = _DeadCDP(); d.target_id = None
    try:
        asyncio.run(d._reap_own_tabs())
    except RuntimeError as e:
        assert "could not be closed" in str(e)
    else:
        raise AssertionError("a total close failure was reported as success")
    assert json.loads((tmp_path / "tabs").read_text()) == ["T1", "T2"], \
        "the registry must survive so the launcher's sweep can still find these tabs"


def test_ntp_adoption_decides_and_claims_entirely_inside_the_lock(tmp_path, monkeypatch):
    """THE race: two cold lanes both snapshot targets, both read the registries before either
    writes, both see the New Tab Page unclaimed, both adopt it — two sessions driving one tab,
    either watchdog free to close the other's work.

    The fix is not "check again", it is "never decide on evidence gathered outside the lock".
    So assert exactly that: the target list is re-fetched and the registries are read only
    while the lock is held, and the claim is written before it is released. A snapshot taken
    before the lock is the stale evidence that causes this bug."""
    ntp = {"targetId": "NTP", "type": "page", "url": "chrome://newtab/"}
    _registry(tmp_path, monkeypatch, [])
    d, created = _scripted_for_attach([ntp, _SW_TARGETS["targetInfos"][1]],
                                      monkeypatch, bound=None, label="sess-1", claimed=True)

    events = []
    import contextlib as _c

    @_c.contextmanager
    def _watched_lock():
        events.append("lock-acquired")
        try:
            yield True
        finally:
            events.append("lock-released")
    monkeypatch.setattr(daemon, "_adopt_lock", _watched_lock)
    monkeypatch.setattr(daemon, "_all_tracked", lambda: events.append("read-registries") or set())
    real_track = daemon._track_target
    monkeypatch.setattr(daemon, "_track_target",
                        lambda tid: (events.append("claim:" + tid), real_track(tid))[1])

    pick = asyncio.run(d.attach_first_page())
    assert pick["targetId"] == "NTP" and created["n"] == 0

    held = events[events.index("lock-acquired"):events.index("lock-released")]
    assert "read-registries" in held, f"registries read outside the lock: {events}"
    assert any(e.startswith("claim:NTP") for e in held), f"claim written outside the lock: {events}"
    # …and the targets the decision used were fetched fresh, not reused from before the lock.
    # >= 2: one before the lock (the bound/registry passes need it) and at least one inside,
    # which is the point — the decision must not reuse the pre-lock snapshot.
    assert len([m for (m, _p, _s) in d.cdp.calls if m == "Target.getTargets"]) >= 2, \
        "the decision reused the pre-lock snapshot"


def test_ntp_adoption_is_skipped_when_it_cannot_be_serialised(tmp_path, monkeypatch):
    """No lock means no way to know we are alone. Minting costs one extra tab; adopting on a
    guess costs two sessions on one page — so the unserialisable case must mint."""
    ntp = {"targetId": "NTP", "type": "page", "url": "chrome://newtab/"}
    _registry(tmp_path, monkeypatch, [])
    d, created = _scripted_for_attach([ntp, _SW_TARGETS["targetInfos"][1]],
                                      monkeypatch, bound=None, label="sess-1", claimed=True)

    import contextlib as _c
    @_c.contextmanager
    def _no_lock():
        yield False
    monkeypatch.setattr(daemon, "_adopt_lock", _no_lock)

    pick = asyncio.run(d.attach_first_page())

    assert pick["targetId"] == "NEW-BLANK"
    assert created["n"] == 1
