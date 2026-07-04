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
import re
import shutil
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
                    _require_ocio, _resolve_config_keyed, _scan_files)

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

def _auto_output_cs(container, still_format):
    if container == "video":
        return WORKING
    return "ACEScg" if still_format == "exr" else WORKING


# --------------------------------------------------------------------------- loading

def _read_still(path):
    """One still -> float32 RGBA [H,W,4] (alpha 1.0 if the file has none). Integer formats normalise to 0..1
    (alpha too); float (EXR / float TIFF) keeps its real range (scene-linear values can exceed 1)."""
    ext = os.path.splitext(path)[1].lower()
    a, bgr = None, False
    if ext in (".exr", ".hdr", ".dpx"):
        if cv2 is not None:
            a = cv2.imread(path, cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH)
            bgr = True
        if a is None and ext in (".exr", ".hdr"):
            raise RuntimeError(f"cv2 could not read {path} (set OPENCV_IO_ENABLE_OPENEXR=1 on the server).")
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
        return sorted(fs, key=_frame_num)
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


def _video_input_cs(info):
    """OCIO input colorspace for a video, mapped from its ffprobe color metadata (color_primaries /
    color_transfer / color_space), GUARDED to names that exist in the active config. Only overrides the working
    sRGB - Display default for HDR / wide-gamut sources (PQ / HLG / bt2020), where the tag genuinely matters.
    Owner ask 2026-07-04: ordinary bt709 mp4 / mov are internet deliverables (Adobe Premiere -> web), so they
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
    # SDR Rec.709 / Rec.601 / plain sRGB: fall through to WORKING (sRGB - Display) - the internet-delivery default
    # ordinary mp4/mov users expect. The widget stays editable if a bt709 camera plate really wants Rec.709.
    return cs or WORKING


def _exr_fps(path):
    """The OpenEXR `framesPerSecond` rational attribute as a float, or None if absent/unreadable. OpenEXR is
    NOT a hard dependency of this pack (requirements ship cv2/tifffile/PIL for pixels; ffprobe does NOT read the
    EXR fps attribute - it reports the image2 demuxer default 25). Import it lazily so a machine without the
    module just falls back to the default fps instead of breaking sequence detection. Reads only the header, so
    it is cheap even on a 50 MB EXR. Added 2026-07-03: sequence fps from EXR metadata (see _seq_fps)."""
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
    JPEG) have no fps attribute, so they take the default. Owner spec 2026-07-03: default is 23.976, not 24."""
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
# on them ("ocio read не проигрывает вообще"). Transcode ONCE to a small H.264 mp4 - downscaled to _PROXY_MAX_SIDE
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
    source = source.rstrip("/")
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
    source = source.rstrip("/")
    s = source if os.path.isabs(source) else os.path.join(_input_dir(), source)
    ext = os.path.splitext(s)[1].lower()
    if ext in VIDEO_EXTS:
        pr = subprocess.run([_FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=nb_frames,r_frame_rate,avg_frame_rate,duration,"
                             "color_primaries,color_transfer,color_space",
                             "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1", s],
                            capture_output=True, text=True)
        # stream + format sections flatten to one key=value dict; format's duration is printed last, so it wins
        # over an absent/N-A stream duration (needed for MXF, whose stream carries no nb_frames or duration).
        info = dict(line.split("=", 1) for line in pr.stdout.strip().splitlines() if "=" in line)
        nb = _video_frame_count(info)   # robust: nb_frames, or duration x fps when nb_frames is 'N/A' (MXF)
        # video frame numbering is 1-BASED (owner 2026-07-04: frame 1 is the first frame, not 0) - a 451-frame clip is 1..451
        return {"kind": "video", "start": 1, "end": nb, "count": nb, "fps": _video_fps(info),
                "input_cs": _video_input_cs(info)}   # colorspace from the video's color metadata (owner ask)
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
    if ext in (".exr", ".hdr", ".dpx") and cv2 is not None:
        a = cv2.imread(path, cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH)
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


