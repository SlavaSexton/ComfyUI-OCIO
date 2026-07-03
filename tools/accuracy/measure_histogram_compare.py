"""Histogram comparison via cv2.compareHist - the exact tool Sam Hodge linked.

Answers the review question "did you check colour dithering/quantisation? ... if you don't measure it's only a
guess" with HIS OWN method: per-channel cv2.compareHist (Correlation / Chi-Square / Intersection / Bhattacharyya)
of OUR node output vs an INDEPENDENT raw-PyOpenColorIO reference.

Three panels, four metrics per channel each:
  A) nyc_skyline sRGB -> ACEScg -> sRGB - Display : ours (_convert chain) vs _common.ocio_reference (raw C++)
  B) spectral_sweep sRGB -> ACEScg -> sRGB - Display : ours vs reference (many-hue stress)
  C) nyc_skyline sRGB -> ACEScg -> sRGB  round-trip : ours vs the ORIGINAL sRGB image (does the pipeline
     preserve the picture? Correlation ~1.0, Bhattacharyya ~0.0 = yes)

Ours is io_nodes._convert(tensor[N,H,W,3], in_cs, out_cs) (io_nodes.py:676 -> nodes._apply_processor,
PackedImageDesc). Reference is _common.ocio_reference (a raw processor built fresh from the same config, so a
wiring bug in the node path cannot hide). Panels A/B compare distributions of two DIFFERENT ways to reach the
same transform; near-identical is the correct result. Panel C compares an image to its own round-trip.

Run:  E:/ComfyUI/ComfyUI/ComfyUI/.venv/Scripts/python.exe measure_histogram_compare.py
"""
import os
import numpy as np
import torch

import _common as C

IN_CS = "sRGB - Display"      # ComfyUI working space (the file's space for a PNG); confirmed io_nodes.WORKING
MID_CS = "ACEScg"             # ACES working space
DISP_CS = "sRGB - Display"    # the display target Sam's "->(display)" refers to; confirmed in config (query above)

NYC = os.path.join(os.path.dirname(C.__file__), "..", "..", "example_workflows", "nyc_skyline.png")
BINS = 256
RNG = (0.0, 1.0)


def load_nyc():
    """nyc_skyline.png -> float32 RGB [1,H,W,3] in 0..1, exactly how ComfyUI LoadImage feeds it (x/255, no
    linearisation - it is already display sRGB)."""
    from PIL import Image
    im = Image.open(NYC).convert("RGB")
    a = np.asarray(im, np.float32) / 255.0
    return a[None]   # [1,H,W,3]


def ours_convert(img_nhwc, in_cs, out_cs):
    """OUR shipped node path: io_nodes._convert on a torch tensor [N,H,W,3]. Returns numpy [N,H,W,3]."""
    _, IO = C.load_pack()
    t = torch.from_numpy(np.ascontiguousarray(img_nhwc.astype(np.float32)))
    out = IO._convert(t, in_cs, out_cs)
    return out.detach().cpu().numpy()


def ours_chain(img_nhwc, a_cs, b_cs, c_cs):
    """OUR node path, two hops: a->b then b->c (sRGB -> ACEScg -> display)."""
    step1 = ours_convert(img_nhwc, a_cs, b_cs)
    step2 = ours_convert(step1, b_cs, c_cs)
    return step2


def ref_chain(img_nhwc, a_cs, b_cs, c_cs):
    """INDEPENDENT reference, two hops via raw PyOpenColorIO (fresh processor each hop)."""
    step1 = C.ocio_reference(a_cs, b_cs, img_nhwc)
    step2 = C.ocio_reference(b_cs, c_cs, step1)
    return step2


