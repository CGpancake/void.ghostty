"""Void Ghostty — panel construction (Phases 3–6).

Three-pane layout:
  Pane 0 — Neovim (node-aware)            top-left
  Pane 1 — Claude Code CLI               top-right
  Pane 2 — Shell (always free mode)       bottom

    +---------------------+------------------+
    |  Neovim             |  Claude Code     |
    |  (node-aware)       |  CLI             |
    +---------------------+------------------+
    |  Shell                                 |
    +----------------------------------------+

Node modes (Phase 6):
  free    — no node link (default)
  follow  — tracks Houdini node selection via event-loop callback
  pinned  — locked to a specific node by sessionId

pynvim sync (Phase 5):
  NvimSync background thread keeps the Neovim buffer in sync with the
  registered node's 'code' parm.
"""

from __future__ import annotations

import os
import weakref
from typing import Optional

try:
    from PySide2.QtWidgets import QWidget, QVBoxLayout, QSplitter, QSizePolicy
    from PySide2.QtCore import Qt
except ImportError:
    from PySide6.QtWidgets import QWidget, QVBoxLayout, QSplitter, QSizePolicy
    from PySide6.QtCore import Qt

from void_ghostty._terminal import TerminalWidget

# Weak reference to the active panel so register() can notify it.
_panel_ref: Optional[weakref.ref] = None


def get_panel() -> Optional["VoidGhosttyPanel"]:
    if _panel_ref is not None:
        return _panel_ref()
    return None


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------

def _nvim_cmd() -> list[str]:
    """Return the command list to launch Neovim.

    Searches bundled runtimes first, falls back to system nvim.
    Handles both old (nvim-win64 / nvim-linux64) and new
    (nvim-win64 / nvim-linux-x86_64) release directory naming.
    """
    ghostty = os.environ.get("GHOSTTY", "")
    if os.name == "nt":
        candidates = [
            os.path.join(ghostty, "bin", "windows", "nvim-win64", "bin", "nvim.exe"),
        ]
        fallback = "nvim.exe"
    else:
        candidates = [
            os.path.join(ghostty, "bin", "linux", "nvim-linux-x86_64", "bin", "nvim"),
            os.path.join(ghostty, "bin", "linux", "nvim-linux64",       "bin", "nvim"),
        ]
        fallback = "nvim"

    for path in candidates:
        if os.path.exists(path):
            return [path]
    return [fallback]


def _shell_cmd() -> list[str]:
    if os.name == "nt":
        return ["cmd.exe"]
    return [os.environ.get("SHELL", "/bin/bash")]


def _claude_cmd() -> list[str]:
    """Claude Code CLI — falls back to shell if `claude` is not on PATH."""
    import shutil
    if shutil.which("claude"):
        return ["claude"]
    return _shell_cmd()


# ---------------------------------------------------------------------------
# Panel widget
# ---------------------------------------------------------------------------

