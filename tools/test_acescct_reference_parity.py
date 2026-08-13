# -*- coding: utf-8 -*-
"""Is our ACEScct the SAME CURVE as the reference LTX-2 implementation? Measured, not asserted.

Run:  python tools/test_acescct_reference_parity.py     (no pytest, no ComfyUI, no GPU)

WHY THIS FILE EXISTS. tools/test_acescct.py checks that our curve round-trips to 1e-4 and that middle grey
lands near 0.4135. Both are true of curves that are NOT ACEScct - a round-trip only proves the encode and
decode are each other's inverse, and an anchor within 0.01 is loose enough to admit a different curve. The
claim that actually matters, and the one an audience of colour scientists will check, is parity with the
reference implementation. So this file states the reference formulae explicitly and measures the deviation.

THE REFERENCE. Lightricks/LTX-2, packages/ltx-core/src/ltx_core/hdr.py - constants at :45-50, compress at
:68-72, decompress at :75-79. Those constants are the published AMPAS S-2016-001 values. Transcribed here
by hand rather than imported, because the point is to compare against an independent statement of the
maths, not against ourselves.

WHAT THIS FILE DELIBERATELY TREATS AS A DIFFERENCE AND NOT A BUG. The reference clamps in three places we
do not: input to >= 0 and output to [0, 1] on compress, and input to [0, 1] on decompress. That asymmetry
is correct on both sides and is the reason it is measured separately below rather than folded into a single
pass/fail:

  * The reference feeds a VAE, which was trained on a bounded signal, so it guarantees the bound and
    raises when the caller breaks it (media_io/range_map.py:12).
  * We feed a COLOUR PIPELINE, where negatives and values past the ceiling are real data that a grade
    needs. Clamping them inside a transfer curve would destroy information the artist has not agreed to
    lose, and would do it invisibly.

So the two halves of our pack split the job: OCIO LogConvert preserves the signal, and OCIO VAE Encode is
where the VAE's bound is checked and reported. This file confirms that the CURVE is identical wherever
both are defined, and states exactly where the clamping behaviour diverges.
"""
import importlib.util
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import numpy as np

from nodes import _lin_to_acescct, _acescct_to_lin


def _load_io_nodes():
    """io_nodes uses relative imports, so it only loads as part of a package. This is the pack's own
    established way of doing it in tests (see tools/test_write_output.py) - reused rather than reinvented."""
    tmp = tempfile.mkdtemp(prefix="ocio_parity_")
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


_acescct_to_lin_shaper = _load_io_nodes()._acescct_to_lin

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


# A BARE RELATIVE ERROR IS NOT A VALID CRITERION HERE, and the first version of this file failed three
# checks because of it, not because of any defect in the code. ACEScct decodes code 0.0729055 to exactly
# zero, so just above and below that point the reference value is of order 1e-7 and a perfectly good
# absolute error of 1e-10 shows up as a relative error of 3e-4. The round trip of scene-linear 0.0 came
# back as -1.12e-10 and was reported as an 11% error purely because of the epsilon in the denominator.
# The correct statement of numerical parity carries BOTH tolerances, which is what np.allclose does, and
# both are named here rather than buried: 1e-6 absolute covers the neighbourhood of zero, 1e-5 relative
# covers the top of the curve where the values reach ~222.
ATOL, RTOL = 1e-6, 1e-5


def agree(name, ours, ref, extra=""):
    """Compare with a mixed criterion and report the absolute AND relative worst case, so neither hides."""
    ours = np.asarray(ours, np.float64)
    ref = np.asarray(ref, np.float64)
    a = np.abs(ours - ref)
    ia = int(np.argmax(a))
    ok = bool(np.allclose(ours, ref, rtol=RTOL, atol=ATOL))
    # The relative figure is quoted only where the reference is larger than the absolute tolerance. Below
    # that the ratio is arithmetic noise about a value that is already correct to within ATOL, and quoting
    # it produces numbers like 1e+297 that read as a catastrophe and mean nothing.
    big = np.abs(ref) > ATOL
    if big.any():
        r = a[big] / np.abs(ref[big])
        ir = int(np.argmax(r))
        rel_txt = f"max rel {float(r.max()):.3e} (where ref = {ref[big][ir]:+.3g})"
    else:
        rel_txt = "relative not quoted: every reference value is below the absolute tolerance"
    check(name, ok, f"max abs {float(a.max()):.3e} (where ref = {ref[ia]:+.4g}), {rel_txt}{extra}")
    return ok


# ---------------------------------------------------------------- the reference, transcribed by hand
A_LIN = 10.5402377416545
B_LIN = 0.0729055341958355
X_BRK = 0.0078125
Y_BRK = 0.155251141552511
LOG_M = 17.52
LOG_B = 9.72


def ref_compress(x):
    """hdr.py:68-72, without the reference's clamps, so the CURVE alone is compared."""
    x = np.asarray(x, np.float64)
    return np.where(x > X_BRK, (np.log2(np.maximum(x, 1e-12)) + LOG_B) / LOG_M, A_LIN * x + B_LIN)


