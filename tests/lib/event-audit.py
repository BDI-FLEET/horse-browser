#!/usr/bin/env python3
"""Every field of every input event we synthesise, next to the same field from a real hand.

`force` was found by hand: someone thought to ask what the pointer says about ITSELF rather than
only where it went, and PointerEvent.pressure turned out to be 0 while a button was held — a
state no physical device can produce. That was luck plus a hunch, and there are ~30 more fields
on a pointer event and ~15 on a key event that nobody had looked at.

This looks at all of them. It records the complete property set of every input event, from us
and (optionally) from a person, and prints them side by side so a wrong constant shows up as a
row that differs rather than as a failed challenge three weeks later.

    python3 tests/lib/event-audit.py --ours     # drive our own input, dump every field
    python3 tests/lib/event-audit.py --human    # serve the page, you use it, dump every field
    python3 tests/lib/event-audit.py --diff     # compare the two recordings

Fields with a physically-required value are checked against it regardless of whether a human
recording exists — those are the ones worth failing a build over.
"""
import http.server, json, os, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(HERE, "traces")
PORT = int(os.environ.get("HB_AUDIT_PORT") or 8778)

# Everything a PointerEvent/MouseEvent/KeyboardEvent exposes that a detector could read. Listed
# explicitly rather than enumerated at runtime: an own-property walk misses accessors on the
# prototype, which is where nearly all of these live.
POINTER_FIELDS = """pointerId width height pressure tangentialPressure tiltX tiltY twist
  pointerType isPrimary button buttons clientX clientY screenX screenY pageX pageY offsetX
  offsetY movementX movementY detail isTrusted altKey ctrlKey metaKey shiftKey composed
  bubbles cancelable eventPhase""".split()
KEY_FIELDS = """key code keyCode charCode which location repeat isComposing isTrusted altKey
  ctrlKey metaKey shiftKey composed bubbles cancelable detail""".split()

PAGE = """<!doctype html><meta charset=utf-8><title>event audit</title>
<style>
 :root{color-scheme:light dark} body{margin:0;font:15px/1.5 system-ui,sans-serif;padding:26px 30px}
 h1{font-size:17px;margin:0 0 4px;font-weight:650} p{margin:0 0 20px;opacity:.75}
 #h{width:150px;height:44px;background:#4a90d9;border-radius:6px;cursor:grab;
    display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600;
    touch-action:none;user-select:none}
 input{margin-top:22px;padding:9px 11px;font:15px system-ui;width:260px;border-radius:6px;
       border:1px solid #8886}
 #n{font-variant-numeric:tabular-nums;font-weight:650}
</style>
<body>
<h1>Drag the blue box a little, then type a few letters in the field.</h1>
<p>Both at once is fine. <span id=n>0</span> events captured.</p>
<div id=h>drag me</div>
<input id=t placeholder="type anything here">
<p style="margin-top:18px"><button id=save style="padding:9px 16px;font:15px system-ui;
   border-radius:6px;cursor:pointer">Save recording</button>
   <span id=status style="margin-left:12px;opacity:.75"></span></p>
<script>
var PF = %(pf)s, KF = %(kf)s;
window.EV = [];
function grab(e, fields, kind){
  if (EV.length > 1200) return;
  var o = {_k: kind, _t: e.type};
  for (var i=0;i<fields.length;i++){
    var v = e[fields[i]];
    o[fields[i]] = (typeof v === 'number' && !Number.isInteger(v)) ? Math.round(v*1000)/1000 : v;
  }
  // Coalesced and predicted events are pointer-only, and are exactly the sort of thing a
  // synthetic stream has none of while a real one has several per frame.
  if (kind === 'p') {
    o._coalesced = e.getCoalescedEvents ? e.getCoalescedEvents().length : null;
    o._predicted = e.getPredictedEvents ? e.getPredictedEvents().length : null;
  }
  EV.push(o);
}
['pointerdown','pointermove','pointerup','pointerenter','pointerover'].forEach(function(n){
  addEventListener(n, function(e){ grab(e, PF, 'p'); }, true); });
['mousedown','mousemove','mouseup','click'].forEach(function(n){
  addEventListener(n, function(e){ grab(e, PF, 'm'); }, true); });
['keydown','keypress','keyup'].forEach(function(n){
  addEventListener(n, function(e){ grab(e, KF, 'k'); }, true); });
setInterval(function(){ document.getElementById('n').textContent = EV.length; }, 300);

var h = document.getElementById('h'), down=false, sx=0, ox=0;
h.addEventListener('pointerdown', function(e){ down=true; sx=e.clientX;
  ox=parseInt(h.style.marginLeft||0,10); h.setPointerCapture(e.pointerId); });
addEventListener('pointermove', function(e){ if(down)
  h.style.marginLeft = Math.max(0, Math.min(300, ox + (e.clientX-sx))) + 'px'; });
addEventListener('pointerup', function(){ down=false; });

// The page posts on demand so a human session ends when the person says so.
window.__post = function(tag){
  return fetch('/save?tag='+tag, {method:'POST', headers:{'content-type':'application/json'},
                                  body: JSON.stringify(EV)}).then(function(){ return EV.length; });
};
// A button, not a keystroke. Relying on Enter meant a recording could be made and lost with
// nothing on screen to say either had happened.
document.getElementById('save').addEventListener('click', function(){
  window.__post('human').then(function(n){
    document.getElementById('status').textContent = 'saved ' + n + ' events — you can close this';
  }).catch(function(err){
    document.getElementById('status').textContent = 'save FAILED: ' + err;
  });
});
addEventListener('keydown', function(e){
  if (e.key === 'Enter') document.getElementById('save').click(); });
</script>
"""


