"""Cache parity + cache-behavior regression for the config/processor LRU cache added to the six color nodes
(run: python tools/test_cache_parity.py). Needs opencolorio. No ComfyUI process required - the color nodes
call PyOpenColorIO directly.

Three things checked per OCIO node (OCIOColorSpace, OCIODisplay, OCIOCDLTransform, OCIOFileTransform,
OCIOLookTransform - OCIOLogConvert is dependency-free and untouched, skipped here):

  1. PARITY - running the node (which now goes through the cache) must produce bit-identical output to an
     independent, uncached PyOpenColorIO processor built inline from scratch, on a fixed synthetic image that
     includes HDR (>1) and negative values. Checked across two calls on the same node instance (cache hit) and
     one call on a brand-new instance (fresh cache lookup, same key).
  2. CACHE WORKS - the processor cache is empty before the first call, has exactly one entry after it, and
     does not grow on a second identical call (same key => hit, no rebuild).
  3. DIFFERENT PARAMS MISS - changing one parameter (CDL slope, ColorSpace out, Display view, FileTransform
     interpolation, Look) produces a second distinct cache entry, and the ORIGINAL entry's cached processor
     is unchanged (no wrong-key collision overwriting or returning a stale processor).
"""
import os
import sys

import numpy as np
import torch
import PyOpenColorIO as OCIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nodes as N

fails = 0


def check(cond, msg):
    global fails
    if not cond:
        fails += 1
        print(f"FAIL: {msg}")
    else:
        print(f"PASS: {msg}")


# --------------------------------------------------------------------------- fixed synthetic HDR/negative image
# batch of 2, 4x4, 3ch: includes values >1 (HDR) and <0 (out-of-gamut / negative, e.g. after a CDL offset)
rng = np.random.default_rng(20260701)
_base = rng.uniform(-0.5, 4.0, size=(2, 4, 4, 3)).astype(np.float32)
IMG = torch.from_numpy(_base.copy())


def bit_identical(a, b):
    return torch.equal(a, b)


cfg_raw = N._resolve_config("")
assert cfg_raw is not None, "setup: no OCIO config resolvable, cannot run parity test"
SPACES = N._colorspace_names() or []


def independent_cpu(build_transform_or_args):
    """Build a completely fresh Config + Processor + CPUProcessor, bypassing every cache in nodes.py."""
    fresh_cfg = OCIO.Config.CreateFromBuiltinConfig("studio-config-latest")
    processor = build_transform_or_args(fresh_cfg)
    return processor.getDefaultCPUProcessor()


def apply_independent(img, cpu):
    """Mirrors nodes._apply_processor exactly, but against a processor built with zero caching."""
    arr = img.detach().cpu().numpy().astype(np.float32)
    b, h, w, c = arr.shape
    out = arr.copy()
    for i in range(b):
        rgb = np.ascontiguousarray(out[i, :, :, :3])
        desc = OCIO.PackedImageDesc(rgb, w, h, 3)
        cpu.apply(desc)
        out[i, :, :, :3] = rgb
    return torch.from_numpy(out).to(img.device, img.dtype)


# --------------------------------------------------------------------------- per-node cases
# each case: (label, cache_len_fn, run_once(), run_kwargs, independent transform builder, second (miss) run)

print("=" * 70)
print("OCIOColorSpace")
print("=" * 70)
N._PROCESSOR_CACHE._d.clear()
a = next((s for s in SPACES if "ACES2065" in s), SPACES[0])
b = next((s for s in SPACES if "ACEScg" in s), SPACES[1])
c = next((s for s in SPACES if s not in (a, b)), SPACES[-1])

