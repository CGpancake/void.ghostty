# Void Ghostty — Build Spec v0.5

> **Display name**: Void Ghostty  
> **Repo**: `void.ghostty`  
> **Package file**: `void-ghostty.json`  
> **Global env var**: `$GHOSTTY`  
> **Part of**: Void Monolith suite — `$GHOSTTY`, `$OXD`, `$CRUCIBLE`

---

## Build Progress

| Phase | Status | Notes |
|---|---|---|
| 0 — Repo Scaffold + Package Loads | ✅ Complete | Package registers, `import void_ghostty` works |
| 1 — Python Panel Opens | ✅ Complete | Pane tab appears in Houdini |
| 2 — PTY + Shell | ✅ Complete | winpty on Windows, pyte fallback retained |
| 3 — Three-Pane Layout | ✅ Complete | Dynamic split/close multiplexer (Ctrl+Shift+H/B/X), configurable panes |
| 4 — libghostty Render Surface | ✅ Complete | `vg.dll` built and in use; full VT rendering, scrollback, mouse, key encoding |
| 5 — pynvim Sync + Panel-Node Wiring | ⬜ Not started | `_nvim_sync` stub exists, wired to None |
| 6 — Follow / Pinned / Free Modes | 🔶 Partial | Scaffolding in `_panel.py`; follow/pinned/free logic present but untested end-to-end |
| 7 — Parameter Injection API | ⬜ Not started | `register()` stub exists in `__init__.py` |

**Session ended 2026-04-12.** Phases 0–4 fully working. The multiplexer (Phase 3) was extended beyond the original spec — panes are dynamic (split/close at runtime) rather than a fixed three-pane layout. Next session: evaluate Phase 5, 6, or 7, or address remaining minor resize-debounce glitch in deeply nested splits (5+ panes).

---

## What It Is

Void Ghostty is a GPU-accelerated terminal development environment embedded inside Houdini as a native Python Panel, built on **libghostty** — the C core of the Ghostty terminal (github.com/ghostty-org/ghostty).

It runs natively inside Houdini. `import hou` works. The full session is available exactly as it is in Hython — node graph, cooked geometry, parameters, project file. There is no bridge, no IPC, no file-based indirection for session access.

Void Ghostty has two surfaces inside Houdini:

1. **The Panel** — a first-class Houdini pane tab running a three-pane terminal multiplexer (Neovim / Claude Code / Shell)
2. **The Parameter** — a registration API that any HDA can call to make Void Ghostty aware of that node, enabling parameter sync, hook dispatch, and node-aware editing

Void Ghostty does not ship an HDA or SOP node. Node integration is the HDA author's concern — Void Ghostty provides the API they call into.

---

## Goals

- Neovim as the editor for all HDA script authoring (VEX, Python, Lua, expressions)
- Claude Code / Codex CLI running in the same terminal, with live Houdini session context
- Terminal verification and shell access within the Houdini session
- Node-aware editing: when a node registers itself, the terminal reflects and syncs that node's parameters
- Parameter injection: any HDA author can embed a Void Ghostty config block with custom hooks
- Foundation for future extensions — linters, live preview, asset pipelines, anything a terminal can do

---

## Package Layout

