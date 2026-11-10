#!/usr/bin/env python3
# lane-hook.py — makes parallel Claude Code subagents invisible to each other in the
# browser. Agents never see or manage any of this; it's pure harness plumbing.
#
# PreToolUse (Bash): a subagent's tool calls carry its `agent_id` in the hook payload —
# the id is NOT in the subagent's env, which is byte-identical to the parent's (verified
# on Claude Code 2.1.201; hook payloads gained agent_id in 2.1.168). When such a call
# invokes horse-browser, rewrite it via updatedInput to `horse-browser --lane <agent_id>`:
# the subagent transparently gets its own daemon + tab group. No permissionDecision is
# emitted, so the (rewritten) command still goes through the normal permission flow.
#
# SubagentStop: the subagent is done — close its lane's tabs (direct CDP over a stdlib
# websocket; never launches a browser or daemon) and stop its lane daemon, so parallel
# fan-outs leave zero browser residue. Best-effort: any failure is swallowed, a hook
# must never break the agent loop.
import json
import os
import re
import signal
import subprocess
import sys

SAFE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# horse-browser in command position — start of command, or after ; & | ( ` or a newline,
# optionally behind env-var assignments. Never a path segment like /path/to/horse-browser.
CMD_POS = re.compile(r"(^|[;&|(`]\s*|\n\s*)((?:[A-Za-z_][A-Za-z_0-9]*=\S*\s+)*)horse-browser(?=\s|$|;|<)")
STAMPED = re.compile(r"(horse-browser\s+--lane[= ])[A-Za-z0-9_-]+")


def pretooluse(d):
    aid = d.get("agent_id")
    ti = d.get("tool_input") or {}
    cmd = ti.get("command") or ""
    if not (aid and SAFE.fullmatch(aid) and d.get("tool_name") == "Bash"):
        return
    if STAMPED.search(cmd):
        # already stamped (the model copied an earlier rewritten command) — re-stamp
        # with the TRUE id, so a lane can never be spoofed or go stale
        new = STAMPED.sub(lambda m: m.group(1) + aid, cmd)
    else:
        new, n = CMD_POS.subn(lambda m: m.group(1) + m.group(2) + f"horse-browser --lane {aid}", cmd, count=1)
        if not n:
            return
    if new == cmd:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": {**ti, "command": new},
    }}))


def _port():
    p = os.environ.get("HORSE_BROWSER_PORT")
    if p:
        return p.strip()
    try:
        for line in open(os.path.expanduser("~/.config/horse-browser/config")):
            if line.startswith("PORT="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "9223"


def close_lane_tabs(bu):
    """Close every tab the lane claimed — its registry, over the DevTools HTTP endpoint.

    No websocket and no extension. A lane's tabs are whatever
    ~/.config/horse-browser/tabs/<BU_NAME> says they are, so this works on a browser we
    did not launch and does not need a service worker to be awake — which matters here
    more than anywhere, because this runs while a subagent is being torn down. Replaced
    ~50 lines of hand-rolled CDP-over-websocket that existed only to ask the extension
    which tabs carried the lane's group title. A no-op unless the browser is already up.
    """
    import urllib.error
    import urllib.request
    f = os.path.expanduser("~/.config/horse-browser/tabs/" + bu)
    try:
        ids = [t for t in json.load(open(f)) if isinstance(t, str)]
    except Exception:
        return
    port = _port()
    unclosed = False
    for tid in ids:
        try:
            urllib.request.urlopen("http://127.0.0.1:%s/json/close/%s" % (port, tid),
                                   timeout=3).read()
        except urllib.error.HTTPError as e:
            unclosed = unclosed or e.code != 404    # 404: the tab was already gone
        except Exception:
            unclosed = True
    if unclosed:
        # The registry is the one record of these tabs. Its daemon is about to be stopped,
        # so the launcher's reaper reads it as an ended session and closes what is left.
        return
    try:
        os.unlink(f)                        # the registry dies with the lane
    except OSError:
        pass


def subagentstop(d):
    aid = d.get("agent_id") or ""
    sid = d.get("session_id") or ""
    if not (aid and sid and SAFE.fullmatch(aid)):
        return
    # mirror bin/horse-browser's BU_NAME derivation exactly: hb-<last-12-of-tail>-<lane>
    tail = sid.rsplit("-", 1)[-1][-12:]
    bu = f"hb-{tail}-{aid}"
    runtime = os.path.expanduser("~/.config/browser-harness/runtime")
    pidf = os.path.join(runtime, f"bu-{bu}.pid")
    sockf = os.path.join(runtime, f"bu-{bu}.sock")
    if not (os.path.exists(pidf) or os.path.exists(sockf)):
        return  # this subagent never browsed — the common case, exit cheap
    try:
        close_lane_tabs(bu)
    except Exception:
        pass
    try:
        pid = int(open(pidf).read().strip())
        # Only kill a process that really is a harness daemon (stale-pidfile guard). BOTH
        # names: the harness is vendored as horse_harness now, and matching only the old
        # browser_harness meant this guard rejected every real daemon — so the lane's
        # daemon was never signalled, and only its pid/sock files got tidied below.
        cmdline = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=3).stdout
        if "horse_harness" in cmdline or "browser_harness" in cmdline:
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    # the stock daemon never removes its pid/sock files (killed or self-exited) — tidy
    # them so the lane leaves zero residue; a dying daemon's own unlink is a no-op then
    for f in (pidf, sockf):
        try:
            os.unlink(f)
        except OSError:
            pass


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return
    ev = d.get("hook_event_name")
    if ev == "PreToolUse":
        pretooluse(d)
    elif ev == "SubagentStop":
        subagentstop(d)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
