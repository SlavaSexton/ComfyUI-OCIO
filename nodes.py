# ComfyUI-OCIO - OpenColorIO nodes for ComfyUI, modelled on The Foundry Nuke's OCIO node set.
# @author Slava Sexton, 2026-06-30
#
# Six nodes: OCIO ColorSpace, OCIO LogConvert, OCIO Display, OCIO CDLTransform,
# OCIO FileTransform, OCIO LookTransform. All IMAGE -> IMAGE, float, HDR-safe (no clip).
#
# Pickers, not free text: colorspaces, displays, views, looks are dropdowns from the active config;
# config (.ocio) and LUT files are dropdowns of files in the ComfyUI input folder (drop a file there to
# see it). A one-press "swap" button (web/ocio_swap.js) flips in/out (ColorSpace, Look) or the operation
# (LogConvert).
#
# Implementation notes (confirmed vs inferred):
#  - LogConvert curves are published specs, verified by round-trip + black-point tests (tools/):
#    Cineon (OCIO nuke-default, black 0->0.0928, matches Nuke), ACEScct (S-2016-001), ACEScc (S-2014-003).
#  - The OpenColorIO-backed paths use the OCIO 2.x Python API (PackedImageDesc + apply), verified at runtime
#    against the built-in ACES studio config (OCIO 2.5.2). `pip install opencolorio`.

import os
import threading
from collections import OrderedDict

import numpy as np
import torch

try:
    import PyOpenColorIO as OCIO
    _HAS_OCIO = True
except Exception:
    _HAS_OCIO = False

try:
    import folder_paths  # ComfyUI path helper (present only inside ComfyUI)
except Exception:
    folder_paths = None


# --------------------------------------------------------------------------- bounded thread-safe LRU cache

class _LRUCache:
    """Small bounded LRU: OrderedDict + RLock. get() moves the hit key to the end (most-recent);
    put() evicts the oldest entry (front of the dict) once over maxsize."""

    def __init__(self, maxsize):
        self._maxsize = maxsize
        self._lock = threading.RLock()
        self._d = OrderedDict()

    def get(self, key):
        with self._lock:
            if key not in self._d:
                return None
            self._d.move_to_end(key)
            return self._d[key]

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self._maxsize:
                self._d.popitem(last=False)

    def __len__(self):
        with self._lock:
            return len(self._d)


_CONFIG_CACHE = _LRUCache(8)
_PROCESSOR_CACHE = _LRUCache(64)


def _cached_config(cache_key, build):
    """Fetch-or-build an OCIO Config for cache_key, using build() (called at most once per key)."""
    cfg = _CONFIG_CACHE.get(cache_key)
    if cfg is not None:
        return cfg
    cfg = build()
    if cfg is not None:
        _CONFIG_CACHE.put(cache_key, cfg)
    return cfg


def _cached_cpu_processor(cfg_key, tf_key, build):
    """Fetch-or-build a CPU processor for (cfg_key, tf_key), using build() -> cfg.getProcessor(...).
    build() is called at most once per distinct key; the result is cached as the CPU processor
    (getDefaultCPUProcessor already applied), so callers never call it twice."""
    key = (cfg_key, tf_key)
    cpu = _PROCESSOR_CACHE.get(key)
    if cpu is not None:
        return cpu
    processor = build()
    cpu = processor.getDefaultCPUProcessor()
    _PROCESSOR_CACHE.put(key, cpu)
    return cpu


# --------------------------------------------------------------------------- config + apply helpers

def _resolve_config_keyed(path=""):
    """Resolve an OCIO config: explicit file -> $OCIO env -> built-in ACES studio config.
    Returns (config, cache_key); (None, None) if unavailable. Cached: a real .ocio file is
    keyed on (path, mtime_ns, size) so an edited file is not silently stale; the built-in /
    $OCIO path is keyed on a fixed name."""
    if not _HAS_OCIO:
        return None, None
    if path and os.path.isfile(path):
        st = os.stat(path)
        key = ("file", path, st.st_mtime_ns, st.st_size)
        return _cached_config(key, lambda: OCIO.Config.CreateFromFile(path)), key
    if os.environ.get("OCIO"):
        try:
            key = ("env", os.environ["OCIO"])
            return _cached_config(key, lambda: OCIO.Config.CreateFromEnv()), key
        except Exception:
            pass
    for builtin in ("studio-config-latest", "cg-config-latest", "ocio://default"):
        try:
            key = ("builtin", builtin)
            return _cached_config(key, lambda b=builtin: OCIO.Config.CreateFromBuiltinConfig(b)), key
        except Exception:
            continue
    return None, None


def _resolve_config(path=""):
    """Back-compat wrapper: config only, no cache key (used where the key is not needed)."""
    return _resolve_config_keyed(path)[0]


