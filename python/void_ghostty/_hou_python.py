"""Void Ghostty — in-process Houdini Python REPL pane.

Runs code.InteractiveConsole directly inside Houdini's Python interpreter.
I/O is routed through thread-safe queues; no PTY or subprocess needed.

Pre-imported globals: hou, void_ghostty, open_shell
"""

from __future__ import annotations

import code
import ctypes
import io
import keyword
import os
import queue
import re
import rlcompleter
import sys
import threading
import tokenize
from typing import Optional

try:
    from PySide2.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QSizePolicy
    from PySide2.QtGui import (
        QColor, QFont, QTextCharFormat, QTextCursor, QPalette,
    )
    from PySide2.QtCore import Qt, QTimer, QSize, Signal
except ImportError:
    from PySide6.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QSizePolicy
    from PySide6.QtGui import (
        QColor, QFont, QTextCharFormat, QTextCursor, QPalette,
    )
    from PySide6.QtCore import Qt, QTimer, QSize, Signal

# ---------------------------------------------------------------------------
# Config + theme (loaded once at import time from Ghostty config file)
# ---------------------------------------------------------------------------

try:
    from void_ghostty._config import load_config as _load_config
    from void_ghostty._themes import load_theme as _load_theme
    _CFG   = _load_config()
    _THEME = _load_theme(_CFG.theme)
except Exception:
    _CFG   = None
    _THEME = {
        "bg": "#282828", "fg": "#ebdbb2", "fg_dim": "#a89984",
        "cursor": "#fabd2f", "selection": "#504945",
        "black": "#282828", "red": "#fb4934", "green": "#b8bb26",
        "yellow": "#fabd2f", "blue": "#83a598", "magenta": "#d3869b",
        "cyan": "#8ec07c", "white": "#ebdbb2",
        "brightblack": "#928374", "brightred": "#fb4934",
        "brightgreen": "#b8bb26", "brightyellow": "#fabd2f",
        "brightblue": "#83a598", "brightmagenta": "#d3869b",
        "brightcyan": "#8ec07c", "brightwhite": "#fbf1c7",
    }

# ---------------------------------------------------------------------------
# Theme colour aliases — used throughout this file
# ---------------------------------------------------------------------------
_C_BG      = _THEME["bg"]
_C_FG      = _THEME["fg"]
_C_DIM     = _THEME["fg_dim"]
_C_RED     = _THEME["red"]
_C_GREEN   = _THEME["green"]
_C_YELLOW  = _THEME["yellow"]
_C_BLUE    = _THEME["blue"]
_C_MAGENTA = _THEME["magenta"]
_C_CYAN    = _THEME["cyan"]
_C_BBLACK  = _THEME.get("brightblack", "#928374")

_ANSI_FG = {
    30: _THEME.get("black",  _C_BG),
    31: _THEME.get("red",    _C_RED),
    32: _THEME.get("green",  _C_GREEN),
    33: _THEME.get("yellow", _C_YELLOW),
    34: _THEME.get("blue",   _C_BLUE),
    35: _THEME.get("magenta",_C_MAGENTA),
    36: _THEME.get("cyan",   _C_CYAN),
    37: _C_FG,
    90: _THEME.get("brightblack",   _C_BBLACK),
    91: _THEME.get("brightred",     _C_RED),
    92: _THEME.get("brightgreen",   _C_GREEN),
    93: _THEME.get("brightyellow",  _C_YELLOW),
    94: _THEME.get("brightblue",    _C_BLUE),
    95: _THEME.get("brightmagenta", _C_MAGENTA),
    96: _THEME.get("brightcyan",    _C_CYAN),
    97: _THEME.get("brightwhite",   _C_FG),
}

# ---------------------------------------------------------------------------
# ANSI escape parser (covers Python tracebacks, IPython, rich)
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(r'\x1b\[([0-9;]*)m')


