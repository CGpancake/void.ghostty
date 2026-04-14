# BUILD_NOTES.md — Void Ghostty

## Pinned Commits

| Project | Commit | Date |
|---|---|---|
| ghostty-org/ghostty | `0790937d03df6e7a9420c61de91ce520a85fe4ef` | 2026-04-02 |
| ghostty-org/ghostling (reference) | `bebca84668947bfc92b9a30ed58712e1c34eee1d` | — |

Ghostling is the official reference implementation of a cross-platform terminal built on libghostty-vt using Raylib. Read its `main.c` (~1340 lines) before touching Phase 4.

---

## libghostty — Two Separate APIs

Ghostty ships two distinct C APIs under `include/`. They are **not** interchangeable.

### `ghostty.h` — Native embedding API (macOS/iOS ONLY)

Header comment verbatim: *"The only consumer of this API is the macOS app"*

- Requires a native `NSView*` (macOS) or `UIView*` (iOS) for surface attachment
- `ghostty_platform_e` only has `MACOS` and `IOS` — no Windows, no Linux
- Provides a full GPU-rendered terminal surface via Metal / IOSurfaceLayer
- Tab and split management are internally handled; host triggers via action callbacks
- **Void Ghostty does not use this API.**

### `ghostty/vt.h` — libghostty-vt (cross-platform: Linux, Windows, macOS, WASM)

- Pure terminal state machine — zero dependencies (not even libc)
- Handles VT sequence parsing, cell grid, scrollback, input encoding
- Consumer provides their own PTY, window, and renderer
- **This is what Void Ghostty uses.** Ghostling uses this with Raylib as the renderer.
- For Void Ghostty: PTY via `winpty`/`pty.openpty()`, renderer via Qt `QPainter` (Phase 4)

---

## libghostty-vt API Surface

### Header layout

```
include/ghostty/
├── vt.h                  ← main entry point (includes everything below)
├── vt/
│   ├── types.h           ← GhosttyResult, GhosttyString, GHOSTTY_API macro
│   ├── terminal.h        ← GhosttyTerminal — core VT state machine
│   ├── render.h          ← GhosttyRenderState — cell-iteration renderer state
│   ├── screen.h          ← GhosttyCell, GhosttyRow — cell/row value types
│   ├── style.h           ← GhosttyStyle — bold/italic/underline/color per cell
│   ├── color.h           ← GhosttyColorRgb, GhosttyColorPaletteIndex
│   ├── allocator.h       ← custom allocator interface (NULL = libc malloc)
│   ├── key/
│   │   ├── event.h       ← GhosttyKeyEvent, GhosttyKey enum, GhosttyMods
│   │   └── encoder.h     ← GhosttyKeyEncoder → VT byte sequences
│   ├── mouse/
│   │   ├── event.h       ← GhosttyMouseEvent, GhosttyMouseButton, GhosttyMouseAction
│   │   └── encoder.h     ← GhosttyMouseEncoder → VT byte sequences
│   ├── osc.h             ← GhosttyOscParser — OSC sequence parser
│   ├── sgr.h             ← GhosttySgrParser — SGR attribute parser
│   ├── modes.h           ← GhosttyMode — terminal mode management
│   ├── point.h           ← GhosttyPoint — coordinate types
│   ├── grid_ref.h        ← GhosttyGridRef — direct cell/row access (prefer render state)
│   ├── formatter.h       ← terminal content → plain text / VT / HTML
│   ├── paste.h           ← paste safety validation and bracketed paste encoding
│   ├── focus.h           ← focus event encoding (CSI I / CSI O)
│   ├── device.h          ← DA1/DA2/DA3 device attribute responses
│   ├── size_report.h     ← terminal size reporting (XTWINOPS)
│   ├── build_info.h      ← compile-time query (SIMD, Kitty, optimize mode, version)
│   └── wasm.h            ← WASM allocation helpers (wasm32 only)
```

### Core result type

```c
typedef enum {
    GHOSTTY_SUCCESS        =  0,
    GHOSTTY_OUT_OF_MEMORY  = -1,
    GHOSTTY_INVALID_VALUE  = -2,
    GHOSTTY_OUT_OF_SPACE   = -3,  // buffer too small; call again with larger buf
    GHOSTTY_NO_VALUE       = -4,
} GhosttyResult;
```

