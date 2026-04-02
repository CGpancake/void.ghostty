"""Phase 4 validation — libvg load + terminal create/free."""
import ctypes, os, hou

ghostty = hou.getenv("GHOSTTY") or os.environ.get("GHOSTTY", "")
assert ghostty, "GHOSTTY not set"

bin_dir  = os.path.join(ghostty, "bin", "windows" if os.name == "nt" else "linux")
lib_name = "vg.dll" if os.name == "nt" else "libvg.so"
lib_path = os.path.join(bin_dir, lib_name)

assert os.path.exists(lib_path), f"vg library not found: {lib_path}"

# On Windows, ghostty-vt.dll must also be findable (same directory)
if os.name == "nt":
    # Add bin/windows to DLL search path so ghostty-vt.dll is found
    import ctypes.util
    os.add_dll_directory(bin_dir)

lib = ctypes.CDLL(lib_path)
print("libvg loaded:", lib_path)

# Configure function signatures
lib.vg_terminal_new.restype  = ctypes.c_void_p
lib.vg_terminal_new.argtypes = [ctypes.c_int, ctypes.c_int]
lib.vg_terminal_free.restype  = None
lib.vg_terminal_free.argtypes = [ctypes.c_void_p]

term = lib.vg_terminal_new(80, 24)
assert term, "vg_terminal_new returned null"
print("vg_terminal_new: PASS")

lib.vg_terminal_free(term)
print("vg_terminal_free: PASS")

# Render state
lib.vg_render_state_new.restype  = ctypes.c_void_p
lib.vg_render_state_new.argtypes = []
lib.vg_render_state_free.restype  = None
lib.vg_render_state_free.argtypes = [ctypes.c_void_p]

rs = lib.vg_render_state_new()
assert rs, "vg_render_state_new returned null"
lib.vg_render_state_free(rs)
print("vg_render_state lifecycle: PASS")

print("Phase 4: PASS")
