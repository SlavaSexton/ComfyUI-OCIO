"""Regression: the `view` widget can pick the ACES VERSION, and it changes the maths (run: python tests/test_view_aces_version.py).

The ACES Output Transform is not one transform, it is a version of one, and two versions do not agree.
Measured on a Rec.709 master: an EXR made through the ACES 2.0 inverse Output Transform and viewed back
through the SAME 2.0 transform round-trips to 0.157% of full scale, while the same file viewed through ACES
1.3 is off by 35.5% on the worst pixel and 1.2% on the mean. Nothing in the file says which version made it.
It just expects a matching viewer.

That is why the version has to be the artist's choice rather than the loaded config's. A Nuke 13 / 14 comp is
on ACES 1.2 / 1.3, and an EXR rendered here with a 2.0 inverse arrives there looking "close, but the blacks
and highlights are wrong" - which is exactly how a version mismatch presents, and gives no clue as to why.

What is locked down here:

1. Both versions are OFFERED, and the 1.3 entries are prefixed so the version is visible in the list rather
   than implied. `Raw` and `Un-tone-mapped` exist in both configs and would otherwise collide silently.
2. The choice REACHES THE PIXELS. A 1.3 view must produce what the 1.3 config produces, not what the loaded
   config produces - the failure mode being a label that changes and maths that does not.
3. The two versions must actually DIFFER on the same input, or the whole feature is decoration.
4. The default is unchanged, so no saved workflow moves.
"""
import importlib.util
import os
import sys
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(tmp="."):
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = fp.get_temp_directory = fp.get_input_directory = lambda: tmp
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


DISPLAY = "Rec.1886 Rec.709 - Display"
SCENE = "ACEScg"
V20 = "ACES 2.0 - SDR 100 nits (Rec.709)"
V13 = "ACES 1.3: ACES 1.0 - SDR Video"


def _ramp():
    import torch
    # a display-referred ramp: exactly the material that gets taken back to scene-referred
    x = [i / 15.0 for i in range(16)]
    return torch.tensor([[[[v, v, v] for v in x]]], dtype=torch.float32)


def check_both_versions_offered(io):
    vs = io._view_choices()
    assert V20 in vs, f"the loaded config's own view is missing from the list: {vs[:6]}"
    assert V13 in vs, f"the ACES 1.3 view is not offered; got {[v for v in vs if v.startswith('ACES 1.3')][:4]}"
    assert vs[0] == io._VIEW_NONE, "the do-nothing value must stay first, it is the default"
    # the prefix has to be there, or Raw / Un-tone-mapped from the two configs collide with no way to tell them apart
    dupes = [v for v in vs if vs.count(v) > 1]
    assert not dupes, f"duplicate entries in the view list: {sorted(set(dupes))}"


def check_the_choice_reaches_the_pixels(io):
    """A 1.3 view must produce what the 1.3 config produces. Compared against OCIO directly, not against
    another of our own code paths - two of our paths agreeing proves nothing about either."""
    import numpy as np
    import PyOpenColorIO as OCIO
    img = _ramp()
    ours = io._convert_via_view(img, DISPLAY, SCENE, V13).detach().cpu().numpy()

    cfg = OCIO.Config.CreateFromFile(io._ALT_ACES_URI)
    t = OCIO.DisplayViewTransform(src=SCENE, display=DISPLAY, view=V13[len(io._ALT_ACES_LABEL):])
    proc = cfg.getProcessor(t, OCIO.TRANSFORM_DIR_INVERSE).getDefaultCPUProcessor()
    want = np.ascontiguousarray(img.detach().cpu().numpy().reshape(-1, 3).copy())
    proc.applyRGB(want)
    want = want.reshape(ours.shape)

    d = float(np.abs(ours - want).max())
    assert d < 1e-5, (
        f"a '{V13}' view did not produce the ACES 1.3 result; worst channel differs by {d:.6f}. "
        "The label changed and the maths did not.")


def check_the_versions_actually_differ(io):
    """If 1.3 and 2.0 gave the same numbers there would be nothing to choose between."""
    import numpy as np
    img = _ramp()
    a = io._convert_via_view(img, DISPLAY, SCENE, V20).detach().cpu().numpy()
    b = io._convert_via_view(img, DISPLAY, SCENE, V13).detach().cpu().numpy()
    d = float(np.abs(a - b).max())
    assert d > 0.01, (
        f"ACES 2.0 and ACES 1.3 produced the same pixels (worst difference {d:.6f}); one of them is not being "
        "applied, and the picker is decoration.")


def check_default_unchanged(io):
    import numpy as np
    img = _ramp()
    plain = io._convert(img, DISPLAY, SCENE).detach().cpu().numpy()
    dflt = io._convert_via_view(img, DISPLAY, SCENE, io._VIEW_NONE).detach().cpu().numpy()
    d = float(np.abs(plain - dflt).max())
    assert d == 0.0, f"the do-nothing view stopped being a no-op (differs by {d:.8f}); saved workflows would move"


def main():
    io = _load()
    failures = []
    for fn in (check_both_versions_offered, check_the_choice_reaches_the_pixels,
               check_the_versions_actually_differ, check_default_unchanged):
        try:
            fn(io)
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            failures.append(fn.__name__)
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            failures.append(fn.__name__)
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nthe view widget picks the ACES version, and the version reaches the pixels: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