BUILTIN = "(built-in ACES config)"


def _input_dir():
    if folder_paths:
        try:
            return folder_paths.get_input_directory()
        except Exception:
            pass
    return ""


def _scan_files(exts):
    """Relative paths of files with the given extensions under the ComfyUI input folder."""
    base = _input_dir()
    if not base or not os.path.isdir(base):
        return []
    out = []
    for root, _, files in os.walk(base):
        for f in files:
            if os.path.splitext(f)[1].lower() in exts:
                out.append(os.path.relpath(os.path.join(root, f), base).replace("\\", "/"))
    return sorted(out)


def _config_from_choice_keyed(choice):
    """Combo choice ('(built-in ...)' or a .ocio file in input) -> (OCIO Config, cache_key)."""
    if choice and choice != BUILTIN:
        p = choice if os.path.isabs(choice) else os.path.join(_input_dir(), choice)
        if os.path.isfile(p):
            st = os.stat(p)
            key = ("file", p, st.st_mtime_ns, st.st_size)
            return _cached_config(key, lambda: OCIO.Config.CreateFromFile(p)), key
    return _resolve_config_keyed("")


def _config_from_choice(choice):
    """Back-compat wrapper: config only, no cache key."""
    return _config_from_choice_keyed(choice)[0]


def _lut_path(choice):
    """Combo choice (a LUT file relative to input, or an absolute path) -> an absolute path."""
    if choice and not os.path.isabs(choice):
        p = os.path.join(_input_dir(), choice)
        if os.path.isfile(p):
            return p
    return choice


def _names(getter):
    cfg = _resolve_config("")
    if cfg is None:
        return None
    try:
        return getter(cfg)
    except Exception:
        return None


def _colorspace_names():
    return _names(lambda c: [cs.getName() for cs in c.getColorSpaces()])


def _combo_or_string(items, default, tip):
    if items:
        d = default if default in items else items[0]
        return (items, {"default": d, "tooltip": tip})
    return ("STRING", {"default": default, "multiline": False, "tooltip": tip})


def _cs_input(default):
    return _combo_or_string(_colorspace_names(), default, "Colorspace from the active config.")


def _config_input():
    return _combo_or_string([BUILTIN] + _scan_files({".ocio"}), BUILTIN,
                            "OCIO config: built-in ACES, or a .ocio file dropped in the ComfyUI input folder. Pick from the list.")


def _lut_input():
    files = _scan_files({".cube", ".3dl", ".spi1d", ".spi3d", ".csp", ".ccc", ".cdl", ".clf", ".lut"})
    if files:
        return (files, {"tooltip": "LUT file from the ComfyUI input folder (drop a .cube/.3dl/.spi1d there)."})
    return ("STRING", {"default": "", "multiline": False,
                       "tooltip": "No LUT in the input folder yet. Drop a .cube/.3dl/.spi1d there, or type a path."})


def _display_input():
    return _combo_or_string(_names(lambda c: list(c.getDisplays())), "sRGB - Display",
                            "Display device (from the config).")


def _view_input():
    def union(c):
        vs = []
        for d in c.getDisplays():
            for v in c.getViews(d):
                if v not in vs:
                    vs.append(v)
        return vs
    return _combo_or_string(_names(union), "ACES 2.0 - SDR 100 nits (Rec.709)",
                            "View transform (from the config). Must be valid for the chosen display.")


def _looks_input():
    looks = _names(lambda c: [l.getName() for l in c.getLooks()])
    items = (["(none)"] + looks) if looks else None
    return _combo_or_string(items, "(none)", "OCIO look from the config. '(none)' = no look.")


def _mix_input():
    return ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01,
                      "tooltip": "Blend with the original: 1.0 = full effect, 0.0 = bypass."})


def _blend(orig, new, mix):
    mix = float(max(0.0, min(1.0, mix)))
    if mix >= 1.0:
        return new
    if mix <= 0.0:
        return orig
    return orig * (1.0 - mix) + new * mix


def _apply_processor(img, cpu):
    """Apply an already-resolved CPU processor (OCIO.CPUProcessor) to a batched IMAGE tensor."""
    arr = img.detach().cpu().numpy().astype(np.float32)
    b, h, w, c = arr.shape
    out = arr.copy()
    for i in range(b):
        rgb = np.ascontiguousarray(out[i, :, :, :3])
        desc = OCIO.PackedImageDesc(rgb, w, h, 3)
        cpu.apply(desc)
        out[i, :, :, :3] = rgb
    return torch.from_numpy(out).to(img.device, img.dtype)


def _require_ocio():
    if not _HAS_OCIO:
        raise RuntimeError("This node needs OpenColorIO. Install it: pip install opencolorio. OCIO LogConvert runs without it.")


# --------------------------------------------------------------------------- log curves (dependency-free)

