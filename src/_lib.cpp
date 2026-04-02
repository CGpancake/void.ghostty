/**
 * _lib.cpp — Void Ghostty ABI isolation layer
 *
 * ALL calls to the libghostty-vt C API are made here and ONLY here.
 * This is the only file that changes when the libghostty-vt ABI updates.
 *
 * Verified against headers at: ghostty-org/ghostty@0790937
 *
 * Key API differences from BUILD_NOTES.md (pre-1.0 ABI):
 *   - ghostty_terminal_new(allocator*, terminal*, options)  [not (options)->handle]
 *   - ghostty_terminal_resize(term, cols, rows, cw_px, ch_px)  [5 args, no total px]
 *   - ghostty_render_state_new(allocator*, state*)          [not ()->handle]
 *   - ghostty_key_encoder_new(allocator*, encoder*)
 *   - ghostty_mouse_encoder_new(allocator*, encoder*)
 *   - ghostty_key_encoder_encode(enc, ev, char*, size, size_t*)
 *   - GHOSTTY_SCROLL_VIEWPORT_* (not GHOSTTY_TERMINAL_SCROLL_VIEWPORT_*)
 *   - No GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_WIDTH enum
 *   - GhosttyTerminalOptions: {cols, rows, max_scrollback}  [no .size field]
 *
 * Build:
 *   Windows (MSVC):
 *     cl /LD /EHsc /O2 /std:c++20 src\_lib.cpp /I include
 *        /link bin\windows\ghostty-vt.lib /OUT:bin\windows\vg.dll
 *   Linux:
 *     g++ -std=c++20 -shared -fPIC src/_lib.cpp -Lbin/linux -lghostty-vt -Iinclude
 *         -Wl,-rpath,'$ORIGIN' -o bin/linux/libvg.so
 */

#include "ghostty/vt.h"
#include <cstdint>
#include <cstring>
#include <cstdlib>

#ifdef _WIN32
#  define VG_EXPORT __declspec(dllexport)
#else
#  define VG_EXPORT __attribute__((visibility("default")))
#endif

