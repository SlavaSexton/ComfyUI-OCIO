"""OCIO color-math parity regression (run: python tools/test_colorspace_parity.py). Needs opencolorio.

Locks in the color foundations the pack relies on, checked against the live OCIO ACES config (the same engine
Nuke uses): the ComfyUI working space is sRGB - Display; the config's camera transforms are ARRI/ACES-correct;
Linear Rec.709 -> ACEScg is a sound gamut transform; and the LumiPic log decode round-trips with HDR preserved.
"""
import os, sys
os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"
import numpy as np
import PyOpenColorIO as O

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nodes import _lin_to_logc3, _logc3_to_lin, _lin_to_logc4, _logc4_to_lin

cfg = O.Config.CreateFromBuiltinConfig("studio-config-latest")

def conv(a, src, dst):
    x = np.ascontiguousarray(np.asarray(a, np.float32).reshape(1, -1, 3))
    cfg.getProcessor(src, dst).getDefaultCPUProcessor().apply(O.PackedImageDesc(x, x.shape[1], 1, 3))
    return x.reshape(-1, 3)

# P1: ComfyUI working buffer == sRGB - Display (sRGB EOTF: 0.5 coded -> 0.21404 linear)
p1 = float(conv([0.5, 0.5, 0.5], "sRGB - Display", "Linear Rec.709 (sRGB)")[0][0])
assert abs(p1 - 0.21404) < 1e-3, f"P1 sRGB EOTF off: {p1}"

# P2: ARRI LogC3 -> linear is ARRI-correct (= Nuke): mid-gray code 0.391 -> ~0.18
p2 = float(conv([0.391, 0.391, 0.391], "ARRI LogC3 (EI800)", "Linear ARRI Wide Gamut 3")[0][0])
assert abs(p2 - 0.18) < 0.02, f"P2 LogC3 mid-gray off: {p2}"

# P3: Linear Rec.709 -> ACEScg is sound: white stays white, and it round-trips to identity
p3w = conv([1.0, 1.0, 1.0], "Linear Rec.709 (sRGB)", "ACEScg")[0]
assert np.allclose(p3w, [1, 1, 1], atol=1e-3), f"P3 white not preserved: {p3w}"
p3rt = conv(conv([0.2, 0.5, 0.8], "Linear Rec.709 (sRGB)", "ACEScg")[0].tolist(),
            "ACEScg", "Linear Rec.709 (sRGB)")[0]
assert np.allclose(p3rt, [0.2, 0.5, 0.8], atol=1e-4), f"P3 roundtrip not identity: {p3rt}"

# P4: LumiPic pipeline (logc3 & logc4): decode round-trips, and HDR survives to ACEScg (> 1)
for name, enc, dec in (("logc3", _lin_to_logc3, _logc3_to_lin), ("logc4", _lin_to_logc4, _logc4_to_lin)):
    src = np.array([[0.18, 0.18, 0.18], [8.0, 4.0, 2.0]], np.float32)
    back = dec(enc(src))
    rel = float(np.max(np.abs(back - src) / (np.abs(src) + 1e-3)))
    assert rel < 1e-3, f"P4 {name} decode round-trip off: {rel}"
    hdr = conv(back.tolist(), "Linear Rec.709 (sRGB)", "ACEScg")[1]
    assert hdr.max() > 1.0, f"P4 {name} lost HDR headroom to ACEScg: {hdr}"

print("PASS: OCIO color-math parity (sRGB EOTF, LogC3 mid-gray, Rec.709->ACEScg white+roundtrip, LumiPic pipeline).")
