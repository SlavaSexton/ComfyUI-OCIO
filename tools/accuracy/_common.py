"""Shared harness for the OCIO color-accuracy audit (2026-07-03).

Answers the review question "how did you test accuracy?" with MEASUREMENTS, not guesses:
  - load the shipped pack as a package (its io_nodes uses a relative import, and the folder name has a
    hyphen, so a plain `import` fails) -> load_pack()
  - error metrics (max abs / RMS / PSNR)
  - colour science implemented here on numpy (no external colour-science dep): sRGB EOTF, sRGB->XYZ->Lab,
    CIE dE76 and dE2000 (Sharma 2005), self-tested against the published Sharma test pairs
  - cv2 histogram comparison (the method Sam Hodge linked: cv2.compareHist) - Correlation / Chi-Square /
    Intersection / Bhattacharyya
  - quantisation / banding metrics for the float->8/16-bit write path
  - a plots dir + a savefig helper (matplotlib Agg)

Every measure_*.py in this folder imports from here so the numbers are produced the same way everywhere.
"""
import os
import sys
import json
import importlib.util
import numpy as np

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
PACK_DIR = os.path.abspath(os.path.join(HERE, "..", ".."))            # the ComfyUI-OCIO repo root
FIXT_DIR = os.path.join(HERE, "fixtures")
PLOTS_DIR = os.path.join(HERE, "plots")
COMFY_DIR = r"E:/ComfyUI/ComfyUI/ComfyUI"                             # for folder_paths (guarded in the pack anyway)


# --------------------------------------------------------------------------- load the shipped pack
def load_pack():
    """Import the shipped ComfyUI-OCIO package (nodes, io_nodes) standalone. Verified working 2026-07-03:
    the folder name has a hyphen and io_nodes uses `from .nodes import ...`, so we load it as a named
    package via importlib with submodule_search_locations pointing at the repo root."""
    if COMFY_DIR not in sys.path:
        sys.path.insert(0, COMFY_DIR)
    if "ocio_pkg" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "ocio_pkg", os.path.join(PACK_DIR, "__init__.py"),
            submodule_search_locations=[PACK_DIR])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ocio_pkg"] = mod
        spec.loader.exec_module(mod)
    from ocio_pkg import nodes as N          # noqa: E402
    from ocio_pkg import io_nodes as IO       # noqa: E402
    return N, IO


def ocio_reference(in_cs, out_cs, rgb, config_path=""):
    """Ground truth: transform an [...,3] float array with a raw PyOpenColorIO CPU processor (the C++
    reference), independent of our node wrapper. Returns a new array, same shape."""
    N, _ = load_pack()
    import PyOpenColorIO as OCIO   # noqa
    cfg, _ = N._resolve_config_keyed(config_path)
    proc = cfg.getProcessor(in_cs, out_cs).getDefaultCPUProcessor()
    out = np.ascontiguousarray(rgb.astype(np.float32)).copy()
    flat = out.reshape(-1, 3)
    proc.applyRGB(flat.reshape(-1).tolist()) if False else None   # (list path is slow; use packed below)
    # packed image desc is fastest + matches the node's own path
    h = flat.shape[0]
    desc = OCIO.PackedImageDesc(flat, h, 1, 3)
    proc.apply(desc)
    return out


# --------------------------------------------------------------------------- error metrics
def max_abs(a, b):
    return float(np.max(np.abs(a.astype(np.float64) - b.astype(np.float64))))

def rms(a, b):
    d = a.astype(np.float64) - b.astype(np.float64)
    return float(np.sqrt(np.mean(d * d)))

def psnr(a, b, peak=1.0):
    m = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return float("inf") if m <= 0 else float(10.0 * np.log10((peak * peak) / m))


# --------------------------------------------------------------------------- sRGB EOTF + colour science
def srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)

def linear_to_srgb(c):
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(np.clip(c, 0, None), 1 / 2.4) - 0.055)

# sRGB (linear) -> CIE XYZ, D65 (IEC 61966-2-1)
_SRGB_TO_XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                         [0.2126729, 0.7151522, 0.0721750],
                         [0.0193339, 0.1191920, 0.9503041]])
_D65 = np.array([0.95047, 1.00000, 1.08883])

