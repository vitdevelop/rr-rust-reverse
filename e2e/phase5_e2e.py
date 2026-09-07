#!/usr/bin/env python3
"""Phase 5 - automatable end-to-end check of the full stack MINUS the IntelliJ UI.

  rr record  ->  rr replay -s <port> -d lldb  ->  patched lldb-dap (attach by port)
  ->  raw DAP JSON driving: breakpoint, forward continue, reverseContinue, history boundary,
      Rust variable rendering, earlier-values-after-reversing.

Writes E2E-RESULTS.md with the captured DAP exchanges and a PASS/FAIL per plan bullet.
Exit 0 iff every checked bullet passes.
"""
import json, os, re, shutil, socket, subprocess, sys, time, datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DAP = os.path.join(ROOT, "build", "bin", "lldb-dap")
DEMO_DIR = os.path.join(ROOT, "e2e", "rrdemo")
EXE = os.path.join(DEMO_DIR, "target", "debug", "rrdemo")
SRC = os.path.join(DEMO_DIR, "src", "main.rs")
BP_LINE = 9                      # `p.x += round;`
PORT = 50823
OUT = os.path.join(ROOT, "E2E-RESULTS.md")

assert shutil.which("rr"), "rr not on PATH"
assert os.access(DAP, os.X_OK), DAP
assert os.path.exists(EXE), "run `cargo build` in e2e/rrdemo first"

MANUAL_CHECKLIST = """## Manual checklist - the IntelliJ IDEA UI part

The automatable run above proves the protocol layer end to end with the **patched
`lldb-dap`**. The following must be done by hand in IntelliJ IDEA (Ultimate, or
Community with LSP4IJ - the debugger UI is identical) with the **patched LSP4IJ**
plugin installed, because they are about the IDE toolbar and panes.

### One-time setup

1. Install the patched plugin:
   `src/lsp4ij/build/distributions/lsp4ij-0.21.1-SNAPSHOT.zip`
   (Settings > Plugins > gear > Install Plugin from Disk).
2. Put the patched `lldb-dap` on PATH, or note its absolute path
   `build/bin/lldb-dap`.
3. Terminal A - start the replay server (leave it running):
   ```
   cd e2e/rrdemo && cargo build && rr record -n target/debug/rrdemo
   rr replay -s 50823 -k -d lldb
   ```
4. In IntelliJ: Run > Edit Configurations > + > **Debug Adapter Protocol**.
   - Debug Adapter Server: `lldb-dap` (command = the patched `build/bin/lldb-dap`).
   - Debug mode: **Attach**.
   - DAP parameters (JSON):
     `{ "program": "<abs path>/e2e/rrdemo/target/debug/rrdemo",
        "gdb-remote-port": 50823, "gdb-remote-hostname": "127.0.0.1" }`
   - Mappings tab: map `*.rs` to a language so breakpoints can be set (Rust, or
     "Plain text" if no Rust plugin).
5. Open `e2e/rrdemo/src/main.rs`, click the gutter on line 9 (`p.x += round;`).

### Checks (tick each; screenshot where noted)

- [ ] **Breakpoint hit** - start Debug; execution stops on line 9, the editor
      highlights it, the Frames panel shows `rrdemo::mutate` -> `rrdemo::main`.
      *(screenshot: editor + Frames)*
- [ ] **Variables pane shows Rust types readably** - in Variables, expand the
      `mutate` frame: `round` is an int; select the `main` frame and expand `p`
      (shows `x`, `y`, `label`) and `v` (a `Vec` with `len` and buffer). Not raw
      pointers. *(screenshot: Variables)*
- [ ] **"Reverse Continue" button is present** in the debugger toolbar (top),
      next to Step Over / Into / Out, only for this DAP session. A green
      left-pointing triangle. "Step Back" (blue) sits next to it.
- [ ] **"Reverse Continue" moves execution backwards** - note `p.x` / `v.len` in
      Variables, then click Reverse Continue. Execution stops on line 9 again,
      one loop iteration earlier. *(screenshot: Variables before vs after)*
- [ ] **Variables show the EARLIER values after reversing** - `p.x` is smaller,
      `v.len` is smaller, `p.label` is the previous `round-N`. The panes updated
      through the normal stop path (no stale values, no manual refresh needed).
- [ ] **History boundary is graceful** - remove the breakpoint, click Reverse
      Continue until it stops with reason/description "history boundary" (shown
      in the Frames/thread tooltip or the debug console). The session stays
      alive; Resume/Step still work forwards. *(screenshot)*
- [ ] **No stray buttons for ordinary DAP** - start any non-rr DAP session (e.g.
      the bundled `debugpy` template against a Python script). The debugger
      toolbar shows **no** Reverse Continue / Step Back buttons.
- [ ] **"Step Back" degrades cleanly** - with the rr session suspended, click
      Step Back. The debug console prints
      `Reverse execution failed: ... reverse stepping (stepBack) is not supported ...`
      and the session stays suspended (this is expected - `stepBack` is a
      deliberate "not supported" per Phase 2.3).

Record the outcome of each box and the screenshots in this file under a
"Manual results" heading.
"""