`GHOSTTY_OUT_OF_SPACE` is the pattern for buffer sizing: call with a small buffer, get -3, call again with the reported size.

### Terminal lifecycle

```c
// Create
GhosttyTerminalOptions opts = {
    .size = sizeof(GhosttyTerminalOptions),
    .columns = 220, .rows = 50,
    .max_scrollback = 10000,  // lines; 0 = unlimited
};
GhosttyTerminal term = ghostty_terminal_new(opts);

// Resize (call whenever TerminalWidget size changes)
ghostty_terminal_resize(term, cols, rows,
    width_px, height_px, cell_width_px, cell_height_px);

// Feed PTY output into terminal
ghostty_terminal_vt_write(term, buf, len);

// Register effect callbacks
ghostty_terminal_set(term, GHOSTTY_TERMINAL_SET_WRITE_PTY_FN, my_write_pty_cb);
ghostty_terminal_set(term, GHOSTTY_TERMINAL_SET_TITLE_CHANGED_FN, my_title_cb);
ghostty_terminal_set(term, GHOSTTY_TERMINAL_SET_BELL_FN, my_bell_cb);

// Destroy
ghostty_terminal_free(term);
```

### Terminal callbacks (effect dispatch)

| Callback enum | C signature | When it fires |
|---|---|---|
| `WRITE_PTY_FN` | `void(*)(void* ud, const uint8_t* buf, size_t len)` | Terminal wants to write to PTY (OSC responses, device attributes, etc.) |
| `TITLE_CHANGED_FN` | `void(*)(void* ud, GhosttyString title)` | OSC 0 / OSC 2 title change |
| `BELL_FN` | `void(*)(void* ud)` | BEL character (0x07) |
| `COLOR_SCHEME_FN` | `void(*)(void* ud, GhosttyColorScheme*)` | Terminal queries current color scheme |
| `DEVICE_ATTRIBUTES_FN` | `void(*)(void* ud, GhosttyDeviceAttributesPrimary*)` | DA1 query |
| `ENQUIRY_FN` | `void(*)(void* ud)` | ENQ (0x05) |
| `SIZE_FN` | `void(*)(void* ud, GhosttySizeReportStyle, GhosttySizeReportSize*)` | XTWINOPS size query |

The `WRITE_PTY_FN` callback is critical — write its bytes directly to the PTY master fd. This is how OSC responses reach the shell.

### Render state — cell-iteration model

This is the core rendering loop, matching Ghostling's implementation:

