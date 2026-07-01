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
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import glob
import re
import shutil
import subprocess

import numpy as np
import torch

try:
    import cv2
except Exception:
    cv2 = None
try:
    import tifffile
except Exception:
    tifffile = None
from PIL import Image

from .nodes import (_apply_processor, _colorspace_names, _combo_or_string,
                    _input_dir, _require_ocio, _resolve_config, _scan_files)

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
    if a is None:                                   # png / jpg / bmp / dpx fallback via PIL
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
    """`.../name.0132.exr` -> (prefix='.../name.', frame=132, pad=4, ext='.exr'); None if no trailing number.
    Handles dot or underscore separators (name.0132.ext, name_0132.ext, name0132.ext)."""
    d, base = os.path.dirname(path), os.path.basename(path)
    stem, ext = os.path.splitext(base)
    m = re.match(r"^(.*?)(\d+)$", stem)
    if not m:
        return None
    prefix = os.path.join(d, m.group(1)) if d else m.group(1)
    return (prefix, int(m.group(2)), len(m.group(2)), ext)


def _frame_num(path):
    sp = _split_frame(path)
    return sp[1] if sp else 0


def _sequence_siblings(path):
    """Every frame sharing the same prefix + extension as `path`, sorted by frame number (Nuke: grab the
    sequence from one selected frame)."""
    sp = _split_frame(path)
    if not sp:
        return []
    prefix, _, _, ext = sp
    out = [c for c in glob.glob(prefix + "*" + ext)
           if (_split_frame(c) or (None,))[0] == prefix and os.path.splitext(c)[1].lower() == ext.lower()]
    return sorted(out, key=_frame_num)


def _seq_label(files):
    """Nuke-style 'name.####.ext [first-last]'."""
    sp = _split_frame(files[0])
    if not sp:
        return os.path.basename(files[0])
    prefix, n0, pad, ext = sp
    return f"{os.path.basename(prefix)}{'#' * pad}{ext} [{n0}-{_frame_num(files[-1])}]"


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


def _read_video(path, frame_start, frame_count):
    """Decode a video -> float32 RGB [N,H,W,3] (0..1) via ffmpeg piping 16-bit rgb48le."""
    _require_ffmpeg()
    probe = subprocess.run([_FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height,nb_frames,codec_name,r_frame_rate,avg_frame_rate",
                            "-of", "default=noprint_wrappers=1", path], capture_output=True, text=True)
    info = dict(line.split("=", 1) for line in probe.stdout.strip().splitlines() if "=" in line)
    w, h = int(info.get("width", 0) or 0), int(info.get("height", 0) or 0)
    if not (w and h):
        raise RuntimeError(f"ffprobe could not read {path}: {probe.stderr[:200]}")
    cmd = [_FFMPEG, "-v", "error", "-i", path]
    if frame_count > 0:
        cmd += ["-frames:v", str(frame_start + frame_count)]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {proc.stderr.decode('utf-8', 'ignore')[:300]}")
    buf = np.frombuffer(proc.stdout, dtype="<u2")
    n = buf.size // (w * h * 3)
    if n == 0:
        raise RuntimeError("ffmpeg returned no frames")
    arr = buf[: n * w * h * 3].reshape(n, h, w, 3).astype(np.float32) / 65535.0
    if frame_start:
        arr = arr[frame_start:]
    info["fps"] = _video_fps(info)
    return arr, info


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
        count = (end_frame - start_frame + 1) if end_frame >= start_frame and end_frame > 0 else 0
        arr, info = _read_video(s, start_frame, count)
        alpha = np.ones((*arr.shape[:3], 1), np.float32)     # video has no alpha -> opaque
        arr = np.concatenate([arr, alpha], axis=-1)
        info["kind"] = "video"
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
        return np.stack(frames, 0), {"kind": "sequence", "count": len(frames), "fps": 0.0,
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
                             "stream=nb_frames,r_frame_rate", "-of", "default=noprint_wrappers=1", s],
                            capture_output=True, text=True)
        info = dict(line.split("=", 1) for line in pr.stdout.strip().splitlines() if "=" in line)
        nb = int(info.get("nb_frames", 0) or 0)
        return {"kind": "video", "start": 0, "end": max(0, nb - 1), "count": nb, "fps": _video_fps(info)}
    files = _frame_files(s)
    if not files and os.path.isfile(s):
        files = _sequence_siblings(s)
    if len(files) >= 2:
        present = sorted(_frame_num(f) for f in files)
        lo, hi = present[0], present[-1]
        pset = set(present)
        missing = [f for f in range(lo, hi + 1) if f not in pset]
        return {"kind": "sequence", "start": lo, "end": hi, "count": len(files), "fps": 0.0,
                "orig_start": lo, "orig_end": hi, "missing": _collapse_ranges(missing),
                "missing_count": len(missing)}
    return {"kind": "still", "start": 0, "end": 0, "count": 1, "fps": 0.0}