check(len(N._PROCESSOR_CACHE) == 0, "ColorSpace: cache empty before first call")
node = N.OCIOColorSpace()
out1 = node.convert(IMG, a, b)[0]
check(len(N._PROCESSOR_CACHE) == 1, "ColorSpace: cache has 1 entry after first call")
out2 = node.convert(IMG, a, b)[0]
check(len(N._PROCESSOR_CACHE) == 1, "ColorSpace: cache still 1 entry after repeat call (hit, no rebuild)")
out3 = N.OCIOColorSpace().convert(IMG, a, b)[0]  # fresh node instance
indep = apply_independent(IMG, independent_cpu(lambda cfg: cfg.getProcessor(a, b)))
check(bit_identical(out1, indep), "ColorSpace: cached output bit-identical to independent processor (call 1)")
check(bit_identical(out2, indep), "ColorSpace: cached output bit-identical to independent processor (call 2, hit)")
check(bit_identical(out3, indep), "ColorSpace: cached output bit-identical to independent processor (fresh instance)")

out_diff = node.convert(IMG, a, c)[0]
check(len(N._PROCESSOR_CACHE) == 2, "ColorSpace: different out_colorspace adds a distinct cache entry")
indep_diff = apply_independent(IMG, independent_cpu(lambda cfg: cfg.getProcessor(a, c)))
check(bit_identical(out_diff, indep_diff), "ColorSpace: different-params output matches its own independent processor")
check(not bit_identical(out_diff, out1), "ColorSpace: different-params output differs from the original (no key collision)")
out1_again = node.convert(IMG, a, b)[0]
check(bit_identical(out1_again, indep), "ColorSpace: original entry unchanged after a different key was cached")

print()
print("=" * 70)
print("OCIODisplay")
print("=" * 70)
N._PROCESSOR_CACHE._d.clear()
disp = cfg_raw.getDefaultDisplay()
view1 = cfg_raw.getDefaultView(disp)
views_all = list(cfg_raw.getViews(disp))
view2 = next((v for v in views_all if v != view1), view1)
lin = next((s for s in SPACES if "ACES2065" in s or "lin" in s.lower()), SPACES[0])


def indep_display(cfg, view, invert):
    t = OCIO.DisplayViewTransform(src=lin, display=disp, view=view)
    t.setDirection(OCIO.TRANSFORM_DIR_INVERSE if invert else OCIO.TRANSFORM_DIR_FORWARD)
    return cfg.getProcessor(t)


check(len(N._PROCESSOR_CACHE) == 0, "Display: cache empty before first call")
node = N.OCIODisplay()
out1 = node.run(IMG, lin, disp, view1, False)[0]
check(len(N._PROCESSOR_CACHE) == 1, "Display: cache has 1 entry after first call")
out2 = node.run(IMG, lin, disp, view1, False)[0]
check(len(N._PROCESSOR_CACHE) == 1, "Display: cache still 1 entry after repeat call (hit, no rebuild)")
out3 = N.OCIODisplay().run(IMG, lin, disp, view1, False)[0]
indep = apply_independent(IMG, independent_cpu(lambda cfg: indep_display(cfg, view1, False)))
check(bit_identical(out1, indep), "Display: cached output bit-identical to independent processor (call 1)")
check(bit_identical(out2, indep), "Display: cached output bit-identical to independent processor (call 2, hit)")
check(bit_identical(out3, indep), "Display: cached output bit-identical to independent processor (fresh instance)")

if view2 != view1:
    out_diff = node.run(IMG, lin, disp, view2, False)[0]
    check(len(N._PROCESSOR_CACHE) == 2, "Display: different view adds a distinct cache entry")
    indep_diff = apply_independent(IMG, independent_cpu(lambda cfg: indep_display(cfg, view2, False)))
    check(bit_identical(out_diff, indep_diff), "Display: different-view output matches its own independent processor")
    check(not bit_identical(out_diff, out1), "Display: different-view output differs from the original")
else:
    print("SKIP: only one view on the default display, cannot test a distinct view key")

out_inv = node.run(IMG, lin, disp, view1, True)[0]
check(len(N._PROCESSOR_CACHE) in (2, 3), "Display: invert_direction adds a distinct cache entry")
indep_inv = apply_independent(IMG, independent_cpu(lambda cfg: indep_display(cfg, view1, True)))
check(bit_identical(out_inv, indep_inv), "Display: inverted output matches its own independent processor")
check(not bit_identical(out_inv, out1), "Display: inverted output differs from the forward output")
out1_again = node.run(IMG, lin, disp, view1, False)[0]
check(bit_identical(out1_again, indep), "Display: original entry unchanged after other keys were cached")