```c
// Create once per TerminalWidget
GhosttyRenderState rs = ghostty_render_state_new();

// Call after each ghostty_terminal_vt_write() batch (or on timer)
ghostty_render_state_update(rs, term);

// Per-frame render in paintEvent() / paintGL():
GhosttyRenderStateColors colors = GHOSTTY_INIT_SIZED(GhosttyRenderStateColors);
ghostty_render_state_colors_get(rs, &colors);
// colors.foreground, colors.background, colors.cursor, colors.palette[256]

GhosttyRenderStateRowIterator row_iter = NULL;
ghostty_render_state_get(rs, GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR, &row_iter);

int y = 0;
while (ghostty_render_state_row_iterator_next(row_iter)) {
    GhosttyRenderStateRowCells cells = NULL;
    ghostty_render_state_row_get(row_iter, GHOSTTY_RENDER_STATE_ROW_DATA_CELLS, &cells);

    int x = 0;
    while (ghostty_render_state_row_cells_next(cells)) {
        uint32_t grapheme_len = 0;
        ghostty_render_state_row_cells_get(cells,
            GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_LEN, &grapheme_len);

        if (grapheme_len == 0) {
            // Empty cell — draw background only if non-default
            GhosttyColorRgb bg = {0};
            bool has_bg = ghostty_render_state_row_cells_get(cells,
                GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_BG_COLOR, &bg) == GHOSTTY_SUCCESS;
            if (has_bg) { /* fill rect x,y with bg */ }
            x += cell_w;
            continue;
        }

        // Read codepoints (grapheme cluster, up to 16)
        uint32_t codepoints[16];
        uint32_t len = grapheme_len < 16 ? grapheme_len : 16;
        ghostty_render_state_row_cells_get(cells,
            GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_BUF, codepoints);
        // UTF-8 encode codepoints → text string

        GhosttyColorRgb fg = colors.foreground;
        ghostty_render_state_row_cells_get(cells,
            GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_FG_COLOR, &fg);

        GhosttyColorRgb bg = colors.background;
        bool has_bg = ghostty_render_state_row_cells_get(cells,
            GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_BG_COLOR, &bg) == GHOSTTY_SUCCESS;

        GhosttyStyle style = GHOSTTY_INIT_SIZED(GhosttyStyle);
        ghostty_render_state_row_cells_get(cells,
            GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_STYLE, &style);
        // style.bold, style.italic, style.faint, style.inverse,
        // style.invisible, style.blink, style.strikethrough, style.underline

        if (style.inverse) { GhosttyColorRgb tmp = fg; fg = bg; bg = tmp; has_bg = true; }

        // Draw: fill bg rect, draw text with fg
        x += cell_w;
    }
    // Mark row clean after rendering
    bool clean = false;
    ghostty_render_state_row_set(row_iter, GHOSTTY_RENDER_STATE_ROW_OPTION_DIRTY, &clean);
    y += cell_h;
}

// Draw cursor
bool cursor_visible = false, cursor_in_vp = false;
uint16_t cx = 0, cy = 0;
ghostty_render_state_get(rs, GHOSTTY_RENDER_STATE_DATA_CURSOR_VISIBLE, &cursor_visible);
ghostty_render_state_get(rs, GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_HAS_VALUE, &cursor_in_vp);
ghostty_render_state_get(rs, GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_X, &cx);
ghostty_render_state_get(rs, GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_Y, &cy);
// Draw cursor block at (cx * cell_w, cy * cell_h) using colors.cursor

// Mark global dirty clean
GhosttyRenderStateDirty clean_state = GHOSTTY_RENDER_STATE_DIRTY_FALSE;
ghostty_render_state_set(rs, GHOSTTY_RENDER_STATE_OPTION_DIRTY, &clean_state);
```

### Key encoding

```c
GhosttyKeyEncoder ke = ghostty_key_encoder_new();
// Sync encoder to terminal's current keyboard mode (Kitty protocol, etc.)
ghostty_key_encoder_setopt_from_terminal(ke, term);

// On Qt keyPressEvent: encode → write to PTY
uint8_t buf[64];
GhosttyResult r = ghostty_key_encoder_encode(ke, key_event, buf, sizeof(buf));
if (r == GHOSTTY_SUCCESS) { write_to_pty(buf, r_len); }
else if (r == GHOSTTY_OUT_OF_SPACE) { /* use larger buf */ }
```

`GhosttyKeyEvent` fields: action (PRESS/RELEASE/REPEAT), key (W3C KeyboardEvent code), mods (bitmask), text (UTF-8 string), unshifted codepoint.

### Mouse encoding

```c
GhosttyMouseEncoder me = ghostty_mouse_encoder_new();
ghostty_mouse_encoder_setopt_from_terminal(me, term);

uint8_t buf[32];
GhosttyResult r = ghostty_mouse_encoder_encode(me, mouse_event, buf, sizeof(buf));
if (r == GHOSTTY_SUCCESS) { write_to_pty(buf, r_len); }
```

Supports X10, normal, button, any-motion modes; X10/UTF-8/SGR/URxvt/SGR-Pixels encoding. `setopt_from_terminal` keeps the encoder in sync with whatever mode the running program requests.

### Scrollback

```c
// Scroll
GhosttyTerminalScrollViewport sv = {
    .tag = GHOSTTY_TERMINAL_SCROLL_VIEWPORT_DELTA,
    .value.delta = -3  // negative = scroll up
};
ghostty_terminal_scroll_viewport(term, sv);
// Tags: TOP, BOTTOM, DELTA

// Query scrollbar position
GhosttyTerminalScrollbar sb;
ghostty_terminal_get(term, GHOSTTY_TERMINAL_DATA_SCROLLBAR, &sb);
// sb.total, sb.offset, sb.len  (all uint64_t)
```

### Paste safety

```c
// Before pasting into PTY: validate and encode
uint8_t out[4096];
GhosttyResult r = ghostty_paste_encode(raw_text, raw_len, out, sizeof(out),
    .bracketed = term_is_in_bracketed_paste_mode,
    .strip_unsafe = true);
write_to_pty(out, r_len);
```