```
$GHOSTTY/                         <- set GHOSTTY as a system env var before launching Houdini
├── void-ghostty.json             <- Houdini package definition
├── pythonrc.py                   <- session init: path setup and import only (Houdini PATH root)
├── BUILD_NOTES.md                <- ghostty commit pin, build commands
├── bin/
│   ├── windows/
│   │   ├── ghostty-vt.dll        <- libghostty-vt C API (compiled, committed ~2-4MB)
│   │   ├── vg.dll                <- ABI wrapper (compiled from src/_lib.cpp)
│   │   ├── nvim.exe              <- Neovim portable binary
│   │   └── nvim-win64/           <- Neovim runtime (share/, lib/)
│   └── linux/
│       ├── libghostty-vt.so      <- libghostty-vt C API (compiled, committed)
│       ├── libvg.so              <- ABI wrapper (compiled from src/_lib.cpp)
│       ├── nvim                  <- Neovim portable binary
│       └── nvim-linux64/         <- Neovim runtime
├── python/
│   └── void_ghostty/
│       ├── __init__.py           <- public API: register(), __version__
│       ├── _terminal.py          <- TerminalWidget, PTY bridge, pyte screen (Phases 2-3)
│       ├── _nvim_sync.py         <- pynvim RPC thread, hdefereval wrappers
│       ├── _panel.py             <- three-pane QSplitter, onCreateInterface()
│       └── _hooks.py             <- hook dispatch, config block parser
├── python_deps/                  <- bundled deps, no pip on target
│   ├── pynvim/
│   ├── msgpack/
│   ├── greenlet/
│   ├── pyte/                     <- VT emulator for Phases 2-3 (replaced by libghostty in Phase 4)
│   └── pywinpty/                 <- Windows only (pty is stdlib on Linux); import name is `winpty`
├── python_panels/
│   └── void_ghostty.pypanel      <- XML panel registration
├── scripts/                      <- Houdini searches <HOUDINI_PATH>/scripts/ for event scripts
│   └── (empty — no 456.py needed; HDA authors call register() in their own OnCreated)
├── toolbar/
│   └── void_ghostty.shelf        <- shelf tools
├── include/
│   └── ghostty.h                 <- C API header (from ghostty source)
└── src/
    └── _lib.cpp                  <- ABI isolation layer (all ghostty_* calls)
```

No `dso/`, no `CMakeLists.txt`, no compiled HDK plugin. Void Ghostty is pure Python + one compiled terminal library.

---

## Package Registration

**Prerequisite:** Set `GHOSTTY` as a system environment variable pointing to the repo root before launching Houdini. Example: `export GHOSTTY=/path/to/VoidMonolith/Ghostty` (Linux) or set via System Properties (Windows). All paths in the package JSON are derived from `$GHOSTTY`.

**`void-ghostty.json`**:
```json
{
  "env": [
    {
      "PYTHONPATH": {
        "value": "$GHOSTTY/python",
        "method": "prepend"
      }
    },
    {
      "PYTHONPATH": {
        "value": "$GHOSTTY/python_deps",
        "method": "prepend"
      }
    },
    {
      "PATH": {
        "value": "$GHOSTTY/bin/windows",
        "method": "prepend",
        "houdini_os": "windows"
      }
    },
    {
      "PATH": {
        "value": "$GHOSTTY/bin/linux",
        "method": "prepend",
        "houdini_os": "linux"
      }
    }
  ],
  "path": "$GHOSTTY",
  "load_package_once": true
}
```

---

## Component Responsibilities

**libghostty-vt + `src/_lib.cpp`**
Void Ghostty uses `ghostty/vt.h` — the cross-platform libghostty-vt library (Linux, Windows, macOS, WASM). This is NOT `ghostty.h`, which is macOS/iOS only. See `BUILD_NOTES.md` for the full distinction.

All calls to the libghostty-vt C API are isolated in `_lib.cpp`. This is the only file that changes when the ABI updates. `_lib.cpp` is compiled as a thin separate shared library (`bin/windows/vg.dll` / `bin/linux/libvg.so`) wrapping `ghostty-vt.dll`/`libghostty-vt.so`. Python loads `vg.dll`/`libvg.so` via ctypes. Build commands are documented in `BUILD_NOTES.md`; no CMakeLists.txt. Committed binaries: `ghostty-vt.dll`/`libghostty-vt.so` and `vg.dll`/`libvg.so`. Source commit pinned in `BUILD_NOTES.md`.

**Python Panel (`_panel.py`)**
Construction only. Creates a `QSplitter` with three independent `TerminalWidget` panes. After construction the panel is inert until focused — no paint events, no PTY reads, no overhead while the user is elsewhere in Houdini. Registered via `void_ghostty.pypanel` XML, appears in Houdini pane tab menu as "Void Ghostty".

**PTY bridge (`_terminal.py`)**
`TerminalWidget(QWidget)` owns a `PtyProcess`. Platform selection at runtime via `os.name`:
- Windows: `winpty.PtyProcess` (ConPTY, bundled as `pywinpty` in `python_deps/` — package name is `pywinpty`, Python import is `import winpty`)
- Linux: `pty` stdlib module (no bundle needed)