print()
print("=" * 70)
print("OCIOCDLTransform")
print("=" * 70)
N._PROCESSOR_CACHE._d.clear()


def indep_cdl(cfg, slope, offset, power, sat, direction):
    t = OCIO.CDLTransform()
    t.setSlope(slope); t.setOffset(offset); t.setPower(power); t.setSat(sat)
    t.setDirection(OCIO.TRANSFORM_DIR_FORWARD if direction == "forward" else OCIO.TRANSFORM_DIR_INVERSE)
    return cfg.getProcessor(t)


check(len(N._PROCESSOR_CACHE) == 0, "CDL: cache empty before first call")
node = N.OCIOCDLTransform()
args1 = (1.1, 1.0, 0.9, 0.02, 0.0, -0.01, 1.0, 1.05, 0.95, 1.0, "forward")
out1 = node.run(IMG, *args1)[0]
check(len(N._PROCESSOR_CACHE) == 1, "CDL: cache has 1 entry after first call")
out2 = node.run(IMG, *args1)[0]
check(len(N._PROCESSOR_CACHE) == 1, "CDL: cache still 1 entry after repeat call (hit, no rebuild)")
out3 = N.OCIOCDLTransform().run(IMG, *args1)[0]
indep = apply_independent(IMG, independent_cpu(
    lambda cfg: indep_cdl(cfg, [1.1, 1.0, 0.9], [0.02, 0.0, -0.01], [1.0, 1.05, 0.95], 1.0, "forward")))
check(bit_identical(out1, indep), "CDL: cached output bit-identical to independent processor (call 1)")
check(bit_identical(out2, indep), "CDL: cached output bit-identical to independent processor (call 2, hit)")
check(bit_identical(out3, indep), "CDL: cached output bit-identical to independent processor (fresh instance)")

args_diff_slope = (1.4, 1.0, 0.9, 0.02, 0.0, -0.01, 1.0, 1.05, 0.95, 1.0, "forward")  # different slope_r
out_diff = node.run(IMG, *args_diff_slope)[0]
check(len(N._PROCESSOR_CACHE) == 2, "CDL: different slope_r adds a distinct cache entry")
indep_diff = apply_independent(IMG, independent_cpu(
    lambda cfg: indep_cdl(cfg, [1.4, 1.0, 0.9], [0.02, 0.0, -0.01], [1.0, 1.05, 0.95], 1.0, "forward")))
check(bit_identical(out_diff, indep_diff), "CDL: different-slope output matches its own independent processor")
check(not bit_identical(out_diff, out1), "CDL: different-slope output differs from the original (no key collision)")
out1_again = node.run(IMG, *args1)[0]
check(bit_identical(out1_again, indep), "CDL: original entry unchanged after a different key was cached")

print()
print("=" * 70)
print("OCIOFileTransform")
print("=" * 70)
_lut_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "_test_cache_parity.cube")
with open(_lut_path, "w") as f:
    f.write("LUT_3D_SIZE 2\n")
    f.write("0.0 0.0 0.0\n1.0 0.0 0.0\n0.0 1.0 0.0\n1.0 1.0 0.0\n")
    f.write("0.0 0.0 1.0\n0.9 0.0 1.0\n0.0 0.9 1.0\n0.9 0.9 0.9\n")
