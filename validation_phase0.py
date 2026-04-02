import hou, void_ghostty, sys
print("version:", void_ghostty.__version__)
print("GHOSTTY:", hou.getenv("GHOSTTY"))
print("paths:", [p for p in sys.path if "Ghostty" in p])
assert void_ghostty.__version__ == "0.1.0"
assert hou.getenv("GHOSTTY") is not None, "GHOSTTY env var not set in package"
assert any("Ghostty" in p for p in sys.path), "Ghostty not in sys.path"
print("Phase 0: PASS")
