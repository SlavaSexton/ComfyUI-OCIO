"""Perceptual dE2000 on the 24 X-Rite ColorChecker patches (OCIO color-accuracy audit, 2026-07-03).

Answers the reviewer (external review): "how did you test accuracy?" with dE2000 MEASUREMENTS, not vibes.

Two measurements, both through the SHIPPED pack path (io_nodes._convert), scored in CIELAB dE2000:

  (a) ROUND-TRIP identity:  sRGB - Display -> ACEScg -> sRGB - Display.
      A lossless round-trip should land back on the input; dE2000 well under 1.0 (JND) = imperceptible.

  (b) FORWARD PARITY vs raw OCIO: our io_nodes._convert(sRGB -> ACEScg) vs _common.ocio_reference
      (a RAW PyOpenColorIO CPU processor built independently of the wrapper). Both legs land in ACEScg,
      which is scene-linear, NOT an sRGB display space -- so we cannot Lab-score ACEScg directly. Instead
      we bring BOTH ACEScg results back to sRGB - Display with the SAME raw-OCIO reference leg, then Lab-
      score. Any dE here is a wrapper/wiring discrepancy, since the ground-truth return leg is identical.

_convert wants a [N,H,W,3] torch tensor (io_nodes.py:676 -> nodes.py:_apply_processor unpacks b,h,w,c),
so the 24 patches are shaped [1,24,1,3] -- the real batched-image layout the node runs on.

Run:  E:/ComfyUI/ComfyUI/ComfyUI/.venv/Scripts/python.exe measure_deltaE_colorchecker.py
"""
import os
import json
import numpy as np
import torch

import _common as C

IN_CS = "sRGB - Display"
OUT_CS = "ACEScg"
JND = 1.0   # 1.0 dE2000 ~ just-noticeable difference


def _patch_names():
    """The 24 patch names, in fixture row order, from the fixtures manifest."""
    mani = os.path.join(C.FIXT_DIR, "manifest.json")
    with open(mani, "r", encoding="utf-8") as f:
        return json.load(f)["colorchecker_srgb.npy"]["names"]


def _as_batch(rgb_24x3):
    """[24,3] float -> [1,24,1,3] torch tensor (N=1, H=24, W=1), the layout _convert/_apply_processor run on."""
    a = np.ascontiguousarray(rgb_24x3.astype(np.float32)).reshape(1, 24, 1, 3)
    return torch.from_numpy(a)


def _from_batch(t):
    """[1,24,1,3] torch tensor -> [24,3] numpy."""
    return t.detach().cpu().numpy().reshape(24, 3)


