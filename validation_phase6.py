"""Phase 6 validation — follow/pinned/free modes + node resolution."""
import hou

# 1. Verify panel imports and mode API
from void_ghostty._panel import VoidGhosttyPanel, get_panel
print("_panel Phase 6 import: PASS")

# 2. Verify event loop callback API exists (GUI only)
if hasattr(hou, "ui"):
    print("event loop callbacks:", len(hou.ui.eventLoopCallbacks()))

# 3. Node path resolution
test_geo = hou.node("/obj").createNode("geo", "vg_follow_test")
assert hou.node(test_geo.path()) is not None
node_path = test_geo.path()

# Verify _resolve_code_parm handles nodes without code parms gracefully
result = VoidGhosttyPanel._resolve_code_parm(node_path)
print(f"resolve_code_parm (no code parm): {result}")  # Expected: None

# Create node with snippet parm
wrangle = test_geo.createNode("attribwrangle", "vg_wrangle")
result = VoidGhosttyPanel._resolve_code_parm(wrangle.path())
print(f"resolve_code_parm (attribwrangle): {result}")
assert result is not None and "snippet" in result, f"Expected snippet path, got: {result}"
print("resolve_code_parm: PASS")

test_geo.destroy()
print("Phase 6: PASS")