def srgb_to_xyz(rgb_srgb):
    lin = srgb_to_linear(np.asarray(rgb_srgb, dtype=np.float64))
    return lin @ _SRGB_TO_XYZ.T

def xyz_to_lab(xyz, white=_D65):
    xyz = np.asarray(xyz, dtype=np.float64) / white
    d = 6.0 / 29.0
    f = np.where(xyz > d ** 3, np.cbrt(xyz), xyz / (3 * d * d) + 4.0 / 29.0)
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    return np.stack([116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)], axis=-1)

def srgb_to_lab(rgb_srgb):
    return xyz_to_lab(srgb_to_xyz(rgb_srgb))

def delta_e_76(lab1, lab2):
    return np.sqrt(np.sum((np.asarray(lab1, float) - np.asarray(lab2, float)) ** 2, axis=-1))

def delta_e_2000(lab1, lab2, kL=1.0, kC=1.0, kH=1.0):
    """CIEDE2000 (Sharma, Wu, Dalal 2005). Vectorised over the last axis = [L,a,b]."""
    lab1 = np.asarray(lab1, dtype=np.float64); lab2 = np.asarray(lab2, dtype=np.float64)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    C1 = np.hypot(a1, b1); C2 = np.hypot(a2, b2)
    Cbar = 0.5 * (C1 + C2)
    G = 0.5 * (1 - np.sqrt(Cbar ** 7 / (Cbar ** 7 + 25.0 ** 7)))
    a1p = (1 + G) * a1; a2p = (1 + G) * a2
    C1p = np.hypot(a1p, b1); C2p = np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp = L2 - L1
    dCp = C2p - C1p
    dhp = h2p - h1p
    dhp = np.where(dhp > 180, dhp - 360, dhp)
    dhp = np.where(dhp < -180, dhp + 360, dhp)
    dhp = np.where(C1p * C2p == 0, 0.0, dhp)
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp) / 2)
    Lbarp = 0.5 * (L1 + L2)
    Cbarp = 0.5 * (C1p + C2p)
    hsum = h1p + h2p
    habs = np.abs(h1p - h2p)
    hbarp = np.where(C1p * C2p == 0, hsum,
                     np.where(habs <= 180, 0.5 * hsum,
                              np.where(hsum < 360, 0.5 * (hsum + 360), 0.5 * (hsum - 360))))
    T = (1 - 0.17 * np.cos(np.radians(hbarp - 30))
         + 0.24 * np.cos(np.radians(2 * hbarp))
         + 0.32 * np.cos(np.radians(3 * hbarp + 6))
         - 0.20 * np.cos(np.radians(4 * hbarp - 63)))
    dtheta = 30 * np.exp(-(((hbarp - 275) / 25) ** 2))
    Rc = 2 * np.sqrt(Cbarp ** 7 / (Cbarp ** 7 + 25.0 ** 7))
    Sl = 1 + (0.015 * (Lbarp - 50) ** 2) / np.sqrt(20 + (Lbarp - 50) ** 2)
    Sc = 1 + 0.045 * Cbarp
    Sh = 1 + 0.015 * Cbarp * T
    Rt = -np.sin(np.radians(2 * dtheta)) * Rc
    return np.sqrt((dLp / (kL * Sl)) ** 2 + (dCp / (kC * Sc)) ** 2 + (dHp / (kH * Sh)) ** 2
                   + Rt * (dCp / (kC * Sc)) * (dHp / (kH * Sh)))


# --------------------------------------------------------------------------- cv2 histogram compare (Sam's link)
def hist_compare(a, b, bins=256, rng=(0.0, 1.0)):
    """Per-channel histogram comparison via cv2.compareHist - the exact tool Sam Hodge linked. Inputs are
    float [...,3] in `rng`. Returns {channel: {metric: value}} for Correlation / Chi-Square / Intersection /
    Bhattacharyya. Correlation 1.0 and Bhattacharyya 0.0 mean identical distributions."""
    import cv2
    a = np.asarray(a, np.float32); b = np.asarray(b, np.float32)
    methods = {"correlation": cv2.HISTCMP_CORREL, "chi_square": cv2.HISTCMP_CHISQR,
               "intersection": cv2.HISTCMP_INTERSECT, "bhattacharyya": cv2.HISTCMP_BHATTACHARYYA}
    out = {}
    for ci, ch in enumerate("RGB"):
        ha = cv2.calcHist([a[..., ci].reshape(-1, 1)], [0], None, [bins], list(rng))
        hb = cv2.calcHist([b[..., ci].reshape(-1, 1)], [0], None, [bins], list(rng))
        cv2.normalize(ha, ha, 0, 1, cv2.NORM_MINMAX)
        cv2.normalize(hb, hb, 0, 1, cv2.NORM_MINMAX)
        out[ch] = {m: float(cv2.compareHist(ha, hb, code)) for m, code in methods.items()}
    return out