def read_meta(source):
    """Read-only metadata panel data for the front-end (/ocio/meta): resolution, format, frame range + count,
    fps, the auto-detected input colorspace, and whether the file carries an alpha channel. Reuses _seq_range
    for the range/count/fps/kind (same detection the auto-fill uses) and adds the fields _seq_range does not
    need: resolution, container/codec, and alpha presence. Never raises - callers get {"error": ...} instead,
    matching /ocio/thumb's contract, since this backs a UI panel that must not 500 on a bad path."""
    source = (source or "").rstrip("/")
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
                    "fps": rng.get("fps", 0.0), "input_colorspace": _video_input_cs(info),
                    "alpha": pix_fmt.endswith("a") or "argb" in pix_fmt or "rgba" in pix_fmt,
                    "color_primaries": info.get("color_primaries", "") or "",
                    "color_transfer": info.get("color_transfer", "") or ""}
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
                "missing": rng.get("missing", ""), "missing_count": rng.get("missing_count", 0)}
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


def _save_still(path, rgb, fmt, bit_depth, alpha=None, colorspace=None, compression="zip"):
    """Write one frame. bit_depth per format: exr 16f/32f (half/float), tiff 8/16/32f, png 8/16, jpeg 8.
    alpha (H,W) -> RGBA (exr/tiff/png; ignored for jpeg). colorspace is stamped into the file metadata
    where the format allows it (png text, tiff description, jpeg comment)."""
    fmt = fmt.lower()
    has_a = alpha is not None
    desc = colorspace or ""

    def with_a(x):                                   # (H,W,3) -> (H,W,4) if alpha present
        return np.dstack([x, np.clip(alpha, 0, 1) if x.dtype == np.float32 else alpha]) if has_a else x

    if fmt == "exr":
        if cv2 is None:
            raise RuntimeError("Writing EXR needs OpenCV (cv2).")
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
        cv2.imwrite(path, np.ascontiguousarray(data), params)                 # (EXR colorspace attr: TODO OpenEXR)
        return
    if fmt in ("tif", "tiff"):
        if bit_depth == "32f":
            data = with_a(rgb.astype(np.float32))
        elif bit_depth == "8":                                              # 2026-07-03: round, not floor (kills the half-LSB bias)
            data = np.round(np.clip(with_a(rgb), 0, 1) * 255).astype(np.uint8)
        else:
            data = np.round(np.clip(with_a(rgb), 0, 1) * 65535).astype(np.uint16)
        tifffile.imwrite(path, np.ascontiguousarray(data), description=desc)
        return
    if fmt == "png":
        if bit_depth == "16":
            if cv2 is None:
                raise RuntimeError("16-bit PNG needs OpenCV (cv2).")
            bgr = np.clip(rgb, 0, 1)[..., ::-1]
            data = np.dstack([bgr, np.clip(alpha, 0, 1)]) if has_a else bgr
            cv2.imwrite(path, np.ascontiguousarray(np.round(data * 65535).astype(np.uint16)))   # 2026-07-03: round, not floor
            return
        arr8 = np.round(np.clip(rgb, 0, 1) * 255).astype(np.uint8)
        if has_a:
            im = Image.fromarray(np.dstack([arr8, np.round(np.clip(alpha, 0, 1) * 255).astype(np.uint8)]), "RGBA")
        else:
            im = Image.fromarray(arr8, "RGB")
        info = None
        if colorspace:
            from PIL import PngImagePlugin
            info = PngImagePlugin.PngInfo()
            info.add_text("colorspace", colorspace)
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
    cs = (output_colorspace or "").lower()
    if "2100" in cs or "pq" in cs:
        prim, trc, spc = "bt2020", "smpte2084", "bt2020nc"          # HDR
    elif "1886" in cs or "rec.709" in cs or "rec709" in cs:
        prim, trc, spc = "bt709", "bt709", "bt709"                  # broadcast 2.4
    else:                                                           # sRGB - Display default (WYSIWYG)
        prim, trc, spc = "bt709", _SRGB_TRC, "bt709"
    vf = f"setparams=color_primaries={prim}:color_trc={trc}:colorspace={spc}:range=tv"
    return ["-vf", vf, "-color_primaries", prim, "-color_trc", trc, "-colorspace", spc,
            "-color_range", "tv", "-movflags", "+write_colr"]


