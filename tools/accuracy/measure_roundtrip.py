"""Round-trip accuracy + Read/Write I/O measurement for the ComfyUI-OCIO pack (2026-07-03).

Answers Sam Hodge's "how did you test accuracy?" for the DIMENSION: Round-trip A->B->A + Read/Write I/O.

Every number here is MEASURED against the shipped code path (io_nodes._convert / _save_still / _read_still),
using the shared, self-tested harness in _common.py. No placeholders.

Paths measured on the 24-patch X-Rite ColorChecker (sRGB fixture):
  1. sRGB -> ACEScg -> sRGB                      via io_nodes._convert  (in-memory, the OCIOWrite/_convert path)
  2. sRGB -> 'Linear Rec.709 (sRGB)' -> sRGB     via io_nodes._convert  (in-memory)
  3. EXR 16f I/O loop: write ColorChecker to a 16-bit-float EXR via _save_still, read back via _read_still
  4. EXR 16f full node loop: sRGB -> ACEScg -> [EXR 16f on disk] -> read -> ACEScg -> sRGB
  5. PNG 8-bit I/O loop: write sRGB ColorChecker to 8-bit PNG, read back (bit-depth bound; + a NOT-on-grid ramp)
Plus an HDR-safety probe on the EXR write/read path (negatives and super-white must survive, not clip to 0..1)
and an OCIO-parity cross-check (our _convert vs a RAW PyOpenColorIO processor) so a wiring bug cannot hide.

Run:  E:/ComfyUI/ComfyUI/ComfyUI/.venv/Scripts/python.exe measure_roundtrip.py
"""
import os
import tempfile
import numpy as np
import torch

import _common as C


def _t(rgb_np):
    """(...,3) numpy -> torch tensor shaped [N,H,W,3] the way _convert expects (N=1, H=rows, W=1)."""
    a = np.ascontiguousarray(rgb_np.astype(np.float32))
    return torch.from_numpy(a.reshape(1, -1, 1, 3))


def _np(t):
    """[1,H,1,3] torch -> (H,3) numpy."""
    return t.detach().cpu().numpy().reshape(-1, 3)


def convert(rgb_np, in_cs, out_cs, IO):
    """Run the shipped io_nodes._convert on an (N,3) sRGB-ish patch array. Returns (N,3) numpy."""
    return _np(IO._convert(_t(rgb_np), in_cs, out_cs))


def path_metrics(orig_srgb, rt_srgb):
    """max abs / RMS over linear code values + dE2000 (perceptual) between two sRGB (0..1) patch sets."""
    lab0 = C.srgb_to_lab(np.clip(orig_srgb, 0, 1))
    lab1 = C.srgb_to_lab(np.clip(rt_srgb, 0, 1))
    de = C.delta_e_2000(lab0, lab1)
    return {
        "max_abs": C.max_abs(orig_srgb, rt_srgb),
        "rms": C.rms(orig_srgb, rt_srgb),
        "psnr_db": C.psnr(orig_srgb, rt_srgb),
        "dE2000_max": float(np.max(de)),
        "dE2000_mean": float(np.mean(de)),
    }