def serve():
    os.makedirs(OUT, exist_ok=True)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            b = (PAGE % {"pf": json.dumps(POINTER_FIELDS), "kf": json.dumps(KEY_FIELDS)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

        def do_POST(self):
            tag = self.path.split("tag=")[-1] or "human"
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            p = os.path.join(OUT, "events-%s.json" % tag)
            with open(p, "wb") as f:
                f.write(raw)
            print("  saved %s (%d bytes)" % (os.path.basename(p), len(raw)), flush=True)
            self.send_response(204); self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── physics: values a real device cannot contradict, checked with or without a human sample ──
def invariants(ev):
    bad = []
    for e in ev:
        if e["_k"] == "k":
            continue
        held = e.get("buttons", 0) != 0
        moving = e["_t"] in ("pointermove", "mousemove")
        if e["_k"] == "p":
            if held and not e.get("pressure"):
                bad.append("%s: buttons=%s but pressure=%s — no device reports force-free contact"
                           % (e["_t"], e.get("buttons"), e.get("pressure")))
            if not held and e.get("pressure"):
                bad.append("%s: no button down but pressure=%s" % (e["_t"], e.get("pressure")))
            if e.get("pointerType") == "mouse" and (e.get("width") != 1 or e.get("height") != 1):
                bad.append("%s: mouse contact geometry %sx%s (a mouse is 1x1)"
                           % (e["_t"], e.get("width"), e.get("height")))
            if not e.get("isPrimary"):
                bad.append("%s: isPrimary false with one pointer" % e["_t"])
        if not e.get("isTrusted"):
            bad.append("%s: isTrusted false — the page can see this was scripted" % e["_t"])
        _ = moving  # movement (0,0) is legitimate — a paused hand reports it too; see below
    # Sub-pixel positions. Chrome preserves fractional dispatched coordinates exactly, and on a
    # display with devicePixelRatio > 1 a real pointer produces them constantly — 256 of 256
    # samples in a recorded human session. All-integer coordinates across a whole gesture is
    # `Number.isInteger(e.clientX)`, which is one line and which no hand passes on such a screen.
    pts = [e for e in ev if e["_k"] == "p" and e.get("clientX") is not None]
    if pts and all(float(e["clientX"]).is_integer() and float(e["clientY"]).is_integer()
                   for e in pts):
        bad.append("every one of %d pointer positions is a whole number — real input is "
                   "sub-pixel wherever devicePixelRatio > 1" % len(pts))
    # Rate, not presence. A pointer that never moves between samples is a stalled loop; one
    # that occasionally does is a hand pausing.
    mv = [e for e in ev if e["_t"] in ("pointermove", "mousemove")]
    if mv:
        zero = sum(1 for e in mv if not e.get("movementX") and not e.get("movementY"))
        if zero / len(mv) > 0.25:
            bad.append("%.0f%% of move events report movement (0,0) — the pointer is not moving"
                       % (100.0 * zero / len(mv)))
    # dedupe, keep order
    seen, out = set(), []
    for b in bad:
        if b not in seen:
            seen.add(b); out.append(b)
    return out


def summarise(ev):
    """field -> the set of values it took, per event kind."""
    out = {}
    for e in ev:
        for k, v in e.items():
            if k.startswith("_") and k not in ("_coalesced", "_predicted"):
                continue
            out.setdefault((e["_k"], k), set()).add(json.dumps(v))
    return out


def load(tag):
    p = os.path.join(OUT, "events-%s.json" % tag)
    return json.load(open(p)) if os.path.exists(p) else None


def report():
    ours, human = load("ours"), load("human")
    if not ours:
        print("no 'ours' recording — run --ours first"); return 1
    print("horse-browser event-audit — what our input events say about themselves")
    print("\n  Physics checks on our own events (no human sample needed):")
    bad = invariants(ours)
    for b in bad:
        print("    ✗ %s" % b)
    if not bad:
        print("    ✓ nothing physically impossible")
    if not human:
        print("\n── %d passed, %d failed  (no human recording; --human compares every other field)"
              % (0 if bad else 1, 1 if bad else 0))
        return 1 if bad else 0
    a, b = summarise(ours), summarise(human)
    # Coordinates are continuous and depend on where the window happens to be, so their value
    # SETS never overlap between two sessions and comparing them raw reports six differences that
    # mean nothing. What matters about a coordinate is its shape: fractional or whole, and
    # whether screen space is distinct from client space. Those are checked separately.
    CONTINUOUS = {"clientX", "clientY", "pageX", "pageY", "screenX", "screenY",
                  "offsetX", "offsetY", "movementX", "movementY"}
    print("\n  Coordinate shape (the part that is comparable across sessions):")
    for src, rows in [("ours", ours), ("human", human)]:
        pts = [e for e in rows if e["_k"] == "p" and e.get("clientX") is not None]
        if not pts:
            continue
        frac = sum(1 for e in pts if not float(e["clientX"]).is_integer())
        sint = sum(1 for e in pts if float(e["screenX"]).is_integer())
        same = sum(1 for e in pts if e["screenX"] == e["clientX"])
        print("    %-6s client fractional %3d%%   screen whole %3d%%   screen==client %3d%%"
              % (src, 100 * frac // len(pts), 100 * sint // len(pts), 100 * same // len(pts)))
    print("\n  Fields where our values and a hand's do not overlap:")
    diffs = 0
    for key in sorted(set(a) | set(b), key=lambda k: (k[0], k[1])):
        kind, field = key
        if field in CONTINUOUS:
            continue
        av, bv = a.get(key, set()), b.get(key, set())
        if not av or not bv or (av & bv):
            continue
        diffs += 1
        print("    %-3s %-18s ours %-26s hand %s"
              % (kind, field, ",".join(sorted(av))[:26], ",".join(sorted(bv))[:34]))
    if not diffs:
        print("    (none — every field's values overlap)")
    print("\n── %d passed, %d failed" % (2 - bool(bad) - bool(diffs), bool(bad) + bool(diffs)))
    return 1 if (bad or diffs) else 0


def drive_ours():
    srv = serve()
    script = """
import json, time
from horse_harness.helpers import drag, type_into, click_xy
open_tab("http://127.0.0.1:%d/"); wait_for_load(); time.sleep(0.6)
g = json.loads(js('''(function(){var r=document.getElementById('h').getBoundingClientRect();
  return JSON.stringify({x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)});})()'''))
drag((g["x"], g["y"]), dx=180)
time.sleep(0.4)
type_into("#t", "Hello Wor1d!")
time.sleep(0.6)
print("EVENTS", js("window.EV.length"))
js("window.__post('ours')")
time.sleep(1.0)
for t in list_tabs():
    try: cdp("Target.closeTarget", targetId=t["targetId"])
    except Exception: pass
""" % PORT
    # A test run must never reach the operator's ~/.claude or ~/.grok: without this the
    # launcher wires THIS checkout's path into the real settings.json and rule file.
    r = subprocess.run([os.path.join(ROOT, "bin", "horse-browser")], input=script,
                       env={**os.environ, "HORSE_BROWSER_NO_RECONCILE": "1"},
                       text=True, capture_output=True, timeout=300)
    for line in (r.stdout or "").splitlines():
        if line.startswith("EVENTS"):
            print("  captured %s events from our own input" % line.split()[1])
    time.sleep(0.8)
    srv.shutdown()
    return report()


if __name__ == "__main__":
    if "--ours" in sys.argv:
        sys.exit(drive_ours())
    if "--human" in sys.argv:
        serve()
        print("\n  Open  http://127.0.0.1:%d/  — drag the box, type in the field, then press\n"
              "  Enter in the field to save. Ctrl-C when done.\n" % PORT)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n  stopped")
        sys.exit(0)
    sys.exit(report())
