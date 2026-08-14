# ComfyUI-OCIO - Read / Write nodes, modelled on The Foundry Nuke's Read and Write.
# @author Slava Sexton
#
# OCIO Read : load a still / image sequence / video, tell it which colorspace the file is in, and the IMAGE
#             output is already converted to the working colorspace (default sRGB - Display, ComfyUI's space).
#             No separate "convert" toggle: input colorspace -> output colorspace, always. fps comes from the
#             video metadata. Defaults follow the file type (EXR -> ACEScg, JPG/PNG/TIFF -> sRGB - Display).
# OCIO Write: save an IMAGE batch as a still / sequence (EXR / TIFF / PNG) or a video (ProRes / DNxHR / h264 /
#             hevc). from_colorspace (ComfyUI's working space) -> out colorspace (the file's space). The format
#             drives the right colorspace default (EXR -> ACEScg, PNG/TIFF -> sRGB). Folder + name; the
#             sequence numbering is added automatically. A node preview shows the written frame + its
#             colorspace, so a wrong pick (sRGB vs ACEScg) is visible at a glance.
#
# EXR is written directly by cv2 here, NOT by ComfyUI's SaveImage, so ComfyUI's lack of EXR-sequence support
# does not apply. Foundation (confirmed 2026-06-30): cv2 4.13 (EXR/TIFF), tifffile, Pillow, ffmpeg full build
# (prores_ks 4444/HQ, dnxhd, libx264/5). OCIO 2.5.2 ACES studio config for color. EXR needs
# OPENCV_IO_ENABLE_OPENEXR=1 in the server environment (set on launch).

import os
import tempfile
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import glob
import logging                                   # range-loss warnings, the same channel nodes.py uses
import re
import shutil
import struct
import binascii                                  # PNG chunk CRC-32; see _png_itxt_chunk
import subprocess
import hashlib
from fractions import Fraction

import numpy as np
import torch

# Video output for OCIO Read / Player so their frame batch can feed VIDEO-typed inputs (Wan/Kling/Grok/Topaz
# edit nodes, SaveVideo, etc. - those take VIDEO, not IMAGE). Guarded: an old ComfyUI without comfy_api's video
# type just gets a None VIDEO slot instead of an import crash. Added 2026-07-03.
try:
    from comfy_api.latest import InputImpl as _CA_IMPL, Types as _CA_TYPES
    _HAS_VIDEO_API = True
except Exception:
    _HAS_VIDEO_API = False


def _make_video(images, fps):
    """Build a ComfyUI VIDEO object from an IMAGE batch + fps (so OCIO Read/Player can feed video nodes).
    None if the comfy_api video type is unavailable (very old ComfyUI) - the output slot then stays empty."""
    if not _HAS_VIDEO_API or images is None:
        return None
    try:
        r = float(fps) if fps and float(fps) > 0 else 24.0
        return _CA_IMPL.VideoFromComponents(_CA_TYPES.VideoComponents(images=images, frame_rate=Fraction(r).limit_denominator(600000)))
    except Exception:
        return None

try:
    import cv2
except Exception:
    cv2 = None
try:
    import tifffile
except Exception:
    tifffile = None
from PIL import Image

from .nodes import (_apply_processor, _cached_cpu_processor, _colorspace_names,
                    _combo_or_string, _input_dir, _logc3_to_lin, _logc4_to_lin,
                    _require_ocio, _resolve_config_keyed, _scan_files, _video_unwrap)

try:
    import folder_paths
except Exception:
    folder_paths = None

_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"     # relies on ffmpeg being on PATH (see README)
# ffprobe sits beside ffmpeg; derive only the basename so a directory containing "ffmpeg" is left intact.
_dir = os.path.dirname(_FFMPEG)
_FFPROBE = shutil.which("ffprobe") or (os.path.join(_dir, "ffprobe.exe") if _dir else "ffprobe")


def _require_ffmpeg():
    """Video Read/Write shell out to ffmpeg (the codec engine for ProRes/DNxHR/h264/hevc). Fail with a clear
    message if it is not installed, rather than a cryptic FileNotFoundError. Stills need no ffmpeg."""
    if shutil.which("ffmpeg") is None and not os.path.isfile(_FFMPEG):
        raise RuntimeError(
            "Video needs ffmpeg on your PATH (a full build, for ProRes / DNxHR / h264 / hevc). Install it from "
            "gyan.dev (Windows), 'brew install ffmpeg' (macOS) or your package manager, then restart ComfyUI. "
            "Stills and image sequences (EXR / TIFF / PNG / JPEG) work without ffmpeg.")


