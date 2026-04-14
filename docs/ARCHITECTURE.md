# Void Ghostty — Architecture Reference

> Last updated: 2026-04-13
> Status: Phases 0–4 complete. Phase 5 in progress (HouPythonWidget built, _terminal.py and _panel.py rewrites pending). Phases 8+ planned.

---

## What Void Ghostty Is

A creative coding environment embedded inside Houdini as a native Python Panel. The panel is the **UI layer**. Behind it, a set of background threads expose the live Houdini session to external tools — AI agents, live coding environments, DAWs — via their native protocols.

It is part of the Void Monolith suite alongside `$OXD` (Houdini→Crucible bridge) and `$CRUCIBLE` (Rust/Vulkan game runtime). Void Ghostty does not do asset authoring. It is the **developer surface** — the place where code is written, agents are run, and live feedback arrives.

---

## Design Principles

1. **In-process first.** The panel runs inside Houdini. `import hou` works everywhere. No bridge for session access.
2. **Native protocols.** Each integration speaks the protocol that tool already uses. Claude and Codex use MCP. Strudel uses OSC. Shells use PTY. No custom protocol invented where a standard exists.
3. **`dispatch_to_main` is the thread-safety boundary.** Any `hou.*` call from a background thread goes through `hou.ui.addEventLoopCallback`. No exceptions. Not even `parm.set()`.
4. **UI is independent of IPC.** The panel (panes, splitter, shortcuts) works with or without any background server running. Servers are opt-in at session init.
5. **No Jupyter.** Jupyter is the right architecture for Python notebook servers. Void Ghostty's integrations (MCP for AI, OSC for Strudel, HTTP for generic tools) do not speak Jupyter protocol. Adding it would introduce `ipykernel`/`zmq` dependencies into HFS Python with no direct benefit.

---

## Full Architecture

```
HOUDINI PROCESS
├── Main Thread ── Qt event loop ── hou.* API
│   └── hou.ui.addEventLoopCallback(_drain_queue)  ← all hou.* from threads go here
│
├── Qt Panel Widget (void_ghostty._panel)
│   ├── HouPythonWidget       ← in-process Python REPL (code.InteractiveConsole)
│   ├── TerminalWidget/nvim   ← libghostty-vt VT renderer, pynvim param sync
│   └── TerminalWidget/shell  ← PTY subprocess (winpty/pty), free mode
│
├── Thread: FastAPI HTTP :8765          ← universal bridge — any tool, any language
│   ├── POST /exec  {"code": "..."}     → dispatch_to_main → hou.*
│   ├── POST /eval  {"code": "..."}     → dispatch_to_main → hou.* → return repr
│   ├── GET  /scene                     → serialised scene summary JSON
│   └── GET  /hip                       → hou.hipFile.path()
│
├── Thread: MCP server (stdio / SSE)    ← specifically for Claude Code + Codex
│   ├── tool: get_scene_info            → scene graph summary
│   ├── tool: get_node(path)            → node type, parms, children
│   ├── tool: set_parm(path, value)     → dispatch_to_main → parm.set()
│   ├── tool: cook_node(path)           → dispatch_to_main → node.cook()
│   └── tool: run_python(code)         → dispatch_to_main → exec in hou context
│
└── Thread: OSC UDP server :57120       ← Strudel + DAWs + live coding tools
    ├── /note  note vel speed           → map to hou parms via dispatch_to_main
    ├── /houdini/<parm_path>  value     → generic: address maps to parm path
    └── (bidirectional: Houdini can send OSC out to Strudel via python-osc client)

EXTERNAL PROCESSES (connect to Houdini via the threads above)
├── Claude Code CLI   → --mcp-config .vg/mcp.json   → MCP server
├── Codex CLI         → --mcp-config .vg/mcp.json   → MCP server (same protocol)
├── Strudel           → .osc() pattern output        → OSC server
├── Shell scripts     → curl localhost:8765/exec      → FastAPI
└── Ghostty (external)→ subprocess in pane           → PTY (already works, lower priority)
```

---

## Thread-Safety Contract

`hou.*` is only safe on Houdini's main Qt thread. Every background thread (FastAPI, MCP, OSC) submits work via a shared queue drained by `hou.ui.addEventLoopCallback`.