def _ansi_segments(text: str):
    """Yield (chunk, QTextCharFormat) pairs, parsing SGR escape codes."""
    bold = False
    italic = False
    underline = False
    fg: Optional[str] = None   # hex string or None

    def _make_fmt():
        f = QTextCharFormat()
        if fg:
            f.setForeground(QColor(fg))
        if bold:
            f.setFontWeight(700)
        if italic:
            f.setFontItalic(True)
        if underline:
            f.setFontUnderline(True)
        return f

    pos = 0
    for m in _ANSI_RE.finditer(text):
        if m.start() > pos:
            yield text[pos:m.start()], _make_fmt()
        raw = m.group(1)
        codes = [int(c) for c in raw.split(';') if c] if raw else [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                bold = italic = underline = False; fg = None
            elif c == 1:
                bold = True
            elif c == 3:
                italic = True
            elif c == 4:
                underline = True
            elif c in _ANSI_FG:
                fg = _ANSI_FG[c]
            elif c == 38 and i + 1 < len(codes):
                if codes[i + 1] == 5 and i + 2 < len(codes):
                    i += 2  # 256-colour — skip, keep current
                elif codes[i + 1] == 2 and i + 4 < len(codes):
                    r, g, b = codes[i + 2], codes[i + 3], codes[i + 4]
                    fg = f"#{r:02x}{g:02x}{b:02x}"
                    i += 4
            i += 1
        pos = m.end()
    if pos < len(text):
        yield text[pos:], _make_fmt()


# ---------------------------------------------------------------------------
# _HouConsole — InteractiveConsole that routes write() to a queue
# ---------------------------------------------------------------------------

class _HouConsole(code.InteractiveConsole):
    """Routes InteractiveConsole.write() (SyntaxError etc.) to the output queue."""

    def __init__(self, out_q: queue.Queue, **kwargs) -> None:
        super().__init__(**kwargs)
        self._out_q = out_q

    def write(self, data: str) -> None:
        if data:
            self._out_q.put(('err', data))


# ---------------------------------------------------------------------------
# _ReplEdit — QTextEdit with enforced prompt boundary
# ---------------------------------------------------------------------------

class _ReplEdit(QTextEdit):
    line_entered         = Signal(str)
    interrupt_requested  = Signal()
    split_h_requested    = Signal()
    split_v_requested    = Signal()
    close_pane_requested = Signal()
    shell_pane_requested  = Signal()
    python_pane_requested = Signal()
    tab_requested         = Signal(str)   # emitted with current input; HouPythonWidget handles

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._prompt_end = 0      # char offset: user input starts here
        self._history: list[str] = []
        self._hist_idx = -1
        self._last_prompt = ">>> "

        # Ghost text (fish-style inline autosuggestion)
        self._ghost_text  = ""   # suffix currently shown after cursor
        self._ghost_start = -1   # document position where ghost text begins

        # Syntax highlighting guard
        self._highlighting = False

        # Tab completer (set by HouPythonWidget.set_completer)
        self._completer: Optional[rlcompleter.Completer] = None

        self.setReadOnly(False)
        self.setUndoRedoEnabled(False)
        self.setAcceptRichText(False)

        _font_family = (_CFG.font_family if _CFG is not None
                        else ("Consolas" if os.name == "nt" else "Monospace"))
        _font_size   = _CFG.font_size if _CFG is not None else 10
        font = QFont(_font_family)
        font.setPointSize(_font_size)
        font.setFixedPitch(True)
        self.setFont(font)

        pal = self.palette()
        pal.setColor(QPalette.Base, QColor(_C_BG))
        pal.setColor(QPalette.Text, QColor(_C_FG))
        self.setPalette(pal)
        self.setStyleSheet(
            f"QTextEdit {{ background-color: {_C_BG}; color: {_C_FG}; border: none; }}"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def insert_output(self, text: str, fmt: QTextCharFormat) -> None:
        """Append output text to the end and advance _prompt_end."""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text, fmt)
        self._prompt_end = cursor.position()
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def show_prompt(self, prompt: str) -> None:
        """Append prompt string and mark _prompt_end."""
        self._last_prompt = prompt
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(_C_DIM))
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(prompt, fmt)
        self._prompt_end = cursor.position()
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _current_input(self) -> str:
        """Return the text the user has typed (excludes ghost text suffix)."""
        text = self.toPlainText()
        if self._ghost_start >= self._prompt_end:
            return text[self._prompt_end:self._ghost_start]
        return text[self._prompt_end:]

    def _set_input(self, text: str) -> None:
        self._clear_ghost()
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(_C_FG))
        cursor = self.textCursor()
        cursor.setPosition(self._prompt_end)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.insertText(text, fmt)
        self.setTextCursor(cursor)
        self._highlight_input()
        self._update_ghost()

    # ------------------------------------------------------------------
    # Completer API (called by HouPythonWidget)
    # ------------------------------------------------------------------

    def set_completer(self, completer: rlcompleter.Completer) -> None:
        self._completer = completer

    # ------------------------------------------------------------------
    # Tab completion
    # ------------------------------------------------------------------

    def _insert_completion(self, suffix: str) -> None:
        """Insert a completion suffix at cursor and refresh ghost/highlight."""
        self._clear_ghost()
        cursor = self.textCursor()
        if cursor.position() < self._prompt_end:
            cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(_C_FG))
        cursor.insertText(suffix, fmt)
        self.setTextCursor(cursor)
        self._highlight_input()
        self._update_ghost()

    def _show_completions(self, completions: list[str]) -> None:
        """Display a completion list and re-show the prompt + current input."""
        saved = self._current_input()
        self._clear_ghost()

        # Erase everything from _prompt_end to end
        cursor = self.textCursor()
        cursor.setPosition(self._prompt_end)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)

        # Build a common-prefix insertion if possible
        common = os.path.commonprefix(completions)
        current_prefix_match = re.search(r'[\w\.]+$', saved)
        current_prefix = current_prefix_match.group() if current_prefix_match else ""
        extra = common[len(current_prefix):] if len(common) > len(current_prefix) else ""

        # Render completion list
        cols = min(4, len(completions))
        col_w = max((len(c) for c in completions), default=0) + 2
        lines = []
        for i in range(0, len(completions), cols):
            line = "  ".join(c.ljust(col_w) for c in completions[i:i + cols])
            lines.append(line.rstrip())
        comp_block = "\n".join(lines) + "\n"

        comp_fmt = QTextCharFormat()
        comp_fmt.setForeground(QColor(_C_DIM))
        cursor.insertText(comp_block, comp_fmt)

        # Re-show prompt
        prompt_fmt = QTextCharFormat()
        prompt_fmt.setForeground(QColor(_C_DIM))
        cursor.insertText(self._last_prompt, prompt_fmt)
        self._prompt_end = cursor.position()

        # Re-show saved input + any common prefix extension
        new_input = saved + extra
        if new_input:
            input_fmt = QTextCharFormat()
            input_fmt.setForeground(QColor(_C_FG))
            cursor.insertText(new_input, input_fmt)

        self.setTextCursor(cursor)
        self.ensureCursorVisible()
        self._highlight_input()
        self._update_ghost()

    # ------------------------------------------------------------------
    # Syntax highlighting (tokenize-based, input region only)
    # ------------------------------------------------------------------

    def _highlight_input(self) -> None:
        """Apply Python syntax highlighting to the current input region."""
        if self._highlighting:
            return
        self._highlighting = True
        try:
            end_pos = self._ghost_start if self._ghost_start >= self._prompt_end else -1
            full_text = self.toPlainText()
            inp = (full_text[self._prompt_end:end_pos]
                   if end_pos >= 0 else full_text[self._prompt_end:])
            if not inp:
                return

            doc = self.document()
            cursor = QTextCursor(doc)

            # Reset entire input region to default fg
            cursor.setPosition(self._prompt_end)
            region_end = end_pos if end_pos >= 0 else len(full_text)
            cursor.setPosition(region_end, QTextCursor.KeepAnchor)
            default_fmt = QTextCharFormat()
            default_fmt.setForeground(QColor(_C_FG))
            cursor.setCharFormat(default_fmt)

            # Tokenize and apply colors
            try:
                tokens = list(tokenize.generate_tokens(io.StringIO(inp).readline))
            except tokenize.TokenError:
                return

            for tok_type, tok_string, tok_start, tok_end, _ in tokens:
                if tok_type == tokenize.ENDMARKER:
                    continue
                if tok_type == tokenize.NAME and keyword.iskeyword(tok_string):
                    color = _C_YELLOW
                elif tok_type == tokenize.STRING:
                    color = _C_GREEN
                elif tok_type == tokenize.COMMENT:
                    color = _C_DIM
                elif tok_type == tokenize.NUMBER:
                    color = _C_MAGENTA
                else:
                    continue

                # tok_start/tok_end are (line, col); single-line input → line=1
                abs_start = self._prompt_end + tok_start[1]
                abs_end   = self._prompt_end + tok_end[1]
                if abs_start < abs_end <= region_end:
                    cursor.setPosition(abs_start)
                    cursor.setPosition(abs_end, QTextCursor.KeepAnchor)
                    fmt = QTextCharFormat()
                    fmt.setForeground(QColor(color))
                    cursor.setCharFormat(fmt)
        finally:
            self._highlighting = False

    # ------------------------------------------------------------------
    # Ghost text — fish-style inline autosuggestion
    # ------------------------------------------------------------------

    def _clear_ghost(self) -> None:
        """Remove ghost text from the document and reset tracking state."""
        if self._ghost_start < 0 or not self._ghost_text:
            return
        cursor = self.textCursor()
        cursor.setPosition(self._ghost_start)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        self._ghost_text  = ""
        self._ghost_start = -1
        # Leave cursor at where ghost started (end of real input)
        cursor.setPosition(min(cursor.position(),
                               len(self.toPlainText())))
        self.setTextCursor(cursor)

    def _set_ghost(self, suffix: str) -> None:
        """Insert ghost text after the cursor in dim color."""
        self._clear_ghost()
        if not suffix:
            return
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._ghost_start = cursor.position()
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(_C_DIM))
        cursor.insertText(suffix, fmt)
        # Move editing cursor back to before ghost text
        cursor.setPosition(self._ghost_start)
        self.setTextCursor(cursor)
        self._ghost_text = suffix

    def _update_ghost(self) -> None:
        """Recompute and refresh ghost text suggestion."""
        text = self._current_input()
        if not text or not text.strip():
            self._clear_ghost()
            return

        suggestion: Optional[str] = None

        # 1. History: most recent entry that starts with current input
        for entry in reversed(self._history):
            if entry.startswith(text) and entry != text:
                suggestion = entry[len(text):]
                break

        # 2. rlcompleter: first attribute match as fallback
        if suggestion is None and self._completer is not None:
            word_match = re.search(r'[\w\.]+$', text)
            if word_match:
                prefix = word_match.group()
                try:
                    first = self._completer.complete(prefix, 0)
                    if (first is not None
                            and first.startswith(prefix)
                            and first != prefix):
                        suggestion = first[len(prefix):]
                except Exception:
                    pass

        if suggestion:
            self._set_ghost(suggestion)
        else:
            self._clear_ghost()

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def _history_up(self) -> None:
        if not self._history:
            return
        if self._hist_idx == -1:
            self._hist_idx = len(self._history) - 1
        elif self._hist_idx > 0:
            self._hist_idx -= 1
        self._set_input(self._history[self._hist_idx])

    def _history_down(self) -> None:
        if self._hist_idx == -1:
            return
        self._hist_idx += 1
        if self._hist_idx >= len(self._history):
            self._hist_idx = -1
            self._set_input('')
        else:
            self._set_input(self._history[self._hist_idx])

    # ------------------------------------------------------------------
    # Key events
    # ------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        key  = event.key()
        mods = event.modifiers()
        ctrl  = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)

        # Multiplexer shortcuts — highest priority
        if ctrl and shift:
            if key == Qt.Key_H:
                self.split_h_requested.emit(); return
            if key == Qt.Key_B:
                self.split_v_requested.emit(); return
            if key == Qt.Key_X:
                self.close_pane_requested.emit(); return
            if key == Qt.Key_T:
                self.shell_pane_requested.emit(); return
            if key == Qt.Key_P:
                self.python_pane_requested.emit(); return
            if key == Qt.Key_C:
                self.copy(); return

        if ctrl and key == Qt.Key_C:
            self.interrupt_requested.emit(); return

        if ctrl and key == Qt.Key_L:
            self._clear_ghost()
            self.clear()
            self._prompt_end = 0
            return

        # Tab — emit tab_requested for HouPythonWidget to handle completion
        if key == Qt.Key_Tab and not ctrl and not shift:
            self._clear_ghost()
            self.tab_requested.emit(self._current_input())
            return

        # Right / End — accept ghost text if cursor is at ghost boundary
        if key in (Qt.Key_Right, Qt.Key_End) and self._ghost_start >= 0:
            cursor = self.textCursor()
            if cursor.position() == self._ghost_start:
                suffix = self._ghost_text
                self._clear_ghost()
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(_C_FG))
                c2 = self.textCursor()
                c2.insertText(suffix, fmt)
                self.setTextCursor(c2)
                self._highlight_input()
                self._update_ghost()
                return

        # Any printable key clears ghost first
        if (self._ghost_start >= 0
                and not ctrl
                and key not in (Qt.Key_Left, Qt.Key_Right, Qt.Key_End,
                                Qt.Key_Up, Qt.Key_Down, Qt.Key_Home,
                                Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt,
                                Qt.Key_Meta)):
            self._clear_ghost()

        # Clamp cursor to after prompt
        cursor = self.textCursor()
        if cursor.position() < self._prompt_end and not ctrl:
            cursor.movePosition(QTextCursor.End)
            self.setTextCursor(cursor)
            cursor = self.textCursor()

        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._clear_ghost()
            line = self._current_input()
            cursor.movePosition(QTextCursor.End)
            cursor.insertText('\n', QTextCharFormat())
            self._prompt_end = cursor.position()
            self.setTextCursor(cursor)
            if line.strip():
                self._history.append(line)
                self._hist_idx = -1
            self.line_entered.emit(line)
            return

        if key == Qt.Key_Up:
            self._history_up(); return
        if key == Qt.Key_Down:
            self._history_down(); return

        if key == Qt.Key_Home:
            cursor.setPosition(self._prompt_end)
            self.setTextCursor(cursor)
            return

        if key == Qt.Key_Backspace:
            if cursor.position() <= self._prompt_end:
                return
            if cursor.hasSelection() and cursor.selectionStart() < self._prompt_end:
                return

        super().keyPressEvent(event)

        # After input change: refresh highlighting and ghost suggestion
        if not ctrl and key not in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up,
                                     Qt.Key_Down, Qt.Key_Home, Qt.Key_End,
                                     Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt,
                                     Qt.Key_Meta, Qt.Key_CapsLock):
            self._highlight_input()
            self._update_ghost()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        cursor = self.textCursor()
        if cursor.position() < self._prompt_end:
            cursor.movePosition(QTextCursor.End)
            self.setTextCursor(cursor)

    # ------------------------------------------------------------------
    # Drag-and-drop — Houdini node drops from the network editor
    # ------------------------------------------------------------------
    # Houdini places node paths in MIME type "application/sidefx-houdini-node.path".
    # Multiple nodes are tab-separated.  We convert each path to a hou.node() call
    # so the result is immediately usable as a Python expression.

    _NODE_MIME = "application/sidefx-houdini-node.path"

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(self._NODE_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(self._NODE_MIME):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        if mime.hasFormat(self._NODE_MIME):
            raw = bytes(mime.data(self._NODE_MIME)).decode("utf-8", errors="replace")
            paths = [p.strip() for p in raw.split("\t") if p.strip()]
            if len(paths) == 1:
                insert = f'hou.node("{paths[0]}")'
            else:
                joined = ", ".join(f'hou.node("{p}")' for p in paths)
                insert = f"[{joined}]"
            # Ensure cursor is in the editable region
            cursor = self.textCursor()
            if cursor.position() < self._prompt_end:
                cursor.movePosition(QTextCursor.End)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(_C_FG))
            cursor.insertText(insert, fmt)
            self.setTextCursor(cursor)
            self.ensureCursorVisible()
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


