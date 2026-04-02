"""Phase 1 validation — can run headless via hython OR in Houdini console.

The GUI check (hou.ui.pythonPanelInterfaces) only works inside a live
Houdini session.  The headless check verifies the .pypanel file is valid
XML and the panel module imports cleanly.
"""
import hou, os, sys, xml.etree.ElementTree as ET

ghostty = hou.getenv("GHOSTTY") or os.environ.get("GHOSTTY", "")
assert ghostty, "GHOSTTY not set"

# 1. Verify .pypanel file exists and is valid XML
pypanel_path = os.path.join(ghostty, "python_panels", "void_ghostty.pypanel")
assert os.path.exists(pypanel_path), f"Missing: {pypanel_path}"
tree = ET.parse(pypanel_path)
root = tree.getroot()
iface = root.find("interface")
assert iface is not None and iface.get("name") == "void_ghostty", \
    "interface element missing or wrong name"
print("pypanel XML valid: PASS")

# 2. Verify panel module imports without error
from void_ghostty._panel import onCreateInterface, VoidGhosttyPanel
print("_panel import: PASS")

# 3. GUI check (only available in a live Houdini session, skip in hython)
if hasattr(hou, "ui"):
    panels = [p.name() for p in hou.ui.pythonPanelInterfaces()]
    assert "void_ghostty" in panels, f"not registered, found: {panels}"
    print("Panel registration (GUI): PASS")
else:
    print("Panel registration (GUI): SKIPPED (headless hython — open Houdini to verify)")

print("Phase 1: PASS")