_CINEON_OFF = 10.0 ** ((95.0 - 685.0) / 300.0)  # 0.0107977...

def _lin_to_cineon(x):
    x = np.maximum(np.asarray(x, np.float64), 0.0)
    return ((685.0 + 300.0 * np.log10(x * (1.0 - _CINEON_OFF) + _CINEON_OFF)) / 1023.0).astype(np.float32)

def _cineon_to_lin(y):
    y = np.asarray(y, np.float64)
    return ((10.0 ** ((1023.0 * y - 685.0) / 300.0) - _CINEON_OFF) / (1.0 - _CINEON_OFF)).astype(np.float32)

def _lin_to_acescct(x):
    x = np.asarray(x, np.float64)
    return np.where(x <= 0.0078125, 10.5402377416545 * x + 0.0729055341958355,
                    (np.log2(np.maximum(x, 1e-10)) + 9.72) / 17.52).astype(np.float32)

def _acescct_to_lin(y):
    y = np.asarray(y, np.float64)
    return np.where(y <= 0.155251141552511, (y - 0.0729055341958355) / 10.5402377416545,
                    np.exp2(y * 17.52 - 9.72)).astype(np.float32)

def _lin_to_acescc(x):
    x = np.asarray(x, np.float64)
    return np.where(x <= 0.0, (np.log2(2.0 ** -16) + 9.72) / 17.52,
            np.where(x < 2.0 ** -15, (np.log2(np.maximum(2.0 ** -16 + x * 0.5, 2.0 ** -16)) + 9.72) / 17.52,
                     (np.log2(np.maximum(x, 2.0 ** -16)) + 9.72) / 17.52)).astype(np.float32)

def _acescc_to_lin(y):
    y = np.asarray(y, np.float64)
    return np.where(y < (9.72 - 15.0) / 17.52, (np.exp2(y * 17.52 - 9.72) - 2.0 ** -16) * 2.0,
            np.where(y < (np.log2(65504.0) + 9.72) / 17.52, np.exp2(y * 17.52 - 9.72), 65504.0)).astype(np.float32)

# ARRI LogC3 (EI 800) - the tone curve LTX-2's HDR IC-LoRA uses to fit HDR into [0,1]. Curve ONLY: the LTX data
# stays in Rec.709 primaries, so pair this with a Rec.709 -> ACEScg colorspace step, NOT the config's "ARRI LogC3"
# (that one assumes ARRI Wide Gamut primaries and would shift the gamut). Published ARRI constants.
_LC3_A, _LC3_B, _LC3_C, _LC3_D = 5.555556, 0.052272, 0.247190, 0.385537
_LC3_E, _LC3_F, _LC3_CUT = 5.367655, 0.092809, 0.010591

def _lin_to_logc3(x):
    x = np.maximum(np.asarray(x, np.float64), 0.0)
    return np.where(x >= _LC3_CUT, _LC3_C * np.log10(_LC3_A * x + _LC3_B) + _LC3_D,
                    _LC3_E * x + _LC3_F).astype(np.float32)

def _logc3_to_lin(y):
    y = np.asarray(y, np.float64)
    cut_log = _LC3_E * _LC3_CUT + _LC3_F
    return np.where(y >= cut_log, (10.0 ** ((y - _LC3_D) / _LC3_C) - _LC3_B) / _LC3_A,
                    (y - _LC3_F) / _LC3_E).astype(np.float32)

# ARRI LogC4 - the wider-headroom successor to LogC3 (ALEXA 35 era): ceiling ~469.8 linear (~11.3 stops over
# mid-gray, ~3 stops more highlight room than LogC3 EI800), the curve LumiPic's V10 *_logc4_* HDR LoRA family
# targets. Published ARRI LogC4 constants (spec, 1 May 2022). Like logc3 this is the CURVE only: the plate
# stays in Rec.709 primaries, so pair log_to_lin with a Rec.709 -> ACEScg colorspace step, NOT the config's
# "ARRI LogC4" colorspace (that assumes ARRI Wide Gamut 4). Piecewise split at the spec point (encoded V=0,
# linear toe below / log above), so the whole [0,1] LoRA range decodes through the log branch and round-trips
# exactly; matches oumad ComfyUI_Gear's LogC4 Decode across the log region (V >= c: all mids/highlights/ceiling).
_LC4_A = (2.0 ** 18 - 16) / 117.45          # 2231.8263...
_LC4_B = (1023.0 - 95.0) / 1023.0           # 0.9071358...
_LC4_C = 95.0 / 1023.0                       # 0.0928641...
_LC4_S = (7.0 * np.log(2.0) * 2.0 ** (7.0 - 14.0 * _LC4_C / _LC4_B)) / (_LC4_A * _LC4_B)
_LC4_T = (2.0 ** (14.0 * (-_LC4_C / _LC4_B) + 6.0) - 64.0) / _LC4_A   # -0.018060... (encoded 0 -> this linear)

