#!/usr/bin/env python3
"""Phase 5 (stock-adapter variant) - end-to-end minus the IntelliJ UI, using an
UNPATCHED lldb-dap.

Reverse execution is driven exactly the way the stock-variant LSP4IJ plugin does
it: the DAP `evaluate` request with context "repl" running the built-in LLDB
commands `process continue -R` (reverse) and `process continue -F` (force forward
again, for the plugin's Resume after a reverse).

  rr record -> rr replay -s <port> -d lldb -> STOCK lldb-dap (attach by port)
  -> raw DAP JSON: breakpoint, forward continue, evaluate("process continue -R"),
     evaluate("process continue -F"), history boundary, Rust vars, earlier values.

Writes E2E-STOCK-RESULTS.md. Exit 0 iff every checked bullet passes.
"""
import json, os, re, shutil, socket, subprocess, sys, threading, time, datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DAP = os.path.join(ROOT, "build", "bin", "lldb-dap")          # rebuilt from the pristine tree = stock
DEMO_DIR = os.path.join(ROOT, "e2e", "rrdemo")
EXE = os.path.join(DEMO_DIR, "target", "debug", "rrdemo")
SRC = os.path.join(DEMO_DIR, "src", "main.rs")
BP_LINE = 9
PORT = 50833
OUT = os.path.join(ROOT, "E2E-STOCK-RESULTS.md")

assert shutil.which("rr"), "rr not on PATH"
assert os.access(DAP, os.X_OK), DAP
assert os.path.exists(EXE), "run `cargo build` in e2e/rrdemo first"

log_lines = []
def L(s=""):
    print(s); log_lines.append(s)

