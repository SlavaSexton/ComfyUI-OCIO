"""Quantisation / dithering audit of io_nodes._save_still (Sam Hodge's direct ask 2026-07-03).

Sam: "did you check colour dithering/quantisation?" This MEASURES the shipped float->file write path.

We write the smooth ramp2d and ramp_linear fixtures through the REAL io_nodes._save_still at:
  - PNG  8-bit  (io_nodes.py:594  arr8 = (clip(rgb,0,1)*255).astype(uint8)         -- TRUNCATING cast, PIL)
  - PNG  16-bit (io_nodes.py:592  ( ...*65535).astype(uint16)  via cv2             -- TRUNCATING cast)
  - EXR  16f    (io_nodes.py:563  cv2.IMWRITE_EXR_TYPE_HALF, no clamp on rgb)       -- IEEE half float

and measure the WRITTEN FILE two ways so a read-back bug cannot hide a write result or vice-versa:
  (A) PACK read-back  : io_nodes._read_still  -- the path OCIORead actually uses in-graph
  (B) TRUE-FILE read  : cv2.IMREAD_UNCHANGED  -- what the bytes on disk really hold
Metrics per read:
  - quantisation RMS / max error vs float, PSNR
  - banding: distinct output levels, longest identical run in px (= band width) on the 1-D ramp
  - dithering test: is the written 8-bit data identical to a NAIVE quantiser? If yes -> NO dithering.
    We test BOTH candidate quantisers because the harness models np.round but the code TRUNCATES:
      round model     : np.round(clip*255)/255      (harness _common.quantise)
      truncate model  : np.floor(clip*255)/255      (what .astype(uint8) actually does on x*255)

Run with:  E:/ComfyUI/ComfyUI/ComfyUI/.venv/Scripts/python.exe measure_quantisation_dither.py
"""
import os
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")   # must precede cv2 import (the ComfyUI server sets
                                                          # this on launch; io_nodes.py:17 documents it)
import tempfile
import numpy as np
import cv2

import _common as C

N, IO = C.load_pack()

FIXT = C.FIXT_DIR
OUTDIR = tempfile.mkdtemp(prefix="ocio_quant_")


def _truefile_read(path, fmt):
    """Read the file the way its full bit-depth is preserved (independent of the pack's _read_still):
    cv2.IMREAD_UNCHANGED, then BGR->RGB, normalise integer formats to 0..1. This is the ground-truth of
    what the WRITE actually put on disk."""
    a = cv2.imread(path, cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH)
    a = a.astype(np.float64)
    if a.ndim == 2:
        a = np.stack([a] * 3, -1)
    rgb = a[..., [2, 1, 0]] if a.shape[2] >= 3 else np.repeat(a[..., :1], 3, 2)
    if fmt in ("png", "tiff") and rgb.max() > 1.5:
        rgb = rgb / (65535.0 if rgb.max() > 255.0 else 255.0)
    return rgb


def _write(rgb01, fmt, bit_depth, name):
    ext = {"png": "png", "exr": "exr", "tiff": "tif"}[fmt]
    path = os.path.join(OUTDIR, f"{name}.{ext}")
    IO._save_still(path, np.ascontiguousarray(rgb01.astype(np.float32)), fmt, bit_depth,
                   alpha=None, colorspace=None, compression="zip")
    return path


def _levels_and_run(vals_1d, bits):
    """distinct integer levels + longest identical run on the ACTUAL 1-D read-back signal, snapped to the
    format's integer grid so fp noise doesn't inflate 'distinct'."""
    maxv = (1 << bits) - 1
    qi = np.round(np.clip(vals_1d, 0.0, 1.0) * maxv).astype(np.int64)
    distinct = int(np.unique(qi).size)
    run = best = 1
    for i in range(1, qi.size):
        run = run + 1 if qi[i] == qi[i - 1] else 1
        best = max(best, run)
    return distinct, int(best)


def _dither_probe(src_row, file_row, bits):
    """Is the written data identical to a naive deterministic quantiser? Compare the integer codes the file
    stores against round() and floor() models of the same source. Identical to either => NO dithering."""
    maxv = (1 << bits) - 1
    file_codes = np.round(np.clip(file_row, 0.0, 1.0) * maxv).astype(np.int64)
    round_codes = np.round(np.clip(src_row, 0.0, 1.0) * maxv).astype(np.int64)
    floor_codes = np.floor(np.clip(src_row, 0.0, 1.0) * maxv).astype(np.int64)
    return {
        "matches_round_model": bool(np.array_equal(file_codes, round_codes)),
        "matches_floor_truncate_model": bool(np.array_equal(file_codes, floor_codes)),
        "codes_differ_from_round_count": int(np.sum(file_codes != round_codes)),
        "codes_differ_from_floor_count": int(np.sum(file_codes != floor_codes)),
        "deviates_from_both_models_count":
            int(np.sum((file_codes != round_codes) & (file_codes != floor_codes))),
    }


