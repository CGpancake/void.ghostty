"""Void Ghostty — Ghostty-compatible config reader.

Reads the same config file as native Ghostty so one file controls both apps.

Ghostty documented search order (Windows and Linux/macOS):
  1. $XDG_CONFIG_HOME/ghostty/config.ghostty   (if XDG_CONFIG_HOME is set)
  2. $XDG_CONFIG_HOME/ghostty/config
  3. $USERPROFILE/.config/ghostty/config.ghostty  (Windows default)
     ~/.config/ghostty/config.ghostty              (Linux/macOS default)
  4. Same path without .ghostty extension

Note: Ghostty does NOT use %APPDATA% on Windows — it follows XDG conventions
      with USERPROFILE\\.config as the fallback base directory.

Ghostty config format
---------------------
  font-family = JetBrains Mono
  font-size = 12
  theme = dark:Dracula+,light:catppuccin-latte
  font-family-2 = Symbols Nerd Font Mono
  keybind = ctrl+shift+h=split_right
  keybind = ctrl+shift+b=split_down

Void Ghostty reads the above keys.  All other keys are silently ignored
(they apply only to the standalone Ghostty window).
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Platform paths — mirrors Ghostty's XDG-based search order
# ---------------------------------------------------------------------------

def _ghostty_config_base() -> pathlib.Path:
    """Return the base config directory (~/.config equivalent) for this platform."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg:
        return pathlib.Path(xdg)
    if sys.platform == "win32":
        # Windows: USERPROFILE\.config  (NOT %APPDATA%)
        userprofile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        return pathlib.Path(userprofile) / ".config"
    # Linux / macOS
    return pathlib.Path(os.path.expanduser("~/.config"))


def _config_path() -> Optional[pathlib.Path]:
    """Return the first existing Ghostty config file, or None."""
    base = _ghostty_config_base() / "ghostty"
    for name in ("config.ghostty", "config"):
        p = base / name
        if p.exists():
            return p
    return None


def _themes_dir() -> pathlib.Path:
    return _ghostty_config_base() / "ghostty" / "themes"


# ---------------------------------------------------------------------------
# VgConfig dataclass
# ---------------------------------------------------------------------------

_KNOWN_ACTIONS = frozenset({
    "split_right", "split_down", "close_surface", "new_tab", "new_window",
})


class VgConfig:
    """Parsed Ghostty config values relevant to Void Ghostty."""
    __slots__ = ("font_family", "font_size", "theme", "font_fallbacks", "keybinds")

    def __init__(self) -> None:
        self.font_family: str = "Consolas" if sys.platform == "win32" else "Monospace"
        self.font_size: int = 10
        self.theme: str = "GruvboxDark"
        self.font_fallbacks: list[str] = []
        self.keybinds: dict[str, str] = {}  # action → "ctrl+shift+h" string


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_theme_name(raw: str) -> str:
    """Handle 'dark:ThemeName,light:ThemeName' or plain 'ThemeName'."""
    for part in raw.split(","):
        part = part.strip()
        if part.lower().startswith("dark:"):
            return part[5:].strip()
    # No dark: prefix — use value as-is
    return raw.strip()


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def load_config() -> VgConfig:
    """Read Ghostty config file.  Returns defaults when file is absent or unreadable."""
    cfg = VgConfig()
    p = _config_path()
    if p is None:
        return cfg
    try:
        for raw_line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().lower()
            val = val.strip()
            if not val:
                continue
            if key == "font-family":
                cfg.font_family = val.strip('"').strip("'")
            elif key == "font-size":
                try:
                    cfg.font_size = max(6, min(72, int(float(val))))
                except ValueError:
                    pass
            elif key == "theme":
                cfg.theme = _parse_theme_name(val)
            elif key == "font-family-2":
                name = val.strip('"').strip("'")
                if name not in cfg.font_fallbacks:
                    cfg.font_fallbacks.append(name)
            elif key == "keybind":
                # "ctrl+shift+h=split_right"
                if "=" in val:
                    combo, _, action = val.rpartition("=")
                    action = action.strip()
                    if action in _KNOWN_ACTIONS:
                        cfg.keybinds[action] = combo.strip()
    except Exception:
        pass
    return cfg


# ---------------------------------------------------------------------------
# find_ghostty_binary
# ---------------------------------------------------------------------------

def find_ghostty_binary() -> Optional[str]:
    """Return path to ghostty executable, or None if not found.

    Checks (in order):
    1. Standard PATH  (shutil.which)
    2. $GHOSTTY env var directory
    3. Common OS-specific install locations
    """
    # 1. PATH
    found = shutil.which("ghostty")
    if found:
        return found

    # 2. $GHOSTTY env var
    ghostty_env = os.environ.get("GHOSTTY", "")
    if ghostty_env:
        for candidate in (
            os.path.join(ghostty_env, "ghostty.exe"),
            os.path.join(ghostty_env, "bin", "ghostty"),
            os.path.join(ghostty_env, "ghostty"),
        ):
            if os.path.isfile(candidate):
                return candidate

    # 3. Common install paths
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Ghostty\ghostty.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ghostty\ghostty.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Ghostty\ghostty.exe"),
        ]
    else:
        candidates = [
            "/usr/local/bin/ghostty",
            "/opt/homebrew/bin/ghostty",
            "/usr/bin/ghostty",
        ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


# ---------------------------------------------------------------------------
# list_available_themes
# ---------------------------------------------------------------------------

def list_available_themes() -> list[str]:
    """Return sorted deduplicated list of all loadable theme names.

    Sources (all merged):
    1. BUNDLED_THEMES dict in _themes.py
    2. *.conf files in the user Ghostty themes directory
    3. Output of 'ghostty +list-themes' (if binary found)
    """
    names: set[str] = set()

    # 1. Bundled
    try:
        from void_ghostty._themes import BUNDLED_THEMES
        names.update(BUNDLED_THEMES.keys())
    except Exception:
        pass

    # 2. User themes dir
    td = _themes_dir()
    if td.is_dir():
        for p in td.glob("*.conf"):
            names.add(p.stem)

    # 3. Ghostty binary
    binary = find_ghostty_binary()
    if binary:
        try:
            result = subprocess.run(
                [binary, "+list-themes"],
                capture_output=True, text=True, timeout=3,
            )
            for line in result.stdout.splitlines():
                name = line.strip()
                if name:
                    names.add(name)
        except Exception:
            pass

    return sorted(names)