def _lin_to_logc4(x):
    x = np.asarray(x, np.float64)
    xc = np.maximum(x, _LC4_T)  # guard the discarded log branch from log2(<=0)
    return np.where(x >= _LC4_T, (np.log2(_LC4_A * xc + 64.0) - 6.0) / 14.0 * _LC4_B + _LC4_C,
                    (x - _LC4_T) / _LC4_S).astype(np.float32)

def _logc4_to_lin(y):
    y = np.asarray(y, np.float64)
    return np.where(y >= 0.0, (2.0 ** (14.0 * (y - _LC4_C) / _LC4_B + 6.0) - 64.0) / _LC4_A,
                    y * _LC4_S + _LC4_T).astype(np.float32)

# Sony S-Log3, the Sony native camera log curve (VENICE, F5/F55/F65, FX-series): tone curve only, pair with
# S-Gamut3.Cine/S-Gamut3 -> ACEScg for the gamut. Constants confirmed from Sony's own primary spec: "Technical
# Summary for S-Gamut3.Cine/S-Log3 and S-Gamut3/S-Log3" (Sony Corporation), Appendix "S-Log3 Formula". Linear
# cut 0.01125, log branch offset 0.01, gain 261.5/1023, log-domain cut at code 171.2102946929/1023. Confirmed
# anchors from the same doc's "S-Log3 10bit code values" table: 0% black -> CV 95 (0.0928...), 18% grey -> CV
# 420 (0.41056...), 90% white -> CV ~598 (0.58445...); this code matches that table to 5 decimal places.
_SL3_CUT_LIN = 0.01125000
_SL3_B95, _SL3_B171 = 95.0, 171.2102946929
_SL3_CUT_LOG = _SL3_B171 / 1023.0

def _lin_to_slog3(x):
    x = np.asarray(x, np.float64)
    xc = np.maximum(x + 0.01, 1e-10)  # guard the discarded log branch from log10(<=0)
    return np.where(x >= _SL3_CUT_LIN, (420.0 + np.log10(xc / 0.19) * 261.5) / 1023.0,
                     (x * (_SL3_B171 - _SL3_B95) / _SL3_CUT_LIN + _SL3_B95) / 1023.0).astype(np.float32)

def _slog3_to_lin(y):
    y = np.asarray(y, np.float64)
    return np.where(y >= _SL3_CUT_LOG, (10.0 ** ((y * 1023.0 - 420.0) / 261.5)) * 0.19 - 0.01,
                     (y * 1023.0 - _SL3_B95) * _SL3_CUT_LIN / (_SL3_B171 - _SL3_B95)).astype(np.float32)

# Panasonic V-Log, the VariCam/S1H/GH-series native camera log curve: tone curve only, pair with V-Gamut ->
# ACEScg for the gamut. Constants confirmed from Panasonic's own primary spec: "V-Log/V-Gamut REFERENCE
# MANUAL" (Panasonic Corporation, Rev.1.0, November 28 2014), section 3 "V-Log Formula". Linear cut 0.01,
# b=0.00873, c=0.241514, d=0.598206; log-domain cut2=0.181 (= c*log10(cut1+b)+d, the encoded value at the
# linear/log crossover). Confirmed anchors from the same doc's Fig.2.2 "V-Log Code Value" table (10bit code /
# 1023): 0% black -> CV 128 (0.12512...), 18% grey -> CV 433 (0.42327...), 90% white -> CV 602 (0.58847...).
_VLOG_B, _VLOG_C, _VLOG_D = 0.00873, 0.241514, 0.598206
_VLOG_CUT1 = 0.01
_VLOG_CUT2 = _VLOG_C * np.log10(_VLOG_CUT1 + _VLOG_B) + _VLOG_D  # 0.18098... (spec states 0.181)

def _lin_to_vlog(x):
    x = np.asarray(x, np.float64)
    xc = np.maximum(x + _VLOG_B, 1e-10)  # guard the discarded log branch from log10(<=0)
    return np.where(x >= _VLOG_CUT1, _VLOG_C * np.log10(xc) + _VLOG_D,
                    5.6 * x + 0.125).astype(np.float32)

def _vlog_to_lin(y):
    y = np.asarray(y, np.float64)
    return np.where(y >= _VLOG_CUT2, 10.0 ** ((y - _VLOG_D) / _VLOG_C) - _VLOG_B,
                    (y - 0.125) / 5.6).astype(np.float32)