```python
import threading, queue, hou

_task_queue: queue.Queue = queue.Queue()

def _drain_queue() -> None:
    while not _task_queue.empty():
        fn, result_box, evt = _task_queue.get_nowait()
        try:
            result_box.append(fn())
        except Exception as e:
            result_box.append(e)
        evt.set()

hou.ui.addEventLoopCallback(_drain_queue)  # registered once at session init

def dispatch_to_main(fn):
    """Block calling thread until fn() completes on Houdini's main thread."""
    result_box, evt = [], threading.Event()
    _task_queue.put((fn, result_box, evt))
    evt.wait()
    r = result_box[0]
    if isinstance(r, Exception):
        raise r
    return r
```

This pattern is used identically in all three server threads. It is the single seam between the async world and Houdini's main thread.

---

## Component Index

| File | Role |
|---|---|
| `python/void_ghostty/__init__.py` | Public API: `register()`, `pin()`, `unpin()`, `open_nvim()`, server lifecycle |
| `python/void_ghostty/_panel.py` | Qt panel widget, QSplitter multiplexer, pane management |
| `python/void_ghostty/_terminal.py` | `TerminalWidget` — libghostty-vt renderer, PTY bridge (nvim only) |
| `python/void_ghostty/_hou_python.py` | `HouPythonWidget` — in-process Python REPL |
| `python/void_ghostty/_dispatch.py` | `dispatch_to_main()`, `_drain_queue()`, `addEventLoopCallback` setup |
| `python/void_ghostty/_mcp.py` | MCP server thread (stdio/SSE), tool definitions |
| `python/void_ghostty/_api.py` | FastAPI HTTP server thread (:8765) |
| `python/void_ghostty/_osc.py` | python-osc UDP server thread (:57120), Strudel/DAW integration |
| `python/void_ghostty/_hooks.py` | Hook dispatch, `_vg_config` block parser |
| `src/_lib.cpp` | ABI isolation — all `ghostty_*` calls (libghostty-vt) |
| `bin/windows/vg.dll` | Compiled ABI wrapper (Windows) |
| `bin/linux/libvg.so` | Compiled ABI wrapper (Linux) |

Files prefixed `_` are internal. The public surface is `__init__.py` only.

---

## Panel Panes

The panel hosts three pane types. All implement the same interface so `_panel.py` treats them uniformly.

**Pane interface contract:**
```python
pane.start()         # begin execution
pane.stop()          # clean shutdown
pane.mux_split_h     # callable: split this pane horizontally
pane.mux_split_v     # callable: split this pane vertically
pane.mux_close       # callable: close this pane
```

| Pane type | Class | Use |
|---|---|---|
| Python REPL | `HouPythonWidget` | In-process `hou.*`, default pane |
| nvim | `TerminalWidget` | Editor pane, pynvim param sync via `pin()` |
| Shell | `TerminalWidget` | PTY subprocess — Claude Code CLI, Codex, bash |

Default layout: single `HouPythonWidget`. User splits to add nvim or shell panes via shortcuts.

Keyboard shortcuts (work even when PTY has focus):
- `Ctrl+Shift+H` — split current pane right
- `Ctrl+Shift+B` — split current pane below  
- `Ctrl+Shift+X` — close current pane
- `Ctrl+Shift+N` — open new nvim pane

---

## The `pin()` API

`pin(parm)` binds an nvim pane to a Houdini parameter. The nvim buffer becomes the live editor for that parm's value.

```python
# From any Python pane or shelf tool:
import void_ghostty
void_ghostty.pin(hou.node('/obj/hero/wrangle1').parm('snippet'))
```

Sync model:
- **nvim `TextChanged`** → debounce ~30hz → `parm.set(buffer_content)` via `dispatch_to_main` — display only, no cook
- **nvim `BufWritePost` (`:w`)** → `parm.set()` + `node.cook(force=True)` via `dispatch_to_main`
- **pane focus gain** → read `parm.unexpandedString()` → push to `nvim.current.buffer[:]`
- **`unpin()` or pane close** → disconnect pynvim, clear sync

The nvim pane exposes its socket path via `widget._nvim_sock` (a named pipe on Windows, a Unix socket on Linux). `pynvim.attach('socket', path=sock)` connects to it. The connection is made in a background thread; `dispatch_to_main` handles the parm writes.

---

## Session Bootstrap (`pythonrc.py`)

`pythonrc.py` at the repo root runs at Houdini Python init (via `HOUDINI_PATH` discovery). It does two things only:

1. Path setup (already handled by `void-ghostty.json`)
2. `import void_ghostty`

It does NOT start servers. Servers start on-demand: either when the panel is opened (`_panel.py` calls `void_ghostty._ensure_servers()`), or explicitly from a shelf tool or `456.py`.