def _metrics(src_img, back, src_row, back_row, bits):
    m = {
        "rms_vs_float": C.rms(src_img, back),
        "max_abs_vs_float": C.max_abs(src_img, back),
        "psnr_db": C.psnr(src_img, back),
        "readback_min": float(back.min()),
        "readback_max": float(back.max()),
    }
    if bits is not None:
        distinct, run = _levels_and_run(back_row, bits)
        m["distinct_levels"] = distinct
        m["ideal_levels"] = 1 << bits
        m["longest_flat_run_px"] = run
        m["dither"] = _dither_probe(src_row, back_row, bits)
    else:
        uniq = int(np.unique(np.float32(back_row)).size)
        m["distinct_float_values_in_row"] = uniq
        m["row_length_px"] = int(back_row.size)
        d = np.diff(np.float32(back_row).astype(np.float64))
        nz = d[d > 0]
        m["smallest_positive_step"] = float(nz.min()) if nz.size else 0.0
    return m


def main():
    ramp2d = np.load(os.path.join(FIXT, "ramp2d.npy")).astype(np.float64)            # [256,1024,3]
    ramp_linear = np.load(os.path.join(FIXT, "ramp_linear.npy")).astype(np.float64)  # [2048]
    ramp1_img = np.repeat(ramp_linear[None, :, None], 3, axis=2)                      # [1,2048,3]

    results = {"write_path_source_lines": {
        "png8": "io_nodes.py:594  (np.clip(rgb,0,1)*255).astype(np.uint8)  [PIL, truncating cast]",
        "png16": "io_nodes.py:592  (data*65535).astype(np.uint16)  [cv2, truncating cast]",
        "exr16f": "io_nodes.py:563  cv2.IMWRITE_EXR_TYPE_HALF  [IEEE half, no clip]",
    }, "readback_note": {
        "pack": "io_nodes._read_still - the in-graph OCIORead path",
        "truefile": "cv2.IMREAD_UNCHANGED - the actual bytes on disk",
    }, "output_dir": OUTDIR, "fixtures": {}}

    for fname, img in [("ramp2d", ramp2d), ("ramp_linear", ramp1_img)]:
        src_row = img[0, :, 0]
        entry = {}
        for tag, fmt, bits, bitdepth in [("png8", "png", 8, "8"),
                                         ("png16", "png", 16, "16"),
                                         ("exr16f", "exr", None, "16f")]:
            path = _write(img, fmt, bitdepth, f"{fname}_{tag}")
            # (A) pack read-back (what OCIORead gives you back in-graph)
            pack_back = IO._read_still(path)[..., :3].astype(np.float64)
            # (B) true-file read (what the write actually stored on disk)
            true_back = _truefile_read(path, fmt)
            entry[tag] = {
                "pack_readback": _metrics(img, pack_back, src_row, pack_back[0, :, 0], bits),
                "truefile": _metrics(img, true_back, src_row, true_back[0, :, 0], bits),
            }
        entry["png16_readback_bug"] = {
            "pack_distinct_levels": entry["png16"]["pack_readback"].get("distinct_levels"),
            "truefile_distinct_levels": entry["png16"]["truefile"].get("distinct_levels"),
        }
        results["fixtures"][fname] = entry

    results["harness_model_banding"] = {
        "ramp_linear_8bit": C.banding_metrics(ramp_linear, 8),
        "ramp_linear_16bit": C.banding_metrics(ramp_linear, 16),
    }

    _make_chart(ramp2d, results)
    C.dump("quantisation_dither.json", results)

    r = results["fixtures"]["ramp2d"]
    print("\n=== QUANTISATION / DITHER: ramp2d (smooth grey, 1024 px wide) ===")
    print("(truefile = bytes on disk;  pack = io_nodes._read_still round-trip)\n")
    for tag in ("png8", "png16", "exr16f"):
        for which in ("truefile", "pack_readback"):
            m = r[tag][which]
            line = f"{tag:7s} [{which:13s}] rms={m['rms_vs_float']:.6e} max={m['max_abs_vs_float']:.6e} psnr={m['psnr_db']:6.2f}dB"
            if "longest_flat_run_px" in m:
                line += f" distinct={m['distinct_levels']:5d}/{m['ideal_levels']} band={m['longest_flat_run_px']}px"
                d = m["dither"]
                line += f" | dither round={d['matches_round_model']} floor={d['matches_floor_truncate_model']}"
            else:
                line += f" distinct_floats={m['distinct_float_values_in_row']}/{m['row_length_px']} min_step={m['smallest_positive_step']:.2e}"
            print(line)
        print()
    bug = r["png16_readback_bug"]
    print(f"PNG16 read-back bug: pack sees {bug['pack_distinct_levels']} levels, disk holds {bug['truefile_distinct_levels']}.")
    print("plot + json written.")