def main():
    N, IO = C.load_pack()

    # ---- confirm the config identity live (not from memory) -------------------------------------
    cfg, cfg_key = N._resolve_config_keyed("")
    config_id = {
        "ocio_version": __import__("PyOpenColorIO").__version__,
        "config_name": cfg.getName(),
        "config_cache_key": list(cfg_key),
        "colorspace_count": len(list(cfg.getColorSpaces())),
    }

    cc = np.load(os.path.join(C.FIXT_DIR, "colorchecker_srgb.npy")).astype(np.float32)  # (24,3) sRGB 0..1
    results = {}

    # ---- self-test the dE2000 implementation so the perceptual numbers can be trusted ----------
    selftest = C._selftest()

    # ============================================================ PATH 1: sRGB->ACEScg->sRGB (mem)
    to_acescg = convert(cc, "sRGB - Display", "ACEScg", IO)
    back1 = convert(to_acescg, "ACEScg", "sRGB - Display", IO)
    results["srgb_acescg_srgb_mem"] = path_metrics(cc, back1)

    # ============================================================ PATH 2: sRGB->LinRec709->sRGB (mem)
    LIN709 = "Linear Rec.709 (sRGB)"
    to_lin = convert(cc, "sRGB - Display", LIN709, IO)
    back2 = convert(to_lin, LIN709, "sRGB - Display", IO)
    results["srgb_lin709_srgb_mem"] = path_metrics(cc, back2)

    # ============================================================ PATH 3: EXR 16f pure I/O loop
    # Write the ColorChecker straight to a 16f EXR (no color convert) and read it back. This isolates the
    # _save_still (cv2 EXR, BGR swap, IMWRITE_EXR_TYPE_HALF) <-> _read_still (cv2 UNCHANGED, BGR->RGB) round-trip:
    # what precision does a 16-bit-half EXR cost on 0..1 display values?
    tmp = tempfile.mkdtemp(prefix="ocio_rt_")
    exr_path = os.path.join(tmp, "colorchecker_16f.exr")
    cc_img = cc.reshape(24, 1, 3)                                   # (H,W,3) for _save_still
    IO._save_still(exr_path, cc_img, "exr", "16f", None, "sRGB - Display", "zip")
    read_back = IO._read_still(exr_path)[..., :3].reshape(24, 3)    # drop synthesized alpha
    results["exr16f_io_loop"] = path_metrics(cc, read_back)
    results["exr16f_io_loop"]["file_bytes"] = os.path.getsize(exr_path)

    # 32f EXR I/O loop for comparison (should be bit-exact) --------------------------------------
    exr32_path = os.path.join(tmp, "colorchecker_32f.exr")
    IO._save_still(exr32_path, cc_img, "exr", "32f", None, "sRGB - Display", "zip")
    read32 = IO._read_still(exr32_path)[..., :3].reshape(24, 3)
    results["exr32f_io_loop"] = path_metrics(cc, read32)

    # ============================================================ PATH 4: full node loop through EXR 16f
    # sRGB -> ACEScg (convert) -> write EXR 16f -> read EXR -> ACEScg -> sRGB. This is the real OCIOWrite(EXR,
    # ACEScg) then OCIORead(EXR ACEScg -> sRGB) pipeline, disk included.
    acescg_img = to_acescg.reshape(24, 1, 3)
    exr_acescg = os.path.join(tmp, "cc_acescg_16f.exr")
    IO._save_still(exr_acescg, acescg_img, "exr", "16f", None, "ACEScg", "zip")
    acescg_read = IO._read_still(exr_acescg)[..., :3].reshape(24, 3)
    back4 = convert(acescg_read, "ACEScg", "sRGB - Display", IO)
    results["node_loop_srgb_acescg_exr16f_srgb"] = path_metrics(cc, back4)

    # ============================================================ PATH 5: PNG 8-bit I/O loop (bit-depth bound)
    # NOTE: the ColorChecker fixture is authored ON the 8-bit grid (manifest: "sRGB 8-bit / 255"; measured
    # frac deviation from integers = 0.0), so THIS loop is trivially lossless (max_abs 0). That is a fixture
    # artifact, not proof the 8-bit path is exact. To report the REAL 8-bit rounding floor we also push a
    # NOT-on-grid smooth gradient (ramp2d) through the same _save_still(png,8)/_read_still loop.
    png_path = os.path.join(tmp, "colorchecker_8.png")
    IO._save_still(png_path, cc_img, "png", "8", None, "sRGB - Display", "zip")
    png_read = IO._read_still(png_path)[..., :3].reshape(24, 3)
    results["png8_io_loop"] = path_metrics(cc, png_read)
    results["png8_io_loop"]["note"] = ("ColorChecker sits on the 8-bit grid -> trivially lossless; "
                                       "see png8_io_loop_ramp for the real 8-bit floor")

    ramp2d = np.load(os.path.join(C.FIXT_DIR, "ramp2d.npy")).astype(np.float32)      # (256,1024,3) smooth 0..1
    ramp_png = os.path.join(tmp, "ramp2d_8.png")
    IO._save_still(ramp_png, ramp2d, "png", "8", None, "sRGB - Display", "zip")
    ramp_read = IO._read_still(ramp_png)[..., :3].reshape(ramp2d.shape)
    r_max = C.max_abs(ramp2d, ramp_read)
    results["png8_io_loop_ramp"] = {
        "max_abs": r_max,
        "rms": C.rms(ramp2d, ramp_read),
        "psnr_db": C.psnr(ramp2d, ramp_read),
        "one_lsb_8bit": 1.0 / 255.0,
        "within_one_lsb": bool(r_max <= 1.0 / 255.0 + 1e-9),
    }

    # ============================================================ HDR-SAFETY on the EXR write/read path
    # Feed negatives + super-white (the hdr_ramp fixture, -0.25..16) through _save_still(EXR 16f)/_read_still.
    # A scene-linear path MUST preserve these, not clamp to 0..1. Half-float tops out at 65504, so 16.0 is safe.
    hdr = np.load(os.path.join(C.FIXT_DIR, "hdr_ramp.npy")).astype(np.float32)   # (256,3) -0.25..16
    hdr_img = hdr.reshape(256, 1, 3)
    hdr_exr = os.path.join(tmp, "hdr_16f.exr")
    IO._save_still(hdr_exr, hdr_img, "exr", "16f", None, "ACEScg", "zip")
    hdr_read = IO._read_still(hdr_exr)[..., :3].reshape(256, 3)
    neg_in = float(hdr.min()); neg_out = float(hdr_read.min())
    hi_in = float(hdr.max()); hi_out = float(hdr_read.max())
    hdr_safety = {
        "input_min": neg_in, "output_min": neg_out,
        "input_max": hi_in, "output_max": hi_out,
        "negatives_preserved": bool(neg_out < -1e-4),      # not clamped to 0
        "superwhite_preserved": bool(hi_out > 1.5),        # not clamped to 1
        "max_abs_half_roundtrip": C.max_abs(hdr, hdr_read),
        "rel_err_at_16": abs(hi_out - hi_in) / max(1e-9, abs(hi_in)),
    }
    results["hdr_safety_exr16f"] = hdr_safety

    # ============================================================ OCIO PARITY (wiring cannot hide)
    # Our _convert(sRGB->ACEScg) vs a RAW PyOpenColorIO processor built fresh from the config in _common.
    ref_acescg = C.ocio_reference("sRGB - Display", "ACEScg", cc)
    parity = {
        "srgb_to_acescg_max_abs_vs_raw_ocio": C.max_abs(to_acescg, ref_acescg),
        "srgb_to_acescg_rms_vs_raw_ocio": C.rms(to_acescg, ref_acescg),
    }
    results["ocio_parity"] = parity

    # ---------------------------------------------------------------- chart: round-trip max err per path
    order = [
        ("sRGB->ACEScg->sRGB\n(mem)", "srgb_acescg_srgb_mem"),
        ("sRGB->LinRec709->sRGB\n(mem)", "srgb_lin709_srgb_mem"),
        ("EXR 32f I/O", "exr32f_io_loop"),
        ("EXR 16f I/O", "exr16f_io_loop"),
        ("node loop\nsRGB<->ACEScg via EXR16f", "node_loop_srgb_acescg_exr16f_srgb"),
        ("PNG 8-bit I/O\n(ramp, not-on-grid)", "png8_io_loop_ramp"),
    ]
    labels = [o[0] for o in order]
    maxerr = [results[o[1]]["max_abs"] for o in order]
    # dE2000 only exists for the sRGB-domain paths; the ramp path reports raw code error instead.
    de = [results[o[1]].get("dE2000_max", float("nan")) for o in order]

    fig, (ax1, ax2) = C.new_fig(1, 2, figsize=(12, 4.6))
    xs = np.arange(len(labels))
    b1 = ax1.bar(xs, [max(v, 1e-9) for v in maxerr], color="#5ad", width=0.62)
    ax1.set_yscale("log")
    ax1.set_ylim(1e-9, 1.0)
    ax1.set_ylabel("round-trip max abs error (code value, log)")
    ax1.set_title("Round-trip max error per path (ColorChecker, 24 patches)")
    ax1.set_xticks(xs); ax1.set_xticklabels(labels, rotation=30, ha="right", fontsize=7)
    ax1.axhline(1.0 / 255.0, color="#f80", ls="--", lw=1.0)
    ax1.text(len(labels) - 0.5, 1.0 / 255.0 * 1.2, "1 LSB @ 8-bit", color="#f80", ha="right", fontsize=7)
    ax1.axhline(2 ** -11, color="#8f8", ls=":", lw=1.0)
    ax1.text(0.0, 2 ** -11 * 1.2, "half-float step near 1.0 (~2^-11)", color="#8f8", ha="left", fontsize=7)
    for r, v in zip(b1, maxerr):
        ax1.text(r.get_x() + r.get_width() / 2, max(v, 1e-9) * 1.5, f"{v:.1e}", ha="center",
                 fontsize=6.5, color="#dde")

    de_plot = [0.0 if (v != v) else v for v in de]
    b2 = ax2.bar(xs, de_plot, color="#c9a", width=0.62)
    ax2.set_ylabel("round-trip dE2000 max (perceptual)")
    ax2.set_title("Round-trip perceptual error (dE2000; ramp path n/a)")
    ax2.set_xticks(xs); ax2.set_xticklabels(labels, rotation=30, ha="right", fontsize=7)
    ax2.axhline(1.0, color="#f80", ls="--", lw=1.0)
    ax2.text(0.0, 1.02, "dE2000 = 1 (JND)", color="#f80", ha="left", fontsize=7)
    ymax = max([v for v in de_plot] + [1.0])
    for r, v, raw in zip(b2, de_plot, de):
        txt = "n/a" if (raw != raw) else f"{v:.2e}"
        ax2.text(r.get_x() + r.get_width() / 2, v + ymax * 0.02 + 1e-6, txt, ha="center",
                 fontsize=6.5, color="#dde")

    fig.suptitle("ComfyUI-OCIO round-trip + Read/Write I/O accuracy  (OCIO 2.5.2 ACES studio-config)",
                 color="#e8ecff", fontsize=11)
    plot_path = C.savefig(fig, "roundtrip.png")

    out = {
        "config": config_id,
        "deltaE2000_selftest": selftest,
        "paths": results,
        "plot": plot_path,
    }
    C.dump("roundtrip.json", out)

    # ---- console summary --------------------------------------------------------------------------
    print("=== config ===")
    for k, v in config_id.items():
        print(f"  {k}: {v}")
    print(f"  dE2000 selftest ok: {selftest['deltaE2000_selftest_ok']} (worst {selftest['worst_abs_err_vs_sharma']:.2e})")
    print("\n=== round-trip paths (ColorChecker unless noted) ===")
    for lbl, key in order:
        m = results[key]
        de_s = f"dE2000_max={m['dE2000_max']:.3e}  " if "dE2000_max" in m else "dE2000_max=n/a       "
        print(f"  {key:34s} max_abs={m['max_abs']:.3e}  rms={m['rms']:.3e}  {de_s}psnr={m['psnr_db']:.1f}dB")
    print("\n=== OCIO parity (our _convert vs RAW PyOpenColorIO) ===")
    print(f"  sRGB->ACEScg max_abs vs raw: {parity['srgb_to_acescg_max_abs_vs_raw_ocio']:.3e}  "
          f"rms: {parity['srgb_to_acescg_rms_vs_raw_ocio']:.3e}")
    print("\n=== HDR safety (EXR 16f write/read, hdr_ramp -0.25..16) ===")
    for k, v in hdr_safety.items():
        print(f"  {k}: {v}")
    print(f"\nplot: {plot_path}")
    print(f"json: {os.path.join(C.HERE, 'results', 'roundtrip.json')}")


if __name__ == "__main__":
    main()