def ref_decompress(y):
    """hdr.py:75-79, without the reference's clamps."""
    y = np.asarray(y, np.float64)
    return np.where(y > Y_BRK, np.power(2.0, y * LOG_M - LOG_B), (y - B_LIN) / A_LIN)


print("the reference constants are self-consistent (the two branches must meet at the breakpoint)")
lo = A_LIN * X_BRK + B_LIN
hi = (np.log2(X_BRK) + LOG_B) / LOG_M
check("compress branches meet at x = 2^-7", abs(lo - hi) < 1e-15, f"gap {abs(lo - hi):.3e}")
check("the breakpoint code equals the published Y_BRK", abs(lo - Y_BRK) < 1e-15,
      f"computed {lo:.15f} vs stated {Y_BRK:.15f}")
ceiling = float(ref_decompress(np.array([1.0]))[0])
check("code 1.0 decodes to the published ceiling 222.8609442038076",
      abs(ceiling - 222.8609442038076) < 1e-9, f"{ceiling:.10f}")

print("\nour ENCODE against the reference curve, over the whole defined range")
# Dense, and deliberately including the breakpoint itself plus values either side of it: a curve that is
# right in the middle of each branch and wrong at the join is a real and easy mistake.
x = np.unique(np.concatenate([
    np.geomspace(1e-8, 1e3, 20000),
    np.linspace(0.0, 0.02, 5000),
    np.array([X_BRK, np.nextafter(X_BRK, 0.0), np.nextafter(X_BRK, 1.0), 0.18, 1.0, 222.8609442038076]),
]))
agree("our encode IS the reference curve", _lin_to_acescct(x), ref_compress(x))

print("\nour DECODE against the reference curve, over the whole code range")
y = np.unique(np.concatenate([
    np.linspace(0.0, 1.0, 40001),
    np.array([Y_BRK, np.nextafter(Y_BRK, 0.0), np.nextafter(Y_BRK, 1.0)]),
]))
ours_d = _acescct_to_lin(y).astype(np.float64)
ref_d = ref_decompress(y)
agree("our decode IS the reference curve", ours_d, ref_d)

print("\nthe pack's SECOND implementation (the LUT shaper in io_nodes.py) must be the same curve")
# Two functions answering the same question in one pack diverge silently and permanently unless something
# compares them. This is that something.
shaper = _acescct_to_lin_shaper(y.astype(np.float32)).astype(np.float64)
agree("the shaper IS the reference curve too", shaper, ref_d)
agree("the pack's two implementations agree with EACH OTHER", shaper, ours_d)
print(f"         nodes.py computes in float64 and casts down, io_nodes.py computes in float32 throughout. "
      f"That\n         is the entire difference between them, and it stays inside float32 resolution.")

print("\nround trip through OUR pair, on HDR values the reference's ceiling actually admits")
hdr = np.array([0.0, 0.18, 1.0, 5.0, 20.0, 100.0, 222.0], np.float32)
rt = _acescct_to_lin(_lin_to_acescct(hdr)).astype(np.float64)
agree("round trip holds across 0..222", rt, hdr.astype(np.float64))
for a, b in zip(hdr, rt):
    print(f"         {float(a):9.3f} -> code {float(_lin_to_acescct(np.array([a]))[0]):.6f} -> {b:9.4f}")

print("\nWHERE WE DIVERGE FROM THE REFERENCE ON PURPOSE: the clamps")
neg = np.array([-0.5, -0.05, -0.001], np.float32)
print(f"  negative scene-linear {list(map(float, neg))}")
print(f"    ours            -> {[round(float(v), 6) for v in _lin_to_acescct(neg)]}   "
      f"(kept, so a grade can still recover them)")
print(f"    reference       -> {[round(float(v), 6) for v in ref_compress(np.maximum(neg, 0.0))]}   "
      f"(clamped to >= 0 first, so they all collapse onto the black offset)")
check("we do NOT collapse negatives onto the black offset",
      not np.allclose(_lin_to_acescct(neg).astype(np.float64), B_LIN),
      "negatives stay distinguishable")

over = np.array([300.0, 1000.0], np.float32)
print(f"  scene-linear past the ceiling {list(map(float, over))}")
print(f"    ours            -> codes {[round(float(v), 6) for v in _lin_to_acescct(over)]}   (above 1.0, kept)")
print(f"    reference       -> codes {[round(float(v), 6) for v in np.clip(ref_compress(over), 0, 1)]}   "
      f"(clamped to 1.0, so 300 and 1000 become the same value)")
check("we keep codes above 1.0 distinguishable", float(_lin_to_acescct(over)[0]) < float(_lin_to_acescct(over)[1]),
      "300 and 1000 do not collapse")
print("  -> this is why OCIO VAE Encode carries the range check instead: the curve preserves, the VAE")
print("     boundary is where the bound belongs (their equivalent raises at media_io/range_map.py:12).")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
