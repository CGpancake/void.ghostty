"""Void Ghostty — terminal development environment embedded in Houdini."""

__version__ = "0.1.0"

_registry = {}


def register(node):
    """Register a Houdini node with Void Ghostty.

    Call from any HDA's OnCreated script:
        import void_ghostty
        void_ghostty.register(hou.pwd())

    Reads the optional '_vg_config' spare parameter (JSON) from the node
    and stores the parsed config keyed by node.sessionId().
    """
    import json

    try:
        parm = node.parm("_vg_config")
        config = json.loads(parm.eval()) if parm else {}
    except Exception:
        config = {}

    _registry[node.sessionId()] = config

    try:
        from void_ghostty._panel import get_panel
        panel = get_panel()
        if panel is not None:
            panel.on_node_registered(node, config)
    except Exception:
        pass
