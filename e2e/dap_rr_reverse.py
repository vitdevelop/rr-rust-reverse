#!/usr/bin/env python3
"""Phase 3.2: rr-backed end-to-end check of the patched lldb-dap `reverseContinue`.

Records a small C program with rr, replays it as an lldb-compatible gdbserver
(`rr replay -s <port> -d lldb`), attaches the patched lldb-dap over that port via
raw DAP JSON, then:
  * sets a breakpoint, continues forward twice (to a later loop iteration),
  * `reverseContinue` and asserts the stack line moved backwards,
  * `reverseContinue` again past the start of the recording and asserts a
    `stopped` event with reason "history boundary".

Usage:  python3 e2e/dap_rr_reverse.py [path-to-lldb-dap]
Exit 0 on success.
"""
import json, os, re, shutil, socket, subprocess, sys, tempfile, threading, time

DAP = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(__file__), "..", "build", "bin", "lldb-dap")
DAP = os.path.abspath(DAP)
assert shutil.which("rr"), "rr not on PATH"
assert os.access(DAP, os.X_OK), f"not executable: {DAP}"

work = tempfile.mkdtemp(prefix="dap_rr_")
src = os.path.join(work, "loop.c")
exe = os.path.join(work, "loop")
with open(src, "w") as f:
    f.write(
        "#include <stdio.h>\n"
        "int step(int i){ int x = i*i; return x; }        /* BP */\n"
        "int main(void){\n"
        "  int total = 0;\n"
        "  for (int i = 0; i < 8; ++i) total += step(i);\n"
        "  printf(\"%d\\n\", total);\n"
        "  return 0;\n"
        "}\n"
    )
bp_line = 2
subprocess.run(["cc", "-g", "-O0", "-o", exe, src], check=True)

env = dict(os.environ)
subprocess.run(["rr", "record", "-n", exe], check=True, cwd=work,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

port = 50777
replay = subprocess.Popen(
    ["rr", "replay", "-s", str(port), "-k", "-d", "lldb"],
    cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True)
# wait for the port to be listening
for _ in range(100):
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        break
    except OSError:
        time.sleep(0.1)
    finally:
        pass
else:
    raise SystemExit("rr replay gdbserver never came up")


class DAPClient:
    def __init__(self, argv):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.seq = 0
        self.lock = threading.Lock()
        self.events, self.responses = [], {}
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        buf = b""
        while True:
            b1 = self.p.stdout.read(1)
            if not b1:
                return
            buf += b1
            if buf.endswith(b"\r\n\r\n"):
                n = int(re.search(rb"Content-Length:\s*(\d+)", buf).group(1))
                msg = json.loads(self.p.stdout.read(n))
                with self.lock:
                    if msg["type"] == "event":
                        self.events.append(msg)
                    elif msg["type"] == "response":
                        self.responses[msg["request_seq"]] = msg
                buf = b""

    def send(self, command, arguments=None):
        with self.lock:
            self.seq += 1
            s = self.seq
        m = {"seq": s, "type": "request", "command": command}
        if arguments is not None:
            m["arguments"] = arguments
        data = json.dumps(m).encode()
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n%s" % (len(data), data))
        self.p.stdin.flush()
        return s

    def wait_response(self, s, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if s in self.responses:
                    return self.responses[s]
            time.sleep(0.01)
        raise TimeoutError(f"no response to seq {s}")

    def wait_event(self, name, timeout=30, after=0.0):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                for e in self.events:
                    if e["event"] == name:
                        return e
            time.sleep(0.01)
        raise TimeoutError(f"no '{name}' event")

    def req(self, command, arguments=None, **kw):
        return self.wait_response(self.send(command, arguments), **kw)


def top_line(dap, tid):
    r = dap.req("stackTrace", {"threadId": tid, "levels": 1})
    fr = r["body"]["stackFrames"][0]
    return fr.get("line"), fr.get("name")


ok = True
dap = DAPClient([DAP])
try:
    r = dap.req("initialize", {"adapterID": "lldb-dap", "linesStartAt1": True,
                               "columnsStartAt1": True, "pathFormat": "path"})
    assert r["success"], r
    assert r["body"].get("supportsStepBack") is True, "supportsStepBack not advertised"
    print("initialize: supportsStepBack = True  OK")

    dap.send("attach", {"program": exe, "gdb-remote-port": port,
                        "gdb-remote-hostname": "127.0.0.1"})
    dap.wait_event("initialized")
    r = dap.req("setBreakpoints",
                {"source": {"path": src}, "breakpoints": [{"line": bp_line}]})
    assert r["success"] and r["body"]["breakpoints"][0]["verified"], r
    dap.req("configurationDone")

    st = dap.wait_event("stopped")
    tid = st["body"]["threadId"]

    # Forward to the 3rd hit of the breakpoint.
    for _ in range(3):
        dap.send("continue", {"threadId": tid})
        dap.events[:] = [e for e in dap.events if e["event"] != "stopped"]
        dap.wait_event("stopped")
    fwd_line, fwd_fn = top_line(dap, tid)
    fwd_i = dap.req("evaluate", {"expression": "i", "context": "watch",
                                 "frameId": dap.req("stackTrace",
                                 {"threadId": tid, "levels": 2})["body"]["stackFrames"][1]["id"]})
    print(f"forward stop: {fwd_fn}:{fwd_line}  (main's i = {fwd_i['body'].get('result')})")

    # Reverse-continue: should hit the SAME breakpoint at an EARLIER point.
    dap.events[:] = [e for e in dap.events if e["event"] != "stopped"]
    r = dap.req("reverseContinue", {"threadId": tid})
    assert r["success"], ("reverseContinue failed: %s" % r)
    dap.wait_event("stopped")
    rev_line, rev_fn = top_line(dap, tid)
    rev_i = dap.req("evaluate", {"expression": "i", "context": "watch",
                                 "frameId": dap.req("stackTrace",
                                 {"threadId": tid, "levels": 2})["body"]["stackFrames"][1]["id"]})
    print(f"after reverseContinue: {rev_fn}:{rev_line}  (main's i = {rev_i['body'].get('result')})")
    assert rev_fn == fwd_fn, (rev_fn, fwd_fn)
    assert int(rev_i["body"]["result"]) < int(fwd_i["body"]["result"]), \
        "loop counter did not go backwards"
    print("reverseContinue moved execution backwards  OK")

    # Keep reversing past the start of the recording -> history boundary.
    # Clear the breakpoint first so reverse-continue isn't stopped by it.
    dap.req("setBreakpoints", {"source": {"path": src}, "breakpoints": []})
    dap.events[:] = [e for e in dap.events if e["event"] != "stopped"]
    r = dap.req("reverseContinue", {"threadId": tid})
    assert r["success"], r
    time.sleep(1.0)
    with dap.lock:
        reasons = [e["body"].get("reason") for e in dap.events if e["event"] == "stopped"]
    print("stop reasons after final reverseContinue:", reasons)
    assert "history boundary" in reasons, reasons
    print("history boundary reported  OK")

    print("\nRR-BACKED DAP REVERSE TEST: PASS")
except Exception:
    ok = False
    import traceback
    traceback.print_exc()
finally:
    try:
        dap.req("disconnect", {}, timeout=5)
    except Exception:
        pass
    dap.p.kill()
    replay.kill()
    shutil.rmtree(work, ignore_errors=True)

sys.exit(0 if ok else 1)