def main():
    N, IO = C.load_pack()

    # config identity, confirmed live (not from memory) so the reviewer can trust the ground truth
    cfg, cfg_key = N._resolve_config_keyed("")
    cs_names = [cs.getName() for cs in cfg.getColorSpaces()]
    config_id = {"name": cfg.getName(), "resolved_via": list(cfg_key),
                 "colorspace_count": len(cs_names),
                 "has_in_cs": IN_CS in cs_names, "has_out_cs": OUT_CS in cs_names}

    names = _patch_names()
    src = np.load(os.path.join(C.FIXT_DIR, "colorchecker_srgb.npy")).astype(np.float32)  # [24,3] sRGB 0..1
    assert src.shape == (24, 3), src.shape

    # ---- (a) round-trip through the SHIPPED node path: sRGB -> ACEScg -> sRGB
    mid_acescg = IO._convert(_as_batch(src), IN_CS, OUT_CS)         # leg 1 (real wrapper)
    rt = _from_batch(IO._convert(mid_acescg, OUT_CS, IN_CS))        # leg 2 (real wrapper)

    lab_src = C.srgb_to_lab(src)
    lab_rt = C.srgb_to_lab(rt)
    dE_rt = C.delta_e_2000(lab_src, lab_rt)                         # [24]

    # ---- (b) forward parity: our _convert(sRGB->ACEScg) vs raw-OCIO reference, both returned to sRGB
    #      by the SAME raw reference leg so only the forward leg differs.
    ours_acescg = _from_batch(mid_acescg)                          # our wrapper's forward result
    ref_acescg = C.ocio_reference(IN_CS, OUT_CS, src)              # raw PyOpenColorIO forward result
    # bring both back to sRGB - Display via the raw reference (identical return leg -> isolates the forward leg)
    ours_back = C.ocio_reference(OUT_CS, IN_CS, ours_acescg)
    ref_back = C.ocio_reference(OUT_CS, IN_CS, ref_acescg)
    dE_fwd = C.delta_e_2000(C.srgb_to_lab(ours_back), C.srgb_to_lab(ref_back))   # [24]

    # raw ACEScg diff (bit-level) between our forward leg and the raw reference, for the record
    fwd_max_abs = C.max_abs(ours_acescg, ref_acescg)
    fwd_rms = C.rms(ours_acescg, ref_acescg)

    per_patch = []
    for i, nm in enumerate(names):
        per_patch.append({
            "name": nm,
            "srgb_in": [round(float(x), 6) for x in src[i]],
            "acescg": [round(float(x), 6) for x in ours_acescg[i]],
            "srgb_roundtrip": [round(float(x), 6) for x in rt[i]],
            "dE2000_roundtrip": float(dE_rt[i]),
            "dE2000_forward_vs_raw": float(dE_fwd[i]),
        })

    def stats(arr):
        return {"max": float(np.max(arr)), "mean": float(np.mean(arr)),
                "median": float(np.median(arr)), "min": float(np.min(arr)),
                "patches_over_1_JND": int(np.sum(arr > JND)),
                "worst_patch": names[int(np.argmax(arr))]}

    rt_stats = stats(dE_rt)
    fwd_stats = stats(dE_fwd)

    results = {
        "dimension": "Perceptual dE2000 on the ColorChecker",
        "config": config_id,
        "in_cs": IN_CS, "out_cs": OUT_CS,
        "n_patches": 24,
        "roundtrip": {"transform": f"{IN_CS} -> {OUT_CS} -> {IN_CS} (via io_nodes._convert, both legs)",
                      "dE2000": rt_stats},
        "forward_vs_raw_ocio": {
            "transform": f"io_nodes._convert({IN_CS}->{OUT_CS}) vs _common.ocio_reference, returned to {IN_CS}",
            "acescg_raw_diff": {"max_abs": fwd_max_abs, "rms": fwd_rms},
            "dE2000": fwd_stats},
        "deltaE2000_selftest": C._selftest(),
        "per_patch": per_patch,
    }

    # -------------------------------------------------------------- chart: per-patch dE2000 bar, JND line
    fig, ax = C.new_fig(figsize=(13, 5.2))
    x = np.arange(24)
    w = 0.42
    b1 = ax.bar(x - w / 2, dE_rt, w, label="round-trip sRGB->ACEScg->sRGB", color="#4fc3f7")
    b2 = ax.bar(x + w / 2, dE_fwd, w, label="forward vs raw OCIO", color="#ba9cff")
    ax.axhline(JND, color="#ff6e6e", ls="--", lw=1.4, label="1.0 dE2000 (JND, imperceptible below)")
    ax.set_xticks(x)
    ax.set_xticklabels(names, rotation=55, ha="right", fontsize=7)
    ax.set_ylabel("dE2000")
    ax.set_title("ColorChecker 24-patch perceptual error  (ComfyUI-OCIO, ACES studio-config v2.0, OCIO 2.5.2)")
    ymax = max(float(np.max(dE_rt)), float(np.max(dE_fwd)), JND) * 1.35 + 1e-6
    ax.set_ylim(0, ymax)
    ax.legend(loc="upper right", fontsize=8, framealpha=0.15)
    ax.grid(axis="y", alpha=0.25)
    # annotate the round-trip max so the headline number is legible on the chart
    wi = int(np.argmax(dE_rt))
    ax.annotate(f"max RT {dE_rt[wi]:.2e} dE", (wi - w / 2, dE_rt[wi]),
                textcoords="offset points", xytext=(0, 8), fontsize=7, color="#4fc3f7", ha="center")
    plot_path = C.savefig(fig, "deltaE_colorchecker.png")

    dump_path = C.dump("deltaE_colorchecker.json", results)

    print(json.dumps({
        "config": config_id,
        "roundtrip_dE2000": rt_stats,
        "forward_vs_raw_dE2000": fwd_stats,
        "forward_acescg_raw_diff": {"max_abs": fwd_max_abs, "rms": fwd_rms},
        "plot_path": plot_path,
        "json_path": dump_path,
    }, indent=2))


if __name__ == "__main__":
    main()
