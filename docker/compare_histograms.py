"""Compare an OCIO round-trip's input vs output with OpenCV histograms (+ a pixel-error backstop).

The requested metric is an OpenCV histogram comparison (cv2.calcHist / cv2.compareHist). Because a
histogram is binning-lossy (two different images can share one), a max/mean absolute pixel error is
also reported as the rigorous "is the math actually perfect" check. Both are printed; PASS requires
both to clear their tolerances.

Usable as a module (compare()) or a CLI:  python compare_histograms.py <input.exr> <output.exr> [label]
"""
import os
import sys

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")  # must precede cv2 import for EXR
import cv2  # noqa: E402
import numpy as np  # noqa: E402


def _load_rgb(path):
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH | cv2.IMREAD_ANYCOLOR)
    if img is None:
        raise FileNotFoundError(f"could not read image: {path}")
    img = np.asarray(img, dtype=np.float32)
    if img.ndim == 2:
        img = img[:, :, None]
    return img[:, :, :3]  # first 3 channels; ignore alpha (channel order is consistent in & out)


def compare(input_path, output_path, bins=8192, tol_corr=0.99999, tol_pix=1e-4):
    a = _load_rgb(input_path)
    b = _load_rgb(output_path)
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch: input {a.shape} vs output {b.shape}")

    # Per-channel OpenCV histograms over a shared range (union of both images so bins align), then
    # cv2.compareHist. CORREL: 1.0 == identical distribution. BHATTACHARYYA: 0.0 == identical.
    corr, bhat = [], []
    for c in range(3):
        ca, cb = a[:, :, c], b[:, :, c]
        lo = float(min(ca.min(), cb.min()))
        hi = float(max(ca.max(), cb.max()))
        if hi <= lo:
            hi = lo + 1e-6
        ha = cv2.calcHist([ca], [0], None, [bins], [lo, hi])
        hb = cv2.calcHist([cb], [0], None, [bins], [lo, hi])
        cv2.normalize(ha, ha, 0, 1, cv2.NORM_MINMAX)
        cv2.normalize(hb, hb, 0, 1, cv2.NORM_MINMAX)
        corr.append(float(cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL)))
        bhat.append(float(cv2.compareHist(ha, hb, cv2.HISTCMP_BHATTACHARYYA)))

    diff = np.abs(a - b)
    metrics = {
        "hist_correl_rgb": corr,          # per-channel, want ~1.0
        "hist_correl_min": min(corr),
        "hist_bhattacharyya_rgb": bhat,   # per-channel, want ~0.0
        "hist_bhattacharyya_max": max(bhat),
        "max_abs_pixel_error": float(diff.max()),
        "mean_abs_pixel_error": float(diff.mean()),
        "tol_corr": tol_corr,
        "tol_pix": tol_pix,
    }
    metrics["hist_pass"] = metrics["hist_correl_min"] >= tol_corr
    metrics["pixel_pass"] = metrics["max_abs_pixel_error"] <= tol_pix
    metrics["pass"] = metrics["hist_pass"] and metrics["pixel_pass"]
    return metrics


def format_report(label, metrics):
    p = lambda vals: "[" + ", ".join(f"{v:.6f}" for v in vals) + "]"  # noqa: E731
    verdict = "PASS" if metrics["pass"] else "FAIL"
    return (
        f"--- {label}: {verdict} ---\n"
        f"  histogram correlation  (want >= {metrics['tol_corr']}) : "
        f"min {metrics['hist_correl_min']:.6f}  rgb {p(metrics['hist_correl_rgb'])}\n"
        f"  histogram bhattacharyya (want ~0)                      : "
        f"max {metrics['hist_bhattacharyya_max']:.6f}  rgb {p(metrics['hist_bhattacharyya_rgb'])}\n"
        f"  max  abs pixel error   (want <= {metrics['tol_pix']})       : {metrics['max_abs_pixel_error']:.6e}\n"
        f"  mean abs pixel error                                  : {metrics['mean_abs_pixel_error']:.6e}"
    )


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: compare_histograms.py <input.exr> <output.exr> [label]", file=sys.stderr)
        sys.exit(2)
    label = sys.argv[3] if len(sys.argv) > 3 else "round-trip"
    m = compare(sys.argv[1], sys.argv[2])
    print(format_report(label, m))
    sys.exit(0 if m["pass"] else 1)
