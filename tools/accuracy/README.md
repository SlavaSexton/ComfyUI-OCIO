# Color-accuracy regression suite

Measured, not vibe-coded. Every color transform in this pack is checked against an **independent**
PyOpenColorIO reference and against published specs, so accuracy is a number, not a guess.

## Run it

```bash
# from the repo root, with the ComfyUI venv python (needs OPENCV_IO_ENABLE_OPENEXR=1):
python tools/accuracy/gen_fixtures.py          # deterministic test material -> fixtures/
python tools/accuracy/measure_ocio_parity.py   # (and the other measure_*.py)
```

Each `measure_*.py` imports `_common.py` (the shared harness), writes a chart to `plots/` and raw
metrics to `results/`. `fixtures/`, `plots/`, `results/` are generated and git-ignored.

## What is measured

| Script | Checks |
|--------|--------|
| `measure_ocio_parity.py`        | our node output vs a raw PyOpenColorIO CPU processor (bit-exact), + a mutation control that must fail |
| `measure_log_curves.py`         | 10 hand-rolled log curves: round-trip, published vendor-spec anchors, C1 continuity, HDR-safety |
| `measure_quantisation_dither.py`| float -> 8/16-bit/EXR write + read-back: banding, dithering, quantisation RMS (the reviewer's direct ask) |
| `measure_roundtrip.py`          | A->B->A + real EXR/PNG write/read loops, ΔE2000 |
| `measure_hdr_safety.py`         | negatives and values > 1 survive conversions/curves/grade (silent-clamp count) |
| `measure_histogram_compare.py`  | `cv2.compareHist` (Correlation / Chi-Square / Bhattacharyya) ours vs reference |
| `measure_deltaE_colorchecker.py`| perceptual ΔE2000 on the 24 X-Rite patches vs a 1.0 JND |
| `measure_gamut_volume.py`       | RGB gamut volume of sRGB / ACEScg / ACES2065-1 / ARRI Wide Gamut 3, as 3D hulls in CIE Lab via OCIO's own XYZ-D65 interchange - why sRGB isn't enough |

## The harness (`_common.py`)

`load_pack()` imports the shipped pack standalone · `ocio_reference()` is the independent OCIO ground
truth · error metrics (max-abs / RMS / PSNR) · sRGB↔Lab + ΔE76 + ΔE2000 (self-tested against the
published Sharma 2005 pairs, worst error 4e-5) · `hist_compare()` (cv2.compareHist) · banding /
quantisation metrics · matplotlib chart helpers.

First measurement pass 2026-07-03. It caught three real defects (16-bit PNG read-back, LogC3/Cineon
below-black toe, integer-write round-vs-floor), all fixed and re-confirmed by re-running these scripts.
