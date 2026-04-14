"""Void Ghostty — TerminalWidget (Phase 4: libghostty-vt rendering path).

Architecture
------------
- TerminalWidget(QWidget) owns a PtyProcess
- A background reader thread drains the PTY into a thread-safe deque
- A QTimer on the main thread consumes the deque, feeds GhosttyBackend,
  sets _vt_dirty, triggers repaint
- GhosttyBackend wraps vg_terminal + vg_render_state (libghostty-vt)
- paintEvent gates full-screen repaint on _vt_dirty — matches Ghostling's
  unconditional-all-rows approach (see BUILD_NOTES.md § Rendering Performance
  Research R1)
- Double-buffered painting: QPixmap backbuffer blitted each frame
- Scrollback via QScrollBar child widget (right edge) wired to vg_scroll*
- Click-vs-drag: clicks go to PTY, drags select text for clipboard

GhosttyBackend (libghostty-vt) is the sole VT rendering path.
The pyte fallback was removed in the Phase 6 refactor.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import collections
import re
import unicodedata
from typing import Optional

try:
    from PySide2.QtWidgets import QWidget, QSizePolicy, QApplication, QScrollBar
    from PySide2.QtGui import (
        QPainter, QColor, QFont, QFontMetrics, QKeyEvent,
        QMouseEvent, QWheelEvent, QPixmap,
    )
    from PySide2.QtCore import Qt, QTimer, QRect, QSize, QPoint, Signal
except ImportError:
    from PySide6.QtWidgets import QWidget, QSizePolicy, QApplication, QScrollBar
    from PySide6.QtGui import (
        QPainter, QColor, QFont, QFontMetrics, QKeyEvent,
        QMouseEvent, QWheelEvent, QPixmap,
    )
    from PySide6.QtCore import Qt, QTimer, QRect, QSize, QPoint, Signal

# vg ctypes bindings (Phase 4) — None if vg.dll not found
try:
    from void_ghostty._vg_ctypes import (
        VG, VgColors, VgCell,
        WRITE_PTY_CFUNCTYPE,
        _cell_buf, _CELL_BUF_COLS,
        _cell_buf_all, _row_counts_buf, _CELL_BUF_ROWS,
    )
except Exception:
    VG = None
    VgColors = None
    VgCell = None
    WRITE_PTY_CFUNCTYPE = None
    _cell_buf = None
    _CELL_BUF_COLS = 256
    _cell_buf_all = None
    _row_counts_buf = None
    _CELL_BUF_ROWS = 64

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
    }

# ---------------------------------------------------------------------------
# Nerd Fonts v3 — double-width PUA glyph ranges
# Cell width is 2 for these; unicodedata.east_asian_width returns 'N' for PUA.
# ---------------------------------------------------------------------------

_NF_WIDE_RANGES: tuple[tuple[int, int], ...] = (
    (0xE000, 0xE00A),   # Pomicons
    (0xE0A0, 0xE0A2),   # Powerline symbols
    (0xE0B4, 0xE0C8),   # Powerline extra — wide arrows / separators
    (0xE0CC, 0xE0D2),   # Powerline extra continued
    (0xE0D4, 0xE0D4),   # Powerline extra — right half-circle
)


def _cell_width(cp: int) -> int:
    """Return display width (1 or 2 cells) for a codepoint.

    Handles Nerd Fonts v3 double-width PUA ranges that unicodedata doesn't know about.
    """
    for lo, hi in _NF_WIDE_RANGES:
        if lo <= cp <= hi:
            return 2
    try:
        eaw = unicodedata.east_asian_width(chr(cp))
        return 2 if eaw in ("W", "F") else 1
    except Exception:
        return 1

# QColor cache for VG backend path — keyed by (r, g, b) tuple.
_VG_QCOLOR_CACHE: dict = {}


def _vg_qcolor(r: int, g: int, b: int) -> "QColor":
    key = (r, g, b)
    c = _VG_QCOLOR_CACHE.get(key)
    if c is None:
        c = QColor(r, g, b)
        _VG_QCOLOR_CACHE[key] = c
    return c


_DSR_RE = re.compile(rb"\x1b\[6n")

# Key → VT byte map — module-level so it is built once, not on every keypress.
# Qt.Key_* constants are resolved at import time.
def _build_key_special() -> dict:
    return {
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
_QT_KEY_SPECIAL: dict = _build_key_special()

# Scrollbar width in pixels
_SB_WIDTH = 12


# ---------------------------------------------------------------------------
# PTY process abstraction
# ---------------------------------------------------------------------------

if os.name == "nt":
    # winpty.cp311-win_amd64.pyd depends on winpty.dll and conpty.dll in the same
    # directory. Register it with the Windows DLL loader before importing, otherwise
    # Python reports "No module named 'winpty.winpty'" (DLL load failure).
    # Derive path from __file__ so this works even if GHOSTTY env var isn't set yet.
    _ghostty_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _winpty_dir = os.path.join(_ghostty_root, "python_deps", "winpty")
    if not os.path.isdir(_winpty_dir):
        # Fallback: GHOSTTY env var
        _winpty_dir = os.path.join(os.environ.get("GHOSTTY", ""), "python_deps", "winpty")
    if os.path.isdir(_winpty_dir):
        os.add_dll_directory(_winpty_dir)
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
# GhosttyBackend — libghostty-vt state machine (Phase 4)
# ---------------------------------------------------------------------------

class GhosttyBackend:
    """libghostty-vt VT state machine.

    Instantiated in TerminalWidget.start() when VG (vg.dll / libvg.so) loads.
    If VG is absent the terminal widget will not render output.
    """

    def __init__(self, cols: int, rows: int) -> None:
        self._term = VG.vg_terminal_new(cols, rows)
        self._rs   = VG.vg_render_state_new()
        self._cols = cols
        self._rows = rows
        self._colors = VgColors()

    def write(self, data: bytes) -> None:
        if self._term:
            VG.vg_terminal_write(self._term, data, len(data))

    def resize(self, cols: int, rows: int, cw: int, ch: int) -> None:
        self._cols = cols
        self._rows = rows
        if self._term:
            VG.vg_terminal_resize(
                self._term, cols, rows, cols * cw, rows * ch, cw, ch
            )

    def update_render_state(self) -> None:
        if self._rs and self._term:
            VG.vg_render_state_update(self._rs, self._term)
            VG.vg_render_colors(self._rs, ctypes.byref(self._colors))

    def clear_dirty(self) -> None:
        """Reset the global render-state dirty flag after a completed frame.

        Required by the libghostty-vt API (BUILD_NOTES § 'Mark global dirty
        clean').  Without this, vg_render_state_update accumulates dirty marks
        across frames instead of tracking only rows that changed since the last
        render.
        """
        if self._rs:
            VG.vg_render_clear_dirty(self._rs)

    def get_row(self, row_idx: int, cell_buf, max_cols: int) -> int:
        """Fill cell_buf with cells for row_idx. Returns cell count written."""
        if not self._rs:
            return 0
        return VG.vg_render_row_cells(self._rs, row_idx, cell_buf, max_cols)

    def get_all_rows(self, cell_buf_all, cells_per_row: int,
                     out_counts, max_rows: int) -> int:
        """Single-pass render of all rows into flat cell_buf_all.

        Returns number of rows filled.  out_counts[i] holds the cell count
        for row i.  Matches Ghostling's single-iterator-pass design and avoids
        the dirty-row-seeking bug in per-row index-based calls.
        """
        if not self._rs:
            return 0
        return VG.vg_render_row_cells_all(
            self._rs, cell_buf_all, cells_per_row, out_counts, max_rows
        )

    def get_cursor(self) -> tuple[int, int, bool]:
        cx = ctypes.c_int(0)
        cy = ctypes.c_int(0)
        vis = ctypes.c_int(0)
        if self._rs:
            VG.vg_render_cursor(
                self._rs,
                ctypes.byref(cx), ctypes.byref(cy), ctypes.byref(vis),
            )
        return int(cx.value), int(cy.value), bool(vis.value)

    def scroll(self, delta: int) -> None:
        if self._term:
            VG.vg_scroll(self._term, delta)

    def scroll_top(self) -> None:
        if self._term:
            VG.vg_scroll_top(self._term)

    def scroll_bottom(self) -> None:
        if self._term:
            VG.vg_scroll_bottom(self._term)

    def scrollbar(self) -> tuple[int, int, int]:
        """Returns (total, offset, length) as ints."""
        t = ctypes.c_uint64(0)
        o = ctypes.c_uint64(0)
        l = ctypes.c_uint64(0)
        if self._term:
            VG.vg_scrollbar(
                self._term,
                ctypes.byref(t), ctypes.byref(o), ctypes.byref(l),
            )
        return int(t.value), int(o.value), int(l.value)

    def set_write_pty_fn(self, write_fn) -> None:
        """Register the PTY write-back callback for OSC/DA query responses.

        write_fn(data: bytes) is called on the main thread whenever libghostty-vt
        needs to send a response to the PTY master (e.g. DA1, DA2, DSR replies).
        Without this, programs that send device-attribute queries (nvim, tmux, fzf)
        may hang waiting for a response that never arrives.

        Keeps self._write_pty_cb alive so ctypes does not garbage-collect the
        C-callable function pointer before the terminal is freed.
        """
        if not self._term:
            return

        def _cb(term, ud, data_ptr, length):
            if length > 0:
                write_fn(ctypes.string_at(data_ptr, length))

        self._write_pty_cb = WRITE_PTY_CFUNCTYPE(_cb)
        VG.vg_terminal_set_write_pty_fn(self._term, self._write_pty_cb, None)

    def free(self) -> None:
        if self._rs:
            VG.vg_render_state_free(self._rs)
            self._rs = None
        if self._term:
            VG.vg_terminal_free(self._term)
            self._term = None


# ---------------------------------------------------------------------------
# FontAtlas — deferred stub for Phase 5 (OpenGL renderer)
# ---------------------------------------------------------------------------

class FontAtlas:
    """Deferred stub — GPU glyph texture atlas for the future OpenGL rendering path.

    NOT instantiated in Phase 4. The current QPainter path (GhosttyBackend +
    _paint_rows_vg) calls QPainter.drawText() per cell, relying on Qt's own
    internal glyph cache. When QOpenGLWidget replaces QPainter, swap in
    FontAtlas.draw() at the integration point below.

    ── Atlas design (src/font/Atlas.zig @ ghostty-org/ghostty@0790937) ──────────

    Storage
      Square power-of-2 texture (e.g. 1024×1024 or 2048×2048).
      Grayscale for alpha masks; BGRA for color emoji.
      1-pixel border around every allocation prevents GPU sampler bleed.

    Packing algorithm
      Shelf packing (best-fit) from "A Thousand Ways to Pack the Bin" (Jylänki).
      Node list tracks available shelf segments (x, y, width).
      After each reservation, adjacent same-y nodes are merged to reduce
      fragmentation.

    Growth policy
      If reserve(w, h) returns AtlasFull, double texture size and re-upload.
      No eviction — clear() resets all nodes and zeroes texture data.
      Callers must handle re-rasterization after clear().

    Cache key (caller responsibility — atlas has NO built-in glyph cache)
      dict[
          (codepoint: int, face_idx: int, size_px: int, bold: bool, italic: bool)
          → Region(x: int, y: int, width: int, height: int)
      ]

    GPU synchronization
      modified: int — incremented on any pixel write → call glTexSubImage2D
      resized:  int — incremented on texture resize  → call glTexImage2D (full)

    ── Integration point (future QOpenGLWidget path) ────────────────────────────

    In _paint_rows_vg(), replace:

        bp.setFont(font)
        bp.setPen(fg)
        bp.drawText(QRect(x, y, draw_w, self._cell_h), Qt.AlignLeft, text)

    With:

        region = atlas.get_or_rasterize(
            c.codepoints[0], face_idx=0, size_px=self._cell_h,
            bold=bool(c.bold), italic=bool(c.italic)
        )
        bp.drawPixmap(x, y, atlas.pixmap, region.x, region.y, region.w, region.h)

    For full OpenGL:
        gl.BindTexture(GL_TEXTURE_2D, atlas.texture_id)
        # emit one quad per cell with UV coords from Region
    """

    def __init__(self):
        raise NotImplementedError(
            "FontAtlas is a deferred socket for Phase 5 (OpenGL renderer). "
            "See class docstring for the full atlas design spec from "
            "src/font/Atlas.zig @ ghostty-org/ghostty@0790937."
        )


# ---------------------------------------------------------------------------
# TerminalWidget
# ---------------------------------------------------------------------------

_DEFAULT_SHELL_WINDOWS = ["cmd.exe"]
_DEFAULT_SHELL_LINUX = [os.environ.get("SHELL", "/bin/bash")]

_DRAG_THRESHOLD = 4


class TerminalWidget(QWidget):
    """Single-pane terminal: PTY process + GhosttyBackend (libghostty-vt) + QPainter."""

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

        # Font — from Ghostty config, with bold/italic variants
        _font_family = (_CFG.font_family if _CFG is not None
                        else ("Consolas" if os.name == "nt" else "Monospace"))
        _font_size   = _CFG.font_size if _CFG is not None else 10
        self._font = QFont(_font_family)
        self._font.setPointSize(_font_size)
        self._font.setStyleHint(QFont.Monospace)
        self._font.setFixedPitch(True)
        self._font_bold = QFont(self._font)
        self._font_bold.setBold(True)
        self._font_italic = QFont(self._font)
        self._font_italic.setItalic(True)
        self._font_bold_italic = QFont(self._font_bold)
        self._font_bold_italic.setItalic(True)
        self._fm = QFontMetrics(self._font)
        self._cell_w = self._fm.horizontalAdvance("M")
        self._cell_h = self._fm.height()
        self._ascent = self._fm.ascent()

        # Nerd Font / user-configured fallback fonts
        # Used per-cell when primary font lacks a glyph (PUA icons etc.)
        self._fallback_fonts: list[tuple[QFont, QFontMetrics]] = []
        _fallback_names = list(_CFG.font_fallbacks) if _CFG is not None else []
        # Auto-add Symbols Nerd Font Mono unless already listed
        if not any("symbols nerd" in n.lower() for n in _fallback_names):
            _fallback_names.append("Symbols Nerd Font Mono")
        for _fb_name in _fallback_names:
            _fb = QFont(_fb_name)
            _fb.setPointSize(_font_size)
            self._fallback_fonts.append((_fb, QFontMetrics(_fb)))

        # Theme colors (default fg/bg/cursor when VG returns zeros)
        self._bg_color        = QColor(_THEME["bg"])
        self._fg_color        = QColor(_THEME["fg"])
        self._cursor_color    = QColor(_THEME.get("cursor", "#fabd2f"))
        self._selection_color = QColor(_THEME.get("selection", "#504945"))

        # GhosttyBackend (Phase 4 — created in start() when VG is loaded)
        self._backend: Optional[GhosttyBackend] = None
        self._vt_dirty = False
        self._full_redraw = True

        # PTY process
        self._pty: Optional[PtyProcess] = None

        # Double-buffered painting
        self._backbuffer: Optional[QPixmap] = None

        # Scrollbar (right-edge child — hidden until scrollback exists)
        self._scrollbar = QScrollBar(Qt.Vertical, self)
        self._scrollbar.setRange(0, 0)
        self._scrollbar.valueChanged.connect(self._on_scrollbar_changed)
        self._scrollbar.hide()

        # Text selection state
        self._mouse_press_pos: Optional[QPoint] = None
        self._selection_start: Optional[tuple[int, int]] = None
        self._selection_end: Optional[tuple[int, int]] = None
        self._selecting = False

        # Multiplexer callbacks (set by VoidGhosttyPanel)
        self.mux_split_h = None  # () -> None  Ctrl+Shift+H
        self.mux_split_v = None  # () -> None  Ctrl+Shift+B
        self.mux_close   = None  # () -> None  Ctrl+Shift+X

        # Read/repaint timer (~30 fps)
        self._read_timer = QTimer(self)
        self._read_timer.setInterval(33)
        self._read_timer.timeout.connect(self._drain_pty)

        # Resize debounce — coalesces rapid resizeEvents during splitter reparenting
        # into a single _resize_screen call at the settled geometry.  Without this,
        # moving a pane into an orphaned (unplaced) splitter fires a 0-width resize
        # that sends a spurious SIGWINCH to the shell at the wrong terminal size.
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(0)
        self._resize_timer.timeout.connect(self._deferred_resize)

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
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error("PTY start failed: %s", exc)
            return
        if VG is not None:
            self._backend = GhosttyBackend(cols, rows)
            self._apply_theme()
            try:
                self._backend.set_write_pty_fn(self._pty.write)
            except Exception:
                pass  # DLL pre-dates vg_terminal_set_write_pty_fn — degrade gracefully
        self._read_timer.start()

    def stop(self) -> None:
        self._read_timer.stop()
        if self._backend is not None:
            self._backend.free()
            self._backend = None
        if self._pty is not None:
            self._pty.terminate()
            self._pty = None

    def write_to_pty(self, data: bytes) -> None:
        if self._pty is not None:
            self._pty.write(data)
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
        if not data:
            return

        if self._backend is not None:
            self._backend.write(data)
            self._vt_dirty = True

        self.update()

    # ------------------------------------------------------------------
    # Layout / resize
    # ------------------------------------------------------------------

    def _compute_grid(self) -> tuple[int, int]:
        sb_w = _SB_WIDTH if not self._scrollbar.isHidden() else 0
        w = max(self.width() - sb_w, 1)
        h = max(self.height(), 1)
        cols = max(w // self._cell_w, 1)
        rows = max(h // self._cell_h, 1)
        return cols, rows

    def _resize_screen(self, cols: int, rows: int) -> None:
        if cols == self._cols and rows == self._rows:
            return
        self._cols = cols
        self._rows = rows
        if self._backend is not None:
            self._backend.resize(cols, rows, self._cell_w, self._cell_h)
            if self._pty is not None:
                self._pty.resize(cols, rows)
            self._vt_dirty = True
        elif self._pty is not None:
            self._pty.resize(cols, rows)
        self._backbuffer = None
        self._full_redraw = True

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Keep scrollbar pinned to right edge
        self._scrollbar.setGeometry(
            self.width() - _SB_WIDTH, 0, _SB_WIDTH, self.height()
        )
        # Debounce: start (or restart) the zero-timeout timer instead of calling
        # _resize_screen directly.  Multiple rapid resizeEvents during splitter
        # reparenting collapse into one _deferred_resize at the settled geometry.
        self._resize_timer.start()

    def _deferred_resize(self) -> None:
        """Called once (debounced) after geometry settles."""
        cols, rows = self._compute_grid()
        self._resize_screen(cols, rows)

    # ------------------------------------------------------------------
    # Painting — GhosttyBackend path
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        w, h = self.width(), self.height()
        if w < 1 or h < 1:
            return

        if self._backend is not None:
            # Allocate backbuffer if needed
            if (self._backbuffer is None
                    or self._backbuffer.width() != w
                    or self._backbuffer.height() != h):
                self._backbuffer = QPixmap(w, h)
                self._backbuffer.fill(self._bg_color)
                self._vt_dirty = True

            if self._vt_dirty:
                self._backend.update_render_state()
                self._paint_rows_vg()
                self._backend.clear_dirty()
                self._vt_dirty = False
                self._refresh_scrollbar()

        if self._backbuffer is None:
            return

        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._backbuffer)
        self._paint_cursor(painter)
        if self._selecting and self._selection_start and self._selection_end:
            self._paint_selection(painter)
        painter.end()

    def _paint_rows_vg(self) -> None:
        """Render dirty rows from GhosttyBackend into the backbuffer.

        Uses vg_render_row_cells_all for a single iterator pass (Ghostling R1).
        The per-row index-seeking approach in the old vg_render_row_cells suffers
        from dirty-row iterator exhaustion: clearing row N's dirty bit before
        seeking to row N+1 shifts every subsequent row, producing blank output.

        The single-pass function iterates the iterator once in sequence, matching
        Ghostling's while(row_iterator_next) loop exactly.  Only dirty rows are
        returned; unchanged rows remain as-is in the backbuffer (correct because
        the backbuffer persists between frames).

        Color resolution: _lib.cpp initialises cell fg to {0,0,0} and only
        overwrites it when ghostty returns an explicit color (GHOSTTY_SUCCESS).
        Cells that use the terminal's *default* foreground therefore have
        fg=(0,0,0).  We fall back to colors.fg when the cell fg is all-zero,
        matching Ghostling's ``GhosttyColorRgb fg = colors.foreground; …`` pattern.
        """
        colors = self._backend._colors

        # Global default colors — fall back to Gruvbox when VG returns zeros.
        _nz = lambda r, g, b: (r or g or b)
        if _nz(colors.bg.r, colors.bg.g, colors.bg.b):
            bg_color = _vg_qcolor(colors.bg.r, colors.bg.g, colors.bg.b)
        else:
            bg_color = self._bg_color

        if _nz(colors.fg.r, colors.fg.g, colors.fg.b):
            default_fg = _vg_qcolor(colors.fg.r, colors.fg.g, colors.fg.b)
        else:
            default_fg = self._fg_color

        cells_per_row = min(self._cols, _CELL_BUF_COLS)
        max_rows      = min(self._rows, _CELL_BUF_ROWS)
        n_rows = self._backend.get_all_rows(
            _cell_buf_all, cells_per_row, _row_counts_buf, max_rows
        )

        if n_rows == 0:
            return  # nothing dirty — backbuffer already up to date

        bp = QPainter(self._backbuffer)
        fm = self._fm
        ascent = self._ascent

        for row_idx in range(n_rows):
            n = _row_counts_buf[row_idx]
            y = row_idx * self._cell_h

            # Clear this row's background before drawing cells.
            bp.fillRect(0, y, cells_per_row * self._cell_w, self._cell_h, bg_color)

            row_offset = row_idx * cells_per_row
            for i in range(n):
                c = _cell_buf_all[row_offset + i]
                x = i * self._cell_w  # col field unset in C; use loop index (Ghostling)

                # Resolve fg — fall back to terminal default when cell has no explicit color.
                cell_fg = (_vg_qcolor(c.fg_r, c.fg_g, c.fg_b)
                           if (c.fg_r or c.fg_g or c.fg_b) else default_fg)
                cell_bg_has = bool(c.has_bg)
                cell_bg = (_vg_qcolor(c.bg_r, c.bg_g, c.bg_b) if cell_bg_has else None)

                # Resolve fg / bg
                if c.inverse:
                    fg      = cell_bg if cell_bg_has else bg_color
                    bg_c    = cell_fg
                    draw_bg = True
                else:
                    fg      = cell_fg
                    draw_bg = cell_bg_has
                    bg_c    = cell_bg

                if c.faint:
                    fg = QColor(fg)   # clone before mutating cached color
                    fg.setAlpha(128)

                # Wide-char detection — handles Nerd Font PUA + CJK (BUILD_NOTES R2)
                draw_w = self._cell_w
                if c.codepoint_count > 0:
                    draw_w = self._cell_w * _cell_width(c.codepoints[0])

                if draw_bg and bg_c:
                    bp.fillRect(x, y, draw_w, self._cell_h, bg_c)

                if c.codepoint_count > 0:
                    # Grapheme codepoints → string (BUILD_NOTES R3)
                    try:
                        text = "".join(
                            chr(c.codepoints[k])
                            for k in range(c.codepoint_count)
                        )
                    except (ValueError, OverflowError):
                        text = "\uFFFD"

                    # Choose font variant (bold/italic/regular)
                    if c.bold:
                        chosen_font = self._font_bold_italic if c.italic else self._font_bold
                    else:
                        chosen_font = self._font_italic if c.italic else self._font

                    # Font fallback: try user-configured and Symbols Nerd Font fonts
                    # before substituting replacement character.
                    if c.codepoints[0] > 0x7F and not fm.inFontUcs4(c.codepoints[0]):
                        found_fb = False
                        for fb_font, fb_fm in self._fallback_fonts:
                            if fb_fm.inFontUcs4(c.codepoints[0]):
                                chosen_font = fb_font
                                found_fb = True
                                break
                        if not found_fb:
                            text = "\uFFFD"

                    bp.setFont(chosen_font)
                    bp.setPen(fg)
                    bp.drawText(
                        QRect(x, y, draw_w, self._cell_h),
                        Qt.AlignLeft | Qt.AlignTop,
                        text,
                    )

                # Underline
                if c.underline:
                    bp.setPen(fg)
                    uy = y + ascent + 2
                    bp.drawLine(x, uy, x + draw_w - 1, uy)

                # Strikethrough
                if c.strikethrough:
                    bp.setPen(fg)
                    sy = y + self._cell_h // 2
                    bp.drawLine(x, sy, x + draw_w - 1, sy)

        bp.end()

    # ------------------------------------------------------------------
    # Cursor painting (both paths)
    # ------------------------------------------------------------------

    def _paint_cursor(self, painter: QPainter) -> None:
        if self._backend is not None:
            cx, cy, visible = self._backend.get_cursor()
            if not visible:
                return
            c = self._backend._colors
            # Fall back to Gruvbox cursor color when VG returns zero.
            if c.cursor.r or c.cursor.g or c.cursor.b:
                color = QColor(c.cursor.r, c.cursor.g, c.cursor.b)
            else:
                color = self._cursor_color
        else:
            return

        painter.fillRect(
            cx * self._cell_w, cy * self._cell_h,
            self._cell_w, self._cell_h,
            color,
        )

    # ------------------------------------------------------------------
    # Selection painting (shared between both paths)
    # ------------------------------------------------------------------

    def _paint_selection(self, painter: QPainter) -> None:
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
    # Scrollback QScrollBar
    # ------------------------------------------------------------------

    def _refresh_scrollbar(self) -> None:
        if self._backend is None:
            return
        total, offset, length = self._backend.scrollbar()
        if total <= length:
            if not self._scrollbar.isHidden():
                self._scrollbar.hide()
                # Recalculate grid — scrollbar no longer takes space
                cols, rows = self._compute_grid()
                self._resize_screen(cols, rows)
            return
        if self._scrollbar.isHidden():
            self._scrollbar.show()
            self._scrollbar.setGeometry(
                self.width() - _SB_WIDTH, 0, _SB_WIDTH, self.height()
            )
            cols, rows = self._compute_grid()
            self._resize_screen(cols, rows)
        self._scrollbar.setRange(0, int(total - length))
        self._scrollbar.blockSignals(True)
        self._scrollbar.setValue(int(offset))
        self._scrollbar.blockSignals(False)

    def _on_scrollbar_changed(self, value: int) -> None:
        if self._backend is None:
            return
        _, current_offset, _ = self._backend.scrollbar()
        delta = value - int(current_offset)
        if delta != 0:
            self._backend.scroll(delta)
            self._vt_dirty = True
            self.update()

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
        if self._backend is not None:
            # Extract text from VG render state via cell buffer
            cell_buf = _cell_buf
            max_cells = min(self._cols, _CELL_BUF_COLS)
            for row in range(sr, er + 1):
                n = self._backend.get_row(row, cell_buf, max_cells)
                row_chars = []
                for ci in range(n):
                    c = cell_buf[ci]
                    if c.codepoint_count > 0:
                        try:
                            row_chars.append("".join(
                                chr(c.codepoints[k])
                                for k in range(c.codepoint_count)
                            ))
                        except (ValueError, OverflowError):
                            row_chars.append(" ")
                    else:
                        row_chars.append(" ")
                line = "".join(row_chars)
                if sr == er:
                    lines.append(line[sc:ec + 1])
                elif row == sr:
                    lines.append(line[sc:])
                elif row == er:
                    lines.append(line[:ec + 1])
                else:
                    lines.append(line)
            return "\n".join(l.rstrip() for l in lines)
        return ""

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
        key  = event.key()

        if (mods & Qt.ControlModifier and mods & Qt.ShiftModifier
                and key == Qt.Key_C):
            if self._selection_start and self._selection_end:
                text = self._get_selected_text()
                if text:
                    QApplication.clipboard().setText(text)
            return

        if (mods & Qt.ControlModifier and mods & Qt.ShiftModifier
                and key == Qt.Key_V):
            clip = QApplication.clipboard().text()
            if clip:
                self.write_to_pty(clip.encode("utf-8"))
            return

        if mods & Qt.ControlModifier and mods & Qt.ShiftModifier:
            if key == Qt.Key_H and self.mux_split_h:
                self.mux_split_h()
                return
            if key == Qt.Key_B and self.mux_split_v:
                self.mux_split_v()
                return
            if key == Qt.Key_X and self.mux_close:
                self.mux_close()
                return
            if key == Qt.Key_T and getattr(self, 'mux_replace_shell', None):
                self.mux_replace_shell()
                return
            if key == Qt.Key_P and getattr(self, 'mux_replace_python', None):
                self.mux_replace_python()
                return

        if self._selecting:
            self._clear_selection()

        data = self._qt_key_to_bytes(event)
        if data:
            self.write_to_pty(data)
        else:
            super().keyPressEvent(event)

    @staticmethod
    def _qt_key_to_bytes(event: QKeyEvent) -> bytes:
        key  = event.key()
        mods = event.modifiers()
        text = event.text()
        ctrl = bool(mods & Qt.ControlModifier)

        if ctrl and text:
            c = text.upper()
            if "A" <= c <= "Z":
                return bytes([ord(c) - ord("A") + 1])
            if c == "@":  return b"\x00"
            if c == "[":  return b"\x1b"
            if c == "\\":  return b"\x1c"
            if c == "]":  return b"\x1d"
            if c == "^":  return b"\x1e"
            if c == "_":  return b"\x1f"

        if key in _QT_KEY_SPECIAL:
            return _QT_KEY_SPECIAL[key]
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
            self.update()
        elif event.button() == Qt.RightButton:
            clip = QApplication.clipboard().text()
            if clip:
                self.write_to_pty(clip.encode("utf-8"))
        elif event.button() == Qt.MiddleButton:
            try:
                clip = QApplication.clipboard().text(
                    QApplication.clipboard().Selection
                )
            except Exception:
                clip = ""
            if clip:
                self.write_to_pty(clip.encode("utf-8"))
        self.setFocus(Qt.MouseFocusReason)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            return
        if self._selecting and self._selection_start and self._selection_end:
            text = self._get_selected_text()
            if text:
                QApplication.clipboard().setText(text)
        elif self._mouse_press_pos is not None:
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
        if self._backend is not None:
            delta = event.angleDelta().y()
            # negative delta = scroll up (into scrollback)
            self._backend.scroll(-3 if delta > 0 else 3)
            self._vt_dirty = True
            self.update()

    @staticmethod
    def _mouse_sgr(cb: int, col: int, row: int, release: bool) -> bytes:
        suffix = "m" if release else "M"
        return f"\x1b[<{cb};{col + 1};{row + 1}{suffix}".encode()

    # ------------------------------------------------------------------
    # Theme initialisation — sends OSC sequences to the VG backend
    # so the terminal's default fg/bg/cursor/palette match _THEME.
    # Called once in start() immediately after GhosttyBackend is created.
    # ------------------------------------------------------------------

    def _apply_theme(self) -> None:
        if self._backend is None:
            return
        t = _THEME
        _palette_keys = (
            "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
            "brightblack", "brightred", "brightgreen", "brightyellow",
            "brightblue", "brightmagenta", "brightcyan", "brightwhite",
        )
        parts: list = [
            f"\x1b]10;{t['fg']}\x07",                     # default foreground
            f"\x1b]11;{t['bg']}\x07",                     # default background
            f"\x1b]12;{t.get('cursor', t['fg'])}\x07",    # cursor colour
        ]
        for i, key in enumerate(_palette_keys):
            color = t.get(key)
            if color:
                parts.append(f"\x1b]4;{i};{color}\x07")
        self._backend.write("".join(parts).encode("utf-8"))

    # ------------------------------------------------------------------
    # Focus
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        """Grab keyboard focus 80 ms after becoming visible.

        Qt's singleShot(0) focus call in _panel.py fires before deleteLater()
        cleanup finishes on the replaced pane, so focus reverts to the old
        widget.  An 80 ms delay outlasts the cleanup and reliably lands focus
        on the new terminal without requiring a click.
        """
        super().showEvent(event)
        QTimer.singleShot(80, self._try_grab_focus)

    def _try_grab_focus(self) -> None:
        if self.isVisible():
            self.setFocus(Qt.OtherFocusReason)

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
