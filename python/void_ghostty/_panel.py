"""Void Ghostty — panel construction.

Default layout: single in-process Python REPL pane (HouPythonWidget).

Keyboard shortcuts
------------------
  Ctrl+Shift+H  — split current pane right (adds new Python REPL)
  Ctrl+Shift+B  — split current pane below (adds new Python REPL)
  Ctrl+Shift+X  — close current pane (never closes the last pane)
  Ctrl+Shift+T  — replace current pane with shell (bash / cmd.exe)
  Ctrl+Shift+P  — replace current pane with Python REPL

Pane types
----------
  HouPythonWidget  — in-process Python REPL, full hou.* access, default pane
  TerminalWidget   — libghostty-vt PTY pane: shell subprocess
"""

from __future__ import annotations

import os
import weakref
from typing import Optional

try:
    from PySide2.QtWidgets import (
        QWidget, QVBoxLayout, QSplitter, QSizePolicy,
        QApplication,
    )
    from PySide2.QtGui import QKeySequence, QShortcut
    from PySide2.QtCore import Qt, QTimer
except ImportError:
    from PySide6.QtWidgets import (
        QWidget, QVBoxLayout, QSplitter, QSizePolicy,
        QApplication,
    )
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtCore import Qt, QTimer

from void_ghostty._terminal import TerminalWidget
from void_ghostty._hou_python import HouPythonWidget
from void_ghostty._config import load_config as _load_config

# Config loaded once at panel startup — drives keybindings
_CFG = _load_config()

# Map Ghostty keybind action names → Qt key sequence strings.
# User can override via keybind = ctrl+shift+h=split_right in Ghostty config.
_DEFAULT_SHORTCUTS = {
    "split_right":    "Ctrl+Shift+H",
    "split_down":     "Ctrl+Shift+B",
    "close_surface":  "Ctrl+Shift+X",
    "new_shell":      "Ctrl+Shift+T",   # VoidGhostty-specific action
    "new_python":     "Ctrl+Shift+P",   # VoidGhostty-specific action
}

def _shortcut(action: str) -> str:
    """Return Qt key sequence string for a panel action, respecting config overrides."""
    # Ghostty config uses lowercase "ctrl+shift+h"; Qt needs "Ctrl+Shift+H"
    raw = _CFG.keybinds.get(action, "")
    if raw:
        return "+".join(p.capitalize() for p in raw.split("+"))
    return _DEFAULT_SHORTCUTS.get(action, "")

# Weak reference to the active panel so open_shell() and external callers can reach it.
_panel_ref: Optional[weakref.ref] = None


def get_panel() -> Optional["VoidGhosttyPanel"]:
    if _panel_ref is not None:
        return _panel_ref()
    return None


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------

def _shell_cmd() -> list[str]:
    if os.name == "nt":
        return ["cmd.exe"]
    return [os.environ.get("SHELL", "/bin/bash")]


def _houdini_env() -> dict:
    """Build environment for PTY processes with Houdini context."""
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["VG_HOUDINI"] = "1"
    try:
        import hou
        env["HOUDINI_VERSION"] = hou.applicationVersionString()
        hip = hou.hipFile.path()
        if hip and hip != "untitled.hip":
            env["HIP"] = os.path.dirname(hip)
        env["HOUDINI_TEMP_DIR"] = hou.getenv("HOUDINI_TEMP_DIR", "")
    except Exception:
        pass
    return env


def _houdini_cwd() -> str:
    """Return the best working directory for spawned terminals."""
    try:
        import hou
        hip_path = hou.hipFile.path()
        if hip_path and hip_path != "untitled.hip":
            hip_dir = os.path.dirname(hip_path)
            if os.path.isdir(hip_dir):
                return hip_dir
    except Exception:
        pass
    for var in ("HIP", "JOB", "GHOSTTY"):
        val = os.environ.get(var, "")
        if val and os.path.isdir(val):
            return val
    return os.path.expanduser("~")


# ---------------------------------------------------------------------------
# Panel widget
# ---------------------------------------------------------------------------