---

## Build — libghostty-vt

### Prerequisites

- Zig 0.15.x (`zig version` to confirm)
- C compiler (gcc/clang on Linux, MSVC on Windows)
- No other dependencies

### Build the shared library

```bash
# Clone ghostty at the pinned commit
git clone https://github.com/ghostty-org/ghostty.git
cd ghostty
git checkout 0790937d03df6e7a9420c61de91ce520a85fe4ef

# Build libghostty-vt shared library
# NOTE: at pinned commit 0790937, the step is NOT "zig build lib-vt".
# The correct invocation uses the -Demit-lib-vt flag:
zig build -Demit-lib-vt=true --release=fast

# Outputs:
#   Linux:   zig-out/lib/libghostty-vt.so
#   Windows: zig-out/bin/ghostty-vt.dll  (import lib: zig-out/lib/ghostty-vt.lib)
#   macOS:   zig-out/lib/libghostty-vt.dylib
```

Copy outputs to `$GHOSTTY/bin/linux/` or `$GHOSTTY/bin/windows/` as appropriate.
Copy `include/ghostty/` tree to `$GHOSTTY/include/ghostty/`.

### Build `src/_lib.cpp` → `vg.dll` / `libvg.so`

`_lib.cpp` wraps `ghostty/vt.h` and exposes a simplified C interface loaded by Python via ctypes. The `vg_*` functions are the only ghostty-vt calls allowed outside `_lib.cpp`.

```bash
# Linux — $ORIGIN rpath: libvg.so finds libghostty-vt.so next to itself, no LD_LIBRARY_PATH needed
g++ -std=c++20 -shared -fPIC src/_lib.cpp \
    -L$GHOSTTY/bin/linux -lghostty-vt \
    -I$GHOSTTY/include \
    -Wl,-rpath,'$ORIGIN' \
    -o $GHOSTTY/bin/linux/libvg.so

# Windows (MSVC) — DLLs search their own directory first, no rpath needed
# Note: must link vcruntime.lib + ucrt.lib explicitly when /LD is used standalone
# Run from a VS Developer Command Prompt (vcvars64.bat must be sourced first)
cl /LD /EHsc /O2 /std:c++20 src\_lib.cpp \
   /I %GHOSTTY%\include \
   /link %GHOSTTY%\bin\windows\ghostty-vt.lib vcruntime.lib ucrt.lib \
   /OUT:%GHOSTTY%\bin\windows\vg.dll
```

### Key ABI notes for pinned commit 0790937

The following differ from the initial BUILD_NOTES.md description (pre-1.0 ABI was updated):

- `ghostty_terminal_new(allocator*, terminal*, options)` — takes out-param, not a return value
- `ghostty_terminal_resize(term, cols, rows, cell_w_px, cell_h_px)` — 4 args (no total px dims)
- `ghostty_render_state_new(allocator*, state*)` — takes out-param
- `ghostty_key_encoder_new(allocator*, encoder*)` / `ghostty_mouse_encoder_new(allocator*, encoder*)`
- `GhosttyKeyEvent` and `GhosttyMouseEvent` are opaque handles — use `ghostty_key_event_new()` / setters / `ghostty_key_event_free()` (same pattern for mouse)
- `ghostty_key_encoder_encode(enc, ev, char*, size_t, size_t* written)` — takes `size_t*` out-param
- Scroll viewport enum: `GHOSTTY_SCROLL_VIEWPORT_TOP/BOTTOM/DELTA` (not `GHOSTTY_TERMINAL_SCROLL_VIEWPORT_*`)
- `GHOSTTY_INIT_SIZED` expands to `((type){ .size = sizeof(type) })` — C99 compound literal, not valid in MSVC C++; replace with `memset + .size = sizeof(type)` pattern
- `GhosttyTerminalOptions`: `{cols, rows, max_scrollback}` — no `.size` field, no `.columns` field
- `GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_WIDTH` does not exist; wide char detection not available via this path

### `_lib.cpp` API surface exposed to Python