log_lines = []
def L(s=""):
    print(s)
    log_lines.append(s)

# ---- record + replay -------------------------------------------------------
subprocess.run(["cargo", "build"], cwd=DEMO_DIR, check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
env = dict(os.environ)
rec = subprocess.run(["rr", "record", "-n", EXE], cwd=DEMO_DIR, env=env,
                     capture_output=True, text=True)
assert rec.returncode == 0, rec.stderr
replay = subprocess.Popen(["rr", "replay", "-s", str(PORT), "-k", "-d", "lldb"],
                          cwd=DEMO_DIR, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(120):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.5).close()
        break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit("rr replay gdbserver did not come up")

# ---- minimal DAP client --------------------------------------------------
import threading
class Dap:
    def __init__(self, argv):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.seq = 0
        self.lock = threading.Lock()
        self.events, self.responses = [], {}
        threading.Thread(target=self._rx, daemon=True).start()
    def _rx(self):
        buf = b""
        while True:
            c = self.p.stdout.read(1)
            if not c:
                return
            buf += c
            if buf.endswith(b"\r\n\r\n"):
                n = int(re.search(rb"Content-Length:\s*(\d+)", buf).group(1))
                m = json.loads(self.p.stdout.read(n))
                with self.lock:
                    if m["type"] == "event":
                        self.events.append(m)
                    elif m["type"] == "response":
                        self.responses[m["request_seq"]] = m
                buf = b""
    def send(self, command, arguments=None):
        with self.lock:
            self.seq += 1
            s = self.seq
        msg = {"seq": s, "type": "request", "command": command}
        if arguments is not None:
            msg["arguments"] = arguments
        d = json.dumps(msg).encode()
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n%s" % (len(d), d))
        self.p.stdin.flush()
        return s
    def resp(self, s, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if s in self.responses:
                    return self.responses[s]
            time.sleep(0.01)
        raise TimeoutError("no response %d" % s)
    def req(self, command, arguments=None, **kw):
        return self.resp(self.send(command, arguments), **kw)
    def wait_event(self, name, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                for e in self.events:
                    if e["event"] == name:
                        return e
            time.sleep(0.01)
        raise TimeoutError("no %s event" % name)
    def drop_stopped(self):
        with self.lock:
            self.events[:] = [e for e in self.events if e["event"] != "stopped"]

results = []   # (bullet, ok, detail)
def check(bullet, ok, detail=""):
    results.append((bullet, bool(ok), detail))
    L("  [%s] %s%s" % ("PASS" if ok else "FAIL", bullet, ("  -- " + detail) if detail else ""))

dap = Dap([DAP])
try:
    r = dap.req("initialize", {"adapterID": "lldb-dap", "linesStartAt1": True,
                               "columnsStartAt1": True, "pathFormat": "path"})
    caps = r["body"]
    L("### initialize")
    L("supportsStepBack = %r" % caps.get("supportsStepBack"))
    check("adapter advertises reverse-execution capability (supportsStepBack)",
          caps.get("supportsStepBack") is True)

    dap.send("attach", {"program": EXE, "gdb-remote-port": PORT,
                        "gdb-remote-hostname": "127.0.0.1"})
    dap.wait_event("initialized")
    r = dap.req("setBreakpoints", {"source": {"path": SRC},
                                   "breakpoints": [{"line": BP_LINE}]})
    verified = r["body"]["breakpoints"][0].get("verified")
    L("\n### setBreakpoints main.rs:%d -> verified=%r line=%r"
      % (BP_LINE, verified, r["body"]["breakpoints"][0].get("line")))
    dap.req("configurationDone")
    st = dap.wait_event("stopped")            # initial attach stop (rr replay start)
    tid = st["body"]["threadId"]
    L("initial stop after attach: reason=%r" % st["body"].get("reason"))
    # First forward continue must land on the source breakpoint.
    dap.drop_stopped()
    dap.send("continue", {"threadId": tid})
    bpst = dap.wait_event("stopped")
    bp_frame = dap.req("stackTrace", {"threadId": tid, "levels": 1})["body"]["stackFrames"][0]
    check("breakpoint in Rust source is hit",
          verified and bpst["body"].get("reason") == "breakpoint"
          and bp_frame.get("line") == BP_LINE
          and bp_frame["name"].startswith("rrdemo::mutate"),
          "stopped reason=%r at %s:%s"
          % (bpst["body"].get("reason"), bp_frame["name"], bp_frame.get("line")))

    def frames():
        return dap.req("stackTrace", {"threadId": tid, "levels": 4})["body"]["stackFrames"]

    def var_tree(frame_id, depth=0, maxd=3):
        out = {}
        sc = dap.req("scopes", {"frameId": frame_id})["body"]["scopes"]
        loc = next((s for s in sc if s["name"].lower().startswith("local")), sc[0])
        def walk(ref, d):
            if d > maxd or not ref:
                return {}
            vs = dap.req("variables", {"variablesReference": ref})["body"]["variables"]
            node = {}
            for v in vs:
                node[v["name"]] = {"value": v.get("value"), "type": v.get("type")}
                if v.get("variablesReference"):
                    child = walk(v["variablesReference"], d + 1)
                    if child:
                        node[v["name"]]["children"] = child
            return node
        return walk(loc["variablesReference"], 0)

    def snapshot(tag):
        frs = frames()
        top, caller = frs[0], frs[1]
        L("\n### %s" % tag)
        L("frame0: %s at %s:%s" % (top["name"], top.get("source", {}).get("name"), top.get("line")))
        L("frame1: %s at %s:%s" % (caller["name"], caller.get("source", {}).get("name"), caller.get("line")))
        top_vars = var_tree(top["id"])
        caller_vars = var_tree(caller["id"])
        L("frame0 locals: " + json.dumps(top_vars))
        L("frame1 (main) locals: " + json.dumps(caller_vars, indent=1))
        return top, caller, top_vars, caller_vars

    # Forward to the 5th hit of the breakpoint (round == 5).
    for _ in range(4):
        dap.drop_stopped()
        dap.send("continue", {"threadId": tid})
        dap.wait_event("stopped")
    f0, f1, tv, cv = snapshot("FORWARD STOP #5")
    fwd_round = int(re.sub(r"\D", "", tv.get("round", {}).get("value", "0")) or 0)
    p_node = cv.get("p", {}).get("children", {})
    fwd_px = p_node.get("x", {}).get("value")
    v_node = cv.get("v", {}).get("children", {})
    fwd_vlen = v_node.get("len", {}).get("value")
    fwd_label = json.dumps(p_node.get("label", {}))
    check("stopped inside mutate() at the Rust breakpoint line",
          f0["name"].startswith("rrdemo::mutate") and f0.get("line") == BP_LINE,
          "%s:%s" % (f0["name"], f0.get("line")))
    readable = ("Point" in (cv.get("p", {}).get("type") or "") and
               "Vec<long" in (cv.get("v", {}).get("type") or "") and
               fwd_px is not None and fwd_vlen is not None)
    check("variables pane shows Rust types readably (Point{x,y,label}, Vec<i64>{len,..})",
          readable, "p.type=%r  v.type=%r  p.x=%r  v.len=%r"
          % (cv.get("p", {}).get("type"), cv.get("v", {}).get("type"), fwd_px, fwd_vlen))
    L("captured: round=%s  p.x=%s  v.len=%s  p.label=%s" % (fwd_round, fwd_px, fwd_vlen, fwd_label))

    # Reverse-continue: hit the SAME breakpoint one round earlier.
    dap.drop_stopped()
    r = dap.req("reverseContinue", {"threadId": tid})
    L("\n### reverseContinue -> success=%r" % r["success"])
    check("reverseContinue request accepted", r["success"], json.dumps(r.get("body", {})))
    rst = dap.wait_event("stopped")
    L("stopped reason=%r description=%r" % (rst["body"].get("reason"), rst["body"].get("description")))
    r0, r1, rtv, rcv = snapshot("AFTER reverseContinue")
    rev_round = int(re.sub(r"\D", "", rtv.get("round", {}).get("value", "0")) or 0)
    rp = rcv.get("p", {}).get("children", {})
    rev_px = rp.get("x", {}).get("value")
    rev_vlen = rcv.get("v", {}).get("children", {}).get("len", {}).get("value")
    rev_label = json.dumps(rp.get("label", {}))
    L("captured: round=%s  p.x=%s  v.len=%s  p.label=%s" % (rev_round, rev_px, rev_vlen, rev_label))
    check("reverseContinue moved execution BACKWARDS to an earlier breakpoint hit",
          rst["body"].get("reason") == "breakpoint" and rev_round == fwd_round - 1,
          "round %s -> %s" % (fwd_round, rev_round))
    try:
        earlier_vals = (int(rev_px) < int(fwd_px)) and (int(rev_vlen) < int(fwd_vlen))
    except (TypeError, ValueError):
        earlier_vals = False
    check("variables pane shows the EARLIER values after reversing",
          earlier_vals, "p.x %s->%s   v.len %s->%s" % (fwd_px, rev_px, fwd_vlen, rev_vlen))

    # Reverse past the start of the recording -> graceful history boundary.
    dap.req("setBreakpoints", {"source": {"path": SRC}, "breakpoints": []})
    dap.drop_stopped()
    r = dap.req("reverseContinue", {"threadId": tid})
    assert r["success"], r
    hb = dap.wait_event("stopped")
    L("\n### reverseContinue with no breakpoint -> stopped reason=%r description=%r"
      % (hb["body"].get("reason"), hb["body"].get("description")))
    check("reversing past the recording start reports the history boundary gracefully",
          hb["body"].get("reason") == "history boundary" and dap.p.poll() is None)

    # non-rr behaviour: reverseContinue against an ordinary process errors cleanly.
    L("\n### control: reverseContinue against a non-record/replay process (separate lldb-dap)")
    ctl = Dap([DAP])
    try:
        ctl.req("initialize", {"adapterID": "lldb-dap", "linesStartAt1": True, "columnsStartAt1": True})
        ctl.send("launch", {"program": EXE, "stopOnEntry": True})
        ctl.wait_event("initialized")
        ctl.req("setBreakpoints", {"source": {"path": SRC}, "breakpoints": [{"line": BP_LINE}]})
        ctl.req("configurationDone")
        cst = ctl.wait_event("stopped")
        ctid = cst["body"]["threadId"]
        cr = ctl.req("reverseContinue", {"threadId": ctid})
        msg = cr.get("message") or cr.get("body", {}).get("error", {}).get("format", "")
        L("reverseContinue -> success=%r message=%r" % (cr["success"], msg))
        check("reverseContinue on a NON-rr process returns a clean DAP error (no crash/hang)",
              cr["success"] is False and "reverse" in msg.lower() and ctl.p.poll() is None, msg)
    finally:
        try:
            ctl.req("disconnect", {"terminateDebuggee": True}, timeout=5)
        except Exception:
            pass
        ctl.p.kill()
finally:
    try:
        dap.req("disconnect", {"terminateDebuggee": True}, timeout=5)
    except Exception:
        pass
    dap.p.kill()
    replay.kill()

# ---- write E2E-RESULTS.md ------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
now = datetime.date.today().isoformat()
with open(OUT, "w") as f:
    f.write("# E2E-RESULTS.md - Phase 5 (automatable portion)\n\n")
    f.write("Date: %s. Generated by `e2e/phase5_e2e.py`.\n\n" % now)
    f.write("Covers the whole stack **except the IntelliJ UI**: a real Rust binary, "
            "`rr record`, `rr replay -s <port> -d lldb`, the **patched** `lldb-dap` "
            "attached by port, driven through raw DAP JSON. The IntelliJ-UI checks "
            "(toolbar buttons, variables *pane*) are in the manual checklist below.\n\n")
    f.write("## Exact commands\n\n```\n")
    f.write("cd e2e/rrdemo && cargo build\n")
    f.write("rr record -n target/debug/rrdemo\n")
    f.write("rr replay -s %d -k -d lldb            # NB: -d lldb is required for an LLDB client\n" % PORT)
    f.write("build/bin/lldb-dap                    # attach with gdb-remote-port=%d\n" % PORT)
    f.write("python3 e2e/phase5_e2e.py             # drives the DAP exchange below\n```\n\n")
    f.write("## Checked results\n\n| # | check | result |\n|---|---|---|\n")
    for i, (bullet, ok, detail) in enumerate(results, 1):
        f.write("| %d | %s | **%s**%s |\n" % (i, bullet, "PASS" if ok else "FAIL",
                                              (" - " + detail) if detail else ""))
    f.write("\n**%d / %d automatable checks passed.**\n\n" % (passed, len(results)))
    f.write("## Full DAP transcript\n\n```\n")
    f.write("\n".join(log_lines))
    f.write("\n```\n\n")
    f.write(MANUAL_CHECKLIST)

L("\n%d/%d automatable checks passed. Wrote %s" % (passed, len(results), OUT))
sys.exit(0 if passed == len(results) else 1)