# Canon Log 3, the Cinema EOS native camera log curve (C300 Mark II onward): tone curve only, pair with Cinema
# Gamut -> ACEScg for the gamut. Constants confirmed from Canon's own primary spec: "Canon Log Gamma Curves"
# white paper (Canon USA, November 1 2018), Appendix [3] "Canon Log 3 (Full %)". Three-piece curve: negative
# log branch (x < -0.014), a linear mid segment (-0.014 <= x <= 0.014) that keeps the curve C1-continuous
# across both breakpoints, and a positive log branch (x > 0.014). Anchor: this code's x is direct scene-linear
# reflectance (0.18 = 18% grey, matching every other curve in this file); Canon's own table indexes by "Scene
# Linear %" with reflection = Scene Linear * 0.9, so Canon's own tabulated "18% Grey" row (input 0.20 in their
# convention) reads CV 351 / 0.343 - confirmed from the same doc's Appendix [4] table. At x=0.18 direct
# (this code's convention) the value is ~0.3298, inferred consistent with the same formula, not separately
# tabulated by Canon.
_CL3_NEG_A, _CL3_NEG_S = 0.36726845, 14.98325
_CL3_NEG_OFF = 0.12783901
_CL3_MID_S, _CL3_MID_OFF = 1.9754798, 0.12512219
_CL3_POS_A, _CL3_POS_S = 0.36726845, 14.98325
_CL3_POS_OFF = 0.12240537
_CL3_LO, _CL3_HI = -0.014, 0.014

def _lin_to_canonlog3(x):
    x = np.asarray(x, np.float64)
    neg_arg = np.maximum(1.0 - _CL3_NEG_S * np.minimum(x, _CL3_LO), 1e-10)   # guard discarded branches
    pos_arg = np.maximum(_CL3_POS_S * np.maximum(x, _CL3_HI) + 1.0, 1e-10)
    neg = -_CL3_NEG_A * np.log10(neg_arg) + _CL3_NEG_OFF
    mid = _CL3_MID_S * x + _CL3_MID_OFF
    pos = _CL3_POS_A * np.log10(pos_arg) + _CL3_POS_OFF
    return np.where(x < _CL3_LO, neg, np.where(x <= _CL3_HI, mid, pos)).astype(np.float32)

# breakpoints in the encoded domain (Canon Log3 code values at x = -0.014 / +0.014)
_CL3_Y_LO = _CL3_MID_S * _CL3_LO + _CL3_MID_OFF   # 0.097465...
_CL3_Y_HI = _CL3_MID_S * _CL3_HI + _CL3_MID_OFF   # 0.152779...

def _canonlog3_to_lin(y):
    y = np.asarray(y, np.float64)
    neg = -(10.0 ** ((_CL3_NEG_OFF - y) / _CL3_NEG_A) - 1.0) / _CL3_NEG_S
    mid = (y - _CL3_MID_OFF) / _CL3_MID_S
    pos = (10.0 ** ((y - _CL3_POS_OFF) / _CL3_POS_A) - 1.0) / _CL3_POS_S
    return np.where(y < _CL3_Y_LO, neg, np.where(y <= _CL3_Y_HI, mid, pos)).astype(np.float32)

# RED Log3G10, the REDWideGamutRGB native camera log curve (RED cameras, IPP2 pipeline): tone curve only,
# pair with REDWideGamutRGB -> ACEScg for the gamut. Constants confirmed from RED's own primary spec: "White
# Paper on REDWideGamutRGB and Log3G10" (RED Digital Cinema, Form 915-0187 Rev C, ECO 012644, 11/17),
# section "Equations". a=0.224282, b=155.975327, c=0.01 (input offset), linear-toe gradient g=15.1927 for
# x+c < 0. "3G10" name: 18% mid-gray encodes to 1/3, and 10 stops over mid-gray (0.18*2^10=184.32) reaches
# encoded 1.0. Confirmed anchors from the same doc's "Log3G10 Mapping Values" table: linear -0.01 -> 0.0,
# linear 0.0 -> 0.091551, linear 0.18 -> 0.333333, linear 1.0 -> 0.493449, linear 184.322 -> 1.0.
_L3G10_A, _L3G10_B, _L3G10_C, _L3G10_G = 0.224282, 155.975327, 0.01, 15.1927

def _lin_to_log3g10(x):
    x = np.asarray(x, np.float64) + _L3G10_C
    xc = np.maximum(x * _L3G10_B + 1.0, 1e-10)  # guard the discarded log branch from log10(<=0)
    return np.where(x >= 0.0, _L3G10_A * np.log10(xc),
                    x * _L3G10_G).astype(np.float32)

def _log3g10_to_lin(y):
    y = np.asarray(y, np.float64)
    return np.where(y >= 0.0, (10.0 ** (y / _L3G10_A) - 1.0) / _L3G10_B - _L3G10_C,
                    y / _L3G10_G - _L3G10_C).astype(np.float32)

