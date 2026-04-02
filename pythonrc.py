"""Void Ghostty — Houdini session init.

This file lives at $GHOSTTY/pythonrc.py.  Houdini discovers it because the
package JSON adds $GHOSTTY to HOUDINI_PATH via "path": "$GHOSTTY".

Keep this file to import and path setup ONLY.  No scene logic.
"""
import void_ghostty  # noqa: F401 — registers the package, nothing more