In Phases 2–3, VT output is interpreted by `pyte` (pure Python VT emulator, bundled in `python_deps/`). `TerminalWidget` feeds PTY bytes into a `pyte.Screen` and paints `screen.display`. The pyte layer is replaced entirely by the libghostty render surface in Phase 4.

Each pane is an independent `TerminalWidget` with its own PTY and process. No tmux dependency — multiplexing is handled by `QSplitter`.

**pynvim RPC thread (`_nvim_sync.py`)**
Lightweight background thread, only active when the pane is focused. Every `hou.*` call dispatched via `hdefereval.executeDeferred()` without exception — including `parm.set()`.
- `TextChanged` / `TextChangedI` → debounce timer (~30hz, cancel-and-reschedule) → `hou.parm("code").set(text)` — display sync only, no cook
- `BufWritePost` → set parm + `node.cook(force=True)` via hdefereval
- `BufLeave` → set parm + `node.cook(force=True)` via hdefereval
- On focus gain → push current parm value into Neovim buffer via `nvim.current.buffer[:]`

**`pythonrc.py`**
Runs at Python init (Houdini searches `HOUDINI_PATH` root directories for `pythonrc.py`). `import void_ghostty` and path setup only. Never scene logic. File lives at `$GHOSTTY/pythonrc.py` — the repo root is a `HOUDINI_PATH` entry via `path: "$GHOSTTY"` in the package JSON.

Node re-registration is not Ghostty's responsibility. Any HDA that wants Void Ghostty awareness calls `void_ghostty.register(hou.pwd())` in its own `OnCreated` script. There is no `456.py`.

---

## The Panel

Void Ghostty appears in Houdini's pane tab menu. It can be docked anywhere Houdini allows a pane — floating, split, or embedded alongside the parameter editor. It is a standard Houdini pane in every respect.

### Three-Pane Layout (Default)

```
+---------------------+------------------+
|                     |                  |
|   Neovim            |   Claude Code    |
|   (node-aware)      |   CLI            |
|                     |                  |
+---------------------+------------------+
|   Shell                                |
+----------------------------------------+
```

Three independent `TerminalWidget` instances in a `QSplitter`. Each owns its own PTY and process. Neovim pane respects follow/pinned/free mode. Claude Code and Shell panes are always free mode. Layout is configurable — three-pane is the opinionated default.

### Terminal Modes

**Follow Mode**
Tracks Houdini node selection via `hou.ui.addEventLoopCallback()`. When the user clicks a registered node, the terminal context switches — loads that node's `code` parm into the Neovim buffer, updates the node path display. Mirrors Parameter Editor behaviour. Unregistered nodes are ignored.

**Pinned Mode**
Locked to a specific registered node by path, resolved via `node.sessionId()`. Survives Network Editor selection changes. Set by drag-dropping a node into the pane or via shelf tool.

**Free Mode**
No node link. Standalone terminal — shell, Claude Code CLI, Hython REPL. Default state before any node is pinned or followed.

---

## The Parameter

Any HDA can make itself known to Void Ghostty by calling `void_ghostty.register(node)` in its `OnCreated` script. This is the entire integration contract from the HDA side — one line.

```python
# Inside any HDA's OnCreated script
import void_ghostty
void_ghostty.register(hou.pwd())
```

Void Ghostty reads a hidden `_vg_config` spare parameter (JSON string) from the registering node. The HDA author is responsible for adding this spare parm with their desired config:

```json
{
  "hooks": {
    "on_cook":        "scripts/on_cook.lua",
    "on_parm_change": "scripts/sync.py",
    "on_focus":       "scripts/load_context.py"
  },
  "editor_mode":    "vex",
  "claude_context": "prompts/vex_assistant.md",
  "watch_parms":    ["code", "iterations", "threshold"],
  "cook_trigger":   "on_leave"
}
```

`cook_trigger` options: `"on_leave"` (default), `"on_write"`, `"manual"`, `"debounce_300ms"`.

Hook dispatch and context loading live entirely in Void Ghostty. The HDA declares rules only — it does not contain terminal logic. Registration is stored keyed by `node.sessionId()` and cleaned up on the `456.py` re-registration cycle.

