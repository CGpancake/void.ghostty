"""Void Ghostty — theme system.

Theme resolution order (first hit wins)
-----------------------------------------
1. User Ghostty themes dir:
     Windows : %USERPROFILE%\.config\ghostty\themes\<name>.conf
               (or %XDG_CONFIG_HOME%\ghostty\themes\ if XDG_CONFIG_HOME is set)
     Linux   : ~/.config/ghostty/themes/<name>.conf
   Drop any .conf file from https://github.com/ghostty-org/ghostty/tree/main/src/config/themes
   there and it will be auto-detected.
2. BUNDLED_THEMES dict below (always available, no files needed).
3. Fallback to 'GruvboxDark'.

Theme dict keys
---------------
  bg, fg, fg_dim       — background, foreground, dimmed foreground (prompts/ghost text)
  cursor, selection     — cursor fill, selection highlight
  black…white           — ANSI 0-7 palette colors
  brightblack…brightwhite — ANSI 8-15 palette colors

All values are CSS hex strings (#rrggbb).
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# ANSI palette index → theme dict key
# ---------------------------------------------------------------------------

PALETTE_KEYS: list[str] = [
    "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "brightblack", "brightred", "brightgreen", "brightyellow",
    "brightblue", "brightmagenta", "brightcyan", "brightwhite",
]


# ---------------------------------------------------------------------------
# Bundled themes — names match Ghostty's canonical theme registry
# ---------------------------------------------------------------------------

BUNDLED_THEMES: dict[str, dict[str, str]] = {

    "GruvboxDark": {
        "bg": "#282828",        "fg": "#ebdbb2",        "fg_dim": "#a89984",
        "cursor": "#fabd2f",    "selection": "#504945",
        "black": "#282828",     "red": "#fb4934",       "green": "#b8bb26",
        "yellow": "#fabd2f",    "blue": "#83a598",      "magenta": "#d3869b",
        "cyan": "#8ec07c",      "white": "#ebdbb2",
        "brightblack": "#928374",   "brightred": "#fb4934",
        "brightgreen": "#b8bb26",   "brightyellow": "#fabd2f",
        "brightblue": "#83a598",    "brightmagenta": "#d3869b",
        "brightcyan": "#8ec07c",    "brightwhite": "#fbf1c7",
    },

    # Dracula+ — enhanced Dracula with vivid accents and pink cursor
    "Dracula+": {
        "bg": "#282a36",        "fg": "#f8f8f2",        "fg_dim": "#6272a4",
        "cursor": "#ff79c6",    "selection": "#44475a",
        "black": "#21222c",     "red": "#ff5555",       "green": "#50fa7b",
        "yellow": "#f1fa8c",    "blue": "#bd93f9",      "magenta": "#ff79c6",
        "cyan": "#8be9fd",      "white": "#f8f8f2",
        "brightblack": "#6272a4",   "brightred": "#ff6e6e",
        "brightgreen": "#69ff94",   "brightyellow": "#ffffa5",
        "brightblue": "#d6acff",    "brightmagenta": "#ff92df",
        "brightcyan": "#a4ffff",    "brightwhite": "#ffffff",
    },

    "Dracula": {
        "bg": "#282a36",        "fg": "#f8f8f2",        "fg_dim": "#6272a4",
        "cursor": "#f8f8f2",    "selection": "#44475a",
        "black": "#21222c",     "red": "#ff5555",       "green": "#50fa7b",
        "yellow": "#f1fa8c",    "blue": "#bd93f9",      "magenta": "#ff79c6",
        "cyan": "#8be9fd",      "white": "#f8f8f2",
        "brightblack": "#6272a4",   "brightred": "#ff6e6e",
        "brightgreen": "#69ff94",   "brightyellow": "#ffffa5",
        "brightblue": "#d6acff",    "brightmagenta": "#ff92df",
        "brightcyan": "#a4ffff",    "brightwhite": "#ffffff",
    },

    "catppuccin-mocha": {
        "bg": "#1e1e2e",        "fg": "#cdd6f4",        "fg_dim": "#585b70",
        "cursor": "#f5e0dc",    "selection": "#313244",
        "black": "#45475a",     "red": "#f38ba8",       "green": "#a6e3a1",
        "yellow": "#f9e2af",    "blue": "#89b4fa",      "magenta": "#f5c2e7",
        "cyan": "#94e2d5",      "white": "#bac2de",
        "brightblack": "#585b70",   "brightred": "#f38ba8",
        "brightgreen": "#a6e3a1",   "brightyellow": "#f9e2af",
        "brightblue": "#89b4fa",    "brightmagenta": "#f5c2e7",
        "brightcyan": "#94e2d5",    "brightwhite": "#a6adc8",
    },

    "tokyonight-storm": {
        "bg": "#24283b",        "fg": "#c0caf5",        "fg_dim": "#565f89",
        "cursor": "#bb9af7",    "selection": "#364a82",
        "black": "#1d202f",     "red": "#f7768e",       "green": "#9ece6a",
        "yellow": "#e0af68",    "blue": "#7aa2f7",      "magenta": "#bb9af7",
        "cyan": "#7dcfff",      "white": "#a9b1d6",
        "brightblack": "#414868",   "brightred": "#f7768e",
        "brightgreen": "#9ece6a",   "brightyellow": "#e0af68",
        "brightblue": "#7aa2f7",    "brightmagenta": "#bb9af7",
        "brightcyan": "#7dcfff",    "brightwhite": "#c0caf5",
    },

    "One Dark": {
        "bg": "#282c34",        "fg": "#abb2bf",        "fg_dim": "#5c6370",
        "cursor": "#528bff",    "selection": "#3e4451",
        "black": "#282c34",     "red": "#e06c75",       "green": "#98c379",
        "yellow": "#e5c07b",    "blue": "#61afef",      "magenta": "#c678dd",
        "cyan": "#56b6c2",      "white": "#abb2bf",
        "brightblack": "#5c6370",   "brightred": "#e06c75",
        "brightgreen": "#98c379",   "brightyellow": "#e5c07b",
        "brightblue": "#61afef",    "brightmagenta": "#c678dd",
        "brightcyan": "#56b6c2",    "brightwhite": "#ffffff",
    },
}


# ---------------------------------------------------------------------------
# Theme file parser — reads Ghostty .conf format
# ---------------------------------------------------------------------------

def _themes_dir() -> pathlib.Path:
    """Return the Ghostty user themes directory (same base logic as _config.py)."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg:
        base = pathlib.Path(xdg)
    elif sys.platform == "win32":
        userprofile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        base = pathlib.Path(userprofile) / ".config"
    else:
        base = pathlib.Path(os.path.expanduser("~/.config"))
    return base / "ghostty" / "themes"