try:
    N._PROCESSOR_CACHE._d.clear()

    def indep_file(cfg, path, interp_name, direction):
        interp = {"linear": OCIO.INTERP_LINEAR, "nearest": OCIO.INTERP_NEAREST,
                  "tetrahedral": OCIO.INTERP_TETRAHEDRAL, "best": OCIO.INTERP_BEST}[interp_name]
        t = OCIO.FileTransform(src=path, interpolation=interp)
        t.setDirection(OCIO.TRANSFORM_DIR_FORWARD if direction == "forward" else OCIO.TRANSFORM_DIR_INVERSE)
        return cfg.getProcessor(t)

    check(len(N._PROCESSOR_CACHE) == 0, "FileTransform: cache empty before first call")
    node = N.OCIOFileTransform()
    out1 = node.run(IMG, _lut_path, "linear", "forward")[0]
    check(len(N._PROCESSOR_CACHE) == 1, "FileTransform: cache has 1 entry after first call")
    out2 = node.run(IMG, _lut_path, "linear", "forward")[0]
    check(len(N._PROCESSOR_CACHE) == 1, "FileTransform: cache still 1 entry after repeat call (hit, no rebuild)")
    out3 = N.OCIOFileTransform().run(IMG, _lut_path, "linear", "forward")[0]
    indep = apply_independent(IMG, independent_cpu(lambda cfg: indep_file(cfg, _lut_path, "linear", "forward")))
    check(bit_identical(out1, indep), "FileTransform: cached output bit-identical to independent processor (call 1)")
    check(bit_identical(out2, indep), "FileTransform: cached output bit-identical to independent processor (call 2, hit)")
    check(bit_identical(out3, indep), "FileTransform: cached output bit-identical to independent processor (fresh instance)")

    out_diff = node.run(IMG, _lut_path, "tetrahedral", "forward")[0]
    check(len(N._PROCESSOR_CACHE) == 2, "FileTransform: different interpolation adds a distinct cache entry")
    indep_diff = apply_independent(IMG, independent_cpu(lambda cfg: indep_file(cfg, _lut_path, "tetrahedral", "forward")))
    check(bit_identical(out_diff, indep_diff), "FileTransform: different-interp output matches its own independent processor")
    check(not bit_identical(out_diff, out1), "FileTransform: different-interp output differs from the original")
    out1_again = node.run(IMG, _lut_path, "linear", "forward")[0]
    check(bit_identical(out1_again, indep), "FileTransform: original entry unchanged after a different key was cached")

    # invalidation: touch the LUT file (new mtime/size) and confirm the cache does NOT return the stale entry
    before_len = len(N._PROCESSOR_CACHE)
    with open(_lut_path, "a") as f:
        f.write("\n# edited\n")
    os.utime(_lut_path, None)
    out_after_edit = node.run(IMG, _lut_path, "linear", "forward")[0]
    check(len(N._PROCESSOR_CACHE) == before_len + 1,
          "FileTransform: editing the LUT file on disk adds a new cache entry (not silently stale)")
    check(bit_identical(out_after_edit, out1),
          "FileTransform: edited file's cache-busted output still matches original content (append was a no-op edit)")
finally:
    if os.path.isfile(_lut_path):
        os.remove(_lut_path)

print()
print("=" * 70)
print("OCIOLookTransform")
print("=" * 70)
looks = [l.getName() for l in cfg_raw.getLooks()]
if not looks:
    print("SKIP: active config has no OCIO looks defined, cannot exercise OCIOLookTransform")
else:
    N._PROCESSOR_CACHE._d.clear()
    look1 = looks[0]
    a = next((s for s in SPACES if "ACES2065" in s), SPACES[0])
    b = next((s for s in SPACES if "sRGB" in s and "Display" in s), SPACES[1])

    def indep_look(cfg, look, invert):
        t = OCIO.LookTransform(src=a, dst=b, looks=look)
        t.setDirection(OCIO.TRANSFORM_DIR_INVERSE if invert else OCIO.TRANSFORM_DIR_FORWARD)
        return cfg.getProcessor(t)

    check(len(N._PROCESSOR_CACHE) == 0, "Look: cache empty before first call")
    node = N.OCIOLookTransform()
    out1 = node.run(IMG, a, b, look1, False)[0]
    check(len(N._PROCESSOR_CACHE) == 1, "Look: cache has 1 entry after first call")
    out2 = node.run(IMG, a, b, look1, False)[0]
    check(len(N._PROCESSOR_CACHE) == 1, "Look: cache still 1 entry after repeat call (hit, no rebuild)")
    out3 = N.OCIOLookTransform().run(IMG, a, b, look1, False)[0]
    indep = apply_independent(IMG, independent_cpu(lambda cfg: indep_look(cfg, look1, False)))
    check(bit_identical(out1, indep), "Look: cached output bit-identical to independent processor (call 1)")
    check(bit_identical(out2, indep), "Look: cached output bit-identical to independent processor (call 2, hit)")
    check(bit_identical(out3, indep), "Look: cached output bit-identical to independent processor (fresh instance)")

    out_none = node.run(IMG, a, b, "(none)", False)[0]
    check(len(N._PROCESSOR_CACHE) == 2, "Look: '(none)' look adds a distinct cache entry")
    indep_none = apply_independent(IMG, independent_cpu(lambda cfg: indep_look(cfg, "", False)))
    check(bit_identical(out_none, indep_none), "Look: '(none)' output matches its own independent processor")
    check(not bit_identical(out_none, out1), "Look: '(none)' output differs from the look-applied output")
    out1_again = node.run(IMG, a, b, look1, False)[0]
    check(bit_identical(out1_again, indep), "Look: original entry unchanged after a different key was cached")