class VoidGhosttyPanel(QWidget):
    """Top-level widget returned to Houdini's pane tab system."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        global _panel_ref
        _panel_ref = weakref.ref(self)

        self._panes: list[QWidget] = []
        self._current_pane: Optional[QWidget] = None
        self._cwd: str = ""
        self._env: dict = {}

        self._build_ui()

        # Register dispatch_to_main callback for background server threads.
        try:
            import void_ghostty
            void_ghostty._ensure_dispatch()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._cwd = _houdini_cwd()
        self._env = _houdini_env()

        repl = HouPythonWidget(parent=self)
        layout.addWidget(repl)

        self._panes.append(repl)
        self._current_pane = repl
        self._install_shortcuts()
        self._configure_pane(repl)

        QApplication.instance().focusChanged.connect(self._on_focus_changed)

        QTimer.singleShot(0, repl.start)

    # ------------------------------------------------------------------
    # Focus tracking
    # ------------------------------------------------------------------

    def _on_focus_changed(self, old_widget, new_widget) -> None:
        if new_widget is None:
            return
        # Walk up the widget tree — focus lands on a child (e.g. _ReplEdit inside
        # HouPythonWidget) so we can't do a direct `in self._panes` check.
        w = new_widget
        while w is not None:
            if w in self._panes:
                self._current_pane = w
                return
            try:
                w = w.parent()
            except RuntimeError:
                return

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def _install_shortcuts(self) -> None:
        # Fallback QShortcuts — active when a non-terminal widget has focus.
        # Primary path is callbacks on each pane (see _configure_pane).
        # Key sequences come from Ghostty config (with built-in defaults).
        def _bind(action: str, slot) -> None:
            seq = _shortcut(action)
            if seq:
                QShortcut(QKeySequence(seq), self).activated.connect(slot)

        _bind("split_right",   lambda: self._split_pane(Qt.Horizontal))
        _bind("split_down",    lambda: self._split_pane(Qt.Vertical))
        _bind("close_surface", self._close_pane)
        _bind("new_shell",     self._replace_shell_pane)
        _bind("new_python",    self._replace_python_pane)

    def _configure_pane(self, pane: QWidget) -> None:
        """Wire multiplexer callbacks so keys fire even while the pane has focus."""
        pane.mux_split_h       = lambda: self._split_pane(Qt.Horizontal)
        pane.mux_split_v       = lambda: self._split_pane(Qt.Vertical)
        pane.mux_close         = self._close_pane
        pane.mux_replace_shell  = self._replace_shell_pane
        pane.mux_replace_python = self._replace_python_pane

    # ------------------------------------------------------------------
    # Split pane management
    # ------------------------------------------------------------------

    _MAX_PANES = 8  # matches Ghostty's default surface limit

    @staticmethod
    def _qt_valid(widget) -> bool:
        """Return False if the underlying C++ Qt object has already been deleted."""
        try:
            widget.objectName()
            return True
        except RuntimeError:
            return False

    def _purge_stale_panes(self) -> None:
        """Remove any pane references whose C++ objects Qt has already freed."""
        self._panes = [p for p in self._panes if self._qt_valid(p)]
        if self._current_pane and not self._qt_valid(self._current_pane):
            self._current_pane = next((p for p in self._panes), None)

    def _insert_pane(self, new_pane: QWidget, orientation: Qt.Orientation) -> None:
        """Core splitter insertion logic shared by all split methods."""
        self._purge_stale_panes()
        current = self._current_pane
        if current is None or len(self._panes) >= self._MAX_PANES:
            return

        splitter = QSplitter(orientation)
        splitter.setChildrenCollapsible(False)

        parent = current.parentWidget()
        if isinstance(parent, QSplitter):
            idx = parent.indexOf(current)
            old_sizes = parent.sizes()

            parent.setUpdatesEnabled(False)
            splitter.addWidget(current)
            splitter.addWidget(new_pane)
            parent.insertWidget(idx, splitter)
            parent.setSizes(old_sizes)
            parent.setUpdatesEnabled(True)
        else:
            lyt = self.layout()
            self.setUpdatesEnabled(False)
            lyt.removeWidget(current)
            splitter.addWidget(current)
            splitter.addWidget(new_pane)
            lyt.addWidget(splitter)
            self.setUpdatesEnabled(True)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.show()

        self._panes.append(new_pane)
        self._current_pane = new_pane
        self._configure_pane(new_pane)
        QTimer.singleShot(0, new_pane.start)
        QTimer.singleShot(
            0, lambda p=new_pane: p.setFocus(Qt.OtherFocusReason)
                                  if self._qt_valid(p) else None
        )

    def _split_pane(self, orientation: Qt.Orientation) -> None:
        """Split the current pane with a new Python REPL pane."""
        new_pane = HouPythonWidget()
        self._insert_pane(new_pane, orientation)

    def _split_shell_pane(self) -> None:
        """Open a new shell pane alongside the current pane (used by open_shell() API)."""
        new_pane = TerminalWidget(
            cmd=_shell_cmd(), cwd=self._cwd, env=self._env
        )
        self._insert_pane(new_pane, Qt.Horizontal)

    def _replace_pane(self, new_pane: QWidget) -> None:
        """Replace the current pane in-place (no new split)."""
        self._purge_stale_panes()
        current = self._current_pane
        if current is None:
            return

        parent = current.parentWidget()
        if isinstance(parent, QSplitter):
            idx = parent.indexOf(current)
            sizes = parent.sizes()
            parent.setUpdatesEnabled(False)
            parent.insertWidget(idx, new_pane)
            current.stop()
            current.setParent(None)     # detaches from splitter → count back to original
            current.deleteLater()
            parent.setSizes(sizes)
            parent.setUpdatesEnabled(True)
        else:
            lyt = self.layout()
            self.setUpdatesEnabled(False)
            lyt.removeWidget(current)
            current.stop()
            current.deleteLater()
            lyt.addWidget(new_pane)
            self.setUpdatesEnabled(True)

        self._panes = [new_pane if p is current else p for p in self._panes]
        self._current_pane = new_pane
        self._configure_pane(new_pane)
        QTimer.singleShot(0, new_pane.start)
        QTimer.singleShot(
            0, lambda p=new_pane: p.setFocus(Qt.OtherFocusReason)
                                  if self._qt_valid(p) else None
        )

    def _replace_shell_pane(self) -> None:
        """Replace the current pane with a shell terminal (Ctrl+Shift+T)."""
        new_pane = TerminalWidget(
            cmd=_shell_cmd(), cwd=self._cwd, env=self._env
        )
        self._replace_pane(new_pane)

    def _replace_python_pane(self) -> None:
        """Replace the current pane with a Python REPL (Ctrl+Shift+P)."""
        self._replace_pane(HouPythonWidget())

    def _close_pane(self) -> None:
        """Close the current pane. Never closes the last pane."""
        self._purge_stale_panes()
        current = self._current_pane
        if current is None or len(self._panes) <= 1:
            return

        parent = current.parentWidget()
        if not isinstance(parent, QSplitter) or parent.count() != 2:
            return

        sibling_idx = 1 - parent.indexOf(current)
        sibling = parent.widget(sibling_idx)

        grandparent = parent.parentWidget()
        if isinstance(grandparent, QSplitter):
            gp_idx = grandparent.indexOf(parent)
            gp_sizes = grandparent.sizes()
            grandparent.setUpdatesEnabled(False)
            grandparent.insertWidget(gp_idx, sibling)
            grandparent.setSizes(gp_sizes)
            grandparent.setUpdatesEnabled(True)
        else:
            lyt = grandparent.layout()
            self.setUpdatesEnabled(False)
            lyt.removeWidget(parent)
            lyt.addWidget(sibling)
            self.setUpdatesEnabled(True)

        current.stop()
        current.deleteLater()
        parent.deleteLater()
        self._panes.remove(current)

        # Focus sibling if it's a pane, otherwise fall back to first valid pane.
        next_pane = (
            sibling if sibling in self._panes
            else next((p for p in self._panes if self._qt_valid(p)), None)
        )
        if next_pane:
            self._current_pane = next_pane
            next_pane.setFocus(Qt.OtherFocusReason)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        for pane in list(self._panes):
            if self._qt_valid(pane):
                pane.stop()
        super().closeEvent(event)


def onCreateInterface() -> VoidGhosttyPanel:
    """Entry point called by Houdini when the pane tab is created."""
    return VoidGhosttyPanel()
