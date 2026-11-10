#!/usr/bin/env python3
"""Run an expression inside the horse-browser extension's service worker.

The extension is the only thing that can act on a *running* browser's tabs, groups
and storage, and its service worker is reachable like any other CDP target. That
makes this the launcher's one channel INTO a live browser — used to reap dead
sessions' tabs and to tell the extension which debug port it is living on. The same
socket code asks the browser itself for its targets (targets, browser_call).

Stdlib only (hand-rolled websocket framing): the launcher must work on a machine
where nothing but python3 is installed.

  CLI:  sw_eval.py <port> <expression> [wait_s]   → prints the JSON result, if any
  API:  evaluate(port, expr, wait_s=0) -> value | None
"""
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request


def http_json(port, path):
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=4))


def ws_connect(port, ws_url, timeout=5):
    path = "/" + ws_url.split("://", 1)[1].split("/", 1)[1]
    s = socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(1024)
        if not c:
            raise ConnectionError("handshake closed")
        buf += c
    if b" 101 " not in buf.split(b"\r\n", 1)[0]:
        raise ConnectionError("no upgrade")
    return s


def ws_send(s, payload):
    d = payload.encode(); n = len(d); m = os.urandom(4); h = bytearray([0x81])
    if n < 126: h.append(0x80 | n)
    elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
    else: h.append(0x80 | 127); h += struct.pack(">Q", n)
    h += m
    s.sendall(bytes(h) + bytes(b ^ m[i % 4] for i, b in enumerate(d)))


def ws_recv(s):
    def rd(n):
        b = b""
        while len(b) < n:
            c = s.recv(n - len(b))
            if not c:
                raise ConnectionError("closed")
            b += c
        return b
    _b0, b1 = rd(2); ln = b1 & 0x7f
    if ln == 126: ln = struct.unpack(">H", rd(2))[0]
    elif ln == 127: ln = struct.unpack(">Q", rd(8))[0]
    return rd(ln)


def _call(s, method, params, timeout):
    """One CDP command on an open socket; its result, or None if the reply never comes."""
    ws_send(s, json.dumps({"id": 1, "method": method, "params": params or {}}))
    s.settimeout(timeout)
    for _ in range(200):
        m = json.loads(ws_recv(s))
        if m.get("id") == 1:
            return m.get("result")
    return None


def browser_call(port, method, params=None, timeout=5):
    """One CDP command on the browser's own endpoint. Raises when the browser does not answer."""
    s = ws_connect(port, http_json(port, "/json/version")["webSocketDebuggerUrl"], timeout=timeout)
    try:
        result = _call(s, method, params, timeout)
    finally:
        s.close()
    if result is None:
        raise ConnectionError(f"no reply to {method}")
    return result


def targets(port):
    """Every target the browser has, asked of the browser itself.

    Not /json/list: that listing has been seen to come back empty on a browser holding twenty
    tabs, and a reader that takes "no tabs" at its word concludes every tab is already closed."""
    return browser_call(port, "Target.getTargets")["targetInfos"]


def service_workers(port):
    """Every extension service-worker target in that browser, ours or not."""
    try:
        return [dict(t, webSocketDebuggerUrl=f"ws://127.0.0.1:{port}/devtools/page/{t['targetId']}")
                for t in targets(port)
                if t.get("type") == "service_worker"
                and t.get("url", "").startswith("chrome-extension://")]
    except Exception:
        return []


# The operator's profile can carry OTHER extensions (a password manager, an ad blocker),
# each with its own service worker — so "the first chrome-extension:// worker" is not ours.
# Evaluating in the wrong one would seed the port into a stranger's storage and leave the
# Monitor unseeded. Ours is the one that defines the verbs the launcher drives it through;
# ask before trusting, in the same round trip.
# groupTab + activateTab: the two verbs that stay for good, because neither has a CDP
# equivalent. Probing on anything the registry rewrite might delete would make this
# identity check drift with the refactor.
_MINE = "typeof self.groupTab==='function'&&typeof self.activateTab==='function'"
_NOT_MINE = "__not_horse_sw__"


def _eval_on(port, ws_url, expr, timeout):
    try:
        s = ws_connect(port, ws_url, timeout=min(5, timeout))
        try:
            result = _call(s, "Runtime.evaluate",
                           {"expression": f"({_MINE}) ? ({expr}) : '{_NOT_MINE}'",
                            "awaitPromise": True, "returnByValue": True}, timeout)
        finally:
            s.close()
        return ((result or {}).get("result") or {}).get("value")
    except Exception:
        return None


def evaluate(port, expr, wait_s=0, timeout=8):
    """Evaluate `expr` in the horse-browser extension's service worker; return its value
    (None on any failure — every caller here is best-effort and must never break the
    launcher, and None is also what a browser without our extension yields).

    `wait_s` waits for OUR worker, not for any worker: an MV3 worker can still be
    registering right after launch, and a profile with other extensions would otherwise
    satisfy the wait with a stranger's worker on the first poll and give up. The whole call
    is bounded by wait_s + timeout even so — the launcher holds the single-flight lock while
    this runs, and a stranger's worker that accepts a socket then never answers must not be
    able to stretch that by one timeout per worker per round."""
    deadline = time.monotonic() + max(0, wait_s)
    hard = deadline + timeout
    while True:
        for sw in service_workers(port):
            left = hard - time.monotonic()
            if left <= 0:
                return None
            val = _eval_on(port, sw["webSocketDebuggerUrl"], expr, min(timeout, left))
            if val is None or val == _NOT_MINE:
                continue                    # unreachable, or somebody else's extension
            return val
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)
    val = evaluate(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 0)
    if val is not None:
        print(val if isinstance(val, str) else json.dumps(val))