---

## Parameter Display Sync

Display sync and cook are explicitly decoupled. The parameter field in Houdini mirrors the Neovim buffer live as the user types — this is display only, no cook triggered. Cook fires on the configured trigger.

| Event | Action |
|---|---|
| Neovim `TextChanged` (typing) | `hou.parm("code").set(text)` via hdefereval — parameter field mirrors buffer, **no cook** |
| Neovim `BufWritePost` (`:w`) | set parm + `node.cook(force=True)` via hdefereval |
| Click away from pane (`BufLeave`) | set parm + `node.cook(force=True)` via hdefereval |
| Focus pane on registered node | push node's `code` parm value into Neovim buffer |

Display sync path: Neovim buffer → pynvim async notify → `hdefereval` → `hou.parm().set()` → Qt repaints parameter field. No geometry evaluation, no VEX compile. Cost is one deferred call + one Qt paint event.

---

## Drag and Drop — Node → Path

`TerminalWidget` implements `dragEnterEvent` / `dropEvent`. Accepts Houdini node path mime type dragged from the Network Editor.

On drop:
- **Free mode**: pastes node path string at terminal cursor — same as dragging into any path field
- **Pinned / Follow mode**: calls `panel.set_node(dropped_path)`, opens linked Network Editor view via `hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor).setCurrentNode(node)`

---

## Session Awareness

The terminal process runs inside Houdini. `import hou` works in all three panes without any bridge. Available immediately:

```python
hou.hipFile.path()                    # current project file — Claude/Codex context feed point
hou.node(path).type().definition()    # HDA source for any node
hou.node(path).geometry()             # cooked geometry (readable via hou.Geometry)
hou.parm(path).eval()                 # live parameter values
hou.session                           # session-scoped globals
```

---

## Relationship to Void Suite

```
$GHOSTTY  ->  authors and edits  ->  .vex / .py / .lua / .ron files on disk
$OXD      ->  bridges            ->  Houdini <-> Crucible
$CRUCIBLE ->  executes           ->  watches files, hot-reloads (Bevy pattern)
```

Void Ghostty and Crucible never communicate directly. Interface is files on disk: `BufWritePost` → Neovim autocmd writes file → Crucible file watcher hot-reloads.

Crucible DSO mode (renders in Houdini SOP viewport via HDK `RV_Render`) is a separate spec item in `void.crucible`.

---

## Build Phases

### Phase 0 — Repo Scaffold + Package Loads
Goal: `void-ghostty.json` registers cleanly, `import void_ghostty` works in Hython.

- Set `GHOSTTY` as a system environment variable pointing to the repo root (before launching Houdini)
- Create directory tree as shown in Package Layout (empty placeholders where needed)
- Write `void-ghostty.json` per Package Registration section
- Symlink or copy `void-ghostty.json` to Houdini user packages dir:
  - Windows: `%HOUDINI_USER_PREF_DIR%\packages\` (e.g. `C:\Users\<user>\Documents\houdini<version>\packages\`)
  - Linux: `$HOUDINI_USER_PREF_DIR/packages/` (e.g. `~/houdini<version>/packages/`)
- Create `python/void_ghostty/__init__.py` with `__version__ = "0.1.0"` stub
- Create `pythonrc.py` at repo root with `import void_ghostty` only

Validation:
```python
# hython validation_phase0.py
import hou, void_ghostty, sys
print("version:", void_ghostty.__version__)
print("GHOSTTY:", hou.getenv("GHOSTTY"))
print("paths:", [p for p in sys.path if "Ghostty" in p])
```

---

### Phase 1 — Python Panel Opens in Houdini
Goal: "Void Ghostty" appears in pane tab menu and opens a plain Qt widget. Proves `.pypanel` registration before any terminal work.

- Write `python_panels/void_ghostty.pypanel`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<pythonPanelDocument>
  <interface name="void_ghostty" label="Void Ghostty" icon="MISC_python">
    <script><![CDATA[
from void_ghostty._panel import onCreateInterface
    ]]></script>
    <includeInPaneTabMenu menu_position="0" create_separator="false"/>
  </interface>
</pythonPanelDocument>
```
- Write `_panel.py`: `onCreateInterface()` returns a plain `QLabel("Void Ghostty — Phase 1")` — no terminal yet
- Test: open Houdini → new pane tab → "Void Ghostty" appears and opens