```c
// Terminal
void* vg_terminal_new(int cols, int rows);
void  vg_terminal_free(void* term);
void  vg_terminal_write(void* term, const uint8_t* buf, size_t len);
void  vg_terminal_resize(void* term, int cols, int rows,
                          int width_px, int height_px,
                          int cell_w_px, int cell_h_px);

// Render state
void* vg_render_state_new(void);
void  vg_render_state_free(void* rs);
void  vg_render_state_update(void* rs, void* term);

// Cell struct returned by iterator
typedef struct {
    uint32_t codepoints[16];
    uint8_t  codepoint_count;
    uint8_t  col;            // x position (sequential index; set by caller in Python)
    uint8_t  fg_r, fg_g, fg_b;
    uint8_t  bg_r, bg_g, bg_b;
    uint8_t  has_bg;
    uint8_t  bold, italic, inverse, underline, strikethrough, faint;
    uint8_t  _pad;           // pad to even byte count (wide-char detection done in Python via unicodedata)
} VgCell;

typedef struct {
    uint8_t  r, g, b;
} VgColor;

typedef struct {
    VgColor  fg, bg, cursor;
    VgColor  palette[256];
} VgColors;

// Row/cell iteration
// Returns: row count (call vg_render_rows_get in a loop)
int  vg_render_row_count(void* rs);
int  vg_render_row_cells(void* rs, int row, VgCell* out_cells, int max_cells);
void vg_render_colors(void* rs, VgColors* out);
void vg_render_cursor(void* rs, int* out_x, int* out_y, int* out_visible);

// Key/mouse encoding
int  vg_key_encode(void* term,
                   int key_code,      // GhosttyKey enum value
                   int mods,          // GhosttyMods bitmask
                   const char* text,  // UTF-8 text or NULL
                   uint8_t* out_buf, int buf_len);

int  vg_mouse_encode(void* term,
                     int action,      // GhosttyMouseAction
                     int button,      // GhosttyMouseButton
                     float x, float y,
                     int mods,
                     uint8_t* out_buf, int buf_len);

// Scrollback
void vg_scroll(void* term, int delta);  // negative = up
void vg_scroll_top(void* term);
void vg_scroll_bottom(void* term);
void vg_scrollbar(void* term, uint64_t* out_total, uint64_t* out_offset, uint64_t* out_len);
```

---

## libghostty API Audit (`ghostty.h` — for reference only)

Not used by Void Ghostty. Documented here for completeness.

**4 opaque handles**: `ghostty_app_t`, `ghostty_config_t`, `ghostty_surface_t`, `ghostty_inspector_t`

**80+ functions** across init/lifecycle, config management, app lifecycle, surface lifecycle,
rendering, focus, keyboard input, mouse input, IME, split dispatch, clipboard, text selection.

**6 callbacks** (registered in `ghostty_runtime_config_s`):
- `wakeup_cb` → signal host to call `ghostty_app_tick()`
- `action_cb` → all 57 action types dispatch here (keybindings, UI events, terminal state changes)
- `read_clipboard_cb`, `confirm_read_clipboard_cb`, `write_clipboard_cb`
- `close_surface_cb`

**57 action types** include: NEW_TAB, CLOSE_TAB, GOTO_TAB, MOVE_TAB, NEW_SPLIT, GOTO_SPLIT,
RESIZE_SPLIT, EQUALIZE_SPLITS, TOGGLE_SPLIT_ZOOM, SET_TITLE, RELOAD_CONFIG, COLOR_CHANGE,
DESKTOP_NOTIFICATION, MOUSE_SHAPE, RENDERER_HEALTH, PROGRESS_REPORT, and many more.

Embedding flow (macOS only):
```
ghostty_init() → ghostty_config_new/load/finalize →
ghostty_app_new(callbacks) → ghostty_surface_new(app, {nsview=my_nsview}) →
[loop] ghostty_app_tick() + ghostty_surface_draw() →
[input] ghostty_surface_key() / ghostty_surface_mouse_*()
```

---

## Standalone Feature Audit

Features of the standalone Ghostty application and their status for Void Ghostty.

