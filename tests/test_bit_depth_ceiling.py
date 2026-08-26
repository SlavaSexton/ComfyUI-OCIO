# -*- coding: utf-8 -*-
"""Bit-depth ceiling guard for OCIO Write: this pack exists so a delivery does not silently lose precision, and
until this file there was no test that would catch a codec, a still format, or an HDR colorspace quietly
landing at 8 bits.

Run:  python tests/test_bit_depth_ceiling.py     (no pytest, no ComfyUI server, no GPU; ffmpeg/ffprobe needed
for batteries 1 and 4 - SKIPs those two cleanly and exits 0 if the tools are not on PATH, the still/EXR
batteries below still run without them)

Four batteries, each against the REAL node entry point (OCIOWrite().write()), never a re-implementation of the
encoder table:

  1. every video_codec: encode a short clip THROUGH THE NODE, read it back with ffprobe, assert the pix_fmt
     AND bits_per_raw_sample MEASURED here on 2026-08-13 (not copied from io_nodes.py's own comment, and not
     compared against the front-end label the way tests/test_codec_ext_parity.py does it - see the note above
     EXPECT_VIDEO for why that is a materially different, and weaker, guarantee).
  2. stills: a wide gradient at each still bit depth, counted for distinct code values on read-back. This
     catches a quantization a header would not show: a file whose IHDR says 16-bit but whose pixels were
     rounded to 256 code values before being re-packed as uint16 passes a header check and fails this one.
  3. EXR 16f / 32f: negative and >1.0 values must round-trip, read back from disk, not merely accepted by the
     writer without raising.
  4. HDR: Rec.2100-HLG - Display must not be written into a container thinner than BT.2100's floor (ITU-R
     BT.2100-2 section 8 / Table 4: HLG systems are specified at 10 or 12 bits per sample) - checked against
     EVERY codec the node offers, because nothing in this node currently ties the colorspace choice to the
     codec choice: video_codec and output_colorspace are two independent combo widgets, and an artist (or an
     API prompt) is free to pair Rec.2100-HLG with an 8-bit codec.

WHY THIS IS NOT A DUPLICATE of tests/test_codec_ext_parity.py's depth section. That file (a) hand-assembles
its own ffmpeg command from io_nodes._video_encoder_args() plus a decoded-from-ffv1 source, never calling
OCIOWrite.write() or even save_video() - so a defect introduced in save_video()'s own command assembly (the
order it combines encoder args, colour tags, metadata args, timecode, mux flags) would not be exercised at
all; and (b) asserts the measured depth against the FRONT-END LABEL (JS_BITS), not a fixed expectation - two
sources that drift TOGETHER (someone "simplifies" a codec's pix_fmt and updates the JS label to match) stay
green there. This file writes through the node's real write() and freezes independently-measured numbers, so
either kind of drift is caught.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


try:
    import numpy as np
    import torch
except ImportError as e:
    print(f"SKIP: {e} - this test needs numpy and torch")
    sys.exit(0)

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

TMP = tempfile.mkdtemp(prefix="ocio_bitdepth_")
_fp = types.ModuleType("folder_paths")
_fp.get_output_directory = _fp.get_temp_directory = _fp.get_input_directory = lambda: TMP
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
io = sys.modules["ocio_pkg.io_nodes"]

W = io.OCIOWrite()
BASE = dict(profile="none", input_colorspace="ACEScg", output_colorspace="ACEScg", video_codec="prores_4444",
            auto_range=False, first_frame=1, last_frame=0, start_number=1, source_start=1, raw_data=False,
            colorspace_in_name=False, auto_colorspace=False, compression="zip", fps=24.0,
            still_format="exr", bit_depth="16f")


def probe(path, fields):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=" + ",".join(fields), "-of", "json", path],
                       capture_output=True, text=True, encoding="utf-8")
    try:
        return json.loads(r.stdout)["streams"][0]
    except Exception:
        return {}


def measured_bits(info):
    """bits_per_raw_sample when ffprobe reports it, else inferred from the pix_fmt name - the SAME fallback
    tests/test_codec_ext_parity.py already uses. Needed because this build's ffprobe reports NO
    bits_per_raw_sample field at all for hevc/yuv420p (measured 2026-08-13: the key is absent from the
    stream), while h264/yuv420p on the SAME build does report it ('8') - a codec-specific ffprobe quirk, not
    a missing field in general, so the pix_fmt-name fallback is only needed for the one codec that hits it."""
    pix = info.get("pix_fmt", "") or ""
    raw = info.get("bits_per_raw_sample")
    if raw not in (None, "", "N/A"):
        return int(raw)
    return 12 if "12" in pix else (10 if "10" in pix else 8)


# Frames for the video batteries. 256x128 clears DNxHR/DNxHD's own floor ("Input dimensions too small" under
# 256x120), and both axes are even, which yuv420p subsampling needs.
VID = torch.from_numpy(np.stack([np.full((128, 256, 3), v, np.float32) for v in (0.10, 0.50, 0.90)]))

# ---------------------------------------------------------------------------------------------------------
# MEASURED 2026-08-13, one real encode per codec through OCIOWrite().write(), read back with ffprobe - not
# copied from io_nodes.py's _video_encoder_args comment, precisely so this file cannot pass by restating the
# thing it is meant to check. (pix_fmt, bits_per_raw_sample-or-inferred).
EXPECT_VIDEO = {
    "prores_4444":         ("yuv444p12le", 12),
    "prores_422hq":        ("yuv422p10le", 10),
    "prores_422":          ("yuv422p10le", 10),
    "dnxhr_hq":            ("yuv422p",      8),
    "h264":                ("yuv420p",      8),
    "hevc":                ("yuv420p",      8),
    "dnxhr_hq_mxf":        ("yuv422p",      8),
    "dnxhr_hq_mxf_opatom": ("yuv422p",      8),
    "dnxhr_hqx":           ("yuv422p10le", 10),
    "dnxhr_444":           ("yuv444p10le", 10),
    # MXF above 8 bits, added after this file first ran and reported that the only MXF route was 8-bit.
    "prores_4444_mxf":     ("yuv444p12le", 12),
    "prores_4444xq_mxf":   ("yuv444p12le", 12),
    "dnxhr_hqx_mxf":       ("yuv422p10le", 10),
    "dnxhr_444_mxf":       ("yuv444p10le", 10),
    # The one encode in this build that carries 12 real bits. ProRes reads back as 12 because the FORMAT is
    # 12-bit, but ffmpeg's ProRes encoders advertise only 10-bit pixel formats and decline a 12-bit request
    # out loud ("auto-selecting format 'yuv444p10le'"), so the label there describes the container. libx265
    # writes the samples.
    "hevc_444_12":         ("yuv444p12le", 12),
    "prores_4444xq":       ("yuv444p12le", 12),   # reads back as the format's 12, encodes 10 like every ProRes
    "ffv1":                ("gbrp16le",    16),   # the one that returns the 16-bit input unchanged
}

# Profiles that are 8-bit by definition. Asking them for a BT.2100 delivery is now refused rather than written,
# so battery 4 expects an exception from these and a 10-bit-or-better file from everything else. Kept as its own
# list rather than read from io_nodes, so that deleting the guard there makes this file fail instead of agreeing.
HDR_REFUSERS = {"dnxhr_hq", "dnxhr_hq_mxf", "dnxhr_hq_mxf_opatom"}

# =============================================================================================== 1. video codecs
print("1. every video_codec: pix_fmt and bit depth MEASURED through OCIOWrite().write(), not restated from a table")
NODE_CODECS = list(io.OCIOWrite.INPUT_TYPES()["required"]["video_codec"][0])
check("the codec list this file measures is exactly the list the node offers (no codec added on one side only)",
      sorted(NODE_CODECS) == sorted(EXPECT_VIDEO),
      f"node offers {sorted(NODE_CODECS)}, this file has expectations for {sorted(EXPECT_VIDEO)}")

if HAVE_FFMPEG:
    for codec, (want_pix, want_bits) in EXPECT_VIDEO.items():
        try:
            res = W.write(**{**BASE, "video_codec": codec}, container="video", output_folder=f"vc_{codec}",
                          filename="v", metadata="", images=VID)
            path = res["result"][0]
            info = probe(path, ["pix_fmt", "bits_per_raw_sample", "codec_name"])
            got_pix = info.get("pix_fmt", "")
            got_bits = measured_bits(info)
            check(f"{codec}: pix_fmt is {want_pix!r}", got_pix == want_pix, f"got {got_pix!r}")
            check(f"{codec}: bit depth is {want_bits} (not silently thinner)", got_bits == want_bits,
                  f"got {got_bits}-bit (bits_per_raw_sample field: {info.get('bits_per_raw_sample')!r}, "
                  f"pix_fmt {got_pix!r})")
        except Exception as e:
            check(f"{codec}: write + probe completes", False, f"{type(e).__name__}: {str(e)[:120]}")
else:
    print("  SKIP (ffmpeg/ffprobe not on PATH)")

# =============================================================================================== 2. still levels
# EXR IS DELIBERATELY NOT IN THIS BATTERY. Its bit_depth combo only ever offers 16f/32f (no 8-bit EXR exists to
# fall back to), so "did it quietly become 8-bit" is not a question EXR can even be asked; EXR's actual risk -
# whether an out-of-range VALUE survives at all - is battery 3 below. This battery is for the formats that
# genuinely have an integer ceiling to fall through: PNG and TIFF both offer an 8-bit mode, so a bug that
# quantizes a "16" request down to 256 code values before re-packing as uint16 is a real, silent possibility a
# header check (IHDR bit depth, TIFF BitsPerSample tag) would not catch - only counting the levels that
# actually made it to disk does.
print("\n2. stills: a wide gradient must show the level count its bit depth promises, not merely claim it in "
      "the header")
GRAD_W = 2048   # 2048 distinct input code values: spaced ~32x finer than a 16-bit quantization step (1/65535)
                # and ~8x finer than an 8-bit step (1/255) at the GAP BETWEEN ADJACENT SAMPLES - fine enough that
                # 16-bit resolves them individually and 8-bit is FORCED to collapse most of them, which is the
                # discriminator this battery needs.
_ramp = np.tile(np.linspace(0.0, 1.0, GRAD_W, dtype=np.float32)[None, :, None], (16, 1, 3))
GRAD = torch.from_numpy(_ramp[None].copy())


def write_grad(fmt, bd, tag):
    r = W.write(**{**BASE, "still_format": fmt, "bit_depth": bd}, container="still image",
               output_folder=f"lvl_{tag}", filename="g", metadata="", images=GRAD)
    return r["result"][0]


# PNG: cv2 is the reader for BOTH depths, not Pillow. Pillow has no 48-bit RGB mode and silently hands back
# uint8 for a real 16-bit RGB PNG (documented in tests/test_metadata_all_formats.py's _png_bitdepth docstring,
# measured there on Pillow 12.2.0), which would UNDER-report a CORRECT 16-bit file as 8-bit - a false negative
# this guard must not manufacture. R, G and B are read identically here because the fixture puts the same
# gradient in all three channels, so cv2's BGR channel order does not matter for a level count.
import cv2   # noqa: E402

p8 = write_grad("png", "8", "png8")
a8 = cv2.imread(p8, cv2.IMREAD_UNCHANGED)
check("png bit_depth=8: really is a uint8 array on read-back", a8.dtype == np.uint8, str(a8.dtype))
u8 = len(np.unique(a8[..., 0]))
check("png bit_depth=8: at most 256 distinct code values (the real ceiling of the format, not a bug)",
      u8 <= 256, f"{u8} distinct")
check("png bit_depth=8: and close to the full 256 (the fixture actually spans 0..1)", u8 >= 200, f"{u8} distinct")

p16 = write_grad("png", "16", "png16")
a16 = cv2.imread(p16, cv2.IMREAD_UNCHANGED)
check("png bit_depth=16: really is a uint16 array on read-back", a16.dtype == np.uint16, str(a16.dtype))
u16 = len(np.unique(a16[..., 0]))
check("png bit_depth=16: SIGNIFICANTLY more than 256 distinct levels (>=1000 of 2048 possible)",
      u16 >= 1000, f"{u16} distinct")
check("png bit_depth=16 out-resolves bit_depth=8 on the identical fixture",
      u16 > u8, f"16-bit={u16} vs 8-bit={u8}")

# TIFF: same fixture, same question, tifffile as the reader - it round-trips dtype exactly (see
# tests/test_metadata_all_formats.py check_tiff()).
import tifffile   # noqa: E402

pt8 = write_grad("tiff", "8", "tiff8")
at8 = tifffile.imread(pt8)
check("tiff bit_depth=8: really is uint8 on read-back", at8.dtype == np.uint8, str(at8.dtype))
ut8 = len(np.unique(at8[..., 0]))
check("tiff bit_depth=8: at most 256 distinct code values", ut8 <= 256, f"{ut8} distinct")

pt16 = write_grad("tiff", "16", "tiff16")
at16 = tifffile.imread(pt16)
check("tiff bit_depth=16: really is uint16 on read-back", at16.dtype == np.uint16, str(at16.dtype))
ut16 = len(np.unique(at16[..., 0]))
check("tiff bit_depth=16: SIGNIFICANTLY more than 256 distinct levels (>=1000 of 2048 possible)",
      ut16 >= 1000, f"{ut16} distinct")

# =============================================================================================== 3. EXR HDR values
print("\n3. EXR 16f / 32f: negative and >1.0 values must survive, read back from disk (not just accepted by "
      "the writer without raising)")
# NOT A FULL DUPLICATE of tests/test_write_output.py, which already puts an out-of-range pixel (-0.25, 1.75, 3.5)
# through 16f/32f and asserts it is not clipped - but it calls io_nodes._save_still() DIRECTLY, never
# OCIOWrite.write(). That proves the low-level writer does not clip; it does not prove the NODE'S OWN entry
# point does not clip somewhere upstream of that call (the OCIO conversion, a range guard added later, etc).
# This battery re-confirms the same fact through W.write() - the path an artist or an API prompt actually
# uses - which is the gap this whole file exists to close for every battery, not just this one.
#
# raw_data=True, so nothing en route can be blamed on the OCIO transform: this battery is about whether the
# FILE FORMAT clips, not about colour-management math. (from==to in BASE is already an identity conversion -
# see io_nodes._convert - so raw_data is not doing extra work here beyond documenting the intent; it is used
# because it is the artist-facing, documented way to say "write these pixels exactly": raw_data's own contract
# in io_nodes.py promises no gamut conversion and no re-tagging.)
HDR_VALS = {"R": -0.5, "G": 2.5, "B": 0.5}   # -0.5 and 2.5 are exact in float16 (both a power of two times a
                                              # small integer), so any readback mismatch is a REAL clip/quantize,
                                              # not float16 rounding noise masquerading as one.
_px = np.zeros((1, 8, 8, 3), np.float32)
_px[0, ..., 0], _px[0, ..., 1], _px[0, ..., 2] = HDR_VALS["R"], HDR_VALS["G"], HDR_VALS["B"]
HDR = torch.from_numpy(_px)

import OpenEXR   # noqa: E402

for bd in ("16f", "32f"):
    try:
        res = W.write(**{**BASE, "still_format": "exr", "bit_depth": bd, "raw_data": True},
                      container="still image", output_folder=f"hdr_{bd}", filename="h", metadata="", images=HDR)
        saved = res["result"][0]
        with OpenEXR.File(saved) as f:
            ch = f.channels()
            key = next((k for k in ("RGBA", "RGB") if k in ch), None)
            arr = np.array(ch[key].pixels, copy=True)
        want_dtype = np.float16 if bd == "16f" else np.float32
        check(f"exr {bd}: channel dtype on disk is {want_dtype.__name__}", arr.dtype == want_dtype, str(arr.dtype))
        for i, (name, want) in enumerate(HDR_VALS.items()):
            got = float(arr[..., i].mean())
            check(f"exr {bd}: {name}={want} survives (not clamped into [0,1])", abs(got - want) < 1e-3,
                  f"got {got:.6f}")
        r_mean, g_mean = float(arr[..., 0].mean()), float(arr[..., 1].mean())
        check(f"exr {bd}: the negative sample is still negative on disk", r_mean < 0, f"R={r_mean:.6f}")
        check(f"exr {bd}: the >1.0 sample is still >1.0 on disk", g_mean > 1.0, f"G={g_mean:.6f}")
    except Exception as e:
        check(f"exr {bd}: write + read-back completes", False, f"{type(e).__name__}: {str(e)[:120]}")

# =============================================================================================== 4. HDR bit floor
print("\n4. HDR: Rec.2100-HLG - Display must not be claimed over a container thinner than BT.2100's floor "
      "(ITU-R BT.2100-2: HLG systems are specified at 10 or 12 bits per sample)")
# EVERY codec, not just the 8-bit ones: video_codec and output_colorspace are two independent combo widgets on
# this node (confirmed by reading INPUT_TYPES and write() - nothing cross-validates them), so an artist or an
# API prompt can pair ANY codec with Rec.2100-HLG. The 10/12-bit codecs are a control: if this guard fails for
# THEM too, the guard itself is broken, not the pack.
if HAVE_FFMPEG:
    for codec, (want_pix, _want_bits) in EXPECT_VIDEO.items():
        if codec in HDR_REFUSERS:
            # 8-bit by profile, so no pixel format saves it: the only honest outcomes are refusing, or writing
            # a file that states a standard it cannot hold. Refusing is what it does, and the message has to
            # name a way forward or it just blocks the artist.
            try:
                W.write(**{**BASE, "video_codec": codec, "output_colorspace": "Rec.2100-HLG - Display"},
                        container="video", output_folder=f"hlg_{codec}", filename="v", metadata="", images=VID)
                check(f"{codec}: refuses BT.2100 rather than writing 8-bit HDR", False,
                      "it wrote the file instead of refusing")
            except RuntimeError as e:
                msg = str(e)
                check(f"{codec}: refuses BT.2100 rather than writing 8-bit HDR", True, msg[:70])
                check(f"   the refusal names a codec that can do it",
                      any(alt in msg for alt in ("hqx", "prores_4444")), msg[:90])
            except Exception as e:
                check(f"{codec}: refuses with a RuntimeError, not {type(e).__name__}", False, str(e)[:80])
            continue
        try:
            res = W.write(**{**BASE, "video_codec": codec, "output_colorspace": "Rec.2100-HLG - Display"},
                          container="video", output_folder=f"hlg_{codec}", filename="v", metadata="", images=VID)
            path = res["result"][0]
            info = probe(path, ["pix_fmt", "bits_per_raw_sample", "color_primaries", "color_transfer", "color_space"])
            got_pix = info.get("pix_fmt", "")
            got_bits = measured_bits(info)
            claims_hdr = info.get("color_primaries") == "bt2020" and info.get("color_transfer") == "arib-std-b67"
            check(f"{codec}: HLG tags actually landed (bt2020/arib-std-b67), so the guard below means something",
                  claims_hdr,
                  f"got primaries={info.get('color_primaries')!r} transfer={info.get('color_transfer')!r}")
            check(f"{codec}: NOT both HLG-tagged and under BT.2100's 10-bit floor",
                  not (claims_hdr and got_bits < 10),
                  f"pix_fmt={got_pix!r} bits={got_bits} - HDR claimed via bt2020/arib-std-b67 tags but the "
                  f"container only carries {got_bits} bits per sample; ITU-R BT.2100 requires 10 or 12")
        except Exception as e:
            check(f"{codec}: write + probe completes", False, f"{type(e).__name__}: {str(e)[:120]}")
else:
    print("  SKIP (ffmpeg/ffprobe not on PATH)")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