# Blackmagic DaVinci Intermediate, the DaVinci Wide Gamut Intermediate OETF (DaVinci Resolve 17+, "Wide Gamut"
# color science): tone curve only, pair with DaVinci Wide Gamut -> ACEScg for the gamut. Constants confirmed
# from Blackmagic's own primary spec: "DaVinci Resolve 17: Wide Gamut Intermediate" white paper (Blackmagic
# Design, version 1.1, 31/07/2021), section "DaVinci Intermediate (OETF)". DI_A=0.0075, DI_B=7.0,
# DI_C=0.07329248, DI_M=10.44426855, linear-domain cut DI_LIN_CUT=0.00262409, log-domain cut
# DI_LOG_CUT=0.02740668 (values below DI_LIN_CUT / DI_LOG_CUT encode/decode through the linear branch of
# gradient DI_M). Confirmed anchors from the same doc's "Mapping Values" table: linear 0.0 -> 0.0, linear
# 0.18 (18% grey) -> 0.336043, linear 1.0 -> 0.513837, linear 10.0 -> 0.756599.
_DI_A, _DI_B, _DI_C, _DI_M = 0.0075, 7.0, 0.07329248, 10.44426855
_DI_LIN_CUT, _DI_LOG_CUT = 0.00262409, 0.02740668

def _lin_to_davinci_intermediate(x):
    x = np.asarray(x, np.float64)
    xc = np.maximum(x + _DI_A, 1e-10)  # guard the discarded log branch from log2(<=0)
    return np.where(x > _DI_LIN_CUT, (np.log2(xc) + _DI_B) * _DI_C,
                    x * _DI_M).astype(np.float32)

def _davinci_intermediate_to_lin(y):
    y = np.asarray(y, np.float64)
    return np.where(y > _DI_LOG_CUT, 2.0 ** (y / _DI_C - _DI_B) - _DI_A,
                    y / _DI_M).astype(np.float32)

_CURVES = {
    "cineon": (_lin_to_cineon, _cineon_to_lin),
    "acescct": (_lin_to_acescct, _acescct_to_lin),
    "acescc": (_lin_to_acescc, _acescc_to_lin),
    "logc3": (_lin_to_logc3, _logc3_to_lin),
    "logc4": (_lin_to_logc4, _logc4_to_lin),
    "slog3": (_lin_to_slog3, _slog3_to_lin),
    "vlog": (_lin_to_vlog, _vlog_to_lin),
    "canonlog3": (_lin_to_canonlog3, _canonlog3_to_lin),
    "log3g10": (_lin_to_log3g10, _log3g10_to_lin),
    "davinci_intermediate": (_lin_to_davinci_intermediate, _davinci_intermediate_to_lin),
}


# --------------------------------------------------------------------------- nodes

class OCIOLogConvert:
    """Linear <-> log (Nuke: OCIOLogConvert). Default curve Cineon (Nuke's flat film log, black 0 -> 0.0928);
    also ACEScct, ACEScc, ARRI LogC3 EI800 (the LTX-2 HDR IC-LoRA curve), ARRI LogC4 (the wider-headroom
    curve LumiPic's V10 *_logc4_* HDR LoRA targets, ceiling ~469.8 linear), plus five more camera-native log
    curves (Sony S-Log3, Panasonic V-Log, Canon Log 3, RED Log3G10, Blackmagic DaVinci Intermediate) - decode
    a camera-log plate to linear on OUR nodes without the config's camera-gamut assumption. Every curve here
    is the transfer CURVE only: the plate keeps its camera-native primaries, so pair log_to_lin with a
    downstream gamut ColorSpace step (camera gamut -> ACEScg), not the config's same-named colorspace (that
    assumes the camera's own wide gamut). Dependency-free. 'swap direction' flips lin<->log."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "operation": (["lin_to_log", "log_to_lin"], {"default": "lin_to_log"}),
            "curve": (["cineon", "acescct", "acescc", "logc3", "logc4", "slog3", "vlog", "canonlog3",
                       "log3g10", "davinci_intermediate"], {"default": "cineon",
                      "tooltip": "cineon = Nuke flat film log (black 0.0928). acescct = ACES log with a toe (0.0729). acescc = pure ACES log. logc3 = ARRI LogC3 EI800, the LTX-2 HDR curve (ceiling ~55 linear). logc4 = ARRI LogC4, wider headroom (ceiling ~469.8 linear), the LumiPic V10 *_logc4_* curve. slog3 = Sony S-Log3 (18% grey -> code 420/1023). vlog = Panasonic V-Log (18% grey -> code 433/1023). canonlog3 = Canon Log 3, three-piece curve with a linear mid segment around 0. log3g10 = RED Log3G10 (18% grey -> 1/3, 10 stops over grey -> 1.0). davinci_intermediate = Blackmagic DaVinci Intermediate OETF (18% grey -> 0.336043). All five are camera-native transfer curves only: log_to_lin decodes the plate to linear in the camera's own primaries; pair with a camera-gamut -> ACEScg ColorSpace step, not the config's same-named colorspace."}),
            "mix": _mix_input(),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    OUTPUT_TOOLTIPS = ("Image with the lin<->log curve applied.",)
    FUNCTION = "run"
    CATEGORY = "OCIO"

    def run(self, image, operation, curve, mix=1.0):
        f_lin2log, f_log2lin = _CURVES[curve]
        fn = f_lin2log if operation == "lin_to_log" else f_log2lin
        arr = image.detach().cpu().numpy().astype(np.float32)
        out = arr.copy()
        out[..., :3] = fn(arr[..., :3])
        return (_blend(image, torch.from_numpy(out).to(image.device, image.dtype), mix),)


class OCIOColorSpace:
    """Convert between two OCIO colorspaces (Nuke: OCIOColorSpace). 'swap in/out' button flips them."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "in_colorspace": _cs_input("ACES2065-1"),
            "out_colorspace": _cs_input("ACEScg"),
            "mix": _mix_input(),
        }, "optional": {"config_path": _config_input()}}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    OUTPUT_TOOLTIPS = ("Image converted from in_colorspace to out_colorspace.",)
    FUNCTION = "convert"
    CATEGORY = "OCIO"

    def convert(self, image, in_colorspace, out_colorspace, mix=1.0, config_path=BUILTIN):
        _require_ocio()
        cfg, cfg_key = _config_from_choice_keyed(config_path)
        if cfg is None:
            raise RuntimeError("No OCIO config found.")
        tf_key = ("colorspace", in_colorspace, out_colorspace)
        cpu = _cached_cpu_processor(cfg_key, tf_key, lambda: cfg.getProcessor(in_colorspace, out_colorspace))
        return (_blend(image, _apply_processor(image, cpu), mix),)


