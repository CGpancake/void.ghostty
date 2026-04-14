# Void Ghostty

A terminal development environment embedded inside Houdini's pane tab system.
Powered by [libghostty-vt](https://github.com/ghostty-org/ghostty) — the same VT state machine
that drives the standalone Ghostty terminal.

---

## Pane types

| Pane | Description |
|---|---|
| **Python REPL** | In-process Houdini Python — full `hou.*` access, no subprocess |
| **Shell terminal** | libghostty-vt PTY — runs cmd.exe / bash / any installed shell |

---

## Keyboard shortcuts

All shortcuts work from both the Python REPL and the shell terminal pane.
You can override any of them in your Ghostty config (see [Configuration](#configuration)).

| Shortcut | Action |
|---|---|
| `Ctrl+Shift+H` | Split current pane **right** (new Python REPL) |
| `Ctrl+Shift+B` | Split current pane **below** (new Python REPL) |
| `Ctrl+Shift+X` | **Close** current pane (last pane is never closed) |
| `Ctrl+Shift+T` | **Replace** current pane with a shell terminal |
| `Ctrl+Shift+P` | **Replace** current pane with a Python REPL |
| `Ctrl+Shift+C` | Copy selection to clipboard |

### Python REPL only

| Key | Action |
|---|---|
| `Tab` | **Tab completion** — single match inserts in-place; multiple shows list |
| `→` (Right arrow) | **Accept ghost suggestion** when dimmed text is shown |
| `↑` / `↓` | Navigate command history |
| `Home` | Jump to start of input (after prompt) |
| `Ctrl+C` | Interrupt running code (raises KeyboardInterrupt) |
| `Ctrl+L` | Clear screen |

### Shell terminal only

| Shortcut | Action |
|---|---|
| `Ctrl+Shift+C` | Copy selected text |
| Right-click | Paste clipboard |
| Mouse drag | Select text |
| Scroll wheel | Scroll through scrollback |

---

## Python REPL features

### Tab completion
Type a partial expression and press `Tab`:
```python
>>> hou.         # Tab → lists all hou.* attributes
>>> hou.node('/obj').  # Tab → lists node methods
>>> import os; os.path.  # Tab → lists os.path.* functions
```
Single match: completion inserted in-place.  
Multiple matches: completion list printed below, common prefix inserted if any.

### Ghost text (inline autosuggestion)
As you type, a **dimmed suggestion** appears to the right of the cursor.

- **Source 1 — History**: most recent history entry that starts with what you've typed.
  Run `hou.node('/obj/geo1')` once, then type `hou.` → the full call appears as a suggestion.
- **Source 2 — Completion**: first rlcompleter attribute match when no history match exists.

Press `→` (Right arrow) to accept. Any other key dismisses and continues typing.

### Syntax highlighting
Python keywords, strings, numbers, and comments are colored as you type:

| Token | Color |
|---|---|
| `if`, `for`, `def`, `class`, … | Yellow |
| `"strings"`, `'strings'` | Green |
| `42`, `3.14` | Magenta |
| `# comments` | Dim |

### Pre-imported names
```python
hou            # Houdini Python API — full scene access
void_ghostty   # VoidGhostty module
open_shell()   # Open a shell pane alongside the REPL
list_themes()  # Print available theme names
```

---

## Configuration

Void Ghostty reads the **same config file as native Ghostty** — no separate config needed.

| Platform | Config file location |
|---|---|
| Windows | `%USERPROFILE%\.config\ghostty\config` (or `config.ghostty`) |
| Linux / macOS | `~/.config/ghostty/config` (or `config.ghostty`) |
| Either (override) | `%XDG_CONFIG_HOME%\ghostty\config` if `XDG_CONFIG_HOME` is set |

Ghostty uses XDG conventions on all platforms — it does **not** use `%APPDATA%` on Windows.

Create the file if it doesn't exist. It's plain text, one setting per line:

```ini
# Font
font-family = JetBrainsMono Nerd Font Mono
font-size = 12

# Theme (see list_themes() for available names)
theme = Dracula+

# Font fallback — used when primary font lacks a glyph (Nerd Font icons etc.)
font-family-2 = Symbols Nerd Font Mono

# Keybindings (override defaults)
keybind = ctrl+shift+h=split_right
keybind = ctrl+shift+b=split_down
keybind = ctrl+shift+x=close_surface
```

Changes take effect on the next Houdini launch (config is read at import time).

### Dark/light theme syntax
Ghostty supports per-mode themes:
```ini
theme = dark:Dracula+,light:catppuccin-latte
```
Void Ghostty uses the `dark:` variant (or the plain value if no colon).

### Overriding keybindings
Any `keybind = combo=action` line overrides the default for that action.
Supported actions:

| Action | Default |
|---|---|
| `split_right` | `Ctrl+Shift+H` |
| `split_down` | `Ctrl+Shift+B` |
| `close_surface` | `Ctrl+Shift+X` |

The `new_shell` and `new_python` actions (`Ctrl+Shift+T` / `Ctrl+Shift+P`) are
VoidGhostty-specific and not in Ghostty's action list — they use the defaults above and
cannot be remapped via the config currently.

---

## Themes

### Bundled themes (always available, no files needed)

| Name | Style |
|---|---|
| `GruvboxDark` | Warm browns and yellows — **default** |
| `Dracula+` | Enhanced Dracula: vivid accents, pink cursor |
| `Dracula` | Classic Dracula |
| `catppuccin-mocha` | Soft blue-purples (Catppuccin Mocha) |
| `tokyonight-storm` | Cool blue Tokyo Night Storm |
| `One Dark` | Atom One Dark |

### Using any Ghostty theme
1. Download a `.conf` theme file from
   [ghostty-org/ghostty themes](https://github.com/ghostty-org/ghostty/tree/main/src/config/themes)
2. Drop it in `%USERPROFILE%\.config\ghostty\themes\` (Windows) or `~/.config/ghostty/themes/` (Linux)
3. Set `theme = ThemeName` (filename without `.conf`) in your config
4. Restart Houdini

If the Ghostty binary is in your PATH, `list_themes()` also includes all themes it knows about.

### See available themes from the REPL
```python
>>> list_themes()
6 themes available:
  Dracula     Dracula+    GruvboxDark  One Dark
  catppuccin-mocha        tokyonight-storm
```

---

## Nerd Fonts

Nerd Fonts add thousands of icons (file type glyphs, Git status, powerline separators)
used by tools like nvim statuslines, starship, lf, and yazi.

### Installation

**Windows:**
1. Download a Nerd Font from [nerdfonts.com](https://www.nerdfonts.com/font-downloads)
   — recommended: **JetBrainsMono Nerd Font** or **CascadiaCode Nerd Font**
2. Extract the `.zip` and install the font files (right-click → Install for all users)
3. Set in config:
   ```ini
   font-family = JetBrainsMono Nerd Font Mono
   ```
   Use the `Mono` variant — it has single-width icon glyphs, which renders better in
   a fixed-pitch terminal grid.

**Linux:**
```bash
# Using nerd-fonts installer
git clone --depth=1 https://github.com/ryanoasis/nerd-fonts ~/.nerd-fonts
~/.nerd-fonts/install.sh JetBrainsMono
```
Or install via your distro's package manager (`fonts-jetbrains-mono-nerd-fonts` on Debian/Ubuntu).

### Font fallback (Symbols Nerd Font)
If you want to keep your current font but still get Nerd Font icons:
```ini
font-family = JetBrains Mono          # your regular font
font-family-2 = Symbols Nerd Font Mono  # icons only
```
VoidGhostty automatically tries `Symbols Nerd Font Mono` as a last-resort fallback
even without `font-family-2` — if it's installed, icons will appear.

### Wide icon glyphs
Some Nerd Font icons (folder icons, file-type icons) are designed to be 2 cells wide.
VoidGhostty recognizes Nerd Fonts v3 double-width PUA ranges and renders them at 2× width
automatically.

---

## Shell setup (Linux / macOS)

When running on Linux, open a shell pane (`Ctrl+Shift+T`) and use any of these shells:

### fish (recommended — has built-in autosuggestions)
```bash
# Install fish
sudo apt install fish          # Debian/Ubuntu
brew install fish              # macOS

# Set as default shell
chsh -s $(which fish)

# Fish gives you: inline ghost suggestions, tab completion, syntax highlighting
# all at the shell level — no additional config needed
```

### zsh with autosuggestions
```bash
brew install zsh-autosuggestions
echo "source /opt/homebrew/share/zsh-autosuggestions/zsh-autosuggestions.zsh" >> ~/.zshrc
```

### PowerShell (Windows — autosuggestions built-in via PSReadLine)
PSReadLine ships with PowerShell 5.1+ and provides fish-style inline predictions.
Enable in your PowerShell profile:
```powershell
Set-PSReadLineOption -PredictionSource History
Set-PSReadLineOption -PredictionViewStyle InlineView
```

---

## Environment variables

Void Ghostty uses one required env var:

| Variable | Purpose |
|---|---|
| `GHOSTTY` | Path to the Ghostty installation directory (where `bin/windows/vg.dll` lives) |

Set this before launching Houdini. Example (Windows):
```bat
set GHOSTTY=D:\VoidMonolith\Ghostty
houdini.exe
```

---

## Adding custom tools (yazi, nvim, lazygit)

Any tool installed and in your `PATH` works in the shell pane — it's a real PTY.
On Windows, tools that run in WSL will work if you open a WSL shell first:
```
Ctrl+Shift+T  →  type: wsl  →  Enter
# Now in WSL — fish, nvim, yazi, lazygit all work
```

On Linux they work directly once installed.