Validation:
```python
# hython validation_phase1.py
import hou
panels = [p.name() for p in hou.ui.pythonPanelInterfaces()]
assert "void_ghostty" in panels, f"not registered, found: {panels}"
print("Panel registration: PASS")
```

---

### Phase 2 — PTY + Shell in Pane
Goal: A real shell runs inside the Python Panel widget with correct VT rendering.

- Write `_terminal.py`:
  - `PtyProcess`: platform-conditional at runtime via `os.name`
    - Windows: `winpty.PtyProcess` (bundled as `pywinpty`)
    - Linux: `pty` stdlib
  - `TerminalWidget(QWidget)`: owns a `PtyProcess`; feeds PTY bytes into a `pyte.Screen`; paints `screen.display` on each read. This pyte layer is a Phase 2–3 scaffold — it is replaced by the libghostty render surface in Phase 4.
- Update `_panel.py`: `onCreateInterface()` returns a single `TerminalWidget` running a shell
- Confirm: terminal is idle (no paint) when pane is not focused

Validation:
```python
# hython validation_phase2.py
import os, sys, hou
sys.path.insert(0, hou.getenv("GHOSTTY") + "/python_deps")
if os.name == "nt":
    import winpty
    pty = winpty.PtyProcess.spawn(["cmd.exe"])
    import time; time.sleep(0.5)
    data = pty.read(1024)
    pty.terminate()
    print("PTY bytes:", len(data))
    import pyte
    screen = pyte.Screen(80, 24)
    stream = pyte.ByteStream(screen)
    stream.feed(data)
    print("pyte lines:", len([l for l in screen.display if l.strip()]))
    print("PTY + pyte test: PASS")
else:
    print("Linux pty + pyte: manual test in Houdini")
```

---

### Phase 3 — Neovim + Claude Code + Three-Pane Layout
Goal: All three panes running in Houdini. Claude Code CLI session-aware.

- Bundle Neovim portable release:
  - Windows: `nvim-win64.zip` → extract to `bin/windows/nvim-win64/`, copy `nvim.exe`
  - Linux: `nvim-linux64.tar.gz` → extract to `bin/linux/nvim-linux64/`, copy `nvim`
- Update `_panel.py`: `onCreateInterface()` returns `QSplitter` with three `TerminalWidget` instances
  - Pane 0: `nvim` process
  - Pane 1: `claude` CLI (or shell until configured)
  - Pane 2: shell
- Confirm: `import hou` works in all three panes
- Confirm: Neovim TUI renders correctly, full colour

---

### Phase 4 — libghostty Render Surface
Goal: Replace raw PTY byte painting with libghostty GPU-accelerated render surface.

**Before writing any code**: read `ghostty.h` and confirm the Win32 / Linux window attachment mechanism. Record findings in `BUILD_NOTES.md`. Do not assume any specific function name exists.

**Before writing any code**: read `BUILD_NOTES.md` in full. Understand the two-API distinction (ghostty.h vs ghostty/vt.h). Read Ghostling's `main.c` as the reference implementation.

- Build `ghostty-vt.dll` (Windows) and `libghostty-vt.so` (Linux) from ghostty source with Zig:
  ```bash
  git clone https://github.com/ghostty-org/ghostty.git
  cd ghostty && git checkout 0790937d03df6e7a9420c61de91ce520a85fe4ef
  zig build lib-vt
  # Output: zig-out/bin/ghostty-vt.dll (Windows) or zig-out/lib/libghostty-vt.so (Linux)
  ```
  Record source commit hash in `BUILD_NOTES.md` (already pinned).