def main():
    print("[selftest]", C._selftest())
    nyc = load_nyc()
    sweep = np.load(os.path.join(C.FIXT_DIR, "spectral_sweep.npy")).astype(np.float32)[None]  # [1,H,W,3]
    print(f"nyc {nyc.shape}  spectral_sweep {sweep.shape}")

    panels = {}

    # A) nyc: sRGB -> ACEScg -> sRGB-Display, ours vs raw reference
    a_ours = ours_chain(nyc, IN_CS, MID_CS, DISP_CS)
    a_ref = ref_chain(nyc, IN_CS, MID_CS, DISP_CS)
    panels["nyc_srgb->acescg->display"] = {
        "ours": a_ours, "ref": a_ref, "kind": "ours_vs_reference",
        "hist": C.hist_compare(a_ours, a_ref, BINS, RNG),
        "max_abs_pixel_diff": C.max_abs(a_ours, a_ref),
        "psnr_db": C.psnr(np.clip(a_ours, 0, 1), np.clip(a_ref, 0, 1)),
    }

    # B) spectral_sweep: sRGB -> ACEScg -> sRGB-Display, ours vs raw reference
    b_ours = ours_chain(sweep, IN_CS, MID_CS, DISP_CS)
    b_ref = ref_chain(sweep, IN_CS, MID_CS, DISP_CS)
    panels["spectral_srgb->acescg->display"] = {
        "ours": b_ours, "ref": b_ref, "kind": "ours_vs_reference",
        "hist": C.hist_compare(b_ours, b_ref, BINS, RNG),
        "max_abs_pixel_diff": C.max_abs(b_ours, b_ref),
        "psnr_db": C.psnr(np.clip(b_ours, 0, 1), np.clip(b_ref, 0, 1)),
    }

    # C) nyc round-trip: sRGB -> ACEScg -> sRGB, OUR path, vs the ORIGINAL image
    rt_ours = ours_chain(nyc, IN_CS, MID_CS, IN_CS)
    panels["nyc_roundtrip_vs_original"] = {
        "ours": rt_ours, "ref": nyc, "kind": "roundtrip_vs_original",
        "hist": C.hist_compare(rt_ours, nyc, BINS, RNG),
        "max_abs_pixel_diff": C.max_abs(rt_ours, nyc),
        "psnr_db": C.psnr(np.clip(rt_ours, 0, 1), np.clip(nyc, 0, 1)),
    }

    # ---- console table + json payload
    payload = {"config": None, "in_cs": IN_CS, "mid_cs": MID_CS, "display_cs": DISP_CS,
               "bins": BINS, "range": list(RNG), "panels": {}}
    N, _ = C.load_pack()
    cfg, _ = N._resolve_config_keyed("")
    payload["config"] = {"name": cfg.getName(), "num_colorspaces": len(list(cfg.getColorSpaces())),
                         "ocio_version": __import__("PyOpenColorIO").__version__}

    for pname, p in panels.items():
        payload["panels"][pname] = {"kind": p["kind"], "hist": p["hist"],
                                    "max_abs_pixel_diff": p["max_abs_pixel_diff"], "psnr_db": p["psnr_db"]}
        print(f"\n=== {pname}  ({p['kind']})  max|dpix|={p['max_abs_pixel_diff']:.3e}  PSNR={p['psnr_db']:.2f} dB")
        print(f"    {'ch':>3} {'correlation':>12} {'chi_square':>12} {'intersection':>13} {'bhattacharyya':>14}")
        for ch in "RGB":
            h = p["hist"][ch]
            print(f"    {ch:>3} {h['correlation']:>12.6f} {h['chi_square']:>12.6f} "
                  f"{h['intersection']:>13.6f} {h['bhattacharyya']:>14.6f}")

    C.dump("histogram_compare.json", payload)

    # ---- chart: per-panel, per-channel histogram overlays + a score table
    plot_path = make_chart(panels)
    print(f"\nchart -> {plot_path}")
    return payload, plot_path


def make_chart(panels):
    import matplotlib
    matplotlib.use("Agg")
    fig, axes = C.new_fig(3, 4, figsize=(15.5, 9.5))
    pnames = list(panels.keys())
    ch_colors = {"R": "#ff5566", "G": "#44dd77", "B": "#5599ff"}

    for r, pname in enumerate(pnames):
        p = panels[pname]
        ours, ref = p["ours"], p["ref"]
        lbl_ref = "original" if p["kind"] == "roundtrip_vs_original" else "raw OCIO ref"
        for ci, ch in enumerate("RGB"):
            ax = axes[r, ci]
            oa = np.clip(ours[..., ci].reshape(-1), 0, 1)
            rb = np.clip(ref[..., ci].reshape(-1), 0, 1)
            bins = np.linspace(0, 1, BINS + 1)
            ax.hist(rb, bins=bins, histtype="stepfilled", color=ch_colors[ch], alpha=0.30, label=lbl_ref)
            ax.hist(oa, bins=bins, histtype="step", color=ch_colors[ch], lw=1.3, label="ours")
            ax.set_yscale("log")
            corr = p["hist"][ch]["correlation"]
            bh = p["hist"][ch]["bhattacharyya"]
            ax.set_title(f"{ch}  corr={corr:.5f}  bhat={bh:.4f}", fontsize=8)
            if ci == 0:
                ax.set_ylabel(pname.replace("_", " ").replace("->", "->\n"), fontsize=7.5)
            if r == 0 and ci == 0:
                ax.legend(fontsize=7, loc="upper right", framealpha=0.2)
            ax.tick_params(labelsize=6)

        # 4th column: the compareHist score table for this panel
        ax = axes[r, 3]
        ax.axis("off")
        rows = [["ch", "corr", "chiSq", "inter", "bhat"]]
        for ch in "RGB":
            h = p["hist"][ch]
            rows.append([ch, f"{h['correlation']:.5f}", f"{h['chi_square']:.4f}",
                         f"{h['intersection']:.4f}", f"{h['bhattacharyya']:.4f}"])
        rows.append(["max|dpix|", f"{p['max_abs_pixel_diff']:.2e}", "PSNR", f"{p['psnr_db']:.1f}dB", ""])
        tbl = ax.table(cellText=rows, cellLoc="center", loc="center")
        tbl.auto_set_font_size(False); tbl.set_fontsize(7.5); tbl.scale(1.0, 1.35)
        for (rr, cc), cell in tbl.get_celld().items():
            cell.set_edgecolor("#445")
            cell.set_facecolor("#20202b" if rr else "#2c3350")
            cell.set_text_props(color="#dde")

    fig.suptitle("cv2.compareHist (Sam Hodge's method): OUR OCIO node output vs independent raw PyOpenColorIO\n"
                 "sRGB -> ACEScg -> display, per channel  |  corr->1.0 & bhat->0.0 = identical distributions",
                 fontsize=10, color="#e8ecff")
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    return C.savefig(fig, "histogram_compare.png")


if __name__ == "__main__":
    main()
