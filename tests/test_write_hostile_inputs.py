# -*- coding: utf-8 -*-
"""OCIO Write must survive inputs an artist can actually produce, and must never answer with a confident lie.

Run:  python tests/test_write_hostile_inputs.py     (no pytest, no ComfyUI, no GPU)

Five defects, all found by an adversarial pass over the write path and all reproduced before being fixed. What
they share is the failure MODE: each one answered a bad situation with something that looked like success.

  1. `metadata` whose `attrs` is valid JSON but not an object killed the render with a bare TypeError /
     ValueError / AttributeError. The socket is forceInput and takes a wire from any source, and the pack's own
     rule is that metadata never stops a render. The existing suite only varied hostile VALUES inside a proper
     attrs dict, so the shape itself was uncovered.
  2. MXF refused the pack's own default rate. `str(23.976)` makes ffmpeg parse 2997/125, which the strict MXF
     muxer rejects outright; MOV and MP4 accepted the odd rational and carried it into the file.
  3. A still-image write with first_frame past the end wrote `name.0999.exr` containing a different frame and
     reported success. The filename is a claim about which frame it is, and the claim was false.
  4. The same clamp underflowed to -1 on an empty batch and died on arr[-1].
  5. raw_data on a VIDEO write still stamped bt709 / iec61966-2-1 colour tags, while raw_data on a STILL
     correctly wrote no colorimetry at all. A confidently mistagged file is worse than an untagged one.

Each check asserts the FIX, and the comment above it names what the old behaviour was, so a reviewer can tell
what regression the check is standing guard over.
"""
import glob
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

TMP = tempfile.mkdtemp(prefix="ocio_hostile_")
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
# Three frames with distinct constant values, so a readback says WHICH frame landed on disk. A uniform batch
# would let a wrong-frame write pass unnoticed - the same trap that made defect 3 invisible for so long.
IMGS = torch.from_numpy(np.stack([np.full((8, 8, 3), v, np.float32) for v in (0.10, 0.50, 0.90)]))
# DNxHD refuses anything under 256x120 ("Input dimensions too small"), so the video checks get their own,
# larger batch. Same three distinct values, for the same reason.
VID = torch.from_numpy(np.stack([np.full((128, 256, 3), v, np.float32) for v in (0.10, 0.50, 0.90)]))
BASE = dict(profile="none", input_colorspace="ACEScg", output_colorspace="ACEScg", video_codec="prores_4444",
            auto_range=False, first_frame=1, last_frame=0, start_number=1, source_start=1, raw_data=False,
            colorspace_in_name=False, auto_colorspace=False, compression="zip", fps=24.0,
            still_format="exr", bit_depth="16f")

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe(path, fields):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=" + ",".join(fields), "-of", "json", path],
                       capture_output=True, text=True, encoding="utf-8")
    try:
        return json.loads(r.stdout)["streams"][0]
    except Exception:
        return {}


# --------------------------------------------------------------- 1. metadata must never stop a render
print("\n1. metadata whose `attrs` is not an object (was: bare TypeError/ValueError/AttributeError)")
SHAPES = [("list of ints", "[1,2,3]"),
          ("string", '"hello"'),
          ("number", "5"),
          ("list of pairs, as JS Object.entries emits", '[["a",1],["b",2]]'),
          ("null", "null"),
          ("nested list", "[[1,[2]],[3]]")]
for label, attrs_json in SHAPES:
    folder = "m_" + label.split()[0]
    md = '{"source":"plate.0001.exr","attrs":%s}' % attrs_json
    try:
        W.write(**BASE, container="sequence", output_folder=folder, filename="q", metadata=md, images=IMGS)
        wrote = len(glob.glob(os.path.join(TMP, folder, "q.*.exr")))
        check(f"attrs = {label}: render completes", True, f"{wrote} frame(s) written")
        check(f"attrs = {label}: pixels actually reached disk", wrote == 3, f"{wrote} of 3")
    except Exception as e:
        check(f"attrs = {label}: render completes", False, f"{type(e).__name__}: {str(e)[:70]}")

# The wrapper being unreadable is a separate, already-handled case; kept here so a fix to one cannot quietly
# break the other.
try:
    W.write(**BASE, container="sequence", output_folder="m_notjson", filename="q",
            metadata="{not json at all", images=IMGS)
    check("metadata that is not JSON at all: render completes", True)