| Feature | Lives in | PySide6 difficulty | Void Ghostty scope |
|---|---|---|---|
| Tabs | App layer | Easy | Deferred (Phase 3+) |
| Splits/panes | App layer | Medium | QSplitter — not libghostty-vt splits |
| Config system | App layer | Medium | Minimal: GHOSTTY env var + ghostty config for key encoders |
| Keybindings | App layer | Hard | Qt QShortcut for now; key tables deferred |
| Themes/appearance | App layer | Medium | pyte palette in Phase 2-3; libghostty-vt palette in Phase 4 |
| Background blur | App layer | Hard | Out of scope — Houdini owns its window |
| Scrollback UI | Both | Easy | QScrollBar + viewport model |
| Text selection | Both | Easy | Mouse tracking + QPainter overlay |
| Clipboard / paste safety | App layer | Easy | QClipboard + bracketed paste + dangerous byte stripping |
| Font rendering | App layer | Hard | QFont (Phase 2-3); per-cell QFont in Phase 4; full atlas deferred |
| URL detection | App layer | Medium | Phase 6+ |
| OSC callbacks | Both | Medium | title, OSC 7, OSC 52, OSC 133 in Phase 5+ |
| Shell integration | App layer | Medium | OSC 133 semantic zones → jump_to_prompt |
| Search/find | App layer | Easy | Phase 6+ |
| Quick terminal | App layer | Medium | Out of scope — Houdini panel IS the terminal |
| Mouse reporting | Both | Easy | GhosttyMouseEncoder handles all formats |
| Notifications | App layer | Medium | Deferred |
| IME | App layer | Medium | QInputMethodEvent + preedit rendering |
| Window management | App layer | Easy | Out of scope — Houdini controls the window |
| Color management | Both | Medium | GhosttyRenderStateColors in Phase 4 |
| Secure input | App layer | Easy | Flag-gated clipboard |
| Renderer health | App layer | Easy | QElapsedTimer + dirty state |

### What carries over to Void Ghostty automatically (libghostty-vt handles it)

- Full VT sequence parsing (ANSI, xterm, Kitty, DEC private modes)
- Cell grid + scrollback (PageList)
- Color attribute resolution (palette indices → RGB)
- Style attributes (bold, italic, underline, inverse, faint, blink, strikethrough)
- Kitty keyboard protocol (key encoder syncs to terminal mode)
- All mouse encoding formats (X10, SGR, SGR-Pixels, etc.)
- OSC/SGR parsing infrastructure
- Paste safety (ghostty_paste_encode handles bracketed paste + byte stripping)
- Bracketed paste mode detection

### What Void Ghostty implements in Python

Everything above the VT state machine: PTY management, Qt rendering, clipboard integration,
pynvim RPC sync, Houdini session awareness, node registration, hook dispatch.

---

## Environment Setup

Set before launching Houdini:

```bash
# Linux
export GHOSTTY=/path/to/VoidMonolith/Ghostty

# Windows (System Properties > Advanced > Environment Variables)
GHOSTTY=D:\VoidMonolith\Ghostty
```

The Houdini package (`void-ghostty.json`) derives all paths from `$GHOSTTY`.
Copy or symlink `void-ghostty.json` to `$HOUDINI_USER_PREF_DIR/packages/`.

---

## Rendering Performance Research

Research conducted against Ghostling `main.c` at commit `bebca84668947bfc92b9a30ed58712e1c34eee1d` and Ghostty source at commit `0790937d03df6e7a9420c61de91ce520a85fe4ef`. Findings drive the Phase 4 Python rendering path in `_terminal.py`.

### R1 — Ghostling renders ALL rows every frame; no per-row dirty skip (main.c:1078–1176)

```c
while (ghostty_render_state_row_iterator_next(row_iter)) {
    // … render cells …
    bool clean = false;
    ghostty_render_state_row_set(row_iter,
        GHOSTTY_RENDER_STATE_ROW_OPTION_DIRTY, &clean);  // clear AFTER render
    y += cell_height;
}
```

Per-row dirty bits are cleared after rendering, not checked before. All rows are rendered unconditionally each frame.

**Decision for Void Ghostty**: Gate repaints on a Python-level `_vt_dirty` flag only. When dirty, render ALL rows (matching Ghostling). Per-row dirty clearing happens inside `vg_render_row_cells()` in C automatically.

### R2 — Wide/double-width characters: not in Ghostling, not in vg API (main.c:1087, 1165)

Ghostling draws all cells at fixed `cell_width` stride. `GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_WIDTH` does not exist at this commit (confirmed in ABI notes above).

**Decision**: Detect wide chars in Python via `unicodedata.east_asian_width(chr(cp))` → `'W'` or `'F'`. Draw wide cells at `2 * cell_w`. Nerd Font PUA codepoints (U+E000–U+F8FF) are single-width.

