# -*- coding: utf-8 -*-
"""A frame past the HEVC Level 6.x ceiling must be called out, because x265 mislabels it.

Run:  python tests/test_hevc_level_ceiling.py     (needs ffmpeg; no ComfyUI server, no GPU)
      Stop ComfyUI first: a running server holds resources and makes this suite fail at random.

WHY THIS FILE EXISTS. HEVC levels top out at 35 651 584 luma samples per frame across Levels 6, 6.1 and 6.2
(H.265 Table A.8). The standard gained Levels 6.3 and 7.x in 2023, and 16384x8192 is conformant at Level 7.0 -
but x265 does not implement them. Past the 6.x ceiling it stamps the file **Level 8.5**, the "decoder, work it
out yourself" value, which hardware decoders are not obliged to play and generally will not.

Measured on this machine rather than read off a table:

    640x480     ->  Level-3
    8192x4320   ->  Level-6     (35 389 440 samples, just under the ceiling)
    16384x8192  ->  Level-8.5   (134 217 728 samples)

The spec number and the observed switch land on the same boundary, which is why the threshold in the code is
that number and not a rounded resolution.

The pack WARNS rather than refuses. The file is valid and decodes in software, and a dome or large-format
plate may be exactly what someone means to write. What they must not do is discover at playback time that no
hardware will take it.
"""
import importlib.util
import io as _io
import logging
import os
import shutil
import sys
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CEILING = 35651584          # H.265 Table A.8, MaxLumaPs shared by Levels 6, 6.1, 6.2
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
if not shutil.which("ffmpeg"):
    print("SKIP: ffmpeg not on PATH")
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

_buf = _io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(_buf))
logging.getLogger().setLevel(logging.WARNING)


def warned_for(w, h, codec):
    """Did the level warning fire? The encode itself is allowed to fail - a 16K frame is not being written for
    real here, only walked far enough into save_video to pass (or not pass) the check."""
    _buf.truncate(0)
    _buf.seek(0)
    try:
        IO.save_video(np.zeros((1, h, w, 3), np.float32), "probe.mp4", codec, 24.0,
                      "Rec.1886 Rec.709 - Display")
    except Exception:
        pass
    return "Level 8.5" in _buf.getvalue()


print(f"the boundary is the spec number, {CEILING:,} luma samples, not a rounded resolution")
check("8192x4352 sits exactly ON the ceiling and is NOT warned about",
      not warned_for(8192, 4352, "hevc"), f"{8192 * 4352:,} samples")
check("16384x8192 is past it and IS warned about",
      warned_for(16384, 8192, "hevc"), f"{16384 * 8192:,} samples")
check("hevc_444_12 is held to the same ceiling", warned_for(16384, 8192, "hevc_444_12"))

print("\nand the warning belongs to HEVC alone - other codecs have their own limits, not this one")
for codec in ("prores_4444", "dnxhr_hq", "h264", "ffv1"):
    check(f"{codec} at 16384x8192 is not given the HEVC level warning",
          not warned_for(16384, 8192, codec))

print("\nthe threshold in the code is the one from the standard")
src = open(os.path.join(_ROOT, "io_nodes.py"), encoding="utf-8").read()
check("35651584 appears in io_nodes.py, so the number was not rounded away",
      str(CEILING) in src)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
