"""Runtime smoke test of the OpenColorIO-backed nodes. Needs `pip install opencolorio` (2.2+).
Run from anywhere: python tools/test_ocio_nodes.py
Exercises ColorSpace, CDLTransform, Display, and LogConvert(ocio_config) against the built-in ACES studio
config on a synthetic image, and checks each returns a finite, same-shape result. FileTransform (needs a LUT
file) and LookTransform (needs a config look) are reported as needing real assets."""
import os, sys
import numpy as np
import torch
import PyOpenColorIO as OCIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nodes as N

print("OCIO", OCIO.__version__, "| _HAS_OCIO:", N._HAS_OCIO)
cfg = N._resolve_config("")
spaces = N._colorspace_names("") or []
print("config resolved:", cfg is not None, "| colorspaces:", len(spaces))

img = torch.rand(1, 8, 8, 3, dtype=torch.float32)
fails = 0


def trial(name, fn):
    global fails
    try:
        out = fn()
        t = out[0] if isinstance(out, tuple) else out
        assert tuple(t.shape) == tuple(img.shape), f"shape {tuple(t.shape)}"
        assert torch.isfinite(t).all(), "non-finite output"
        print(f"PASS {name}: range [{float(t.min()):.4f},{float(t.max()):.4f}] mean {float(t.mean()):.4f}")
    except Exception as e:
        fails += 1
        print(f"FAIL {name}: {type(e).__name__}: {e}")


trial("OCIOLogConvert acescct", lambda: N.OCIOLogConvert().run(img, "lin_to_log", "acescct"))
trial("OCIOCDLTransform", lambda: N.OCIOCDLTransform().run(img, 1.1, 1.0, 0.9, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, "forward"))
if len(spaces) >= 2:
    a = next((s for s in spaces if "ACES2065" in s or "Linear" in s), spaces[0])
    b = next((s for s in spaces if "sRGB" in s), spaces[1])
    trial(f"OCIOColorSpace [{a}]->[{b}]", lambda: N.OCIOColorSpace().convert(img, a, b))
trial("OCIOLogConvert ocio_config", lambda: N.OCIOLogConvert().run(img, "lin_to_log", "ocio_config", "", ""))
if cfg is not None and spaces:
    try:
        disp = cfg.getDefaultDisplay(); view = cfg.getDefaultView(disp)
        lin = next((s for s in spaces if "ACES2065" in s or "lin" in s.lower()), spaces[0])
        trial(f"OCIODisplay ->{disp}/{view}", lambda: N.OCIODisplay().run(img, lin, disp, view))
    except Exception as e:
        print("Display setup FAIL:", type(e).__name__, e); fails += 1

print("\nNot exercised (need assets): OCIOFileTransform (a LUT file), OCIOLookTransform (a config look).")
print("RESULT:", "ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
