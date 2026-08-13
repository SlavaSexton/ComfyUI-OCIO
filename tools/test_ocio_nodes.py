"""Runtime smoke test of the OpenColorIO-backed nodes. Needs `pip install opencolorio` (2.2+).
Run from anywhere: python tools/test_ocio_nodes.py
Exercises ColorSpace, CDLTransform, and Display against the built-in ACES studio config, plus the
dependency-free LogConvert curves, on a synthetic image; checks each returns a finite, same-shape result.
FileTransform (needs a LUT file) and LookTransform (needs a config look) are reported as needing real assets."""
import os, sys
import numpy as np
import torch
import PyOpenColorIO as OCIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nodes as N

print("OCIO", OCIO.__version__, "| _HAS_OCIO:", N._HAS_OCIO)
cfg = N._resolve_config("")
spaces = N._colorspace_names() or []
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
trial("OCIOLogConvert logc3", lambda: N.OCIOLogConvert().run(img, "log_to_lin", "logc3"))
if cfg is not None and spaces:
    try:
        disp = cfg.getDefaultDisplay(); view = cfg.getDefaultView(disp)
        lin = next((s for s in spaces if "ACES2065" in s or "lin" in s.lower()), spaces[0])
        trial(f"OCIODisplay ->{disp}/{view}", lambda: N.OCIODisplay().run(img, lin, disp, view))
    except Exception as e:
        print("Display setup FAIL:", type(e).__name__, e); fails += 1

# --------------------------------------------------------------------------- display / view pairs
# MOST OF THE PAIRS OCIODisplay OFFERS DO NOT EXIST. `view` is the union of every view across every display,
# because the combo is fixed when INPUT_TYPES runs and nobody has picked a display yet. Measured on the studio
# config: 9 displays x 14 views is 126 combinations and 75 are invalid, and on 'sRGB - Display' only 4 of 14 work.
# Before VALIDATE_INPUTS existed those 75 died mid-execution, after the graph had already spent its time.
print("\ndisplay / view pairs are checked before the job runs, not during it")
if cfg is not None:
    displays = list(N._display_input()[0])
    views = list(N._view_input()[0])
    rejected = accepted = wrong = 0
    for d in displays:
        try:
            real = list(cfg.getViews(d))
        except Exception:
            continue
        for v in views:
            verdict = N.OCIODisplay.VALIDATE_INPUTS(display=d, view=v)
            should_pass = v in real
            if should_pass and verdict is True:
                accepted += 1
            elif not should_pass and verdict is not True:
                rejected += 1
            else:
                wrong += 1
                if wrong <= 3:
                    print(f"  FAIL  {d!r} + {v!r}: exists={should_pass} verdict={str(verdict)[:60]}")
    print(f"  valid pairs accepted: {accepted}   invalid pairs rejected: {rejected}   disagreements: {wrong}")
    if wrong:
        fails += 1
    if rejected == 0:
        print("  FAIL  nothing was rejected, so the validator is inert")
        fails += 1
    # The message has to name the way out, or it is only a tidier way to fail.
    d0 = displays[0]
    bad = [v for v in views if v not in list(cfg.getViews(d0))]
    if bad:
        msg = str(N.OCIODisplay.VALIDATE_INPUTS(display=d0, view=bad[0]))
        if "Valid views for this display" not in msg or not any(v in msg for v in cfg.getViews(d0)):
            print(f"  FAIL  the rejection does not list the views that would work: {msg[:110]}")
            fails += 1
        else:
            print("  the rejection lists the views that would work")
    # It must never raise, whatever it is handed: a broken probe refusing a good prompt is worse than no check.
    for kw in ({"display": None, "view": None}, {"display": "nope", "view": "nope"},
               {"display": displays[0], "view": None}, {}):
        try:
            N.OCIODisplay.VALIDATE_INPUTS(**kw)
        except Exception as e:
            print(f"  FAIL  VALIDATE_INPUTS raised on {kw}: {type(e).__name__}")
            fails += 1

print("\nNot exercised (need assets): OCIOFileTransform (a LUT file), OCIOLookTransform (a config look).")
print("RESULT:", "ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
