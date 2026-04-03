"""Void Ghostty — TerminalWidget (Phases 2–3: pyte scaffold).

Architecture:
  - TerminalWidget(QWidget) owns a PtyProcess
  - A background reader thread drains the PTY into a thread-safe deque
  - A QTimer on the main thread consumes the deque, feeds pyte, repaints
  - Double-buffered painting: QPixmap backbuffer, only dirty rows redrawn
  - DSR responses are intercepted from pyte and written back to the PTY
  - Click-vs-drag: clicks go to PTY, drags select text for clipboard

The pyte layer is replaced entirely by the libghostty-vt render surface
in Phase 4.  The PTY and process management stay the same.
"""

from __future__ import annotations

import os
import sys
import threading
import collections
import re
from typing import Optional

try:
    from PySide2.QtWidgets import QWidget, QSizePolicy, QApplication
    from PySide2.QtGui import (
        QPainter, QColor, QFont, QFontMetrics, QKeyEvent,
        QMouseEvent, QWheelEvent, QPixmap,
    )
    from PySide2.QtCore import Qt, QTimer, QRect, QSize, QPoint, Signal
except ImportError:
    from PySide6.QtWidgets import QWidget, QSizePolicy, QApplication
    from PySide6.QtGui import (
        QPainter, QColor, QFont, QFontMetrics, QKeyEvent,
        QMouseEvent, QWheelEvent, QPixmap,
    )
    from PySide6.QtCore import Qt, QTimer, QRect, QSize, QPoint, Signal

import pyte

# ---------------------------------------------------------------------------
# Gruvbox Dark theme
# ---------------------------------------------------------------------------

_GRUVBOX = {
    "bg":           "#282828",
    "bg1":          "#3c3836",
    "bg2":          "#504945",
    "fg":           "#ebdbb2",
    "fg_dim":       "#a89984",
    "red":          "#fb4934",
    "green":        "#b8bb26",
    "yellow":       "#fabd2f",
    "blue":         "#83a598",
    "magenta":      "#d3869b",
    "cyan":         "#8ec07c",
    "white":        "#ebdbb2",
    "brightblack":  "#928374",
    "brightred":    "#fb4934",
    "brightgreen":  "#b8bb26",
    "brightyellow": "#fabd2f",
    "brightblue":   "#83a598",
    "brightmagenta":"#d3869b",
    "brightcyan":   "#8ec07c",
    "brightwhite":  "#fbf1c7",
    "cursor":       "#fabd2f",
    "selection":    "#504945",
}

_PYTE_COLOR_MAP = {
    "black":         _GRUVBOX["bg"],
    "red":           _GRUVBOX["red"],
    "green":         _GRUVBOX["green"],
    "yellow":        _GRUVBOX["yellow"],
    "blue":          _GRUVBOX["blue"],
    "magenta":       _GRUVBOX["magenta"],
    "cyan":          _GRUVBOX["cyan"],
    "white":         _GRUVBOX["white"],
    "brightblack":   _GRUVBOX["brightblack"],
    "brightred":     _GRUVBOX["brightred"],
    "brightgreen":   _GRUVBOX["brightgreen"],
    "brightyellow":  _GRUVBOX["brightyellow"],
    "brightblue":    _GRUVBOX["brightblue"],
    "brightmagenta": _GRUVBOX["brightmagenta"],
    "brightcyan":    _GRUVBOX["brightcyan"],
    "brightwhite":   _GRUVBOX["brightwhite"],
}

# 256-color palette: 0-15 Gruvbox, 16-231 6x6x6 cube, 232-255 greyscale.
_256_PALETTE: list[str] = []
_GRUVBOX_16 = [
    _GRUVBOX["bg"], _GRUVBOX["red"], _GRUVBOX["green"], _GRUVBOX["yellow"],
    _GRUVBOX["blue"], _GRUVBOX["magenta"], _GRUVBOX["cyan"], _GRUVBOX["white"],
    _GRUVBOX["brightblack"], _GRUVBOX["brightred"], _GRUVBOX["brightgreen"],
    _GRUVBOX["brightyellow"], _GRUVBOX["brightblue"], _GRUVBOX["brightmagenta"],
    _GRUVBOX["brightcyan"], _GRUVBOX["brightwhite"],
]
_256_PALETTE.extend(_GRUVBOX_16)
_CUBE_STEPS = [0, 95, 135, 175, 215, 255]
for _r in _CUBE_STEPS:
    for _g in _CUBE_STEPS:
        for _b in _CUBE_STEPS:
            _256_PALETTE.append(f"#{_r:02x}{_g:02x}{_b:02x}")
