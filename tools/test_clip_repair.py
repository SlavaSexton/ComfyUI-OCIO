"""Does OCIO Clip Repair touch only the clipped ends, and does its plate_space widget do anything?

WHY THIS EXISTS. The node shipped without a test. That is the defect this file closes: a behaviour with
no test in the gate is a behaviour nobody has agreed to keep, and this node has two that are easy to
break silently.

THE FIRST IS THE ONE THAT ALREADY BROKE ONCE. An earlier version decided the plate's space by testing
`p.max() > 1.001`. That cannot work: a scene-linear plate built from an 8-bit source peaks at exactly
1.0, the same as a display-referred one, so every such plate was read as display codes and the mask was
built on the wrong numbers - it found 0.08% of clipped highlights where several percent were clipped.
The fix was an explicit widget, and the check below is that the widget still CHANGES something. A
parameter the body ignores is worse than no parameter, because the caller believes they set it.

THE SECOND IS THE NODE'S WHOLE PURPOSE. Everything outside the mask must come through as the plate,
bit for bit. If that stops being true the node is no longer a repair, it is a full-frame rewrite with
extra steps, and the failure is invisible on a thumbnail.

Run:  python tools/test_clip_repair.py     (no ComfyUI, no GPU, no files - single process, ~1 s)
"""
import importlib.util
import os
import sys
import types

import numpy as np
import torch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The node imports nothing from ComfyUI, but the package it lives in does; stub the
# host module so this stays a small test - one process, no I/O, no server.
fp = types.ModuleType("folder_paths")
fp.get_output_directory = fp.get_temp_directory = fp.get_input_directory = lambda: ROOT
fp.get_filename_list = lambda *a, **k: []
sys.modules.setdefault("folder_paths", fp)

spec = importlib.util.spec_from_file_location("repair_nodes", os.path.join(ROOT, "repair_nodes.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

FAILURES = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  -> ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def ramp(width=128, height=64):
    """A horizontal 0..1 ramp of display codes, and the same encoded to scene-linear.

    The linear one peaks at exactly 1.0, which is the case the old auto-detection could
    not tell apart from a display plate - so it is the case worth testing on.
    """
    codes = np.tile(np.linspace(0.0, 1.0, width, dtype=np.float32), (height, 1))
    linear = np.power(codes, 2.4)[None, ..., None].repeat(3, axis=3)
    return codes, torch.from_numpy(linear.astype(np.float32))


def main():
    node = R.OCIOClipRepair()
    codes, plate = ramp()
    recon = torch.full_like(plate, 8.0)          # unmistakably not the plate

    # --- the widget must change the answer ----------------------------------------
    # On a linear plate the node has to encode before thresholding; on a display plate
    # it must not. Same pixels, two settings, two different mask areas.
    _, mask_lin, _ = node.repair(plate, recon, True, 0.97, False, 0.01, 0, 0, False, "scene linear")
    _, mask_dsp, _ = node.repair(plate, recon, True, 0.97, False, 0.01, 0, 0, False, "display codes")
    area_lin = float(mask_lin.mean())
    area_dsp = float(mask_dsp.mean())
    expected = float((codes >= 0.97).mean())     # what a correct read of display codes gives

    check("plate_space is read, not ignored", abs(area_lin - area_dsp) > 1e-6,
          f"scene linear {area_lin*100:.3f}% vs display codes {area_dsp*100:.3f}%")
    check("scene linear setting reproduces the true clipped area",
          abs(area_lin - expected) < 1e-6,
          f"{area_lin*100:.3f}% against {expected*100:.3f}% expected")
    check("the wrong setting is wrong, which is why the widget exists",
          abs(area_dsp - expected) > 1e-6,
          f"{area_dsp*100:.3f}% - about half, the old bug's signature")

    # --- outside the mask, the plate must survive untouched ------------------------
    out, mask, _ = node.repair(plate, recon, True, 0.97, False, 0.01, 6, 24, False, "scene linear")
    o, p, m = out.numpy(), plate.numpy(), mask.numpy()
    outside = m < 1e-6
    worst = float(np.abs(o[..., :3][outside] - p[..., :3][outside]).max()) if outside.any() else 0.0
    check("pixels outside the mask are bit-identical to the plate", worst == 0.0,
          f"largest difference {worst:.3e} over {int(outside.sum())} px")

    # --- and inside it, the reconstruction actually arrives -------------------------
    # Asked without feathering. With grow 6 and feather 24 the clipped band on this
    # fixture is four columns wide, so the blur leaves nothing at full weight and the
    # question becomes one about the blur radius rather than about the composite. Two
    # separate properties, two separate checks.
    hard_out, hard_mask, _ = node.repair(plate, recon, True, 0.97, False, 0.01,
                                         0, 0, False, "scene linear")
    hm = hard_mask.numpy()
    inside = hm > 0.999
    got = float(hard_out.numpy()[..., :3][inside].min()) if inside.any() else float("nan")
    check("pixels fully inside the mask come from the reconstruction",
          bool(inside.any()) and abs(got - 8.0) < 1e-4,
          f"min {got:.4f} over {int(inside.sum())} px, expected 8.0")

    # Feathering is then its own property: it must soften the edge without reaching
    # either extreme everywhere, which is what makes the composite invisible.
    edge = (m > 0.01) & (m < 0.99)
    check("feathering produces a graded edge rather than a hard cut",
          bool(edge.any()), f"{int(edge.sum())} px partially blended")

    # --- both ends off is a pass-through, not a no-op that still rewrites ----------
    off, mask_off, _ = node.repair(plate, recon, False, 0.97, False, 0.01, 6, 24, True, "scene linear")
    check("with both ends off the plate passes through unchanged",
          float(np.abs(off.numpy() - p).max()) == 0.0 and float(mask_off.max()) == 0.0)

    # --- a size mismatch must refuse rather than guess a resample -------------------
    try:
        node.repair(plate, recon[:, :, :64], True, 0.97, False, 0.01, 0, 0, False, "scene linear")
        check("mismatched sizes are refused", False, "no error raised")
    except ValueError as e:
        check("mismatched sizes are refused", "resize" in str(e).lower(), str(e)[:60])

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {', '.join(FAILURES)}")
        return 1
    print("PASS: clip repair touches only the clipped ends and reads its own widget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