# ---------------------------------------------------------------------------
# HouPythonWidget
# ---------------------------------------------------------------------------

class HouPythonWidget(QWidget):
    """In-process Houdini Python REPL pane.

    Runs code.InteractiveConsole on a daemon thread.  I/O is routed through
    queues — no PTY, no subprocess, no VT emulation needed.  The widget
    shares Houdini's live Python interpreter: hou, hou.hipFile, hou.node(),
    etc. all operate on the active scene.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._in_q:  queue.Queue = queue.Queue()
        self._out_q: queue.Queue = queue.Queue()
        self._stop_evt = threading.Event()
        self._repl_thread_id: Optional[int] = None

        # Shared locals dict: REPL thread writes, main thread reads for completion.
        # GIL protects individual dict operations — safe without extra locks.
        self._console_locals: dict = {"__name__": "__console__", "__doc__": None}

        # Mux callbacks (set by VoidGhosttyPanel._configure_pane)
        self.mux_split_h = None
        self.mux_split_v = None
        self.mux_close   = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._edit = _ReplEdit(self)
        layout.addWidget(self._edit)

        self._edit.line_entered.connect(self._submit_line)
        self._edit.interrupt_requested.connect(self._interrupt_repl)
        self._edit.tab_requested.connect(self._on_tab_complete)
        self._edit.split_h_requested.connect(
            lambda: self.mux_split_h() if self.mux_split_h else None
        )
        self._edit.split_v_requested.connect(
            lambda: self.mux_split_v() if self.mux_split_v else None
        )
        self._edit.close_pane_requested.connect(
            lambda: self.mux_close() if self.mux_close else None
        )
        self._edit.shell_pane_requested.connect(self._open_shell_pane)
        self._edit.python_pane_requested.connect(self._open_python_pane)

        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(16)   # ~60 fps drain
        self._drain_timer.timeout.connect(self._drain_output)
        self._drain_timer.start()

        self._thread = threading.Thread(
            target=self._repl_loop, daemon=True, name="vg-houpython"
        )
        self._thread.start()

        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------------
    # Lifecycle (matches TerminalWidget interface)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """No-op — REPL starts in __init__. Provided for interface parity."""
        pass

    def stop(self) -> None:
        self._stop_evt.set()
        self._in_q.put(None)
        self._drain_timer.stop()

    # ------------------------------------------------------------------
    # Focus
    # ------------------------------------------------------------------

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self._edit.setFocus(Qt.OtherFocusReason)

    # ------------------------------------------------------------------
    # REPL thread
    # ------------------------------------------------------------------

    def _repl_loop(self) -> None:
        self._repl_thread_id = threading.current_thread().ident

        # Populate the shared locals dict (initialised in __init__)
        glob = self._console_locals
        glob['__name__'] = '__console__'
        glob['__doc__']  = None

        try:
            import hou
            glob['hou'] = hou
        except ImportError:
            pass

        try:
            import void_ghostty as _vg
            glob['void_ghostty'] = _vg
            glob['open_shell']   = lambda: _vg.open_shell()
            glob['list_themes']  = _vg.list_themes
            glob['vg_info']      = _vg.vg_info
        except ImportError:
            pass

        console = _HouConsole(out_q=self._out_q, locals=glob)

        # Wire up rlcompleter on the main thread once locals are ready
        self._out_q.put(('set_completer', rlcompleter.Completer(glob)))

        # Banner
        hou_ver = ""
        try:
            hou_ver = f"  Houdini {glob['hou'].applicationVersionString()}\n"
        except Exception:
            pass
        banner = (
            f"Void Ghostty Python {sys.version.split()[0]}\n"
            f"{hou_ver}"
            "  hou  void_ghostty  open_shell  list_themes  vg_info\n"
        )
        self._out_q.put(('banner', banner))

        while not self._stop_evt.is_set():
            prompt = '... ' if console.buffer else '>>> '
            self._out_q.put(('prompt', prompt))

            # Inner loop: wait for input without re-emitting the prompt on timeout.
            line = None
            while not self._stop_evt.is_set():
                try:
                    line = self._in_q.get(timeout=0.05)
                    break
                except queue.Empty:
                    pass

            if self._stop_evt.is_set() or line is None:
                break

            buf_out = io.StringIO()
            buf_err = io.StringIO()
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout = buf_out
            sys.stderr = buf_err
            try:
                console.push(line)
            except SystemExit:
                sys.stdout = old_out
                sys.stderr = old_err
                break
            finally:
                sys.stdout = old_out
                sys.stderr = old_err

            out = buf_out.getvalue()
            err = buf_err.getvalue()
            if out:
                self._out_q.put(('out', out))
            if err:
                self._out_q.put(('err', err))

    # ------------------------------------------------------------------
    # Submit / interrupt
    # ------------------------------------------------------------------

    def _submit_line(self, line: str) -> None:
        self._in_q.put(line)

    def _interrupt_repl(self) -> None:
        tid = self._repl_thread_id
        if tid is None:
            return
        try:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(tid),
                ctypes.py_object(KeyboardInterrupt),
            )
        except Exception:
            pass

    def _on_tab_complete(self, text: str) -> None:
        """Handle Tab key: run rlcompleter and insert or display completions."""
        completer = self._edit._completer
        if completer is None:
            return

        word_match = re.search(r'[\w\.]+$', text)
        if not word_match:
            return
        prefix = word_match.group()

        completions: list[str] = []
        state = 0
        try:
            while True:
                c = completer.complete(prefix, state)
                if c is None:
                    break
                completions.append(c)
                state += 1
                if state > 256:
                    break
        except Exception:
            return

        if not completions:
            return

        if len(completions) == 1:
            suffix = completions[0][len(prefix):]
            if suffix:
                self._edit._insert_completion(suffix)
        else:
            self._edit._show_completions(completions)

    # ------------------------------------------------------------------
    # Output drain (runs on Qt main thread via QTimer)
    # ------------------------------------------------------------------

    def _drain_output(self) -> None:
        for _ in range(64):
            try:
                kind, data = self._out_q.get_nowait()
            except queue.Empty:
                break

            if kind == 'set_completer':
                self._edit.set_completer(data)

            elif kind == 'banner':
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(_C_DIM))
                self._edit.insert_output(data, fmt)

            elif kind == 'prompt':
                self._edit.show_prompt(data)

            elif kind == 'out':
                for chunk, fmt in _ansi_segments(data):
                    self._edit.insert_output(chunk, fmt)

            elif kind == 'err':
                # Strip ANSI, display in red
                clean = _ANSI_RE.sub('', data)
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(_C_RED))
                self._edit.insert_output(clean, fmt)

    def _open_shell_pane(self) -> None:
        try:
            from void_ghostty._panel import get_panel
            panel = get_panel()
            if panel is not None:
                panel._replace_shell_pane()
        except Exception as exc:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(_C_RED))
            self._edit.insert_output(f"open_shell error: {exc}\n", fmt)

    def _open_python_pane(self) -> None:
        try:
            from void_ghostty._panel import get_panel
            panel = get_panel()
            if panel is not None:
                panel._replace_python_pane()
        except Exception as exc:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(_C_RED))
            self._edit.insert_output(f"open_python error: {exc}\n", fmt)

    # ------------------------------------------------------------------
    # Size hint
    # ------------------------------------------------------------------

    def sizeHint(self) -> QSize:
        return QSize(640, 400)
