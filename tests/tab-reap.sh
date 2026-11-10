#!/usr/bin/env bash
# tests/tab-reap.sh — tab-leak characterization + the orphan-tab reaper.
#
# Leaks that accumulate in the shared browser: a tab-less session's first CDP read makes
# _hb_home create an about:blank it never closes, and a process killed mid-open abandons its
# tab. Both leave tabs behind once the agent SESSION has ended. reap_orphan_tabs closes the
# tabs named by any registry with no live daemon behind it — verified here to clean the dead,
# spare the live, take the registry file with it, and never touch a claimed tab.
#
# Tab counts are measured BY TITLE over /json/list: neither the extension nor the registry,
# so the reaper's input and the test's yardstick can't fail together and still pass.
set -u

# A test run must never reach the operator's ~/.claude or ~/.grok. 16 of 19 suites once
# lacked this, so `npm test` from ANY clone wired that clone's path into the real global
# settings.json — which is how a build agent's throwaway checkout came to leave a dead
# hook behind that failed every Bash call on the machine. external-state.sh is the one
# suite that unsets this, against temp paths of its own.
export HORSE_BROWSER_NO_RECONCILE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HB="${HB:-$HERE/../bin/horse-browser}"
[ -x "$HB" ] || HB="$(command -v horse-browser || true)"
[ -x "$HB" ] || { echo "FATAL: horse-browser not found (set HB=…)"; exit 1; }
# Disposable instance so this suite never touches the live :9223 browser. HB_ISOLATE=0 opts out.
source "$HERE/lib/isolate.sh"; hb_isolate || exit 1
PORT="${HORSE_BROWSER_PORT:-$(sed -n 's/^PORT=//p' "$HOME/.config/horse-browser/config" 2>/dev/null | head -1 | tr -d '"')}"
PORT="${PORT:-9223}"
PASS=0; FAIL=0; SKIP=0; FAILED=()
say()  { printf '%s\n' "$*"; }
pass() { PASS=$((PASS+1)); say "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); say "  ✗ $1${2:+ — $2}"; }
skip() { SKIP=$((SKIP+1)); say "  - $1 (skipped${2:+: $2})"; }

"$HB" >/dev/null 2>&1 || { say "FATAL: browser did not come up"; exit 1; }

# All this test's own CDP-reading helpers run under ONE disposable session so their _hb_home
# blanks land in a single group we tear down at the end — the test never litters.
HSESS="reaptest-$$-helper"
cleanup() {
  for p in $(pgrep -f "(browser|horse)_harness.daemon"); do
    ps eww -o command= -p "$p" 2>/dev/null | tr ' ' '\n' | grep -qE "^HORSE_SESSION=reaptest-$$" && kill "$p" 2>/dev/null
  done
  sleep 1                 # let the kills propagate so the reaper sees these sessions as dead
  reap >/dev/null 2>&1 || true
  _hb_isolate_teardown
}
trap cleanup EXIT

blanks() { curl -s "http://127.0.0.1:$PORT/json/list" \
  | python3 -c "import json,sys; print(sum(1 for t in json.load(sys.stdin) if t.get('type')=='page' and (t.get('url') or '') in ('','about:blank')))"; }
titled() {  # count page tabs whose title contains $1 — measured over plain CDP HTTP, so it
            # depends on NEITHER the extension nor the registry. The reaper's inputs and its
            # yardstick must not be the same thing, or the test passes when both are broken.
  curl -s "http://127.0.0.1:$PORT/json/list" \
  | python3 -c "import json,sys; print(sum(1 for t in json.load(sys.stdin) if t.get('type')=='page' and '$1' in (t.get('title') or '')))"; }
daemon_pid_for() {  # echo the daemon pid whose HORSE_SESSION == $1
  for p in $(pgrep -f "(browser|horse)_harness.daemon"); do
    ps eww -o command= -p "$p" 2>/dev/null | tr ' ' '\n' | grep -qx "HORSE_SESSION=$1" && { echo "$p"; return; }
  done
}
ung_blanks() {  # count UNGROUPED, non-pinned about:blank tabs (the stray-blank pile)
  HORSE_SESSION="$HSESS" "$HB" <<PY 2>/dev/null | sed -n 's/^UB //p'
sw = next(t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type")=="service_worker" and t.get("url","").startswith("chrome-extension://"))
s = cdp("Target.attachToTarget", targetId=sw, flatten=True)["sessionId"]
expr = "(async()=>{const t=await chrome.tabs.query({});return t.filter(x=>x.groupId<0 && !x.pinned && ((x.url||'')===''||x.url==='about:blank')).length;})()"
print("UB", cdp("Runtime.evaluate", session_id=s, expression=expr, awaitPromise=True, returnByValue=True).get("result",{}).get("value"))
PY
}
make_ungrouped_blank() {  # mint a raw ungrouped about:blank (like the daemon's attach-blank)
  curl -s -X PUT "http://127.0.0.1:$PORT/json/new?about:blank" >/dev/null 2>&1
}
# A reap that takes a stray of any age; the age gate has a check of its own in [2].
reap() { HORSE_BROWSER_STRAY_AGE="${STRAY_AGE:-0}" HORSE_BROWSER_REAP_INTERVAL=0 HORSE_BROWSER_REAP_STAMP="$(mktemp)" "$HB" >/dev/null 2>&1; }
make_grouped_blank() {  # a blank in a tab group that no registry claims — what a relaunch restores
  python3 - "$PORT" "$HERE/../tools" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
sys.path.insert(0, sys.argv[2]); import sw_eval
port = sys.argv[1]
req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
tid = json.load(urllib.request.urlopen(req))["id"]
sw_eval.evaluate(port, "self.groupTab(%s, 'reaptest-restored')" % json.dumps(tid), 5)
PY
}
grouped_blanks() {  # count about:blank tabs that sit in a tab group
  python3 "$HERE/../tools/sw_eval.py" "$PORT" "(async()=>{const t=await chrome.tabs.query({});return t.filter(x=>x.groupId>=0 && ((x.url||'')===''||x.url==='about:blank')).length;})()" 5 2>/dev/null
}

say "horse-browser tab-reap — port $PORT"

# Only the JANITORIAL stray sweep still goes through the extension — closing a dead
# session's own tabs is registry + plain CDP now, so those checks run unconditionally.
# (MV3 SW may serve stale bytecode until a cache wipe, hence probing rather than assuming.)
have_fn="$(HORSE_SESSION="$HSESS" "$HB" <<'PY' 2>/dev/null | sed -n 's/^FN //p'
sw = next((t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type")=="service_worker" and t.get("url","").startswith("chrome-extension://")), None)
if sw:
    s = cdp("Target.attachToTarget", targetId=sw, flatten=True)["sessionId"]
    print("FN", cdp("Runtime.evaluate", session_id=s, expression="typeof self.sweepStrayTabs", returnByValue=True).get("result",{}).get("value"))
PY
)"

# ═════ 1. leak characterization ══════════════════════════════════════════════
say "[1] leak characterization"
b0="$(blanks)"
# measure the leak in isolation — no reaping mid-measurement (it would clean other orphans
# and skew the delta); this session touches CDP without opening a tab, so _hb_home leaks a
# blank it never closes (the exact count depends on the daemon's first-page attach, so we
# assert "leaks at least one", which is the characterization that matters).
HORSE_SESSION="reaptest-$$-bare" HORSE_BROWSER_NO_REAP=1 HORSE_BROWSER_NO_TAB_REAP=1 \
  "$HB" <<<'page_info()' >/dev/null 2>&1
b1="$(blanks)"
[ "$b1" -gt "$b0" ] \
  && pass "a tab-less read session leaks a never-closed about:blank ($b0->$b1)" \
  || fail "a tab-less read session leaks a blank" "$b0->$b1"

# Adoption fix: the daemon's ungrouped attach-blank must be ADOPTED into the session group,
# not left as a stray ungrouped orphan — so a read session grows GROUPED blanks, never the
# ungrouped pile. Assert the ungrouped count doesn't rise across a fresh read session.
u0="$(ung_blanks)"
HORSE_SESSION="reaptest-$$-adopt" HORSE_BROWSER_NO_REAP=1 HORSE_BROWSER_NO_TAB_REAP=1 \
  "$HB" <<<'page_info()' >/dev/null 2>&1
u1="$(ung_blanks)"
[ "${u1:-0}" -le "${u0:-0}" ] \
  && pass "the daemon's ungrouped blank is adopted, not leaked (ungrouped $u0->$u1)" \
  || fail "daemon blank adopted, not leaked as an ungrouped stray" "ungrouped $u0->$u1"

# ═════ 2. reaper: dead vs live ═══════════════════════════════════════════════
say "[2] orphan-tab reaper"
# No extension gate: a dead session's tabs are named by its registry and closed over plain
# CDP, so this works on any browser. Measured by TITLE, independent of both.
# DEAD session: open a tab, then kill its daemon so the session reads as ended.
DEAD="reaptest-$$-dead"
HORSE_SESSION="$DEAD" "$HB" <<'PY' >/dev/null 2>&1
import urllib.parse
open_tab("data:text/html,"+urllib.parse.quote("<title>DEAD-WORK</title>x"))   # left open on purpose
wait_for_load()
PY
dpid="$(daemon_pid_for "$DEAD")"
[ -n "$dpid" ] && kill "$dpid" 2>/dev/null; sleep 1     # session now has no daemon -> ended
before_dead="$(titled DEAD-WORK)"

# LIVE session: open a tab AND keep a daemon alive for it for the duration.
LIVE="reaptest-$$-live"
HORSE_SESSION="$LIVE" "$HB" <<'PY' >/dev/null 2>&1
import urllib.parse
open_tab("data:text/html,"+urllib.parse.quote("<title>LIVE-WORK</title>x"))
wait_for_load()
PY
before_live="$(titled LIVE-WORK)"

reap    # triggers reap_orphan_daemons + reap_orphan_tabs; LIVE's daemon keeps it alive
after_dead="$(titled DEAD-WORK)"
after_live="$(titled LIVE-WORK)"

{ [ "${before_dead:-0}" -ge 1 ] && [ "${after_dead:-0}" -eq 0 ]; } \
  && pass "reaper closed the ended session's tab(s) ($before_dead->$after_dead)" \
  || fail "reaper closes an ended session's tabs" "DEAD-WORK $before_dead->$after_dead"
{ [ "${before_live:-0}" -ge 1 ] && [ "${after_live:-0}" -eq "${before_live:-0}" ]; } \
  && pass "reaper spared the live session's tab(s) ($after_live kept)" \
  || fail "reaper spares a live session's tabs" "LIVE-WORK $before_live->$after_live"

# the dead session's registry file must go with it — otherwise it accumulates forever
DBU="hb-${DEAD##*-}"
[ ! -e "$HOME/.config/horse-browser/tabs/$DBU" ] \
  && pass "dead session's registry file removed with its tabs" \
  || fail "dead session's registry file removed" "tabs/$DBU still present"

# cleanup the live session's leftover daemon + tab
lpid="$(daemon_pid_for "$LIVE")"; [ -n "$lpid" ] && kill "$lpid" 2>/dev/null
reap >/dev/null 2>&1

# a stray UNGROUPED about:blank (a daemon attach-blank that escaped adoption) must be reaped.
# This half IS extension-only: spotting a stray needs chrome.tabs, and a stray is in no registry.
if [ "$have_fn" != "function" ]; then
  skip "reaper closes a stray ungrouped about:blank" "extension sweepStrayTabs not loaded (relaunch+SW-wipe)"
else
  ub0="$(ung_blanks)"
  make_ungrouped_blank; sleep 0.5
  ub1="$(ung_blanks)"
  reap
  ub2="$(ung_blanks)"
  { [ "${ub1:-0}" -gt "${ub0:-0}" ] && [ "${ub2:-0}" -le "${ub0:-0}" ]; } \
    && pass "reaper closes a stray ungrouped about:blank ($ub0->$ub1->$ub2)" \
    || fail "reaper closes a stray ungrouped about:blank" "$ub0->$ub1->$ub2"
fi

# A relaunch restores every tab with its group and a NEW id, so no registry claims it. The
# group is no claim: such a blank is a stray like any other. Measured on the grouped count,
# which the adopted blanks of this suite's own live sessions never leave (they are claimed).
if [ "$have_fn" != "function" ]; then
  skip "reaper closes a grouped blank no registry claims" "extension sweepStrayTabs not loaded"
  skip "reaper leaves a stray younger than the age gate" "extension sweepStrayTabs not loaded"
else
  gb0="$(grouped_blanks)"
  make_grouped_blank; sleep 0.5
  gb1="$(grouped_blanks)"
  STRAY_AGE=3600 reap
  gb2="$(grouped_blanks)"
  reap
  gb3="$(grouped_blanks)"
  { [ "${gb1:-0}" -gt "${gb0:-0}" ] && [ "${gb2:-0}" -eq "${gb1:-0}" ]; } \
    && pass "reaper leaves a stray younger than the age gate ($gb0->$gb1->$gb2)" \
    || fail "reaper leaves a stray younger than the age gate" "$gb0->$gb1->$gb2"
  { [ "${gb1:-0}" -gt "${gb0:-0}" ] && [ "${gb3:-0}" -le "${gb0:-0}" ]; } \
    && pass "reaper closes a grouped blank no registry claims ($gb0->$gb1->$gb3)" \
    || fail "reaper closes a grouped blank no registry claims" "$gb0->$gb1->$gb3"
fi

# ═════ 3. safety: empty live set never reaps ═════════════════════════════════
say "[3] safety stop"
# The guard that keeps the two rules from colliding. groupTab is best-effort, so one of a
# LIVE session's tabs can end up ungrouped — which looks exactly like a stray. sweepStrayTabs
# must never close a tab any registry claims. Prove both directions on the same tab: claimed
# survives, then unclaimed is taken.
if [ "$have_fn" != "function" ]; then
  skip "stray sweep spares a claimed tab" "extension sweepStrayTabs not loaded"
else
  out="$(HORSE_SESSION="$HSESS" "$HB" <<'PY' 2>/dev/null | sed -n 's/^SAFE //p'
import json, urllib.request, os
port = os.environ.get("HORSE_BROWSER_PORT") or "9223"
req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
tid = json.load(urllib.request.urlopen(req))["id"]   # PUT: Chrome rejects GET/POST here since M111
sw = next(t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type")=="service_worker" and t.get("url","").startswith("chrome-extension://"))
s = cdp("Target.attachToTarget", targetId=sw, flatten=True)["sessionId"]

def sweep(claimed):
    e = f"self.sweepStrayTabs({json.dumps(claimed)})"
    cdp("Runtime.evaluate", session_id=s, expression=e, awaitPromise=True, returnByValue=True)
    return any(t.get("id") == tid for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list")))

print("SAFE", json.dumps({"claimed_survives": sweep([tid]), "unclaimed_taken": not sweep([])}))
PY
)"
  grep -q '"claimed_survives": true' <<<"$out" \
    && pass "stray sweep spares a tab the registry claims" \
    || fail "stray sweep spares a claimed tab" "$out"
  grep -q '"unclaimed_taken": true' <<<"$out" \
    && pass "stray sweep still takes an unclaimed blank" \
    || fail "stray sweep takes an unclaimed blank" "$out"
fi

# ═════ 4. hygiene gate: an UNCLEAN daemon death leaves NOTHING behind ═════════
# The class-level guard. A daemon SIGKILLed (crash / OOM / killed parent — no atexit) must, after
# one reap, leave: zero of its runtime files (pid/sock/lock/port), zero orphan daemon, zero tabs in
# its group. This is what the behavioral checks above (tabs only) + the isolated suites structurally
# missed — the runtime-FILE leak that shipped undetected until v0.9.5. Assert all three together, so
# the suite fails on any leak of this class, not just the one instance we already fixed.
say "[4] hygiene gate — unclean death leaves nothing"
RT="$HOME/.config/browser-harness/runtime"
HYG="reaptest-$$-hg$RANDOM"                       # prefix matches cleanup(); unique last segment
BU="hb-${HYG##*-}"                                # launcher: BU_NAME = hb-<last dash-segment>
HORSE_SESSION="$HYG" "$HB" <<'PY' >/dev/null 2>&1
import urllib.parse
open_tab("data:text/html,"+urllib.parse.quote("<title>HYG-WORK</title>x"))   # a real work tab to leak
wait_for_load()
PY
hpid="$(daemon_pid_for "$HYG")"
fb=0; for e in pid sock lock; do [ -e "$RT/bu-$BU.$e" ] && fb=$((fb+1)); done
if [ -z "$hpid" ] || [ "$fb" -eq 0 ]; then
  skip "hygiene gate" "session did not materialize (BU=$BU pid=${hpid:-none} files=$fb)"
else
  kill -9 "$hpid" 2>/dev/null; sleep 1            # UNCLEAN crash: no atexit — leaves files + tab
  reap >/dev/null 2>&1                            # the real maybe_reap: daemons + tabs + runtime files
  fa=0; for e in pid sock lock port; do [ -e "$RT/bu-$BU.$e" ] && fa=$((fa+1)); done
  [ "$fa" -eq 0 ] \
    && pass "runtime files GC'd after an unclean death ($fb->0)" \
    || fail "runtime files GC'd after an unclean death" "$fa remain (bu-$BU.*)"
  [ -z "$(daemon_pid_for "$HYG")" ] \
    && pass "no orphan daemon survives" \
    || fail "no orphan daemon survives"
  ht="$(titled HYG-WORK)"                         # by title: no extension, no registry
  [ "${ht:-0}" -eq 0 ] \
    && pass "no leaked tabs from the crashed session" \
    || fail "no leaked tabs after an unclean death" "HYG-WORK still has ${ht} tab(s)"
fi

# ═════ 5. idle blank sweep: a silent session gives back its blank tabs ═══════
# The reaper above only acts once a session has ENDED. A session that is still open but done
# browsing would hold its blanks for as long as its process lives; its own daemon closes them
# after HORSE_BROWSER_BLANK_TTL seconds without a call, and never a tab with a page in it.
say "[5] idle blank sweep"
IDLE="reaptest-$$-idle$$"
i0="$(blanks)"
HORSE_SESSION="$IDLE" HORSE_BROWSER_BLANK_TTL=2 HORSE_BROWSER_NO_REAP=1 "$HB" <<'PY' >/dev/null 2>&1
import urllib.parse
open_tab("data:text/html,"+urllib.parse.quote("<title>IDLE-WORK</title>x"))
wait_for_load()
switch_tab(cdp("Target.createTarget", url="about:blank", background=True)["targetId"])
PY
i1="$(blanks)"
sleep 5
i2="$(blanks)"
{ [ "${i1:-0}" -gt "${i0:-0}" ] && [ "${i2:-0}" -le "${i0:-0}" ]; } \
  && pass "a silent session's blank tab is closed after the TTL ($i0->$i1->$i2)" \
  || fail "a silent session's blank tab is closed after the TTL" "$i0->$i1->$i2"
[ "$(titled IDLE-WORK)" -eq 1 ] \
  && pass "the sweep leaves the session's loaded tab alone" \
  || fail "the sweep leaves the session's loaded tab alone" "IDLE-WORK $(titled IDLE-WORK)"
back="$(HORSE_SESSION="$IDLE" HORSE_BROWSER_BLANK_TTL=2 HORSE_BROWSER_NO_REAP=1 "$HB" <<<'print("BACK", "IDLE-WORK" in page_info()["url"])' 2>/dev/null | sed -n 's/^BACK //p')"
[ "$back" = "True" ] \
  && pass "the next call after a sweep lands on the session's remaining tab" \
  || fail "the next call after a sweep lands on the session's remaining tab" "got '${back}'"
ipid="$(daemon_pid_for "$IDLE")"; [ -n "$ipid" ] && kill "$ipid" 2>/dev/null

# The commonest shape: a session that holds nothing but its blank. The sweep takes it, and the
# call after that gets exactly one tab back, as a first call does.
ONLY="reaptest-$$-only$$"
o0="$(blanks)"
HORSE_SESSION="$ONLY" HORSE_BROWSER_BLANK_TTL=2 HORSE_BROWSER_NO_REAP=1 "$HB" <<<'page_info()' >/dev/null 2>&1
o1="$(blanks)"
sleep 5
o2="$(blanks)"
{ [ "${o1:-0}" -gt "${o0:-0}" ] && [ "${o2:-0}" -eq "${o0:-0}" ]; } \
  && pass "a silent blank-only session is left with no tab ($o0->$o1->$o2)" \
  || fail "a silent blank-only session is left with no tab" "$o0->$o1->$o2"
again="$(HORSE_SESSION="$ONLY" HORSE_BROWSER_BLANK_TTL=2 HORSE_BROWSER_NO_REAP=1 "$HB" <<<'print("AGAIN", page_info()["url"], len(list_tabs()))' 2>/dev/null | sed -n 's/^AGAIN //p')"
o3="$(blanks)"
{ [ "$again" = "about:blank 1" ] && [ "${o3:-0}" -eq $(( ${o0:-0} + 1 )) ]; } \
  && pass "the call after that sweep gets one blank back, not two ($o2->$o3)" \
  || fail "the call after that sweep gets one blank back, not two" "call='$again' blanks $o2->$o3"
opid="$(daemon_pid_for "$ONLY")"; [ -n "$opid" ] && kill "$opid" 2>/dev/null

# ═════ 6. a finished sub-agent's lane: its registry outlives tabs that did not close ═════
say "[6] lane registry"
LANEHOME="$(mktemp -d)"; LBU="hb-lanetestsid-agentx"
mkdir -p "$LANEHOME/.config/horse-browser/tabs" "$LANEHOME/.config/browser-harness/runtime"
lane_stop() {  # $1 = the port the hook is to find the browser on
  echo '["0000000000000000000000000000DEAD"]' >| "$LANEHOME/.config/horse-browser/tabs/$LBU"
  : >| "$LANEHOME/.config/browser-harness/runtime/bu-$LBU.pid"
  echo '{"hook_event_name":"SubagentStop","agent_id":"agentx","session_id":"x-lanetestsid"}' \
    | HOME="$LANEHOME" HORSE_BROWSER_PORT="$1" python3 "$HERE/../integrations/claude-code/lane-hook.py"
}
lane_stop 1
[ -e "$LANEHOME/.config/horse-browser/tabs/$LBU" ] \
  && pass "a lane whose tabs could not be closed keeps its registry for the reaper" \
  || fail "a lane whose tabs could not be closed keeps its registry for the reaper"
lane_stop "$PORT"
[ ! -e "$LANEHOME/.config/horse-browser/tabs/$LBU" ] \
  && pass "a lane whose tabs are gone takes its registry with it" \
  || fail "a lane whose tabs are gone takes its registry with it"
rm -rf "$LANEHOME"

say ""
say "── $PASS passed, $FAIL failed, $SKIP skipped"
[ "$FAIL" -gt 0 ] && { for f in "${FAILED[@]}"; do say "   FAILED: $f"; done; exit 1; }
exit 0