for _i in range(24):
    _v = 8 + _i * 10
    _256_PALETTE.append(f"#{_v:02x}{_v:02x}{_v:02x}")

# QColor cache — avoids creating thousands of identical QColor objects.
_QCOLOR_CACHE: dict = {}


def _cached_qcolor(key) -> Optional[QColor]:
    """Return a cached QColor for a pyte color value, or None."""
    if not key or key == "default":
        return None
    cached = _QCOLOR_CACHE.get(key)
    if cached is not None:
        return cached
    if isinstance(key, str):
        if key.startswith("#"):
            c = QColor(key)
        else:
            hex_val = _PYTE_COLOR_MAP.get(key)
            c = QColor(hex_val) if hex_val else None
    elif isinstance(key, int):
        c = QColor(_256_PALETTE[key]) if 0 <= key < len(_256_PALETTE) else None
    else:
        c = None
    if c is not None:
        _QCOLOR_CACHE[key] = c
    return c


_DSR_RE = re.compile(rb"\x1b\[6n")


# ---------------------------------------------------------------------------
# PTY process abstraction
# ---------------------------------------------------------------------------

if os.name == "nt":
    import winpty as _winpty

    class PtyProcess:
        """Thin wrapper around winpty.PtyProcess (Windows ConPTY).

        Reads happen on a background thread to avoid blocking the Qt
        event loop — winpty's read() blocks until data is available.
        """

        def __init__(
            self, cmd: list[str], cols: int = 80, rows: int = 24,
            cwd: Optional[str] = None, env: Optional[dict] = None,
        ) -> None:
            kwargs = {"dimensions": (rows, cols)}
            if cwd:
                kwargs["cwd"] = cwd
            if env:
                kwargs["env"] = env
            self._proc = _winpty.PtyProcess.spawn(cmd, **kwargs)
            self._buf: collections.deque[bytes] = collections.deque(maxlen=512)
            self._stop = threading.Event()
            self._reader = threading.Thread(
                target=self._read_loop, daemon=True, name="pty-reader"
            )
            self._reader.start()

        def _read_loop(self) -> None:
            while not self._stop.is_set():
                try:
                    data = self._proc.read(4096)
                    if data:
                        if isinstance(data, str):
                            data = data.encode("utf-8", errors="replace")
                        self._buf.append(data)
                    else:
                        self._stop.wait(0.01)
                except Exception:
                    if not self.alive:
                        break
                    self._stop.wait(0.01)

        def read(self) -> bytes:
            """Non-blocking: drain all queued chunks."""
            chunks = []
            while self._buf:
                try:
                    chunks.append(self._buf.popleft())
                except IndexError:
                    break
            return b"".join(chunks) if chunks else b""

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
            self._stop.set()
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

        def __init__(
            self, cmd: list[str], cols: int = 80, rows: int = 24,
            cwd: Optional[str] = None, env: Optional[dict] = None,
        ) -> None:
            self._cols = cols
            self._rows = rows
            self._master_fd, self._slave_fd = _pty.openpty()
            self._set_size(cols, rows)
            kwargs = dict(
                stdin=self._slave_fd,
                stdout=self._slave_fd,
                stderr=self._slave_fd,
                close_fds=True,
                start_new_session=True,
            )
            if cwd:
                kwargs["cwd"] = cwd
            if env:
                kwargs["env"] = env
            self._proc = subprocess.Popen(cmd, **kwargs)
            os.close(self._slave_fd)
            flags = fcntl.fcntl(self._master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self._master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        def _set_size(self, cols: int, rows: int) -> None:
            size = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, size)

        def read(self) -> bytes:
            try:
                r, _, _ = select.select([self._master_fd], [], [], 0.0)
                if r:
                    return os.read(self._master_fd, 4096)
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

