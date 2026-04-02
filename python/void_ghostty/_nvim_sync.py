"""Void Ghostty — pynvim RPC sync thread (Phase 5).

Architecture:
  - NvimSync is a background thread that attaches to the Neovim process
    running inside the Neovim TerminalWidget via --embed mode.
  - TextChanged / TextChangedI → debounce timer (~30 Hz) →
      hdefereval.executeDeferred(lambda: hou.parm(code_path).set(text))
  - BufWritePost → set parm + node.cook(force=True) via hdefereval
  - BufLeave     → same as BufWritePost
  - Focus gain   → push current parm value into Neovim buffer

Every hou.* call goes through hdefereval.executeDeferred() without
exception — including parm.set(), not just cook triggers.

Usage (from VoidGhosttyPanel):
    sync = NvimSync(nvim_exe, node_path="obj/geo1/attribwrangle1/snippet")
    sync.start()
    sync.set_node("obj/geo1/attribwrangle1/snippet")  # switch node
    sync.stop()
"""

from __future__ import annotations

import os
import threading
import time
import logging
from typing import Optional

log = logging.getLogger(__name__)

try:
    import pynvim
    _PYNVIM_AVAILABLE = True
except ImportError:
    _PYNVIM_AVAILABLE = False
    log.warning("pynvim not available — Neovim sync disabled")


class NvimSync(threading.Thread):
    """Background thread managing pynvim RPC connection to embedded Neovim."""

    daemon = True

    def __init__(
        self,
        nvim_exe: str,
        node_path: Optional[str] = None,
    ) -> None:
        super().__init__(name="void-ghostty-nvim-sync")
        self._nvim_exe = nvim_exe
        self._node_path: Optional[str] = node_path
        self._node_path_lock = threading.Lock()

        self._nvim: Optional["pynvim.Nvim"] = None
        self._stop_event = threading.Event()

        # Debounce state for TextChanged
        self._debounce_timer: Optional[threading.Timer] = None
        self._debounce_lock = threading.Lock()
        self._debounce_interval = 1.0 / 30  # 30 Hz

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_node(self, node_path: Optional[str]) -> None:
        """Switch the monitored node.  Thread-safe."""
        with self._node_path_lock:
            self._node_path = node_path

        if node_path and self._nvim is not None:
            self._push_parm_to_nvim(node_path)

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Thread entry
    # ------------------------------------------------------------------

    def run(self) -> None:
        if not _PYNVIM_AVAILABLE:
            return

        try:
            self._nvim = pynvim.attach("child", argv=[self._nvim_exe, "--embed"])
        except Exception as exc:
            log.error("pynvim attach failed: %s", exc)
            return

        try:
            self._setup_autocmds()
            # Push initial parm value if a node is already set
            with self._node_path_lock:
                node_path = self._node_path
            if node_path:
                self._push_parm_to_nvim(node_path)

            # Event loop
            while not self._stop_event.is_set():
                try:
                    self._nvim.run_loop(
                        err_cb=self._on_rpc_error,
                        notification_cb=self._on_notification,
                        setup_cb=None,
                    )
                except Exception:
                    break
        finally:
            try:
                if self._nvim:
                    self._nvim.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Autocmd registration
    # ------------------------------------------------------------------

    def _setup_autocmds(self) -> None:
        nvim = self._nvim
        if nvim is None:
            return
        # Register Lua autocmds that fire RPC notifications back to us
        nvim.exec_lua("""
            local chan = vim.fn.stdpath and vim.api.nvim_get_chan_info(0).id or 0
            -- Use the channel established by pynvim (channel 1 for embedded)
            local ch = 1
            vim.api.nvim_create_autocmd({"TextChanged", "TextChangedI"}, {
                callback = function()
                    local lines = vim.api.nvim_buf_get_lines(0, 0, -1, false)
                    vim.rpcnotify(ch, "VG_TextChanged", table.concat(lines, "\\n"))
                end,
            })
            vim.api.nvim_create_autocmd("BufWritePost", {
                callback = function()
                    local lines = vim.api.nvim_buf_get_lines(0, 0, -1, false)
                    vim.rpcnotify(ch, "VG_BufWritePost", table.concat(lines, "\\n"))
                end,
            })
            vim.api.nvim_create_autocmd("BufLeave", {
                callback = function()
                    local lines = vim.api.nvim_buf_get_lines(0, 0, -1, false)
                    vim.rpcnotify(ch, "VG_BufLeave", table.concat(lines, "\\n"))
                end,
            })
        """)

    # ------------------------------------------------------------------
    # RPC callbacks
    # ------------------------------------------------------------------

    def _on_notification(self, name: str, args: list) -> None:
        text = args[0] if args else ""
        if name == "VG_TextChanged":
            self._schedule_display_sync(text)
        elif name in ("VG_BufWritePost", "VG_BufLeave"):
            self._cancel_debounce()
            self._sync_and_cook(text)

    def _on_rpc_error(self, error) -> None:
        log.error("pynvim RPC error: %s", error)

    # ------------------------------------------------------------------
    # Sync helpers
    # ------------------------------------------------------------------

    def _schedule_display_sync(self, text: str) -> None:
        """Debounce TextChanged → display-only parm sync (30 Hz cap)."""
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                self._debounce_interval,
                self._do_display_sync,
                args=(text,),
            )
            self._debounce_timer.start()

    def _cancel_debounce(self) -> None:
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None

    def _do_display_sync(self, text: str) -> None:
        """Display-only sync: set parm value, NO cook."""
        with self._node_path_lock:
            node_path = self._node_path
        if not node_path:
            return
        self._defer(lambda: _set_parm(node_path, text))

    def _sync_and_cook(self, text: str) -> None:
        """Set parm + cook(force=True) — for :w and BufLeave."""
        with self._node_path_lock:
            node_path = self._node_path
        if not node_path:
            return
        self._defer(lambda: _set_parm_and_cook(node_path, text))

    def _push_parm_to_nvim(self, node_path: str) -> None:
        """On focus gain: push node's code parm into Neovim buffer."""
        if self._nvim is None:
            return
        try:
            import hou
            parm = hou.parm(node_path)
            if parm is None:
                return
            value = parm.eval()
            lines = value.splitlines() if value else [""]
            self._nvim.current.buffer[:] = lines
        except Exception as exc:
            log.warning("push_parm_to_nvim: %s", exc)

    @staticmethod
    def _defer(fn) -> None:
        """Dispatch fn on the Houdini main thread via hdefereval."""
        try:
            import hdefereval
            hdefereval.executeDeferred(fn)
        except ImportError:
            # Not in a Houdini session (e.g. tests) — call directly
            try:
                fn()
            except Exception as exc:
                log.warning("_defer direct call failed: %s", exc)


# ---------------------------------------------------------------------------
# Houdini-thread helpers (called only via hdefereval)
# ---------------------------------------------------------------------------

def _set_parm(node_path: str, text: str) -> None:
    """Set a string parm value — display sync, no cook."""
    try:
        import hou
        parm = hou.parm(node_path)
        if parm is not None:
            parm.set(text)
    except Exception as exc:
        log.warning("_set_parm: %s", exc)


def _set_parm_and_cook(node_path: str, text: str) -> None:
    """Set parm + force-cook the owning node."""
    try:
        import hou
        parm = hou.parm(node_path)
        if parm is None:
            return
        parm.set(text)
        node = parm.node()
        if node is not None:
            node.cook(force=True)
    except Exception as exc:
        log.warning("_set_parm_and_cook: %s", exc)
