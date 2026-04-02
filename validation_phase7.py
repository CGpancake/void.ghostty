"""Phase 7 validation — register() + _vg_config parse."""
import hou, void_ghostty

# Simulate an HDA that has called register()
geo = hou.node("/obj").createNode("geo", "vg_hook_test")
node = geo.createNode("attribwrangle", "vg_hook")

import json
config = {
    "hooks": {"on_cook": "echo cook"},
    "cook_trigger": "on_leave",
    "watch_parms": ["snippet"],
}
config_str = json.dumps(config)

node.addSpareParmTuple(
    hou.StringParmTemplate("_vg_config", "VG Config", 1,
                           default_value=[config_str])
)

void_ghostty.register(node)

reg = void_ghostty._registry.get(node.sessionId())
assert reg is not None, "node not registered"
assert reg.get("cook_trigger") == "on_leave", f"Wrong cook_trigger: {reg.get('cook_trigger')}"
assert reg.get("watch_parms") == ["snippet"], f"Wrong watch_parms: {reg.get('watch_parms')}"
print("register() + config parse: PASS")

# Test _hooks dispatch (no actual script file — just verifies graceful no-op)
from void_ghostty._hooks import dispatch, ON_COOK
dispatch(ON_COOK, node)  # Should log a warning and not crash
print("hooks dispatch (missing script): graceful no-op PASS")

geo.destroy()
print("Phase 7: PASS")
