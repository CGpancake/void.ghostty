import os, sys, hou

ghostty = hou.getenv("GHOSTTY") or os.environ.get("GHOSTTY", "")
sys.path.insert(0, os.path.join(ghostty, "python_deps"))

if os.name == "nt":
    import winpty
    pty = winpty.PtyProcess.spawn(["cmd.exe"])
    import time; time.sleep(0.5)
    data = pty.read(4096)
    pty.terminate()
    print("PTY bytes:", len(data))

    import pyte
    screen = pyte.Screen(80, 24)
    stream = pyte.ByteStream(screen)
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    stream.feed(data)
    non_empty = [l for l in screen.display if l.strip()]
    print("pyte non-empty lines:", len(non_empty))
    assert len(data) > 0, "No PTY output received"
    print("PTY + pyte test: PASS")
else:
    print("Linux pty + pyte: manual test in Houdini")

# Also confirm _terminal module imports cleanly
from void_ghostty._terminal import TerminalWidget, PtyProcess
print("_terminal import: PASS")
print("Phase 2: PASS")