class OCIODisplay:
    """Apply a display + view transform (Nuke: OCIODisplay). Display and view are pickers from the config."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "in_colorspace": _cs_input("ACES2065-1"),
            "display": _display_input(),
            "view": _view_input(),
            "invert_direction": ("BOOLEAN", {"default": False,
                      "label_on": "Inverse (display -> scene)", "label_off": "Forward (scene -> display)",
                      "tooltip": "Invert (display-referred back to the input colorspace)."}),
            "mix": _mix_input(),
        }, "optional": {"config_path": _config_input()}}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    OUTPUT_TOOLTIPS = ("Image with the display + view transform applied (or inverted).",)
    FUNCTION = "run"
    CATEGORY = "OCIO"

    def run(self, image, in_colorspace, display, view, invert_direction=False, mix=1.0, config_path=BUILTIN):
        _require_ocio()
        cfg, cfg_key = _config_from_choice_keyed(config_path)
        if cfg is None:
            raise RuntimeError("No OCIO config found.")
        tf_key = ("display", in_colorspace, display, view, invert_direction)

        def build():
            t = OCIO.DisplayViewTransform(src=in_colorspace, display=display, view=view)
            t.setDirection(OCIO.TRANSFORM_DIR_INVERSE if invert_direction else OCIO.TRANSFORM_DIR_FORWARD)
            return cfg.getProcessor(t)

        cpu = _cached_cpu_processor(cfg_key, tf_key, build)
        return (_blend(image, _apply_processor(image, cpu), mix),)


class OCIOCDLTransform:
    """ASC CDL grade: slope, offset, power (per channel) + saturation (Nuke: OCIOCDLTransform)."""

    @classmethod
    def INPUT_TYPES(cls):
        f = lambda d: ("FLOAT", {"default": d, "min": -10.0, "max": 10.0, "step": 0.001})
        return {"required": {
            "image": ("IMAGE",),
            "slope_r": f(1.0), "slope_g": f(1.0), "slope_b": f(1.0),
            "offset_r": f(0.0), "offset_g": f(0.0), "offset_b": f(0.0),
            "power_r": f(1.0), "power_g": f(1.0), "power_b": f(1.0),
            "saturation": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 4.0, "step": 0.001}),
            "direction": (["forward", "inverse"], {"default": "forward"}),
            "mix": _mix_input(),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    OUTPUT_TOOLTIPS = ("Image with the ASC CDL grade applied.",)
    FUNCTION = "run"
    CATEGORY = "OCIO"

    def run(self, image, slope_r, slope_g, slope_b, offset_r, offset_g, offset_b,
            power_r, power_g, power_b, saturation, direction, mix=1.0):
        _require_ocio()
        cfg, cfg_key = _resolve_config_keyed("")
        if cfg is None:
            cfg, cfg_key = OCIO.Config.CreateRaw(), ("raw",)
        tf_key = ("cdl", slope_r, slope_g, slope_b, offset_r, offset_g, offset_b,
                  power_r, power_g, power_b, saturation, direction)

        def build():
            t = OCIO.CDLTransform()
            t.setSlope([slope_r, slope_g, slope_b])
            t.setOffset([offset_r, offset_g, offset_b])
            t.setPower([power_r, power_g, power_b])
            t.setSat(saturation)
            t.setDirection(OCIO.TRANSFORM_DIR_FORWARD if direction == "forward" else OCIO.TRANSFORM_DIR_INVERSE)
            return cfg.getProcessor(t)

        cpu = _cached_cpu_processor(cfg_key, tf_key, build)
        return (_blend(image, _apply_processor(image, cpu), mix),)


class OCIOFileTransform:
    """Apply a LUT / CCC / CDL file (Nuke: OCIOFileTransform). The file is a picker from the input folder."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "file_path": _lut_input(),
            "interpolation": (["linear", "nearest", "tetrahedral", "best"], {"default": "linear"}),
            "direction": (["forward", "inverse"], {"default": "forward"}),
            "mix": _mix_input(),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    OUTPUT_TOOLTIPS = ("Image with the LUT / CCC / CDL file transform applied.",)
    FUNCTION = "run"
    CATEGORY = "OCIO"

    def run(self, image, file_path, interpolation, direction, mix=1.0):
        _require_ocio()
        path = _lut_path(file_path)
        if not (path and os.path.isfile(path)):
            raise RuntimeError(f"LUT file not found: {file_path}")
        interp = {"linear": OCIO.INTERP_LINEAR, "nearest": OCIO.INTERP_NEAREST,
                  "tetrahedral": OCIO.INTERP_TETRAHEDRAL, "best": OCIO.INTERP_BEST}[interpolation]
        cfg, cfg_key = _resolve_config_keyed("")
        if cfg is None:
            cfg, cfg_key = OCIO.Config.CreateRaw(), ("raw",)
        st = os.stat(path)
        tf_key = ("file", path, st.st_mtime_ns, st.st_size, interpolation, direction)

        def build():
            t = OCIO.FileTransform(src=path, interpolation=interp)
            t.setDirection(OCIO.TRANSFORM_DIR_FORWARD if direction == "forward" else OCIO.TRANSFORM_DIR_INVERSE)
            return cfg.getProcessor(t)

        cpu = _cached_cpu_processor(cfg_key, tf_key, build)
        return (_blend(image, _apply_processor(image, cpu), mix),)