class VoidGhosttyPanel(QWidget):
    """Top-level widget returned to Houdini's pane tab system."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        global _panel_ref
        _panel_ref = weakref.ref(self)

        self._node_path: Optional[str] = None
        self._mode: str = "free"       # "free" | "follow" | "pinned"
        self._pinned_session_id: Optional[int] = None

        self._nvim_pane:   Optional[TerminalWidget] = None
        self._claude_pane: Optional[TerminalWidget] = None
        self._shell_pane:  Optional[TerminalWidget] = None

        self._nvim_sync = None  # NvimSync instance (Phase 5)
        self._follow_callback_installed = False

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Outer vertical splitter: top row | bottom shell
        v_split = QSplitter(Qt.Vertical, self)

        # Top row: horizontal splitter Neovim | Claude Code
        h_split = QSplitter(Qt.Horizontal, v_split)

        self._nvim_pane   = TerminalWidget(cmd=_nvim_cmd(),   parent=h_split)
        self._claude_pane = TerminalWidget(cmd=_claude_cmd(), parent=h_split)
        h_split.addWidget(self._nvim_pane)
        h_split.addWidget(self._claude_pane)
        h_split.setSizes([600, 400])

        # Bottom shell
        self._shell_pane = TerminalWidget(cmd=_shell_cmd(), parent=v_split)

        v_split.addWidget(h_split)
        v_split.addWidget(self._shell_pane)
        v_split.setSizes([600, 200])

        root_layout.addWidget(v_split)

        # Start all three panes
        self._nvim_pane.start()
        self._claude_pane.start()
        self._shell_pane.start()

        # Start pynvim sync thread (Phase 5)
        self._start_nvim_sync()

    # ------------------------------------------------------------------
    # pynvim sync (Phase 5)
    # ------------------------------------------------------------------

    def _start_nvim_sync(self) -> None:
        try:
            from void_ghostty._nvim_sync import NvimSync
            nvim_exe = _nvim_cmd()[0]
            self._nvim_sync = NvimSync(nvim_exe=nvim_exe, node_path=self._node_path)
            self._nvim_sync.start()
        except Exception:
            pass  # Non-fatal — Neovim still runs as a terminal

    # ------------------------------------------------------------------
    # Node integration (Phases 5–6)
    # ------------------------------------------------------------------

    def on_node_registered(self, node, config: dict) -> None:
        """Called by void_ghostty.register(node)."""
        # In follow mode we may immediately want to switch context
        if self._mode == "follow":
            self._apply_node(node.path())

    def set_node(self, node_path: Optional[str]) -> None:
        """Switch Neovim pane context — called by follow/pinned/drag-drop."""
        if node_path is None:
            self._mode = "free"
            self._node_path = None
        else:
            self._mode = "pinned"
            self._node_path = node_path
        self._apply_node(node_path)

    def _apply_node(self, node_path: Optional[str]) -> None:
        """Internal: update sync thread and open node in Network Editor."""
        self._node_path = node_path

        if self._nvim_sync is not None:
            # Resolve parm path — look for 'code' or 'snippet' parm first
            parm_path = self._resolve_code_parm(node_path)
            self._nvim_sync.set_node(parm_path)

        if node_path:
            self._open_in_network_editor(node_path)

    @staticmethod
    def _resolve_code_parm(node_path: Optional[str]) -> Optional[str]:
        """Return the full parm path for the node's primary code parameter."""
        if not node_path:
            return None
        try:
            import hou
            from void_ghostty import _registry
            node = hou.node(node_path)
            if node is None:
                return None

            # Check _vg_config for explicit watch_parms list
            config = _registry.get(node.sessionId(), {})
            watch = config.get("watch_parms", [])
            if watch:
                parm = node.parm(watch[0])
                if parm:
                    return parm.path()

            # Fallback: common code parm names
            for name in ("code", "snippet", "script", "python", "vex"):
                parm = node.parm(name)
                if parm:
                    return parm.path()
        except Exception:
            pass
        return None

    @staticmethod
    def _open_in_network_editor(node_path: str) -> None:
        try:
            import hou
            node = hou.node(node_path)
            if node is None:
                return
            editor = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor)
            if editor:
                editor.setCurrentNode(node)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Follow mode (Phase 6)
    # ------------------------------------------------------------------

    def enable_follow_mode(self) -> None:
        """Track Houdini node selection; switch context when a registered
        node is selected in the Network Editor."""
        self._mode = "follow"
        if not self._follow_callback_installed:
            try:
                import hou
                hou.ui.addEventLoopCallback(self._on_selection_change)
                self._follow_callback_installed = True
            except Exception:
                pass

    def disable_follow_mode(self) -> None:
        self._mode = "free"
        self._remove_follow_callback()

    def _on_selection_change(self) -> None:
        """Event loop callback — checks Houdini node selection."""
        if self._mode != "follow":
            return
        try:
            import hou
            from void_ghostty import _registry
            selected = hou.selectedNodes()
            for node in selected:
                if node.sessionId() in _registry:
                    self._apply_node(node.path())
                    return
        except Exception:
            pass

    def _remove_follow_callback(self) -> None:
        if self._follow_callback_installed:
            try:
                import hou
                hou.ui.removeEventLoopCallback(self._on_selection_change)
            except Exception:
                pass
            self._follow_callback_installed = False

    # ------------------------------------------------------------------
    # Drag and drop — node path from Network Editor (Phase 6)
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        text = event.mimeData().text().strip()
        if not text:
            event.ignore()
            return
        # If the dropped text looks like a Houdini node path, set pinned mode
        if text.startswith("/"):
            self.set_node(text)
        else:
            event.ignore()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._remove_follow_callback()
        if self._nvim_sync is not None:
            self._nvim_sync.stop()
        for pane in (self._nvim_pane, self._claude_pane, self._shell_pane):
            if pane is not None:
                pane.stop()
        super().closeEvent(event)


def onCreateInterface() -> VoidGhosttyPanel:
    """Entry point called by Houdini when the pane tab is created."""
    return VoidGhosttyPanel()
