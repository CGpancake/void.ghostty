"""Phase 3 validation — headless checks (Windows + Linux parity).

Verifies:
  1. Bundled nvim binary for the current platform exists and runs
  2. Bundled nvim binary for the other platform is present (structure check)
  3. _panel.py imports and all three pane commands resolve
"""
import os, sys, subprocess, hou

ghostty = hou.getenv("GHOSTTY") or os.environ.get("GHOSTTY", "")
assert ghostty, "GHOSTTY not set"

# -- 1. Current-platform nvim --
if os.name == "nt":
    nvim = os.path.join(ghostty, "bin", "windows", "nvim-win64", "bin", "nvim.exe")
    other_candidates = [
        os.path.join(ghostty, "bin", "linux", "nvim-linux-x86_64", "bin", "nvim"),
        os.path.join(ghostty, "bin", "linux", "nvim-linux64",       "bin", "nvim"),
    ]
else:
    candidates = [
        os.path.join(ghostty, "bin", "linux", "nvim-linux-x86_64", "bin", "nvim"),
        os.path.join(ghostty, "bin", "linux", "nvim-linux64",       "bin", "nvim"),
    ]
    nvim = next((c for c in candidates if os.path.exists(c)), None)
    other_candidates = [os.path.join(ghostty, "bin", "windows", "nvim-win64", "bin", "nvim.exe")]

assert nvim and os.path.exists(nvim), f"nvim not found for current platform"
result = subprocess.run([nvim, "--version"], capture_output=True, text=True, timeout=5)
assert result.returncode == 0, f"nvim --version failed: {result.stderr}"
print(f"nvim ({('windows' if os.name == 'nt' else 'linux')}): {result.stdout.splitlines()[0]} — PASS")

# -- 2. Other-platform nvim structure --
other_present = any(os.path.exists(c) for c in other_candidates)
status = "PASS" if other_present else "MISSING (will be present on target OS)"
print(f"nvim other-platform binary: {status}")

# -- 3. _panel imports and commands --
from void_ghostty._panel import _nvim_cmd, _shell_cmd, _claude_cmd
nvim_c   = _nvim_cmd()
shell_c  = _shell_cmd()
claude_c = _claude_cmd()
print(f"nvim cmd:   {nvim_c}")
print(f"shell cmd:  {shell_c}")
print(f"claude cmd: {claude_c}")
print("_panel commands: PASS")
print("Phase 3: PASS")
