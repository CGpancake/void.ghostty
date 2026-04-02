"""Void Ghostty — TerminalWidget (Phases 2–3: pyte scaffold).

Architecture:
  - TerminalWidget(QWidget) owns a PtyProcess
  - PTY output is fed into a pyte.Screen for VT interpretation
  - A QTimer drives periodic reads from the PTY master fd
  - paintEvent renders screen.display as fixed-width text via QPainter

The pyte layer is replaced entirely by the libghostty-vt render surface
in Phase 4.  The PTY and process management stay the same.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Optional

try:
    from PySide2.QtWidgets import QWidget, QScrollBar, QHBoxLayout, QSizePolicy
    from PySide2.QtGui import QPainter, QColor, QFont, QFontMetrics, QKeyEvent
    from PySide2.QtCore import Qt, QTimer, QRect, QSize, Signal
except ImportError:
    from PySide6.QtWidgets import QWidget, QScrollBar, QHBoxLayout, QSizePolicy
    from PySide6.QtGui import QPainter, QColor, QFont, QFontMetrics, QKeyEvent
    from PySide6.QtCore import Qt, QTimer, QRect, QSize, Signal

import pyte

# ---------------------------------------------------------------------------
# PTY process abstraction
# ---------------------------------------------------------------------------

if os.name == "nt":
    import winpty as _winpty

    class PtyProcess:
        """Thin wrapper around winpty.PtyProcess (Windows ConPTY)."""

        def __init__(self, cmd: list[str], cols: int = 80, rows: int = 24) -> None:
            self._proc = _winpty.PtyProcess.spawn(cmd, dimensions=(rows, cols))

        def read(self, n: int = 4096) -> bytes:
            try:
                data = self._proc.read(n)
                if isinstance(data, str):
                    return data.encode("utf-8", errors="replace")
                return data
            except Exception:
                return b""

        def write(self, data: bytes) -> None:
            try:
                if isinstance(data, bytes):
                    self._proc.write(data.decode("utf-8", errors="replace"))
                else:
                    self._proc.write(data)
            except Exception:
                pass

        def resize(self, cols: int, rows: int) -> None:
            try:
                self._proc.setwinsize(rows, cols)
            except Exception:
                pass

        def terminate(self) -> None:
            try:
                self._proc.terminate()
            except Exception:
                pass

        @property
        def alive(self) -> bool:
            try:
                return self._proc.isalive()
            except Exception:
                return False

else:
    import pty as _pty
    import subprocess
    import fcntl
    import termios
    import struct
    import select

    class PtyProcess:
        """PTY process using stdlib pty (Linux/macOS)."""

        def __init__(self, cmd: list[str], cols: int = 80, rows: int = 24) -> None:
            self._cols = cols
            self._rows = rows
            self._master_fd, self._slave_fd = _pty.openpty()
            self._set_size(cols, rows)
            self._proc = subprocess.Popen(
                cmd,
                stdin=self._slave_fd,
                stdout=self._slave_fd,
                stderr=self._slave_fd,
                close_fds=True,
                start_new_session=True,
            )
            os.close(self._slave_fd)
            # Non-blocking reads
            flags = fcntl.fcntl(self._master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self._master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        def _set_size(self, cols: int, rows: int) -> None:
            size = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, size)

        def read(self, n: int = 4096) -> bytes:
            try:
                r, _, _ = select.select([self._master_fd], [], [], 0.0)
                if r:
                    return os.read(self._master_fd, n)
            except Exception:
                pass
            return b""

        def write(self, data: bytes) -> None:
            try:
                os.write(self._master_fd, data)
            except Exception:
                pass

        def resize(self, cols: int, rows: int) -> None:
            self._cols = cols
            self._rows = rows
            try:
                self._set_size(cols, rows)
            except Exception:
                pass

        def terminate(self) -> None:
            try:
                self._proc.terminate()
            except Exception:
                pass

        @property
        def alive(self) -> bool:
            return self._proc.poll() is None


# ---------------------------------------------------------------------------
# TerminalWidget
# ---------------------------------------------------------------------------

_DEFAULT_SHELL_WINDOWS = ["cmd.exe"]
_DEFAULT_SHELL_LINUX = [os.environ.get("SHELL", "/bin/bash")]


class TerminalWidget(QWidget):
    """Single-pane terminal: PTY process + pyte screen + QPainter renderer.

    This is the Phase 2–3 implementation.  Phase 4 replaces the pyte
    screen with libghostty-vt cell iteration but keeps everything else.
    """

    title_changed = Signal(str)

    def __init__(
        self,
        cmd: Optional[list[str]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        if cmd is None:
            cmd = _DEFAULT_SHELL_WINDOWS if os.name == "nt" else _DEFAULT_SHELL_LINUX

        self._cmd = cmd
        self._cols = 80
        self._rows = 24
        self._cell_w = 8
        self._cell_h = 16

        # pyte VT state machine
        self._screen = pyte.Screen(self._cols, self._rows)
        self._stream = pyte.ByteStream(self._screen)

        # PTY process (started lazily on first focus / explicit start)
        self._pty: Optional[PtyProcess] = None
        self._pty_lock = threading.Lock()

        # Font
        self._font = QFont("Consolas" if os.name == "nt" else "Monospace")
        self._font.setPointSize(10)
        self._font.setStyleHint(QFont.Monospace)
        self._font.setFixedPitch(True)
        fm = QFontMetrics(self._font)
        self._cell_w = fm.horizontalAdvance("M")
        self._cell_h = fm.height()

        # Read timer — only fires when PTY is active
        self._read_timer = QTimer(self)
        self._read_timer.setInterval(16)  # ~60 fps
        self._read_timer.timeout.connect(self._drain_pty)

        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the PTY process and begin reading."""
        if self._pty is not None:
            return
        cols, rows = self._compute_grid()
        self._resize_screen(cols, rows)
        self._pty = PtyProcess(self._cmd, cols=cols, rows=rows)
        self._read_timer.start()

    def stop(self) -> None:
        """Terminate the PTY process and stop the read timer."""
        self._read_timer.stop()
        if self._pty is not None:
            self._pty.terminate()
            self._pty = None

    def write_to_pty(self, data: bytes) -> None:
        if self._pty is not None:
            self._pty.write(data)

    # ------------------------------------------------------------------
    # PTY drain
    # ------------------------------------------------------------------

    def _drain_pty(self) -> None:
        if self._pty is None:
            return
        data = self._pty.read(4096)
        if data:
            self._stream.feed(data)
            self.update()

    # ------------------------------------------------------------------
    # Layout / resize
    # ------------------------------------------------------------------

    def _compute_grid(self) -> tuple[int, int]:
        w = max(self.width(), 1)
        h = max(self.height(), 1)
        cols = max(w // self._cell_w, 1)
        rows = max(h // self._cell_h, 1)
        return cols, rows

    def _resize_screen(self, cols: int, rows: int) -> None:
        if cols == self._cols and rows == self._rows:
            return
        self._cols = cols
        self._rows = rows
        self._screen.resize(rows, cols)
        if self._pty is not None:
            self._pty.resize(cols, rows)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cols, rows = self._compute_grid()
        self._resize_screen(cols, rows)

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setFont(self._font)

        bg_default = QColor("#1e1e2e")
        fg_default = QColor("#cdd6f4")

        # Fill background
        painter.fillRect(self.rect(), bg_default)

        fm = QFontMetrics(self._font)

        for row_idx, line in enumerate(self._screen.display):
            y = row_idx * self._cell_h
            for col_idx, char in enumerate(line):
                x = col_idx * self._cell_w

                # Retrieve per-cell style from pyte
                try:
                    cell = self._screen.buffer[row_idx][col_idx]
                    fg = self._pyte_color(cell.fg, fg_default)
                    bg = self._pyte_color(cell.bg, None)
                    bold = cell.bold
                    reverse = cell.reverse
                except (KeyError, IndexError, AttributeError):
                    fg = fg_default
                    bg = None
                    bold = False
                    reverse = False

                if reverse:
                    fg, bg = (bg or bg_default), fg

                if bg and bg != bg_default:
                    painter.fillRect(x, y, self._cell_w, self._cell_h, bg)

                if char and char != " ":
                    draw_font = QFont(self._font)
                    draw_font.setBold(bold)
                    painter.setFont(draw_font)
                    painter.setPen(fg)
                    painter.drawText(
                        QRect(x, y, self._cell_w, self._cell_h),
                        Qt.AlignLeft | Qt.AlignTop,
                        char,
                    )

        # Cursor
        cx = self._screen.cursor.x
        cy = self._screen.cursor.y
        cursor_rect = QRect(cx * self._cell_w, cy * self._cell_h,
                            self._cell_w, self._cell_h)
        painter.fillRect(cursor_rect, QColor("#f5c2e7"))

        painter.end()

    @staticmethod
    def _pyte_color(color_val, default: Optional[QColor]) -> QColor:
        """Convert a pyte color value to QColor."""
        if not color_val or color_val == "default":
            return default or QColor("#cdd6f4")
        if isinstance(color_val, str):
            # Named color or hex
            if color_val.startswith("#"):
                return QColor(color_val)
            # pyte named colours map to ANSI indices
            _NAMED = {
                "black": "#1e1e2e", "red": "#f38ba8", "green": "#a6e3a1",
                "yellow": "#f9e2af", "blue": "#89b4fa", "magenta": "#f5c2e7",
                "cyan": "#89dceb", "white": "#cdd6f4",
                "brightblack": "#585b70", "brightred": "#f38ba8",
                "brightgreen": "#a6e3a1", "brightyellow": "#f9e2af",
                "brightblue": "#89b4fa", "brightmagenta": "#f5c2e7",
                "brightcyan": "#89dceb", "brightwhite": "#ffffff",
            }
            return QColor(_NAMED.get(color_val, "#cdd6f4"))
        if isinstance(color_val, int):
            # 256-color index — use a simple approximation
            return QColor("#cdd6f4")
        return default or QColor("#cdd6f4")

    # ------------------------------------------------------------------
    # Keyboard input
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        data = self._qt_key_to_bytes(event)
        if data:
            self.write_to_pty(data)
        else:
            super().keyPressEvent(event)

    @staticmethod
    def _qt_key_to_bytes(event: QKeyEvent) -> bytes:
        """Translate a Qt key event to VT escape bytes (basic mapping)."""
        key = event.key()
        mods = event.modifiers()
        text = event.text()

        ctrl = bool(mods & Qt.ControlModifier)

        # Control characters
        if ctrl and text:
            c = text.upper()
            if "A" <= c <= "Z":
                return bytes([ord(c) - ord("A") + 1])
            if c == "@":
                return b"\x00"
            if c == "[":
                return b"\x1b"
            if c == "\\":
                return b"\x1c"
            if c == "]":
                return b"\x1d"
            if c == "^":
                return b"\x1e"
            if c == "_":
                return b"\x1f"

        _SPECIAL = {
            Qt.Key_Return:    b"\r",
            Qt.Key_Enter:     b"\r",
            Qt.Key_Backspace: b"\x7f",
            Qt.Key_Tab:       b"\t",
            Qt.Key_Escape:    b"\x1b",
            Qt.Key_Up:        b"\x1b[A",
            Qt.Key_Down:      b"\x1b[B",
            Qt.Key_Right:     b"\x1b[C",
            Qt.Key_Left:      b"\x1b[D",
            Qt.Key_Home:      b"\x1b[H",
            Qt.Key_End:       b"\x1b[F",
            Qt.Key_PageUp:    b"\x1b[5~",
            Qt.Key_PageDown:  b"\x1b[6~",
            Qt.Key_Delete:    b"\x1b[3~",
            Qt.Key_Insert:    b"\x1b[2~",
            Qt.Key_F1:        b"\x1bOP",
            Qt.Key_F2:        b"\x1bOQ",
            Qt.Key_F3:        b"\x1bOR",
            Qt.Key_F4:        b"\x1bOS",
            Qt.Key_F5:        b"\x1b[15~",
            Qt.Key_F6:        b"\x1b[17~",
            Qt.Key_F7:        b"\x1b[18~",
            Qt.Key_F8:        b"\x1b[19~",
            Qt.Key_F9:        b"\x1b[20~",
            Qt.Key_F10:       b"\x1b[21~",
            Qt.Key_F11:       b"\x1b[23~",
            Qt.Key_F12:       b"\x1b[24~",
        }
        if key in _SPECIAL:
            return _SPECIAL[key]

        if text:
            return text.encode("utf-8")

        return b""

    # ------------------------------------------------------------------
    # Focus
    # ------------------------------------------------------------------

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        if self._pty is None:
            self.start()
        if not self._read_timer.isActive():
            self._read_timer.start()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        # Keep timer running so background output is captured; stop if desired
        # for strict "no paint when not focused" behaviour:
        # self._read_timer.stop()

    # ------------------------------------------------------------------
    # Drag and drop (node paths from Houdini Network Editor)
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

        parent = self.parentWidget()
        # Walk up to find VoidGhosttyPanel
        while parent is not None:
            if hasattr(parent, "set_node"):
                parent.set_node(text)
                return
            parent = parent.parentWidget()

        # Free mode: paste path at cursor
        self.write_to_pty(text.encode("utf-8"))

    def sizeHint(self) -> QSize:
        return QSize(640, 400)