### R3 — Grapheme codepoints → UTF-8 string (main.c:1104–1119)

```c
for (uint32_t i = 0; i < len && pos < 60; i++) {
    char u8[4];
    int n = utf8_encode(codepoints[i], u8);
    memcpy(&text[pos], u8, n);
    pos += n;
}
```

Python equivalent used in `_paint_rows_vg`: `''.join(chr(cp) for cp in codepoints[:count])`

### R4 — Repaint rate: Ghostling unconditional 60 Hz (main.c:797)

`SetTargetFPS(60)` — no adaptive rate, no dirty check.

**Decision**: Use 30 Hz QTimer in Houdini gated on `_vt_dirty` to protect Houdini's Qt event loop from unnecessary repaints.

### R5 — Font atlas structure (src/font/Atlas.zig)

- **Storage**: Square power-of-2 texture (e.g. 1024×1024). Grayscale for alpha masks; BGRA for color emoji. 1-pixel border prevents GPU sampler bleed.
- **Packing**: Shelf packing (best-fit) from "A Thousand Ways to Pack the Bin" (Jylänki). Node list tracks shelf segments. Adjacent same-y nodes merged after each reservation.
- **Growth**: If `reserve(w,h)` returns AtlasFull, double texture size and re-upload. No eviction — `clear()` resets all nodes and zeroes data.
- **Cache**: Atlas has NO built-in glyph cache. Callers maintain `dict[(codepoint, face_idx, size_px, bold, italic) → Region(x,y,w,h)]`.
- **GPU sync**: `modified` atomic counter → `glTexSubImage2D` for updates; `resized` counter → `glTexImage2D` full re-upload.

**Decision**: Deferred to Phase 5. The `FontAtlas` class in `_terminal.py` is a stub with this design documented in its docstring.

### R6 — Cell metrics (src/font/Metrics.zig, src/font/face.zig)

- Cell width: `@round(face_width)` — rounded advance width of ASCII printable reference character.
- Cell height: `@round(lineHeight())` = ascent + descent + line gap.
- Baseline offset: used for underline / strikethrough positioning relative to cell top.

**Qt equivalent already in `_terminal.py`**: `QFontMetrics.horizontalAdvance('M')` for width, `QFontMetrics.height()` for height, `QFontMetrics.ascent()` for baseline — matches Ghostty's algorithm.

### R7 — Scrollback (src/terminal/Screen.zig)

PageList model with linked pages (bounded by `explicit_max_size`, unbounded if 0). Viewport is a union: `active | top | pin`. Pin-based tracking preserves positions across page rotations.

**Decision**: libghostty-vt handles all scrollback internally. Void Ghostty exposes it via `QScrollBar` wired to `vg_scroll()` / `vg_scrollbar()` — no re-implementation needed.

### R8 — Dirty flag propagation (src/terminal/Screen.zig)

Propagation chain: Cell write → `page_row.dirty = true` → Page dirty → Screen-level `dirty.selection` / `dirty.hyperlink_hover`. Python only needs the global `_vt_dirty` Python flag; per-row bits are cleared in C inside `vg_render_row_cells()`.

### Summary table

| Decision | Finding | Source |
|---|---|---|
| Render all rows, no per-row skip | Ghostling does not skip clean rows | main.c:1078–1176 |
| Wide chars: `unicodedata.east_asian_width` | No vg API for width, Ghostling ignores it | main.c:1087,1165 / BUILD_NOTES ABI notes |
| Grapheme → `''.join(chr(cp)...)` | Direct port of Ghostling's UTF-8 encode loop | main.c:1104–1119 |
| 30 Hz dirty-gated in Houdini | Ghostling uses 60 Hz unconditional | main.c:797 |
| FontAtlas deferred, stub in place | Shelf packing, no eviction, caller-managed keys | src/font/Atlas.zig |
| QFontMetrics already correct | Matches `@round(face_width)` / `lineHeight()` | src/font/Metrics.zig, face.zig |
| Scrollback via vg_scroll* only | PageList fully internal to libghostty-vt | src/terminal/Screen.zig |
| Python `_vt_dirty` gate sufficient | Per-row bits cleared in C by `vg_render_row_cells` | src/terminal/Screen.zig |
