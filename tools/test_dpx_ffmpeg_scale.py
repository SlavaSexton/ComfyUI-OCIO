# -*- coding: utf-8 -*-
"""A DPX read through the ffmpeg fallback must land on 1.0, not 1.0009.

Run:  python tools/test_dpx_ffmpeg_scale.py     (no pytest, no ComfyUI, no GPU; needs ffmpeg)

WHY THIS FILE EXISTS. `_read_dpx` unpacks the common layouts itself and hands the rest to ffmpeg. The
fallback normalised the returned 16-bit samples by `2**16 - 2**(16-N)`, on the belief that ffmpeg widens an
N-bit sample by shifting it left. It does not: it maps the code range onto the full 16-bit range, so the top
code comes back as 65535 at every depth. Dividing that by 65472 returns 1.000962 for a 10-bit file whose
highest code is exactly 1023, which reported 15.9% of a legal plate as above white.

Reported by Andrei Orehov as issue #7. It survived earlier DPX testing because a file only reaches the
fallback when `width * channels` is not divisible by 3 - a 4096-wide RGB probe goes down the pack's own
unpacker and never sees this line.

The test writes files with ffmpeg and reads them back through the node, so it measures the pair that actually
runs rather than restating the formula.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


try:
    import numpy as np
except ImportError as e:
    print(f"SKIP: {e} - this test needs numpy")
    sys.exit(0)

if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP: ffmpeg/ffprobe not on PATH - the fallback under test is ffmpeg's")
    sys.exit(0)

TMP = tempfile.mkdtemp(prefix="ocio_dpxscale_")
_fp = types.ModuleType("folder_paths")
_fp.get_output_directory = _fp.get_temp_directory = _fp.get_input_directory = lambda: TMP
_fp.get_filename_list = lambda *a, **k: []
sys.modules.setdefault("folder_paths", _fp)
_pkg = types.ModuleType("ocio_pkg")
_pkg.__path__ = [_ROOT]
sys.modules["ocio_pkg"] = _pkg
for _n in ("nodes", "io_nodes"):
    _sp = importlib.util.spec_from_file_location(f"ocio_pkg.{_n}", os.path.join(_ROOT, f"{_n}.py"))
    _m = importlib.util.module_from_spec(_sp)
    sys.modules[f"ocio_pkg.{_n}"] = _m
    _sp.loader.exec_module(_m)
io = sys.modules["ocio_pkg.io_nodes"]

# WHICH ROW ACTUALLY EXERCISES THE FALLBACK, measured rather than assumed by instrumenting the ffmpeg call:
#
#   10-bit, 512 wide, RGB -> the pack's own unpacker
#   12-bit, 512 wide, RGB -> ffmpeg fallback          <- the line this file was written for
#   16-bit, 512 wide, RGB -> the pack's own unpacker
#
# The report that started this used a 10-bit RGBA plate, where 512 * 4 = 2048 is not divisible by 3 and the
# fallback takes it. That file cannot be produced here: ffmpeg's dpx encoder advertises no 10-bit layout with
# alpha and silently writes rgba64le instead, so the 12-bit row is the one that proves the fix. The other two
# are kept because the same assertion should hold whichever decoder answers, and because a future change to
# the routing rule would otherwise move a case out from under any test without saying so.
print("a DPX whose top code is the maximum for its depth must read back as exactly 1.0\n")
for bits, pix in ((10, "gbrp10le"), (12, "gbrp12le"), (16, "rgb48le")):
    top = (1 << bits) - 1
    w, h = 512, 32
    codes = np.full((h, w), top, np.uint32)
    wide = ((codes * 65535) // top).astype("<u2")               # full scale, which is what ffmpeg reads back
    raw = os.path.join(TMP, f"src{bits}.raw")
    np.stack([wide] * 3, -1).tofile(raw)
    dpx = os.path.join(TMP, f"top{bits}.dpx")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb48le",
                    "-s", f"{w}x{h}", "-i", raw, "-c:v", "dpx", "-pix_fmt", pix, dpx],
                   capture_output=True)
    if not os.path.exists(dpx) or os.path.getsize(dpx) == 0:
        check(f"{bits}-bit: ffmpeg wrote the fixture", False, "no file")
        continue

    a = io._read_still(dpx)
    mx = float(a[..., :3].max())
    over = int((a[..., :3] > 1.0).sum())
    check(f"{bits}-bit: the top code reads as 1.0, not above it", abs(mx - 1.0) <= 1e-4,
          f"max={mx:.6f}  (the old bit-shift normaliser gave "
          f"{65535 / ((1 << 16) - (1 << (16 - bits))) if bits < 16 else 1.0:.6f})")
    check(f"   {bits}-bit: no sample of a legal plate is reported above white", over == 0,
          f"{over} samples over 1.0")

# And the reverse guarantee: a mid-grey code must not move either, since a wrong divisor scales everything.
print()
w, h = 512, 32
mid = 512                                                        # a 10-bit code, half of 1023 rounded down
wide = np.full((h, w), (mid * 65535) // 1023, "<u2")
raw = os.path.join(TMP, "mid.raw")
np.stack([wide] * 3, -1).tofile(raw)
dpx = os.path.join(TMP, "mid.dpx")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb48le", "-s", f"{w}x{h}",
                "-i", raw, "-c:v", "dpx", "-pix_fmt", "gbrp10le", dpx], capture_output=True)
a = io._read_still(dpx)
want = mid / 1023.0
got = float(a[..., 0].mean())
check("a mid-grey 10-bit code lands where it should", abs(got - want) <= 2e-4,
      f"code {mid}/1023 = {want:.6f}, read {got:.6f}, error {abs(got-want)*100:.4f}%")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