except Exception as e:
    check("metadata that is not JSON at all: render completes", False, f"{type(e).__name__}")

# A well-formed plate must still be carried, or the guard above would be "safe" by throwing everything away.
try:
    good = json.dumps({"source": "plate.0001.exr",
                       "attrs": {"reel_name": "A001R2XY", "shot": "0106", "scene": "S11"}})
    W.write(**BASE, container="sequence", output_folder="m_good", filename="q", metadata=good, images=IMGS)
    side = glob.glob(os.path.join(TMP, "m_good", "*.json"))
    payload = json.load(open(side[0], encoding="utf-8")) if side else {}
    carried = (payload.get("source", {}).get("attrs", {}) or {})
    check("a WELL-FORMED plate is still carried through (guard is not a blanket drop)",
          carried.get("reel_name") == "A001R2XY" and carried.get("shot") == "0106",
          f"reel={carried.get('reel_name')!r} shot={carried.get('shot')!r}")
except Exception as e:
    check("a WELL-FORMED plate is still carried through (guard is not a blanket drop)", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------- 2. NTSC rates as exact rationals
print("\n2. -r must be the exact rational for NTSC rates (was: str(23.976) -> 2997/125, MXF refused it)")
CASES = [(23.976, "24000/1001"), (29.97, "30000/1001"), (59.94, "60000/1001"),
         (47.952, "48000/1001"), (119.88, "120000/1001"),
         (24, "24"), (24.0, "24.0"), (25, "25"), (30, "30"), (48, "48"), (50, "50"), (60, "60")]
for value, want in CASES:
    got = io._fps_arg(value)
    check(f"_fps_arg({value}) == {want}", got == want, f"got {got}")
# A true 24 must never be rewritten as 23.976: that would retime every integer-rate delivery by 0.1%.
check("_fps_arg never turns an integer rate into an NTSC one",
      "1001" not in io._fps_arg(24) and "1001" not in io._fps_arg(30) and "1001" not in io._fps_arg(60))
# BOTH -r sites must go through _fps_arg, and this check is structural on purpose. Only the OUTPUT -r reaches
# the muxer, so reverting the INPUT one to str(fps) leaves every behavioural check green: the rawvideo demuxer
# would be told 2997/125 while the muxer is told 24000/1001, and those differ by a relative 1e-6, far too little
# to drop or duplicate a frame in any clip a test can afford to write. Structural is the only affordable guard
# for a mismatch that only shows up over hours of footage. It is an ADDITION to the behavioural checks below,
# never a substitute: counting occurrences proves the text is present, not that it runs.
_SRC = open(os.path.join(_ROOT, "io_nodes.py"), encoding="utf-8").read()
check("both ffmpeg -r sites go through _fps_arg (input demuxer and output muxer)",
      _SRC.count('"-r", _fps_arg(fps)') == 2 and '"-r", str(fps)' not in _SRC,
      f"_fps_arg sites: {_SRC.count('\"-r\", _fps_arg(fps)')}, raw str(fps) sites: {_SRC.count('\"-r\", str(fps)')}")

for junk in (None, "", "abc"):
    got = io._fps_arg(junk)
    check(f"_fps_arg({junk!r}) degrades to a string instead of raising", isinstance(got, str), got)

if HAVE_FFMPEG:
    for codec in ("dnxhr_hq_mxf", "dnxhr_hq_mxf_opatom"):
        for f in (23.976, 29.97):
            folder = f"r_{codec}_{f}"
            try:
                W.write(**{**BASE, "fps": f, "video_codec": codec}, container="video",
                        output_folder=folder, filename="v", metadata="", images=VID)
                out = glob.glob(os.path.join(TMP, folder, "v*.mxf"))
                rate = probe(out[0], ["r_frame_rate"]).get("r_frame_rate", "") if out else ""
                check(f"{codec} writes at {f}", bool(out), rate)
                check(f"{codec} at {f} carries the canonical rational",
                      rate in ("24000/1001", "30000/1001"), rate)
            except Exception as e:
                check(f"{codec} writes at {f}", False, f"{type(e).__name__}: {str(e)[:70]}")
else:
    print("  (ffmpeg/ffprobe absent - container checks skipped, _fps_arg checks above still ran)")

# --------------------------------------------------------------- 3 & 4. out-of-range and empty batches
print("\n3. still image, first_frame outside the batch (was: wrote a DIFFERENT frame under that name)")
try:
    W.write(**{**BASE, "first_frame": 999}, container="still image", output_folder="oob",
            filename="q", metadata="", images=IMGS)
    stray = glob.glob(os.path.join(TMP, "oob", "*.exr"))
    check("out-of-range still is refused, not silently substituted", False,
          f"wrote {[os.path.basename(x) for x in stray]}")
except RuntimeError as e:
    msg = str(e)
    check("out-of-range still raises RuntimeError", True)
    check("   the message names the frame asked for and the range available",
          "999" in msg and "1-3" in msg, msg[:90])
    check("   nothing was written under a name that would have lied",
          not glob.glob(os.path.join(TMP, "oob", "*.exr")))
except Exception as e:
    check("out-of-range still raises RuntimeError (not a bare builtin)", False, type(e).__name__)

# In-range must keep working, including the frame-numbered name a still grabbed from a sequence gets.
try:
    W.write(**{**BASE, "first_frame": 3}, container="still image", output_folder="inr",
            filename="q", metadata="", images=IMGS)
    got = sorted(glob.glob(os.path.join(TMP, "inr", "*.exr")))
    import OpenEXR
    with OpenEXR.File(got[0]) as fh:
        ch = fh.channels()
        key = next((k for k in ("RGBA", "RGB") if k in ch), None)
        px = np.array(ch[key].pixels, copy=True)
    check("an IN-RANGE still still works", len(got) == 1, os.path.basename(got[0]) if got else "none")
    check("   and it contains the frame that was asked for", abs(float(px[..., 0].mean()) - 0.90) < 0.01,
          f"R={float(px[..., 0].mean()):.4f} (0.90 == frame 3)")
except Exception as e:
    check("an IN-RANGE still still works", False, f"{type(e).__name__}: {str(e)[:70]}")

print("\n4. empty batch (was: IndexError from a clamp underflowing to -1)")
EMPTY = torch.zeros((0, 8, 8, 3))
for container in ("still image", "sequence"):
    try:
        W.write(**BASE, container=container, output_folder="e_" + container[:4],
                filename="q", metadata="", images=EMPTY)
        check(f"empty batch, {container}: refused", False, "no exception raised")
    except RuntimeError as e:
        check(f"empty batch, {container}: RuntimeError in words", True, str(e)[:60])
    except Exception as e:
        check(f"empty batch, {container}: RuntimeError, not {type(e).__name__}", False, str(e)[:60])

# --------------------------------------------------------------- 5. raw_data must not claim a gamut
print("\n5. raw_data on video must not claim a colorspace (was: bt709 stated confidently)")
check("_video_color_tags(None) emits no tags at all", io._video_color_tags(None) == [],
      str(io._video_color_tags(None))[:60])
check("_video_color_tags('') still lands on the documented default",
      "bt709" in io._video_color_tags(""), "empty string is a caller that could not name a space, not raw_data")
tagged = io._video_color_tags("Rec.2100-HLG - Display")
check("_video_color_tags on a real space is unchanged",
      "bt2020" in tagged and "arib-std-b67" in tagged)

if HAVE_FFMPEG:
    for label, raw, want_tagged in (("raw_data=False", False, True), ("raw_data=True", True, False)):
        folder = "raw_" + str(raw)
        try:
            W.write(**{**BASE, "raw_data": raw, "output_colorspace": "Rec.2100-HLG - Display"},
                    container="video", output_folder=folder, filename="v", metadata="", images=VID)
            out = glob.glob(os.path.join(TMP, folder, "v*.mov"))
            t = probe(out[0], ["color_space", "color_transfer", "color_primaries"]) if out else {}
            has = bool(t.get("color_primaries")) or bool(t.get("color_transfer"))
            check(f"{label}: colour tags {'present' if want_tagged else 'ABSENT'}", has == want_tagged, str(t))
            if want_tagged:
                check("   and they are the HLG ones, not the sRGB default",
                      t.get("color_transfer") == "arib-std-b67" and t.get("color_primaries") == "bt2020", str(t))
        except Exception as e:
            check(f"{label}: write completes", False, f"{type(e).__name__}: {str(e)[:70]}")
else:
    print("  (ffmpeg/ffprobe absent - container checks skipped)")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