subprocess.run(["cargo", "build"], cwd=DEMO_DIR, check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
env = dict(os.environ)
assert subprocess.run(["rr", "record", "-n", EXE], cwd=DEMO_DIR, env=env,
                      capture_output=True).returncode == 0
replay = subprocess.Popen(["rr", "replay", "-s", str(PORT), "-k", "-d", "lldb"],
                          cwd=DEMO_DIR, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for _ in range(120):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.5).close(); break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit("rr replay gdbserver did not come up")

class Dap:
    def __init__(s, argv):
        s.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        s.seq = 0; s.lock = threading.Lock(); s.events = []; s.responses = {}
        threading.Thread(target=s._rx, daemon=True).start()
    def _rx(s):
        buf = b""
        while True:
            c = s.p.stdout.read(1)
            if not c:
                return
            buf += c
            if buf.endswith(b"\r\n\r\n"):
                n = int(re.search(rb"Content-Length:\s*(\d+)", buf).group(1))
                m = json.loads(s.p.stdout.read(n))
                with s.lock:
                    if m["type"] == "event":
                        s.events.append(m)
                    elif m["type"] == "response":
                        s.responses[m["request_seq"]] = m
                buf = b""
    def send(s, command, arguments=None):
        with s.lock:
            s.seq += 1; q = s.seq
        m = {"seq": q, "type": "request", "command": command}
        if arguments is not None:
            m["arguments"] = arguments
        d = json.dumps(m).encode()
        s.p.stdin.write(b"Content-Length: %d\r\n\r\n%s" % (len(d), d)); s.p.stdin.flush()
        return q
    def rsp(s, q, t=30):
        end = time.time() + t
        while time.time() < end:
            with s.lock:
                if q in s.responses:
                    return s.responses[q]
            time.sleep(0.01)
        raise TimeoutError(q)
    def req(s, command, arguments=None, **kw):
        return s.rsp(s.send(command, arguments), **kw)
    def wait(s, name, t=20):
        end = time.time() + t
        while time.time() < end:
            with s.lock:
                for e in s.events:
                    if e["event"] == name:
                        return e
            time.sleep(0.01)
        return None
    def drop(s, name):
        with s.lock:
            s.events[:] = [e for e in s.events if e["event"] != name]
    def repl(s, cmd):
        r = s.req("evaluate", {"expression": cmd, "context": "repl"})
        return r.get("body", {}).get("result", "")

results = []
def check(bullet, ok, detail=""):
    results.append((bullet, bool(ok), detail))
    L("  [%s] %s%s" % ("PASS" if ok else "FAIL", bullet, ("  -- " + detail) if detail else ""))

d = Dap([DAP])
try:
    init = d.req("initialize", {"adapterID": "lldb-dap", "linesStartAt1": True,
                                "columnsStartAt1": True, "pathFormat": "path"})
    L("### initialize (STOCK adapter)")
    L("supportsStepBack in caps = %r  (expected: absent/None)" % init["body"].get("supportsStepBack"))
    check("stock lldb-dap does NOT advertise supportsStepBack (gate is the config opt-in instead)",
          init["body"].get("supportsStepBack") in (None, False))

    d.send("attach", {"program": EXE, "gdb-remote-port": PORT, "gdb-remote-hostname": "127.0.0.1"})
    d.wait("initialized")
    bp = d.req("setBreakpoints", {"source": {"path": SRC}, "breakpoints": [{"line": BP_LINE}]})
    verified = bp["body"]["breakpoints"][0].get("verified")
    d.req("configurationDone")
    d.wait("stopped")
    tid = None
    with d.lock:
        for e in d.events:
            if e["event"] == "stopped":
                tid = e["body"]["threadId"]
    d.drop("stopped")
    d.send("continue", {"threadId": tid})
    bpst = d.wait("stopped")
    f0 = d.req("stackTrace", {"threadId": tid, "levels": 1})["body"]["stackFrames"][0]
    check("breakpoint in Rust source is hit",
          verified and bpst["body"].get("reason") == "breakpoint"
          and f0.get("line") == BP_LINE and f0["name"].startswith("rrdemo::mutate"),
          "%s:%s reason=%r" % (f0["name"], f0.get("line"), bpst["body"].get("reason")))

    def caller_snapshot(tag):
        frs = d.req("stackTrace", {"threadId": tid, "levels": 3})["body"]["stackFrames"]
        f0, f1 = frs[0], frs[1]
        L("\n### %s" % tag)
        L("frame0 %s:%s   frame1 %s:%s" % (f0["name"], f0.get("line"), f1["name"], f1.get("line")))
        sc = d.req("scopes", {"frameId": f1["id"]})["body"]["scopes"]
        loc = next(s for s in sc if s["name"].lower().startswith("local"))
        vs = d.req("variables", {"variablesReference": loc["variablesReference"]})["body"]["variables"]
        vals = {}
        for v in vs:
            entry = {"value": v.get("value"), "type": v.get("type")}
            if v.get("variablesReference"):
                kids = d.req("variables", {"variablesReference": v["variablesReference"]})["body"]["variables"]
                entry["children"] = {k["name"]: {"value": k.get("value"), "type": k.get("type")} for k in kids}
            vals[v["name"]] = entry
        L("main locals: " + json.dumps(vals, indent=1))
        return f0, vals

    # forward to round 5
    for _ in range(4):
        d.drop("stopped"); d.send("continue", {"threadId": tid}); d.wait("stopped")
    f0, fwd = caller_snapshot("FORWARD STOP (round 5)")
    fwd_px = fwd["p"]["children"]["x"]["value"]
    fwd_vlen = fwd["v"]["children"]["len"]["value"]
    fwd_round = fwd["round"]["value"]
    check("variables show Rust types readably (Point{x,y,label}, Vec<i64>{len,..})",
          "Point" in (fwd["p"]["type"] or "") and "Vec<long" in (fwd["v"]["type"] or "")
          and fwd_px is not None and fwd_vlen is not None,
          "p.type=%r v.type=%r p.x=%s v.len=%s" % (fwd["p"]["type"], fwd["v"]["type"], fwd_px, fwd_vlen))
    L("captured round=%s p.x=%s v.len=%s" % (fwd_round, fwd_px, fwd_vlen))

    # --- reverse the way the stock-variant plugin does: evaluate repl ---
    d.drop("stopped")
    out = d.repl("process continue -R")
    L('\n### evaluate("process continue -R", repl) -> %r' % out.strip())
    check("reverse via `process continue -R` through the DAP evaluate/REPL request",
          "error:" not in out.lower())
    rst = d.wait("stopped")
    check("reverse produces a normal `stopped` event (so panes refresh via positionReached)",
          rst is not None and rst["body"].get("reason") in ("breakpoint", "step"),
          "reason=%r" % (rst["body"].get("reason") if rst else None))
    r0, rev = caller_snapshot("AFTER reverse (round should be 4)")
    rev_px = rev["p"]["children"]["x"]["value"]
    rev_vlen = rev["v"]["children"]["len"]["value"]
    rev_round = rev["round"]["value"]
    check("execution moved BACKWARDS one loop iteration",
          int(re.sub(r"\D", "", rev_round)) == int(re.sub(r"\D", "", fwd_round)) - 1,
          "round %s -> %s" % (fwd_round, rev_round))
    try:
        earlier = int(rev_px) < int(fwd_px) and int(rev_vlen) < int(fwd_vlen)
    except (TypeError, ValueError):
        earlier = False
    check("variables show the EARLIER values after reversing",
          earlier, "p.x %s->%s  v.len %s->%s" % (fwd_px, rev_px, fwd_vlen, rev_vlen))

    # --- the direction-bug fix the plugin applies: plain DAP `continue` after a
    #     reverse runs BACKWARDS; `process continue -F` restores forward. ---
    d.drop("stopped")
    d.send("continue", {"threadId": tid})
    d.wait("stopped")
    _, after_plain = caller_snapshot("after a PLAIN DAP continue (expected: goes backwards - the bug)")
    plain_round = after_plain["round"]["value"]
    d.drop("stopped")
    fout = d.repl("process continue -F")
    d.wait("stopped")
    _, after_F = caller_snapshot('after evaluate("process continue -F") (plugin\'s Resume path)')
    f_round = after_F["round"]["value"]
    d.drop("stopped")
    d.send("continue", {"threadId": tid})
    d.wait("stopped")
    _, after_F2 = caller_snapshot("then a plain DAP continue (expected: forward, dir was reset)")
    f2_round = after_F2["round"]["value"]
    went_back = int(re.sub(r"\D", "", plain_round)) < int(re.sub(r"\D", "", rev_round))
    fwd_after_F = int(re.sub(r"\D", "", f2_round)) > int(re.sub(r"\D", "", f_round))
    check("the stock-variant plugin must route Resume through `process continue -F` "
          "(plain DAP continue after a reverse goes backwards; -F restores forward)",
          went_back and fwd_after_F,
          "plain continue: round %s->%s (back);  after -F then continue: %s->%s (fwd)"
          % (rev_round, plain_round, f_round, f2_round))

    # --- history boundary ---
    d.req("setBreakpoints", {"source": {"path": SRC}, "breakpoints": []})
    d.drop("stopped")
    hb_out = d.repl("process continue -R")
    hb = d.wait("stopped")
    L('\n### evaluate("process continue -R") with no breakpoint -> stopped reason=%r'
      % (hb["body"].get("reason") if hb else None))
    check("reversing past the recording start reports the history boundary gracefully",
          hb is not None and hb["body"].get("reason") == "history boundary" and d.p.poll() is None)
finally:
    try:
        d.req("disconnect", {"terminateDebuggee": True}, t=5)
    except Exception:
        pass
    d.p.kill(); replay.kill()

passed = sum(1 for _, ok, _ in results if ok)
with open(OUT, "w") as f:
    f.write("# E2E-STOCK-RESULTS.md — Phase 5, stock-adapter variant\n\n")
    f.write("Date: %s. Generated by `e2e/phase5_stock_e2e.py`.\n\n" % datetime.date.today().isoformat())
    f.write("**No LLVM patch.** `build/bin/lldb-dap` here was rebuilt from the pristine "
            "`llvmorg-22.1.8` tree (the Phase 2–3 patch is stashed). Reverse execution is "
            "driven exactly as the *stock-adapter* LSP4IJ variant does it: the DAP "
            "`evaluate` request, `context:\"repl\"`, running `process continue -R` / `-F`.\n\n")
    f.write("## Commands\n\n```\ncd e2e/rrdemo && cargo build && rr record -n target/debug/rrdemo\n"
            "rr replay -s %d -k -d lldb\nbuild/bin/lldb-dap            # STOCK, attach gdb-remote-port=%d\n"
            "python3 e2e/phase5_stock_e2e.py\n```\n\n" % (PORT, PORT))
    f.write("## Checked results\n\n| # | check | result |\n|---|---|---|\n")
    for i, (b, ok, det) in enumerate(results, 1):
        f.write("| %d | %s | **%s**%s |\n" % (i, b, "PASS" if ok else "FAIL", (" — " + det) if det else ""))
    f.write("\n**%d / %d automatable checks passed.**\n\n" % (passed, len(results)))
    f.write("## Full transcript\n\n```\n" + "\n".join(log_lines) + "\n```\n")
L("\n%d/%d passed. Wrote %s" % (passed, len(results), OUT))
sys.exit(0 if passed == len(results) else 1)
