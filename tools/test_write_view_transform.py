# -*- coding: utf-8 -*-
"""OCIO Write's `view`: the scene/display crossing is a choice, and it must not leak anywhere else.

Run:  python tools/test_write_view_transform.py     (no pytest, no ComfyUI server, no GPU)
      Stop ComfyUI first. A running server holds resources and makes unrelated tests in this suite fail.

WHY THIS FILE EXISTS. Converting between a scene-referred and a display-referred colorspace has two correct
answers and this pack only ever gave one. A plain ColorSpaceTransform crosses the boundary through the
config's `default_view_transform` (`Un-tone-mapped` in the ACES 2.0 studio config), so ACEScg 0.18 lands on
0.489436 and scene-linear 4.0 leaves as 1.781807 to be clipped flat by whatever container is written.
Through the ACES Output Transform the same values land on 0.383116 and 0.905089, and nothing reaches code 1.0
until scene-linear around 128. A review deliverable usually wants the second, and could not reach it here.

What this pins, worst consequence first:

1. THE DEFAULT DOES NOTHING. ComfyUI fills a widget missing from a saved workflow with the node's current
   default, so a default that applied a transform would silently re-render every graph ever saved - including
   for anyone already compensating with an OCIO Display node, who would get it applied twice.
2. THE WIDGET IS OPTIONAL AND APPENDED. A new required input is a hard validation error for an API caller
   that omits it; a widget inserted mid-list shifts every value in every saved workflow, which reads them by
   position.
3. A VIEW CHANGES THE ANSWER in both directions, to the documented values.
4. PAIRS WITH NO FORK IGNORE IT. Camera log to scene-linear, scene to scene, display to display: applying a
   rendering transform there would be wrong, so a view must be a no-op.
"""
import importlib.util
import inspect
import os
import sys
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


try:
    import torch
except ImportError as e:
    print(f"SKIP: {e} - this test needs torch")
    sys.exit(0)

_fp = types.ModuleType("folder_paths")
_fp.get_output_directory = _fp.get_temp_directory = _fp.get_input_directory = lambda: "."
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
IO = sys.modules["ocio_pkg.io_nodes"]

VIEW = "ACES 2.0 - SDR 100 nits (Rec.709)"
DISP = "Rec.1886 Rec.709 - Display"


def px(*vals):
    return torch.tensor([[[[v, v, v] for v in vals]]], dtype=torch.float32)


def at(t, i):
    return float(t[0, 0, i, 0])


print("1. the default is a no-op, or every saved workflow silently re-renders")
img = px(0.18, 4.0)
plain = IO._convert(img, "ACEScg", DISP)
dflt = IO._convert_via_view(img, "ACEScg", DISP, IO._VIEW_NONE)
check("_VIEW_NONE is byte-identical to the plain conversion", bool(torch.equal(plain, dflt)),
      f"{at(plain, 0):.6f} vs {at(dflt, 0):.6f}")
it = IO.OCIOWrite.INPUT_TYPES()
check("the widget's declared default IS that value", it["optional"]["view"][1]["default"] == IO._VIEW_NONE)
check("and the signature agrees, so canvas and API cannot differ",
      inspect.signature(IO.OCIOWrite.write).parameters["view"].default == IO._VIEW_NONE)
check("_VIEW_NONE is the FIRST choice offered", IO._view_choices()[0] == IO._VIEW_NONE)

print("\n2. optional and appended, so no saved graph and no API call moves")
check("view is OPTIONAL, not required", "view" in it.get("optional", {}) and "view" not in it["required"])
check("the required list is still 18 long, i.e. nothing shifted", len(it["required"]) == 18,
      str(len(it["required"])))

print("\n3. a view changes the answer, in both directions, to the documented values")
v = IO._convert_via_view(img, "ACEScg", DISP, VIEW)
check("scene->display 0.18 tone maps to 0.383116", abs(at(v, 0) - 0.383116) < 1e-4, f"{at(v, 0):.6f}")
check("scene->display 4.0 rolls off to 0.905089", abs(at(v, 1) - 0.905089) < 1e-4, f"{at(v, 1):.6f}")
check("   and the default still carries it past 1.0", at(dflt, 1) > 1.5, f"{at(dflt, 1):.6f}")
d = px(0.18, 0.5)
check("display->scene 0.5 inverts to 0.324827 with a view",
      abs(at(IO._convert_via_view(d, DISP, "ACEScg", VIEW), 1) - 0.324827) < 1e-4)
check("   against 0.189468 without one",
      abs(at(IO._convert_via_view(d, DISP, "ACEScg", IO._VIEW_NONE), 1) - 0.189468) < 1e-4)

print("\n4. pairs with no scene/display boundary ignore the view completely")
for a, b in (("ARRI LogC3 (EI800)", "ACEScg"), ("ACEScg", "ACEScct"),
             ("ACEScg", "ACES2065-1"), ("sRGB - Display", DISP)):
    n = IO._convert_via_view(d, a, b, IO._VIEW_NONE)
    w = IO._convert_via_view(d, a, b, VIEW)
    check(f"{a} -> {b}: a view is a no-op", bool(torch.equal(n, w)), f"{at(n, 0):.6f} vs {at(w, 0):.6f}")
    check(f"   and no crossing is reported for it", IO._crosses_scene_display(a, b) is None)

print("\n5. the fork is read from OCIO's metadata, not from a name list")
check("scene -> display is a forward crossing",
      IO._crosses_scene_display("ACEScg", DISP) == (DISP, "ACEScg", "forward"))
check("display -> scene is an inverse crossing",
      IO._crosses_scene_display(DISP, "ACEScg") == (DISP, "ACEScg", "inverse"))
check("identical colorspaces report no crossing", IO._crosses_scene_display("ACEScg", "ACEScg") is None)
check("an empty colorspace reports no crossing", IO._crosses_scene_display("", "ACEScg") is None)

print("\n6. a view that does not belong to the resolved display is refused, not ignored")
try:
    IO._convert_via_view(img, "ACEScg", DISP, "ACES 2.0 - HDR 1000 nits (P3 D65)")
    check("an alien view raises", False, "it was accepted silently")
except ValueError as e:
    check("an alien view raises, naming what the display does offer", "does not exist on display" in str(e))
except Exception as e:
    check("an alien view raises ValueError", False, f"raised {type(e).__name__}")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
