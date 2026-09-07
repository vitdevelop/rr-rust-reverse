# rr reverse debugging for Rust in IntelliJ

Debug an [`rr`](https://rr-project.org/)-recorded Rust program **backwards** from
inside IntelliJ IDEA (or any JetBrains IDE), with working breakpoints and variable
inspection.

Adds a **Reverse Continue** button to the debugger toolbar for DAP sessions that
attach to an `rr` replay server. It runs LLDB's built-in `process continue -R`
through the Debug Adapter Protocol, so it works with a **stock `lldb-dap`** — no
patched debugger to build or distribute.

```
IntelliJ IDEA
  └─ LSP4IJ ── DAP (socket) ──▶ lldb-dap ── gdb-remote ──▶ rr replay -s <port> -d lldb
     (this fork:                 (--connection             (replays your recording,
      "Reverse Continue")         listen://...)             bc/bs reverse packets)
```

## Repository layout

| Path | What |
|---|---|
| `src/lsp4ij/` | **submodule** — fork of [`redhat-developer/lsp4ij`](https://github.com/redhat-developer/lsp4ij), branch `main` = upstream + the plugin change |
| `e2e/` | end-to-end drivers (`phase5_stock_e2e.py` is the active one) and `rrdemo/` (sample Rust program) |
| `patches/` | optional **variant A** — a patched `lldb-dap` with first-class `reverseContinue` / `stepBack` DAP requests, as a `.patch` against `llvmorg-22.1.8` + the matching LSP4IJ diff |
| `scripts/configure-llvm.sh` | LLVM/LLDB CMake config (variant A only) |

## Requirements

- Linux x86-64 (`rr` only runs there).
- `rr` ≥ 5.6.
- `lldb-dap` from LLVM ≥ 20 (`process continue -R/-F` landed there). A distro
  package is fine (`pacman -S lldb`, `apt install lldb`, …). Check: `lldb-dap --version`.
- A Rust toolchain with debug info (`cargo build`, default `dev` profile).
- IntelliJ IDEA 2024.2+ (Community or Ultimate); CLion / RustRover work too.
- JDK 21 to build the plugin.

## Install

### 1. Clone with the submodule

```bash
git clone --recurse-submodules git@github.com:vitdevelop/rr-rust-reverse.git
cd rr-rust-reverse
# already cloned without --recurse-submodules?
git submodule update --init
```

### 2. Build the LSP4IJ plugin

```bash
cd src/lsp4ij
./gradlew buildPlugin        # needs JDK 21; downloads the IntelliJ SDK on first run
# -> build/distributions/lsp4ij-<version>.zip
```

### 3. Install it in the IDE

**Settings ▸ Plugins ▸ ⚙ ▸ Install Plugin from Disk…** → pick
`src/lsp4ij/build/distributions/lsp4ij-*.zip` → restart. If the marketplace
LSP4IJ is already installed, remove it first — only one.

## Run

LSP4IJ's *Attach* mode speaks DAP to a debug adapter **over a socket** — it does
not spawn `lldb-dap` itself. So two processes run in terminals, and the IDE
attaches to `lldb-dap` over a DAP port that is separate from rr's gdb-remote port.

### Terminal 1 — rr replay

```bash
cd e2e/rrdemo
cargo build
rr record -n ./target/debug/rrdemo
rr replay -s 50505 -k -d lldb
```

`-d lldb` is **mandatory** — a plain `rr replay -s <port>` crashes when an LLDB
client connects. Leave this running.

### Terminal 2 — lldb-dap as a DAP server

```bash
lldb-dap --connection listen://127.0.0.1:12345
```

`12345` is an arbitrary **DAP** port; it must differ from rr's `50505`. Leave it
running.

### Run configuration

**Run ▸ Edit Configurations… ▸ + ▸ Debug Adapter Protocol**

**Server tab**

| Field | Value |
|---|---|
| Command | `lldb-dap` (required by validation; not launched in Attach mode) |
| Connect to the server by waiting | `Timeout` — `1000` ms |
| Trace | `verbose` while setting up (prints the DAP JSON to the console); `off` later |

**Mappings tab** — add one row so breakpoints resolve in `.rs` files:

- File name patterns: `*.rs`
- Language: `Rust` (with the Rust plugin / RustRover / CLion) or `Plain Text`.

**Configuration tab**

| Field | Value |
|---|---|
| Working directory | repo root (absolute) |
| File | `<repo>/e2e/rrdemo/src/main.rs` |
| Debug mode | **Attach** |
| **Attach address** | `127.0.0.1` |
| **Attach port** | **`12345`** — the lldb-dap DAP port, **not** 50505 |
| DAP parameters (JSON) → **Attach** sub-tab | ↓ |

```json
{
  "request": "attach",
  "program": "${workspaceFolder}/e2e/rrdemo/target/debug/rrdemo",
  "gdb-remote-port": 50505,
  "gdb-remote-hostname": "127.0.0.1",
  "reverseDebugging": true
}
```

Two connections, two ports: **Attach port `12345`** carries DAP between the IDE
and `lldb-dap`; **`gdb-remote-port` `50505`** is `lldb-dap` → `rr`. Only
`${workspaceFolder}` / `${file}` are substituted in the JSON — use an absolute
`program` path if in doubt, and keep `gdb-remote-port` an unquoted number.

`"reverseDebugging": true` is what shows the **Reverse Continue** button. Omit it
and the toolbar is unchanged.

### Debug

1. Open `e2e/rrdemo/src/main.rs` and click the gutter on **line 9**
   (`p.x += round;`).
2. **Activate the breakpoint.** With a DAP session, a normal red gutter dot is
   not yet a live breakpoint — a second, smaller marker appears next to the first
   code character of that line, tooltip **"DAP Breakpoint"**. Click it so it
   turns solid; that is the one `lldb-dap` actually sets. (Do this while, or
   after, the session is connected.)
3. Start the run configuration. Execution stops on line 9; Frames shows
   `rrdemo::mutate` → `rrdemo::main`; Variables shows `round`, and `p` / `v` in
   the `main` frame.
4. Click **Reverse Continue** (green ◀ in the debugger toolbar). Execution moves
   **backwards** to the previous time that line was hit; Variables shows the
   earlier `p.x` / `v.len`.
5. Keep clicking to reach the start of the recording — it stops with reason
   *history boundary*. Forward Resume / Step still work.

To iterate you can re-run the configuration without restarting Terminal 2;
restart `rr replay` in Terminal 1 if replay state gets stale.

### Optional — pretty-printed Rust values

By default `lldb-dap` shows `String` / `Vec` / `Option` / … as their raw structs
(`alloc::string::String { vec: … }`). To load rustc's LLDB providers so they read
as `"round-7"` / `[1, 4, 9]` / `Some(x)`, add an `initCommands` array to the
**Attach** JSON.

**Paste the real path — not a command.** The JSON is not run through a shell, so
`$(rustc --print sysroot)` is taken literally and the import fails silently. Run

```bash
rustc --print sysroot
```

and substitute the result for `<sysroot>`:

```json
"initCommands": [
  "command script import \"<sysroot>/lib/rustlib/etc/lldb_lookup.py\"",
  "command source -s 0 \"<sysroot>/lib/rustlib/etc/lldb_commands\""
]
```

e.g. with `<sysroot>` = `/home/you/.rustup/toolchains/stable-x86_64-unknown-linux-gnu`:

```json
"initCommands": [
  "command script import \"/home/you/.rustup/toolchains/stable-x86_64-unknown-linux-gnu/lib/rustlib/etc/lldb_lookup.py\"",
  "command source -s 0 \"/home/you/.rustup/toolchains/stable-x86_64-unknown-linux-gnu/lib/rustlib/etc/lldb_commands\""
]
```

Requires an `lldb-dap` built with Python (`lldb -o "script print(1)" -o quit`
should print `1`).

**Only owned values are formatted.** In a frame whose variable is a reference —
e.g. inside `fn mutate(v: &mut Vec<i64>, …)`, where `v` has type
`…::Vec<…> *` — it still renders as a raw pointer, because the providers match the
non-pointer type. Select the caller (`main`) frame to see the owned `Vec` /
`String`. This is how `&mut T` is encoded in debug info, not a plugin issue.

## How it works

Stock `lldb-dap` has no `reverseContinue` DAP request. This fork's
**Reverse Continue** action sends a DAP `evaluate` request with `context:"repl"`
running `process continue -R`. `lldb-dap` runs the command, `rr` replays
backwards, and `lldb-dap` emits an ordinary `stopped` event — so the Frames and
Variables panes refresh through exactly the same path as a forward stop
(including the *history boundary* stop reason).

A plain forward Resume after a reverse would keep running backwards (LLDB's base
direction stays reverse until reset), so the plugin routes **Resume** through
`process continue -F` while `reverseDebugging` is on.

Gating is per run configuration (`"reverseDebugging": true`) rather than a DAP
capability, since stock `lldb-dap` does not advertise `supportsStepBack`.

## Variant A — patched `lldb-dap` (optional)

`patches/` contains an alternative that adds first-class `reverseContinue` /
`stepBack` DAP requests and the `supportsStepBack` capability to `lldb-dap`
itself. It is cleaner (typed requests, capability-based gating, direction fix in
the adapter) but requires building and shipping a custom `lldb-dap`.

```bash
# checkout of llvm-project at tag llvmorg-22.1.8:
git -C /path/to/llvm-project apply /path/to/patches/lldb-dap-reverse-execution.patch
# then build with scripts/configure-llvm.sh + ninja -C build lldb-dap
# and apply patches/lsp4ij-patched-adapter.patch instead of this fork's branch.
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `port out of range: -1` | Debug mode = Attach but Attach address/port empty — set them (`127.0.0.1` / `12345`). |
| Hangs after `Sending request 'initialize'` | Attach port points at rr (`50505`) instead of lldb-dap's DAP port (`12345`). |
| Console: `rr … FATAL … require_timeline_current_task` | `rr replay` started without `-d lldb`. |
| `Connection refused` | Terminal 1 / Terminal 2 not running, or wrong port; start them before Debug. |
| Breakpoint never hits | no `*.rs` mapping, **or** the "DAP Breakpoint" marker was not activated (step 2 above). |
| No **Reverse Continue** button | `"reverseDebugging": true` missing / pasted in the Launch tab, or the marketplace LSP4IJ is loaded instead of this build. |
| `Reverse execution failed: error: … does not support reverse execution` | attached to something that is not an `rr` replay. |

Best diagnostic: Server tab → **Trace = `verbose`**, re-run, read the DAP JSON in
the Debug console.