extern "C" {

/* -------------------------------------------------------------------------
 * Cell and color structures (shared ABI with Python ctypes).
 * Must match the ctypes definitions in _vg_ctypes.py exactly.
 * ---------------------------------------------------------------------- */

typedef struct {
    uint32_t codepoints[16];
    uint8_t  codepoint_count;
    uint8_t  col;
    uint8_t  fg_r, fg_g, fg_b;
    uint8_t  bg_r, bg_g, bg_b;
    uint8_t  has_bg;
    uint8_t  bold, italic, inverse, underline, strikethrough, faint;
    uint8_t  _pad;  /* pad to even byte count */
} VgCell;

typedef struct { uint8_t r, g, b; } VgColor;

typedef struct {
    VgColor fg, bg, cursor;
    VgColor palette[256];
} VgColors;

/* -------------------------------------------------------------------------
 * Terminal lifecycle
 * ---------------------------------------------------------------------- */

VG_EXPORT void* vg_terminal_new(int cols, int rows) {
    GhosttyTerminalOptions opts;
    opts.cols          = (uint16_t)cols;
    opts.rows          = (uint16_t)rows;
    opts.max_scrollback = 10000;

    GhosttyTerminal term = NULL;
    GhosttyResult r = ghostty_terminal_new(NULL, &term, opts);
    return (r == GHOSTTY_SUCCESS) ? (void*)term : NULL;
}

VG_EXPORT void vg_terminal_free(void* term) {
    if (term) ghostty_terminal_free((GhosttyTerminal)term);
}

VG_EXPORT void vg_terminal_write(void* term, const uint8_t* buf, size_t len) {
    if (term && buf && len)
        ghostty_terminal_vt_write((GhosttyTerminal)term, buf, len);
}

VG_EXPORT void vg_terminal_resize(void* term,
                                   int cols, int rows,
                                   int /*width_px*/, int /*height_px*/,
                                   int cell_w_px, int cell_h_px)
{
    /* API: ghostty_terminal_resize(term, cols, rows, cell_w_px, cell_h_px) */
    if (term)
        ghostty_terminal_resize((GhosttyTerminal)term,
                                (uint16_t)cols, (uint16_t)rows,
                                (uint32_t)cell_w_px, (uint32_t)cell_h_px);
}

/* -------------------------------------------------------------------------
 * Render state
 * ---------------------------------------------------------------------- */

VG_EXPORT void* vg_render_state_new(void) {
    GhosttyRenderState rs = NULL;
    GhosttyResult r = ghostty_render_state_new(NULL, &rs);
    return (r == GHOSTTY_SUCCESS) ? (void*)rs : NULL;
}

VG_EXPORT void vg_render_state_free(void* rs) {
    if (rs) ghostty_render_state_free((GhosttyRenderState)rs);
}

VG_EXPORT void vg_render_state_update(void* rs, void* term) {
    if (rs && term)
        ghostty_render_state_update((GhosttyRenderState)rs, (GhosttyTerminal)term);
}

/* -------------------------------------------------------------------------
 * Row / cell iteration
 * ---------------------------------------------------------------------- */

VG_EXPORT int vg_render_row_count(void* rs) {
    if (!rs) return 0;
    GhosttyRenderStateRowIterator row_iter = NULL;
    if (ghostty_render_state_get((GhosttyRenderState)rs,
            GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR, &row_iter) != GHOSTTY_SUCCESS)
        return 0;
    int count = 0;
    while (ghostty_render_state_row_iterator_next(row_iter)) count++;
    return count;
}

VG_EXPORT int vg_render_row_cells(void* rs, int row_index,
                                   VgCell* out_cells, int max_cells)
{
    if (!rs || !out_cells || max_cells <= 0) return 0;

    GhosttyRenderStateRowIterator row_iter = NULL;
    if (ghostty_render_state_get((GhosttyRenderState)rs,
            GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR, &row_iter) != GHOSTTY_SUCCESS)
        return 0;

    int cur = 0;
    while (ghostty_render_state_row_iterator_next(row_iter)) {
        if (cur == row_index) break;
        cur++;
    }

    GhosttyRenderStateRowCells cells = NULL;
    if (ghostty_render_state_row_get(row_iter,
            GHOSTTY_RENDER_STATE_ROW_DATA_CELLS, &cells) != GHOSTTY_SUCCESS)
        return 0;

    int n = 0;
    while (n < max_cells && ghostty_render_state_row_cells_next(cells)) {
        VgCell* c = &out_cells[n];
        memset(c, 0, sizeof(*c));

        uint32_t glen = 0;
        ghostty_render_state_row_cells_get(cells,
            GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_LEN, &glen);
        c->codepoint_count = (uint8_t)(glen < 16 ? glen : 16);

        if (glen > 0) {
            ghostty_render_state_row_cells_get(cells,
                GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_BUF,
                c->codepoints);
        }

        GhosttyColorRgb fg = {0, 0, 0};
        ghostty_render_state_row_cells_get(cells,
            GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_FG_COLOR, &fg);
        c->fg_r = fg.r; c->fg_g = fg.g; c->fg_b = fg.b;

        GhosttyColorRgb bg = {0, 0, 0};
        c->has_bg = (ghostty_render_state_row_cells_get(cells,
            GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_BG_COLOR, &bg) == GHOSTTY_SUCCESS)
            ? 1 : 0;
        c->bg_r = bg.r; c->bg_g = bg.g; c->bg_b = bg.b;

        GhosttyStyle style;
        memset(&style, 0, sizeof(style));
        style.size = sizeof(style);
        ghostty_render_state_row_cells_get(cells,
            GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_STYLE, &style);
        c->bold          = style.bold          ? 1 : 0;
        c->italic        = style.italic        ? 1 : 0;
        c->inverse       = style.inverse       ? 1 : 0;
        c->underline     = style.underline     ? 1 : 0;
        c->strikethrough = style.strikethrough ? 1 : 0;
        c->faint         = style.faint         ? 1 : 0;

        n++;
    }

    bool clean = false;
    ghostty_render_state_row_set(row_iter,
        GHOSTTY_RENDER_STATE_ROW_OPTION_DIRTY, &clean);

    return n;
}

VG_EXPORT void vg_render_colors(void* rs, VgColors* out) {
    if (!rs || !out) return;
    memset(out, 0, sizeof(*out));

    GhosttyRenderStateColors colors;
    memset(&colors, 0, sizeof(colors));
    colors.size = sizeof(colors);
    if (ghostty_render_state_colors_get((GhosttyRenderState)rs, &colors)
            != GHOSTTY_SUCCESS) return;

    out->fg.r = colors.foreground.r;
    out->fg.g = colors.foreground.g;
    out->fg.b = colors.foreground.b;
    out->bg.r = colors.background.r;
    out->bg.g = colors.background.g;
    out->bg.b = colors.background.b;
    out->cursor.r = colors.cursor.r;
    out->cursor.g = colors.cursor.g;
    out->cursor.b = colors.cursor.b;

    for (int i = 0; i < 256; i++) {
        out->palette[i].r = colors.palette[i].r;
        out->palette[i].g = colors.palette[i].g;
        out->palette[i].b = colors.palette[i].b;
    }
}

VG_EXPORT void vg_render_cursor(void* rs,
                                 int* out_x, int* out_y, int* out_visible)
{
    if (!rs) return;
    bool visible = false, in_vp = false;
    uint16_t cx = 0, cy = 0;
    ghostty_render_state_get((GhosttyRenderState)rs,
        GHOSTTY_RENDER_STATE_DATA_CURSOR_VISIBLE, &visible);
    ghostty_render_state_get((GhosttyRenderState)rs,
        GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_HAS_VALUE, &in_vp);
    ghostty_render_state_get((GhosttyRenderState)rs,
        GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_X, &cx);
    ghostty_render_state_get((GhosttyRenderState)rs,
        GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_Y, &cy);

    if (out_x)       *out_x       = (int)cx;
    if (out_y)       *out_y       = (int)cy;
    if (out_visible) *out_visible = (visible && in_vp) ? 1 : 0;
}

/* -------------------------------------------------------------------------
 * Key encoding
 * GhosttyKeyEvent is an opaque handle — use new/setters/free.
 * ghostty_key_encoder_encode(enc, ev, char*, size_t, size_t*) -> GhosttyResult
 * ---------------------------------------------------------------------- */

VG_EXPORT int vg_key_encode(void* term,
                             int key_code, int mods,
                             const char* text,
                             uint8_t* out_buf, int buf_len)
{
    if (!term || !out_buf || buf_len <= 0) return -1;

    GhosttyKeyEncoder ke = NULL;
    if (ghostty_key_encoder_new(NULL, &ke) != GHOSTTY_SUCCESS) return -1;
    ghostty_key_encoder_setopt_from_terminal(ke, (GhosttyTerminal)term);

    GhosttyKeyEvent ev = NULL;
    if (ghostty_key_event_new(NULL, &ev) != GHOSTTY_SUCCESS) {
        ghostty_key_encoder_free(ke);
        return -1;
    }
    ghostty_key_event_set_action(ev, GHOSTTY_KEY_ACTION_PRESS);
    ghostty_key_event_set_key(ev, (GhosttyKey)key_code);
    ghostty_key_event_set_mods(ev, (GhosttyMods)mods);
    if (text && text[0])
        ghostty_key_event_set_utf8(ev, text, strlen(text));

    size_t written = 0;
    GhosttyResult r = ghostty_key_encoder_encode(
        ke, ev, (char*)out_buf, (size_t)buf_len, &written);

    ghostty_key_event_free(ev);
    ghostty_key_encoder_free(ke);

    return (r == GHOSTTY_SUCCESS) ? (int)written : (int)r;
}

/* -------------------------------------------------------------------------
 * Mouse encoding
 * GhosttyMouseEvent is an opaque handle — use new/setters/free.
 * ---------------------------------------------------------------------- */

VG_EXPORT int vg_mouse_encode(void* term,
                               int action, int button,
                               float x, float y, int mods,
                               uint8_t* out_buf, int buf_len)
{
    if (!term || !out_buf || buf_len <= 0) return -1;

    GhosttyMouseEncoder me = NULL;
    if (ghostty_mouse_encoder_new(NULL, &me) != GHOSTTY_SUCCESS) return -1;
    ghostty_mouse_encoder_setopt_from_terminal(me, (GhosttyTerminal)term);

    GhosttyMouseEvent ev = NULL;
    if (ghostty_mouse_event_new(NULL, &ev) != GHOSTTY_SUCCESS) {
        ghostty_mouse_encoder_free(me);
        return -1;
    }
    ghostty_mouse_event_set_action(ev, (GhosttyMouseAction)action);
    ghostty_mouse_event_set_button(ev, (GhosttyMouseButton)button);
    ghostty_mouse_event_set_mods(ev, (GhosttyMods)mods);
    GhosttyMousePosition pos = { x, y };
    ghostty_mouse_event_set_position(ev, pos);

    size_t written = 0;
    GhosttyResult r = ghostty_mouse_encoder_encode(
        me, ev, (char*)out_buf, (size_t)buf_len, &written);

    ghostty_mouse_event_free(ev);
    ghostty_mouse_encoder_free(me);

    return (r == GHOSTTY_SUCCESS) ? (int)written : (int)r;
}

/* -------------------------------------------------------------------------
 * Scrollback
 * Enum names at this commit: GHOSTTY_SCROLL_VIEWPORT_TOP/BOTTOM/DELTA
 * ---------------------------------------------------------------------- */

VG_EXPORT void vg_scroll(void* term, int delta) {
    if (!term) return;
    GhosttyTerminalScrollViewport sv;
    memset(&sv, 0, sizeof(sv));
    sv.tag         = GHOSTTY_SCROLL_VIEWPORT_DELTA;
    sv.value.delta = (intptr_t)delta;
    ghostty_terminal_scroll_viewport((GhosttyTerminal)term, sv);
}

VG_EXPORT void vg_scroll_top(void* term) {
    if (!term) return;
    GhosttyTerminalScrollViewport sv;
    memset(&sv, 0, sizeof(sv));
    sv.tag = GHOSTTY_SCROLL_VIEWPORT_TOP;
    ghostty_terminal_scroll_viewport((GhosttyTerminal)term, sv);
}

VG_EXPORT void vg_scroll_bottom(void* term) {
    if (!term) return;
    GhosttyTerminalScrollViewport sv;
    memset(&sv, 0, sizeof(sv));
    sv.tag = GHOSTTY_SCROLL_VIEWPORT_BOTTOM;
    ghostty_terminal_scroll_viewport((GhosttyTerminal)term, sv);
}

VG_EXPORT void vg_scrollbar(void* term,
                             uint64_t* out_total,
                             uint64_t* out_offset,
                             uint64_t* out_len)
{
    if (!term) return;
    GhosttyTerminalScrollbar sb;
    memset(&sb, 0, sizeof(sb));
    ghostty_terminal_get((GhosttyTerminal)term,
        GHOSTTY_TERMINAL_DATA_SCROLLBAR, &sb);
    if (out_total)  *out_total  = sb.total;
    if (out_offset) *out_offset = sb.offset;
    if (out_len)    *out_len    = sb.len;
}

} /* extern "C" */
