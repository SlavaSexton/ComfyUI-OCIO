"""Does OCIO Exposure multiply exactly, and does it leave the range alone at both ends?

WHY THIS EXISTS. This node was added so an exposure move would not require a node from
another pack, and every candidate outside this one clamps to 0..1 by DEFAULT: kjnodes'
RemapImageRange has `clamp` on unless you turn it off, and radiance's Float32ColorCorrect
has a `clamp_output` toggle. A clamp here would silently destroy exactly what the rest of
the pack exists to carry - the values above 1.0 that an HDR pass produces and the negatives
an unclamped VAE decode leaves behind.

So the checks are not "does it look right". They are:

  1. the output is the input times 2**exposure, to float precision, at several stops;
  2. a value well ABOVE 1.0 comes through scaled, not pinned to 1.0;
  3. a NEGATIVE comes through scaled and still negative, because a multiply preserves sign;
  4. alpha, when present, is not touched - only RGB is scaled;
  5. mix behaves: 0.0 is a bit-exact bypass, 1.0 is the full move, 0.5 is halfway.

Check 2 and 3 are the ones that would catch a clamp creeping in, and neither is visible on
a thumbnail: a clamped frame looks like a slightly flatter version of a correct one.

Run:  python tests/test_exposure.py     (no ComfyUI, no GPU, no files - one process, ~1 s)
"""
import importlib.util
import os
import sys
import types

import numpy as np
import torch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fp = types.ModuleType("folder_paths")
fp.get_output_directory = fp.get_temp_directory = fp.get_input_directory = lambda: ROOT
fp.get_filename_list = lambda *a, **k: []
sys.modules["folder_paths"] = fp

spec = importlib.util.spec_from_file_location("ocio_pack", os.path.join(ROOT, "__init__.py"),
                                              submodule_search_locations=[ROOT])
pack = importlib.util.module_from_spec(spec)
sys.modules["ocio_pack"] = pack
spec.loader.exec_module(pack)

FAILED = []


def check(label, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  -> " + str(detail)) if detail else ""))
    if not ok:
        FAILED.append(label)


def main():
    assert "OCIOExposure" in pack.NODE_CLASS_MAPPINGS, "OCIOExposure is not registered"
    node = pack.NODE_CLASS_MAPPINGS["OCIOExposure"]()

    # the sockets have to match every other colour node in the pack, or it will not drop
    # into an existing graph in place of one
    cls = pack.NODE_CLASS_MAPPINGS["OCIOExposure"]
    it = cls.INPUT_TYPES()
    check("sockets match the pack's other colour nodes",
          sorted(it["optional"]) == ["image", "video"]
          and cls.RETURN_TYPES == ("IMAGE", "VIDEO")
          and cls.RETURN_NAMES == ("image/sequence/video", "ComfyUI Video")
          and cls.CATEGORY == "OCIO",
          "%s / %s" % (cls.RETURN_TYPES, cls.CATEGORY))

    # a frame holding everything a clamp would destroy
    rgb = torch.tensor([[[[-0.25, 0.18, 4.0], [0.5, 12.0, -0.001]]]], dtype=torch.float32)

    for ev in (0.0, 1.0, -1.57, 3.0, -6.0):
        out, _ = node.run(image=rgb, exposure=ev, mix=1.0)
        want = rgb.numpy() * (2.0 ** ev)
        check("exposure %+.2f is an exact multiply" % ev,
              np.allclose(out.numpy(), want, rtol=1e-6, atol=1e-9),
              "max abs error %.3e" % float(np.abs(out.numpy() - want).max()))

    out, _ = node.run(image=rgb, exposure=1.0, mix=1.0)
    a = out.numpy()
    check("a value above 1.0 is scaled, not pinned", abs(float(a.max()) - 24.0) < 1e-5,
          "12.0 at +1 stop -> %.4f" % float(a.max()))
    check("a negative stays negative and is scaled", abs(float(a.min()) - (-0.5)) < 1e-6,
          "-0.25 at +1 stop -> %.4f" % float(a.min()))

    # alpha, if the tensor carries one, must not move
    rgba = torch.tensor([[[[0.2, 0.4, 0.6, 0.33]]]], dtype=torch.float32)
    out, _ = node.run(image=rgba, exposure=2.0, mix=1.0)
    check("alpha is passed through untouched", abs(float(out[0, 0, 0, 3]) - 0.33) < 1e-7,
          "alpha -> %.5f" % float(out[0, 0, 0, 3]))
    check("...while RGB beside it was scaled", abs(float(out[0, 0, 0, 0]) - 0.8) < 1e-6,
          "0.2 at +2 stops -> %.5f" % float(out[0, 0, 0, 0]))

    out, _ = node.run(image=rgb, exposure=2.0, mix=0.0)
    check("mix 0.0 is a bit-exact bypass", torch.equal(out, rgb))
    out, _ = node.run(image=rgb, exposure=2.0, mix=0.5)
    check("mix 0.5 lands halfway",
          np.allclose(out.numpy(), (rgb.numpy() + rgb.numpy() * 4.0) / 2.0, rtol=1e-6))

    # the control that proves the checks above can fail: a clamped version must break them
    clamped = torch.clamp(rgb * 2.0, 0.0, 1.0)
    check("control: a clamped result WOULD fail check 2 and 3",
          not (abs(float(clamped.max()) - 24.0) < 1e-5) and not (float(clamped.min()) < 0),
          "clamped max %.3f min %.3f" % (float(clamped.max()), float(clamped.min())))

    print()
    if FAILED:
        print("FAILED: %d -> %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("PASS: OCIO Exposure multiplies exactly and clamps nothing at either end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
