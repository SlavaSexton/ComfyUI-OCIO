# -*- coding: utf-8 -*-
"""OCIO Read must decode EXR WITHOUT depending on OPENCV_IO_ENABLE_OPENEXR being set before cv2 is imported.

Run:  python tools/test_exr_read_no_envflag.py     (no pytest, no ComfyUI, no GPU)

WHY THIS FILE EXISTS, AND WHY IT SPAWNS A SUBPROCESS. cv2's EXR codec is gated on a PROCESS-GLOBAL variable
that has to be set before `import cv2`. The pack sets it at the top of io_nodes.py, which cannot help when
another node pack imports cv2 first - and several load ahead of this one alphabetically. On a live server the
variable read '1' and cv2 still raised "OpenEXR codec is disabled", so OCIO Read failed outright on the format
the pack exists to handle.

A test cannot demonstrate that inside its own process: importing cv2 with the flag already set is exactly the
favourable ordering that hid the bug for the entire 16-test gate. So each case below runs in a FRESH
interpreter with the environment arranged deliberately, including the hostile arrangement where cv2 is imported
first with the flag OFF. That is the whole point - the check has to be able to fail.
"""
import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- a real EXR to read, written by OpenEXR
try:
    import numpy as np
    import OpenEXR
except ImportError as e:
    print(f"SKIP: {e} - this test needs numpy and the OpenEXR module")
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="ocio_exrread_")
EXR = os.path.join(tmp, "probe.exr")
H, W = 8, 12
# Values chosen so a channel swap or a precision loss is visible: R, G and B are distinct ramps, and one
# sample sits above 1.0 so a clamping reader is caught too.
rgb = np.zeros((H, W, 3), np.float32)
rgb[..., 0] = np.linspace(0.0, 0.5, W, dtype=np.float32)[None, :]
rgb[..., 1] = np.linspace(0.5, 1.0, W, dtype=np.float32)[None, :]
rgb[..., 2] = np.linspace(1.0, 4.0, W, dtype=np.float32)[None, :]
# OpenEXR.File takes (header, channels) - TWO dicts. Passing one is a TypeError that helpfully lists every
# supported form, which is how this line got written correctly the second time.
with OpenEXR.File({}, {"RGB": rgb}) as f:
    f.write(EXR)
print(f"wrote a probe EXR: {W}x{H}, R 0..0.5, G 0.5..1.0, B 1.0..4.0 (one channel deliberately above white)")

# ---------------------------------------------------------------- the child program
CHILD = r'''
import os, sys
mode = sys.argv[1]
if mode == "cv2_first_flag_off":
    os.environ.pop("OPENCV_IO_ENABLE_OPENEXR", None)
    try:
        import cv2            # the hostile ordering: cv2 locks in its codec table with the flag OFF
    except ImportError:
        pass
elif mode == "cv2_first_flag_on":
    os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"
    try:
        import cv2
    except ImportError:
        pass
elif mode == "no_cv2":
    # cv2 made unimportable outright. This stands in for the two ways a real install arrives with no usable
    # EXR through OpenCV - a wheel whose imgcodecs was built without the codec, and an environment with no
    # OpenCV at all - without needing either machine. `import cv2` in io_nodes must fall to `cv2 = None` and
    # the EXR path must still decode, because the OpenEXR module is the primary reader, not the fallback.
    import importlib.abc
    class _NoCv2(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "cv2" or name.startswith("cv2."):
                raise ImportError("cv2 unavailable in this probe")
            return None
    sys.meta_path.insert(0, _NoCv2())
import importlib.util, tempfile, types
ROOT = sys.argv[2]
t = tempfile.mkdtemp()
fp = types.ModuleType("folder_paths")
fp.get_output_directory = fp.get_temp_directory = fp.get_input_directory = lambda: t
fp.get_filename_list = lambda *a, **k: []
sys.modules.setdefault("folder_paths", fp)
pkg = types.ModuleType("ocio_pkg"); pkg.__path__ = [ROOT]; sys.modules["ocio_pkg"] = pkg
for n in ("nodes", "io_nodes"):
    s = importlib.util.spec_from_file_location("ocio_pkg." + n, os.path.join(ROOT, n + ".py"))
    m = importlib.util.module_from_spec(s); sys.modules["ocio_pkg." + n] = m; s.loader.exec_module(m)
io = sys.modules["ocio_pkg.io_nodes"]
a = io._read_still(sys.argv[3])
import numpy as np
# io.cv2 is reported so the caller can prove the no_cv2 case actually ran WITHOUT cv2. Without it a green
# result there would be indistinguishable from the finder silently failing to block the import.
print("OK", a.shape[0], a.shape[1], a.shape[2],
      round(float(a[..., 0].max()), 4), round(float(a[..., 1].max()), 4), round(float(a[..., 2].max()), 4),
      "cv2none" if io.cv2 is None else "cv2live")
'''
child = os.path.join(tmp, "child.py")
open(child, "w", encoding="utf-8").write(CHILD)

CASES = [
    ("cv2_first_flag_off", "cv2 imported FIRST with the flag OFF (the arrangement that broke it live)"),
    ("cv2_first_flag_on", "cv2 imported first with the flag ON"),
    ("clean", "no cv2 pre-import"),
    ("no_cv2", "cv2 not importable at all (a build with no EXR codec, or no OpenCV)"),
]
print()
for mode, label in CASES:
    env = dict(os.environ)
    env.pop("OPENCV_IO_ENABLE_OPENEXR", None)
    env["PYTHONUTF8"] = "1"
    p = subprocess.run([sys.executable, child, mode, _ROOT, EXR],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    line = next((l for l in (p.stdout or "").splitlines() if l.startswith("OK")), None)
    if line is None:
        tail = (p.stderr or "").strip().splitlines()[-1:] or ["(no stderr)"]
        check(f"EXR read succeeds: {label}", False, tail[0][:150])
        continue
    _, h, w, c, rmax, gmax, bmax, cv2state = line.split()
    ok_shape = (int(h), int(w)) == (H, W) and int(c) >= 3
    check(f"EXR read succeeds: {label}", True, f"{w}x{h}x{c}")
    if mode == "no_cv2":
        # Guards the guard: if the import block did not take, this case would pass on cv2's codec and prove
        # nothing about the OpenEXR path it exists to test.
        check("   cv2 really was unavailable (the case is not passing via cv2)",
              cv2state == "cv2none", cv2state)
    check(f"   dimensions correct ({label.split('(')[0].strip()})", ok_shape, f"{w}x{h}")
    # R must stay R. A BGR mix-up would put the 4.0 ramp in channel 0.
    check(f"   channel order preserved, R != B ({mode})",
          abs(float(rmax) - 0.5) < 1e-3 and abs(float(bmax) - 4.0) < 1e-2,
          f"R max {rmax}, G max {gmax}, B max {bmax}")
    # The value above white must survive: a reader that clamps is as bad as one that fails.
    check(f"   values above 1.0 survive ({mode})", float(bmax) > 1.0, f"B max {bmax}")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