# --------------------------------------------------------------------------- saving

def _save_still(path, rgb, fmt, bit_depth, alpha=None, colorspace=None):
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
        bgr = rgb[..., ::-1].astype(np.float32)
        data = np.dstack([bgr, alpha.astype(np.float32)]) if has_a else bgr   # BGRA for cv2
        cv2.imwrite(path, np.ascontiguousarray(data), params)                 # (EXR colorspace attr: TODO OpenEXR)
        return
    if fmt in ("tif", "tiff"):
        if bit_depth == "32f":
            data = with_a(rgb.astype(np.float32))
        elif bit_depth == "8":
            data = (np.clip(with_a(rgb), 0, 1) * 255).astype(np.uint8)
        else:
            data = (np.clip(with_a(rgb), 0, 1) * 65535).astype(np.uint16)
        tifffile.imwrite(path, np.ascontiguousarray(data), description=desc)
        return
    if fmt == "png":
        if bit_depth == "16":
            if cv2 is None:
                raise RuntimeError("16-bit PNG needs OpenCV (cv2).")
            bgr = np.clip(rgb, 0, 1)[..., ::-1]
            data = np.dstack([bgr, np.clip(alpha, 0, 1)]) if has_a else bgr
            cv2.imwrite(path, np.ascontiguousarray((data * 65535).astype(np.uint16)))
            return
        arr8 = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
        if has_a:
            im = Image.fromarray(np.dstack([arr8, (np.clip(alpha, 0, 1) * 255).astype(np.uint8)]), "RGBA")
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
    im = Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8), "RGB")
    im.save(path, quality=95, **({"comment": colorspace.encode()} if colorspace else {}))


def save_video(arr01, out_path, codec, fps):
    _require_ffmpeg()
    n, h, w, _ = arr01.shape
    enc = {
        "prores_4444": ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuv444p10le"],
        "prores_422hq": ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"],
        "prores_422": ["-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le"],
        "dnxhr_hq": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"],
        "h264": ["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p"],
        "hevc": ["-c:v", "libx265", "-crf", "18", "-pix_fmt", "yuv420p"],
    }.get(codec, ["-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p"])
    cmd = [_FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb48le",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", *enc, "-r", str(fps), out_path]
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
    """OCIO convert between two colorspaces using the active (built-in ACES) config. Identity if equal."""
    if not in_cs or not out_cs or in_cs == out_cs:
        return image
    _require_ocio()
    cfg = _resolve_config("")
    if cfg is None:
        return image
    return _apply_processor(image, cfg.getProcessor(in_cs, out_cs))


def _cs_combo(default):
    return _combo_or_string(_colorspace_names(), default, "Colorspace from the active OCIO (ACES) config.")


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
            "frame_mode": (["auto", "single", "sequence"], {"default": "auto",
                           "tooltip": "How to read a selected frame (Nuke's 'grab sequence'). auto: numbered file with siblings -> whole sequence; single: just this file; sequence: force-collapse its siblings. A folder is always a sequence; a video is always its full clip."}),
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

    RETURN_TYPES = ("IMAGE", "MASK", "FLOAT", "STRING")
    RETURN_NAMES = ("image/video", "alpha", "fps", "info")
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
        return (rgb, mask, out_fps, txt)


