# -*- coding: utf-8 -*-
"""OCIO Player's frame cache: every written frame is four channels, whatever it was handed.

Run:  python tests/test_player_frames.py        (no pytest, no ComfyUI server, no GPU)

WHY THIS FILE EXISTS. The player had NO test at all, which is how a real defect lived in it unnoticed:
`_player_cache` read `rgb = arr[i]` with a comment above it asserting `[N,H,W,3]`, and nothing enforced that.
Handed a four-channel IMAGE it wrote a FIVE-channel frame file. Measured before the fix: a (2,8,12,4) input
produced (8,12,5) on disk where the viewer reads four, so every texel after the first was misaligned. The front
end's own size guard cannot catch it either, because it tests for a buffer SMALLER than expected and five
channels is larger.

Four other places in io_nodes.py already slice `[..., :3]`; this one had been missed. OCIO Read cannot reach it,
since it slices on the way in, but any third-party node that emits RGBA as IMAGE can, and several do.

The alpha channel of the image is DROPPED rather than used, and that is the pack's convention rather than an
oversight: alpha arrives on its own MASK socket, which is what the fourth written channel comes from.
"""
import glob
import importlib.util
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


TMP = tempfile.mkdtemp(prefix="ocio_player_")


def _load():
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = fp.get_temp_directory = fp.get_input_directory = lambda: TMP
    fp.get_filename_list = lambda *a, **k: []
    sys.modules.setdefault("folder_paths", fp)
    pkg = types.ModuleType("ocio_pkg")
    pkg.__path__ = [_ROOT]
    sys.modules["ocio_pkg"] = pkg
    for name in ("nodes", "io_nodes"):
        spec = importlib.util.spec_from_file_location(f"ocio_pkg.{name}", os.path.join(_ROOT, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"ocio_pkg.{name}"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["ocio_pkg.io_nodes"]


io = _load()
import numpy as np                                                          # noqa: E402
import torch                                                                # noqa: E402


def frames(uid, images, alpha=None):
    d, n, cap, h, w = io._player_cache(uid, images, alpha)
    files = sorted(glob.glob(os.path.join(d, "*.npy")))
    return [np.load(f) for f in files], (n, cap, h, w)


print("a frame file is always four channels, whatever the IMAGE carried")
for ch in (3, 4):
    got, meta = frames(f"ch{ch}", torch.rand(2, 8, 12, ch))
    check(f"a {ch}-channel IMAGE writes 4-channel frames",
          bool(got) and all(a.shape[-1] == 4 for a in got),
          f"got {[a.shape for a in got]}")
    check(f"a {ch}-channel IMAGE keeps its resolution", bool(got) and got[0].shape[:2] == (8, 12),
          f"got {got[0].shape[:2] if got else None}")

print("\nthe three channels kept are R, G and B in order, not a reshuffle")
img = torch.zeros(1, 2, 2, 4)
img[..., 0], img[..., 1], img[..., 2], img[..., 3] = 0.25, 0.5, 0.75, 0.125
got, _ = frames("order", img)
px = [round(float(v), 4) for v in got[0][0, 0]]
check("R, G, B survive unchanged", px[:3] == [0.25, 0.5, 0.75], f"got {px[:3]}")
check("the image's own 4th channel is dropped, not written as alpha", abs(px[3] - 0.125) > 1e-3,
      f"4th written channel is {px[3]}")
check("and the written alpha is opaque when no MASK is wired", abs(px[3] - 1.0) < 1e-3, f"got {px[3]}")

print("\na wired MASK becomes the alpha, at both channel counts")
for ch in (3, 4):
    im = torch.zeros(1, 4, 6, ch)
    im[..., :3] = 0.5
    mask = torch.full((1, 4, 6), 0.25)
    got, _ = frames(f"mask{ch}", im, mask)
    check(f"{ch}-channel IMAGE plus a MASK still writes 4 channels",
          got and got[0].shape[-1] == 4, f"got {got[0].shape if got else None}")
    check(f"{ch}-channel: the MASK value is the alpha",
          got and abs(float(got[0][0, 0, 3]) - 0.25) < 1e-3,
          f"got {float(got[0][0, 0, 3]) if got else None}")

print("\nHDR survives the cache, which is the point of a float viewer")
hdr = torch.zeros(1, 2, 2, 3)
hdr[..., 0], hdr[..., 1], hdr[..., 2] = -0.25, 1.0, 12.5
got, _ = frames("hdr", hdr)
v = [float(x) for x in got[0][0, 0][:3]]
check("a value above 1 is not clamped", v[2] > 12.0, f"got {v[2]}")
check("a value below 0 is not clamped", v[0] < -0.2, f"got {v[0]}")
check("the frame is stored as half float, not 8-bit", got[0].dtype == np.float16, str(got[0].dtype))

print("\na mismatched MASK is ignored rather than crashing or corrupting the shape")
im = torch.zeros(1, 4, 6, 3)
got, _ = frames("badmask", im, torch.full((1, 99, 99), 0.5))
check("the frame is still 4 channels at the image's resolution",
      got and got[0].shape == (4, 6, 4), f"got {got[0].shape if got else None}")
check("and the alpha fell back to opaque", got and abs(float(got[0][0, 0, 3]) - 1.0) < 1e-3)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