def _make_chart(ramp2d, results):
    import matplotlib
    matplotlib.use("Agg")
    fig, axes = C.new_fig(2, 2, figsize=(11, 7))

    W = ramp2d.shape[1]
    dark_n = int(W * 0.06)
    src_dark = ramp2d[0, :dark_n, 0]
    q8 = C.quantise(src_dark, 8)
    q16 = C.quantise(src_dark, 16)
    strip8 = np.tile(q8[None, :], (28, 1))
    strip16 = np.tile(q16[None, :], (28, 1))
    strip_src = np.tile(src_dark[None, :], (10, 1))
    stack = np.vstack([strip_src, np.full((3, dark_n), np.nan), strip8,
                       np.full((3, dark_n), np.nan), strip16])
    ax = axes[0, 0]
    ax.imshow(stack, cmap="gray", aspect="auto", vmin=0, vmax=src_dark.max(), interpolation="nearest")
    ax.set_title("Dark end of gradient (0..%.3f): float / 8-bit / 16-bit" % float(src_dark.max()))
    ax.set_yticks([5, 24, 55]); ax.set_yticklabels(["float", "8-bit", "16-bit"])
    ax.set_xlabel("pixel (darkest %d of %d)" % (dark_n, W))

    ax = axes[0, 1]
    x = np.arange(dark_n)
    ax.plot(x, src_dark, color="#8fd", lw=1.2, label="float source")
    ax.plot(x, q8, color="#f77", lw=1.0, drawstyle="steps-post", label="8-bit (round)")
    ax.plot(x, q16, color="#7af", lw=0.8, drawstyle="steps-post", label="16-bit")
    ax.set_title("Quantisation staircase, dark end")
    ax.set_xlabel("pixel"); ax.set_ylabel("code value (0..1)")
    ax.legend(loc="upper left", fontsize=7)

    ax = axes[1, 0]
    r2 = results["fixtures"]["ramp2d"]["png8"]["truefile"]
    src_row = ramp2d[0, :, 0]
    err8 = (np.floor(np.clip(src_row, 0, 1) * 255) / 255.0 - src_row)   # TRUNCATE model = the real png8 error
    ax.hist(err8 * 255.0, bins=61, color="#f89", edgecolor="#733")
    ax.set_title("8-bit quantisation error histogram (truncate, ramp2d row)")
    ax.set_xlabel("error in 8-bit codes (1 code = 1/255)"); ax.set_ylabel("pixels")
    ax.text(0.02, 0.95, "rms=%.3e\nmax=%.3e" % (r2["rms_vs_float"], r2["max_abs_vs_float"]),
            transform=ax.transAxes, va="top", fontsize=8, color="#fbb")

    ax = axes[1, 1]
    r2d = results["fixtures"]["ramp2d"]
    tags = ["png8", "png16"]
    bands = [r2d[t]["truefile"]["longest_flat_run_px"] for t in tags]
    exr_step = r2d["exr16f"]["truefile"]["smallest_positive_step"]
    ax.bar(tags, bands, color=["#f77", "#7af"])
    for i, b in enumerate(bands):
        ax.text(i, b, str(b), ha="center", va="bottom", color="#eee", fontsize=9)
    ax.set_title("Longest flat run (band width) in px, TRUE file  [EXR16f step=%.1e]" % exr_step)
    ax.set_ylabel("px per band (higher = more visible banding)")

    fig.suptitle("io_nodes._save_still quantisation / dithering  (no dither: file == deterministic quantiser)",
                 color="#e8ecff", fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    p = C.savefig(fig, "quantisation_dither.png")
    results["plot_path"] = p
    print("chart ->", p)


if __name__ == "__main__":
    main()