```python
# pythonrc.py
import void_ghostty  # registers the package, nothing else
```

---

## Integration Details

### Claude Code + Codex (MCP)

Claude Code and Codex both support `--mcp-config <path>`. A config file at `.vg/mcp.json` (relative to the hip file) points at the MCP server started by Void Ghostty:

```json
{
  "mcpServers": {
    "houdini": {
      "command": "python",
      "args": ["-m", "void_ghostty._mcp_client_stub"],
      "env": { "VG_MCP_PORT": "8766" }
    }
  }
}
```

Tools exposed:
- `get_scene_info` — node tree summary, current hip path, selected nodes
- `get_node(path)` — node type, parameter values, children
- `set_parm(path, value)` — set a parameter value (dispatched to main thread)
- `cook_node(path)` — force cook (dispatched to main thread)
- `run_python(code)` — execute arbitrary Python in Houdini context (dispatched to main thread)
- `get_geometry(path)` — point/prim count, attribute names from cooked geometry

The MCP server in `_mcp.py` runs on a daemon thread and calls `dispatch_to_main` for all `hou.*` operations.

### Strudel (OSC)

Strudel's `@strudel/osc` package sends OSC messages to UDP :57120. The OSC server in `_osc.py`:

```python
# Generic handler: OSC address /houdini/<parm_path> maps to hou parm
def on_houdini(address, *args):
    parm_path = address[len('/houdini'):]  # e.g. /houdini/obj/geo1/noise1/amp
    dispatch_to_main(lambda: hou.parm(parm_path).set(float(args[0])))
```

Strudel → Houdini direction: pattern events drive parameter values in real time.  
Houdini → Strudel direction: use `python-osc`'s `SimpleUDPClient` to send OSC from any Python pane or callback.

Example bidirectional feedback loop: Houdini geometry point density (computed each cook) sent back as an OSC value that modulates a Strudel pattern's gain or frequency. Close the loop with `python-osc` sending to Strudel's WebSocket relay.

For continuous streams (amplitude, LFO), prefer CHOP OSC In nodes in Houdini — they handle sub-frame resolution without Python overhead. The Python OSC bridge is best for discrete events (note triggers, state changes, beat-driven scene mutations).

### Generic HTTP (FastAPI)

Any tool that can make HTTP calls can reach Houdini:

```bash
curl -X POST http://127.0.0.1:8765/exec \
  -H "Content-Type: application/json" \
  -d '{"code": "hou.node(\"/obj/hero\").parm(\"tx\").set(5.0)"}'
```

Useful for: shell scripts, external build tools, web-based editors, Strudel control plane (separate from OSC stream), custom integration scripts in the pipeline.

---

## Void Suite Relationship

```
$GHOSTTY   — developer surface: code authoring, AI agents, live coding
$OXD       — authoring bridge: Houdini → Crucible asset export (Monaco editor planned here)
$CRUCIBLE  — runtime: Rust/Vulkan game engine, LuaJIT scripts, hot reload
```

Void Ghostty and Crucible never communicate directly. The interface is files on disk:
- nvim `:w` (or `BufWritePost`) → Crucible file watcher → hot reload Lua scripts
- `oxd_run` cook → exports `.entity`, `.mesh`, `.anim` etc → Crucible reloads scene

The Strudel/OSC integration is Void Ghostty only — it modifies the live Houdini scene in response to music patterns, which can then be exported to Crucible via `oxd_run`.

---

## Windows Notes

1. **asyncio event loop policy.** If any background thread uses `asyncio` (FastAPI via uvicorn), set this before spawning threads:
   ```python
   import sys, asyncio
   if sys.platform == 'win32':
       asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
   ```
   Omit on Linux (no-op).

2. **nvim socket path.** Use a named pipe on Windows, Unix socket on Linux:
   ```python
   import uuid, os
   if os.name == 'nt':
       sock = rf'\\.\pipe\vg_nvim_{uuid.uuid4().hex[:8]}'
   else:
       sock = f'/tmp/vg_nvim_{uuid.uuid4().hex[:8]}.sock'
   ```

3. **OSC UDP.** Cross-platform, no issues.

4. **Ghostty external embedding.** `QWindow::fromWinId` re-parenting of Ghostty as a subprocess inside the panel is unreliable on Windows with GPU compositing. Deprioritised. Terminal panes via `TerminalWidget` (libghostty-vt) are the correct path.