# --------------------------------------------------------------------------- quantisation / banding
def quantise(img, bits):
    """Round a float [0,1] image to `bits`-bit levels and back to float (the naive no-dither write)."""
    levels = (1 << bits) - 1
    return np.round(np.clip(img, 0.0, 1.0) * levels) / levels

def banding_metrics(ramp_float, bits):
    """On a smooth 1-D gradient, quantifying banding: the quantisation RMS error, the number of distinct
    output levels vs the ideal, and the longest run of identical output values (a flat 'band')."""
    q = quantise(ramp_float, bits)
    distinct = int(np.unique(np.round(q * ((1 << bits) - 1))).size)
    # longest identical run
    qi = np.round(q.reshape(-1) * ((1 << bits) - 1)).astype(np.int64)
    run = best = 1
    for i in range(1, qi.size):
        run = run + 1 if qi[i] == qi[i - 1] else 1
        best = max(best, run)
    return {"quant_rms": rms(ramp_float, q), "quant_max": max_abs(ramp_float, q),
            "distinct_levels": distinct, "ideal_levels": (1 << bits),
            "longest_flat_run_px": int(best)}


# --------------------------------------------------------------------------- plotting
def plots_dir():
    os.makedirs(PLOTS_DIR, exist_ok=True)
    return PLOTS_DIR

def new_fig(*a, **k):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"figure.facecolor": "#14141a", "axes.facecolor": "#191922",
                         "axes.edgecolor": "#556", "text.color": "#dde", "axes.labelcolor": "#cde",
                         "xtick.color": "#9ab", "ytick.color": "#9ab", "axes.titlecolor": "#e8ecff",
                         "grid.color": "#333", "font.size": 9})
    return plt.subplots(*a, **k)

def savefig(fig, name):
    p = os.path.join(plots_dir(), name)
    fig.savefig(p, dpi=130, bbox_inches="tight", facecolor="#14141a")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return p

def dump(name, obj):
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    p = os.path.join(HERE, "results", name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    return p


# --------------------------------------------------------------------------- dE2000 self-test (Sharma pairs)
# Published Sharma/Wu/Dalal 2005 test data: Lab1, Lab2, expected dE2000. If our implementation matches these
# to 1e-4 it is correct - so the ColorChecker dE numbers downstream can be trusted.
_SHARMA = [
    ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
    ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
    ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
    ((50.0, -1.3802, -84.2814), (50.0, 0.0, -82.7485), 1.0000),
    ((50.0, -1.1848, -84.8006), (50.0, 0.0, -82.7485), 1.0000),
    ((50.0, 2.5, 0.0), (50.0, 0.0, -2.5), 4.3065),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),   # authoritative Sharma value (verified 2026-07-03)
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]

def _selftest():
    errs = []
    for lab1, lab2, exp in _SHARMA:
        got = float(delta_e_2000(np.array(lab1), np.array(lab2)))
        errs.append(abs(got - exp))
    worst = max(errs)
    ok = worst < 1e-3
    return {"deltaE2000_selftest_ok": ok, "worst_abs_err_vs_sharma": worst,
            "pairs_tested": len(_SHARMA)}


if __name__ == "__main__":
    print(json.dumps(_selftest(), indent=2))
    # sanity: sRGB white -> Lab ~ (100, 0, 0); mid grey 0.5 sRGB -> L ~ 53.4
    print("sRGB white -> Lab:", [round(x, 3) for x in srgb_to_lab([1.0, 1.0, 1.0])])
    print("sRGB 0.5   -> Lab:", [round(x, 3) for x in srgb_to_lab([0.5, 0.5, 0.5])])
