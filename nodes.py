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


# --------------------------------------------------------------------------- config + apply helpers

def _resolve_config(path=""):
    """Resolve an OCIO config: explicit file -> $OCIO env -> built-in ACES studio config. None if unavailable."""
    if not _HAS_OCIO:
        return None
    if path and os.path.isfile(path):
        return OCIO.Config.CreateFromFile(path)
    if os.environ.get("OCIO"):
        try:
            return OCIO.Config.CreateFromEnv()
        except Exception:
            pass
    for builtin in ("studio-config-latest", "cg-config-latest", "ocio://default"):
        try:
            return OCIO.Config.CreateFromBuiltinConfig(builtin)
        except Exception:
            continue
    return None


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


def _config_from_choice(choice):
    """Combo choice ('(built-in ...)' or a .ocio file in input) -> an OCIO Config."""
    if choice and choice != BUILTIN:
        p = choice if os.path.isabs(choice) else os.path.join(_input_dir(), choice)
        if os.path.isfile(p):
            return OCIO.Config.CreateFromFile(p)
    return _resolve_config("")


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


def _apply_processor(img, processor):
    cpu = processor.getDefaultCPUProcessor()
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
            np.where(x < 2.0 ** -15, (np.log2(2.0 ** -16 + x * 0.5) + 9.72) / 17.52,
                     (np.log2(np.maximum(x, 2.0 ** -16)) + 9.72) / 17.52)).astype(np.float32)

def _acescc_to_lin(y):
    y = np.asarray(y, np.float64)
    return np.where(y < (9.72 - 15.0) / 17.52, (np.exp2(y * 17.52 - 9.72) - 2.0 ** -16) * 2.0,
            np.where(y < (np.log2(65504.0) + 9.72) / 17.52, np.exp2(y * 17.52 - 9.72), 65504.0)).astype(np.float32)

_CURVES = {
    "cineon": (_lin_to_cineon, _cineon_to_lin),
    "acescct": (_lin_to_acescct, _acescct_to_lin),
    "acescc": (_lin_to_acescc, _acescc_to_lin),
}


# --------------------------------------------------------------------------- nodes

class OCIOLogConvert:
    """Linear <-> log (Nuke: OCIOLogConvert). Default curve Cineon (Nuke's flat film log, black 0 -> 0.0928);
    also ACEScct, ACEScc. Dependency-free. The 'swap direction' button flips lin<->log."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "operation": (["lin_to_log", "log_to_lin"], {"default": "lin_to_log"}),
            "curve": (["cineon", "acescct", "acescc"], {"default": "cineon",
                      "tooltip": "cineon = Nuke flat film log (black 0.0928). acescct = ACES log with a toe (0.0729). acescc = pure ACES log."}),
            "mix": _mix_input(),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
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
    FUNCTION = "convert"
    CATEGORY = "OCIO"

    def convert(self, image, in_colorspace, out_colorspace, mix=1.0, config_path=BUILTIN):
        _require_ocio()
        cfg = _config_from_choice(config_path)
        if cfg is None:
            raise RuntimeError("No OCIO config found.")
        return (_blend(image, _apply_processor(image, cfg.getProcessor(in_colorspace, out_colorspace)), mix),)


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
                      "tooltip": "Invert (display-referred back to the input colorspace)."}),
            "mix": _mix_input(),
        }, "optional": {"config_path": _config_input()}}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "run"
    CATEGORY = "OCIO"

    def run(self, image, in_colorspace, display, view, invert_direction=False, mix=1.0, config_path=BUILTIN):
        _require_ocio()
        cfg = _config_from_choice(config_path)
        if cfg is None:
            raise RuntimeError("No OCIO config found.")
        t = OCIO.DisplayViewTransform(src=in_colorspace, display=display, view=view)
        t.setDirection(OCIO.TRANSFORM_DIR_INVERSE if invert_direction else OCIO.TRANSFORM_DIR_FORWARD)
        return (_blend(image, _apply_processor(image, cfg.getProcessor(t)), mix),)


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
    FUNCTION = "run"
    CATEGORY = "OCIO"

    def run(self, image, slope_r, slope_g, slope_b, offset_r, offset_g, offset_b,
            power_r, power_g, power_b, saturation, direction, mix=1.0):
        _require_ocio()
        cfg = _resolve_config("") or OCIO.Config.CreateRaw()
        t = OCIO.CDLTransform()
        t.setSlope([slope_r, slope_g, slope_b])
        t.setOffset([offset_r, offset_g, offset_b])
        t.setPower([power_r, power_g, power_b])
        t.setSat(saturation)
        t.setDirection(OCIO.TRANSFORM_DIR_FORWARD if direction == "forward" else OCIO.TRANSFORM_DIR_INVERSE)
        return (_blend(image, _apply_processor(image, cfg.getProcessor(t)), mix),)


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
    FUNCTION = "run"
    CATEGORY = "OCIO"

    def run(self, image, file_path, interpolation, direction, mix=1.0):
        _require_ocio()
        path = _lut_path(file_path)
        if not (path and os.path.isfile(path)):
            raise RuntimeError(f"LUT file not found: {file_path}")
        interp = {"linear": OCIO.INTERP_LINEAR, "nearest": OCIO.INTERP_NEAREST,
                  "tetrahedral": OCIO.INTERP_TETRAHEDRAL, "best": OCIO.INTERP_BEST}[interpolation]
        t = OCIO.FileTransform(src=path, interpolation=interp)
        t.setDirection(OCIO.TRANSFORM_DIR_FORWARD if direction == "forward" else OCIO.TRANSFORM_DIR_INVERSE)
        cfg = _resolve_config("") or OCIO.Config.CreateRaw()
        return (_blend(image, _apply_processor(image, cfg.getProcessor(t)), mix),)


class OCIOLookTransform:
    """Apply an OCIO look (Nuke: OCIOLookTransform). 'look' is a picker; 'swap in/out' button flips in/out."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "in_colorspace": _cs_input("ACES2065-1"),
            "out_colorspace": _cs_input("sRGB - Display"),
            "look": _looks_input(),
            "invert_direction": ("BOOLEAN", {"default": False}),
            "mix": _mix_input(),
        }, "optional": {"config_path": _config_input()}}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "run"
    CATEGORY = "OCIO"

    def run(self, image, in_colorspace, out_colorspace, look, invert_direction=False, mix=1.0, config_path=BUILTIN):
        _require_ocio()
        cfg = _config_from_choice(config_path)
        if cfg is None:
            raise RuntimeError("No OCIO config found.")
        looks = "" if (not look or look == "(none)") else look
        t = OCIO.LookTransform(src=in_colorspace, dst=out_colorspace, looks=looks)
        t.setDirection(OCIO.TRANSFORM_DIR_INVERSE if invert_direction else OCIO.TRANSFORM_DIR_FORWARD)
        return (_blend(image, _apply_processor(image, cfg.getProcessor(t)), mix),)


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