class OCIOLookTransform:
    """Apply an OCIO look (Nuke: OCIOLookTransform). 'look' is a picker; 'swap in/out' button flips in/out."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "in_colorspace": _cs_input("ACES2065-1"),
            "out_colorspace": _cs_input("sRGB - Display"),
            "look": _looks_input(),
            "invert_direction": ("BOOLEAN", {"default": False,
                      "label_on": "Inverse (out -> in)", "label_off": "Forward (in -> out)"}),
            "mix": _mix_input(),
        }, "optional": {"config_path": _config_input()}}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    OUTPUT_TOOLTIPS = ("Image with the OCIO look applied (or inverted).",)
    FUNCTION = "run"
    CATEGORY = "OCIO"

    def run(self, image, in_colorspace, out_colorspace, look, invert_direction=False, mix=1.0, config_path=BUILTIN):
        _require_ocio()
        cfg, cfg_key = _config_from_choice_keyed(config_path)
        if cfg is None:
            raise RuntimeError("No OCIO config found.")
        looks = "" if (not look or look == "(none)") else look
        tf_key = ("look", in_colorspace, out_colorspace, looks, invert_direction)

        def build():
            t = OCIO.LookTransform(src=in_colorspace, dst=out_colorspace, looks=looks)
            t.setDirection(OCIO.TRANSFORM_DIR_INVERSE if invert_direction else OCIO.TRANSFORM_DIR_FORWARD)
            return cfg.getProcessor(t)

        cpu = _cached_cpu_processor(cfg_key, tf_key, build)
        return (_blend(image, _apply_processor(image, cpu), mix),)


NODE_CLASS_MAPPINGS = {
    "OCIOColorSpace": OCIOColorSpace,
    "OCIOLogConvert": OCIOLogConvert,
    "OCIODisplay": OCIODisplay,
    "OCIOCDLTransform": OCIOCDLTransform,
    "OCIOFileTransform": OCIOFileTransform,
    "OCIOLookTransform": OCIOLookTransform,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "OCIOColorSpace": "OCIO ColorSpace",
    "OCIOLogConvert": "OCIO LogConvert",
    "OCIODisplay": "OCIO Display",
    "OCIOCDLTransform": "OCIO CDLTransform",
    "OCIOFileTransform": "OCIO FileTransform",
    "OCIOLookTransform": "OCIO LookTransform",
}