def _hex(s: str) -> str:
    s = s.strip()
    return s if s.startswith("#") else "#" + s


def parse_theme_file(path: pathlib.Path) -> Optional[dict[str, str]]:
    """Parse a Ghostty .conf theme file into a theme dict.

    Ghostty theme format (lines are key = value):
        background = #1e1e2e
        foreground = #cdd6f4
        cursor-color = #f5e0dc
        selection-background = #313244
        palette = 0=#45475a
        ...
        palette = 15=#a6adc8
    """
    theme: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().lower()
            val = val.strip()
            if not val:
                continue

            if key == "background":
                theme["bg"] = _hex(val)
            elif key == "foreground":
                theme["fg"] = _hex(val)
            elif key == "cursor-color":
                theme["cursor"] = _hex(val)
            elif key == "selection-background":
                theme["selection"] = _hex(val)
            elif key == "palette":
                # val is "N=COLOR" e.g. "0=#45475a"
                if "=" in val:
                    idx_s, _, color = val.partition("=")
                    try:
                        idx = int(idx_s.strip())
                        if 0 <= idx <= 15:
                            theme[PALETTE_KEYS[idx]] = _hex(color.strip())
                    except ValueError:
                        pass
    except Exception:
        return None

    if "bg" not in theme or "fg" not in theme:
        return None  # not a valid theme file

    # Fill derived keys if absent
    if "fg_dim" not in theme:
        theme["fg_dim"] = theme.get("brightblack", theme["fg"])
    if "cursor" not in theme:
        theme["cursor"] = theme["fg"]
    if "selection" not in theme:
        theme["selection"] = theme["bg"]

    return theme


# ---------------------------------------------------------------------------
# load_theme
# ---------------------------------------------------------------------------

def load_theme(name: str) -> dict[str, str]:
    """Load theme by name.  Priority: user dir → bundled → GruvboxDark fallback."""
    # 1. User themes directory
    td = _themes_dir()
    conf = td / (name + ".conf")
    if conf.exists():
        parsed = parse_theme_file(conf)
        if parsed:
            return parsed

    # 2. Bundled (exact name)
    if name in BUNDLED_THEMES:
        return BUNDLED_THEMES[name]

    # 3. Bundled (case-insensitive)
    nl = name.lower()
    for k, v in BUNDLED_THEMES.items():
        if k.lower() == nl:
            return v

    # 4. Fallback
    return BUNDLED_THEMES["GruvboxDark"]