_STILL_EXT = {"exr": "exr", "tiff": "tif", "png": "png", "jpeg": "jpg"}


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
            "from_colorspace": _cs_combo(WORKING),
            "output_colorspace": _cs_combo("ACEScg"),
            "container": (["still image", "sequence", "video"], {"default": "sequence"}),
            "still_format": (["exr", "tiff", "png", "jpeg"], {"default": "exr",
                             "tooltip": "Used for still image / sequence (hidden for video)."}),
            "video_codec": (["prores_4444", "prores_422hq", "prores_422", "dnxhr_hq", "h264", "hevc"],
                            {"default": "prores_4444", "tooltip": "Used for video (hidden otherwise)."}),
            "bit_depth": (["16f", "32f", "16", "8"], {"default": "16f",
                          "tooltip": "Per format: JPEG 8; PNG 8/16; TIFF 8/16/32f; EXR 16f/32f. The list narrows to the chosen format."}),
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
            "output_folder": ("STRING", {"default": "", "tooltip": "Server folder. Empty = ComfyUI output dir. Relative = under it. Use the browse button."}),
            "filename": ("STRING", {"default": "ocio_out", "tooltip": "Base name. Numbering / extension are added automatically."}),
        }, "optional": {
            "alpha": ("MASK", {"tooltip": "Optional alpha channel -> RGBA (EXR / TIFF / PNG; ignored for JPEG). Wire OCIO Read's alpha output, or any MASK."}),
            "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0, "step": 0.001,
                              "tooltip": "Video frame rate. Wire OCIO Read's fps output here to carry the source rate."}),
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

    def write(self, images, from_colorspace, output_colorspace, container, still_format, video_codec,
              bit_depth, auto_range, first_frame, last_frame, start_number, source_start, raw_data,
              output_folder, filename, alpha=None, fps=24.0):
        img = images if raw_data else _convert(images, from_colorspace, output_colorspace)
        arr = img.detach().cpu().numpy().astype(np.float32)
        a_arr = None
        if alpha is not None:
            a = alpha.detach().cpu().numpy().astype(np.float32)
            a_arr = a if a.ndim == 3 else a[None]                          # [N,H,W]
        n = arr.shape[0]
        folder = self._resolve_folder(output_folder)
        os.makedirs(folder, exist_ok=True)
        name = filename.strip() or "ocio_out"
        cs = None if raw_data else output_colorspace                       # colorspace stamped in metadata
        base = source_start if source_start else 1                         # logical number of the first batch frame

        def alpha_of(src_a, i, ref):
            if src_a is None:
                return None
            fr = src_a[min(i, src_a.shape[0] - 1)]
            return fr if fr.shape[:2] == ref.shape[:2] else None

        if container == "still image":
            ext = _STILL_EXT[still_format]
            idx = min(max(0, first_frame - base), n - 1)                   # frame number -> batch index
            saved = os.path.join(folder, f"{name}.{ext}")
            _save_still(saved, arr[idx], still_format, bit_depth, alpha_of(a_arr, idx, arr[idx]), cs)
            count, preview = 1, arr[idx]
        else:
            s = max(0, first_frame - base)                                 # frame numbers -> batch sub-range
            e = (last_frame - base + 1) if (last_frame and last_frame >= first_frame) else n
            sub, sub_a = arr[s:e], (a_arr[s:e] if a_arr is not None else None)
            if sub.shape[0] == 0:
                raise RuntimeError(f"nothing in write range [{first_frame}-{last_frame}] (input has {n} frame(s))")
            if container == "video":
                ext = ".mov" if video_codec.startswith(("prores", "dnxhr")) else ".mp4"
                saved = os.path.join(folder, name + ext)
                save_video(sub, saved, video_codec, float(fps) if fps and fps > 0 else 24.0)
            else:                                                          # sequence
                ext = _STILL_EXT[still_format]
                for i in range(sub.shape[0]):
                    _save_still(os.path.join(folder, f"{name}.{start_number + i:04d}.{ext}"),
                                sub[i], still_format, bit_depth, alpha_of(sub_a, i, sub[i]), cs)
                saved = os.path.join(folder, f"{name}.{start_number:04d}.{ext}")
            count, preview = sub.shape[0], sub[0]

        return {"ui": {"images": self._preview(preview),
                       "ocio": [("raw" if raw_data else f"{from_colorspace} -> {output_colorspace}")],
                       "count": [str(count)], "saved": [os.path.basename(saved)]},
                "result": (saved,)}

    def _preview(self, frame0):
        """First written frame, shown naively in its output colorspace (a wrong pick looks visibly wrong)."""
        if folder_paths is None:
            return []
        tdir = folder_paths.get_temp_directory()
        os.makedirs(tdir, exist_ok=True)
        fn = "ocio_write_preview.png"
        Image.fromarray((np.clip(frame0, 0, 1) * 255).astype(np.uint8)).save(os.path.join(tdir, fn))
        return [{"filename": fn, "subfolder": "", "type": "temp"}]


NODE_CLASS_MAPPINGS = {"OCIORead": OCIORead, "OCIOWrite": OCIOWrite}
NODE_DISPLAY_NAME_MAPPINGS = {"OCIORead": "OCIO Read", "OCIOWrite": "OCIO Write"}
