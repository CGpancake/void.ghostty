"""Void Ghostty — hook dispatch (Phase 7).

Hook scripts are declared by HDA authors in the '_vg_config' spare parm.
This module resolves and executes them on the appropriate event.

Supported events:
  on_cook        — fired after node.cook()
  on_parm_change — fired when a watched parm changes (display sync)
  on_focus       — fired when the terminal switches context to this node

Hook values are file paths relative to the HDA's definition directory.
Scripts receive a globals dict with: node, hou, event_name.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Event name constants
ON_COOK        = "on_cook"
ON_PARM_CHANGE = "on_parm_change"
ON_FOCUS       = "on_focus"


def dispatch(event_name: str, node, extra: dict[str, Any] | None = None) -> None:
    """Run the hook script for event_name on node, if configured.

    Silently no-ops if:
      - node is not registered with Void Ghostty
      - no hook is configured for this event
      - the hook script file does not exist
    """
    from void_ghostty import _registry

    config = _registry.get(node.sessionId())
    if not config:
        return

    hooks = config.get("hooks", {})
    script_rel = hooks.get(event_name)
    if not script_rel:
        return

    # Resolve relative to HDA definition directory, then cwd
    script_path = _resolve_script(node, script_rel)
    if script_path is None:
        log.warning("Hook script not found: %s (event=%s)", script_rel, event_name)
        return

    _run_script(script_path, node, event_name, extra or {})


def _resolve_script(node, rel_path: str) -> str | None:
    """Resolve rel_path relative to the HDA definition directory."""
    try:
        hda_def = node.type().definition()
        if hda_def is not None:
            hda_dir = os.path.dirname(hda_def.libraryFilePath())
            candidate = os.path.join(hda_dir, rel_path)
            if os.path.exists(candidate):
                return candidate
    except Exception:
        pass

    # Fallback: relative to cwd
    if os.path.exists(rel_path):
        return os.path.abspath(rel_path)

    return None


def _run_script(script_path: str, node, event_name: str, extra: dict) -> None:
    """Execute a hook script in an isolated namespace."""
    try:
        import hou
    except ImportError:
        hou = None  # type: ignore[assignment]

    script_globals = {
        "node":       node,
        "hou":        hou,
        "event_name": event_name,
        **extra,
    }

    try:
        with open(script_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        exec(compile(source, script_path, "exec"), script_globals)  # noqa: S102
    except Exception as exc:
        log.error("Hook script %s raised: %s", script_path, exc)
