"""Void Ghostty — terminal development environment embedded in Houdini."""

from __future__ import annotations

import queue as _queue
import threading as _threading

__version__ = "0.1.0"

_registry: dict = {}

# ---------------------------------------------------------------------------
# dispatch_to_main — thread-safety primitive for background servers
# ---------------------------------------------------------------------------
# Any hou.* call from a background thread (FastAPI, OSC, MCP) goes through
# this queue, drained on Houdini's main Qt thread each event loop tick.
# ---------------------------------------------------------------------------

_task_queue: _queue.Queue = _queue.Queue()


def _drain_queue() -> None:
    """Drain pending tasks on Houdini's main thread. Called by addEventLoopCallback."""
    while not _task_queue.empty():
        try:
            fn, box, evt = _task_queue.get_nowait()
        except _queue.Empty:
            break
        try:
            box.append(fn())
        except Exception as e:
            box.append(e)
        evt.set()


def dispatch_to_main(fn):
    """Execute fn() on Houdini's main thread. Blocks until complete.

    Use for any hou.* call from a background thread — parm.set(), node.cook(),
    hou.node(), etc. Raises the exception if fn() raised one.

    Example:
        from void_ghostty import dispatch_to_main
        dispatch_to_main(lambda: hou.node('/obj/geo1').parm('tx').set(5.0))
    """
    box: list = []
    evt = _threading.Event()
    _task_queue.put((fn, box, evt))
    evt.wait()
    r = box[0]
    if isinstance(r, Exception):
        raise r
    return r


def _ensure_dispatch() -> None:
    """Register _drain_queue with Houdini's event loop. Idempotent. Called from panel __init__."""
    try:
        import hou
        if _drain_queue not in hou.ui.eventLoopCallbacks():
            hou.ui.addEventLoopCallback(_drain_queue)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Panel access helpers — called by shelf tools or external scripts
# ---------------------------------------------------------------------------

def open_shell() -> None:
    """Open a shell pane (bash / cmd.exe) inside the Void Ghostty panel."""
    from void_ghostty._panel import get_panel
    panel = get_panel()
    if panel is not None:
        panel._split_shell_pane()


def list_themes() -> None:
    """Print all available theme names (bundled + user themes dir + ghostty binary).

    Call from the Python REPL to see what you can set in your Ghostty config:
        theme = Dracula+
    """
    from void_ghostty._config import list_available_themes
    names = list_available_themes()
    print(f"{len(names)} themes available:")
    col = 4
    w = max((len(n) for n in names), default=0) + 2
    for i in range(0, len(names), col):
        print("  " + "".join(n.ljust(w) for n in names[i:i + col]))


def vg_info() -> None:
    """Print config and theme loading status.

    Use from the REPL to diagnose why a theme or font setting isn't applying:
        >>> vg_info()
    """
    import os, sys
    try:
        from void_ghostty._config import _config_path, load_config
        p = _config_path()
        cfg = load_config()
        print(f"Config file : {p if p else 'NOT FOUND — using defaults'}")
        print(f"Theme       : {cfg.theme}")
        print(f"Font        : {cfg.font_family}  {cfg.font_size}pt")
        if cfg.font_fallbacks:
            print(f"Fallbacks   : {', '.join(cfg.font_fallbacks)}")
        if cfg.keybinds:
            for action, combo in cfg.keybinds.items():
                print(f"Keybind     : {action} = {combo}")
    except Exception as e:
        print(f"Config load error: {e}")
    try:
        from void_ghostty._themes import _themes_dir
        td = _themes_dir()
        confs = list(td.glob("*.conf")) if td.is_dir() else []
        print(f"Themes dir  : {td}  ({len(confs)} .conf files)")
    except Exception as e:
        print(f"Themes dir error: {e}")
    try:
        from void_ghostty._config import find_ghostty_binary
        binary = find_ghostty_binary()
        print(f"Ghostty bin : {binary if binary else 'not found'}")
    except Exception as e:
        print(f"Binary search error: {e}")


# ---------------------------------------------------------------------------
# Node registration API
# ---------------------------------------------------------------------------

def register(node) -> None:
    """Register a Houdini node with Void Ghostty.

    Call from any HDA's OnCreated script:
        import void_ghostty
        void_ghostty.register(hou.pwd())

    Reads the optional '_vg_config' spare parameter (JSON) from the node
    and stores the parsed config keyed by node.sessionId().
    """
    import json

    try:
        parm = node.parm("_vg_config")
        config = json.loads(parm.eval()) if parm else {}
    except Exception:
        config = {}

    _registry[node.sessionId()] = config
