"""Regression: the video colorspace guess, and the 32-bit trap in DWA compression.

Run:  python tools/test_video_cs_and_dwa.py     (no pytest, no ComfyUI server, no GPU)

Both defects here were found by an adversarial review on 2026-08-13 and both were live.

1. A NAMED TRANSFER MUST VETO THE CONTAINER GUESS.
   An SDR ProRes / DNxHD, or anything in an MXF, is read as Rec.1886 Rec.709 rather than sRGB, because a
   post codec in a professional container is a camera or mastering file. That rule is right for an UNTAGGED
   file - a real DaVinci Resolve MXF reports color_space=bt709 with primaries and transfer both 'unknown' -
   but it was written as an `or` chain, so `color_transfer` could never overrule it. A file that explicitly
   declared iec61966-2-1 (sRGB), linear or log100 still came back Rec.1886. That is wrong PIXELS: read()
   feeds this value straight into the conversion. Measured cost on a ColorChecker: dE2000 max 3.58, mean
   2.28, 15 of 24 patches past dE 2.0.

2. DWA QUANTISES FLOAT32 TO HALF BEFORE COMPRESSING, so a 32f file written with dwaa/dwab carries half
   precision while its header still says `float`. OpenEXR's own ImfDwaCompressor says so: "When dealing with
   FLOAT source buffers, we first quantize the source to HALF and continue down as we would for HALF
   source." Measured here rather than quoted. DWAA is the pack's default compression, so the trap is one
   click away: pick 32f for precision, keep the default, get half. It is not blocked - a review copy may
   want it - but it must never be silent.
"""
import importlib.util
import os
import sys
import tempfile
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import numpy as np
import torch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"   {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def _load(tmp):
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


tmp = tempfile.mkdtemp(prefix="ocio_cs_dwa_")
io = _load(tmp)

print("a transfer tag that NAMES a curve overrules the container guess")
POST = {"codec_name": "prores", "color_primaries": "bt709", "color_space": "bt709"}
for trc in ("iec61966-2-1", "linear", "log100", "log316", "bt470m", "smpte428"):
    got = io._video_input_cs(dict(POST, color_transfer=trc), ".mov")
    check(f"prores tagged {trc:14} is NOT claimed as Rec.1886", got != "Rec.1886 Rec.709 - Display", got)

print("\n...and an UNTAGGED post file still gets Rec.1886, which is the whole point of the rule")
for label, info, ext in (
    ("Resolve MXF, both unknown", {"codec_name": "prores", "color_transfer": "unknown",
                                   "color_primaries": "unknown", "color_space": "bt709"}, ".mxf"),
    ("ProRes .mov, tags absent", {"codec_name": "prores", "color_transfer": "", "color_primaries": "",
                                  "color_space": "bt709"}, ".mov"),
    ("DNxHD in an mxf", {"codec_name": "dnxhd", "color_transfer": "unknown", "color_primaries": "bt709",
                         "color_space": "bt709"}, ".mxf"),
    ("explicitly bt709 transfer", {"codec_name": "prores", "color_transfer": "bt709",
                                   "color_primaries": "bt709", "color_space": "bt709"}, ".mov"),
):
    got = io._video_input_cs(info, ext)
    check(f"{label:28} -> Rec.1886", got == "Rec.1886 Rec.709 - Display", got)

print("\nan ordinary web deliverable is untouched by any of this")
for label, info, ext in (
    ("h264 mp4", {"codec_name": "h264", "color_transfer": "bt709", "color_primaries": "bt709",
                  "color_space": "bt709"}, ".mp4"),
    ("hevc mp4", {"codec_name": "hevc", "color_transfer": "unknown", "color_primaries": "unknown",
                  "color_space": "unknown"}, ".mp4"),
):
    got = io._video_input_cs(info, ext)
    check(f"{label:28} -> sRGB - Display", got == "sRGB - Display", got)

print("\nHDR transfers still win over everything, including the post-codec rule")
for label, info in (
    ("PQ", {"codec_name": "prores", "color_transfer": "smpte2084", "color_primaries": "bt2020",
            "color_space": "bt2020nc"}),
    ("HLG", {"codec_name": "prores", "color_transfer": "arib-std-b67", "color_primaries": "bt2020",
             "color_space": "bt2020nc"}),
):
    got = io._video_input_cs(info, ".mxf")
    check(f"{label:28} -> a Rec.2100 space", "2100" in got, got)

print("\nDWA on a 32f write is REPORTED, because the file says float and carries half")
W = io.OCIOWrite()
rng = np.random.default_rng(3)
img = torch.from_numpy((rng.random((1, 64, 64, 3), dtype=np.float32) * 3.0 - 0.4).astype(np.float32))
BASE = dict(profile="none", from_colorspace="ACEScg", output_colorspace="ACEScg", container="sequence",
            still_format="exr", video_codec="prores_4444", auto_range=False, first_frame=1, last_frame=0,
            start_number=1, source_start=1, raw_data=False, colorspace_in_name=False, auto_colorspace=False,
            fps=24.0, images=img)
NEEDLE = "quantises float32 to half"


def note_of(depth, comp, folder):
    r = W.write(**BASE, bit_depth=depth, compression=comp, output_folder=folder, filename="q")
    return ((r.get("ui") or {}).get("meta") or [""])[0], r["result"][0]


n_dwaa, p_dwaa = note_of("32f", "dwaa", "n1")
n_dwab, _ = note_of("32f", "dwab", "n2")
n_zip, p_zip = note_of("32f", "zip", "n3")
n_half, _ = note_of("16f", "dwaa", "n4")
check("32f + dwaa says so", NEEDLE in n_dwaa, n_dwaa[:70])
check("32f + dwab says so", NEEDLE in n_dwab, n_dwab[:70])
check("32f + zip stays quiet", NEEDLE not in n_zip)
check("16f + dwaa stays quiet (half was asked for)", NEEDLE not in n_half)

print("\n...and the warning is TRUE, measured on the written files")
import cv2
a_dwaa = cv2.imread(p_dwaa, cv2.IMREAD_UNCHANGED).astype(np.float32)
a_zip = cv2.imread(p_zip, cv2.IMREAD_UNCHANGED).astype(np.float32)


def off_half_grid(a):
    """Values that no float16 can represent - the proof that a file really carries 32-bit precision."""
    return int((a.astype(np.float16).astype(np.float32) != a).sum())


off_dwaa, off_zip = off_half_grid(a_dwaa), off_half_grid(a_zip)
check("a 32f/zip file carries values half cannot hold", off_zip > 0, f"{off_zip} samples")
check("a 32f/dwaa file carries NONE - it is half", off_dwaa == 0, f"{off_dwaa} samples")

if FAILS:
    print(f"\n{len(FAILS)} FAILED: " + ", ".join(FAILS))
    raise SystemExit(1)
print("\nall checks passed")