def save_video(arr01, out_path, codec, fps, output_colorspace=None):
    _require_ffmpeg()
    n, h, w, _ = arr01.shape
    enc = {
        "prores_4444": ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuv444p12le"],
        "prores_422hq": ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"],
        "prores_422": ["-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le"],
        "dnxhr_hq": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"],
        "h264": ["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p"],
        "hevc": ["-c:v", "libx265", "-crf", "18", "-pix_fmt", "yuv420p"],
    }.get(codec, ["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p"])
    cmd = [_FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb48le",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", *enc, *_video_color_tags(output_colorspace),
           "-r", str(fps), out_path]
    proc = subprocess.run(cmd, input=(np.clip(arr01, 0, 1) * 65535).astype("<u2").tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg encode failed: {proc.stderr.decode('utf-8', 'ignore')[:300]}")
    return out_path


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


def _lut_rgba8(in_cs, out_cs, size=33, raw=False):
    """Bake the in_cs -> out_cs transform into an N x N x N RGBA8 3D LUT for the OCIO Read WebGL video
    viewport. The browser plays the raw <video> and the shader samples this LUT, so a moving video reacts
    to a colorspace change (the browser cannot apply OCIO to a <video> itself). Domain is [0,1]^3 (browser
    decode is display-referred 8-bit); output is clamped to [0,1] so the texture is 8-bit and always
    linear-filterable (no float-texture extension needed). Data is laid out for WebGL texImage3D: R (x)
    varies fastest, then G (y), then B (z). raw, in==out, or OCIO-unavailable returns the identity ramp.
    Returns (n, bytes)."""
    import numpy as np
    n = int(size)
    lin = np.linspace(0.0, 1.0, n, dtype=np.float32)
    bb, gg, rr = np.meshgrid(lin, lin, lin, indexing="ij")   # C-order flatten -> index ((b*n+g)*n+r), r fastest
    grid = np.stack([rr, gg, bb], axis=-1).reshape(-1, 3).astype(np.float32)   # [n^3, 3] identity rgb
    if not raw and in_cs and out_cs and in_cs != out_cs:
        try:
            import torch
            t = torch.from_numpy(np.ascontiguousarray(grid[None, :, None, :]))   # [1, n^3, 1, 3]
            grid = _convert(t, in_cs, out_cs)[0, :, 0, :].contiguous().numpy()
        except RuntimeError:
            pass   # OCIO lib/config unavailable -> identity passthrough (same as _convert / _ocio_thumb)
    rgba = np.empty((grid.shape[0], 4), np.uint8)
    rgba[:, :3] = (np.clip(grid, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    rgba[:, 3] = 255
    return n, rgba.tobytes()


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
            "source": ("STRING", {"default": "", "tooltip": r"Path to a still / sequence folder / frame / video, ANYWHERE on disk (absolute like D:\shots\LeftGirl.v01, or relative to the ComfyUI input folder). Use the browse button. A folder or one frame of a sequence -> the whole sequence (see frame_mode)."}),
            "frame_mode": (["auto", "single", "sequence", "video"], {"default": "auto",
                           "tooltip": "How to read a selected frame (Nuke's 'grab sequence'). auto: numbered file with siblings -> whole sequence; single: just this file; sequence: force-collapse its siblings; video: a movie clip (auto-detected by extension). A folder is always a sequence; a video is always its full clip. The front end auto-sets this to the detected kind on load."}),
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

    RETURN_TYPES = ("IMAGE", "MASK", "FLOAT", "STRING", "VIDEO")
    RETURN_NAMES = ("image/sequence/video", "alpha", "fps", "info", "video")   # VIDEO appended (index 4) so the frame batch can feed VIDEO-typed edit/save nodes; indices 0-3 unchanged
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
        return (rgb, mask, out_fps, txt, _make_video(rgb, out_fps))   # VIDEO from the SAME color-managed batch + fps


_STILL_EXT = {"exr": "exr", "tiff": "tif", "png": "png", "jpeg": "jpg"}


# Short, recognizable filename tags for the common OCIO / ACES colorspaces: keep the CORE token, drop the
# descriptive tail (" - Display", "Rec.1886 ...", camera/EI suffixes). Most specific token first.
_CS_TAG_RULES = [
    ("acescct", "acescct"), ("acescc", "acescc"), ("acescg", "acescg"), ("aces2065", "aces2065"),
    ("logc", "logc"), ("canon log", "clog"), ("clog", "clog"), ("slog", "slog"), ("v-log", "vlog"), ("vlog", "vlog"),
    ("rec.2020", "rec2020"), ("rec2020", "rec2020"),
    ("rec.709", "rec709"), ("rec709", "rec709"), (" 709", "rec709"),
    ("display p3", "p3"), ("p3-d", "p3"), ("p3", "p3"),
    ("srgb", "srgb"),
    ("linear", "linear"),
]

def _cs_tag(name):
    """Colorspace name -> short filename token: 'sRGB - Display' -> 'srgb', 'Rec.1886 Rec.709 - Display' -> 'rec709',
    'ARRI LogC3 (EI800)' -> 'logc', 'ACEScg' -> 'acescg'. Unknown names fall back to a trimmed sanitize."""
    low = (name or "").lower()
    for needle, tag in _CS_TAG_RULES:
        if needle in low:
            return tag
    return re.sub(r"[^a-z0-9]+", "_", low).strip("_")[:24]


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
        ext = ".mov" if str(video_codec).startswith(("prores", "dnxhr")) else ".mp4"
        return [os.path.join(folder, stem + ext)]
    if container == "still image":
        ext = _STILL_EXT[still_format]
        if still_frame is not None:                                        # a frame grabbed from a seq/video -> stamp its source frame number
            return [os.path.join(folder, f"{stem}.{int(still_frame):04d}.{ext}")]
        return [os.path.join(folder, f"{stem}.{ext}")]
    ext = _STILL_EXT[still_format]                                          # sequence: 4-digit numbered frames
    sn = int(start_number)
    return [os.path.join(folder, f"{stem}.{sn + i:04d}.{ext}") for i in range(max(1, int(count)))]


class OCIOWrite:
    """Color-manage an IMAGE batch and write it (Nuke: Write).

    container: still image (one frame), sequence (numbered frames), or video.
    'from_colorspace' is ComfyUI's working space (default sRGB - Display); 'output_colorspace' is the file's
    space, and the format picks the right default (EXR -> ACEScg, PNG/TIFF/JPEG -> sRGB). Give a folder + a
    name; the rest is added automatically:
        still image    -> <folder>/<name>.<ext>
        sequence       -> <folder>/<name>.<start_number..>.<ext>   (4-digit, re-based to start_number)
        video          -> <folder>/<name>.mov (ProRes/DNxHR) or .mp4 (h264/hevc)
    bit_depth is per format (JPEG 8; PNG 8/16; TIFF 8/16/32f; EXR 16f/32f). The node preview shows the first
    written frame in its output colorspace, so a wrong colorspace pick is visible at a glance."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "images": ("IMAGE",),
            "profile": (["none", "auto", "LTX 2.3 HDR", "LumiPic LogC3 (Flux/Qwen)", "LumiPic V10 LogC4",
                        "Seedance 4K 10-bit"],
                        {"default": "none",
                         "tooltip": "HDR source preset. Sets from/output colorspace, forces EXR 16f, and (LumiPic) decodes the log curve inside Write. 'auto' detects the upstream source in the front-end (LTX reliably; LumiPic best-effort). Manual colorspace edits still win. Seedance is a placeholder (pending)."}),
            "from_colorspace": _cs_combo(WORKING),
            "output_colorspace": _cs_combo("ACEScg"),
            "container": (["still image", "sequence", "video"], {"default": "sequence"}),
            "still_format": (["exr", "tiff", "png", "jpeg"], {"default": "exr",
                             "tooltip": "Used for still image / sequence (hidden for video)."}),
            "video_codec": (["prores_4444", "prores_422hq", "prores_422", "dnxhr_hq", "h264", "hevc"],
                            {"default": "prores_4444", "tooltip": "Used for video (hidden otherwise)."}),
            "bit_depth": (["16f", "32f", "16", "8"], {"default": "16f",
                          "tooltip": "Per format: JPEG 8; PNG 8/16; TIFF 8/16/32f; EXR 16f/32f. The list narrows to the chosen format."}),
            "compression": (["zip", "zips", "piz", "pxr24", "dwaa", "dwab", "rle", "none"], {"default": "zip",
                            "tooltip": "EXR compression (Nuke Write style). ZIP / ZIPS = lossless (default). PIZ = lossless, good for grain. DWAA / DWAB = smaller, lossy. Applies to EXR only."}),
            "auto_range": ("BOOLEAN", {"default": True,
                           "tooltip": "ON: first_frame / last_frame / start_number / fps are pulled automatically from the OCIO Read at the other end of the wire (through any number of nodes). Editing them by hand turns this OFF; turn it back ON to re-detect."}),
            "first_frame": ("INT", {"default": 1, "min": 0, "max": 100000000,
                            "tooltip": "still image: WHICH frame to save. sequence/video: first frame to write (frame numbers, auto-filled from the source, e.g. 86)."}),
            "last_frame": ("INT", {"default": 0, "min": 0, "max": 100000000,
                           "tooltip": "sequence/video: last frame to write (0 = to the end; auto-filled from the source, e.g. 97). Ignored for a still image."}),
            "start_number": ("INT", {"default": 1, "min": 0, "max": 100000000,
                             "tooltip": "OUTPUT file numbering start (auto-filled to the source's first frame, e.g. 86; set 1 for 0001..). This is the re-base, NOT retime."}),
            "source_start": ("INT", {"default": 1, "min": 0, "max": 100000000,
                             "tooltip": "(auto) the source's first frame number, used to map first_frame/last_frame to the batch. Set by the wire."}),
            "raw_data": ("BOOLEAN", {"default": False,
                         "tooltip": "Nuke 'Raw Data': write the pixels as-is, skipping the from->out colorspace conversion."}),
            "colorspace_in_name": ("BOOLEAN", {"default": True,
                                    "tooltip": "Put the output colorspace in the file name, before the frame number: name_acescg.0001.exr. Uses the sanitized output_colorspace (or 'raw' when Raw Data is on)."}),
            "output_folder": ("STRING", {"default": "", "tooltip": "Server folder. Empty = ComfyUI output dir. Relative = under it. Use the browse button."}),
            "filename": ("STRING", {"default": "ocio_out", "tooltip": "Base name. Numbering / extension are added automatically."}),
            "auto_colorspace": ("BOOLEAN", {"default": True,
                                 "tooltip": "When the input is wired from LTX's LTXVHDRDecodePostprocess (SDR->HDR), auto-set from_colorspace = 'Linear Rec.709 (sRGB)' and output_colorspace = 'ACEScg', so you do not have to. Editing the colorspaces by hand still wins. Front-end only."}),
        }, "optional": {
            "alpha": ("MASK", {"tooltip": "Optional alpha channel -> RGBA (EXR / TIFF / PNG; ignored for JPEG). Wire OCIO Read's alpha output, or any MASK."}),
            "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0, "step": 0.001,
                              "tooltip": "Video frame rate. Wire OCIO Read's fps output here to carry the source rate."}),
            "render_nonce": ("STRING", {"default": "",
                             "tooltip": "(internal, hidden) The Render button bumps this so a repeat render to the SAME path actually re-writes - ComfyUI would otherwise cache an identical Write and skip it (no file written on the 2nd click). STRING so a blank value can never fail validation."}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("path",)
    FUNCTION = "write"
    OUTPUT_NODE = True
    CATEGORY = "OCIO"

    def _resolve_folder(self, output_folder):
        root = folder_paths.get_output_directory() if folder_paths else os.getcwd()
        if not output_folder.strip():
            return root
        return output_folder if os.path.isabs(output_folder) else os.path.join(root, output_folder)

    def write(self, images, profile, from_colorspace, output_colorspace, container, still_format, video_codec,
              bit_depth, auto_range, first_frame, last_frame, start_number, source_start, raw_data,
              output_folder, filename, colorspace_in_name=True, auto_colorspace=True, compression="zip",
              alpha=None, fps=24.0, render_nonce=""):   # render_nonce: cache-buster, bumped by the Render button so a repeat write happens (see INPUT_TYPES)
        _LOG_PROFILES = {"LumiPic LogC3 (Flux/Qwen)": _logc3_to_lin, "LumiPic V10 LogC4": _logc4_to_lin}
        if profile in _LOG_PROFILES and not raw_data:
            arr_lin = images.detach().cpu().numpy().astype(np.float32).copy()
            arr_lin[..., :3] = _LOG_PROFILES[profile](arr_lin[..., :3])   # log_to_lin, RGB only; alpha untouched
            images = torch.from_numpy(arr_lin).to(images.device, images.dtype)
            from_colorspace = "Linear Rec.709 (sRGB)"
            output_colorspace = "ACEScg"
        elif profile == "LTX 2.3 HDR" and not raw_data:
            from_colorspace = "Linear Rec.709 (sRGB)"
            output_colorspace = "ACEScg"
        # "Seedance 4K 10-bit" and "none"/"auto": no backend mapping - auto is resolved front-end, Seedance is
        # a pending placeholder (do not invent a colorspace mapping for it).
        if profile in ("LTX 2.3 HDR", "LumiPic LogC3 (Flux/Qwen)", "LumiPic V10 LogC4") and not raw_data \
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

        if container == "still image":
            idx = min(max(0, first_frame - base), n - 1)                   # frame number -> batch index
            # a still grabbed from a sequence / video (n>1) stamps its SOURCE frame number in the name
            # (name_cs.0039.png, not name_cs.png); a genuine single image (n==1) keeps the plain name.
            saved = _wp(1, still_frame=(first_frame if n > 1 else None))[0]
            _save_still(saved, arr[idx], still_format, bit_depth, alpha_of(a_arr, idx, arr[idx]), cs, compression)
            count, preview = 1, arr[idx]
        else:
            s = max(0, first_frame - base)                                 # frame numbers -> batch sub-range
            e = (last_frame - base + 1) if (last_frame and last_frame >= first_frame) else n
            sub, sub_a = arr[s:e], (a_arr[s:e] if a_arr is not None else None)
            if sub.shape[0] == 0:
                raise RuntimeError(f"nothing in write range [{first_frame}-{last_frame}] (input has {n} frame(s))")
            if container == "video":
                saved = _wp(1)[0]
                save_video(sub, saved, video_codec, float(fps) if fps and fps > 0 else 24.0,
                           None if raw_data else output_colorspace)
            else:                                                          # sequence
                paths = _wp(sub.shape[0])
                for i in range(sub.shape[0]):
                    _save_still(paths[i], sub[i], still_format, bit_depth, alpha_of(sub_a, i, sub[i]), cs, compression)
                saved = paths[0]
            count, preview = sub.shape[0], sub[0]

        return {"ui": {"images": self._preview(preview),
                       "ocio": [("raw" if raw_data else f"{from_colorspace} -> {output_colorspace}")],
                       "count": [str(count)], "saved": [os.path.basename(saved)]},
                "result": (saved,)}

    def _preview(self, frame0):
        """First written frame, shown naively in its output colorspace (a wrong pick looks visibly wrong)."""
        return _save_preview_png(frame0, "ocio_write_preview.png")


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
        rgb = arr[i]
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
    node OUTPUTS the batch converted input_colorspace -> output_colorspace and trimmed to [start_frame,
    end_frame]. Exposure is a viewing tool only - it never touches the output. A still is N=1, a sequence /
    video is N>1; the viewer scales to the frame size either way."""

    @classmethod
    def INPUT_TYPES(cls):
        cs = _colorspace_names()
        return {
            "required": {
                "input_colorspace": _combo_or_string(cs, WORKING, "The colorspace the incoming batch is in (front-end auto-guesses ACEScg for HDR / >1 data, else sRGB - Display)."),
                "output_colorspace": _combo_or_string(cs, WORKING, "The colorspace to convert the OUTPUT to."),
                "raw_data": ("BOOLEAN", {"default": False, "label_on": "raw (no convert)", "label_off": "color-managed",
                                         "tooltip": "Pass pixels through untouched (no colorspace convert on the output)."}),
                "start_frame": ("INT", {"default": 0, "min": 0, "max": 100000000,
                                        "tooltip": "First batch index to OUTPUT (0-based). The viewer always shows the whole input."}),
                "end_frame": ("INT", {"default": 0, "min": 0, "max": 100000000,
                                      "tooltip": "Last batch index to output (0 = through the end)."}),
                "fps": ("FLOAT", {"default": 24.0, "min": 0.0, "max": 1000.0, "step": 0.001,
                                  "tooltip": "Playback rate for the viewer + the fps output."}),
            },
            "optional": {
                "images": ("IMAGE", {"tooltip": "Any IMAGE batch - still (N=1), sequence or video (N>1). Optional: connect a Load Video node to 'video' instead to stream a movie without materializing it."}),
                "video": ("VIDEO", {"tooltip": "Connect a Load Video node here to STREAM a movie in the viewport (WebCodecs decode-on-demand, no whole-clip materialization). Takes priority over 'images' for what the viewer shows."}),
                "alpha": ("MASK", {"tooltip": "Optional alpha to view / carry through."}),
                "base": ("STRING", {"default": "0",
                                    "tooltip": "Source first-frame number, set by the front end from the upstream OCIO Read (hidden). start_frame/end_frame are SOURCE frame numbers; the node subtracts base to get 0-based batch indices. 0 = already 0-based indices. STRING (not INT) so a blank value can NEVER fail prompt validation."}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    # 2026-07-04 (owner): the Player is a pure VIEWER - INPUT ONLY, NO outputs (like Preview Image). It branches
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