- Copy library outputs to `bin/windows/` and `bin/linux/`
- Copy `include/ghostty/` tree (all headers under `include/ghostty/`) to `$GHOSTTY/include/ghostty/`
- Write `src/_lib.cpp` — ABI isolation layer. Wraps `ghostty/vt.h`. All `ghostty_*` calls here only. Exposes: `vg_terminal_new/free/write/resize`, `vg_render_state_new/free/update`, `vg_render_row_cells`, `vg_render_colors`, `vg_render_cursor`, `vg_key_encode`, `vg_mouse_encode`, `vg_scroll`, `vg_scrollbar`
- Compile `_lib.cpp` into `vg.dll` / `libvg.so` (see BUILD_NOTES.md for exact commands):
  ```bash
  # Linux
  g++ -shared -fPIC src/_lib.cpp -Lbin/linux -lghostty-vt -Iinclude \
      -Wl,-rpath,'$ORIGIN' -o bin/linux/libvg.so
  # Windows (MSVC)
  cl /LD src/_lib.cpp /I include /link bin/windows/ghostty-vt.lib /OUT:bin/windows/vg.dll
  ```
- Update `TerminalWidget`: replace pyte with libghostty-vt cell iteration. Feed PTY bytes → `vg_terminal_write()`. Per frame: `vg_render_state_update()` → iterate rows/cells → QPainter per cell. Key input → `vg_key_encode()` → write to PTY. Mouse input → `vg_mouse_encode()` → write to PTY.

Validation:
```python
# hython validation_phase4.py
import ctypes, os, hou
bin_dir = hou.getenv("GHOSTTY") + ("/bin/windows" if os.name == "nt" else "/bin/linux")
lib_name = "vg.dll" if os.name == "nt" else "libvg.so"
lib = ctypes.CDLL(os.path.join(bin_dir, lib_name))
print("libvg loaded:", lib)
term = lib.vg_terminal_new(80, 24)
assert term, "vg_terminal_new returned null"
lib.vg_terminal_free(term)
print("libvg terminal create/free: PASS")
```

---

### Phase 5 — pynvim Sync + Panel-Node Wiring
Goal: Typing in Neovim updates registered node's `code` parm live (display only). `:w` triggers cook.

- Write `_nvim_sync.py`:
  - Attach pynvim: `pynvim.attach("child", argv=[nvim_exe, "--embed"])`
  - `TextChanged` / `TextChangedI` → debounce (`threading.Timer`, cancel-and-reschedule, ~30hz) → `hdefereval.executeDeferred(lambda: hou.parm(code_path).set(text))`
  - `BufWritePost` → `hdefereval.executeDeferred(lambda: [hou.parm(code_path).set(text), node.cook(force=True)])`
  - `BufLeave` → same as `BufWritePost`
  - On focus gain → `nvim.current.buffer[:] = hou.parm(code_path).eval().splitlines()`
- `_panel.py` gains `set_node(node_path)` — called by follow/pinned mode logic

Validation:
```python
# hython validation_phase5.py
import hou, hdefereval, time
# Use any existing node with a string parm for this test
geo = hou.node("/obj").createNode("geo", "sync_test")
attribwrangle = geo.createNode("attribwrangle")
code_path = attribwrangle.parm("snippet").path()
hdefereval.executeDeferred(lambda: hou.parm(code_path).set("x = 42"))
time.sleep(0.1)
assert hou.parm(code_path).eval() == "x = 42"
geo.destroy()
print("hdefereval sync: PASS")
```

---

### Phase 6 — Follow / Pinned / Free Modes + Drag-Drop
Goal: Panel switches context when a registered node is selected. Drag-drop sets pinned mode.

- Follow mode: `hou.ui.addEventLoopCallback(on_selection_change)` → queries `hou.selectedNodes()` → if selected node is registered, calls `panel.set_node()`
- Pinned mode: `dropEvent` in `TerminalWidget` accepts Houdini node mime type → `panel.set_node(dropped_path)`
- Free mode: `panel.set_node(None)`
- Node drop opens linked Network Editor: `hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor).setCurrentNode(node)`

Validation:
```python
# hython validation_phase6.py
import hou
print("event loop callbacks:", len(hou.ui.eventLoopCallbacks()))
test = hou.node("/obj").createNode("geo", "follow_test")
assert hou.node(test.path()) is not None
test.destroy()
print("follow mode path resolution: PASS")
```

---

### Phase 7 — Parameter Injection API
Goal: `void_ghostty.register(node)` works. Hook config parsed and dispatched correctly.

