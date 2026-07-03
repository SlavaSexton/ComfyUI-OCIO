"""Generate deterministic test material for the color-accuracy audit (2026-07-03).

Synthetic + published-constant charts beat random internet photos for accuracy work: they have KNOWN values,
smooth gradients expose banding, and HDR/negative ramps probe out-of-range survival. Real footage (the shipped
nyc_skyline.png and, externally, a real EXR) is used on top for the histogram tests.

Writes tools/accuracy/fixtures/:
  colorchecker_srgb.npy  (24,3)   X-Rite ColorChecker Classic sRGB values (BabelColor averages)
  colorchecker.png                6x4 patch chart for the report
  ramp_linear.npy        (2048,)  smooth 0..1 gradient (banding / quantisation)
  ramp2d.npy             (256,1024,3) smooth 2D gradient 0..1 (banding visualisation)
  hdr_ramp.npy           (K,3)    values from -0.25 to 16.0 (HDR-safety / clipping)
  spectral_sweep.npy     (H,W,3)  hue x value sweep (varied colors for parity + histograms)
  manifest.json                   inventory for the measure_*.py scripts
"""
import os
import json
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
FIXT = os.path.join(HERE, "fixtures")
os.makedirs(FIXT, exist_ok=True)

# X-Rite ColorChecker Classic, sRGB 8-bit (BabelColor average, D65) - the widely cited reference values.
COLORCHECKER_SRGB8 = np.array([
    [115, 82, 68], [194, 150, 130], [98, 122, 157], [87, 108, 67], [133, 128, 177], [103, 189, 170],
    [214, 126, 44], [80, 91, 166], [193, 90, 99], [94, 60, 108], [157, 188, 64], [224, 163, 46],
    [56, 61, 150], [70, 148, 73], [175, 54, 60], [231, 199, 31], [187, 86, 149], [8, 133, 161],
    [243, 243, 242], [200, 200, 200], [160, 160, 160], [122, 122, 121], [85, 85, 85], [52, 52, 52],
], dtype=np.float32)
CC_NAMES = ["dark skin", "light skin", "blue sky", "foliage", "blue flower", "bluish green",
            "orange", "purplish blue", "moderate red", "purple", "yellow green", "orange yellow",
            "blue", "green", "red", "yellow", "magenta", "cyan",
            "white", "neutral 8", "neutral 6.5", "neutral 5", "neutral 3.5", "black"]


def save(name, arr):
    p = os.path.join(FIXT, name)
    np.save(p, np.ascontiguousarray(arr.astype(np.float32)))
    return p


def main():
    man = {}

    cc = COLORCHECKER_SRGB8 / 255.0
    save("colorchecker_srgb.npy", cc)
    man["colorchecker_srgb.npy"] = {"shape": list(cc.shape), "space": "sRGB - Display (0..1)",
                                    "names": CC_NAMES, "note": "24 X-Rite patches, sRGB 8-bit / 255"}

    # ColorChecker chart PNG (6 wide x 4 tall), 100px patches, for the report
    try:
        import cv2
        ps = 100
        chart = np.zeros((4 * ps, 6 * ps, 3), np.float32)
        for i, c in enumerate(cc):
            r, col = divmod(i, 6)
            chart[r * ps:(r + 1) * ps, col * ps:(col + 1) * ps] = c
        cv2.imwrite(os.path.join(FIXT, "colorchecker.png"), (np.clip(chart, 0, 1) * 255)[..., ::-1].astype(np.uint8))
        man["colorchecker.png"] = "6x4 patch chart"
    except Exception as e:
        man["colorchecker.png"] = f"skipped: {e}"

    ramp = np.linspace(0.0, 1.0, 2048, dtype=np.float32)
    save("ramp_linear.npy", ramp)
    man["ramp_linear.npy"] = {"shape": [2048], "note": "smooth 0..1, for banding/quantisation"}

    # 2D gradient (dark region emphasised where banding is worst)
    x = np.linspace(0, 1, 1024, dtype=np.float32)
    r2 = np.tile(x, (256, 1))
    r2 = np.stack([r2, r2, r2], -1)
    save("ramp2d.npy", r2)
    man["ramp2d.npy"] = {"shape": list(r2.shape), "note": "smooth horizontal 0..1 grey gradient"}

    hdr = np.concatenate([np.linspace(-0.25, 0.0, 32), np.linspace(0.0, 1.0, 96),
                          np.linspace(1.0, 16.0, 128)]).astype(np.float32)
    hdr = np.stack([hdr, hdr, hdr], -1)
    save("hdr_ramp.npy", hdr)
    man["hdr_ramp.npy"] = {"shape": list(hdr.shape), "range": [-0.25, 16.0],
                           "note": "negatives + super-white, for HDR-safety / silent-clip detection"}

    # spectral / hue x value sweep for varied colors (parity + histogram richness)
    H, W = 128, 512
    hue = np.linspace(0, 1, W, dtype=np.float32)[None, :].repeat(H, 0)
    val = np.linspace(0.05, 1.0, H, dtype=np.float32)[:, None].repeat(W, 1)
    try:
        import cv2
        hsv = np.stack([hue * 179, np.full_like(hue, 220), val * 255], -1).astype(np.uint8)
        sweep = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB).astype(np.float32) / 255.0
    except Exception:
        sweep = np.stack([hue, val, 1 - hue], -1).astype(np.float32)
    save("spectral_sweep.npy", sweep)
    man["spectral_sweep.npy"] = {"shape": list(sweep.shape), "note": "hue x value sweep, many colors"}

    # point at the shipped real image
    nyc = os.path.abspath(os.path.join(HERE, "..", "..", "example_workflows", "nyc_skyline.png"))
    man["real_image"] = nyc if os.path.isfile(nyc) else "MISSING"
    man["real_exr_seq"] = r"D:\Projects\Red_Sky_Studios\The_Shift\0531\precomp\LeftGirl.v01"

    with open(os.path.join(FIXT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, indent=2)
    print(json.dumps({k: (v if not str(k).endswith('.npy') else v.get('shape', v) if isinstance(v, dict) else v)
                      for k, v in man.items()}, indent=2, default=str))


if __name__ == "__main__":
    main()
