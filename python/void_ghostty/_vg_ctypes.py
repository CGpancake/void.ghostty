"""Void Ghostty — ctypes bindings for vg.dll / libvg.so.

This is the ONLY file in Python that calls the vg ABI isolation layer.
All callers check ``if VG is None`` and fall back to pyte if the DLL is absent.

Module exports
--------------
VG                  : loaded ctypes.CDLL or None
VgCell              : ctypes.Structure matching _lib.cpp VgCell (80 bytes)
VgColor             : ctypes.Structure matching _lib.cpp VgColor (3 bytes)
VgColors            : ctypes.Structure matching _lib.cpp VgColors
WRITE_PTY_CFUNCTYPE : ctypes CFUNCTYPE for vg_terminal_set_write_pty_fn callback
_cell_buf           : pre-allocated (VgCell * _CELL_BUF_COLS) reusable per-frame buffer
_CELL_BUF_COLS      : int — capacity of _cell_buf
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Structures — must match _lib.cpp exactly
# VgCell: 16×uint32 + 16×uint8 = 64 + 16 = 80 bytes
# ---------------------------------------------------------------------------

class VgCell(ctypes.Structure):
    _fields_ = [
        ("codepoints",      ctypes.c_uint32 * 16),
        ("codepoint_count", ctypes.c_uint8),
        ("col",             ctypes.c_uint8),
        ("fg_r",            ctypes.c_uint8),
        ("fg_g",            ctypes.c_uint8),
        ("fg_b",            ctypes.c_uint8),
        ("bg_r",            ctypes.c_uint8),
        ("bg_g",            ctypes.c_uint8),
        ("bg_b",            ctypes.c_uint8),
        ("has_bg",          ctypes.c_uint8),
        ("bold",            ctypes.c_uint8),
        ("italic",          ctypes.c_uint8),
        ("inverse",         ctypes.c_uint8),
        ("underline",       ctypes.c_uint8),
        ("strikethrough",   ctypes.c_uint8),
        ("faint",           ctypes.c_uint8),
        ("_pad",            ctypes.c_uint8),
    ]


class VgColor(ctypes.Structure):
    _fields_ = [
        ("r", ctypes.c_uint8),
        ("g", ctypes.c_uint8),
        ("b", ctypes.c_uint8),
    ]


class VgColors(ctypes.Structure):
    _fields_ = [
        ("fg",      VgColor),
        ("bg",      VgColor),
        ("cursor",  VgColor),
        ("palette", VgColor * 256),
    ]


# ---------------------------------------------------------------------------
# Callback type for vg_terminal_set_write_pty_fn
# Matches GhosttyTerminalWritePtyFn:
#   void(*)(GhosttyTerminal term, void* userdata, const uint8_t* data, size_t len)
# GhosttyTerminal is an opaque handle — treated as void* at the ABI level.
# ---------------------------------------------------------------------------

WRITE_PTY_CFUNCTYPE = ctypes.CFUNCTYPE(
    None,                               # return void
    ctypes.c_void_p,                    # GhosttyTerminal (opaque handle)
    ctypes.c_void_p,                    # userdata
    ctypes.POINTER(ctypes.c_uint8),     # data
    ctypes.c_size_t,                    # len
)


# ---------------------------------------------------------------------------
# Module-level reusable cell buffers — avoids per-frame allocation
# ---------------------------------------------------------------------------

_CELL_BUF_COLS = 256
_CELL_BUF_ROWS = 64   # max terminal rows supported by single-pass renderer
_cell_buf = (VgCell * _CELL_BUF_COLS)()
# Flat buffer for vg_render_row_cells_all: rows × cols cells, row-major
_cell_buf_all = (VgCell * (_CELL_BUF_COLS * _CELL_BUF_ROWS))()
# Per-row cell counts filled by vg_render_row_cells_all
_row_counts_buf = (ctypes.c_int * _CELL_BUF_ROWS)()


# ---------------------------------------------------------------------------
# argtypes / restype bindings
# ---------------------------------------------------------------------------

def _bind(lib: ctypes.CDLL) -> None:
    """Set argtypes and restype on every vg_* function in lib."""

    # Terminal lifecycle
    lib.vg_terminal_new.restype   = ctypes.c_void_p
    lib.vg_terminal_new.argtypes  = [ctypes.c_int, ctypes.c_int]

    lib.vg_terminal_free.restype  = None
    lib.vg_terminal_free.argtypes = [ctypes.c_void_p]

    lib.vg_terminal_write.restype  = None
    lib.vg_terminal_write.argtypes = [
        ctypes.c_void_p,    # term
        ctypes.c_char_p,    # buf
        ctypes.c_size_t,    # len
    ]

    lib.vg_terminal_resize.restype  = None
    lib.vg_terminal_resize.argtypes = [
        ctypes.c_void_p,    # term
        ctypes.c_int,       # cols
        ctypes.c_int,       # rows
        ctypes.c_int,       # width_px
        ctypes.c_int,       # height_px
        ctypes.c_int,       # cell_w_px
        ctypes.c_int,       # cell_h_px
    ]

    # Optional — only present in DLLs built after vg_terminal_set_write_pty_fn was added.
    # Bind if available; absent on older DLLs (start() degrades gracefully).
    try:
        lib.vg_terminal_set_write_pty_fn.restype  = None
        lib.vg_terminal_set_write_pty_fn.argtypes = [
            ctypes.c_void_p,        # term
            WRITE_PTY_CFUNCTYPE,    # fn — must match GhosttyTerminalWritePtyFn exactly
            ctypes.c_void_p,        # userdata
        ]
    except AttributeError:
        pass  # pre-WRITE_PTY DLL — vg_terminal_set_write_pty_fn not exported yet

    # Render state
    lib.vg_render_state_new.restype   = ctypes.c_void_p
    lib.vg_render_state_new.argtypes  = []

    lib.vg_render_state_free.restype  = None
    lib.vg_render_state_free.argtypes = [ctypes.c_void_p]

    lib.vg_render_state_update.restype  = None
    lib.vg_render_state_update.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    # Row / cell iteration
    lib.vg_render_row_count.restype  = ctypes.c_int
    lib.vg_render_row_count.argtypes = [ctypes.c_void_p]

    lib.vg_render_row_cells.restype  = ctypes.c_int
    lib.vg_render_row_cells.argtypes = [
        ctypes.c_void_p,            # rs
        ctypes.c_int,               # row_index
        ctypes.POINTER(VgCell),     # out_cells
        ctypes.c_int,               # max_cells
    ]

    # Single-pass all-rows renderer (replaces per-row index-seeking approach)
    lib.vg_render_row_cells_all.restype  = ctypes.c_int
    lib.vg_render_row_cells_all.argtypes = [
        ctypes.c_void_p,             # rs
        ctypes.POINTER(VgCell),      # out_cells (flat: row0 cells, row1 cells, ...)
        ctypes.c_int,                # cells_per_row (stride)
        ctypes.POINTER(ctypes.c_int),# out_counts[i] = cell count for row i
        ctypes.c_int,                # max_rows
    ]

    lib.vg_render_clear_dirty.restype  = None
    lib.vg_render_clear_dirty.argtypes = [ctypes.c_void_p]

    lib.vg_render_colors.restype  = None
    lib.vg_render_colors.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(VgColors),
    ]

    lib.vg_render_cursor.restype  = None
    lib.vg_render_cursor.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),   # out_x
        ctypes.POINTER(ctypes.c_int),   # out_y
        ctypes.POINTER(ctypes.c_int),   # out_visible
    ]

    # Key encoding
    lib.vg_key_encode.restype  = ctypes.c_int
    lib.vg_key_encode.argtypes = [
        ctypes.c_void_p,    # term
        ctypes.c_int,       # key_code (GhosttyKey enum)
        ctypes.c_int,       # mods (GhosttyMods bitmask)
        ctypes.c_char_p,    # text (UTF-8 or NULL)
        ctypes.c_char_p,    # out_buf
        ctypes.c_int,       # buf_len
    ]

    # Mouse encoding
    lib.vg_mouse_encode.restype  = ctypes.c_int
    lib.vg_mouse_encode.argtypes = [
        ctypes.c_void_p,    # term
        ctypes.c_int,       # action (GhosttyMouseAction)
        ctypes.c_int,       # button (GhosttyMouseButton)
        ctypes.c_float,     # x
        ctypes.c_float,     # y
        ctypes.c_int,       # mods
        ctypes.c_char_p,    # out_buf
        ctypes.c_int,       # buf_len
    ]

    # Scrollback
    lib.vg_scroll.restype       = None
    lib.vg_scroll.argtypes      = [ctypes.c_void_p, ctypes.c_int]

    lib.vg_scroll_top.restype   = None
    lib.vg_scroll_top.argtypes  = [ctypes.c_void_p]

    lib.vg_scroll_bottom.restype  = None
    lib.vg_scroll_bottom.argtypes = [ctypes.c_void_p]

    lib.vg_scrollbar.restype  = None
    lib.vg_scrollbar.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint64),    # out_total
        ctypes.POINTER(ctypes.c_uint64),    # out_offset
        ctypes.POINTER(ctypes.c_uint64),    # out_len
    ]


# ---------------------------------------------------------------------------
# DLL loading
# ---------------------------------------------------------------------------

def _load_vg() -> Optional[ctypes.CDLL]:
    ghostty = os.environ.get("GHOSTTY", "")
    if sys.platform == "win32":
        dep_path = os.path.join(ghostty, "bin", "windows", "ghostty-vt.dll")
        dll_path = os.path.join(ghostty, "bin", "windows", "vg.dll")
        # Load dependency first so the Windows loader finds it alongside vg.dll
        try:
            ctypes.CDLL(dep_path)
        except Exception:
            pass
    else:
        dll_path = os.path.join(ghostty, "bin", "linux", "libvg.so")

    try:
        lib = ctypes.CDLL(dll_path)
    except Exception:
        return None

    try:
        _bind(lib)
    except Exception:
        return None

    return lib


VG: Optional[ctypes.CDLL] = _load_vg()