- `__init__.py`: `register(node)` reads `_vg_config` spare parm (JSON), stores in `_registry` keyed by `node.sessionId()`
- `_hooks.py`: `dispatch(event, node)` looks up config, runs hook script

Validation:
```python
# hython validation_phase7.py
import hou, void_ghostty

# Simulate an HDA that has called register()
geo = hou.node("/obj").createNode("geo", "hook_test")
node = geo.createNode("attribwrangle", "vg_hook")
config = '{"hooks": {"on_cook": "echo cook"}, "cook_trigger": "on_leave"}'
node.addSpareParmTuple(
    hou.StringParmTemplate("_vg_config", "VG Config", 1, default_value=[config])
)
void_ghostty.register(node)
reg = void_ghostty._registry.get(node.sessionId())
assert reg is not None, "node not registered"
assert reg["cook_trigger"] == "on_leave"
geo.destroy()
print("register() + config parse: PASS")
```

---

## Reference Material

- `oxd_wrangle` HDA — parameter layout reference for any HDA that registers with Void Ghostty
- `$OXD` and `$CRUCIBLE` package JSON — pattern for `void-ghostty.json` structure
- Ghostty source: `ghostty-org/ghostty` — C API headers at `include/ghostty/vt.h` (and full `include/ghostty/` tree). **Not** `ghostty.h` (that is macOS/iOS only). See `BUILD_NOTES.md`.
- Ghostling reference: `ghostty-org/ghostling` — canonical cross-platform libghostty-vt integration using Raylib. Read `main.c` before Phase 4.
- pynvim: `neovim/pynvim` — `nvim_buf_set_lines`, `nvim_buf_attach`, msgpack RPC
- Neovim portable releases: `github.com/neovim/neovim/releases` — `nvim-win64.zip` / `nvim-linux64.tar.gz`

---

## Notes for Claude Code

- Void Ghostty is a panel and a parameter API. It does not ship a SOP node, HDA, or compiled HDK plugin. There is no `dso/`, no `CMakeLists.txt`, no `hcustom` build.
- `$GHOSTTY` is the package root. `bin/`, `python/`, `src/` etc. sit directly under it — no subfolder layer.
- `python_panels/`, `toolbar/`, `scripts/` are directory conventions, not package JSON keys.
- `_lib.cpp` is the ABI isolation layer — all `ghostty_*` calls go here and nowhere else in the codebase.
- Every `hou.*` call from the pynvim background thread goes through `hdefereval.executeDeferred()` without exception — including `parm.set()`, not just cook triggers.
- Display sync (`TextChanged`) and cook trigger (`BufWritePost` / `BufLeave`) are separate code paths. Do not conflate them.
- `pythonrc.py` is import and path setup only. It lives at `$GHOSTTY/pythonrc.py` (repo root), not in `scripts/`. There is no `456.py` — node re-registration is the HDA author's responsibility via `OnCreated`.
- PTY backend is platform-conditional at runtime via `os.name`: `import winpty` (pywinpty package) on Windows, `pty` stdlib on Linux.
- Before writing Phase 4 code, read `BUILD_NOTES.md` in full and read Ghostling `main.c`. Ghostty exposes TWO C APIs — Void Ghostty uses `ghostty/vt.h` (libghostty-vt), NOT `ghostty.h` (macOS/iOS only).
- `_lib.cpp` wraps `ghostty/vt.h` and compiles to `vg.dll` / `libvg.so` via a standalone `cl` / `g++` one-liner (no CMakeLists). Build commands are in `BUILD_NOTES.md`. On Linux, `libvg.so` uses `-Wl,-rpath,'$ORIGIN'` so it finds `libghostty-vt.so` in the same directory without `LD_LIBRARY_PATH`.
- libghostty-vt ABI is pre-1.0 — the source commit is pinned in `BUILD_NOTES.md`. If the committed binary fails, rebuild from the pinned commit.
- Phase 4 rendering is cell-based, not native-surface-based: feed PTY bytes into `vg_terminal_write()`, call `vg_render_state_update()`, iterate cells with `vg_render_row_cells()`, paint each cell with QPainter. No native window attachment needed. Ghostling's render loop is the direct reference.
- `import hou` works in the terminal process because it runs inside Houdini. No bridge needed for session access.