print()
print("=" * 70)
print("Config cache: LRU eviction + bounded size")
print("=" * 70)
N._CONFIG_CACHE._d.clear()
check(len(N._CONFIG_CACHE) == 0, "Config cache: empty before use")
cfgA, keyA = N._resolve_config_keyed("")
check(len(N._CONFIG_CACHE) == 1, "Config cache: 1 entry after first resolve")
cfgA2, keyA2 = N._resolve_config_keyed("")
check(keyA == keyA2 and cfgA is cfgA2, "Config cache: repeat resolve returns the SAME cached object (identity, not just equality)")
check(len(N._CONFIG_CACHE) == 1, "Config cache: still 1 entry after repeat resolve (hit, no rebuild)")

# bounded: push more entries than maxsize (8) through synthetic file keys, confirm it never exceeds maxsize
maxsize = N._CONFIG_CACHE._maxsize
for i in range(maxsize + 5):
    N._CONFIG_CACHE.put(("synthetic", i), object())
check(len(N._CONFIG_CACHE) == maxsize, f"Config cache: bounded at maxsize={maxsize} after overflow")
check(N._CONFIG_CACHE.get(("synthetic", 0)) is None, "Config cache: oldest entry evicted (LRU)")
check(N._CONFIG_CACHE.get(("synthetic", maxsize + 4)) is not None, "Config cache: newest entry retained")

print()
print("=" * 70)
print("Processor cache: LRU eviction + bounded size")
print("=" * 70)
N._PROCESSOR_CACHE._d.clear()
maxsize_p = N._PROCESSOR_CACHE._maxsize
for i in range(maxsize_p + 10):
    N._PROCESSOR_CACHE.put(("synthetic", i), object())
check(len(N._PROCESSOR_CACHE) == maxsize_p, f"Processor cache: bounded at maxsize={maxsize_p} after overflow")
check(N._PROCESSOR_CACHE.get(("synthetic", 0)) is None, "Processor cache: oldest entry evicted (LRU)")
check(N._PROCESSOR_CACHE.get(("synthetic", maxsize_p + 9)) is not None, "Processor cache: newest entry retained")

# move_to_end on hit: touch an old-ish key, then overflow again, confirm the touched key survives
N._PROCESSOR_CACHE._d.clear()
for i in range(maxsize_p):
    N._PROCESSOR_CACHE.put(("k", i), i)
N._PROCESSOR_CACHE.get(("k", 0))  # touch the oldest -> should move to MRU end
for i in range(maxsize_p, maxsize_p + 3):
    N._PROCESSOR_CACHE.put(("k", i), i)
check(N._PROCESSOR_CACHE.get(("k", 0)) == 0, "Processor cache: get() touch protects a key from eviction (move_to_end)")
check(N._PROCESSOR_CACHE.get(("k", 1)) is None, "Processor cache: untouched next-oldest key was evicted instead")

N._PROCESSOR_CACHE._d.clear()
N._CONFIG_CACHE._d.clear()

print()
print("=" * 70)
print("RESULT:", "ALL PASS" if fails == 0 else f"{fails} FAILED")
print("=" * 70)
sys.exit(1 if fails else 0)
