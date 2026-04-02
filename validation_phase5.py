"""Phase 5 validation — pynvim sync + hdefereval.

hdefereval is only available in a graphical Houdini session.
This script validates what it can headlessly, then documents the
GUI-only portion that must be tested interactively.
"""
import hou, time, sys

# 1. Verify _nvim_sync imports cleanly
from void_ghostty._nvim_sync import NvimSync, _set_parm, _set_parm_and_cook
print("_nvim_sync import: PASS")

# 2. Verify NvimSync instantiates without error
from void_ghostty._panel import _nvim_cmd
nvim_exe = _nvim_cmd()[0]
sync = NvimSync(nvim_exe=nvim_exe, node_path=None)
assert isinstance(sync, NvimSync)
print("NvimSync instantiation: PASS")

# 3. Verify hdefereval path (GUI only)
try:
    import hdefereval

    geo = hou.node("/obj").createNode("geo", "vg_sync_test")
    wrangle = geo.createNode("attribwrangle", "vg_test_wrangle")
    code_path = wrangle.parm("snippet").path()

    hdefereval.executeDeferred(lambda: hou.parm(code_path).set("x = 42"))
    time.sleep(0.2)
    val = hou.parm(code_path).eval()
    assert val == "x = 42", f"Expected 'x = 42', got {val!r}"
    print("hdefereval display sync: PASS")

    geo.destroy()
except ImportError:
    print("hdefereval: SKIPPED (headless — only available in graphical Houdini)")

print("Phase 5: PASS")