STILL_EXTS = (".exr", ".hdr", ".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".dpx")
VIDEO_EXTS = (".mov", ".mp4", ".mkv", ".avi", ".webm", ".mxf", ".m4v")

# ComfyUI works in plain gamma-encoded sRGB (LoadImage does x/255 with no linearisation), so its working
# colorspace is sRGB - Display. Read converts INTO it; Write converts OUT of it.
WORKING = "sRGB - Display"

# Per-format colorspace defaults (the JS front-end mirrors this so the right value is visible on the node).
# EXR/HDR carry scene-linear render data -> ACEScg (the ACES working space). Everything else is display sRGB.
# (The strict OCIO file-rule for an EXR is ACES2065-1 interchange; ACEScg suits a render pipeline and is in
#  the dropdown alongside it.)
def _auto_input_cs(path):
    ext = os.path.splitext(path)[1].lower()
    return "ACEScg" if ext in (".exr", ".hdr") else WORKING

# A VIDEO deliverable is Rec.709, not sRGB. Both share the same primaries, but the transfer functions differ,
# and the pack used to default a movie to the working space (sRGB - Display) - which tagged every ProRes and
# DNxHR it wrote with trc=iec61966-2-1, the computer-display curve, where a delivery wants the Rec.709 / BT.1886
# one. An NLE or player then either honours the sRGB tag and shifts the picture, or ignores it and the tag is a
# lie. Rec.1886 Rec.709 - Display is the broadcast default and makes _video_color_tags emit bt709/bt709/bt709.
# Stills are unchanged: EXR is a scene-linear master (ACEScg), PNG / TIFF / JPEG are viewed on a computer
# display (sRGB). Fixed 2026-08-12.
VIDEO_DISPLAY = "Rec.1886 Rec.709 - Display"


def _auto_output_cs(container, still_format):
    if container == "video":
        return VIDEO_DISPLAY
    return "ACEScg" if still_format == "exr" else WORKING


# --------------------------------------------------------------------------- loading

# DPX descriptor codes (SMPTE ST 268-1) -> channel count. Only the ones a plate actually arrives as.
_DPX_DESCRIPTORS = {50: 3, 51: 4, 52: 4, 6: 1, 1: 1, 2: 1, 3: 1, 4: 1}


def _read_dpx(path):
    """Decode a DPX to float32 [H, W, C] normalised by its true code ceiling.

    Needed because the two libraries this pack already has cannot do it. Measured on a real Nuke-written
    2048x1152 10-bit plate: cv2.imread returns None for IMREAD_UNCHANGED, ANYCOLOR and COLOR alike, and PIL
    cannot open DPX at all - so the pack used to fall through to PIL and fail with "UnidentifiedImageError" on
    the single most common plate format in film finishing, while advertising .dpx as supported. imageio does
    read it and hands back UINT8, silently throwing away two of the ten bits, which is worse than failing.

    Handles what plates actually ship as: 8 / 16-bit, and 10-bit packing 1 ("filled, method A", three 10-bit
    samples left-aligned in a 32-bit word with two unused low bits). Anything else - RLE, 12-bit, exotic
    packing - defers to ffmpeg, which decodes DPX properly, instead of guessing at the bits.

    Normalisation matters: 10-bit code / 1023, NOT value / 65535. ffmpeg scales 10-bit into 16-bit by exactly
    64, so dividing that by 65535 is off by 0.1% - small, but this is a colour pipeline.
    Added 2026-08-12, after a real 10-bit DPX plate would not load.
    """
    with open(path, "rb") as f:
        hdr = f.read(2048)
    if len(hdr) < 2048:
        raise RuntimeError(f"{os.path.basename(path)} is too short to be a DPX ({len(hdr)} bytes).")
    magic = hdr[0:4]
    if magic == b"SDPX":
        E = ">"
    elif magic == b"XPDS":
        E = "<"
    else:
        raise RuntimeError(f"{os.path.basename(path)} is not a DPX (magic {magic!r}).")

    img_off = struct.unpack_from(E + "I", hdr, 4)[0]
    w = struct.unpack_from(E + "I", hdr, 772)[0]
    h = struct.unpack_from(E + "I", hdr, 776)[0]
    descriptor = hdr[800]
    bits = hdr[803]
    packing = struct.unpack_from(E + "H", hdr, 804)[0]
    encoding = struct.unpack_from(E + "H", hdr, 806)[0]
    ch = _DPX_DESCRIPTORS.get(descriptor)
    if not (w and h) or ch is None:
        raise RuntimeError(f"{os.path.basename(path)}: unsupported DPX layout "
                           f"(descriptor {descriptor}, {w}x{h}).")

    def _via_ffmpeg(why):
        """ffmpeg decodes every DPX variant; used for the ones not unpacked here."""
        _require_ffmpeg()
        tmp = os.path.join(tempfile.gettempdir(), f"ocio_dpx_{os.getpid()}_{abs(hash(path)) % 99999}.raw")
        try:
            cmd = [_FFMPEG, "-v", "error", "-y", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb48le", tmp]
            pr = subprocess.run(cmd, capture_output=True)
            if pr.returncode != 0 or not os.path.exists(tmp):
                raise RuntimeError(f"{os.path.basename(path)}: {why}, and ffmpeg could not decode it either: "
                                   f"{pr.stderr.decode('utf-8', 'ignore')[:200]}")
            arr = np.fromfile(tmp, dtype="<u2")
            if arr.size != w * h * 3:
                raise RuntimeError(f"{os.path.basename(path)}: ffmpeg returned {arr.size} samples, "
                                   f"expected {w * h * 3}.")
            # FULL SCALE, NOT A BIT SHIFT. The line here used to divide by 2**16 - 2**(16-N), on the belief
            # that ffmpeg widens an N-bit sample by shifting it left. It does not: it maps the code range onto
            # the full 16-bit range, round(code * 65535 / (2**N - 1)), so the top code arrives as 65535 for
            # every depth. Dividing that by 65472 returned 1.000962 for a 10-bit file whose highest code was
            # exactly 1023, which put 15.9% of a legal plate above 1.0 and made a correct read look like an
            # out-of-range one. Measured on files written and read back by ffmpeg itself:
            #
            #   10-bit  top code 1023  -> 65535    /65472 = 1.000962   /65535 = 1.000000
            #   12-bit  top code 4095  -> 65535    /65520 = 1.000229   /65535 = 1.000000
            #   16-bit  top code 65535 -> 65535    /65535 = 1.000000   /65535 = 1.000000
            #
            # Reported by Andrei Orehov as issue #7, on a 10-bit RGBA plate. It only ever showed on this
            # fallback: the pack's own unpacker handles the common widths, and a file reaches ffmpeg only when
            # width * channels is not divisible by 3, which is why a 4096-wide RGB test never saw it.
            return arr.reshape(h, w, 3).astype(np.float32) / 65535.0
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    if encoding != 0:
        return _via_ffmpeg(f"RLE-encoded DPX (encoding {encoding}) is not unpacked here")

    with open(path, "rb") as f:
        f.seek(img_off)
        data = f.read()

    if bits in (8, 16):
        dt = np.dtype(np.uint8) if bits == 8 else np.dtype(E + "u2")
        need = w * h * ch
        arr = np.frombuffer(data, dtype=dt, count=min(need, len(data) // dt.itemsize))
        if arr.size < need:
            return _via_ffmpeg(f"{bits}-bit payload is short ({arr.size} of {need} samples)")
        return arr.reshape(h, w, ch).astype(np.float32) / float((1 << bits) - 1)

    if bits == 10 and packing == 1:
        # three 10-bit samples per 32-bit word, left-aligned: bits 31..22, 21..12, 11..2
        per_line_samples = w * ch
        if per_line_samples % 3:
            return _via_ffmpeg("10-bit line is not a whole number of 32-bit words")
        words_per_line = per_line_samples // 3
        need = words_per_line * h
        wd = np.frombuffer(data, dtype=np.dtype(E + "u4"), count=min(need, len(data) // 4))
        if wd.size < need:
            return _via_ffmpeg(f"10-bit payload is short ({wd.size} of {need} words)")
        s = np.stack([(wd >> 22) & 0x3FF, (wd >> 12) & 0x3FF, (wd >> 2) & 0x3FF], -1)
        return s.reshape(h, w, ch).astype(np.float32) / 1023.0

    return _via_ffmpeg(f"{bits}-bit packing {packing} is not unpacked here")


def _read_still(path):
    """One still -> float32 RGBA [H,W,4] (alpha 1.0 if the file has none). Integer formats normalise to 0..1
    (alpha too); float (EXR / float TIFF) keeps its real range (scene-linear values can exceed 1)."""
    ext = os.path.splitext(path)[1].lower()
    a, bgr = None, False
    if ext == ".dpx":
        # Own decoder first: cv2 returns None on real 10-bit plates and PIL cannot open DPX at all, so the old
        # path fell through to PIL and raised "UnidentifiedImageError" on the commonest plate format there is.
        # The `.dpx` case used to be deliberately excluded from the raise below, which is what hid it.
        a = _read_dpx(path)                       # already RGB(A) in file order, already normalised
        bgr = False
    elif ext == ".exr":
        # OpenEXR FIRST, cv2 only as a fallback, and the fallback's exception is caught.
        #
        # Two separate defects were here. cv2's EXR codec is gated on the process-global
        # OPENCV_IO_ENABLE_OPENEXR variable, which must be set BEFORE cv2 is imported; the setdefault at the
        # top of this file cannot guarantee that, because several packs load ahead of this one alphabetically
        # and any one of them importing cv2 first turns the guard into dead code. Measured on a live server:
        # the variable reads '1' and cv2 still refuses. And when it refuses, `cv2.imread` RAISES
        # "OpenEXR codec is disabled" instead of returning None - so the `if a is None` diagnostic below,
        # which named the exact variable to set, could never run, and the artist got a raw grfmt_exr.cpp
        # traceback instead. The gate missed both because every relevant test sets the variable at the top of
        # its OWN process, where the import ordering is always favourable.
        #
        # The OpenEXR module is already a hard dependency here - it writes these files and reads their
        # metadata - it depends on no environment flag, and it reads at the file's true precision.
        try:
            import OpenEXR

            with OpenEXR.File(path) as f:
                ch = f.channels()
                key = next((k for k in ("RGBA", "RGB") if k in ch), None)
                if key is not None:
                    a = np.array(ch[key].pixels, copy=True)
                else:
                    # Separate per-channel entries. Assembled in RGBA order explicitly: sorting the keys
                    # would put B before G and silently swap two channels.
                    order = [n for n in ("R", "G", "B", "A") if n in ch]
                    if order:
                        a = np.stack([np.array(ch[n].pixels, copy=True) for n in order], axis=-1)
            bgr = False
        except Exception:
            a = None
        if a is None and cv2 is not None:
            try:
                a = cv2.imread(path, cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH)
                bgr = a is not None
            except Exception:
                a = None
        if a is None:
            raise RuntimeError(
                f"OCIO Read: could not decode {os.path.basename(path)}. The primary reader here is the "
                f"OpenEXR module - install it into ComfyUI's environment with `pip install \"OpenEXR>=3.3\"`. "
                f"The cv2 fallback cannot stand in for it: on OpenCV 4 it needs OPENCV_IO_ENABLE_OPENEXR=1 set "
                f"BEFORE ComfyUI starts, and on OpenCV 5 no environment variable helps, because those wheels "
                f"carry no EXR codec at all.")
    elif ext == ".hdr":
        if cv2 is not None:
            try:
                a = cv2.imread(path, cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH)
                bgr = a is not None
            except Exception:
                a = None
        if a is None:
            raise RuntimeError(f"OCIO Read: could not decode {os.path.basename(path)} - Radiance .hdr needs "
                               f"opencv-python installed in ComfyUI's environment.")
    elif ext in (".tif", ".tiff") and tifffile is not None:
        a = np.asarray(tifffile.imread(path))
    elif ext in (".png", ".bmp") and cv2 is not None:
        a = cv2.imread(path, cv2.IMREAD_UNCHANGED)   # 2026-07-03: cv2 reads TRUE 8/16-bit; PIL opens a 16-bit PNG as 8-bit (lost ~2 bits on in-graph read-back)
        bgr = a is not None
    if a is None:                                   # jpg / no-cv2 / cv2-failed fallback via PIL
        im = Image.open(path)
        im = im.convert("RGBA") if "A" in im.getbands() else im.convert("RGB")
        a = np.asarray(im)
    a = a.astype(np.float32)
    if a.ndim == 2:
        a = np.stack([a] * 3, -1)
    if ext not in (".exr", ".hdr") and a.max() > 1.5:   # normalise integer formats (float EXR kept as-is)
        a = a / (65535.0 if a.max() > 255.0 else 255.0)
    c = a.shape[2]
    order = [2, 1, 0] if bgr else [0, 1, 2]
    rgb = a[..., order] if c >= 3 else np.repeat(a[..., :1], 3, 2)
    alpha = a[..., 3] if c >= 4 else np.ones(a.shape[:2], np.float32)
    return np.ascontiguousarray(np.dstack([rgb, alpha]).astype(np.float32))


def _split_frame(path):
    """`.../name.0132.exr` -> (prefix='.../name.', frame=132, pad=4, ext='.exr', suffix=''); None if no number.
    The frame is the LAST run of digits in the stem; any trailing NON-digit suffix after it (before the extension)
    is captured separately, so 'supir_out_00001_.png' -> ('supir_out_', 1, 5, '.png', '_') collapses correctly
    (Nuke handles this too). Handles dot / underscore separators (name.0132.ext, name_0132.ext, name0132.ext)."""
    d, base = os.path.dirname(path), os.path.basename(path)
    stem, ext = os.path.splitext(base)
    m = re.match(r"^(.*?)(\d+)(\D*)$", stem)   # last digit run + trailing non-digits (a suffix like '_'); (\D*)$ forces the LAST run
    if not m:
        return None
    prefix = os.path.join(d, m.group(1)) if d else m.group(1)
    return (prefix, int(m.group(2)), len(m.group(2)), ext, m.group(3))


def _frame_num(path):
    sp = _split_frame(path)
    return sp[1] if sp else 0


def _sequence_siblings(path):
    """Every frame sharing the same prefix + extension as `path`, sorted by frame number (Nuke: grab the
    sequence from one selected frame)."""
    sp = _split_frame(path)
    if not sp:
        return []
    prefix, _, _, ext, suffix = sp
    out = []
    for c in glob.glob(prefix + "*" + suffix + ext):        # e.g. supir_out_*_.png
        x = _split_frame(c)
        if x and x[0] == prefix and x[4] == suffix and os.path.splitext(c)[1].lower() == ext.lower():
            out.append(c)
    return sorted(out, key=_frame_num)


def _seq_label(files):
    """Nuke-style 'name.####.ext [first-last]'."""
    sp = _split_frame(files[0])
    if not sp:
        return os.path.basename(files[0])
    prefix, n0, pad, ext, suffix = sp
    return f"{os.path.basename(prefix)}{'#' * pad}{suffix}{ext} [{n0}-{_frame_num(files[-1])}]"


def _collapse_ranges(nums):
    """[24, 73, 74, 75, 76, 84] -> '24, 73-76, 84' (Nuke-style missing-frame list)."""
    nums = sorted(set(int(n) for n in nums))
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def _assemble_sequence(files, start_frame, end_frame, missing_mode, edge_mode):
    """Build a contiguous frame list over [start,end] frame numbers from the present files (Nuke Read model).

    Gaps INSIDE the original range use missing_mode (black / hold last / error). Frames OUTSIDE the original
    range use edge_mode (hold end / loop / bounce / black). Returns (frames, meta) where meta has the original
    range and the missing-frame list."""
    fmap = {_frame_num(f): f for f in files}
    present = sorted(fmap)
    lo0, hi0 = present[0], present[-1]
    lo = start_frame if start_frame else lo0
    hi = end_frame if end_frame else hi0
    if hi < lo:
        hi = lo
    ref = _read_still(fmap[lo0])
    black = np.zeros_like(ref)
    cache = {lo0: ref}

    def load(f):
        if f not in cache:
            cache[f] = _read_still(fmap[f])
        return cache[f]

    span = hi0 - lo0 + 1
    frames, missing, last_good = [], [], None
    for f in range(lo, hi + 1):
        if lo0 <= f <= hi0:                              # inside original range
            if f in fmap:
                last_good = load(f)
                frames.append(last_good)
            else:                                        # a gap
                missing.append(f)
                if missing_mode == "error":
                    raise RuntimeError(f"missing frame {f} in sequence {os.path.basename(fmap[lo0])}")
                frames.append(last_good if (missing_mode == "hold" and last_good is not None) else black)
        else:                                            # outside original range -> edge behaviour
            if edge_mode == "black" or span <= 0:
                frames.append(black)
            elif edge_mode == "hold":
                frames.append(load(lo0 if f < lo0 else hi0))
            elif edge_mode == "loop":
                m = lo0 + ((f - lo0) % span)
                frames.append(load(m) if m in fmap else black)
            else:                                        # bounce
                period = max(1, 2 * span - 2)
                p = (f - lo0) % period
                m = lo0 + (p if p < span else period - p)
                frames.append(load(m) if m in fmap else black)
    return frames, {"orig_start": lo0, "orig_end": hi0, "missing": missing, "start": lo, "end": hi}


def _frame_files(source):
    """A folder or a #### / %0Nd pattern -> frame paths sorted by frame number; [] for a single still / video."""
    if os.path.isdir(source):
        fs = [os.path.join(source, f) for f in os.listdir(source)
              if os.path.splitext(f)[1].lower() in STILL_EXTS]
        # A folder holds ONE sequence, not "every numbered file in it". Without this grouping, pointing at a
        # mixed folder - the ComfyUI input directory, an output dump, or just "." - reported all of them as a
        # single clip spanning the lowest to the highest number found, with everything between them counted as
        # missing frames. Group by the same pattern _sequence_siblings uses (prefix + trailing suffix +
        # extension) and answer with the sequence that is actually there (2026-08-10).
        groups = {}
        for f in fs:
            sp = _split_frame(f)
            if not sp:
                continue
            groups.setdefault((sp[0], sp[4], sp[3].lower()), []).append(f)
        if not groups:
            return []
        return sorted(max(groups.values(), key=len), key=_frame_num)
    if "%0" in source or "#" in source:
        pat = re.sub(r"%0\d*d", "*", source)
        pat = re.sub(r"#+", "*", pat)
        return sorted(glob.glob(pat), key=_frame_num)
    return []


def _video_fps(info):
    rate = info.get("r_frame_rate", "") or info.get("avg_frame_rate", "")
    if "/" in rate:
        n, d = rate.split("/", 1)
        try:
            n, d = float(n), float(d)
            return round(n / d, 3) if d else 0.0
        except Exception:
            return 0.0
    try:
        return float(rate)
    except Exception:
        return 0.0


def _video_frame_count(info):
    """Frame count of a video from its ffprobe dict. Prefers the exact nb_frames; when absent or 'N/A' (MXF and
    some ProRes do not expose a stream frame count - int('N/A') used to raise and mis-flag the clip as a lone
    still, so the Read would not play it at all), derives it from duration x fps. 0 when neither is available.
    Added 2026-07-03 (video metadata: MXF frame-count fix)."""
    try:
        n = int(info.get("nb_frames", ""))
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    fps = _video_fps(info)
    try:
        dur = float(info.get("duration", "") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return int(round(dur * fps)) if (fps > 0 and dur > 0) else 0


def _video_input_cs(info, ext=None):
    """OCIO input colorspace for a video, mapped from its ffprobe color metadata (color_primaries /
    color_transfer / color_space), GUARDED to names that exist in the active config. Only overrides the working
    sRGB - Display default for HDR / wide-gamut sources (PQ / HLG / bt2020), where the tag genuinely matters.
    Ordinary bt709 mp4 / mov are internet deliverables (Adobe Premiere -> web), so they
    KEEP sRGB - Display, NOT broadcast BT.1886 Rec.709 - most viewers are on sRGB. Added 2026-07-03; SDR->sRGB 2026-07-04."""
    prim = (info.get("color_primaries", "") or "").lower()
    trc = (info.get("color_transfer", "") or "").lower()
    spc = (info.get("color_space", "") or "").lower()
    names = set(_colorspace_names())
    def pick(*cands):
        for c in cands:
            if c in names:
                return c
        return None
    cs = None
    if trc == "smpte2084":                                       # PQ HDR transfer
        cs = pick("Rec.2100-PQ - Display", "ST2084-P3-D65 - Display")
    elif trc == "arib-std-b67":                                  # HLG HDR transfer
        cs = pick("Rec.2100-HLG - Display")
    elif prim == "bt2020" or spc in ("bt2020nc", "bt2020c"):     # wide-gamut UHD
        cs = pick("Rec.2100-PQ - Display", "Rec.2100-HLG - Display")
    # SDR, and now the fork the original note left to the artist: "the widget stays editable if a bt709 camera
    # plate really wants Rec.709". A CAMERA MASTER IS NOT AN INTERNET DELIVERABLE, and the file says which it
    # is. MXF is a professional container - nobody publishes one to the web - and ProRes / DNxHD / DNxHR are
    # post codecs; those are graded and viewed on BT.1886 reference displays, so sRGB's transfer is the wrong
    # answer for them. An h264 / hevc mp4 keeps sRGB, which is what it was and why (2026-07-04).
    # Measured on a real master: a Resolve MXF of ProRes 4444 XQ tags color_space=bt709 with primaries and
    # transfer both 'unknown', so the tags alone cannot separate the two cases - the container and codec can.
    #
    # A NAMED TRANSFER VETOES THE GUESS, and it has to: the guess is about a transfer function, and
    # color_transfer is the only one of the three tags that describes one. Written first as an `or` chain, it
    # could not be vetoed at all - a file declaring iec61966-2-1 (sRGB), linear or log100 still came back
    # Rec.1886, because color_space or color_primaries alone satisfied the condition. That is wrong PIXELS,
    # not a wrong label: read() feeds this straight into the conversion. Measured cost of the wrong guess on
    # a ColorChecker: dE2000 max 3.58, mean 2.28, with 15 of 24 patches past dE 2.0, and a mid-grey code of
    # 0.18 landing 0.74 stops off.
    if cs is None:
        codec = (info.get("codec_name", "") or "").lower()
        is_post = codec in ("prores", "dnxhd") or (ext or "").lower() == ".mxf"
        # Transfers that NAME a curve which is not Rec.709. An untagged file says 'unknown' or nothing, and
        # that is the case the container/codec rule exists for - a real Resolve MXF tags color_space=bt709
        # with primaries and transfer both 'unknown'.
        named_other_trc = trc not in ("bt709", "unknown", "", "reserved")
        sdr_709 = spc in ("bt709", "smpte170m", "bt470bg") or prim == "bt709" or trc == "bt709"
        if is_post and sdr_709 and not named_other_trc:
            cs = pick("Rec.1886 Rec.709 - Display")
    return cs or WORKING


def _exr_fps(path):
    """The OpenEXR `framesPerSecond` rational attribute as a float, or None if absent/unreadable. Nothing else
    reads it: ffprobe does NOT report this attribute, it answers with the image2 demuxer default of 25.

    The import stays lazy and the failure stays quiet even though OpenEXR became a hard requirement in v1.3.0,
    because losing fps detection is not worth taking sequence detection down with it. An install that somehow
    lacks the module falls back to the default fps and keeps working. Reads only the header, so it is cheap even
    on a 50 MB EXR. Added 2026-07-03: sequence fps from EXR metadata (see _seq_fps)."""
    if os.path.splitext(path)[1].lower() != ".exr":
        return None
    try:
        import OpenEXR
        r = OpenEXR.InputFile(path).header().get("framesPerSecond")
    except Exception:
        return None
    if r is None:
        return None
    try:
        n, d = getattr(r, "n", None), getattr(r, "d", None)   # Imath.Rational (e.g. 24000/1001 -> 23.976)
        if n and d:
            return round(float(n) / float(d), 3)
        return round(float(r), 3)                             # some bindings hand back a plain number
    except Exception:
        return None


_SEQ_FPS_DEFAULT = 23.976   # cinema base rate (24000/1001); the fallback when a sequence carries no fps metadata

def _seq_fps(files):
    """Sequence playback rate: the first frame's EXR `framesPerSecond` if it carries one (a comp / render EXR
    usually does - Nuke stamps it), else the cinema default 23.976 (24000/1001). Non-EXR sequences (PNG / TIFF /
    JPEG) have no fps attribute, so they take the default, which is 23.976, not 24."""
    if files:
        fps = _exr_fps(files[0])
        if fps and fps > 0:
            return fps
    return _SEQ_FPS_DEFAULT


# A whole 4K clip decoded to a raw batch is enormous (65 s x 3840x2160 x rgb48le ~= 155 GB) - piping that through
# subprocess stdout OOMs / hangs the box. Cap the raw decode to this many bytes; adapts to resolution (fewer 4K
# frames, more 1080p). An over-budget / unbounded request returns the first N that fit + info['capped']=True.
def _video_decode_budget():
    """Bytes of raw rgb48le ONE video decode may buffer - ADAPTIVE to the machine's FREE RAM so it is safe on a
    small box and generous on a big one (this is a repo-wide default, not tuned to one user). ~1/4 of currently
    available RAM (the decode also holds the downstream float32 batch, ~2x the rgb48le buffer, so 1/4 leaves
    headroom), clamped to [2 GB, 16 GB]. 2026-07-04: was a flat 2 GB (capped a full HD clip on every machine); now
    a 128 GB box gets the 16 GB ceiling (a whole HD/2K clip decodes at once) while a 16 GB box stays ~2-4 GB (no
    OOM). psutil ships with ComfyUI; if it is somehow missing, fall back to 4 GB."""
    try:
        import psutil
        avail = int(psutil.virtual_memory().available)
        return min(16 * 1024 ** 3, max(2 * 1024 ** 3, avail // 4))
    except Exception:
        return 4 * 1024 ** 3
def _read_video(path, frame_start, frame_count):
    """Decode a video -> float32 RGB [N,H,W,3] (0..1) via ffmpeg piping 16-bit rgb48le, from frame_start. BOUNDED:
    never buffers more than _video_decode_budget() of raw pixels (a long 4K clip would otherwise OOM); an unbounded
    or over-budget request is capped, info['capped']=True. Uses -ss input seeking so a deep frame_start does not
    decode the whole head into memory."""
    _require_ffmpeg()
    probe = subprocess.run([_FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height,nb_frames,codec_name,pix_fmt,r_frame_rate,avg_frame_rate",
                            "-of", "default=noprint_wrappers=1", path], capture_output=True, text=True)
    info = dict(line.split("=", 1) for line in probe.stdout.strip().splitlines() if "=" in line)
    w, h = int(info.get("width", 0) or 0), int(info.get("height", 0) or 0)
    if not (w and h):
        raise RuntimeError(f"ffprobe could not read {path}: {probe.stderr[:200]}")
    per_frame = max(1, w * h * 3 * 2)                       # raw rgb48le bytes per frame
    budget = _video_decode_budget()                         # adaptive to free RAM (repo-wide safe), [2..16] GB
    cap = max(1, int(budget // per_frame))                  # frames that fit the budget at this resolution
    want = frame_count if frame_count > 0 else 10 ** 9      # 0 = unbounded (the whole clip)
    eff = min(want, cap)
    capped = eff < want
    fps = _video_fps(info) or 24.0
    # 2026-07-04: pick the decode depth from the SOURCE. An 8-bit source (h264 yuv420p, etc.) gains nothing from a
    # 16-bit rgb48le decode - it just doubles the data. Hi-bit sources (10/12-bit ProRes/DNxHR) carry an le/be
    # endianness tag in pix_fmt and DO need rgb48le to keep precision. rgb24 for 8-bit halves the raw bytes.
    pix = (info.get("pix_fmt") or "")
    hi = ("le" in pix) or ("be" in pix)
    px, dt, maxv = ("rgb48le", "<u2", 65535.0) if hi else ("rgb24", "u1", 255.0)
    # 2026-07-04: decode to a TEMP FILE, not a subprocess pipe. On Windows, reading a multi-GB raw stream back through
    # subprocess.run(capture_output=True) is pathologically slow (measured ~61 s for 3.9 GB); ffmpeg -> temp file +
    # np.fromfile is 12-25x faster (4.8 s rgb48le / 2.4 s rgb24, same 450-frame 1920x800 clip). This - not the memory
    # budget - was the cause of the multi-minute Player loads with a color node in the chain.
    cmd = [_FFMPEG, "-v", "error", "-y"]
    if frame_start > 0:
        cmd += ["-ss", f"{frame_start / fps:.6f}"]          # seek so we decode only [start, start+eff), not the head
    tmp_dir = None
    if folder_paths is not None:
        try:
            tmp_dir = folder_paths.get_temp_directory()
            os.makedirs(tmp_dir, exist_ok=True)                 # ComfyUI's temp dir may not exist yet -> create it (else fall back to the system temp)
        except Exception:
            tmp_dir = None
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".raw", prefix="ocio_vid_", dir=tmp_dir)
    os.close(tmp_fd)
    try:
        cmd += ["-i", path, "-frames:v", str(eff), "-f", "rawvideo", "-pix_fmt", px, tmp_path]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg decode failed: {proc.stderr.decode('utf-8', 'ignore')[:300]}")
        buf = np.fromfile(tmp_path, dtype=dt)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    n = buf.size // (w * h * 3)
    if n == 0:
        raise RuntimeError("ffmpeg returned no frames")
    arr = buf[: n * w * h * 3].reshape(n, h, w, 3).astype(np.float32) / maxv
    info["fps"] = fps
    info["capped"] = capped
    if capped:
        print(f"[OCIO] video decode capped at {eff} frames (of {want if want < 10**9 else info.get('nb_frames','?')}) "
              f"to fit the ~{budget // 1024**3} GB budget (adaptive to free RAM) at {w}x{h}; set start_frame/end_frame to view another range.")
    return arr, info


def _read_video_frame(path):
    """Decode ONLY frame 1 of a video (for the thumb route - never pull the whole clip). Same probe + pipe
    shape as _read_video, but '-frames:v 1' bounds the decode to a single frame. Returns float32 RGB [H,W,3]
    (0..1)."""
    _require_ffmpeg()
    probe = subprocess.run([_FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height",
                            "-of", "default=noprint_wrappers=1", path], capture_output=True, text=True)
    info = dict(line.split("=", 1) for line in probe.stdout.strip().splitlines() if "=" in line)
    w, h = int(info.get("width", 0) or 0), int(info.get("height", 0) or 0)
    if not (w and h):
        raise RuntimeError(f"ffprobe could not read {path}: {probe.stderr[:200]}")
    cmd = [_FFMPEG, "-v", "error", "-i", path, "-frames:v", "1",
           "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {proc.stderr.decode('utf-8', 'ignore')[:300]}")
    buf = np.frombuffer(proc.stdout, dtype="<u2")
    n = buf.size // (w * h * 3)
    if n == 0:
        raise RuntimeError("ffmpeg returned no frames")
    return buf[: w * h * 3].reshape(h, w, 3).astype(np.float32) / 65535.0


# --- H.264 proxy for the on-node Player -----------------------------------------------------------------------
# Browsers cannot decode ProRes / DNxHR (and HEVC has no software fallback), so the Player's <video> element errors
# on them at all. Transcode ONCE to a small H.264 mp4 - downscaled to _PROXY_MAX_SIDE
# (the Player already caps its display there, so no visible loss for a viewer) - cache it keyed by the source's
# realpath+mtime+size, and stream the proxy instead. The __init__ /ocio/proxy route drives + caches the transcode;
# these are the pure helpers. Full-res float review stays the EXR path, not video streaming. Added 2026-07-03.
_BROWSER_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}   # codecs a desktop <video> decodes directly; everything else (prores, dnxhd, hevc w/o HW SW-fallback, ...) gets a proxy - conservative so a clip ALWAYS plays
_PROXY_MAX_SIDE = 1920

def _video_codec(path):
    """The video stream's codec_name (lower-case), '' if unreadable."""
    try:
        pr = subprocess.run([_FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", path],
                            capture_output=True, text=True)
        return (pr.stdout.strip().splitlines() or [""])[0].strip().lower()
    except Exception:
        return ""

def _needs_proxy(path):
    """True if a browser <video> cannot be relied on to decode this file's codec -> a server H.264 proxy is needed."""
    return _video_codec(path) not in _BROWSER_VIDEO_CODECS

def _proxy_dir():
    root = folder_paths.get_temp_directory() if folder_paths is not None else os.path.join(os.path.expanduser("~"), ".ocio_tmp")
    d = os.path.join(root, "ocio_proxy")
    os.makedirs(d, exist_ok=True)
    return d

def _proxy_path(path):
    """Deterministic cache path for a source's H.264 proxy, keyed by realpath+mtime+size (an edited source
    re-transcodes). .mp4 (a VIDEO_EXTS ext) so /ocio/stream will serve it back."""
    try:
        st = os.stat(path)
        key = f"{os.path.realpath(path)}|{int(st.st_mtime)}|{st.st_size}"
    except OSError:
        key = path
    return os.path.join(_proxy_dir(), hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:16] + ".mp4")

def _proxy_transcode_cmd(src, dst):
    """ffmpeg args to build the H.264 proxy: downscale to _PROXY_MAX_SIDE (even dims for yuv420p), yuv420p +
    faststart so the <video> streams + seeks, no audio (the Player is muted). One-time; cached by _proxy_path."""
    _require_ffmpeg()
    return [_FFMPEG, "-v", "error", "-y", "-i", src,
            "-map", "0:v:0",                                  # ONLY the first video stream (drop audio / extra tracks)
            "-vf", f"scale='min({_PROXY_MAX_SIDE},iw)':-2:flags=bicubic",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-an", "-write_tmcd", "0",                        # a ProRes/MXF carries a timecode track that ffmpeg auto-writes as a 'tmcd' DATA stream into the mp4; a <video> element can STALL on that extra stream (buffers but no picture). -write_tmcd 0 = a clean VIDEO-ONLY proxy.
            "-movflags", "+faststart", dst]


def load_source(source, start_frame=0, end_frame=0, frame_mode="auto", missing_mode="black", edge_mode="hold"):
    """source -> (np [N,H,W,3] float32, info dict). Single still / folder / pattern / video.

    start_frame / end_frame bound the range (0 = unbounded): for a sequence they are FRAME NUMBERS
    (Nuke-style, e.g. 20..30 from files name.0020..name.0030); for a video they are 0-based indices.
    missing_mode fills gaps inside the range (black / hold / error); edge_mode fills frames outside the
    original range (hold / loop / bounce / black). frame_mode (Nuke's 'grab sequence' toggle):
      auto - numbered file with siblings -> whole sequence; single - just this file; sequence - force it."""
    source = (source or "").strip().rstrip("/")
    # An empty source resolves to the input FOLDER, and the sequence scan below would then sweep every numbered
    # file in it and load that pile as if it were one clip. Say what is wrong instead of reading something the
    # artist never asked for (2026-08-10).
    if not source:
        raise ValueError("OCIO Read: no source. Type a path into 'source', or pick one with Open Files.")
    s = source if os.path.isabs(source) else os.path.join(_input_dir(), source)
    ext = os.path.splitext(s)[1].lower()
    if ext in VIDEO_EXTS:
        # video frame numbers are 1-BASED (frame 1 = first); map to the 0-based ffmpeg decode index (frame 1 -> 0).
        # start_frame 0 (unbounded/default) also maps to index 0. count spans [start_frame, end_frame] inclusive.
        count = (end_frame - start_frame + 1) if (end_frame >= start_frame and end_frame > 0) else 0
        arr, info = _read_video(s, max(0, start_frame - 1), count)
        alpha = np.ones((*arr.shape[:3], 1), np.float32)     # video has no alpha -> opaque
        arr = np.concatenate([arr, alpha], axis=-1)
        info["kind"] = "video"
        info["orig_start"] = 1                               # 1-based numbering (frame 1 is the first frame)
        info["label"] = os.path.basename(s)
        return arr, info
    files = _frame_files(s)                       # an explicit folder or #### pattern
    if not files and os.path.isfile(s) and frame_mode != "single":
        sib = _sequence_siblings(s)               # one selected frame -> its sequence
        if frame_mode == "sequence" or (frame_mode == "auto" and len(sib) > 1):
            files = sib
    if files:
        label = _seq_label(files)
        frames, meta = _assemble_sequence(files, start_frame, end_frame, missing_mode, edge_mode)
        h0, w0 = frames[0].shape[:2]
        frames = [f if f.shape[:2] == (h0, w0) else np.zeros((h0, w0, 4), np.float32) for f in frames]
        return np.stack(frames, 0), {"kind": "sequence", "count": len(frames), "fps": _seq_fps(files),
                                     "format": os.path.splitext(files[0])[1].lstrip("."), "label": label,
                                     "orig_start": meta["orig_start"], "orig_end": meta["orig_end"],
                                     "missing": meta["missing"]}
    return _read_still(s)[None], {"kind": "still", "count": 1, "fps": 0.0,
                                  "format": ext.lstrip("."), "label": os.path.basename(s)}


def _scan_sources():
    """Input-folder media for the Read picker: real file names (and folder-of-stills entries). A frame is
    shown by its real name, not a confusing 'name.####.ext' - the frame_mode widget collapses a sequence from
    any one selected frame, so the picker stays readable."""
    items = list(_scan_files(set(STILL_EXTS) | set(VIDEO_EXTS)))
    base = _input_dir()
    if base and os.path.isdir(base):
        for root, _, fs in os.walk(base):
            if sum(os.path.splitext(f)[1].lower() in STILL_EXTS for f in fs) >= 2:
                rel = os.path.relpath(root, base).replace("\\", "/")
                if rel != ".":
                    items.append(rel + "/")
    return sorted(set(items))


def _seq_range(source):
    """For the JS auto-fill: detect a sequence's [first, last, count] + fps from a selected source. Returns a
    dict; count 0 for a lone still."""
    source = (source or "").strip().rstrip("/")
    # Same trap as load_source: an empty source used to resolve to the input folder, so the scan reported one
    # invented sequence spanning every numbered file in it - thousands of frames, nearly all "missing". Report
    # "nothing to detect" rather than a confident fiction (2026-08-10).
    if not source:
        return {"kind": "still", "start": 0, "end": 0, "count": 0, "fps": 0.0, "error": "empty source"}
    s = source if os.path.isabs(source) else os.path.join(_input_dir(), source)
    ext = os.path.splitext(s)[1].lower()
    if ext in VIDEO_EXTS:
        # codec_name is asked for here as well as in read_meta, because _video_input_cs needs it to tell a
        # camera master from an internet deliverable. Without it this path and the panel's path would answer
        # the colorspace question differently for the same file - the widget filled one way, the panel showing
        # another.
        pr = subprocess.run([_FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=nb_frames,r_frame_rate,avg_frame_rate,duration,codec_name,"
                             "color_primaries,color_transfer,color_space",
                             "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1", s],
                            capture_output=True, text=True)
        # stream + format sections flatten to one key=value dict; format's duration is printed last, so it wins
        # over an absent/N-A stream duration (needed for MXF, whose stream carries no nb_frames or duration).
        info = dict(line.split("=", 1) for line in pr.stdout.strip().splitlines() if "=" in line)
        nb = _video_frame_count(info)   # robust: nb_frames, or duration x fps when nb_frames is 'N/A' (MXF)
        # video frame numbering is 1-BASED (frame 1 is the first frame, not 0) - a 451-frame clip is 1..451
        return {"kind": "video", "start": 1, "end": nb, "count": nb, "fps": _video_fps(info),
                "input_cs": _video_input_cs(info, ext)}   # colorspace from the video's color metadata
    files = _frame_files(s)
    if not files and os.path.isfile(s):
        files = _sequence_siblings(s)
    if len(files) >= 2:
        present = sorted(_frame_num(f) for f in files)
        lo, hi = present[0], present[-1]
        pset = set(present)
        missing = [f for f in range(lo, hi + 1) if f not in pset]
        return {"kind": "sequence", "start": lo, "end": hi, "count": len(files), "fps": _seq_fps(files),
                "orig_start": lo, "orig_end": hi, "missing": _collapse_ranges(missing),
                "missing_count": len(missing), "input_cs": _auto_input_cs(files[0])}
    return {"kind": "still", "start": 0, "end": 0, "count": 1, "fps": 0.0}


def _still_shape_alpha(path):
    """(H, W, has_alpha) for one still, WITHOUT the RGBA padding _read_still applies for the pixel pipeline -
    the metadata panel needs to know whether the file itself carries an alpha channel, not whether one was
    synthesized. Mirrors _read_still's decode path (same libs, same ext branches) but reads the real channel
    count off the decoded array before any padding."""
    ext = os.path.splitext(path)[1].lower()
    a, bands = None, None
    if ext == ".exr":
        # THE HEADER ANSWERS THIS, so no pixels are decoded at all - the panel used to pull a whole 49 MB
        # frame off disk to learn its width.
        #
        # It also has to, for the reason _read_still spells out at length: cv2's EXR codec is gated on
        # OPENCV_IO_ENABLE_OPENEXR being set BEFORE cv2 is imported, and when it is not, `cv2.imread` RAISES
        # rather than returning None - so the `if a is None` fallback below could never catch it and the whole
        # metadata read failed with a raw grfmt_exr.cpp error. Reproduced on the live server 2026-08-13:
        # /ocio/meta answered {"error": "... OpenEXR codec is disabled ..."} and the panel stayed empty, while
        # reading and writing pixels were both fine - they had already been moved off cv2, and this had not.
        try:
            import OpenEXR

            with OpenEXR.File(path) as f:
                lo, hi = f.parts[0].header["dataWindow"]
                w_, h_ = int(hi[0]) - int(lo[0]) + 1, int(hi[1]) - int(lo[1]) + 1
                ch = f.channels()
                names = set(ch)
                has_a = ("A" in names) or any("A" in k for k in ("RGBA",) if k in names)
                if w_ > 0 and h_ > 0:
                    return h_, w_, bool(has_a)
        except Exception:
            pass
        if cv2 is not None:
            try:
                a = cv2.imread(path, cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH)
            except Exception:
                a = None
    elif ext in (".hdr", ".dpx") and cv2 is not None:
        try:
            a = cv2.imread(path, cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH)
        except Exception:
            a = None
    elif ext in (".tif", ".tiff") and tifffile is not None:
        a = np.asarray(tifffile.imread(path))
    if a is None:
        im = Image.open(path)
        bands = im.getbands()
        a = np.asarray(im.convert("RGBA") if "A" in bands else im.convert("RGB"))
    if a.ndim == 2:
        return a.shape[0], a.shape[1], False
    h, w, c = a.shape[0], a.shape[1], a.shape[2]
    has_alpha = (bands is not None and "A" in bands) or (bands is None and c >= 4)
    return h, w, has_alpha


# --------------------------------------------------------------------------- source metadata (Read -> Write)
# RESPONSIBLE FOR: carrying a plate's OWN metadata - camera body, lens, focal length, stop, ISO, shutter,
# timecode, reel, show attributes - from OCIO Read through to OCIO Write. Until now every one of those was
# dropped at the door: cv2 writes an EXR with the nine mandatory attributes and nothing else (measured), so a
# graded frame left the pack knowing less about itself than the plate that entered it. Added 2026-08-12.

# Attributes that describe the CONTAINER, not the shot. Whatever writes the output recreates them from the
# actual pixels, so copying them across is not merely useless, it is wrong: a dataWindow lifted off a 640x352
# plate and stamped onto a 1280x704 render is a header that contradicts its own image. Anything NOT listed
# here is treated as shot metadata and travels.
_EXR_STRUCTURAL = frozenset({
    "channels", "compression", "dataWindow", "displayWindow", "lineOrder", "pixelAspectRatio",
    "screenWindowCenter", "screenWindowWidth", "type", "tiles", "chunkCount", "version", "name", "view",
    "envmap", "deepImageState", "preview",
})

# OCIO Write's compression widget -> the OpenEXR module constant. Confirmed by writing and re-reading one file
# per method: all eight round-trip, and the module also exposes B44 / B44A / HTJ2K which the widget does not
# offer (lossy or very new; not added without a reason to).
_EXR_COMP_OPENEXR = {"zip": "ZIP_COMPRESSION", "zips": "ZIPS_COMPRESSION", "piz": "PIZ_COMPRESSION",
                     "pxr24": "PXR24_COMPRESSION", "dwaa": "DWAA_COMPRESSION", "dwab": "DWAB_COMPRESSION",
                     "rle": "RLE_COMPRESSION", "none": "NO_COMPRESSION"}


def _meta_scalar(v):
    """One metadata value -> something json.dumps can take, without silently losing it.

    The readers hand back numpy scalars and arrays, pybind enums, byte strings and library-specific tuples.
    Anything not reducible keeps its repr rather than being dropped: an attribute we cannot type is still
    evidence that the plate carried it, and a shot with an unrecognised custom attribute is the normal case in
    a real facility, not the exception."""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return [_meta_scalar(x) for x in v.tolist()]
    if isinstance(v, (list, tuple, set)):
        return [_meta_scalar(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _meta_scalar(x) for k, x in v.items()}
    return repr(v)


def _read_exr_meta(path):
    """Every non-structural attribute in an EXR header, via OpenEXR (cv2 exposes none of them at all)."""
    import OpenEXR
    with OpenEXR.File(path) as f:
        hdr = dict(f.header())
    return {k: _meta_scalar(v) for k, v in hdr.items() if k not in _EXR_STRUCTURAL}


def _read_tiff_meta(path):
    """TIFF tags, including the EXIF block a camera or a DI tool writes. Structural tags (dimensions, strip
    layout, bit depth) are skipped for the same reason as the EXR ones."""
    if tifffile is None:
        return {}
    skip = {"ImageWidth", "ImageLength", "BitsPerSample", "Compression", "PhotometricInterpretation",
            "StripOffsets", "SamplesPerPixel", "RowsPerStrip", "StripByteCounts", "PlanarConfiguration",
            "SampleFormat", "ExtraSamples", "TileWidth", "TileLength", "TileOffsets", "TileByteCounts",
            "NewSubfileType", "PredictorClass", "Predictor"}
    out = {}
    with tifffile.TiffFile(path) as tf:
        for tag in tf.pages[0].tags:
            if tag.name in skip:
                continue
            out[tag.name] = _meta_scalar(tag.value)
    return out


def _read_pil_meta(path):
    """PNG text chunks / JPEG EXIF + comment, by their human-readable EXIF tag names where PIL knows them."""
    out = {}
    with Image.open(path) as im:
        for k, v in (getattr(im, "text", None) or {}).items():
            out[str(k)] = _meta_scalar(v)
        for k, v in (im.info or {}).items():
            if k in ("exif", "icc_profile", "transparency", "background", "palette"):
                continue          # binary blobs, not shot metadata; exif is expanded below
            if str(k) not in out:
                out[str(k)] = _meta_scalar(v)
        try:
            from PIL.ExifTags import TAGS
            for tid, v in (im.getexif() or {}).items():
                out[TAGS.get(tid, f"Exif{tid}")] = _meta_scalar(v)
        except Exception:
            pass
    return out


def _read_video_meta(path):
    """Container and video-stream tags from ffprobe: what a camera or an NLE writes into a .mov / .mxf -
    reel and shot names, timecode, creation time, encoder, and any vendor tag. The timecode is pulled out
    explicitly because ffmpeg needs it as its own -timecode option, not as a generic tag."""
    _require_ffmpeg()
    pr = subprocess.run([_FFPROBE, "-v", "error", "-show_entries",
                         "format_tags:stream_tags:stream=index,codec_type", "-of", "json", path],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if pr.returncode != 0:
        return {}
    try:
        import json as _json
        d = _json.loads(pr.stdout or "{}")
    except Exception:
        return {}
    out = {}
    for k, v in ((d.get("format") or {}).get("tags") or {}).items():
        out[str(k)] = _meta_scalar(v)
    for st in d.get("streams") or []:
        for k, v in (st.get("tags") or {}).items():
            if str(k) not in out:                     # container tags win; a stream tag only fills a gap
                out[str(k)] = _meta_scalar(v)
    return out


# DPX header fields worth carrying, per SMPTE ST 268-1:2014. The two offsets that are easy to get wrong were
# confirmed against a real plate by arithmetic on the file size rather than from memory: the Film header starts
# at 1664 and the Television header at 1920 (1408 is the Orientation header, and holding 1408 for television is
# exactly the mistake that was made once already this session).
#
# UNSET FIELDS MUST NOT BE REPORTED AS VALUES. DPX fills unused ASCII with 0xFF, unused integers with
# 0xFFFFFFFF and unused floats with NaN. Read naively an empty timecode becomes 4294967295 and an empty frame
# rate becomes nan, and either would travel into an EXR header as though the plate had said it. Every reader
# below returns None for its sentinel instead, and `put` drops None.
_DPX_TRANSFER = {
    0: "User defined", 1: "Printing density", 2: "Linear", 3: "Logarithmic", 4: "Unspecified video",
    5: "SMPTE 274M", 6: "ITU-R 709-4", 7: "ITU-R 601-5 B/G", 8: "ITU-R 601-5 M", 9: "NTSC composite",
    10: "PAL composite", 11: "Z linear", 12: "Z homogeneous",
}
_DPX_COLORIMETRIC = {
    0: "User defined", 1: "Printing density", 2: "Unspecified", 3: "Unspecified", 4: "Unspecified video",
    5: "SMPTE 274M", 6: "ITU-R 709-4", 7: "ITU-R 601-5 B/G", 8: "ITU-R 601-5 M", 9: "NTSC composite",
    10: "PAL composite",
}


def _dpx_str(buf, off, length):
    """An ASCII field, or None when the plate never filled it (0xFF padding, or empty after trimming)."""
    raw = bytes(buf[off:off + length])
    if not raw or raw[0] in (0x00, 0xFF):
        return None
    txt = raw.split(b"\x00", 1)[0].replace(b"\xff", b"").decode("ascii", "replace").strip()
    return txt or None


def _dpx_u32(E, buf, off, sane_max=None):
    """A U32 field, or None when it is the standard's sentinel OR outside a stated plausible range.

    `sane_max` exists because 0xFFFFFFFF is not the only way a writer leaves a field unfilled. A real ADX10
    plate reports SequenceLength and HeldCount as 16777216 (0x01000000), which is 194 days of frames at 25 fps
    - a writer artefact, not a count. Carrying it into an EXR header would state as fact something no one
    measured, so the bound is applied and named rather than the number being passed on.
    """
    v = struct.unpack_from(E + "I", buf, off)[0]
    if v == 0xFFFFFFFF:
        return None
    if sane_max is not None and v > sane_max:
        return None
    return v


def _dpx_f32(E, buf, off, zero_is_unset=False):
    """A float field, or None for NaN / infinity, and optionally for an exact zero.

    `zero_is_unset` is for the fields where zero is not a physical value - gamma, reference white, shutter
    angle, frame rate, integration time. A writer that leaves them blank writes 0.0, and passing that on would
    put "this plate has zero gamma" into an EXR header as though someone had measured it.
    """
    v = struct.unpack_from(E + "f", buf, off)[0]
    if v != v or v in (float("inf"), float("-inf")):
        return None
    if zero_is_unset and v == 0.0:
        return None
    return float(v)


def _dpx_timecode(E, buf, off=1920):
    """The Television header's packed BCD timecode as 'HH:MM:SS:FF', or None if unset.

    Each nibble is one decimal digit, so 0x14561610 reads as 14:56:16:10. A nibble above 9 means the field is
    not BCD - a corrupt or non-conforming header - and is reported as None rather than decoded into nonsense.
    """
    v = struct.unpack_from(E + "I", buf, off)[0]
    if v in (0, 0xFFFFFFFF):
        return None
    d = [(v >> s) & 0xF for s in (28, 24, 20, 16, 12, 8, 4, 0)]
    if any(x > 9 for x in d):
        return None
    h, m, s, f = (d[0] * 10 + d[1], d[2] * 10 + d[3], d[4] * 10 + d[5], d[6] * 10 + d[7])
    if h > 23 or m > 59 or s > 59:
        return None
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def _read_dpx_meta(path):
    """Everything a DPX header carries that a downstream ACEScg EXR should keep. Keys namespaced `dpx:`.

    WHY THIS EXISTS: a DPX scan is where a shot's identity lives - its own timecode, slate, reel, film format,
    the device that made it, the transfer it was scanned with. Before this, `read_source_meta` routed `.dpx` to
    a "header parsing not implemented" note, so every one of those was dropped and OCIO Write stamped its own
    default timecode in their place. On a real ADX10 plate that lost `dpx:TimeCode 14:56:16:10`, the slate, and
    the fact that it was scanned on daVinci. The pack already parses this same 2048-byte header for the pixel
    layout, so this is an increment on working code.
    """
    with open(path, "rb") as f:
        buf = f.read(2048)
    if len(buf) < 2048:
        return {}
    magic = buf[0:4]
    if magic == b"SDPX":
        E = ">"
    elif magic == b"XPDS":
        E = "<"
    else:
        return {}

    out = {}

    def put(key, val):
        if val is not None and val != "":
            out[key] = val

    # ---- file information header (0-767)
    put("dpx:Version", _dpx_str(buf, 8, 8))
    put("dpx:FileName", _dpx_str(buf, 36, 100))
    put("dpx:CreationDate", _dpx_str(buf, 136, 24))
    put("Software", _dpx_str(buf, 160, 100))          # the DI tool that wrote it; EXR's own attribute name
    put("dpx:Project", _dpx_str(buf, 260, 200))
    put("dpx:Copyright", _dpx_str(buf, 460, 200))

    # ---- image information header, element 0 (768-1407). Element 0 begins at 780, so descriptor is 800.
    put("dpx:Transfer", _DPX_TRANSFER.get(buf[801]))
    put("dpx:Colorimetric", _DPX_COLORIMETRIC.get(buf[802]))
    put("dpx:BitDepth", buf[803] if buf[803] not in (0, 0xFF) else None)
    put("dpx:ElementDescription", _dpx_str(buf, 820, 32))

    # ---- image orientation header (1408-1663)
    put("dpx:SourceFileName", _dpx_str(buf, 1432, 100))
    put("dpx:SourceDate", _dpx_str(buf, 1532, 24))
    put("dpx:InputDevice", _dpx_str(buf, 1556, 32))
    put("dpx:InputDeviceSerial", _dpx_str(buf, 1588, 32))

    # ---- film information header (1664-1919)
    put("dpx:FilmMfgId", _dpx_str(buf, 1664, 2))
    put("dpx:FilmType", _dpx_str(buf, 1666, 2))
    put("dpx:FilmOffset", _dpx_str(buf, 1668, 2))
    put("dpx:Prefix", _dpx_str(buf, 1670, 6))
    put("dpx:Count", _dpx_str(buf, 1676, 4))
    put("dpx:Format", _dpx_str(buf, 1680, 32))
    # A frame count beyond ten million is 5 months of footage at 24 fps - a writer artefact, not a count.
    put("dpx:FramePosition", _dpx_u32(E, buf, 1712, sane_max=10_000_000))
    put("dpx:SequenceLength", _dpx_u32(E, buf, 1716, sane_max=10_000_000))
    put("dpx:HeldCount", _dpx_u32(E, buf, 1720, sane_max=10_000_000))
    put("dpx:FilmFrameRate", _dpx_f32(E, buf, 1724, zero_is_unset=True))
    put("dpx:ShutterAngle", _dpx_f32(E, buf, 1728, zero_is_unset=True))
    put("dpx:FrameId", _dpx_str(buf, 1732, 32))
    put("dpx:SlateInfo", _dpx_str(buf, 1764, 100))

    # ---- television information header (1920-2047)
    put("dpx:TimeCode", _dpx_timecode(E, buf, 1920))
    put("dpx:FrameRate", _dpx_f32(E, buf, 1940, zero_is_unset=True))
    # THE VIDEO SIGNAL BLOCK IS TREATED AS A UNIT. Gamma and reference white cannot physically be zero, so if
    # either reads 0.0 the block was never filled, and the neighbouring fields where zero IS plausible - black
    # level, black gain, break point, time offset - are unset too rather than genuinely measured zeros.
    # Reporting "dpx:Gamma 0.0" into an EXR header would state as fact that the plate has zero gamma. The real
    # ADX10 plate this was built against has exactly that: every float in this block reads 0.0.
    gamma = _dpx_f32(E, buf, 1948, zero_is_unset=True)
    white = _dpx_f32(E, buf, 1964, zero_is_unset=True)
    if gamma is not None or white is not None:
        put("dpx:Gamma", gamma)
        put("dpx:WhiteLevel", white)
        put("dpx:BlackLevel", _dpx_f32(E, buf, 1952))
        put("dpx:BlackGain", _dpx_f32(E, buf, 1956))
        put("dpx:BreakPoint", _dpx_f32(E, buf, 1960))
        put("dpx:TimeOffset", _dpx_f32(E, buf, 1944))
    put("dpx:IntegrationTime", _dpx_f32(E, buf, 1968, zero_is_unset=True))
    return out


def read_source_meta(source):
    """The metadata OCIO Read puts on the wire for OCIO Write: {"source", "kind", "attrs"}.

    Reads the FIRST frame of a sequence, deliberately. Per-frame attributes do exist (timecode advances), but
    Write emits one header per output frame from one incoming dict, so promising per-frame fidelity would be a
    lie; the first frame's attributes are the shot's attributes. Returns {} on anything unreadable rather than
    raising - a missing camera tag must never be the reason a render does not start."""
    source = (source or "").strip().rstrip("/")
    if not source:
        return {}
    s = source if os.path.isabs(source) else os.path.join(_input_dir(), source)
    files = _frame_files(s)
    if not files and os.path.isfile(s):
        sib = _sequence_siblings(s)
        files = sib if len(sib) > 1 else [s]
    first = files[0] if files else (s if os.path.isfile(s) else None)
    if not first:
        return {}
    ext = os.path.splitext(first)[1].lower()
    try:
        if ext in VIDEO_EXTS:
            attrs, kind = _read_video_meta(first), "video"
        elif ext in (".exr",):
            attrs, kind = _read_exr_meta(first), "exr"
        elif ext in (".tif", ".tiff"):
            attrs, kind = _read_tiff_meta(first), "tiff"
        elif ext in (".png", ".jpg", ".jpeg", ".bmp"):
            attrs, kind = _read_pil_meta(first), ext.lstrip(".")
        elif ext == ".dpx":
            # A DPX scan carries the shot's identity - its own timecode, slate, reel, film format, the device
            # that scanned it. This used to fall into the "not implemented" branch below, so all of it was
            # dropped and Write stamped its own default timecode instead. EXR in ACEScg is what the industry
            # feeds in today, but scans and archive still arrive as DPX, and losing a plate's timecode on the
            # way into an ACEScg master is not something to be relaxed about.
            attrs, kind = _read_dpx_meta(first), "dpx"
        else:
            # .hdr is read for PIXELS but its header is not parsed. Saying so beats returning {} and letting a
            # caller read that as "this plate carried nothing".
            return {"source": os.path.basename(first), "kind": ext.lstrip("."), "attrs": {},
                    "note": f"{ext.lstrip('.')} header parsing not implemented; pixels are read, metadata is not"}
    except Exception as e:
        return {"source": os.path.basename(first), "kind": ext.lstrip("."), "attrs": {},
                "note": f"metadata unreadable: {str(e)[:160]}"}
    return {"source": os.path.basename(first), "kind": kind, "attrs": attrs}


def _save_exr_with_meta(path, rgb, bit_depth, alpha=None, compression="zip", attrs=None):
    """Write an EXR carrying arbitrary header attributes. Needed because cv2 - which writes every other EXR in
    this pack - cannot write a single custom attribute (measured: a cv2 EXR comes back with exactly the nine
    mandatory ones). Half vs float follows the array dtype, which is how OpenEXR 3.x picks the channel type.

    Only called when there ARE attributes to write, so a plain render keeps going through cv2 on exactly the
    path it used before, byte for byte."""
    import OpenEXR
    dt = np.float32 if bit_depth == "32f" else np.float16
    if alpha is not None:
        px = np.dstack([rgb[..., :3], np.asarray(alpha, np.float32)]).astype(dt)
        channels = {"RGBA": np.ascontiguousarray(px)}
    else:
        channels = {"RGB": np.ascontiguousarray(rgb[..., :3].astype(dt))}
    header = {"compression": getattr(OpenEXR, _EXR_COMP_OPENEXR.get(compression, "ZIP_COMPRESSION")),
              "type": OpenEXR.scanlineimage}
    for k, v in (attrs or {}).items():
        if k in _EXR_STRUCTURAL:
            continue
        if _meta_is_private(k, v):
            # A DELIVERED EXR MUST NOT CARRY THE MACHINE OR THE GRAPH. This guard existed on the MOV path and
            # was missing here, and the gap was reachable from an ordinary graph rather than in theory: a ComfyUI
            # PNG's text chunks ARE `prompt` and `workflow`, _read_pil_meta hands them back, and OCIO Read ->
            # OCIO Write(EXR) then wrote the whole graph JSON - absolute paths inside it - into the delivered
            # header. Reproduced 2026-08-12 with output_folder="D:/secret/project/shots" landing in an EXR.
            continue
        # OpenEXR type-checks its OWN standard attribute names and RAISES on a value of the wrong shape, so a
        # single bad attribute would kill the render. Found by mutation 2026-08-12: a plate's chromaticities
        # arriving over the JSON wire is a LIST, not a tuple, and would have been stringified and then rejected
        # ("expected a 6-tuple") - a metadata detail taking down a finished render. Metadata must never be the
        # reason a render dies, so each attribute is written on its own and a rejected one is skipped.
        if isinstance(v, list) and v and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            v = tuple(float(x) for x in v)    # JSON has no tuples; the numeric standard attributes need one
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            header[k] = v                     # OpenEXR types these itself; bool has no attribute type
        elif isinstance(v, tuple) or type(v).__name__ in ("TimeCode", "KeyCode", "Chromaticities"):
            # STRUCTURED STANDARD TYPES MUST NOT BE STRINGIFIED. chromaticities takes a FLAT 8-float tuple and
            # timeCode an OpenEXR.TimeCode; str() on either writes a *string*-typed attribute with the right
            # text and the wrong type, and a standards-aware reader (oiiotool, Nuke, Resolve) then ignores it.
            # Measured on OpenEXR 3.4.13: the flat tuple and the TimeCode object round-trip; a numpy array or a
            # nested 4x2 tuple raise ValueError "expected a 6-tuple" (the message is wrong about the arity, the
            # rejection is real). Added 2026-08-12 with the metadata authoring wiring.
            header[k] = v
        elif v is not None:
            header[k] = str(v)                # lists / dicts / anything else survive as text, not as nothing
    def _emit(hdr, out, px):
        # COPIES OF BOTH DICTS, always. OpenEXR.File used as a context manager EMPTIES *both* the header and the
        # channels dict it was handed, on __exit__, whether the write succeeded or failed (measured on 3.4.13: a
        # 3-key header and a 1-key channels dict both read back as {} afterwards). Handing it the originals meant
        # the first attempt wiped them, so the retry below died on KeyError 'compression' and then wrote a file
        # with no pixels at all - a recovery path destroyed by the failure it exists to recover from. Found by
        # mutation 2026-08-12. dict() is a shallow copy: the pixel arrays are not duplicated, only the mapping.
        with OpenEXR.File(dict(hdr), dict(px)) as f:
            f.write(out)

    try:
        _emit(header, path, channels)
        return path
    except Exception:
        pass
    # ONE BAD ATTRIBUTE MUST NOT COST A FINISHED RENDER. OpenEXR type-checks its own standard attribute names and
    # raises - and it does so at write() time, not when the header dict is built, so the shape cannot be vetted by
    # constructing a File and looking for an exception (found by mutation 2026-08-12: the construct-only version
    # silently validated nothing). The real case is a plate attribute arriving over the JSON wire: keyCode comes
    # back as a list, chromaticities as a list, and str() of either is a value the library rejects outright.
    # So: retry, probing each attribute with a genuine 1x1 write, and drop the ones that will not go. The happy
    # path pays nothing for this - it already returned above.
    probe = os.path.join(tempfile.gettempdir(), f"ocio_attr_probe_{os.getpid()}.exr")
    dummy = {"RGB": np.zeros((1, 1, 3), np.float16)}
    safe = {"compression": header["compression"], "type": header["type"]}
    for k, v in header.items():
        if k in safe:
            continue
        try:
            _emit({**safe, k: v}, probe, dummy)
            safe[k] = v
        except Exception:
            continue                          # this ONE attribute cannot be written; the frame still ships
    try:
        os.remove(probe)
    except Exception:
        pass
    _emit(safe, path, channels)               # if THIS raises, the failure is the pixels, not a metadata detail
    return path


# --------------------------------------------------------------------------- metadata AUTHORING (output side)
# RESPONSIBLE FOR: telling the receiving application what our own render is, so Nuke / Resolve / Premiere / AE do
# not need a hand-set colorspace on every import. Until 2026-08-12 an EXR left this pack carrying nine mandatory
# attributes and nothing else. Authoring on OUTPUT comes FIRST here; pass-through from an input plate is second,
# because the normal graph is generate-in-ComfyUI -> deliver-to-an-NLE, not plate-in-plate-out.

# The config's own standard interchange roles (OCIO v2). Named by ROLE, not by colorspace name: the literal
# names in the ACES 2.0 studio config are "CIE XYZ-D65 - Display-referred" / " - Scene-referred" AND BOTH ARE
# INACTIVE, so getColorSpaces() does not list them and a hardcoded name is a landmine.
_ROLE_XYZ_D65 = "cie_xyz_d65_interchange"
_ROLE_ACES = "aces_interchange"

# Published chromaticities, used as ANCHORS that must agree with what OCIO derives - not as the primary source.
# Anchoring is what makes the derivation safe to ship: a config change, a renamed colorspace or the wrong
# interchange hub shows up as a mismatch and we omit the attribute instead of writing a plausible-looking lie.
# Flat 8: Rx Ry Gx Gy Bx By Wx Wy (the order OpenEXR's chromaticities attribute takes).
_GAMUT_ANCHORS = (
    ("Rec.709",  (0.640,  0.330,  0.300,  0.600,  0.150,  0.060,  0.3127,  0.3290)),    # ITU-R BT.709-6 = sRGB IEC 61966-2-1
    ("P3-D65",   (0.680,  0.320,  0.265,  0.690,  0.150,  0.060,  0.3127,  0.3290)),    # SMPTE RP 431-2 primaries at D65 (Display P3)
    ("Rec.2020", (0.708,  0.292,  0.170,  0.797,  0.131,  0.046,  0.3127,  0.3290)),    # ITU-R BT.2020-2, also BT.2100
    ("AdobeRGB", (0.640,  0.330,  0.210,  0.710,  0.150,  0.060,  0.3127,  0.3290)),    # Adobe RGB (1998)
    ("AP1",      (0.713,  0.293,  0.165,  0.830,  0.128,  0.044,  0.32168, 0.33767)),   # ACES AP1 (ACEScg / ACEScct)
    ("AP0",      (0.7347, 0.2653, 0.0,    1.0,    0.0001, -0.0770, 0.32168, 0.33767)),  # SMPTE ST 2065-1 (ACES2065-1)
)
# Separates a hit from a miss with room to spare. MEASURED on this config: a hub whose white matches the
# colorspace's adopted white reproduces its anchor to ~1e-5 (sRGB via the D65 hub came back 0.640/0.330 exactly;
# ACEScg via the ACES hub came back 0.713/0.293/0.165/0.830/0.128/0.044/0.32168/0.33767 exactly). The WRONG hub
# misses by >=1.5e-3 (sRGB via the ACES hub -> 0.64249/0.33036, ACEScg via the D65 hub -> 0.1595/0.8388 for
# green). So 5e-4 sits 50x above the noise and 3x below the smallest real error.
_CHROMA_TOL = 5e-4


def _chroma_rgb_to_xyz(ch8):
    """Primaries + white (flat 8 xy) -> the 3x3 RGB->XYZ matrix, by the standard construction (SMPTE RP 177).
    Computed here rather than stored so no colour matrix is hardcoded anywhere in this file: the only constants
    are chromaticity pairs, which is the literal content of the attribute being written."""
    xr, yr, xg, yg, xb, yb, xw, yw = (float(v) for v in ch8)
    m = np.array([[xr / yr, xg / yg, xb / yb],
                  [1.0, 1.0, 1.0],
                  [(1.0 - xr - yr) / yr, (1.0 - xg - yg) / yg, (1.0 - xb - yb) / yb]], np.float64)
    w = np.array([xw / yw, 1.0, (1.0 - xw - yw) / yw], np.float64)
    return m * np.linalg.solve(m, w)


# AP0 -> XYZ, built at import from the ST 2065-1 anchor above (see _chroma_rgb_to_xyz). The ACES hub hands back
# AP0 RGB; this turns it into XYZ *without* a chromatic adaptation, which is the whole point of using it.
_AP0_TO_XYZ = _chroma_rgb_to_xyz(dict(_GAMUT_ANCHORS)["AP0"])


def _primaries_via_hub(cfg, cfg_key, cs, hub, hub_to_xyz=None):
    """Push pure R, G, B and white through OCIO into `hub` and read back their CIE xy. Returns a flat 8-tuple,
    or None if the transform does not exist.

    hub_to_xyz: None when the hub IS an XYZ space; otherwise the matrix that takes hub RGB to XYZ.

    WHY TWO HUBS (measured 2026-08-12, and the reason the obvious one-hub version is wrong): an OCIO colorspace
    conversion is relative-colorimetric, so it carries a chromatic adaptation whenever the two spaces disagree
    about white. Route ACEScg through the D65 hub and every value comes back Bradford-adapted - green lands at
    (0.1595, 0.8388) instead of AP1's (0.165, 0.830), and white reads D65 instead of the ACES white. Stamping
    that into chromaticities would be a header describing a gamut nobody encoded. So each family goes through
    the hub that shares its white, and the anchor check below is what proves the right one was used."""
    try:
        cpu = _cached_cpu_processor(cfg_key, ("primaries", cs, hub), lambda: cfg.getProcessor(cs, hub))
    except Exception:
        return None
    out = []
    for rgb in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 1.0, 1.0)):
        v = np.array(rgb, np.float32).reshape(1, 1, 3).copy()
        try:
            cpu.applyRGB(v)
        except Exception:
            return None
        vec = np.asarray(v, np.float64).reshape(3)
        x, y, z = (_AP0_TO_XYZ @ vec) if hub_to_xyz is not None else vec
        s = x + y + z
        if not np.isfinite(s) or abs(s) < 1e-12:
            return None
        out += [float(x / s), float(y / s)]
    return tuple(out)


def _derive_chromaticities(cs):
    """(chromaticities 8-tuple, gamut name) for a colorspace, or (None, None) when we cannot stand behind it.

    Derived from the live OCIO config, then required to agree with a published anchor. A colorspace whose gamut
    is not in _GAMUT_ANCHORS gets NOTHING - measured example of why: 'Linear ARRI Wide Gamut 4' comes back
    (0.7348, 0.2649) / (0.1439, 0.8611) / (0.0981, -0.0322) through the D65 hub, which is ~1.5e-3 off the AWG4
    numbers, so we do not have a hub that reproduces it and we do not pretend otherwise."""
    if not cs:
        return None, None
    try:
        _require_ocio()
        cfg, cfg_key = _resolve_config_keyed("")
        if cfg is None:
            return None, None
        space = cfg.getColorSpace(cs)
        if space is None or (space.getEncoding() or "").lower() == "data":
            return None, None                     # 'Raw' carries no colorimetry; saying it does is worse than silence
        for hub, m in ((_ROLE_XYZ_D65, None), (_ROLE_ACES, _AP0_TO_XYZ)):
            got = _primaries_via_hub(cfg, cfg_key, cs, hub, m)
            if got is None:
                continue
            for name, anchor in _GAMUT_ANCHORS:
                if max(abs(a - b) for a, b in zip(got, anchor)) <= _CHROMA_TOL:
                    return tuple(float(v) for v in anchor), name
    except Exception:
        return None, None
    return None, None


# Colour-interop IDs come from the CONFIG'S OWN ALIASES, not from a table in this file: the ACES 2.0 studio
# config carries the Color Interop Forum names (lin_ap1_scene, srgb_rec709_display, rec2100_hlg_display, ...)
# precisely so a writer does not have to invent them. Only an UNPREFIXED alias in the registered
# <transfer>_<gamut>_<scene|display> shape is used - the config marks its own non-registered inventions with an
# "ocio:" prefix (ocio:acescct_ap1_scene, ocio:lin_awg4_scene), and passing one of those off as a registered
# interop ID is exactly the guess this attribute exists to avoid. Confirmed downstream: oiiotool reads
# colorInteropID "lin_ap1" back as oiio:ColorSpace. NOTE the limit: Nuke's own EXR reader shows the attribute in
# the metadata panel but its Read node colorspace stays a manual widget - this helps OIIO-based readers
# (Katana, Blender, Houdini, Arnold, oiiotool), not Nuke.
_INTEROP_RE = re.compile(r"^[a-z0-9][a-z0-9_.]*(?:_scene|_display)$")


def _interop_id(cs):
    """The registered colour-interop ID for a colorspace, or None. Read off the config's alias list."""
    if not cs:
        return None
    try:
        _require_ocio()
        cfg, _ = _resolve_config_keyed("")
        if cfg is None:
            return None
        space = cfg.getColorSpace(cs)
        if space is None or (space.getEncoding() or "").lower() == "data":
            return None
        cands = [a for a in space.getAliases() if _INTEROP_RE.match(a or "")]
        return max(cands, key=len) if cands else None      # most specific of the registered spellings
    except Exception:
        return None


# Drop-frame exists ONLY at 29.97 and 59.94 (SMPTE ST 12-1:2014). 23.976 is COUNTED AS 24 NON-DROP - the pack's
# own _SEQ_FPS_DEFAULT is 23.976, so this case is not hypothetical, it is the default. Setting dropFrame
# unconditionally (or from "is this rate fractional") mislabels every 23.976 sequence.
_DROP_FRAME_RATES = (29.97, 59.94)


def _tc_nominal_rate(fps):
    """Timecode counting rate: the INTEGER frames-per-second a timecode counts to before rolling the second.
    Timecode stores no rate of its own (ST 12-1), so 23.976 counts to 24, 29.97 to 30, 59.94 to 60."""
    r = float(fps) if fps and float(fps) > 0 else 24.0
    return max(1, int(round(r)))


def _is_drop_frame(fps):
    r = float(fps) if fps and float(fps) > 0 else 0.0
    return any(abs(r - d) < 0.01 for d in _DROP_FRAME_RATES)


def _parse_timecode(s):
    """'01:00:00:00' -> (h, m, s, f, drop). Raises on anything it cannot read rather than silently starting
    at zero - a wrong start timecode conforms the whole delivery to the wrong place, so it must not fail
    quietly.

    THE FIFTH FIELD IS THE POINT. ';' before the frames is SMPTE's drop-frame marker and ':' is non-drop;
    both are legal at 29.97 and 59.94 and both are in daily use. This used to return four fields, throwing
    that away, and everything downstream then re-derived drop status from the frame rate - which is a guess,
    and it was wrong in both directions (see _tc_advance). '.' is accepted as the sloppy separator and, like
    ':', means non-drop.

    Returns 5 fields where it used to return 4, deliberately: every caller that still unpacks four gets a
    loud error instead of a value that quietly means something else."""
    txt = (str(s or "").strip())
    if not txt:
        return None
    m = re.match(r"^(\d{1,2})[:;.](\d{1,2})[:;.](\d{1,2})([:;.])(\d{1,3})$", txt)
    if not m:
        raise ValueError(f"OCIO Write: timecode {txt!r} is not HH:MM:SS:FF (e.g. 01:00:00:00). "
                         "Leave it empty to write no timecode.")
    h, mi, se = (int(g) for g in m.group(1, 2, 3))
    fr = int(m.group(5))
    drop = m.group(4) == ";"
    if h > 23 or mi > 59 or se > 59:
        raise ValueError(f"OCIO Write: timecode {txt!r} is out of range (HH<=23, MM<=59, SS<=59).")
    return h, mi, se, fr, drop


def _tc_advance(start, offset, fps, drop=None):
    """Advance a timecode by `offset` FRAMES. Returns (h, m, s, f, drop_frame).

    THE POINT OF THIS FUNCTION: OCIO Write emits N files from one settings dict, so writing the start timecode
    into every header gives a sequence where every frame claims the same instant. Resolve and Premiere accept
    that without a word and conform it wrong. Frame numbers advance, so the timecode must too.

    Drop-frame counting (29.97 / 59.94 only) skips frame numbers 00 and 01 of every minute except every tenth,
    per SMPTE ST 12-1:2014 - it drops LABELS, never pictures.

    `drop` OVERRIDES THE GUESS FROM fps, and a caller that knows must pass it. At 29.97 and 59.94 BOTH counts
    are legal and both are in daily use, so deriving drop status from the rate alone is wrong in both
    directions - measured, and both were live:

      * A legal NON-drop plate lost its timecode entirely. 01:01:00:00 at 29.97 is an ordinary NDF label, but
        the rate-derived guess declared the clip drop-frame, the drop-frame validator then rejected frames
        00/01 at minute 01, and the code vanished from every written header with no reason given.
      * A real drop-frame plate was renumbered. Its own flag was parsed and discarded, so a 00:00:59;29 start
        came back as 00:01:00;02 where the NDF truth is 00:01:00;00: a two-frame conform error, produced from
        a signal that was already in the data.

    The source's own answer wins; the rate is the fallback for a code that carries no answer."""
    h, mi, se, fr = start[:4]
    nom = _tc_nominal_rate(fps)
    if drop is None and len(start) > 4:
        drop = bool(start[4])                          # the plate said so itself
    drop = _is_drop_frame(fps) if drop is None else bool(drop)
    if drop and not _is_drop_frame(fps):
        drop = False                                   # drop-frame does not exist outside 29.97 / 59.94
    total = ((h * 60 + mi) * 60 + se) * nom + fr + int(offset)
    if drop:
        # THE TWO DIRECTIONS HAVE TO USE THE SAME COUNTING. The line above converts the start LABEL to a frame
        # count with the nominal formula, while the branch below decodes a count back to a label with the
        # drop-frame formula. Left unmatched they disagree by exactly the labels SMPTE tells you to skip, so
        # the typed start silently drifts forward: at 29.97 the widget's own default 01:00:00:00 stamped
        # 01:00:03;18 - 108 frames, 3.60 s - and 10:00:00:00 stamped 10:00:36;00, 36 s. Measured end to end in
        # a real ProRes tmcd track, not inferred. The function already knew the right answer in one direction:
        # offset 107892 decodes to 01:00:00;00, which is what one hour at 29.97 is.
        #
        # A drop event happens on ENTERING minute k for every k that is not a multiple of ten, so after `tm`
        # elapsed minutes the number of events is tm - tm//10. Checked against the published anchors rather
        # than reasoned: 60 minutes at 29.97 gives 108000 - 2*(60-6) = 107892, and the boundaries
        # 00:00:59;29 -> 00:01:00;02 and 00:09:59;29 -> 00:10:00;00 both fall out of it.
        tm = h * 60 + mi
        total -= (nom // 15) * (tm - tm // 10)
        # An illegal drop-frame label has no frame 00 or 01 at a minute that is not a multiple of ten. Silently
        # accepting one puts the whole delivery an unknown distance from where the artist asked for it, and the
        # docstring of _parse_timecode already commits to failing loudly on exactly that class of mistake.
        if fr < (nom // 15) and mi % 10 != 0 and se == 0:
            raise ValueError(
                f"OCIO Write: timecode "
                f"{_timecode_string(h, mi, se, fr, True)} is not a legal drop-frame code at "
                f"{fps:g} fps - frames 00 and 01 do not exist at minute {mi:02d} (SMPTE ST 12-1). "
                f"Use {_timecode_string(h, mi, se, nom // 15, True)} or a minute that is a multiple of ten.")
        # count in dropped labels: per 10 minutes, 9 minutes lose (nom // 15) labels each (2 at 30, 4 at 60)
        d = nom // 15
        per_min, per_10min = nom * 60 - d, (nom * 60) * 10 - 9 * d
        total %= per_10min * 6 * 24
        ten, rem = divmod(total, per_10min)
        if rem < per_min + d:                     # inside the first minute of the ten - nothing dropped yet
            m_in, f_in = 0, rem
        else:
            m_in, f_in = divmod(rem - (per_min + d), per_min)
            m_in, f_in = m_in + 1, f_in + d       # every later minute starts at frame label `d`
        mins_total = ten * 10 + m_in
        se, fr = divmod(f_in, nom)
        h, mi = divmod(mins_total, 60)
        h %= 24
    else:
        total %= nom * 60 * 60 * 24
        rest, fr = divmod(total, nom)
        rest, se = divmod(rest, 60)
        h, mi = divmod(rest, 60)
    return h, mi, se, fr, drop


def _timecode_string(h, mi, se, fr, drop=False):
    return f"{h:02d}:{mi:02d}:{se:02d}{';' if drop else ':'}{fr:02d}"


def _exr_timecode(start, offset, fps):
    """An OpenEXR.TimeCode for frame `offset` of the write. A plain string would store as a str-typed attribute -
    right text, wrong type - so a standards-aware reader ignores it (measured on 3.4.13)."""
    import OpenEXR
    h, mi, se, fr, drop = _tc_advance(start, offset, fps)
    tc = OpenEXR.TimeCode()
    tc.hours, tc.minutes, tc.seconds, tc.frame = h, mi, se, fr
    tc.dropFrame = bool(drop)
    return tc


# Metadata that describes ONE SPECIFIC PIXEL STATE and becomes a lie the moment a colour transform runs. None of
# it may cross this node, in either direction. A C2PA manifest is cryptographically bound to the bytes it was
# signed over (and does not embed in EXR or DPX at all); ST 2086 mastering-display primaries and ST 2094 dynamic
# HDR metadata describe the display the ORIGINAL was graded on; an ACES AMF names the transforms that produced
# the file it shipped with; an MHL is a hash manifest of files that no longer exist once we write new ones.
# Copying any of them forward produces a file that carries a confident, checkable, WRONG claim about itself -
# worse than a file that says nothing. Matched case-insensitively on substrings because every one of these
# arrives under several spellings depending on who wrote it.
_META_FORBIDDEN = (
    "c2pa", "jumbf", "contentcredential", "content_credential",       # C2PA 2.4 manifests / their container
    "mastering", "st2086", "smpte2086", "masteringdisplay",           # ST 2086 static HDR
    "st2094", "smpte2094", "hdr10plus", "hdr_10_plus", "dovi", "dolbyvision", "dynamichdr",   # ST 2094 dynamic HDR
    "amf", "acesmetadatafile", "aces_metadata",                       # ACES AMF sidecar content
    "mhl", "hashlist", "asc_mhl",                                     # ASC MHL hash manifests
    "xmp:contentcredentials",
)


# Attributes that describe the INCOMING file's colour or timing and are re-authored for the outgoing one. Kept
# separate from _META_FORBIDDEN because these are not lies about provenance, they are simply superseded - and they
# must be dropped even when we cannot author a replacement. A plate's chromaticities on a converted render is the
# same class of error as a stale dataWindow, and a single static plate timecode copied onto every output frame is
# exactly the sequence-wide wrong conform this node's per-frame timecode exists to prevent.
#
# whiteLuminance STAYS IN THIS SET, and it is the one entry we author no replacement for. That asymmetry was
# examined on 2026-08-12 and deliberately kept, because the obvious tidy-up - "we cannot author it, so drop the
# promise and let the plate's value survive" - was tried and is WRONG:
#
#   The attribute is defined as the luminance, in candelas per square metre, of the RGB value (1,1,1). This node's
#   entire job is to change what the code values mean, from_colorspace -> output_colorspace. A gamut-only change
#   maps white to white and would keep the number true; a TRANSFER change does not, and this node ships a preset
#   that does exactly that (ACEScct). So an inherited value becomes a confident, checkable, WRONG statement about
#   the file for a conversion the node performs by design - the same class as a stale dataWindow or a plate's
#   chromaticities on a converted render, which is what this whole set exists to stop.
#
#   MEASURED, and it is worse than the argument alone: OpenEXR does NOT type-check this name, so with the strip
#   removed a plate value of [1, 2, 3] arriving over the JSON wire landed in a delivered header as a V3f
#   array([1., 2., 3.]) - a three-vector for an attribute the specification defines as one float. Nothing
#   rejected it. tools/test_write_metadata.py already asserted against exactly this and caught it.
#
# Not knowing the true value is a reason not to inherit someone else's, not a reason to pass it on. OCIO's
# `white_luminance` role is the config's reference display rather than a property of this file, so it is not a
# substitute either: authoring nothing is correct, and so is dropping it.
_META_RE_AUTHORED = frozenset({
    "chromaticities", "adoptedNeutral", "whiteLuminance", "colorInteropID",
    "framesPerSecond", "captureRate", "timeCode", "imageCounter",
})
# The same names normalised, because a plate spells them however its writer felt like. Measured on a real
# camera master: DaVinci Resolve's MXF writes `timecode`, this set writes `timeCode`, and an exact-match strip
# let the plate's value through - so the delivered EXR carried two timecodes at once, ours advancing per frame
# and the plate's frozen at the start. Case and separators are dropped, the way _forbidden_meta_keys already
# normalises, and the timecode spellings this pack knows from elsewhere are folded in rather than restated.
_META_RE_AUTHORED_NORM = frozenset(
    k.lower().replace(" ", "").replace("-", "").replace("_", "")
    for k in tuple(_META_RE_AUTHORED) + ("timecode", "dpx:TimeCode", "smpte:TimeCode")
)


def _is_re_authored(key):
    """True when this incoming key names something OCIO Write authors itself, in any spelling."""
    k = str(key).lower().replace(" ", "").replace("-", "").replace("_", "")
    return k in _META_RE_AUTHORED_NORM


def _forbidden_meta_keys(attrs):
    """Which incoming keys are pixel-state claims that must not survive a colour transform."""
    out = []
    for k in (attrs or {}):
        low = str(k).lower().replace(" ", "").replace("-", "")
        if any(bad in low for bad in _META_FORBIDDEN):
            out.append(str(k))
    return out


def _authored_attrs(output_colorspace, fps, frame_number, start_tc, raw_data=False):
    """The attributes OCIO Write puts on its OWN output, derived from the settings it was given.

    frame_number: the file's frame number, written as imageCounter and used to advance the timecode.
    start_tc: parsed (h, m, s, f) or None.

    raw_data writes NO colorimetry: 'raw' means the pixels were not converted, so we do not know what they are
    and must not claim a gamut. Rate and counter still go in - those are true either way."""
    attrs = {}
    if not raw_data:
        chroma, gamut = _derive_chromaticities(output_colorspace)
        if chroma:
            attrs["chromaticities"] = chroma
            attrs["com.ocio.gamut"] = gamut                 # our own note, in our own namespace, not a standard name
            # adoptedNeutral: the CIE xy the file's neutral axis sits on. It is the last pair of the flat 8
            # (Rx Ry Gx Gy Bx By Wx Wy), so it is the SAME published anchor already being written above rather
            # than a second derivation that could disagree with it - AP1 gives (0.32168, 0.33767), Rec.709 /
            # P3-D65 / Rec.2020 give D65 (0.3127, 0.3290). Until now it was stripped from the incoming plate on
            # a promise to re-author that was never kept, so the plate's value was destroyed and nothing
            # replaced it: worse than either leaving it or writing ours.
            # A TUPLE, NOT A LIST, and that is load-bearing: OpenEXR types this name as a v2f and rejects a list
            # outright ("invalid value for attribute 'adoptedNeutral': expected a v2f", measured on 3.4.13),
            # while the 2-tuple round-trips. _save_exr_with_meta would survive the rejection by dropping the
            # attribute, so a list here would fail SILENTLY rather than loudly.
            attrs["adoptedNeutral"] = (float(chroma[6]), float(chroma[7]))
        iid = _interop_id(output_colorspace)
        if iid:
            attrs["colorInteropID"] = iid
        if output_colorspace:
            attrs["com.ocio.colorspace"] = str(output_colorspace)
    r = float(fps) if fps and float(fps) > 0 else 0.0
    if r > 0:
        # STANDARD TYPE IS Rational AND WE WRITE float: OpenEXR 3.4.13 raises "unrecognized type of attribute"
        # on its own Rational class here (measured), so float is a deliberate, named deviation - 23.976 lands as
        # 23.975999... rather than exactly 24000/1001.
        attrs["framesPerSecond"] = r
        attrs["captureRate"] = r
    if frame_number is not None:
        attrs["imageCounter"] = int(frame_number)
    if start_tc is not None:
        attrs["timeCode"] = ("__TIMECODE__", start_tc, r)   # resolved per frame by _frame_attrs
    return attrs


def _frame_attrs(attrs, frame_offset, as_text=False):
    """Per-frame copy of an attribute dict: resolves the timeCode placeholder for THIS frame and bumps
    imageCounter. Everything else is shot-level and identical across the sequence.

    as_text: resolve timeCode to the SMPTE string instead of an OpenEXR.TimeCode object. TIFF, PNG and the
    sidecar .json all need text; only an EXR header takes the object. The placeholder stays the single source of
    the per-frame advance either way - resolving it twice from two tables is how a sequence ends up with a
    header and a sidecar that disagree about where it starts."""
    if not attrs:
        return attrs
    out = dict(attrs)
    tc = out.get("timeCode")
    if isinstance(tc, tuple) and len(tc) == 3 and tc[0] == "__TIMECODE__":
        if as_text:
            try:
                out["timeCode"] = _timecode_string(*_tc_advance(tc[1], frame_offset, tc[2]))
            except Exception:
                out.pop("timeCode", None)                   # never the reason a frame fails to write
        else:
            try:
                out["timeCode"] = _exr_timecode(tc[1], frame_offset, tc[2])
            except Exception:
                # WIDER THAN ImportError, which is all this caught until 2026-08-13, while the text branch
                # three lines up already caught everything. The asymmetry became a real failure once the start
                # stopped being typed here and started arriving from a foreign file: _tc_advance REJECTS an
                # illegal drop-frame label (frames 00/01 do not exist at a minute that is not a multiple of ten
                # at 29.97, SMPTE ST 12-1), which is correct for a code a human entered and fatal for one a
                # plate carried - the whole write died on someone else's malformed header. Found by a mutation
                # pass: the case that exposed it must PARSE and still be illegal, so "banana" never reached
                # here at all. A frame ships without a timecode rather than not shipping.
                out.pop("timeCode", None)
    if "imageCounter" in out:
        out["imageCounter"] = int(out["imageCounter"]) + int(frame_offset)
    return out


# Container tag matrix, MEASURED against this ffmpeg build (2024-10-02 gyan.dev full_build) by writing a file
# per tag and reading it back: ffmpeg accepts -metadata <anything> and SILENTLY DROPS whatever the container
# cannot represent, so an unfiltered dump looks like it worked and delivers nothing. Everything outside these
# sets - reel_name, lensModel, shot/scene/take, any show attribute - survives in NO container and ships in the
# sidecar .json instead. 'timecode' is deliberately absent because it has its own ffmpeg option (see save_video);
# routing it as a generic tag here would be a second answer to the same question.
_VIDEO_TAGS_MP4 = frozenset({"title", "artist", "album", "album_artist", "composer", "comment", "description",
                             "copyright", "date", "genre"})

# A ProRes / DNxHR .mov is a DELIVERABLE, and a deliverable that identifies itself only by its filename is a
# support ticket waiting to happen. QuickTime's udta box takes arbitrary keys, so a MOV can carry the whole shot
# identity - but only with `-movflags use_metadata_tags`, which is off by default.
#
# MEASURED, three arms on real ProRes 4444 encodes read back with ffprobe: plain -metadata kept 5 of 14 tags;
# adding use_metadata_tags kept 14 of 14; adding the Apple ProApps keys kept 20 of 20, including all six of
# com.apple.proapps.{reel,scene,shot,cameraName,clipID,originalFormat}, which is what Resolve and Final Cut
# read natively for reel / scene / shot. So the old nine-tag whitelist was not a QuickTime limit at all - it
# was ffmpeg's default, and it was throwing away lens, focal length, take, camera and reel.
#
# The whitelist stays for MP4, whose ilst box really is restrictive.
#
# Keys that already have their OWN dedicated ffmpeg route and must NOT also travel as generic -metadata.
# Dropping the whitelist re-opened this: the first version of the widened MOV branch sent `timecode=...` as a
# plain tag alongside the `-timecode` option, and two routes for one value is how they drift apart. The pack's
# existing test caught it, which is the gate doing exactly what it is there for.
_VIDEO_TAGS_OWN_ROUTE = frozenset({
    "timecode", "color_primaries", "colour_primaries", "color_trc", "colorspace", "color_range",
    "chromaticities", "framespersecond", "capturerate",
})
#
# Source keys that a post tool looks for under a different spelling, so the movie is self-describing without
# the artist having to rename anything. Values come from the source metadata; nothing is invented.
_PROAPPS_FROM = {
    "reel_name": "com.apple.proapps.reel",
    "reel": "com.apple.proapps.reel",
    "dpx:FileName": "com.apple.proapps.reel",
    "scene": "com.apple.proapps.scene",
    "shot": "com.apple.proapps.shot",
    "cameraMake": "com.apple.proapps.cameraName",
    "cameraModel": "com.apple.proapps.cameraName",
    "model": "com.apple.proapps.cameraName",
    "dpx:InputDevice": "com.apple.proapps.originalFormat",
}


def _looks_like_a_path(value):
    """Would this value expose a filesystem location if it travelled inside a delivered file?

    BOTH separators are tested, not `os.path.sep`. A first version checked only `os.path.sep`, which on Windows
    is a backslash, and let `D:/secret/path/x` straight into a MOV because the value happened to use forward
    slashes. The data does not have to agree with the host's convention - ComfyUI passes forward slashes
    routinely - so the check cannot depend on it.
    """
    s = str(value)
    if len(s) < 3:
        return False
    has_sep = "/" in s or "\\" in s
    drive = len(s) > 2 and s[1] == ":" and s[0].isalpha()      # D:\x or D:/x
    unc = s.startswith("\\\\") or s.startswith("//")
    rooted = s.startswith(("/", "\\"))
    return (has_sep and (drive or unc or rooted)) or drive


# ONE predicate for "this must not enter a delivered file", used by EVERY writer in this module (EXR header, MOV /
# MP4 / MXF container tags, TIFF tags, PNG text chunks) and by the sidecar's kept/not-kept split. Added
# 2026-08-12 with the universal sidecar.
#
# WHY IT IS SHARED RATHER THAN PER-FORMAT: two real leaks were measured while extending this, both from the same
# cause - the guard existed on ONE path and the others each grew their own answer or none at all.
#   1. _save_exr_with_meta applied NO guard whatsoever. Reproduced: an EXR left this pack carrying
#      output_folder="D:/secret/project/shots" and ComfyUI's whole `prompt` graph JSON in its header. That is
#      reachable from a normal graph, not a hypothetical - _read_pil_meta returns a ComfyUI PNG's text chunks,
#      which are exactly `prompt` and `workflow`, and OCIO Read -> OCIO Write(EXR) then stamped them into the
#      delivered header. It is the leak this pack refuses in MOV, arriving through the format it writes most.
#   2. The MOV branch's own forbidden test was `kl in _META_FORBIDDEN`, an EXACT membership test against a tuple
#      of SUBSTRINGS. So it matched a key spelled exactly "c2pa" and nothing else: a real "c2pa.manifest" was
#      passed to the container. Measured. It never fired in practice only because OCIOWrite.write() strips those
#      keys earlier via _forbidden_meta_keys - a guard that cannot fire is not a guard.
# Substring matching (and the same normalisation _forbidden_meta_keys uses) is the correct form, because every
# one of these arrives under several spellings depending on who wrote it.
def _meta_is_private(key, value):
    """True when this key/value must not be written into ANY delivered file, in any format."""
    kl = str(key).lower()
    if any(bad in kl.replace(" ", "").replace("-", "") for bad in _META_FORBIDDEN):
        return True
    if any(kl.startswith(p) for p in ("prompt", "workflow", "extra_pnginfo")):
        return True                                            # ComfyUI's embedded graph: machine paths inside JSON
    return _looks_like_a_path(value)


# The shot's IDENTITY: the seven fields that answer "which picture is this" and the spellings they arrive under.
# Used for the formats whose containers hold only a handful of strings (TIFF tags, PNG text) - an EXR gets the
# full attribute set instead, and the sidecar gets everything regardless. Values come from the source metadata or
# from what this node authored; nothing is invented, and a field with no value is simply absent.
_IDENTITY_FROM = {
    "reel": ("reel_name", "reel", "dpx:FileName", "com.apple.proapps.reel", "DocumentName"),
    "scene": ("scene", "dpx:Scene", "com.apple.proapps.scene"),
    "shot": ("shot", "dpx:Shot", "com.apple.proapps.shot"),
    "take": ("take", "dpx:Take", "com.apple.proapps.take"),
    "camera": ("cameraModel", "model", "Model", "cameraMake", "make", "Make", "camera",
               "dpx:InputDevice", "com.apple.proapps.cameraName"),
    "lens": ("lens", "lensModel", "LensModel", "dpx:Lens"),
    "timecode": ("timeCode", "timecode", "dpx:TimeCode"),
}


def _looks_like_umid(value):
    """True when this value is a SMPTE UMID rather than anything a person named.

    A UMID (SMPTE ST 330M) is a machine identifier: 32 octets for the Basic form, 64 for the Extended, and it
    opens with the SMPTE Universal Label prefix 06 0A 2B 34. A REEL NAME is a different kind of thing entirely
    and a much smaller one - 8 characters in a CMX3600 EDL, up to 32 on Avid - so a UMID cannot be a reel name
    even in principle; it does not fit in the field it would have to travel in.

    It ends up in the reel field anyway because some applications park it there: measured on a real ProRes 4444
    XQ master, DaVinci Resolve writes `com.apple.proapps.reel=0x060A2B34...`. Reporting that as the shot's reel
    puts a 64-character hash where an assistant editor expects `A001R2XY`, and nothing downstream can use it.
    The value is NOT discarded - it stays in the attributes and is written to the delivered file under its own
    key. It is only refused the claim of being an identity field.
    """
    s = str(value or "").strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if not s or len(s) < 32 or any(c not in "0123456789abcdef" for c in s):
        return False
    return s.startswith("060a2b34") or len(s) in (64, 128)


def _first_meta(attrs, spellings):
    """The first of `spellings` present in attrs as usable text, or None. Same refusals as _identity_meta."""
    for k in spellings:
        if k not in (attrs or {}):
            continue
        v = attrs[k]
        if v is None or isinstance(v, bool) or not isinstance(v, (str, int, float)):
            continue
        s = str(v).strip()
        if s and not _meta_is_private(k, s) and not _looks_like_umid(s):
            return s
    return None


def _timecode_from_source(attrs):
    """The start timecode INHERITED from the plate, as (h, m, s, f), or None when it carries none.

    OCIO Write has no timecode field: a code typed into the writer is a code invented at delivery, while the
    one that has to survive the round trip arrives with the plate. Both spellings this pack actually meets are
    accepted - the SMPTE string ('14:48:24:22') and OpenEXR's TimeCode tuple rendered as text
    ('(14, 48, 24, 22, 0, 0, 0, 0, 0, 0)'), whose first four fields are hours, minutes, seconds, frame.

    NEVER RAISES, which is the difference from _parse_timecode: that one guards a value a human typed, where
    silence would conform the delivery to the wrong place. This one reads whatever a foreign file happened to
    carry, and a plate with an unreadable code must write no timecode rather than fail the render.
    """
    raw = _first_meta(attrs, _IDENTITY_FROM["timecode"])
    if not raw:
        return None
    try:
        return _parse_timecode(raw)
    except ValueError:
        pass
    # OpenEXR's TimeCode stringifies as its field tuple. The FIFTH field is dropFrame, and it is read here
    # rather than re-derived from the frame rate: at 29.97 and 59.94 both counts are legal, and guessing was
    # renumbering real drop-frame plates by two frames while throwing away legal non-drop ones entirely.
    # Confirmed against the module: OpenEXR.TimeCode(..., dropFrame=True) renders as (h, m, s, f, 1, 0, ...).
    m = re.match(r"^\(\s*(\d{1,2})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*,\s*(\d{1,3})\s*(?:,\s*(\d+))?", str(raw).strip())
    if not m:
        return None
    h, mi, se, fr = (int(g) for g in m.group(1, 2, 3, 4))
    drop = bool(int(m.group(5))) if m.group(5) is not None else False
    if h > 23 or mi > 59 or se > 59:
        return None
    return h, mi, se, fr, drop


def _identity_meta(attrs):
    """The identity set as {field: text}, taking the first spelling present. Private values are refused by the
    same predicate every other writer uses, so a reel field holding a filesystem path is dropped rather than
    delivered. Returns text only: TIFF tags and PNG text chunks are strings, and timeCode has already been
    resolved to a SMPTE string by _frame_attrs(as_text=True)."""
    out = {}
    for field, spellings in _IDENTITY_FROM.items():
        s = _first_meta(attrs, spellings)                       # one shared lookup; see _first_meta
        if s:
            out[field] = s
    return out


def _video_tag_args(out_path, attrs):
    """-metadata args for this container, plus the movflag a MOV needs to keep them.

    A MOV gets everything scalar that is not forbidden and is not a filesystem path; an MP4 gets the whitelist.
    The exclusions matter more here than the old whitelist ever did: passing EVERYTHING would put absolute
    machine paths and embedded graph JSON into a delivered file, which is precisely the leak this pack refuses
    elsewhere - and it is what ComfyUI's own SaveVideo does.
    """
    low = str(out_path).lower()
    is_mov, is_mxf = low.endswith(".mov"), low.endswith(".mxf")
    args = []
    proapps = {}
    for k, v in (attrs or {}).items():
        kl = str(k).lower()
        if not isinstance(v, (str, int, float)) or isinstance(v, bool):
            continue
        if _meta_is_private(k, v):
            continue                                    # one shared guard; see _meta_is_private
        if kl in _VIDEO_TAGS_OWN_ROUTE:
            continue                                    # has its own ffmpeg option; two routes for one value drift
        if is_mxf:
            # MEASURED on this build, DNxHR HQ in -f mxf read back with ffprobe: of eleven identity tags passed as
            # plain -metadata, exactly ONE survives - reel_name, which MXF models structurally as the source
            # package name. The other ten are dropped silently, the same way an MP4 drops what its ilst cannot
            # hold. Prefixing the key with `comment_` routes it into the ST 377-1 / DMS-1 USER COMMENTS instead,
            # and there all eleven survive (ffmpeg's mxf muxer option -store_user_comments, default true).
            # So both go out: the plain key for the one the container models itself, and the comment_ copy so the
            # rest are actually in the file. Writing a descriptive-metadata SCHEME is out of scope and is not
            # what this is - it is one existing muxer option, used as documented.
            args += ["-metadata", f"comment_{kl}={v}"]
            if kl in ("reel_name", "reel"):
                args += ["-metadata", f"reel_name={v}"]
            continue
        if is_mov:
            args += ["-metadata", f"{kl}={v}"]
            tgt = _PROAPPS_FROM.get(str(k))
            if tgt and tgt not in proapps:
                proapps[tgt] = v
        elif kl in _VIDEO_TAGS_MP4:
            args += ["-metadata", f"{kl}={v}"]
    for k, v in proapps.items():
        args += ["-metadata", f"{k}={v}"]
    if is_mov:
        # Without this the container silently keeps about a third of what it was handed: measured 5 of 14 tags
        # without the flag, 14 of 14 with it, on real ProRes 4444 encodes read back with ffprobe.
        args += ["-movflags", "use_metadata_tags"]
    return args


def _embedded_meta_keys(out_path, attrs, bit_depth=None):
    """Which of `attrs` the DELIVERED FILE ITSELF carries, as a set of keys. The other half exists only in the
    sidecar. Derived from the SAME predicates and tables the writers use, never from a second list: computing it
    twice is how a sidecar starts lying about what the file holds.

    Every arm below is MEASURED, not assumed - see the comment at each writer for the numbers."""
    low, keys = str(out_path).lower(), set()
    ext = os.path.splitext(low)[1]
    for k, v in (attrs or {}).items():
        kl = str(k).lower()
        if isinstance(v, tuple) and len(v) == 3 and v[0] == "__TIMECODE__":
            continue                                       # unresolved placeholder; never an embedded value
        if _meta_is_private(k, v):
            continue
        scalar = isinstance(v, (str, int, float)) and not isinstance(v, bool)
        if ext == ".exr":
            # _save_exr_with_meta writes every non-structural attribute it is handed, and takes structured
            # standard types (chromaticities tuple, adoptedNeutral v2f, TimeCode) as well as scalars.
            if k not in _EXR_STRUCTURAL and (scalar or isinstance(v, tuple)
                                             or type(v).__name__ in ("TimeCode", "KeyCode", "Chromaticities")):
                keys.add(str(k))
        elif ext in (".tif", ".tiff", ".png"):
            pass                                           # handled below: these carry the identity set only
        elif ext in (".jpg", ".jpeg"):
            pass                                           # JPEG carries the colorspace comment and nothing else
        elif ext == ".mxf":
            if scalar and kl not in _VIDEO_TAGS_OWN_ROUTE:
                keys.add(str(k))                           # as comment_<key> user comments; 11 of 11 measured
        elif ext == ".mov":
            if scalar and kl not in _VIDEO_TAGS_OWN_ROUTE:
                keys.add(str(k))
        elif scalar and kl in _VIDEO_TAGS_MP4:
            keys.add(str(k))                               # .mp4 / .m4v: the ilst box really is restrictive
    if ext in (".tif", ".tiff", ".png"):
        # TIFF tags / PNG iTXt chunks carry the identity set; the key recorded is the INCOMING one whose value
        # was used, so a reader can tell which of its own attributes made it into the file.
        # 16-bit PNG WAS excluded here, on the true-at-the-time grounds that neither cv2 nor Pillow could write
        # text into one. _png_splice_text writes the chunks directly (before IDAT, so OIIO sees them), so both
        # depths now carry the same set and the exclusion would make the sidecar under-report.
        ident_src = {}
        for field, spellings in _IDENTITY_FROM.items():
            for k in spellings:
                if k in (attrs or {}):
                    v = attrs[k]
                    if isinstance(v, (str, int, float)) and not isinstance(v, bool) \
                            and str(v).strip() and not _meta_is_private(k, v):
                        ident_src[field] = str(k)
                        break
        keys |= set(ident_src.values())
    return keys


def _sidecar_path(out_path, strip_frame=False):
    """<out>.json beside the written file. strip_frame drops a .0001 frame number too, so a SEQUENCE gets ONE
    sidecar for the whole run (name_acescg.json) rather than one per frame - the attributes are shot-level, and
    the per-frame parts (timecode, imageCounter) are recorded as a range instead."""
    stem = os.path.splitext(out_path)[0]
    if strip_frame:
        head, _, tail = stem.rpartition(".")
        if head and tail.isdigit():
            stem = head
    return stem + ".json"


def _sidecar_payload(out_path, attrs, timecode, source_meta, fps, codec, kind="video", bit_depth=None,
                     frames=None, first_file=None, last_file=None):
    """What goes in <out>.json, for ANY written format - not just a movie.

    WHY IT IS UNIVERSAL (2026-08-12). It used to be written for video only. But the half of the metadata a file
    cannot hold is not a video problem: a TIFF keeps a handful of tags, a PNG a few text chunks, a JPEG almost
    nothing, and an EXR takes everything but is not what a client asks for. One sidecar beside EVERY written file
    closes that for every format at once, INCLUDING the formats whose container physically cannot take the data.
    Per-format embedding (the TIFF tags and PNG chunks added alongside this) is then an improvement on top rather
    than the rescue.

    Split into what the FILE ITSELF kept and what only this sidecar has, so a reader can see at a glance which
    half is which - a flat dump would leave that ambiguous."""
    # THE SIDECAR IS A DELIVERED FILE TOO, and that is easy to forget because it is "just metadata". It ships in
    # the same folder as the render and travels with it, so the rule that keeps a machine path out of a MOV and out
    # of an EXR header applies here identically. Found by reading a written sidecar rather than by reasoning about
    # it: the payload was dumping every attribute verbatim, so an absolute output_folder and ComfyUI's entire
    # `prompt` graph JSON were sitting in the .json beside the delivery. That predates this function becoming
    # universal - it just used to leak beside a movie only, and making it universal would have multiplied it
    # across every format instead of fixing it.
    # WITHHELD IS NAMED, NOT SILENT: the KEY still appears, because an artist who wired a plate through should be
    # able to see that something was refused and why. Only the VALUE is withheld.
    clean, withheld = {}, []
    for k, v in (attrs or {}).items():
        if isinstance(v, tuple) and len(v) == 3 and v[0] == "__TIMECODE__":
            continue                                       # the per-frame EXR placeholder; the resolved start is below
        if _meta_is_private(k, v):
            withheld.append(str(k))
            continue
        clean[str(k)] = _meta_scalar(v)
    kept = _embedded_meta_keys(out_path, attrs, bit_depth)
    out = {"file": os.path.basename(out_path), "writer": "ComfyUI-OCIO", "codec": str(codec),
           "framesPerSecond": float(fps) if fps else None,
           "container_keeps": sorted(k for k in clean if k in kept),
           "sidecar_only": sorted(k for k in clean if k not in kept),
           "attributes": clean}
    if withheld:
        out["withheld"] = {"keys": sorted(set(withheld)),
                           "reason": "filesystem path, embedded workflow, or a pixel-state claim: not written to "
                                     "any delivered file, this sidecar included"}
    out["kind"] = str(kind)
    if kind != "video":
        # A movie is one file and its own name says so; a sequence is N files and a reader needs to know which.
        if bit_depth:
            out["bitDepth"] = str(bit_depth)
        if frames:
            out["frames"] = int(frames)
        if first_file:
            out["firstFile"] = os.path.basename(first_file)
        if last_file and last_file != first_file:
            out["lastFile"] = os.path.basename(last_file)
    if timecode:
        out["startTimecode"] = str(timecode)
    if isinstance(source_meta, dict) and source_meta:
        # The plate's own attributes, kept SEPARATE from ours rather than merged: they describe the file that came
        # in, and after a colour transform some of them no longer describe the file going out.
        out["source"] = {k: _meta_scalar(v) for k, v in source_meta.items() if k != "attrs"}
        # Same guard on the PLATE's half. This is the block the leak was actually coming through for a plate read
        # from a ComfyUI PNG, whose text chunks are `prompt` and `workflow`.
        src_withheld = []
        out["source"]["attrs"] = {}
        # Read through a mapping check rather than `or {}`. `or {}` only rescues falsy attrs; a list or a string
        # passes it and then raises AttributeError on .items(), which is a second door onto the same crash the
        # caller closes. This function is reachable from more than one writer, so it does not lean on the caller.
        _src_a = source_meta.get("attrs")
        _src_a = _src_a if isinstance(_src_a, dict) else {}
        for k, v in _src_a.items():
            if _meta_is_private(k, v):
                src_withheld.append(str(k))
            else:
                out["source"]["attrs"][str(k)] = _meta_scalar(v)
        drop = _forbidden_meta_keys(_src_a)
        if drop:
            out["source"]["dropped_pixel_state_claims"] = drop
            for k in drop:
                out["source"]["attrs"].pop(k, None)
        if src_withheld:
            out["source"]["withheld_keys"] = sorted(set(src_withheld))
    return out


def _write_meta_sidecar(out_path, payload, strip_frame=False):
    """The FULL metadata set beside the written file as <out>.json, because every format keeps a handful of its
    attributes and drops the rest without a word. Never raises: a sidecar that could not be written must not lose
    the render it describes."""
    import json as _json
    p = _sidecar_path(out_path, strip_frame)
    try:
        with open(p, "w", encoding="utf-8") as fh:
            _json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
        return p
    except Exception:
        return None


def _plate_identity(source):
    """The shot's identity as {field: text} for OCIO Read's Metadata panel, or {} when the plate carries none.

    THE SAME READ THE WIRE USES. read_source_meta is what OCIO Read hands to OCIO Write, and _identity_meta is
    the reduction every writer applies to it - so the panel shows the values that will actually be delivered,
    not a second opinion derived some other way. Timecode is one of those seven fields, which is how it reaches
    the panel at all now that OCIO Write no longer offers anywhere to type one.

    Never raises: this backs a UI panel, and a plate whose header will not parse must show fewer rows rather
    than break the node.
    """
    try:
        meta = read_source_meta(source) or {}
        attrs = meta.get("attrs") or {}
        ident = dict(_identity_meta(attrs) or {})
        # SHOWN THE WAY A HUMAN READS A TIMECODE. _identity_meta hands back whatever spelling the header used,
        # and an EXR's is OpenEXR's TimeCode tuple stringified - "(14, 48, 24, 22, 0, 0, 0, 0, 0, 0)", which
        # is a data structure, not a code. Parsed through the same reader the writer uses, so the panel and the
        # delivered file can never show different codes. A code that will not parse is dropped rather than
        # printed raw: the panel exists to be read at a glance.
        if "timecode" in ident:
            tc = _timecode_from_source(attrs)
            if tc:
                ident["timecode"] = _timecode_string(*tc, False)
            else:
                ident.pop("timecode", None)
        return ident
    except Exception:
        return {}


def read_meta(source):
    """Read-only metadata panel data for the front-end (/ocio/meta): resolution, format, frame range + count,
    fps, the auto-detected input colorspace, whether the file carries an alpha channel, and the shot identity
    the plate itself holds (reel / scene / shot / take / camera / lens / timecode). Reuses _seq_range for the
    range/count/fps/kind (same detection the auto-fill uses) and adds the fields _seq_range does not need:
    resolution, container/codec, and alpha presence. Never raises - callers get {"error": ...} instead,
    matching /ocio/thumb's contract, since this backs a UI panel that must not 500 on a bad path."""
    source = (source or "").strip().rstrip("/")
    if not source:
        return {"error": "empty source"}
    s = source if os.path.isabs(source) else os.path.join(_input_dir(), source)
    ext = os.path.splitext(s)[1].lower()
    try:
        rng = _seq_range(source)   # inside the try too - it shells out to ffprobe for a video source
        if ext in VIDEO_EXTS:
            if not os.path.isfile(s):
                return {"error": f"not found: {s}"}
            pr = subprocess.run([_FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                 "stream=width,height,codec_name,pix_fmt,color_primaries,color_transfer,"
                                 "color_space,nb_frames,r_frame_rate,avg_frame_rate",
                                 "-of", "default=noprint_wrappers=1", s], capture_output=True, text=True)
            info = dict(line.split("=", 1) for line in pr.stdout.strip().splitlines() if "=" in line)
            w, h = int(info.get("width", 0) or 0), int(info.get("height", 0) or 0)
            if not (w and h):
                return {"error": f"ffprobe could not read {s}: {pr.stderr[:200]}"}
            pix_fmt = info.get("pix_fmt", "") or ""
            return {"kind": "video", "resolution": f"{w}x{h}", "format": ext.lstrip("."),
                    "codec": info.get("codec_name", "") or "", "pix_fmt": pix_fmt,
                    "start": rng.get("start", 0), "end": rng.get("end", 0), "count": rng.get("count", 0),
                    "fps": rng.get("fps", 0.0), "input_colorspace": _video_input_cs(info, ext),
                    "alpha": pix_fmt.endswith("a") or "argb" in pix_fmt or "rgba" in pix_fmt,
                    "color_primaries": info.get("color_primaries", "") or "",
                    "color_transfer": info.get("color_transfer", "") or "",
                    "identity": _plate_identity(source)}
        # still / sequence: resolve the same first frame _seq_range/load_source would pick
        files = _frame_files(s)
        if not files and os.path.isfile(s):
            sib = _sequence_siblings(s)
            if len(sib) > 1:
                files = sib
        if files:
            first = files[0]
        elif os.path.isfile(s):
            first = s
        else:
            return {"error": f"not found: {s}"}
        h, w, has_alpha = _still_shape_alpha(first)      # real channel count - _read_still always pads to RGBA
        kind = "sequence" if files else "still"
        return {"kind": kind, "resolution": f"{w}x{h}", "format": os.path.splitext(first)[1].lstrip(".").lower(),
                "start": rng.get("orig_start", rng.get("start", 0)) if kind == "sequence" else 0,
                "end": rng.get("orig_end", rng.get("end", 0)) if kind == "sequence" else 0,
                "count": rng.get("count", 1), "fps": rng.get("fps", 0.0),
                "input_colorspace": _auto_input_cs(first),
                "alpha": has_alpha,
                "missing": rng.get("missing", ""), "missing_count": rng.get("missing_count", 0),
                "identity": _plate_identity(source)}
    except Exception as e:
        return {"error": str(e)[:250]}


def _fit_long_side(rgb, max_side):
    """Downscale (never upscale) so the long side is at most max_side, cv2 INTER_AREA (correct for shrinking).
    Done BEFORE the OCIO convert - cheaper to color-convert a small image than a full-res one."""
    h, w = rgb.shape[:2]
    long_side = max(h, w)
    if long_side <= max_side or cv2 is None:
        return rgb
    scale = max_side / float(long_side)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    return cv2.resize(np.ascontiguousarray(rgb), (nw, nh), interpolation=cv2.INTER_AREA)


def thumb_frame(src, max_side=512, frame=None):
    """Resolve `src` exactly like OCIORead (absolute, or relative to the ComfyUI input dir; a folder or a
    numbered frame collapses to its sequence and picks frame 1; a video decodes ONLY its first frame via
    ffmpeg). Returns float32 RGB [H,W,3] (0..1 for stills/video; EXR/HDR keep scene-linear range), already
    downscaled to fit `max_side` on the long side. Colorspace conversion is the caller's job (via _convert) -
    this only loads + resizes, so the /ocio/thumb route stays thin and cv2 does the expensive work once.
    `frame` (a FRAME NUMBER, not an index) drives the sequence flipbook player (2026-07-03): when given and the
    source is a sequence, return THAT frame instead of the first; a missing/out-of-range number falls back to
    frame 1 so the player never 500s mid-scrub. Ignored for a lone still / video."""
    source = (src or "").rstrip("/")
    if not source:
        raise ValueError("empty source")
    s = source if os.path.isabs(source) else os.path.join(_input_dir(), source)
    ext = os.path.splitext(s)[1].lower()
    if ext in VIDEO_EXTS:
        if not os.path.isfile(s):
            raise FileNotFoundError(s)
        rgb = _read_video_frame(s)
        return _fit_long_side(rgb, max_side)
    files = _frame_files(s)                        # an explicit folder or #### pattern -> its first frame
    if not files and os.path.isfile(s):
        sib = _sequence_siblings(s)
        if len(sib) > 1:
            files = sib
    if files:
        first = files[0]
        if frame is not None:                          # flipbook: exact frame NUMBER, fall back to frame 1 if absent
            first = {_frame_num(f): f for f in files}.get(int(frame), first)
    elif os.path.isfile(s):
        first = s
    else:
        raise FileNotFoundError(s)
    rgba = _read_still(first)
    return _fit_long_side(np.ascontiguousarray(rgba[..., :3]), max_side)


# --------------------------------------------------------------------------- saving

# EXR compression choices (Nuke Write style) -> cv2.IMWRITE_EXR_COMPRESSION_* suffix.
_EXR_COMP = {"none": "NO", "rle": "RLE", "zips": "ZIPS", "zip": "ZIP",
             "piz": "PIZ", "pxr24": "PXR24", "dwaa": "DWAA", "dwab": "DWAB"}

# TIFF tags for the identity fields that HAVE a standard home (TIFF 6.0). The other four - scene, shot, take,
# lens - have no standard tag at all, so they travel as XMP below. Private tags (65000+) were tried first and
# rejected: tifffile writes them and oiiotool does not surface them, so they would be write-only.
_TIFF_IDENT_TAGS = {"reel": 269}                       # 269 DocumentName; 271 Make / 272 Model are set from 'camera'

# XMP namespace for the identity fields TIFF has no tag for. OUR OWN namespace, deliberately: the obvious first
# try was to hang them off Dublin Core (dc:scene, dc:shot, ...), which would mean inventing terms inside someone
# else's schema - the same class of error as claiming a gamut we cannot stand behind. This mirrors the EXR side,
# where our own notes live under com.ocio.*, and it is what _interop_id refuses to guess at for registered names.
# MEASURED: oiiotool --info reports these as ocio:scene / ocio:shot / ocio:take / ocio:lens / ocio:timecode, and
# XML-escaped values with '&' and '<' survive intact. NOTE a reader-side quirk worth knowing: oiiotool DISPLAYS a
# numeric-looking value as a number, so shot "0106" prints as 106. The bytes in the file are the exact string,
# and the sidecar carries it as a string too.
_XMP_NS = "urn:comfyui-ocio:1.0/"


def _tiff_meta_kwargs(attrs):
    """tifffile.imwrite kwargs carrying the shot identity into a TIFF. TIFF is a real DI and matte-paint format,
    and one that identifies itself only by filename is a support ticket waiting to happen.

    reel -> DocumentName (269), camera -> Make (271) + Model (272), plus Software; scene / shot / take / lens /
    timecode -> an XMP packet in tag 700, because TIFF has no standard tag for any of them. All of it confirmed
    readable by oiiotool, i.e. by the OIIO-based readers (Nuke, Katana, Houdini, Blender, Arnold).

    NO DateTime (306), although tifffile offers it and it would be the obvious fourth tag: a wall-clock stamp
    makes two otherwise identical renders differ byte for byte, and comparing deliveries by bytes is something
    this project actually does. Nothing needs it that the sidecar cannot carry."""
    ident = _identity_meta(attrs)
    kw = {"software": "ComfyUI-OCIO"}
    if not ident:
        return kw
    extra = []
    reel = ident.get("reel")
    if reel:
        extra.append((_TIFF_IDENT_TAGS["reel"], "s", 0, reel, False))
    # Make (271) and Model (272) are read from the keys that ACTUALLY MEAN make and model - never split out of one
    # string. The first version split the camera field on its first space, and a plate carrying
    # cameraModel="ALEXA 35" came out as Make="ALEXA", Model="35": a real camera turned into a made-up
    # manufacturer and a two-digit model. Found by reading the written tags. A model is a model whole; if no make
    # key is present, Make is simply absent, which is true rather than invented.
    make = _first_meta(attrs, ("cameraMake", "make", "Make"))
    model = _first_meta(attrs, ("cameraModel", "model", "Model", "camera", "dpx:InputDevice"))
    if make:
        extra.append((271, "s", 0, make, False))
    if model:
        extra.append((272, "s", 0, model, False))
    rest = {k: v for k, v in ident.items() if k in ("scene", "shot", "take", "lens", "timecode")}
    if rest:
        from xml.sax.saxutils import escape
        body = "".join(f"<ocio:{k}>{escape(str(v))}</ocio:{k}>" for k, v in sorted(rest.items()))
        packet = ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
                  '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                  '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                  f'<rdf:Description rdf:about="" xmlns:ocio="{_XMP_NS}">{body}</rdf:Description>'
                  '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>').encode("utf-8")
        extra.append((700, "B", len(packet), packet, False))
    if extra:
        kw["extratags"] = extra
    return kw


def _png_itxt_chunk(keyword, text):
    """One uncompressed iTXt chunk, built by hand. Layout per PNG Third Edition 11.3.4.5: keyword (Latin-1,
    1-79 bytes) NUL, compression flag 0, compression method 0, language tag NUL, translated keyword NUL,
    then the text as UTF-8. Chunk framing is length, type, data, CRC-32 over type+data."""
    kw = str(keyword).encode("latin-1", "replace")[:79]
    if not kw:
        raise ValueError("empty keyword")
    data = kw + b"\x00" + b"\x00" + b"\x00" + b"\x00" + b"\x00" + str(text).encode("utf-8")
    return (struct.pack(">I", len(data)) + b"iTXt" + data
            + struct.pack(">I", binascii.crc32(b"iTXt" + data) & 0xFFFFFFFF))


def _png_splice_text(path, colorspace=None, attrs=None):
    """Put the identity set into an ALREADY-WRITTEN 16-bit PNG, as iTXt chunks placed BEFORE the first IDAT.

    Why by hand: cv2 writes 16-bit RGB and no text; Pillow writes text and cannot represent 16-bit RGB at all.
    Neither can do both halves, so the chunks are spliced in afterwards. The pixels are untouched - measured
    bit-identical before and after on a frame with 0 and 65535 pinned in.

    THE POSITION IS THE WHOLE POINT, and it is where the first attempt failed. Chunks written before IEND (i.e.
    after IDAT) are legal PNG and INVISIBLE to OpenImageIO, because its reader takes the text out of
    png_read_info, before the pixels, and never revisits the end-info. Measured with a control: identical
    chunks before IDAT -> oiiotool lists every key; after IDAT -> oiiotool lists none. OIIO is what Nuke,
    Katana, Houdini and Blender read through, so after-IDAT would have been a chunk nobody sees.

    Keywords: the identity fields under their own names (OIIO puts any non-predefined keyword into the
    ImageSpec verbatim, and ExifTool extracts all of them), plus the two PREDEFINED ones that map to real
    attributes - Description becomes ImageDescription, and XML:com.adobe.xmp is the keyword PNG Third Edition
    defines for an XMP packet, recommending exactly this uncompressed-iTXt form.

    Still not a professional contract, and the sidecar remains authoritative: no Foundry or Blackmagic document
    promises that Nuke or Resolve reads arbitrary PNG text keys, so this is a duplicate hint for the tools that
    demonstrably do. Never raises - a frame is not lost over metadata."""
    try:
        ident = _identity_meta(attrs) if attrs else {}
        pairs = []
        if colorspace:
            pairs.append(("colorspace", colorspace))
            pairs.append(("Description", colorspace))          # predefined -> ImageDescription
        pairs += list(ident.items())
        rest = {k: v for k, v in ident.items() if k in ("scene", "shot", "take", "lens", "timecode", "reel")}
        if rest:
            from xml.sax.saxutils import escape
            body = "".join(f"<ocio:{k}>{escape(str(v))}</ocio:{k}>" for k, v in sorted(rest.items()))
            pairs.append(("XML:com.adobe.xmp",
                          '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
                          '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                          '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                          f'<rdf:Description rdf:about="" xmlns:ocio="{_XMP_NS}">{body}</rdf:Description>'
                          '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>'))
        if not pairs:
            return False
        blob = open(path, "rb").read()
        if blob[:8] != b"\x89PNG\r\n\x1a\n":
            return False
        idat = blob.find(b"IDAT")
        if idat < 4:                                            # no IDAT: not a PNG we understand, leave it be
            return False
        chunks = b""
        for k, v in pairs:
            try:
                chunks += _png_itxt_chunk(k, v)
            except Exception:
                continue                                        # one unwritable chunk must not cost the frame
        if not chunks:
            return False
        with open(path, "wb") as fh:
            fh.write(blob[:idat - 4] + chunks + blob[idat - 4:])
        return True
    except Exception:
        return False


def _container_keeps_range(container, still_format, bit_depth):
    """Does the chosen target store IEEE floats, and therefore carry below-black and above-white?

    ONE source of truth for a fact two places need, so the writer and the warning cannot drift apart. Read off
    `_save_still`'s own branches rather than assumed: EXR is float at BOTH depths (`_save_exr_with_meta` picks
    float32 for '32f' and float16 otherwise, so there is no integer EXR to worry about), and TIFF is float ONLY
    at '32f' - that branch writes `rgb.astype(np.float32)` with no clip at all, while TIFF 16 and TIFF 8 round
    into an unsigned integer and clip both tails. PNG and JPEG are unsigned at every depth. Video lands in a
    limited- or full-range YUV.

    Measured, both arms read back off disk: TIFF 32f returns -1.5 and +20.0 intact with all six test negatives
    distinct, TIFF 16 returns 0 for every one of them. So a float TIFF is a full-range container like EXR and
    must NOT be warned about - a warning on a container that kept the data is the false alarm that teaches
    people to ignore the true ones.
    """
    if container == "video":
        return False
    if still_format == "exr":
        return True
    return still_format in ("tif", "tiff") and bit_depth == "32f"


def _range_clip_note(written, target_label, keeps_range, maxv):
    """What an integer container is about to destroy, or None when nothing is at risk.

    RESPONSIBLE FOR: telling the artist that below-black and above-white did not reach the file (2026-08-13).
    Measured, all through this node with raw_data on so only the container was under test: EXR 16f and 32f
    round-trip -1.5 and +20.0 intact, while TIFF 16/8, PNG 16/8 and JPEG floor every negative to 0.000000 and
    cap everything at 1.0 - they are unsigned integer formats and cannot represent either tail at all. Video is
    the same story through a limited/full-range YUV. The node said NOTHING about any of it: no ui text, no log
    line, confirmed on TIFF 16 and PNG 16 where 100 % of the below-black samples died. This is the one place the
    loss is irreversible, because it has already been written to disk, so it is also the one place worth a word.

    The threshold is the TARGET'S OWN ROUNDING STEP rather than an arbitrary epsilon: a value that rounds to the
    same integer code as the endpoint has lost nothing, and warning about it would be the noise that teaches
    people to ignore the warning. For a video codec the step of the widest depth here is used, which over-warns
    slightly on 8- and 10-bit essence - the deliberate side to err on, the same call the overwrite dialog makes.

    `written` is the array that ACTUALLY went to the writer, not its first frame: a clip on frame 40 alone is
    still a clip, and quoting frame 1 is how a measurement understates itself.
    """
    if keeps_range:
        return None                                        # EXR carries both tails; there is nothing to report
    a = np.asarray(written, dtype=np.float32)
    if a.size == 0:
        return None
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return None
    eps = 0.5 / float(maxv)
    n_lo = int((finite < -eps).sum())
    n_hi = int((finite > 1.0 + eps).sum())
    if not n_lo and not n_hi:
        return None
    parts = []
    if n_lo:
        parts.append(f"{100.0 * n_lo / finite.size:.2f}% below black (down to {float(finite.min()):+.4f})")
    if n_hi:
        parts.append(f"{100.0 * n_hi / finite.size:.2f}% above white (up to {float(finite.max()):+.4f})")
    return f"{target_label} clipped " + " and ".join(parts) + " - EXR keeps both"


def _save_still(path, rgb, fmt, bit_depth, alpha=None, colorspace=None, compression="zip", attrs=None):
    """Write one frame. bit_depth per format: exr 16f/32f (half/float), tiff 8/16/32f, png 8/16, jpeg 8.
    alpha (H,W) -> RGBA (exr/tiff/png; ignored for jpeg). colorspace is stamped into the file metadata
    where the format allows it (png text, tiff description, jpeg comment).

    attrs: extra header metadata for EXR (camera / lens / timecode / editorial). None for a plain render."""
    fmt = fmt.lower()
    has_a = alpha is not None
    desc = colorspace or ""

    def with_a(x):                                   # (H,W,3) -> (H,W,4) if alpha present
        return np.dstack([x, np.clip(alpha, 0, 1) if x.dtype == np.float32 else alpha]) if has_a else x

    if fmt == "exr":
        # PREFERRED PATH: write via OpenEXR. cv2 refuses EXR unless OPENCV_IO_ENABLE_OPENEXR was set in the
        # environment BEFORE cv2 was imported, and the setdefault at the top of this module is too late when
        # another custom node imported cv2 first - which is exactly how a real LTX-2.5 run died here on
        # 2026-08-12 with "OpenEXR codec is disabled" after 125 s of generation. OpenEXR needs no env var, and
        # it is the only way to write header attributes at all (cv2 writes none). Falls back to cv2 when
        # OpenEXR is not installed, so an install without it behaves as before.
        #
        # Moving EXR writing off cv2 is Andrei Orehov's fix (PR #5): he found that every write here was a
        # silent no-op, diagnosed it to OpenCV's disabled-by-default codec, and added the check on the
        # fallback below. This path extends it to carry header attributes as well, which cv2 cannot do at all.
        try:
            return _save_exr_with_meta(path, rgb, bit_depth, alpha, compression, attrs)
        except ImportError:
            pass
        if cv2 is None:
            raise RuntimeError("Writing EXR needs either the OpenEXR module (preferred) or OpenCV (cv2).")
        try:
            t = cv2.IMWRITE_EXR_TYPE_FLOAT if bit_depth == "32f" else cv2.IMWRITE_EXR_TYPE_HALF
            params = [int(cv2.IMWRITE_EXR_TYPE), int(t)]
        except Exception:
            params = []
        try:                                             # EXR compression (Nuke-style choice); ZIP = lossless default
            comp = getattr(cv2, "IMWRITE_EXR_COMPRESSION_" + _EXR_COMP.get(compression, "ZIP"), None)
            if comp is not None:
                params += [int(cv2.IMWRITE_EXR_COMPRESSION), int(comp)]
        except Exception:
            pass
        bgr = rgb[..., ::-1].astype(np.float32)
        data = np.dstack([bgr, alpha.astype(np.float32)]) if has_a else bgr   # BGRA for cv2
        cv2.imwrite(path, np.ascontiguousarray(data), params)   # cv2 writes no header attributes at all
        # NEVER REPORT A WRITE THAT DID NOT LAND. Depending on the build, cv2 with the codec disabled either
        # raises or returns quietly without a file - and in the quiet case Write went on to report
        # "saved name.0001.exr" with the right frame count over an empty folder. That is the defect Andrei
        # Orehov found and fixed in PR #5, and this check is his; it is kept on the fallback because the
        # OpenEXR path above cannot fail this way. A return code is not proof of a result.
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise RuntimeError(
                f"EXR write produced no file at {os.path.basename(path)}. Install the OpenEXR module "
                f"(`pip install \"OpenEXR>=3.3\"`). Setting OPENCV_IO_ENABLE_OPENEXR=1 before ComfyUI starts "
                f"revives cv2 on OpenCV 4, where the codec is present but off by default; it does nothing on "
                f"OpenCV 5, whose wheels ship without the codec entirely.")
        return
    if fmt == "dpx":
        # DPX WRITING, WHICH THIS PACK COULD READ BUT NOT PRODUCE. That asymmetry mattered more than it looks:
        # DPX is how plates move between a film pipeline and everyone else, and Netflix's own Non-Graded
        # Archival Master specification names "16-bit DPX" first for log material, with 10-bit allowed only
        # when half the primary capture was 10-bit or lower. The pack could ingest that and never hand it back.
        #
        # Written through ffmpeg rather than by hand: SMPTE ST 268 has enough header variants (film vs
        # television offsets, packing modes, endianness) that a hand-rolled writer would be a second decoder to
        # maintain, and this pack already learned that lesson from the two copies of the extension rule.
        # 10-bit uses gbrp10le, which is the packed RGB layout a film pipeline expects; 16-bit uses rgb48le.
        _require_ffmpeg()
        pf = {"10": "gbrp10le", "16": "rgb48le"}.get(str(bit_depth))
        if pf is None:
            raise RuntimeError(
                f"DPX is an integer format and takes bit_depth 10 or 16, not {bit_depth!r}. Use 16 for an "
                f"archival master (Netflix NAM asks for 16-bit DPX on log material), 10 for a plate matching "
                f"a 10-bit camera original, or write EXR if you need float.")
        h, w = rgb.shape[:2]
        buf = (np.clip(rgb, 0, 1) * 65535.0).round().astype("<u2").tobytes()
        proc = subprocess.run(
            [_FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb48le", "-s", f"{w}x{h}",
             "-i", "-", "-frames:v", "1", "-c:v", "dpx", "-pix_fmt", pf, path],
            input=buf, capture_output=True)
        # NEVER REPORT A WRITE THAT DID NOT LAND - the same check Andrei Orehov's EXR fix put on the cv2 path,
        # for the same reason: a return code is not proof of a file.
        if proc.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
            raise RuntimeError(
                f"DPX write produced no file at {os.path.basename(path)}: "
                f"{proc.stderr.decode('utf-8', 'ignore')[:200]}")
        return
    if fmt in ("tif", "tiff"):
        if bit_depth == "32f":
            data = with_a(rgb.astype(np.float32))
        elif bit_depth == "8":                                              # 2026-07-03: round, not floor (kills the half-LSB bias)
            data = np.round(np.clip(with_a(rgb), 0, 1) * 255).astype(np.uint8)
        else:
            data = np.round(np.clip(with_a(rgb), 0, 1) * 65535).astype(np.uint16)
        kw = dict(description=desc, metadata=None)
        # metadata=None IS A FIX, NOT A STYLE CHOICE. tifffile's default `metadata={}` appends its own shaped
        # JSON to tag 270, and passing `description=` as well emitted tag 270 TWICE - measured: ImageDescription
        # 'ACEScg' followed by ImageDescription '{"shape": [32, 64, 3]}' in one IFD. TIFF 6.0 allows a tag once
        # per IFD, so readers disagreed about which value is the file's: oiiotool returned 'ACEScg', tifffile's
        # own tag mapping returned the JSON. The colorspace was therefore present but not dependable. With
        # metadata=None there is a single tag 270 holding the colorspace, confirmed by both readers.
        # tifffile also stamps Software='tifffile.py' unless told otherwise, which named the wrong writer.
        try:
            kw.update(_tiff_meta_kwargs(attrs))
        except Exception:
            pass                                    # metadata must never be the reason a frame fails to write
        tifffile.imwrite(path, np.ascontiguousarray(data), **kw)
        return
    if fmt == "png":
        if bit_depth == "16":
            if cv2 is None:
                raise RuntimeError("16-bit PNG needs OpenCV (cv2).")
            bgr = np.clip(rgb, 0, 1)[..., ::-1]
            data = np.dstack([bgr, np.clip(alpha, 0, 1)]) if has_a else bgr
            cv2.imwrite(path, np.ascontiguousarray(np.round(data * 65535).astype(np.uint16)))   # 2026-07-03: round, not floor
            # NEITHER LIBRARY CAN DO BOTH HALVES: cv2 writes 16-bit RGB but no text chunks, and Pillow cannot
            # write a 16-bit RGB PNG at all ("Cannot handle this data type: (1, 1, 3), <u2", measured on Pillow
            # 12.2.0 - its 16-bit support is single-channel I;16). That was recorded here as "16-bit PNG carries
            # no text, and not by choice"; it was wrong, and _png_splice_text closes it (2026-08-12).
            _png_splice_text(path, colorspace, attrs)
            return
        arr8 = np.round(np.clip(rgb, 0, 1) * 255).astype(np.uint8)
        if has_a:
            im = Image.fromarray(np.dstack([arr8, np.round(np.clip(alpha, 0, 1) * 255).astype(np.uint8)]), "RGBA")
        else:
            im = Image.fromarray(arr8, "RGB")
        info = None
        ident = _identity_meta(attrs) if attrs else {}
        if colorspace or ident:
            from PIL import PngImagePlugin
            info = PngImagePlugin.PngInfo()
            if colorspace:
                info.add_text("colorspace", colorspace)
            # iTXt, not tEXt: iTXt is the UTF-8 chunk (PNG 1.2 / ISO 15948), so a lens or reel name with an
            # accent or a dash survives intact - verified byte-exact round-trip on 'Cooke S4/i 32mm - T2.0 cafe'
            # with an en dash and an e-acute. tEXt is Latin-1 and would mangle both. PNG is TRACEABILITY ONLY
            # here, not a delivery format, which is why it gets the seven identity fields and not the full set.
            # Confirmed readable by a third party: oiiotool --info surfaces all seven as named attributes.
            for k, v in ident.items():
                try:
                    info.add_itxt(k, v)
                except Exception:
                    continue                        # one unwritable chunk must not cost the frame
        im.save(path, pnginfo=info)
        return
    # jpeg / jpg - 8-bit, no alpha; colorspace goes in the JPEG comment
    im = Image.fromarray(np.round(np.clip(rgb, 0, 1) * 255).astype(np.uint8), "RGB")   # 2026-07-03: round, not floor
    im.save(path, quality=95, **({"comment": colorspace.encode()} if colorspace else {}))


# sRGB transfer tag for ffmpeg's -color_trc: confirmed accepted by this build's libx264/libx265/prores_ks/dnxhd
# (probed 2026-07-01: `ffmpeg -f lavfi -i testsrc2... -color_trc iec61966-2-1 -f null -` exits 0, no
# unrecognized/invalid warning). Kept as a constant (not inlined) so a build that rejects it only needs this
# line changed to "bt709".
_SRGB_TRC = "iec61966-2-1"


def _fps_arg(fps):
    """ffmpeg's `-r` value as a string, with the NTSC family written as its exact rational.

    `str(23.976)` makes ffmpeg parse the decimal literally and land on 2997/125, which is NOT 24000/1001. MOV and
    MP4 accept the odd rational and carry it (measured: r_frame_rate=2997/125 in the written file), but the MXF
    muxer is strict and refuses outright: `Unsupported frame rate 2997/125`. That took out both MXF codecs at
    23.976 and 29.97 - the pack's own _SEQ_FPS_DEFAULT and two of its own _DROP_FRAME_RATES - while the same
    call with `-r 24000/1001` exits 0. Verified against raw ffmpeg outside this pack, so it is the argument
    form, not our encoder options.

    The test is a round trip rather than a lookup table, so 47.952 and 119.88 are covered without listing them:
    take the nearest integer N to fps*1001/1000 and accept N*1000/1001 only if it lands within a RELATIVE 1e-5
    of fps. The tolerance is relative because the absolute gap grows with the rate - a 3-decimal spelling sits
    2.4e-5 away at 23.976 but 1.2e-4 away at 119.88, so an absolute 1e-4 window silently dropped the high frame
    rates while appearing to cover them. Integer and non-NTSC rates (24, 25, 30, 48, 50, 60) fall through
    unchanged: 24 is a relative 1e-3 from 24000/1001, a hundred times outside the window, so a true 24 can never
    be rewritten as 23.976."""
    try:
        f = float(fps)
    except (TypeError, ValueError):
        return str(fps)
    if f > 0:
        n = int(round(f * 1001.0 / 1000.0))
        exact = (n * 1000.0) / 1001.0
        if n > 0 and abs(f - exact) < exact * 1e-5:
            return f"{n * 1000}/1001"
    return str(fps)


def _video_color_tags(output_colorspace):
    """Map the (already-converted-to) output colorspace to ffmpeg NCLC color tags, so the written file is
    tagged and does not gamma-shift across players (untagged files currently show color_primaries/transfer =
    unknown and players guess). -movflags +write_colr writes the QuickTime colr atom on .mov; confirmed
    harmless on .mp4 too (probed: libx264 -movflags +write_colr on mp4 exits 0, writes nclx/nclc instead).

    Flaw found while probing this build (ffmpeg 2024-10-02 gyan.dev full_build): the generic -color_primaries /
    -color_trc / -colorspace OUTPUT options are silently no-ops for libx264/libx265/prores_ks/dnxhd here (only
    -colorspace's matrix half lands; primaries/transfer stay "unspecified" in the written colr/nclx atom, and
    ffmpeg logs no warning). A -vf setparams=... filter (tagging the frames before they hit the encoder) is
    what actually lands all three tags for every codec tested - confirmed by a real encode+ffprobe per codec.
    So this returns BOTH the (still-needed, still-correct) trailing output options AND the setparams -vf; the
    caller must place the -vf before the output path same as any other output option."""
    # raw_data hands us None, and None means "these pixels were not converted to any delivery space". There is
    # nothing true to say about them, so we say nothing: no primaries, no transfer, no matrix, no colr atom.
    # The still path already works this way (EXR and TIFF under raw_data write neither chromaticities nor
    # colorInteropID), and video was the odd one out - it fell through to the sRGB default below and stamped
    # bt709 / iec61966-2-1 with full confidence onto pixels that may be ACEScg, log, or a data pass. An untagged
    # file leaves a player guessing, which is honest; a confidently mistagged one makes it guess wrong and
    # believe it is right. Empty string is NOT treated this way: that is a caller passing a colorspace it could
    # not name, which still lands on the documented sRGB default.
    if output_colorspace is None:
        return []
    cs = output_colorspace.lower()
    # HLG IS TESTED BEFORE PQ, and that order is the whole fix. The old predicate was `"2100" in cs or "pq" in cs`
    # -> PQ, and the config's HLG space is literally named "Rec.2100-HLG - Display", so it matched on "2100" and
    # every HLG master was written claiming smpte2084 (measured 2026-08-12: trc=smpte2084 on an HLG pick). A player
    # trusting that tag applies the PQ EOTF to an HLG signal - not a subtle shift, a broken image. HLG's transfer
    # characteristic is arib-std-b67 (ARIB STD-B67, ITU-R BT.2100 HLG); confirmed accepted by this ffmpeg build on
    # a real encode + ffprobe read-back.
    if "hlg" in cs or "b67" in cs:
        prim, trc, spc = "bt2020", "arib-std-b67", "bt2020nc"        # BT.2100 HLG
    elif "2100" in cs or "2084" in cs or "pq" in cs:
        # PQ now also catches "ST2084-P3-D65 - Display", which used to fall through to the sRGB default and ship a
        # PQ HDR master tagged as an SDR computer display (measured: trc=iec61966-2-1).
        prim, trc, spc = ("smpte432", "smpte2084", "bt709") if "p3" in cs else ("bt2020", "smpte2084", "bt2020nc")
    elif "1886" in cs or "rec.709" in cs or "rec709" in cs:
        prim, trc, spc = "bt709", "bt709", "bt709"                  # broadcast 2.4
    elif "p3" in cs:
        # Display P3 / P3-D65 used to be tagged bt709 primaries, describing a narrower gamut than the pixels
        # occupy. smpte432 is SMPTE ST 432-1, the P3-D65 primary set (ffmpeg's own help text misprints it as
        # "SMPTE 422-1"; the enum is AVCOL_PRI_SMPTE432). Transfer stays the sRGB curve, which is right for
        # Display P3 and is the least-wrong available code for the gamma-2.6 P3-D65 display space: the NCLC
        # transfer enum has gamma22 and gamma28 but NO 2.6, so there is nothing to point at. Same for the matrix -
        # NCLC has no P3 coefficient set, so bt709 stays.
        prim, trc, spc = "smpte432", _SRGB_TRC, "bt709"
    else:                                                           # sRGB - Display default (WYSIWYG)
        prim, trc, spc = "bt709", _SRGB_TRC, "bt709"
    # -color_range tv IS KEPT, and the reason is measured rather than assumed (2026-08-12). ffprobe reports
    # color_range=unknown on our ProRes .mov files, which reads like a flag that does nothing - so it was put to a
    # control experiment: same frames, same flags, one axis changed at a time.
    #
    #   prores_ks -> .mov   unknown        prores_ks -> .mkv   tv
    #   dnxhd     -> .mov   tv             dnxhd     -> .mxf   tv
    #   libx264   -> .mov   tv             libx264   -> .mp4   tv
    #   libx265   -> .mov   tv             prores_ks -> .mp4   ffmpeg refuses prores in mp4 outright
    #
    # So it is NEITHER the codec alone NOR the MOV container alone: it is prores_ks IN a MOV. The same prores
    # stream reports tv in Matroska, whose Colour element has a dedicated Range field, so the encoder is applying
    # the range; and h264 / hevc / dnxhd all keep it in the very same MOV. All four .mov files carry an identical
    # `colr` box of type 'nclc' (pri 1 trc 1 matrix 1, dumped with -v trace), and the QuickTime NCLC variant has
    # three fields and NO full-range flag - so for the codecs that keep it, the range is being carried in the
    # bitstream (H.264/HEVC VUI, DNxHD frame header), and prores_ks signals no range of its own to fall back on.
    #
    # Hence: NOT removed. Dropping it would silently untag dnxhr_hq, h264 and hevc - three of the six codecs this
    # node offers, plus every .mp4 - to tidy up one combination that has nowhere to put it. The honest statement
    # is "ProRes in a MOV cannot carry it", not "the flag does nothing".
    vf = f"setparams=color_primaries={prim}:color_trc={trc}:colorspace={spc}:range=tv"
    return ["-vf", vf, "-color_primaries", prim, "-color_trc", trc, "-colorspace", spc,
            "-color_range", "tv", "-movflags", "+write_colr"]


# --------------------------------------------------------------------------- audio (OCIO Write's AUDIO input)
# RESPONSIBLE FOR: carrying a synchronized audio track through OCIO Write, so our Write can stand in for core
# SaveVideo in an audio-video graph (LTX-2.5 emits picture and sound from two VAEs). Added 2026-08-12.

def _audio_pcm(audio, fps, n_frames, start_index=0):
    """ComfyUI AUDIO -> (interleaved float32 samples [T, C], sample_rate, channels), cut to EXACTLY the frames
    being written so sound cannot drift from picture. None when no audio is wired.

    AUDIO is {"waveform": tensor [B, C, T], "sample_rate": int} - confirmed against core
    comfy_extras/nodes_audio.py, where LoadAudio unsqueezes a [C, T] decode into [B, C, T]. The batch axis is a
    stack of takes; take the first, the same way core's own savers do.

    The segment is [start_index, start_index + n_frames) in PICTURE time, padded with silence when the track
    runs short. Padding rather than ffmpeg's -shortest is deliberate: -shortest truncates whichever stream ends
    first, so a track one sample short would silently drop a VIDEO frame.

    A malformed AUDIO raises instead of returning None. Swallowing it would put us back where this input
    started - a graph that looks wired for sound and quietly writes a silent file."""
    if audio is None:
        return None
    if not isinstance(audio, dict) or "waveform" not in audio or "sample_rate" not in audio:
        raise ValueError("OCIO Write: the 'audio' input is not a ComfyUI AUDIO (expected a dict with 'waveform' "
                         f"and 'sample_rate', got {type(audio).__name__}). Wire an AUDIO output, e.g. LTXV Audio "
                         "VAE Decode or Load Audio.")
    sr = int(audio["sample_rate"] or 0)
    if sr <= 0:
        raise ValueError(f"OCIO Write: audio sample_rate is {sr}; expected a positive rate.")
    wf = audio["waveform"]
    a = wf.detach().cpu().numpy() if hasattr(wf, "detach") else np.asarray(wf)
    a = np.asarray(a, np.float32)
    while a.ndim > 2:                       # [B, C, T] (or deeper) -> [C, T], first take
        a = a[0]
    if a.ndim == 1:                         # [T] -> mono [1, T]
        a = a[None]
    ch = int(a.shape[0])
    if ch < 1 or a.shape[1] < 1:
        raise ValueError(f"OCIO Write: audio waveform is empty (shape {tuple(a.shape)}).")
    r = float(fps) if fps and float(fps) > 0 else 24.0
    s0 = max(0, int(round(start_index / r * sr)))
    want = max(1, int(round(n_frames / r * sr)))
    seg = a[:, s0:s0 + want]
    if seg.shape[1] < want:
        seg = np.concatenate([seg, np.zeros((ch, want - seg.shape[1]), np.float32)], 1)
    return np.ascontiguousarray(seg.T.astype(np.float32)), sr, ch      # [T, C] interleaved, ffmpeg f32le order


def _save_wav24(path, samples, sr):
    """Write a 24-bit PCM WAV. Used for the sidecar track beside an image sequence: EXR / TIFF / PNG hold no
    audio, and dropping a wired track without a word is the silent-failure this input exists to remove.

    24-bit PCM is the post-house delivery standard, and the stdlib wave module covers it, so a SEQUENCE write
    still needs no ffmpeg (only the video container does)."""
    import wave
    a = np.clip(np.asarray(samples, np.float32), -1.0, 1.0)
    ch = 1 if a.ndim == 1 else int(a.shape[1])
    i32 = np.round(a.reshape(-1) * 8388607.0).astype("<i4")
    # little-endian two's complement: bytes 0..2 ARE the 24-bit sample; byte 3 is only sign extension.
    raw = i32.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    with wave.open(path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(3)
        w.setframerate(int(sr))
        w.writeframes(raw)
    return path


# MXF: which ffmpeg muxer each codec choice means. MEASURED BEFORE BEING OFFERED (2026-08-12), the same way MOV
# was - one real DNxHR HQ encode per pattern, read back with ffprobe, because MXF has no udta box and what
# survives could not be guessed:
#
#   -f mxf (OP1a)      rc=0, 406 573 bytes for 6 frames at 512x288. Colour tags ALL survive - color_range=tv,
#                      primaries=bt709, trc=bt709, space=bt709 (better than ProRes in a MOV, which loses range).
#                      Timecode survives, in both the format tags and the stream tags. Audio survives as a
#                      second pcm_s24le stream (481 325 bytes with a 0.25 s track).
#   -f mxf_opatom      rc=0, 401 465 bytes, video only. Colour tags and timecode survive identically.
#                      REFUSES a second stream: "there must be exactly one stream for mxf opatom".
#
# Identity tags: of eleven passed as plain -metadata, ONE survives - reel_name, and that one is structural and
# worth relying on. ffmpeg writes it as the Name of the underlying Physical Source Package (local tag 0x4402),
# which is the field Avid means by Tape Name, and its own demuxer documents the field as the reel/tape name.
#
# THE OTHER TEN travel with the `comment_` prefix, and the earlier claim here - that they survive "as ST 377-1
# user comments" - WAS WRONG; corrected 2026-08-12 after checking ffmpeg's source rather than only ffprobe.
# What ffmpeg actually writes is an AAF-compatible TaggedValue referenced from the package's local tag 0x4406,
# not an ST 377-1 Comment Marker and not a DM Framework (those are timeline descriptive-metadata objects,
# ST 377-1 Annex B.30-B.32). And ffmpeg's demuxer reads that same private construction back as comment_<key>,
# so ffprobe round-tripping them is NOT independent confirmation of anything.
#
# NEITHER Avid nor Blackmagic documents Media Composer or Resolve surfacing arbitrary TaggedValue. Avid's
# published guides speak of embedded Tape Name, timecode and film metadata; Resolve's manual speaks of Reel
# Name. So the honest position is: reel_name is a real interchange field, the other ten are best-effort and
# THE SIDECAR .json IS THEIR RELIABLE CARRIER. The documented route into Avid for the rest is an ALE, and into
# Resolve its own metadata import - neither is written here yet.
#
# Out of scope and NOT claimed: ST 377-1 descriptive metadata schemes and IMF ST 2067 packages.
_MXF_MUXER = {"dnxhr_hq_mxf": "mxf", "dnxhr_hq_mxf_opatom": "mxf_opatom",
              "prores_4444_mxf": "mxf", "prores_4444xq_mxf": "mxf",
              "dnxhr_hqx_mxf": "mxf", "dnxhr_444_mxf": "mxf"}


def video_ext(video_codec):
    """The container extension for a codec choice. THE one place this is decided.

    It lived inline in write_paths() and was mirrored by a name-prefix test in web/ocio_io.js, which is how
    dnxhr_hq_mxf came to preview .mov on the node while this side wrote .mxf: 'dnxhr_hq_mxf' also startswith
    'dnxhr'. Two copies of a rule drift the moment a codec is added, so there is now one function and
    tools/test_codec_ext_parity.py reads both this and the JS table and fails if they disagree.

    MXF IS TESTED FIRST and that order is load-bearing, for the same startswith reason."""
    c = str(video_codec)
    if c in _MXF_MUXER:
        return ".mxf"
    # FFV1's preservation pairing is with Matroska, and that is the combination the Library of Congress lists,
    # not FFV1 in a MOV. Tested before the prores/dnxhr prefix for the same reason MXF is: a prefix test decides
    # by spelling rather than by fact, which is the bug this function was written to end.
    if c == "ffv1":
        return ".mkv"
    return ".mov" if c.startswith(("prores", "dnxhr")) else ".mp4"


_HDR_8BIT_PROFILES = {
    # Profiles that are 8-bit by definition, so no pixel format can rescue them. Named with the way out.
    "dnxhr_hq": "dnxhr_hqx (10-bit) or prores_4444 (12-bit)",
    "dnxhr_hq_mxf": "dnxhr_hqx_mxf (10-bit) or prores_4444_mxf (12-bit)",
    "dnxhr_hq_mxf_opatom": "dnxhr_hqx_mxf (10-bit) or prores_4444_mxf (12-bit)",
}


def _is_hdr_colorspace(cs):
    """True when this delivery space is an ITU-R BT.2100 HDR one (HLG or PQ).

    ONE predicate, read by both the tag writer and the encoder chooser. They answer the same question - is this
    file claiming HDR - and if each carried its own spelling they would drift apart, which is how a pack ends up
    tagging a file one way and encoding it another. The terms match _video_color_tags' own branches."""
    c = (cs or "").lower()
    return ("hlg" in c or "b67" in c or "2100" in c or "2084" in c or "pq" in c)


def _video_encoder_args(codec, hdr=False):
    """The ffmpeg encoder arguments for a codec choice. THE one place these live.

    Module level rather than a local dict inside save_video so a test can measure what this pack really writes
    instead of restating the mapping - a test that spells out its own "-profile:v dnxhr_hqx" is testing its own
    copy, which is exactly how the front end came to disagree about extensions.

    Unknown codec -> h264, matching save_video's original .get() default."""
    # PRORES IS ASKED FOR 10-BIT, NOT 12, AND THE REASON IS THE ENCODER RATHER THAN THE FORMAT. ProRes 4444 is a
    # 12-bit format on paper and every reader will tell you so: ffprobe reports pix_fmt=yuv444p12le and
    # bits_per_raw_sample=12 on our files, and on a real Resolve-written camera master too. But ffmpeg's ProRes
    # encoders cannot produce it. All three of them - prores, prores_aw, prores_ks - advertise exactly
    # "yuv422p10le yuv444p10le yuva444p10le", with no 12-bit entry anywhere, and asking for yuv444p12le makes
    # ffmpeg print "Incompatible pixel format 'yuv444p12le' for codec 'prores_ks', auto-selecting format
    # 'yuv444p10le'" and encode 10-bit regardless. So the old request was a request ffmpeg silently declined; the
    # file was always 10-bit data wearing a 12-bit label. Asking for what happens keeps the code honest
    # and changes not one byte of output. For genuinely 12-bit samples the only route in this build is libx265
    # (yuv444p12le), which is why hevc_444_12 exists below.
    return {
        "prores_4444": ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuv444p10le"],
        "prores_422hq": ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"],
        "prores_422": ["-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le"],
        "dnxhr_hq": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"],
        # HQX and 444 added 2026-08-12, because offering Avid ONLY at 8 bit contradicts the point of this pack.
        # BIT DEPTHS MEASURED HERE, not read off a table - one encode per profile, read back with ffprobe
        # (bits_per_raw_sample), every pixel format the encoder advertises tried:
        #   dnxhr_lb / sq / hq  yuv422p       8-bit 4:2:2
        #   dnxhr_hqx           yuv422p10le  10-bit 4:2:2
        #   dnxhr_444           yuv444p10le  10-bit 4:4:4   (also accepts gbrp10le, same depth)
        # THIS ENCODER TOPS OUT AT 10 BITS for every profile: its advertised pixel formats are exactly
        # "yuv422p yuv422p10le yuv444p10le gbrp10le", with no 12-bit entry, and libavcodec/dnxhdenc.c binds
        # each profile to those. So 444 here buys full chroma, not more bits, and for 12 bit the route is
        # ProRes 4444 (yuv444p12le, measured).
        # WHAT IS DELIBERATELY NOT CLAIMED: what "DNxHR HQX" or "444" mean as Avid formats. Avid's own sources
        # disagree with the widely-repeated table - its historical High Resolution Workflows Guide calls BOTH
        # HQX and 444 12-bit, while its current naming page (April 2026) says that after the 2025 revision of
        # ST 2019-1 the DNxHD / DNxHR / DNxGX families are unified as "Avid DNx" and every level admits 8 to
        # 16 bits with extended sampling. The 8/10/12 split is a property of particular implementations, this
        # encoder's included, not of the format family. These figures describe the files WE write.
        "dnxhr_hqx": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hqx", "-pix_fmt", "yuv422p10le"],
        "dnxhr_444": ["-c:v", "dnxhd", "-profile:v", "dnxhr_444", "-pix_fmt", "yuv444p10le"],
        # MXF is the SAME DNxHR essence as above with a different muxer - not a new format to support. Both
        # patterns measured end to end (see _MXF_MUXER).
        "dnxhr_hq_mxf": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"],
        "dnxhr_hq_mxf_opatom": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"],
        # MXF AT MORE THAN 8 BITS. Until now the only way into an MXF here was dnxhr_hq, which is 8-bit by
        # profile, so the one container the industry uses to hand masters around was the one place this pack
        # could not carry a master. A real camera MXF measured for this: ProRes 4444, yuv444p12le,
        # bits_per_raw_sample=12, written by Resolve, OP1a. All four below were written and read back with
        # ffprobe before being listed.
        "prores_4444_mxf": ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuv444p10le"],
        "prores_4444xq_mxf": ["-c:v", "prores_ks", "-profile:v", "5", "-pix_fmt", "yuv444p10le"],
        # ProRes 4444 XQ in a MOV, which is its home container. It existed only as an MXF entry, which is
        # backwards: XQ is Apple's highest-bitrate ProRes (~500 Mb/s against ~330 for 4444) and the one their
        # own white paper describes as built for HDR and for grades that stretch the tails.
        "prores_4444xq": ["-c:v", "prores_ks", "-profile:v", "5", "-pix_fmt", "yuv444p10le"],
        # THE ONLY ENCODER HERE THAT IS LOSSLESS FOR THIS PACK'S INPUT. save_video hands ffmpeg 16-bit
        # RGB (rgb48le), and FFV1 at gbrp16le is the one option that gives it back unchanged: md5 of the
        # decoded stream equals md5 of what went in. Everything else differs, including a "lossless" x265,
        # because gbrp12le has already dropped four bits before the lossless part starts; ProRes and DNxHR
        # differ for that reason plus the trip through YCbCr. RFC 9043 describes the format, and FFV1 in
        # Matroska has been a Library of Congress Preferred Format for preservation since December 2023.
        # The cost is size: 700939 bytes against 113879 for ProRes 4444 on the same random-noise clip, so this
        # is an archival master rather than a review copy.
        "ffv1": ["-c:v", "ffv1", "-level", "3", "-g", "1", "-pix_fmt", "gbrp16le"],
        "dnxhr_hqx_mxf": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hqx", "-pix_fmt", "yuv422p10le"],
        "dnxhr_444_mxf": ["-c:v", "dnxhd", "-profile:v", "dnxhr_444", "-pix_fmt", "yuv444p10le"],
        # 10-BIT IS THE HDR FLOOR, NOT A PREFERENCE. ITU-R BT.2100 defines HLG and PQ at 10 or 12 bits per
        # sample and at nothing less. Writing bt2020 + arib-std-b67 tags onto an 8-bit stream produces a file
        # that states an HDR standard it cannot hold, which is worse than an untagged one: a player believes
        # it. Measured before this existed: hevc and h264 both came out yuv420p while carrying honest HLG
        # tags. The SDR entries stay 8-bit, because High/Main 8-bit is the compatible thing to hand someone
        # for review and there is nothing to misstate.
        "h264": ["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p10le" if hdr else "yuv420p"],
        "hevc": ["-c:v", "libx265", "-crf", "18", "-pix_fmt", "yuv420p10le" if hdr else "yuv420p"],
        # THE ONLY GENUINELY 12-BIT PATH IN THIS BUILD. Not a review format: 4:4:4 with no chroma subsampling
        # and 12 bits per sample, which is what BT.2100 recommends for mastering and what no ProRes or DNxHR
        # encoder here can reach. Measured, not assumed: libx265 advertises yuv444p12le and writes it, and
        # ffprobe reads the result back as 12-bit Rext 4:4:4.
        "hevc_444_12": ["-c:v", "libx265", "-crf", "12", "-pix_fmt", "yuv444p12le",
                        "-x265-params", "profile=main444-12"],
    }.get(codec, ["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p10le" if hdr else "yuv420p"])


def save_video(arr01, out_path, codec, fps, output_colorspace=None, audio_pcm=None,
               meta_attrs=None, timecode=None, source_meta=None):
    """Encode the batch. meta_attrs / timecode / source_meta drive the metadata written alongside it.

    Returns the sidecar .json path when one was written, else None. The sidecar is not optional politeness: a
    container keeps only what _video_tag_args passes it (a MOV keeps nearly everything, an MP4 a whitelist), so
    the full set has to live beside the movie or it does not survive at all."""
    _require_ffmpeg()
    n, h, w, _ = arr01.shape
    # A FILE MUST NOT STATE A STANDARD IT CANNOT HOLD. When the delivery space is BT.2100 (HLG or PQ) the
    # tags below say bt2020 + arib-std-b67 / smpte2084, and BT.2100 defines those at 10 or 12 bits per sample.
    # h264 and hevc move up to 10-bit here; the DNxHR profiles that are 8-bit by definition cannot, so this
    # refuses in words and names the codec that can, rather than writing an HDR file that is not one.
    hdr = _is_hdr_colorspace(output_colorspace)
    if hdr and codec in _HDR_8BIT_PROFILES:
        raise RuntimeError(
            f"{codec} is 8-bit by profile and cannot carry {output_colorspace}: ITU-R BT.2100 defines HLG and "
            f"PQ at 10 or 12 bits per sample. Use {_HDR_8BIT_PROFILES[codec]}, or pick an SDR output "
            f"colorspace such as 'Rec.1886 Rec.709 - Display'.")
    enc = _video_encoder_args(codec, hdr=hdr)
    muxer = _MXF_MUXER.get(codec)
    cmd = [_FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb48le",
           "-s", f"{w}x{h}", "-r", _fps_arg(fps), "-i", "-"]
    a_opts, a_path = [], None
    if audio_pcm is not None and muxer == "mxf_opatom":
        # OPAtom IS ONE ESSENCE PER FILE, by design (SMPTE ST 390): picture and sound are separate atoms that an
        # NLE relinks. ffmpeg says so outright - "there must be exactly one stream for mxf opatom" - and the mux
        # FAILS rather than dropping the track, measured. So the track is not offered to it here; OCIOWrite writes
        # it beside the file as a .wav and says so, the same way an image sequence does.
        audio_pcm = None
    if audio_pcm is not None:
        samples, sr, ch = audio_pcm
        fd, a_path = tempfile.mkstemp(suffix=".f32", prefix="ocio_audio_")
        with os.fdopen(fd, "wb") as fh:
            samples.tofile(fh)                      # raw interleaved float32; no header, no precision loss
        cmd += ["-f", "f32le", "-ar", str(sr), "-ac", str(ch), "-i", a_path]
        # .mov (ProRes / DNxHR) and .mxf (OP1a, AES3) take 24-bit PCM, what a post house expects; .mp4 takes AAC.
        a_opts = (["-c:a", "pcm_s24le"] if out_path.lower().endswith((".mov", ".mxf"))
                  else ["-c:a", "aac", "-b:a", "320k"])
        a_opts += ["-map", "0:v:0", "-map", "1:a:0"]
    # ffmpeg's own -timecode option, which writes a real tmcd timecode TRACK a post tool will conform from.
    # Correction to an earlier note in this project: on this build (2024-10-02 gyan.dev) `-metadata timecode=...`
    # is NOT dropped - re-measured on both .mov and .mp4, the muxer promotes that tag to an identical tmcd track.
    # -timecode is still the route used here because it is the documented one and because 'timecode' is deliberately
    # absent from the per-container tag whitelist below, so it would never survive the -metadata path anyway.
    tc_opts = ["-timecode", str(timecode)] if timecode else []
    # -f is REQUIRED for MXF and not merely tidy: both patterns share the .mxf extension, so ffmpeg's
    # extension-based muxer guess would silently pick OP1a for an OPAtom request.
    mux_opts = ["-f", muxer] if muxer else []
    try:
        cmd += [*enc, *_video_color_tags(output_colorspace), *_video_tag_args(out_path, meta_attrs), *tc_opts,
                *a_opts, *mux_opts, "-r", _fps_arg(fps), out_path]
        proc = subprocess.run(cmd, input=(np.clip(arr01, 0, 1) * 65535).astype("<u2").tobytes(), capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg encode failed: {proc.stderr.decode('utf-8', 'ignore')[:300]}")
    finally:
        if a_path:
            try:
                os.remove(a_path)
            except Exception:
                pass
    if meta_attrs or timecode or source_meta:
        return _write_meta_sidecar(out_path, _sidecar_payload(out_path, meta_attrs, timecode, source_meta, fps, codec))
    return None


# --------------------------------------------------------------------------- color

def _retime(image, src_fps, dst_fps):
    """Resample a frame batch from src_fps to dst_fps by nearest-frame index (dup/drop) - a real retime.
    Returns the batch unchanged if either rate is unknown or equal. image is a torch tensor [N,H,W,C]."""
    n = int(image.shape[0])
    if n <= 1 or src_fps <= 0 or dst_fps <= 0 or abs(src_fps - dst_fps) < 1e-6:
        return image
    m = max(1, int(round(n * dst_fps / src_fps)))
    idx = np.minimum((np.arange(m) * src_fps / dst_fps).astype(np.int64), n - 1)
    return image[torch.from_numpy(idx).to(image.device)]


def _convert(image, in_cs, out_cs):
    """OCIO convert between two colorspaces using the active (built-in ACES) config. Identity if equal.
    Uses the same cached CPU processor as the color nodes: getProcessor returns a Processor, which has no
    .apply - _apply_processor needs the CPUProcessor from getDefaultCPUProcessor (done inside
    _cached_cpu_processor). This is the OCIORead / OCIOWrite conversion path AND the /ocio/thumb preview,
    so both re-render correctly (and cheaply, LRU-cached) on a colorspace change."""
    if not in_cs or not out_cs or in_cs == out_cs:
        return image
    _require_ocio()
    cfg, cfg_key = _resolve_config_keyed("")
    if cfg is None:
        return image
    tf_key = ("colorspace", in_cs, out_cs)
    cpu = _cached_cpu_processor(cfg_key, tf_key, lambda: cfg.getProcessor(in_cs, out_cs))
    return _apply_processor(image, cpu)


def _acescct_to_lin(x):
    """ACEScct code value [0,1]-ish -> scene-linear (ACES spec, per channel). Used as a LOG SHAPER so an [0,1]
    3D LUT can carry a scene-linear (HDR) signal: the LUT's sampling axis is ACEScct-coded, decoded to linear
    here before the colorspace transform is baked in. The shader applies the matching lin->ACEScct encode."""
    x = np.asarray(x, np.float32)
    thr = 0.155251141552511                                  # ACEScct breakpoint (code value)
    lin_low = (x - 0.0729055341958355) / 10.5402377416545
    lin_hi = np.power(2.0, x * 17.52 - 9.72).astype(np.float32)
    return np.where(x <= thr, lin_low, lin_hi).astype(np.float32)


def _is_scene_linear(in_cs):
    """Does this colorspace hold SCENE-LINEAR (HDR) values, so a [0,1] display LUT needs a log shaper first?
    Ask OCIO's own encoding metadata (the studio config tags ACEScg / ACES2065-1 / Linear Rec.709 as
    'scene-linear', displays as 'sdr-video', ACEScct as 'log') - robust, unlike guessing from the name. Falls
    back to a name check only if the config exposes no encoding."""
    if not in_cs:
        return False
    try:
        _require_ocio()
        cfg, _ = _resolve_config_keyed("")
        if cfg is not None:
            cs = cfg.getColorSpace(in_cs)
            if cs is not None:
                enc = (cs.getEncoding() or "").lower()
                if enc:
                    return enc == "scene-linear"
    except Exception:
        pass
    low = in_cs.lower()                                      # fallback: only if encoding metadata is missing
    return ("scene-linear" in low) or ("aces2065" in low) or ("acescg" in low) or low.startswith("lin") or ("linear" in low)


def _lut_rgba8(in_cs, out_cs, size=33, raw=False, allow_shaper=False):
    """Bake the in_cs -> out_cs transform into an N x N x N RGBA8 3D LUT for the WebGL viewports. The browser
    plays raw pixels and the shader samples this LUT, so a moving image reacts to a colorspace change (the
    browser cannot apply OCIO itself). Output is clamped to [0,1] so the texture is 8-bit and always
    linear-filterable (no float-texture extension needed). Data is laid out for WebGL texImage3D: R (x) varies
    fastest, then G (y), then B (z). raw, in==out, or OCIO-unavailable returns the identity ramp.

    SHAPER (allow_shaper, for the FLOAT OCIO Player only): when the input is SCENE-LINEAR, a plain [0,1] domain
    can only see linear 0..1 - it crushes highlights >1 and under-samples the shadow toe, so a scene-linear
    Player looked FLAT. With allow_shaper the LUT's SAMPLING AXIS is ACEScct-coded: each
    [0,1] grid coord is decoded to scene-linear via _acescct_to_lin BEFORE the transform, so the LUT spans the
    full HDR range with log resolution. The shader must apply the matching lin->ACEScct encode. The OCIO Read
    video viewport does NOT pass allow_shaper (its data is display-referred 8-bit), so it is unchanged.
    Returns (n, bytes, shaper_on)."""
    import numpy as np
    n = int(size)
    lin = np.linspace(0.0, 1.0, n, dtype=np.float32)
    bb, gg, rr = np.meshgrid(lin, lin, lin, indexing="ij")   # C-order flatten -> index ((b*n+g)*n+r), r fastest
    grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3).astype(np.float32)   # [n^3, 3] identity rgb
    shaper = bool(allow_shaper) and (not raw) and _is_scene_linear(in_cs)
    if not raw and in_cs and out_cs and in_cs != out_cs:
        try:
            import torch
            axis = _acescct_to_lin(grid) if shaper else grid   # shaper: LUT axis is ACEScct-coded -> decode to scene-linear before the transform
            t = torch.from_numpy(np.ascontiguousarray(axis[None, :, None, :]))   # [1, n^3, 1, 3]
            grid = _convert(t, in_cs, out_cs)[0, :, 0, :].contiguous().numpy()
        except RuntimeError:
            shaper = False   # OCIO lib/config unavailable -> identity passthrough (same as _convert / _ocio_thumb)
    rgba = np.empty((grid.shape[0], 4), np.uint8)
    rgba[:, :3] = (np.clip(grid, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    rgba[:, 3] = 255
    return n, rgba.tobytes(), shaper


def _cs_combo(default):
    return _combo_or_string(_colorspace_names(), default, "Colorspace from the active OCIO (ACES) config.")


def _save_preview_png(frame0, filename):
    """Save one frame as an 8-bit PNG to the ComfyUI temp dir and return the ComfyUI ui 'images' list. Shared by
    OCIORead._preview and OCIOWrite._preview (same shape: naive display of the frame in its own colorspace)."""
    if folder_paths is None:
        return []
    tdir = folder_paths.get_temp_directory()
    os.makedirs(tdir, exist_ok=True)
    if hasattr(frame0, "detach"):                 # torch Tensor (OCIORead passes rgb[0]); OCIOWrite passes numpy
        frame0 = frame0.detach().cpu().numpy()
    px = (np.clip(np.asarray(frame0, np.float32), 0.0, 1.0) * 255.0).astype(np.uint8)
    Image.fromarray(px).save(os.path.join(tdir, filename))
    return [{"filename": filename, "subfolder": "", "type": "temp"}]


# --------------------------------------------------------------------------- nodes

class OCIORead:
    """Load a still / sequence / video and color-manage it on the way in (Nuke: Read).

    'input_colorspace' is the colorspace the FILE is in (auto by type: EXR -> ACEScg, JPG/PNG/TIFF -> sRGB).
    The IMAGE output is already converted to 'output_colorspace' (default sRGB - Display, ComfyUI's working
    space). 'fps' is read from the video metadata. 'info' reports frames / resolution / format."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "source": ("STRING", {"default": "", "tooltip": r"Path to a still, a sequence folder, one numbered frame or a video - anywhere on disk, or relative to the ComfyUI input folder. Use Open Files, or type it. A folder or one frame gives the whole sequence."}),
            "frame_mode": (["auto", "single", "sequence", "video"], {"default": "auto",
                           "tooltip": "auto: a numbered file with siblings becomes the whole sequence. single: just this file. sequence: force-collapse siblings. video: a movie clip. A folder is always a sequence; a video always its full clip. Your own choice is kept."}),
            "input_colorspace": _cs_combo(WORKING),
            "output_colorspace": _cs_combo(WORKING),
            "raw_data": ("BOOLEAN", {"default": False,
                         "tooltip": "Nuke 'Raw Data': skip the colorspace conversion and pass the file's values through untouched (input/output colorspace are ignored)."}),
            "start_frame": ("INT", {"default": 0, "min": 0, "max": 100000000,
                            "tooltip": "First frame number to load (0 = from the detected start). Auto-filled to the range when you pick a source. Below the original range, edge_mode fills in."}),
            "end_frame": ("INT", {"default": 0, "min": 0, "max": 100000000,
                          "tooltip": "Last frame number to load (0 = to the detected end). Above the original range, edge_mode fills in."}),
            "frame_shift": ("INT", {"default": 0, "min": 0, "max": 100000000,
                            "tooltip": "Re-base: the number the FIRST frame becomes downstream (Nuke frame offset). 0 = keep the source number (e.g. 86). Set 1 to start at 1, 10 to start at 10 - the whole range shifts with it. Flows to OCIO Write (first_frame + start_number)."}),
            "missing_frames": (["black", "hold", "error"], {"default": "black",
                               "tooltip": "Gaps INSIDE the sequence (e.g. 24 missing between 23 and 25): black = a black frame; hold = repeat the previous frame; error = stop. Missing frames are listed in 'info'."}),
            "edge_mode": (["hold", "loop", "bounce", "black"], {"default": "hold",
                          "tooltip": "Frames OUTSIDE the original range (Nuke before/after): hold the end frame, loop the sequence, bounce (ping-pong), or black."}),
            "fps": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 240.0, "step": 0.001,
                    "tooltip": "0 = take from the video metadata (24 for stills). Flows to OCIO Write through the wire."}),
        }}

    # 'source metadata' is index 5 and MUST stay last: an output connection is stored by SLOT INDEX, so inserting
    # a slot anywhere above it silently re-points every saved link below - a graph would reload with alpha wired
    # into an fps input. Appending cannot move an existing index.
    RETURN_TYPES = ("IMAGE", "MASK", "FLOAT", "STRING", "VIDEO", "STRING")
    RETURN_NAMES = ("image/sequence/video", "alpha", "fps", "info", "ComfyUI Video", "metadata")   # index 4 VIDEO output named "ComfyUI Video" so it reads right even if the front end ignores a post-create label mutation; output names are display-only (connections are by slot index), so no saved-graph break
    FUNCTION = "read"
    CATEGORY = "OCIO"

    def read(self, source, frame_mode, input_colorspace, output_colorspace, raw_data, start_frame, end_frame,
             frame_shift, missing_frames, edge_mode, fps):
        arr, info = load_source(source, start_frame, end_frame, frame_mode, missing_frames, edge_mode)
        image4 = torch.from_numpy(np.ascontiguousarray(arr.astype(np.float32)))   # [N,H,W,4]
        meta_fps = float(info.get("fps", 0.0) or 0.0)
        out_fps = float(fps) if fps and fps > 0 else (meta_fps if meta_fps > 0 else 24.0)
        rgb = image4[..., :3].contiguous()
        mask = image4[..., 3].contiguous()                    # alpha as MASK (1 = opaque)
        if not raw_data:
            rgb = _convert(rgb, input_colorspace, output_colorspace)
        # frame_shift re-bases the downstream numbering (the batch is unchanged; OCIO Write reads it via the wire)
        n = rgb.shape[0]
        base = frame_shift if frame_shift else info.get("orig_start", 0)
        shift_txt = f", frames [{base}-{base + n - 1}]" if info.get("kind") == "sequence" else ""
        kind, res = info.get("kind"), f"{arr.shape[2]}x{arr.shape[1]}"
        label = info.get("label", "")
        miss = info.get("missing") or []
        miss_txt = f", missing: {_collapse_ranges(miss)}" if miss else ""
        orig = f" orig[{info.get('orig_start')}-{info.get('orig_end')}]" if kind == "sequence" else ""
        cs = "raw" if raw_data else f"{input_colorspace} -> {output_colorspace}"
        head = {"sequence": f"sequence: {label}{orig}, {n} frame(s), {res}",
                "video": f"video: {label}, {n} frame(s), {res}, {out_fps:g} fps",
                "still": f"single: {label}, {res}"}.get(kind, f"{n} frame(s), {res}")
        txt = f"{head}{shift_txt}{miss_txt}, {cs}"
        # No "ui": {"images": ...} here - the front-end's own DOM-widget preview (ocio_io.js, /ocio/thumb) is
        # the single on-node preview for Read. A ui.images entry would render a SECOND, stale-after-run
        # thumbnail (ComfyUI paints it from node.imgs independently of the DOM widget). OCIOWrite keeps its
        # ui.images preview - it has no live front-end thumb, so that is still its only preview.
        # source metadata as JSON on a STRING wire: the plate's camera / lens / editorial attributes, for OCIO
        # Write's 'metadata' input. Never fails the read - an unreadable header returns a note, not an
        # exception, because a missing camera tag must not be the reason a render does not start.
        try:
            import json as _json
            meta_txt = _json.dumps(read_source_meta(source), ensure_ascii=False, default=str)
        except Exception as e:
            meta_txt = '{"attrs": {}, "note": "metadata unreadable: %s"}' % str(e)[:120].replace('"', "'")
        return (rgb, mask, out_fps, txt, _make_video(rgb, out_fps), meta_txt)   # VIDEO from the SAME color-managed batch + fps


_STILL_EXT = {"exr": "exr", "tiff": "tif", "png": "png", "jpeg": "jpg", "dpx": "dpx"}


def _cs_tag(name):
    """Colorspace name -> filename token, spelled out in full so that no two colorspaces can share one.

    'ACEScg' -> 'acescg', 'sRGB - Display' -> 'srgb_display',
    'Rec.1886 Rec.709 - Display' -> 'rec_1886_rec_709_display',
    'Linear ARRI Wide Gamut 4' -> 'linear_arri_wide_gamut_4'.

    WHY THIS REPLACED A TABLE OF SHORT TAGS (2026-08-12). The old scheme kept a "core token" and dropped the
    descriptive tail, which made 31 of the config's 55 colorspaces share a tag with at least one other:
    thirteen different GAMUTS all became 'linear' (ARRI AWG3 and AWG4, BMD, DaVinci, CinemaGamut, D-Gamut,
    V-Gamut, REDWideGamut, four S-Gamut3 variants, AdobeRGB); eight different TRANSFERS all became 'rec709'
    (Gamma 1.8 / 2.2 / 2.4, Rec.1886, sRGB-encoded, Camera Rec.709); six became 'p3', putting HDR PQ
    ST2084-P3-D65 next to SDR Display P3; and ARRI LogC3 and LogC4 both became 'logc'.

    That was not cosmetic. `_write_output_paths` builds the DELIVERED PATH from this token, so two writes
    differing only in output_colorspace produced the SAME filename and the second silently overwrote the
    first. The artist believes two versions exist, one does, and which one is not recoverable from the name.
    Spelling the name out takes the number of colliding colorspaces to zero.

    The length objection does not survive measurement: the old sanitiser truncated at 24 characters, and the
    longest spelled-out tag is 30 ('davinci_intermediate_widegamut'). Six characters, against 31 ambiguous
    deliverables.

    Names that were already unambiguous come out UNCHANGED - 'acescg', 'acescct', 'aces2065_1',
    'rec_2100_hlg_display', 'rec_2100_pq_display' - because for those the old fall-through already produced
    the spelled-out form.

    NOT TRUNCATED, deliberately. A fixed-width cut would re-introduce collisions between any two names sharing
    a prefix, which is the whole defect this removes. tools/test_cs_tag_unique.py asserts that no two
    colorspaces in the live config collide, so a truncation cannot be reintroduced quietly.
    """
    low = (name or "").lower()
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", low)).strip("_")


def _write_output_paths(folder, filename, container, still_format, video_codec, output_colorspace,
                        raw_data, colorspace_in_name, start_number, count, still_frame=None):
    """The exact output file path(s) OCIOWrite.write() creates for these params - SINGLE SOURCE OF TRUTH for both
    write() and the /ocio/write_paths overwrite check (so the "file exists?" prompt checks the real names). count =
    number of frames to write (1 for a still / video). still_frame (still image only): when not None, the source
    frame number to stamp in the name (name_cs.0039.png) - a still grabbed from a sequence / video; None = plain
    name (a single image). Added 2026-07-04."""
    name = (str(filename) if filename is not None else "").strip() or "ocio_out"
    tag = ("raw" if raw_data else _cs_tag(output_colorspace)) if colorspace_in_name else ""
    stem = f"{name}_{tag}" if tag else name
    if container == "video":
        return [os.path.join(folder, stem + video_ext(video_codec))]
    if container == "still image":
        ext = _STILL_EXT[still_format]
        if still_frame is not None:                                        # a frame grabbed from a seq/video -> stamp its source frame number
            return [os.path.join(folder, f"{stem}.{int(still_frame):04d}.{ext}")]
        return [os.path.join(folder, f"{stem}.{ext}")]
    ext = _STILL_EXT[still_format]                                          # sequence: 4-digit numbered frames
    sn = int(start_number)
    return [os.path.join(folder, f"{stem}.{sn + i:04d}.{ext}") for i in range(max(1, int(count)))]


# --------------------------------------------------------------------------- output folder (the $OUTPUT token)
# RESPONSIBLE FOR: keeping a machine-specific absolute path out of the saved workflow when nothing needs one.
# WHY IT MATTERS: this widget's value lives in widgets_values, and core SaveVideo / SaveImage embed the entire
# prompt + workflow JSON into the files they write. So an absolute server path typed here does not stay on this
# machine - it ships inside a delivered mp4 or png, in a graph the artist never inspects.
# WHAT WAS AND WAS NOT BROKEN (checked before changing anything): the widget default is already "" (empty ->
# the ComfyUI output dir), and web/ocio_io.js already relativises a browsed folder that sits under the output
# root (relToOutput). So the DEFAULT never carried an absolute path. What was missing is a way to SAY "under the
# output dir" explicitly, which is what the token adds - and the front end's relativiser was byte-exact, so a
# hand-typed path differing only in case or slash direction stayed absolute on Windows.
# An absolute path still resolves verbatim: pointing a Write at a NAS is a real, deliberate thing to do.
_OUTPUT_TOKEN = "$OUTPUT"


def resolve_output_folder(output_folder):
    """OCIO Write's output_folder widget -> a real directory. SINGLE SOURCE OF TRUTH: OCIOWrite.write() and the
    /ocio/write_paths overwrite check must agree, or the "file exists?" prompt checks a different folder than the
    one the render writes to.

        ""                  -> the ComfyUI output dir
        "$OUTPUT"           -> the same, said explicitly
        "$OUTPUT/shot_010"  -> under it (portable: no machine path in the saved graph)
        "shot_010"          -> under it (relative, unchanged behaviour)
        "//nas/vfx/out"     -> verbatim (absolute stays absolute - a NAS target is deliberate)
    """
    root = folder_paths.get_output_directory() if folder_paths else os.getcwd()
    s = str(output_folder or "").strip().strip('"')
    if not s:
        return root
    if s.upper() == _OUTPUT_TOKEN or s.upper().startswith(_OUTPUT_TOKEN + "/") or s.upper().startswith(_OUTPUT_TOKEN + "\\"):
        rest = s[len(_OUTPUT_TOKEN):].lstrip("/\\")
        return os.path.join(root, *[p for p in re.split(r"[\\/]+", rest) if p]) if rest else root
    return s if os.path.isabs(s) else os.path.join(root, s)


class OCIOWrite:
    """Color-manage an IMAGE batch and write it (Nuke: Write).

    container: still image (one frame), sequence (numbered frames), or video.
    'from_colorspace' is ComfyUI's working space (default sRGB - Display); 'output_colorspace' is the file's
    space, and the format picks the right default (EXR -> ACEScg, PNG/TIFF/JPEG -> sRGB). Give a folder + a
    name; the rest is added automatically:
        still image    -> <folder>/<name>.<ext>
        sequence       -> <folder>/<name>.<start_number..>.<ext>   (4-digit, re-based to start_number)
        video          -> <folder>/<name>.mov (ProRes/DNxHR), .mxf (DNxHR OP1a / OPAtom) or .mp4 (h264/hevc)
    bit_depth is per format (JPEG 8; PNG 8/16; TIFF 8/16/32f; EXR 16f/32f). The node preview shows the first
    written frame in its output colorspace, so a wrong colorspace pick is visible at a glance.

    METADATA. Every write also gets a <name>.json sidecar carrying the FULL set, because no format holds all of
    it: an EXR takes the whole attribute set, a TIFF the identity as tags plus XMP, an 8-bit PNG the identity as
    iTXt, a MOV nearly everything, an MP4 a whitelist, an MXF its user comments, a JPEG nothing. One sequence gets
    one sidecar, not one per frame. What each file actually kept is listed in the sidecar itself under
    container_keeps / sidecar_only, so nothing has to be taken on trust."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            # 'LTX 2.3 HDR' is NOT renamed even though its scope is now stated more narrowly: a combo value is
            # matched by STRING, and ComfyUI rejects an unknown one with HTTP 400 and no fallback, so renaming
            # it would break every saved graph that uses it. The scope lives in the tooltip and in the comment
            # at the mapping below instead.
            # SDR Rec.709 delivery appended 2026-08-12: every preset here was an HDR one, so the most ordinary
            # job in the list - hand a generation to an editor as Rec.709 - was the only one with no preset.
            # Unlike the HDR presets it does NOT force EXR 16f: its whole point is a display-referred delivery,
            # and the container stays whatever the artist chose.
            # THE FOOTGUN THAT COMES WITH THAT, named rather than hidden: still_format defaults to exr, and
            # _auto_input_cs maps any .exr back to ACEScg, so SDR-into-EXR writes display-referred codes into a
            # container this same pack re-reads as scene-linear. Not silently corrected - forcing a format is
            # exactly what this preset must not do, and quietly rewriting the artist's container would be worse
            # than saying so. The tooltip says which containers it is for.
            "profile": (["none", "auto", "LTX 2.3 HDR", "LTX 2.5 HDR (ACEScct)",
                        "LumiPic LogC3 (Flux/Qwen)", "LumiPic V10 LogC4", "Seedance 4K 10-bit",
                        "SDR Rec.709 delivery"],
                        {"default": "none",
                         # SHORTENED 2026-08-13, from 1181 characters. Measured in the live canvas: this was the
                         # longest tooltip in the pack by a wide margin, and a hover at the moment of a decision
                         # is not where anyone reads eleven hundred characters. What stays is what CHANGES THE
                         # CHOICE: the two LTX presets are not interchangeable, and SDR must not be left on EXR.
                         # The mechanism behind both - the LogC3 IC-LoRA, the --hdr ACEScct flag, why auto cannot
                         # detect 2.5 - is in README.md, and the code comments in write() carry the sources.
                         "tooltip": "Sets from/output colorspace; the HDR presets also force EXR 16f. LTX 2.3 and 2.5 are NOT interchangeable (2.3 wants linear Rec.709, 2.5 wants ACEScct log) and the wrong one comes out flat. SDR Rec.709 delivery: read docs/NODES_IO.md first."}),
            "from_colorspace": _cs_combo(WORKING),
            "output_colorspace": _cs_combo("ACEScg"),
            "container": (["still image", "sequence", "video"], {"default": "sequence"}),
            "still_format": (["exr", "tiff", "png", "jpeg", "dpx"], {"default": "exr",
                             "tooltip": "Used for still image / sequence (hidden for video). EXR keeps float, so negatives and values above 1.0 survive. DPX is the film-pipeline interchange format, integer, 10 or 16 bit. TIFF is float only at 32f. PNG and JPEG are integer and clip both tails; JPEG is always 4:2:0."}),
            # APPENDED, never inserted: a combo's saved value is matched by STRING, so adding entries at the END
            # leaves every existing saved graph resolving to exactly what it did before. The two MXF entries are
            # the same DNxHR HQ essence as dnxhr_hq with a different muxer, not a new codec.
            "video_codec": (["prores_4444", "prores_422hq", "prores_422", "dnxhr_hq", "h264", "hevc",
                             "dnxhr_hq_mxf", "dnxhr_hq_mxf_opatom", "dnxhr_hqx", "dnxhr_444",
                             # APPENDED, never inserted: this list is positional in a saved workflow, so a new
                             # entry in the middle would silently re-point every graph that stored an index.
                             "prores_4444_mxf", "prores_4444xq_mxf", "dnxhr_hqx_mxf", "dnxhr_444_mxf",
                             "hevc_444_12", "prores_4444xq", "ffv1"],
                            {"default": "prores_4444",
                             # SHORT ON PURPOSE. This tooltip was 855 characters and `profile`'s was 1181;
                             # measured in the live canvas, where a tooltip is a hover at the moment of a
                             # decision and nobody reads a paragraph. The depth table, the Avid-naming caveat
                             # and the OP1a-vs-OPAtom detail now live in README.md under "What each codec
                             # actually writes", which is where someone comparing options will actually look.
                             # The node draws the chosen codec's depth in its footer, so the number an artist
                             # needs is on screen without hovering at all.
                             "tooltip": "Video only. The node's footer states the depth once you pick. FFV1 is the only one that returns this pack's input unchanged (16-bit, .mkv, archival). HEVC 4:4:4 12-bit is the only genuine 12-bit encode - ffmpeg's ProRes and DNxHR encoders top out at 10 whatever the format is nominally worth. 8-bit: DNxHR HQ, h264, hevc, and those two move to 10-bit on an HDR output. Measured table: README.md."}),
            "bit_depth": (["16f", "32f", "16", "8", "10"], {"default": "16f",
                          "tooltip": "Per format: JPEG 8; PNG 8/16; TIFF 8/16/32f; EXR 16f/32f; DPX 10/16. The list narrows to the chosen format."}),
            # BACK TO ZIP, 2026-08-14. It was DWAA for one day, on the argument that lossy-and-small is the
            # house default across VFX for anything that is not a master. Measured on a real camera frame at
            # 1920x1318, raw_data on so only the compressor was under test:
            #
            #   16f zip   6342 KB   1843 distinct greens   max abs error 0.000118
            #   16f dwaa  1164 KB    855 distinct greens   max abs error 0.009525
            #   32f zip  14267 KB  19118 distinct greens   max abs error 0.000000
            #   32f dwaa  1164 KB    855 distinct greens   max abs error 0.009525
            #
            # DWAA does deliver its side of the bargain: 5.4x smaller. It also costs 54% of the distinct values
            # and multiplies the error by eighty, and it does that to 16f as well - which nothing in this pack
            # said, because the documented caveat was only about 32f being quantised to half. A pack whose whole
            # argument is that it does not throw information away should not have a lossy default, and Nuke,
            # whose Write node this one is modelled on, defaults to Zip.
            #
            # DWAA stays one pick away for a review or comp copy, which is what it is for. A graph saved with
            # either value keeps it; a default only ever reaches a node created fresh.
            "compression": (["zip", "zips", "piz", "pxr24", "dwaa", "dwab", "rle", "none"], {"default": "zip",
                            "tooltip": "EXR compression. ZIP (default) / ZIPS / RLE are lossless; PIZ is lossless and suits grain. DWAA / DWAB are much smaller and LOSSY: measured on a real frame they cost about half the distinct values and multiply the error by eighty, at 16f as well as 32f, and they quantise float32 to HALF before compressing - so 32f + DWAA is a half-precision file that still says 'float'. Pick DWAA for a review or comp copy, never for a master or a data pass (depth, normals, IDs). EXR only."}),
            # THE CAVEAT IS IN THE TOOLTIP because the parameter is genuinely front-end only: write() never reads
            # it. That is by construction - the detection walks the GRAPH to find the upstream OCIO Read, which
            # only the canvas can do - but the old wording did not say so, and a reviewer driving the pack through
            # /prompt found the consequence: a plate whose real frame is 106 wrote as .0001 with the box still
            # ticked. Stated rather than silently true. See also the note on auto_colorspace.
            "auto_range": ("BOOLEAN", {"default": True,
                           "tooltip": "ON: first_frame / last_frame / start_number / fps come from the upstream OCIO Read. CANVAS ONLY - an API prompt gets whatever is in its JSON, with this ticked and doing nothing. Set the four explicitly for farm work."}),
            "first_frame": ("INT", {"default": 1, "min": 0, "max": 100000000,
                            "tooltip": "still image: WHICH frame to save - a number outside the batch is refused by name, not rounded to the nearest frame. sequence/video: first frame to write (frame numbers, auto-filled from the source, e.g. 86)."}),
            "last_frame": ("INT", {"default": 0, "min": 0, "max": 100000000,
                           "tooltip": "sequence/video: last frame to write (0 = to the end; auto-filled from the source, e.g. 97). Ignored for a still image."}),
            "start_number": ("INT", {"default": 1, "min": 0, "max": 100000000,
                             "tooltip": "OUTPUT file numbering start (auto-filled to the source's first frame, e.g. 86; set 1 for 0001..). This is the re-base, NOT retime."}),
            "source_start": ("INT", {"default": 1, "min": 0, "max": 100000000,
                             "tooltip": "(auto) the source's first frame number, used to map first_frame/last_frame to the batch. Set by the wire."}),
            "raw_data": ("BOOLEAN", {"default": False,
                         "tooltip": "Nuke 'Raw Data': write the pixels as-is, skipping the from->out colorspace conversion. The file is also left UNTAGGED - no chromaticities on a still, no colour primaries / transfer / matrix on a movie - because unconverted pixels have no delivery space to name."}),
            "colorspace_in_name": ("BOOLEAN", {"default": True,
                                    "tooltip": "Put the output colorspace in the file name, before the frame number: name_acescg.0001.exr. Uses the sanitized output_colorspace (or 'raw' when Raw Data is on)."}),
            "output_folder": ("STRING", {"default": "", "tooltip": r"Empty = the ComfyUI output dir. Relative or $OUTPUT/sub sits under it; an absolute path is written there. PREFER relative or $OUTPUT: this value is stored in the workflow, and other nodes embed the workflow into the files they write."}),
            "filename": ("STRING", {"default": "ocio_out", "tooltip": "Base name. Numbering / extension are added automatically."}),
            # SUPERSEDED, AND THE TOOLTIP SAYS SO. This widget described an upstream trace that set both
            # colorspaces for an LTX HDR source. That job belongs to the `profile` combo's "auto" entry, which
            # runs the same trace and covers more sources, so the front-end helper behind this widget had no
            # callers at all and is gone. The widget stays because widgets_values is positional: dropping it
            # would shift every later value in every saved graph. write() has never read it.
            "auto_colorspace": ("BOOLEAN", {"default": True,
                                 "tooltip": "Superseded and inert - it changes nothing, which is why it is hidden. Use profile = 'auto', which traces the upstream source and covers more cases. Kept only so earlier saved graphs keep their other values in place."}),
        }, "optional": {
            "images": ("IMAGE", {"tooltip": "An image / sequence / video frame batch to write. Mutually exclusive with the ComfyUI Video input."}),
            "video": ("VIDEO", {"tooltip": "A ComfyUI native VIDEO (e.g. Load Video) to render out with ALL these Write settings (container, codec, colorspace, bit depth). Mutually exclusive with the image input; the movie's own frame rate is used for a video container."}),
            "alpha": ("MASK", {"tooltip": "Optional alpha channel -> RGBA (EXR / TIFF / PNG; ignored for JPEG). Wire OCIO Read's alpha output, or any MASK."}),
            "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0, "step": 0.001,
                              "tooltip": "Video frame rate. Wire OCIO Read's fps output here to carry the source rate. This sets the TIME BASE, it does not resample: the same frames are written either way, so a 24fps batch written at 48 plays twice as fast and half as long (a conform, not a frame-rate conversion). NTSC rates go out as their exact rational, 23.976 as 24000/1001."}),
            "render_nonce": ("STRING", {"default": "",
                             "tooltip": "Internal, hidden. The Render button bumps it so a repeat render to the same path really re-writes; ComfyUI would otherwise cache an identical Write and skip it."}),
            # Appended LAST on purpose: AUDIO carries no widget, so widgets_values is untouched, and a new
            # trailing input slot cannot reindex the saved links of the slots above it.
            "audio": ("AUDIO", {"tooltip": "Optional track. A video container muxes it in, trimmed to the frames written so it cannot drift; a sequence gets a sidecar .wav, because EXR / TIFF / PNG hold no audio. Formats per container: docs/NODES_IO.md."}),
            # A `start_timecode` STRING widget sat here from 2026-08-13 until later the same day. A code typed
            # into the writer is a code invented at delivery; the one that matters arrives with the plate, and
            # the writer's job is to carry it and advance it per frame. The start now comes from the wired
            # `metadata` (see _timecode_from_source), so there is nothing to type and nothing to get wrong.
            # Removing a widget is normally forbidden here, because widgets_values is POSITIONAL and dropping one
            # shifts every later value in every saved workflow. It is safe in this one case only because the
            # widget never reached a release: `git grep start_timecode origin/main` finds nothing, and no tag
            # contains the commit that added it, so no published graph can be holding a value at that index.
            # forceInput -> a SOCKET with no widget, so it adds nothing to widgets_values at all, and appending it
            # last keeps every existing optional socket at the index a saved graph already stored.
            # NAMED `metadata`, matching OCIO Read's output of the same name. It was `source_meta` against Read's
            # `source metadata`, so the two ends of one wire read differently and neither matched the other
            # (renamed 2026-08-13, before any release carried it: `source_meta` has zero occurrences in
            # origin/main, so no published graph can be holding the old key).
            "metadata": ("STRING", {"forceInput": True,
                         "tooltip": "(optional) Wire OCIO Read's 'metadata' output here - or any node that emits the same JSON - to carry the plate's camera, lens, editorial attributes AND its start timecode into the written file. Claims a colour transform invalidates are dropped, not copied: docs/NODES_IO.md. A wire this node cannot read is reported and ignored; it never stops the render."}),
            # The last WIDGET, and it must stay last for the positional reason above.
            # It covers a case the 'audio' socket cannot: a native ComfyUI Video input carries its own track and
            # write() adopts it when nothing is wired, so there is no wire to disconnect in order to decline it.
            # A socket can only ADD a track; only a widget can refuse one. Default True, which is the behaviour
            # every existing graph already has, so a saved workflow that has never seen this widget keeps its
            # sound (a missing widget value falls through to the Python default - execution.py treats an absent
            # OPTIONAL input that way, whereas a missing REQUIRED one is a hard error).
            "write_audio": ("BOOLEAN", {"default": True,
                            "tooltip": "OFF: no sound at all, no muxed track and no sidecar .wav, even when one is wired or a native Video input brings its own. ON: the wired input wins, else the Video input's track. Off for a picture-only master."}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("path",)
    FUNCTION = "write"
    OUTPUT_NODE = True
    CATEGORY = "OCIO"

    def _resolve_folder(self, output_folder):
        return resolve_output_folder(output_folder)

    def write(self, profile, from_colorspace, output_colorspace, container, still_format, video_codec,
              bit_depth, auto_range, first_frame, last_frame, start_number, source_start, raw_data,
              output_folder, filename, colorspace_in_name=True, auto_colorspace=True, compression="zip",
              alpha=None, fps=24.0, render_nonce="", images=None, video=None, audio=None,
              metadata="", write_audio=True):   # render_nonce: cache-buster (see INPUT_TYPES). images/video: mutually-exclusive inputs.
        if video is not None:                                        # a native ComfyUI VIDEO -> render it out with ALL these Write settings (container, codec, colorspace, bit depth)
            images, _vfps, _vaudio = _video_unwrap(video)
            if _vfps and _vfps > 0:
                fps = _vfps                                          # a video container inherits the movie's own frame rate
            if audio is None:
                audio = _vaudio                                      # a native VIDEO carries its own track; it used to land in a throwaway and the sound was lost (fixed 2026-08-12). An explicitly wired 'audio' still wins.
        if write_audio is None:
            # None IS NOT False, and treating them alike silently strips the sound from every workflow saved
            # before this widget existed. widgets_values is positional over ALL widgets, BUTTONS INCLUDED, and
            # this pack has two ("Output Folder", "Render") which serialise as null. write_audio was appended
            # right where the first of them used to sit, so an old 23-value graph posts `"write_audio": null`
            # instead of omitting the key - and the optional-input default only covers an ABSENT key, so the
            # value arrives as None. Reproduced in the canvas: every other widget restored correctly (filename,
            # fps 25, timecode 02:00:00:00) and this one came back null. The front end repairs it on load as
            # well (web/ocio_io.js onConfigure); this arm covers a prompt posted straight to the API, which
            # never touches the front end at all.
            write_audio = True
        if not write_audio:
            # Dropped HERE, once, before anything downstream can look at it - not at each of the three places
            # that consume it (the mux, the sidecar .wav, the on-node note). A parameter read in one branch and
            # ignored in the others is the failure this pack has already shipped once: `resend` was added to six
            # methods and three of them never tested it, so the signature accepted it and the behaviour ignored
            # it, with every test green.
            audio = None
        if images is None:
            raise ValueError("OCIO Write: connect an image / sequence to 'OCIO Img/Seq/Vid', OR a movie to 'ComfyUI Video'.")
        _LOG_PROFILES = {"LumiPic LogC3 (Flux/Qwen)": _logc3_to_lin, "LumiPic V10 LogC4": _logc4_to_lin}
        if profile in _LOG_PROFILES and not raw_data:
            arr_lin = images.detach().cpu().numpy().astype(np.float32).copy()
            arr_lin[..., :3] = _LOG_PROFILES[profile](arr_lin[..., :3])   # log_to_lin, RGB only; alpha untouched
            images = torch.from_numpy(arr_lin).to(images.device, images.dtype)
            from_colorspace = "Linear Rec.709 (sRGB)"
            output_colorspace = "ACEScg"
        elif profile == "LTX 2.3 HDR" and not raw_data:
            # 2.3 ONLY, and this is not a naming detail. LTX-2.3's HDR is an IC-LoRA trained on the ARRI LogC3
            # (EI 800) curve, and Lightricks' own ComfyUI node for it (LTXVHDRDecodePostprocess in their
            # ComfyUI-LTXVideo pack, hdr.py, category "Lightricks/HDR") already undoes that curve and emits
            # LINEAR frames. So this preset sits downstream of their node and correctly expects linear.
            # LTX-2.5's HDR is a DIFFERENT mechanism - ACEScct, via the --hdr flag in their reference CLI - and
            # this preset is wrong for it: applied to ACEScct log codes it would treat log as linear and leave
            # the image flat and grey. Use "LTX 2.5 HDR (ACEScct)" there. Confirmed 2026-08-12 against their
            # own repositories: their ComfyUI pack has an HDR workflow for 2.3 and none for 2.5, and greps for
            # acescct/acescg across that pack return zero.
            from_colorspace = "Linear Rec.709 (sRGB)"
            output_colorspace = "ACEScg"
        elif profile == "LTX 2.5 HDR (ACEScct)" and not raw_data:
            # 2.5's HDR path hands the VAE's output straight out as ACEScct LOG CODES in AP1 primaries: their
            # reference rotates source primaries to ACEScg BEFORE compressing (ltx-core hdr.py:126-138), so the
            # codes carry no gamut change and only the transfer has to be undone. ACEScct -> ACEScg in OCIO is
            # exactly that undo and nothing else, which is why no curve is applied by hand here - the config's
            # own transform does it, on the path the community has already vetted.
            # Their reference writes half-float EXR (media_io/exr.py:169,190), which is what the EXR 16f
            # forcing below produces, so our output matches theirs in container as well as in maths.
            from_colorspace = "ACEScct"
            output_colorspace = "ACEScg"
        elif profile == "SDR Rec.709 delivery" and not raw_data:
            # The ordinary delivery, and the only preset here that is not an HDR one: a display-referred sRGB
            # generation handed to an editor as Rec.709. from -> to are both display-referred, so this is a
            # transfer change (sRGB piecewise -> BT.1886 gamma 2.4) at unchanged primaries, which is exactly
            # what "same picture, correct for a broadcast monitor" means. No format forcing: the HDR presets
            # push EXR 16f because their whole point is scene-linear latitude, and this one's point is the
            # opposite, so the container stays whatever was chosen (ProRes and h264 are the usual answers).
            from_colorspace = "sRGB - Display"
            output_colorspace = "Rec.1886 Rec.709 - Display"
        # "Seedance 4K 10-bit" and "none"/"auto": no backend mapping - auto is resolved front-end, Seedance is
        # a pending placeholder (do not invent a colorspace mapping for it).
        if profile in ("LTX 2.3 HDR", "LTX 2.5 HDR (ACEScct)", "LumiPic LogC3 (Flux/Qwen)",
                       "LumiPic V10 LogC4") and not raw_data \
                and container != "video":
            still_format, bit_depth = "exr", "16f"                       # HDR presets always land as EXR 16f
        img = images if raw_data else _convert(images, from_colorspace, output_colorspace)
        arr = img.detach().cpu().numpy().astype(np.float32)
        a_arr = None
        if alpha is not None:
            a = alpha.detach().cpu().numpy().astype(np.float32)
            a_arr = a if a.ndim == 3 else a[None]                          # [N,H,W]
        n = arr.shape[0]
        folder = self._resolve_folder(output_folder)
        os.makedirs(folder, exist_ok=True)
        cs = None if raw_data else output_colorspace                       # colorspace stamped in metadata
        base = source_start if source_start else 1                         # logical number of the first batch frame
        # output paths via the shared _write_output_paths (same names the /ocio/write_paths overwrite check uses)
        def _wp(cnt, still_frame=None):
            return _write_output_paths(folder, filename, container, still_format, video_codec, output_colorspace,
                                       raw_data, colorspace_in_name, start_number, cnt, still_frame=still_frame)

        def alpha_of(src_a, i, ref):
            if src_a is None:
                return None
            fr = src_a[min(i, src_a.shape[0] - 1)]
            return fr if fr.shape[:2] == ref.shape[:2] else None

        rate = float(fps) if fps and fps > 0 else 24.0
        apcm, audio_note = None, ""
        # ---- metadata: what WE author about our own output, then whatever the source plate can legally add ----
        # THE PLATE IS READ FIRST, because the start timecode is now inherited from it rather than typed into
        # this node, and _authored_attrs needs that start before it can lay the per-frame code down. Everything
        # else about the ordering is unchanged: our authored values still win over the plate's.
        meta_note, dropped_keys, src_meta, src_attrs = "", [], {}, {}
        if metadata:
            import json as _json
            try:
                src_meta = _json.loads(metadata) or {}
            except Exception:
                src_meta = {}
                meta_note = "metadata ignored (not JSON)"
            # `attrs` IS CHECKED FOR BEING A MAPPING, not just the wrapper around it. The isinstance here used to
            # guard only `src_meta`, so valid JSON whose attrs was a list, a string or a number walked into
            # `dict()` and killed the render with a bare TypeError / ValueError. This socket is forceInput and
            # documented to take a wire from ANY source, and a JS-side `Object.entries()` serialisation produces
            # exactly the list-of-pairs shape that survives this line only to die later in _sidecar_payload. The
            # pack's rule is that metadata never stops a render: a plate description we cannot read is dropped
            # with a note and the pixels still get written.
            _raw_attrs = src_meta.get("attrs") if isinstance(src_meta, dict) else None
            if _raw_attrs is not None and not isinstance(_raw_attrs, dict):
                meta_note = "source attrs ignored (not an object)"
                _raw_attrs = None
                src_meta = dict(src_meta)
                src_meta["attrs"] = {}      # every later reader of src_meta sees the same sanitised shape
            src_attrs = dict(_raw_attrs or {})
            dropped_keys = _forbidden_meta_keys(src_attrs)
            for k in dropped_keys:
                src_attrs.pop(k, None)
        # Read BEFORE _META_RE_AUTHORED strips timeCode out of src_attrs below: that strip exists because the
        # plate's own per-frame code describes the plate's frame, not ours - we re-author it from this start,
        # advancing it per written frame, which is the only form that conforms correctly.
        start_tc = _timecode_from_source(src_attrs)
        base_attrs = _authored_attrs(output_colorspace, rate, start_number, start_tc, raw_data)
        if metadata:
            # MATCHED WITHOUT REGARD TO CASE OR SPELLING, because the same field arrives under whatever name the
            # writing application felt like. Found on a real camera master: an MXF out of DaVinci Resolve calls
            # its start code `timecode`, the set below spells it `timeCode`, so the plate's value sailed past
            # the strip and the delivered EXR ended up carrying TWO timecodes - ours, typed and advancing per
            # frame, beside a stale string frozen at the start. Whichever one a downstream tool reads first
            # decides how the shot conforms, which is precisely the coin-toss this strip exists to prevent.
            for k in list(src_attrs):
                if k in _EXR_STRUCTURAL or _is_re_authored(k):             # a 640x352 dataWindow on a 1280x704 render
                    src_attrs.pop(k, None)
            # OUR authored values WIN: they describe the file being written, the plate's describe the file that
            # came in. A plate's chromaticities stamped on a converted render is the same lie as a stale dataWindow.
            merged = {k: v for k, v in src_attrs.items() if k not in base_attrs}
            if isinstance(src_meta, dict) and src_meta.get("source"):
                merged.setdefault("com.ocio.sourceFile", str(src_meta["source"]))
            base_attrs = {**merged, **base_attrs}
        # EVERY still format now receives the attribute dict, not EXR alone: TIFF takes the identity set as real
        # tags plus an XMP packet, an 8-bit PNG takes it as iTXt chunks, and _save_still ignores what a format
        # cannot hold. `as_text` is what makes that possible - only an EXR header takes an OpenEXR.TimeCode
        # object, so TIFF and PNG get the resolved SMPTE string from the same per-frame placeholder.
        as_text = still_format != "exr"
        still_attrs = base_attrs if container != "video" else None
        side = None

        def tc_text(offset):
            """The SMPTE start code for the frame at `offset`, or None when the plate carried no usable code.
            Resolved from the SAME _tc_advance the headers use, so a sidecar can never disagree with the frame
            beside it.

            SWALLOWS the advance's own error, which _frame_attrs already does for the header it writes. That
            became necessary when the start stopped being typed here and started arriving from a foreign file:
            _tc_advance rejects an illegal drop-frame label loudly, which is right for a code a human entered
            and wrong for one a plate happened to carry - a delivery must not fail because someone else's
            header is malformed."""
            if start_tc is None:
                return None
            try:
                return _timecode_string(*_tc_advance(start_tc, offset, rate))
            except Exception:
                return None
        if container == "still image":
            # THE CLAMP WAS REMOVED, and it was not a rounding nicety. `min(max(0, first_frame - base), n - 1)`
            # answered every out-of-range request with the nearest frame it had: asking for frame 999 of a
            # 3-frame batch wrote `name.0999.exr` containing frame 3, reported success, and said nothing about
            # the substitution - a file whose name is a claim about which frame it is, and the claim was false.
            # On an empty batch the same expression underflowed to -1 and died on `arr[-1]` with a bare
            # IndexError. The sequence branch below already refuses an empty range in words, and OCIO Read
            # exposes edge_mode for callers who genuinely want a hold or a loop, so silence was never the
            # house style here; this branch had just never been told.
            if n == 0:
                raise RuntimeError("nothing to write (input has 0 frames)")
            idx = first_frame - base
            if not 0 <= idx < n:
                raise RuntimeError(
                    f"frame {first_frame} is not in the input (frames {base}-{base + n - 1}, {n} frame(s)). "
                    f"Set first_frame inside that range, or use OCIO Read's edge_mode if you want a hold.")
            # a still grabbed from a sequence / video (n>1) stamps its SOURCE frame number in the name
            # (name_cs.0039.png, not name_cs.png); a genuine single image (n==1) keeps the plain name.
            saved = _wp(1, still_frame=(first_frame if n > 1 else None))[0]
            _save_still(saved, arr[idx], still_format, bit_depth, alpha_of(a_arr, idx, arr[idx]), cs, compression,
                        _frame_attrs(still_attrs, 0, as_text))
            count, preview, written = 1, arr[idx], arr[idx]     # `written` is what the clip check must measure
            if still_attrs:
                side = _write_meta_sidecar(saved, _sidecar_payload(
                    saved, _frame_attrs(still_attrs, 0, True), tc_text(0), src_meta, rate,
                    f"{still_format} {bit_depth}", kind="still image", bit_depth=bit_depth, frames=1,
                    first_file=saved))
            if audio is not None:
                audio_note = "audio ignored (still image)"                 # one frame of sound is not a deliverable; say so rather than drop it in silence
        else:
            s = max(0, first_frame - base)                                 # frame numbers -> batch sub-range
            e = (last_frame - base + 1) if (last_frame and last_frame >= first_frame) else n
            sub, sub_a = arr[s:e], (a_arr[s:e] if a_arr is not None else None)
            if sub.shape[0] == 0:
                raise RuntimeError(f"nothing in write range [{first_frame}-{last_frame}] (input has {n} frame(s))")
            # cut the track to the frames actually written (s is the batch offset of first_frame), so a partial
            # range stays in sync instead of starting from the head of the clip.
            apcm = _audio_pcm(audio, rate, sub.shape[0], s)
            if container == "video":
                saved = _wp(1)[0]
                side = save_video(sub, saved, video_codec, rate, None if raw_data else output_colorspace, apcm,
                                  meta_attrs=base_attrs, timecode=tc_text(0), source_meta=src_meta)
                if apcm is not None and video_codec in _MXF_MUXER and _MXF_MUXER[video_codec] == "mxf_opatom":
                    # OPAtom holds ONE essence per file (ST 390) and ffmpeg refuses a second stream outright, so
                    # the track goes beside it rather than being dropped in silence - the same answer an image
                    # sequence gets, for the same reason.
                    wav = os.path.splitext(saved)[0] + ".wav"
                    _save_wav24(wav, apcm[0], apcm[1])
                    audio_note = f"+sidecar {os.path.basename(wav)} (OPAtom holds one essence per file)"
                elif apcm is not None:
                    audio_note = f"+audio {apcm[2]}ch {apcm[1]}Hz"
                if side:
                    meta_note = (meta_note + "; " if meta_note else "") + f"sidecar {os.path.basename(side)}"
            else:                                                          # sequence
                paths = _wp(sub.shape[0])
                for i in range(sub.shape[0]):
                    _save_still(paths[i], sub[i], still_format, bit_depth, alpha_of(sub_a, i, sub[i]), cs, compression,
                                _frame_attrs(still_attrs, i, as_text))
                saved = paths[0]
                if still_attrs:
                    # ONE sidecar for the whole sequence, not one per frame: the attributes are shot-level, and
                    # N copies of the same JSON is noise an artist has to tidy up. The per-frame parts are given
                    # as the start code plus the frame count and the first / last filenames.
                    side = _write_meta_sidecar(paths[0], _sidecar_payload(
                        paths[0], _frame_attrs(still_attrs, 0, True), tc_text(0), src_meta, rate,
                        f"{still_format} {bit_depth}", kind="sequence", bit_depth=bit_depth,
                        frames=len(paths), first_file=paths[0], last_file=paths[-1]), strip_frame=True)
                if apcm is not None:
                    # EXR / TIFF / PNG carry no audio, so the track ships beside the frames as a reference WAV.
                    wav = os.path.splitext(paths[0])[0].rsplit(".", 1)[0] + ".wav"   # strip the frame number too
                    _save_wav24(wav, apcm[0], apcm[1])
                    audio_note = f"+sidecar {os.path.basename(wav)}"
            count, preview, written = sub.shape[0], sub[0], sub   # EVERY frame, not sub[0]: see _range_clip_note

        ui = {"ocio": [("raw" if raw_data else f"{from_colorspace} -> {output_colorspace}")],
              "count": [str(count)], "saved": [os.path.basename(saved)]}
        if audio_note:
            ui["audio"] = [audio_note]        # read by web/ocio_io.js (on-node text + toast); 'saved' has no reader
        # ONLY REPORT WHAT ACTUALLY WENT INTO A FILE. PNG / TIFF / JPEG have no header for chromaticities or a
        # timecode, so listing them for those formats would be the node claiming work it did not do - the same
        # class of error as writing a false attribute, just aimed at the artist instead of the file.
        bits = []
        # THE RANGE THAT DID NOT REACH THE FILE COMES FIRST, ahead of the metadata, for two reasons: the canvas
        # draws only the first 120 characters of this line, and a lost stop of highlight matters more to a
        # delivery than which tags travelled. It rides `ui["meta"]` rather than a new key because that key is
        # already the node's status line, is already drawn, and already raises a warning toast on the Vue
        # frontend where no corner text renders at all - a new key would have needed a reader written for it.
        clip_note = _range_clip_note(
            written, f"{still_format} {bit_depth}" if container != "video" else str(video_codec),
            _container_keeps_range(container, still_format, bit_depth),
            255.0 if (container != "video" and bit_depth == "8") else 65535.0)
        if clip_note:
            bits.append(clip_note)
            # AND the log, because an artist driving /prompt from a script never sees the canvas at all.
            logging.warning("OCIO Write: " + clip_note)
        if container != "video" and still_format == "exr":
            # 32-BIT ASKED FOR, HALF DELIVERED - say so, because nothing else will. DWA quantises FLOAT to
            # HALF before it compresses: OpenEXR's own ImfDwaCompressor puts it as "When dealing with FLOAT
            # source buffers, we first quantize the source to HALF and continue down as we would for HALF
            # source." The header still declares `float`, so no reader downstream can tell. Measured on
            # 49152 pixels: a 32f/zip file carries 49086 values that no half can represent, the same data at
            # 32f/dwaa carries ZERO, and distinct values collapse 49071 -> 4765.
            # Not blocked, because the choice may be deliberate for a review copy - but never silent, and
            # the note lands in the ui payload AND in the log for anyone driving /prompt from a script.
            if bit_depth == "32f" and compression in ("dwaa", "dwab"):
                _dwa_note = (f"{compression} quantises float32 to half BEFORE compressing (OpenEXR "
                             "ImfDwaCompressor), so this 32f file carries half precision. Use zip / zips / "
                             "piz for a true 32-bit master, or a data pass (depth, normals, IDs).")
                bits.append(_dwa_note)
                logging.warning("OCIO Write: " + _dwa_note)
            if base_attrs.get("chromaticities"):
                bits.append(f"chromaticities {base_attrs.get('com.ocio.gamut', '')}".strip())
            if base_attrs.get("adoptedNeutral"):
                an = base_attrs["adoptedNeutral"]
                bits.append(f"adoptedNeutral {an[0]:.5g},{an[1]:.5g}")
            if base_attrs.get("colorInteropID"):
                bits.append(base_attrs["colorInteropID"])
            # tc_text can decline even when a start WAS read: the plate's code may parse and still be an
            # illegal drop-frame label, in which case the frame ships without a timecode. Reporting one we
            # did not write would be worse than reporting none.
            _tc0 = tc_text(0)
            if _tc0:
                bits.append("tc " + _tc0)
        elif container == "video":
            # The movie carries the NCLC colour tags, the mappable container tags and a tmcd timecode track; the
            # colorimetry and everything the container cannot hold is in the sidecar named below.
            # tc_text can decline even when a start WAS read: the plate's code may parse and still be an
            # illegal drop-frame label, in which case the frame ships without a timecode. Reporting one we
            # did not write would be worse than reporting none.
            _tc0 = tc_text(0)
            if _tc0:
                bits.append("tc " + _tc0)
        else:
            # STILL TRUE AND STILL SAID: neither TIFF nor PNG has a header for chromaticities or a real timecode.
            # What they DO now carry is the shot's identity, and naming exactly that - rather than the colorimetry
            # they cannot hold - is the difference between a report and a claim.
            n_ident = len(_identity_meta(_frame_attrs(still_attrs, 0, True))) if still_attrs else 0
            # THE 16-BIT EXCLUSION IS GONE, and leaving it was the other half of a fix half-applied
            # (2026-08-13). _png_splice_text made a 16-bit PNG carry the identity set, and _embedded_meta_keys
            # was updated to match - but this branch was not, so the node went on telling the artist "16-bit PNG
            # carries no text chunks" about files that demonstrably had all seven fields in them, with the
            # sidecar beside it saying the opposite. A status line that contradicts the artefact is worse than
            # none: it sends someone looking for a sidecar that already holds nothing extra.
            if still_format in ("tiff", "png") and n_ident:
                where = "tags + XMP" if still_format == "tiff" else "iTXt chunks"
                bits.append(f"{n_ident} identity field(s) as {where}")
            bits.append(f"{still_format} carries no colour metadata header (EXR does)")
        if side and container != "video":
            # The sidecar is NAMED for stills too, not only for movies. It is where the half no format can hold
            # actually lives, so an artist who cannot find a tag needs to know the file is there.
            bits.append(f"sidecar {os.path.basename(side)}")
        if dropped_keys:
            # NAMED, not silently swallowed: these were dropped because a colour transform makes them false, and
            # an artist who wired a plate through has to be told its HDR / provenance claims did not travel.
            bits.append("dropped as pixel-state claims (false after a colour transform): " + ", ".join(dropped_keys[:6]))
        if meta_note:
            bits.append(meta_note)
        if bits:
            ui["meta"] = ["; ".join(bits)]
        if container == "video":
            # The output video can sit ANYWHERE on disk; ComfyUI's native preview only serves output/temp/input, and a
            # still PNG renders broken inside its <video> for a video node ("Invalid URL"). So write a small, always-
            # servable H.264 preview into the temp dir and show it as an animated (playing) preview instead.
            ui["images"] = self._video_preview(sub, fps, saved, apcm)      # apcm, not the raw AUDIO: it is already cut to this write's range, so the preview cannot disagree with the master about where the clip starts
            ui["animated"] = (True,)
        else:
            ui["images"] = self._preview(preview)
        return {"ui": ui, "result": (saved,)}

    def _preview(self, frame0):
        """First written frame, shown naively in its output colorspace (a wrong pick looks visibly wrong)."""
        return _save_preview_png(frame0, "ocio_write_preview.png")

    def _video_preview(self, arr, fps, seed="", audio_pcm=None):
        """A small, always-servable H.264 preview of the just-written clip, in ComfyUI's TEMP dir, for the node's
        video preview (the real output may be an absolute path ComfyUI cannot serve). Downscaled to <=512 wide and
        capped to 96 frames, so it is cheap and browser-playable (h264) even when the master is ProRes/DNxHR. Returns
        the ui 'images' list; pair with 'animated': (True,) so ComfyUI shows a playing video.

        The preview carries the audio too (AAC in the mp4), trimmed to the SAME frame cap - so lip sync can be
        checked on the node instead of only after opening the master in a player."""
        if folder_paths is None:
            return []
        try:
            tdir = folder_paths.get_temp_directory()
            os.makedirs(tdir, exist_ok=True)
            cap = min(int(arr.shape[0]), 96)
            a = np.asarray(arr, np.float32)[:cap, :, :, :3]                            # cap frames, RGB only
            h, w = int(a.shape[1]), int(a.shape[2])
            if w > 512:
                import cv2
                nw = 512
                nh = max(2, int(round(h * (512.0 / w))))
                nh -= nh % 2                                                          # even dims for h264
                a = np.stack([cv2.resize(f, (nw, nh), interpolation=cv2.INTER_AREA) for f in a])
            rate = float(fps) if fps and fps > 0 else 24.0
            ap = None
            if audio_pcm is not None:
                samples, sr, ch = audio_pcm
                keep = max(1, int(round(cap / rate * sr)))                             # same cut as the frames above
                ap = (np.ascontiguousarray(samples[:keep]), sr, ch)
            name = "ocio_write_prev_" + hashlib.md5(str(seed).encode("utf-8", "ignore")).hexdigest()[:8] + ".mp4"
            save_video(a, os.path.join(tdir, name), "h264", rate, None, ap)
            return [{"filename": name, "subfolder": "", "type": "temp"}]
        except Exception:
            return []


# --------------------------------------------------------------------------- OCIO Player (in-graph float viewer)
_PLAYER_FRAME_CAP = 240   # cap CACHED viewer frames per node (full-res half-float is heavy); the OUTPUT is uncapped


def _player_cache(unique_id, images, alpha):
    """Write the incoming batch as full-res HALF-float RGBA frames to a temp dir the on-node float viewport reads.
    NOT a proxy: full resolution, HDR-preserving half float (the EXR-half display standard), so the viewer shows
    the material 'as is' with exposure. This node's previous cache is cleared first; capped to _PLAYER_FRAME_CAP.
    Returns (dir, total_frames, cached_frames, h, w). Added 2026-07-03 for the OCIO Player node."""
    root = folder_paths.get_temp_directory() if folder_paths is not None else os.path.join(os.path.expanduser("~"), ".ocio_tmp")
    d = os.path.join(root, "ocio_player", f"n{unique_id}")
    if os.path.isdir(d):
        for f in os.listdir(d):
            try:
                os.remove(os.path.join(d, f))
            except Exception:
                pass
    os.makedirs(d, exist_ok=True)
    arr = images.detach().cpu().numpy().astype(np.float32)             # [N,H,W,3]
    a = alpha.detach().cpu().numpy().astype(np.float32) if alpha is not None else None
    n, h, w = int(arr.shape[0]), int(arr.shape[1]), int(arr.shape[2])
    cap = min(n, _PLAYER_FRAME_CAP)
    for i in range(cap):
        # SLICED TO THREE CHANNELS, and the slice is the fix (2026-08-13). The comment above says [N,H,W,3] and
        # nothing enforced it: an RGBA IMAGE went through whole and the dstack below produced a FIVE-channel
        # frame file. Measured - a 4-channel input gave (8, 12, 5) where the viewer reads 4, so every texel after
        # the first was misaligned, and the front end's own size guard passes because 5 channels is larger than
        # the 4 it checks for, not smaller. OCIO Read cannot reach it (it slices at :1153 and :1156), but any
        # third-party node that hands out RGBA as IMAGE can, and several do. Four other places in this file
        # already slice the same way; this one had been missed.
        rgb = arr[i][..., :3]
        if a is not None:
            av = a[min(i, a.shape[0] - 1)]
            al = av if (av.ndim == 2 and av.shape[:2] == rgb.shape[:2]) else np.ones(rgb.shape[:2], np.float32)
        else:
            al = np.ones(rgb.shape[:2], np.float32)
        rgba = np.dstack([rgb, al]).astype(np.float16)                # HALF float: HDR-preserving, half the temp + texture
        np.save(os.path.join(d, f"f.{i:05d}.npy"), np.ascontiguousarray(rgba))
    return d, n, cap, h, w


class OCIOPlayer:
    """In-graph float viewer + color / range pass-through (a Nuke 'Viewer' analog, OCIO-managed). Feed it an
    IMAGE batch from LoadImage / a video loader / OCIO Read / anything: the on-node float WebGL viewport shows
    it AS IS (full resolution, HDR) with a VIEW-ONLY exposure control and live colorspace + metadata, and the
    node is INPUT ONLY and returns nothing: input_colorspace -> output_colorspace and [start_frame, end_frame]
    set what the viewer shows, not what leaves the node. Exposure is a viewing tool too. A still is N=1, a sequence /
    video is N>1; the viewer scales to the frame size either way."""

    @classmethod
    def INPUT_TYPES(cls):
        cs = _colorspace_names()
        return {
            "required": {
                "input_colorspace": _combo_or_string(cs, WORKING, "The colorspace the incoming batch is in (front-end auto-guesses ACEScg for HDR / >1 data, else sRGB - Display)."),
                "output_colorspace": _combo_or_string(cs, WORKING, "The colorspace the viewer converts to for display."),
                "raw_data": ("BOOLEAN", {"default": False, "label_on": "raw (no convert)", "label_off": "color-managed",
                                         "tooltip": "Show pixels untouched (no colorspace conversion for display)."}),
                "start_frame": ("INT", {"default": 0, "min": 0, "max": 100000000,
                                        "tooltip": "First frame to play, in the source numbering shown on the timeline. The viewer holds the whole input."}),
                "end_frame": ("INT", {"default": 0, "min": 0, "max": 100000000,
                                      "tooltip": "Last frame to play, source numbering (0 = through the end)."}),
                "fps": ("FLOAT", {"default": 24.0, "min": 0.0, "max": 1000.0, "step": 0.001,
                                  "tooltip": "Playback rate for the viewer."}),
            },
            "optional": {
                "images": ("IMAGE", {"tooltip": "Any IMAGE batch - still (N=1), sequence or video (N>1). Optional: connect a Load Video node to 'video' instead to stream a movie without materializing it."}),
                "video": ("VIDEO", {"tooltip": "Connect a Load Video node here to STREAM a movie in the viewport (WebCodecs decode-on-demand, no whole-clip materialization). Takes priority over 'images' for what the viewer shows."}),
                "alpha": ("MASK", {"tooltip": "Optional alpha to view / carry through."}),
                "base": ("STRING", {"default": "0",
                                    "tooltip": "Hidden. The source's first-frame number, set from the upstream OCIO Read: start_frame / end_frame are SOURCE numbers and this is subtracted to reach batch indices. 0 = already 0-based."}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    # The Player is a pure VIEWER - INPUT ONLY, NO outputs (like Preview Image). It branches
    # OFF the graph to preview; nothing passes THROUGH it. Wiring its output back INTO the flow used to break it
    # (stuck on frame 1, endless Refresh) and tempted people to route data through a viewer, so the outputs are
    # gone. OUTPUT_NODE keeps it running on queue so its viewport updates.
    RETURN_TYPES = ()
    RETURN_NAMES = ()
    OUTPUT_NODE = True
    FUNCTION = "play"
    CATEGORY = "OCIO"

    def play(self, input_colorspace, output_colorspace, raw_data, start_frame, end_frame, fps,
             images=None, alpha=None, base=0, video=None, unique_id="0"):
        # A connected VIDEO streams client-side (WebCodecs decode-on-demand) - do NOT materialize its frames
        # (a long 4K clip is hundreds of GB). Extract just the file path + a cheap probe for the front end; skip
        # the float cache. Takes priority over 'images' for what the viewer shows.
        vpath = ""
        if video is not None:
            try:
                src = video.get_stream_source()
                vpath = src if isinstance(src, str) else ""
            except Exception:
                vpath = ""
        if vpath:
            vw = vh = 0
            vfps = 0.0
            vframes = 0
            try:
                _require_ffmpeg()
                pr = subprocess.run([_FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                     "stream=width,height,nb_frames,r_frame_rate,avg_frame_rate,duration",
                                     "-of", "default=noprint_wrappers=1", vpath], capture_output=True, text=True)
                pi = dict(l.split("=", 1) for l in pr.stdout.strip().splitlines() if "=" in l)
                vw, vh = int(pi.get("width", 0) or 0), int(pi.get("height", 0) or 0)
                vfps = _video_fps(pi)
                vframes = int(pi.get("nb_frames", 0) or 0)
                if not vframes and vfps and pi.get("duration"):
                    vframes = int(round(float(pi["duration"]) * vfps))
            except Exception:
                pass
            return {"ui": {"video_path": [vpath], "video_res": [f"{vw}x{vh}"], "video_fps": [str(vfps)],
                           "video_frames": [str(vframes)], "input_cs": [input_colorspace]},
                    "result": ()}                               # INPUT-ONLY viewer: nothing flows out
        if video is not None and not vpath:
            # 2026-07-04: a PROCESSED / in-memory VIDEO (VideoFromComponents, e.g. from Load Video -> OCIO color node)
            # has NO file to stream. Unwrap it to frames and show the PROCESSED result through the float cache below -
            # otherwise the viewer fell through to the empty branch and the intermediate OCIO nodes looked ignored.
            try:
                _vframes, _vfps, _ = _video_unwrap(video)
                images = _vframes
                if _vfps and _vfps > 0:
                    fps = _vfps
            except Exception:
                pass
        if images is None:                                          # nothing connected -> empty viewer
            return {"ui": {}, "result": ()}
        cache_dir, total, cached, h, w = _player_cache(unique_id, images, alpha)   # cache the input as float .npy for the viewport (INPUT-ONLY: no trim, no output)
        cs = "raw" if raw_data else f"{input_colorspace} -> {output_colorspace}"
        cap_note = f", viewer capped at {cached}" if cached < total else ""
        info = f"player: {total} frame(s) in{cap_note}, {w}x{h}, {cs}"
        # 2026-07-04: cheap CONTENT signal (first-frame mean/std). The front-end skips re-initialising the viewport on an
        # UNRELATED render by comparing an execution signature - but dir/size/count stay identical when only the PIXELS
        # change (e.g. flipping a LogConvert swap_direction upstream), so without a content term the viewport went stale.
        try:
            _f0 = images[0].float()
            content_sig = f"{float(_f0.mean()):.6f}_{float(_f0.std()):.6f}"
        except Exception:
            content_sig = ""
        return {"ui": {"player_dir": [cache_dir], "player_total": [str(total)], "player_cached": [str(cached)],
                       "resolution": [f"{w}x{h}"], "fps": [str(float(fps))], "input_cs": [input_colorspace],
                       "content_sig": [content_sig]},
                "result": ()}


NODE_CLASS_MAPPINGS = {"OCIORead": OCIORead, "OCIOWrite": OCIOWrite, "OCIOPlayer": OCIOPlayer}
NODE_DISPLAY_NAME_MAPPINGS = {"OCIORead": "OCIO Read", "OCIOWrite": "OCIO Write", "OCIOPlayer": "OCIO Player"}