# Drag threshold in pixels before a click becomes a selection.
_DRAG_THRESHOLD = 4


class TerminalWidget(QWidget):
    """Single-pane terminal: PTY process + pyte screen + QPainter renderer."""

    title_changed = Signal(str)

    def __init__(
        self,
        cmd: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)

        if cmd is None:
            cmd = _DEFAULT_SHELL_WINDOWS if os.name == "nt" else _DEFAULT_SHELL_LINUX

        self._cmd = cmd
        self._cwd = cwd
        self._env = env
        self._cols = 80
        self._rows = 24
        self._cell_w = 8
        self._cell_h = 16

        # pyte VT state machine
        self._screen = pyte.Screen(self._cols, self._rows)
        self._stream = pyte.ByteStream(self._screen)

        # PTY process
        self._pty: Optional[PtyProcess] = None

        # Font — cache normal and bold
        self._font = QFont("Consolas" if os.name == "nt" else "Monospace")
        self._font.setPointSize(10)
        self._font.setStyleHint(QFont.Monospace)
        self._font.setFixedPitch(True)
        self._font_bold = QFont(self._font)
        self._font_bold.setBold(True)
        fm = QFontMetrics(self._font)
        self._cell_w = fm.horizontalAdvance("M")
        self._cell_h = fm.height()

        # Theme colors (pre-created)
        self._bg_color = QColor(_GRUVBOX["bg"])
        self._fg_color = QColor(_GRUVBOX["fg"])
        self._cursor_color = QColor(_GRUVBOX["cursor"])
        self._selection_color = QColor(_GRUVBOX["selection"])

        # Double-buffered painting
        self._backbuffer: Optional[QPixmap] = None
        self._pending_dirty: set[int] = set()
        self._full_redraw = True  # first paint is always full

        # Text selection state
        self._mouse_press_pos: Optional[QPoint] = None
        self._selection_start: Optional[tuple[int, int]] = None  # (col, row)
        self._selection_end: Optional[tuple[int, int]] = None
        self._selecting = False

        # Read timer
        self._read_timer = QTimer(self)
        self._read_timer.setInterval(33)  # ~30 fps
        self._read_timer.timeout.connect(self._drain_pty)

        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._pty is not None:
            return
        if self.width() > 1 and self.height() > 1:
            cols, rows = self._compute_grid()
        else:
            cols, rows = 80, 24
        self._resize_screen(cols, rows)
        try:
            self._pty = PtyProcess(
                self._cmd, cols=cols, rows=rows,
                cwd=self._cwd, env=self._env,
            )
        except Exception:
            return
        self._read_timer.start()

    def stop(self) -> None:
        self._read_timer.stop()
        if self._pty is not None:
            self._pty.terminate()
            self._pty = None

    def write_to_pty(self, data: bytes) -> None:
        if self._pty is not None:
            self._pty.write(data)
            # Immediate drain for responsive echo
            QTimer.singleShot(1, self._drain_pty)

    # ------------------------------------------------------------------
    # PTY drain (main thread — never blocks)
    # ------------------------------------------------------------------

    def _drain_pty(self) -> None:
        if self._pty is None:
            return
        if not self._pty.alive:
            self._read_timer.stop()
            return
        data = self._pty.read()
        if data:
            if b"\x1b[6n" in data:
                data = self._handle_dsr(data)
            self._stream.feed(data)
            self._pending_dirty.update(self._screen.dirty)
            self._screen.dirty.clear()
            self.update()

    def _handle_dsr(self, data: bytes) -> bytes:
        row = self._screen.cursor.y + 1
        col = self._screen.cursor.x + 1
        self.write_to_pty(f"\x1b[{row};{col}R".encode())
        return _DSR_RE.sub(b"", data)

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
        # Force new backbuffer on next paint to avoid stale edge artifacts
        self._backbuffer = None
        self._full_redraw = True

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cols, rows = self._compute_grid()
        self._resize_screen(cols, rows)

    # ------------------------------------------------------------------
    # Painting — double-buffered, Gruvbox Dark
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        w, h = self.width(), self.height()
        if w < 1 or h < 1:
            return

        # Allocate or resize backbuffer
        need_full = self._full_redraw
        if (self._backbuffer is None
                or self._backbuffer.width() != w
                or self._backbuffer.height() != h):
            self._backbuffer = QPixmap(w, h)
            self._backbuffer.fill(self._bg_color)  # clear entire buffer
            need_full = True

        if need_full:
            self._paint_rows(range(self._rows), fill_edges=True)
            self._full_redraw = False
            self._pending_dirty.clear()
        elif self._pending_dirty:
            self._paint_rows(self._pending_dirty)
            self._pending_dirty.clear()

        # Blit backbuffer to screen
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._backbuffer)

        # Cursor (drawn live, not cached)
        cx = self._screen.cursor.x
        cy = self._screen.cursor.y
        painter.fillRect(
            cx * self._cell_w, cy * self._cell_h,
            self._cell_w, self._cell_h,
            self._cursor_color,
        )

        # Selection highlight (drawn live)
        if self._selecting and self._selection_start and self._selection_end:
            self._paint_selection(painter)

        painter.end()

    def _paint_rows(self, rows, fill_edges: bool = False) -> None:
        """Redraw specific rows on the backbuffer."""
        bp = QPainter(self._backbuffer)
        bp.setFont(self._font)
        buf_w = self._backbuffer.width()
        buf_h = self._backbuffer.height()

        for row_idx in rows:
            if row_idx >= len(self._screen.display):
                continue
            line = self._screen.display[row_idx]
            y = row_idx * self._cell_h

            # Clear full row width (covers right edge strip too)
            bp.fillRect(0, y, buf_w, self._cell_h, self._bg_color)

            for col_idx, char in enumerate(line):
                x = col_idx * self._cell_w

                try:
                    cell = self._screen.buffer[row_idx][col_idx]
                    fg = _cached_qcolor(cell.fg) or self._fg_color
                    bg = _cached_qcolor(cell.bg)
                    bold = cell.bold
                    reverse = cell.reverse
                except (KeyError, IndexError, AttributeError):
                    fg = self._fg_color
                    bg = None
                    bold = False
                    reverse = False

                if reverse:
                    fg, bg = (bg or self._bg_color), fg

                if bg and bg != self._bg_color:
                    bp.fillRect(x, y, self._cell_w, self._cell_h, bg)

                if char and char != " ":
                    bp.setFont(self._font_bold if bold else self._font)
                    bp.setPen(fg)
                    bp.drawText(
                        QRect(x, y, self._cell_w, self._cell_h),
                        Qt.AlignLeft | Qt.AlignTop,
                        char,
                    )

        # Fill bottom edge strip below the last grid row
        if fill_edges:
            grid_bottom = self._rows * self._cell_h
            if grid_bottom < buf_h:
                bp.fillRect(0, grid_bottom, buf_w, buf_h - grid_bottom, self._bg_color)

        bp.end()

    def _paint_selection(self, painter: QPainter) -> None:
        """Draw translucent selection highlight."""
        sc, sr = self._selection_start
        ec, er = self._selection_end
        if (sr, sc) > (er, ec):
            sc, sr, ec, er = ec, er, sc, sr

        sel = QColor(self._selection_color)
        sel.setAlpha(140)

        for row in range(sr, er + 1):
            if sr == er:
                x0, x1 = sc, ec
            elif row == sr:
                x0, x1 = sc, self._cols - 1
            elif row == er:
                x0, x1 = 0, ec
            else:
                x0, x1 = 0, self._cols - 1
            painter.fillRect(
                x0 * self._cell_w, row * self._cell_h,
                (x1 - x0 + 1) * self._cell_w, self._cell_h,
                sel,
            )

    # ------------------------------------------------------------------
    # Text selection helpers
    # ------------------------------------------------------------------

    def _cell_at(self, pos) -> tuple[int, int]:
        col = max(0, min(pos.x() // self._cell_w, self._cols - 1))
        row = max(0, min(pos.y() // self._cell_h, self._rows - 1))
        return col, row

    def _get_selected_text(self) -> str:
        if not self._selection_start or not self._selection_end:
            return ""
        sc, sr = self._selection_start
        ec, er = self._selection_end
        if (sr, sc) > (er, ec):
            sc, sr, ec, er = ec, er, sc, sr

        lines = []
        display = self._screen.display
        for row in range(sr, er + 1):
            if row >= len(display):
                break
            line = display[row]
            if sr == er:
                lines.append(line[sc:ec + 1])
            elif row == sr:
                lines.append(line[sc:])
            elif row == er:
                lines.append(line[:ec + 1])
            else:
                lines.append(line)

        return "\n".join(l.rstrip() for l in lines)

    def _clear_selection(self) -> None:
        self._selecting = False
        self._selection_start = None
        self._selection_end = None
        self.update()

    # ------------------------------------------------------------------
    # Keyboard input
    # ------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:
        mods = event.modifiers()
        key = event.key()

        # Ctrl+Shift+C → copy selection
        if (mods & Qt.ControlModifier and mods & Qt.ShiftModifier
                and key == Qt.Key_C):
            if self._selection_start and self._selection_end:
                text = self._get_selected_text()
                if text:
                    QApplication.clipboard().setText(text)
            return

        # Ctrl+Shift+V → paste clipboard
        if (mods & Qt.ControlModifier and mods & Qt.ShiftModifier
                and key == Qt.Key_V):
            clip = QApplication.clipboard().text()
            if clip:
                self.write_to_pty(clip.encode("utf-8"))
            return

        # Any other key clears selection
        if self._selecting:
            self._clear_selection()

        data = self._qt_key_to_bytes(event)
        if data:
            self.write_to_pty(data)
        else:
            super().keyPressEvent(event)

    @staticmethod
    def _qt_key_to_bytes(event: QKeyEvent) -> bytes:
        key = event.key()
        mods = event.modifiers()
        text = event.text()

        ctrl = bool(mods & Qt.ControlModifier)

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
    # Mouse — click-vs-drag: clicks go to PTY, drags select text
    # ------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._mouse_press_pos = QPoint(event.pos())
            self._selecting = False
            self._selection_start = None
            self._selection_end = None
            self.update()  # clear old selection highlight
        elif event.button() == Qt.RightButton:
            # Right-click paste
            clip = QApplication.clipboard().text()
            if clip:
                self.write_to_pty(clip.encode("utf-8"))
        elif event.button() == Qt.MiddleButton:
            # Middle-click paste (X11 selection)
            clip = QApplication.clipboard().text(QApplication.clipboard().Selection)
            if clip:
                self.write_to_pty(clip.encode("utf-8"))
        self.setFocus(Qt.MouseFocusReason)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            return

        if self._selecting and self._selection_start and self._selection_end:
            # Drag completed — copy to clipboard
            text = self._get_selected_text()
            if text:
                QApplication.clipboard().setText(text)
        elif self._mouse_press_pos is not None:
            # It was just a click — send press+release to PTY (for nvim etc.)
            col, row = self._cell_at(event.pos())
            self.write_to_pty(self._mouse_sgr(0, col, row, release=False))
            self.write_to_pty(self._mouse_sgr(0, col, row, release=True))

        self._mouse_press_pos = None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not (event.buttons() & Qt.LeftButton) or self._mouse_press_pos is None:
            return

        delta = event.pos() - self._mouse_press_pos
        if not self._selecting:
            if abs(delta.x()) > _DRAG_THRESHOLD or abs(delta.y()) > _DRAG_THRESHOLD:
                self._selecting = True
                self._selection_start = self._cell_at(self._mouse_press_pos)

        if self._selecting:
            self._selection_end = self._cell_at(event.pos())
            self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        col, row = self._cell_at(pos)
        delta = event.angleDelta().y()
        cb = 64 if delta > 0 else 65
        self.write_to_pty(self._mouse_sgr(cb, col, row, release=False))

    @staticmethod
    def _mouse_sgr(cb: int, col: int, row: int, release: bool) -> bytes:
        suffix = "m" if release else "M"
        return f"\x1b[<{cb};{col + 1};{row + 1}{suffix}".encode()

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
        while parent is not None:
            if hasattr(parent, "set_node"):
                parent.set_node(text)
                return
            parent = parent.parentWidget()
        self.write_to_pty(text.encode("utf-8"))

    def sizeHint(self) -> QSize:
        return QSize(640, 400)
