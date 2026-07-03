"""HDR-safety measurement (2026-07-03): do negatives and >1 scene-linear values SURVIVE?

Reviewer (Sam Hodge) asked "how did you test accuracy? did you check dithering/quantisation? vibe-coding
color maths without regression testing is bold." This module answers the HDR-safety slice with MEASUREMENTS,
by the REAL node/function entry paths, not by reasoning about the code:

  1. io_nodes._convert  over several SCENE-LINEAR colorspace pairs (these MUST preserve out-of-range values;
     a silent clamp to [0,1] here is a real defect). Cross-checked against a raw PyOpenColorIO processor
     built fresh from the config (_common.ocio_reference), so a wiring bug in our wrapper cannot hide.
  2. Every _CURVES log curve, BOTH directions, through the real OCIOLogConvert.run node path.
  3. The Grade nodes (grade_nodes.OCIOGrade / OCIOApplyGrade) with identity params AND a non-trivial
     HDR-safe grade, clamp_output=False, through the real node .run.

For each we report input min/max vs output min/max and COUNT how many out-of-range values were silently
clamped to the [0,1] boundary (should be 0 for any scene-linear transform), plus below-black SIGN loss on
log encode and inf/NaN on log decode. Chart: input-vs-output scatter with the y=x line.

Run:  E:/ComfyUI/ComfyUI/ComfyUI/.venv/Scripts/python.exe D:/n8n/projects/ComfyUI-OCIO/tools/accuracy/measure_hdr_safety.py
"""
import os
import numpy as np
import torch

import _common as C

FIXT = os.path.join(C.HERE, "fixtures")

# Boundary tolerance: a value counts as "pinned to a [0,1] boundary" (a candidate silent clamp) if it lands
# within this of 0.0 or 1.0. Loose enough to catch a clamp, tight enough not to false-positive on a real
# transformed value that merely happens to be near the edge.
_BND = 1e-6


def _to_bhwc(rgb_n3):
    """[N,3] -> torch IMAGE tensor [1, N, 1, 3] (the shape the nodes iterate as a batch of 1)."""
    a = np.ascontiguousarray(rgb_n3.astype(np.float32)).reshape(1, -1, 1, 3)
    return torch.from_numpy(a)


def _from_bhwc(t):
    return t.detach().cpu().numpy().reshape(-1, 3)


def _range_stats(inp, out):
    """Per-transform range + silent-clamp + sign-loss + non-finite accounting.

    clamped_neg_to_0 : inputs < 0 that came out pinned at ~0            (negative crushed to the 0 boundary)
    clamped_pos_to_1 : inputs > 1 that came out pinned at ~1            (super-white crushed to the 1 boundary)
    neg_input_lost_negative_sign : negative inputs whose output is >= 0 (sign lost, e.g. floored to a black CODE
                                   value like 0.0928 - not the 0.0 boundary, so not caught by clamped_neg_to_0)
    n_out_inf / n_out_nan : non-finite outputs (float32 overflow on a decode expansion is unbounded, not clamped)
    Only OUT-of-range inputs are considered for the clamp counters; a real transform that maps -0.25 to some
    other negative is NOT a clamp."""
    inp = np.asarray(inp, np.float64)
    out = np.asarray(out, np.float64)
    finite = np.isfinite(out)
    neg_mask = inp < 0.0
    pos_mask = inp > 1.0
    clamped_neg = int(np.sum(neg_mask & finite & (np.abs(out - 0.0) <= _BND)))
    clamped_pos = int(np.sum(pos_mask & finite & (np.abs(out - 1.0) <= _BND)))
    neg_lost_sign = int(np.sum(neg_mask & finite & (out >= 0.0)))
    of = out[finite]
    return {
        "in_min": float(inp.min()), "in_max": float(inp.max()),
        "out_min_finite": float(of.min()) if of.size else None,
        "out_max_finite": float(of.max()) if of.size else None,
        "n_in_neg": int(np.sum(neg_mask)), "n_in_superwhite": int(np.sum(pos_mask)),
        "n_out_neg": int(np.sum(finite & (out < 0.0))), "n_out_superwhite": int(np.sum(finite & (out > 1.0))),
        "n_out_inf": int(np.sum(np.isinf(out))), "n_out_nan": int(np.sum(np.isnan(out))),
        "clamped_neg_to_0": clamped_neg, "clamped_pos_to_1": clamped_pos,
        "neg_input_lost_negative_sign": neg_lost_sign,
        "silent_clamp_count": clamped_neg + clamped_pos,
    }


def main():
    N, IO = C.load_pack()
    from ocio_pkg import grade_nodes as G  # real grade node classes

    hdr = np.load(os.path.join(FIXT, "hdr_ramp.npy")).astype(np.float32)   # [256,3], -0.25..16.0
    assert hdr.shape[1] == 3, hdr.shape
    results = {
        "fixture": "hdr_ramp.npy",
        "fixture_input_min": float(hdr.min()), "fixture_input_max": float(hdr.max()),
        "fixture_n": int(hdr.shape[0]),
        "config": None, "convert_pairs": {}, "curves": {}, "grade": {},
    }

    cfg, key = N._resolve_config_keyed("")
    import PyOpenColorIO as OCIO
    results["config"] = {"name": cfg.getName(), "ocio_version": OCIO.GetVersion(),
                         "colorspace_count": len(list(cfg.getColorSpaceNames())), "cache_key": list(key)}

    # ---------------------------------------------------------------- 1. _convert, scene-linear pairs
    scene_pairs = [
        ("ACEScg", "ACEScg"),                      # identity fast path
        ("ACEScg", "ACES2065-1"),                  # 3x3 matrix, no curve
        ("ACES2065-1", "ACEScg"),
        ("Linear Rec.709 (sRGB)", "ACEScg"),       # the LTX HDR path (Rec.709 lin -> ACEScg)
        ("ACEScg", "Linear Rec.709 (sRGB)"),
    ]
    for a, b in scene_pairs:
        out = _from_bhwc(IO._convert(_to_bhwc(hdr), a, b))      # REAL node conversion path
        ref = C.ocio_reference(a, b, hdr.reshape(-1, 3))        # raw OCIO, independent
        st = _range_stats(hdr, out)
        st["max_abs_vs_raw_ocio"] = C.max_abs(out, ref)         # parity: our wrapper vs fresh processor
        results["convert_pairs"][f"{a} -> {b}"] = st

    # one display pair, labelled: sRGB - Display encode WILL soft-limit super-white (display range) - NOT a
    # scene-linear defect; recorded so the reader sees the contrast with the scene-linear rows.
    out = _from_bhwc(IO._convert(_to_bhwc(hdr), "ACEScg", "sRGB - Display"))
    st = _range_stats(hdr, out)
    st["note"] = "display transform: soft-limiting super-white is expected (display range), NOT a scene-linear defect"
    results["convert_pairs"]["ACEScg -> sRGB - Display (display, expected)"] = st

    # ---------------------------------------------------------------- 2. every _CURVES curve, both directions
    node = N.OCIOLogConvert()
    for cname in N._CURVES:
        for op in ("lin_to_log", "log_to_lin"):
            (out_t,) = node.run(_to_bhwc(hdr), op, cname, mix=1.0)   # REAL node run
            results["curves"][f"{cname}:{op}"] = _range_stats(hdr, _from_bhwc(out_t))

    # ---------------------------------------------------------------- 3. Grade nodes
    grade_node = G.OCIOGrade()
    # 3a. identity params, clamp default False -> must be an exact no-op (bit-identical), range intact
    (out_t, _) = grade_node.run(_to_bhwc(hdr), 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0,
                                1.0, 0.18, 1.0, mix=1.0, clamp_output=False)
    out = _from_bhwc(out_t)
    st = _range_stats(hdr, out)
    st["bit_identical_to_input"] = bool(np.array_equal(out.astype(np.float32), hdr))
    results["grade"]["identity_noclamp"] = st

    # 3b. non-trivial HDR grade, clamp_output=False -> negatives + super-white must SURVIVE (_signed_pow)
    NT = dict(g=0.9, gn=1.3, off=-0.02, con=1.2, sat=1.1)
    (out_t, _) = grade_node.run(_to_bhwc(hdr), 0.0, 0.0, 0.0, NT["g"], NT["g"], NT["g"], NT["gn"], NT["gn"],
                                NT["gn"], NT["off"], NT["off"], NT["off"], NT["con"], 0.18, NT["sat"],
                                mix=1.0, clamp_output=False)
    results["grade"]["nontrivial_noclamp"] = _range_stats(hdr, _from_bhwc(out_t))
    results["grade"]["nontrivial_noclamp"]["params"] = NT

    # 3c. SAME grade, clamp_output=True -> clamp explicitly requested -> [0,1] pinning is correct
    (out_t, _) = grade_node.run(_to_bhwc(hdr), 0.0, 0.0, 0.0, NT["g"], NT["g"], NT["g"], NT["gn"], NT["gn"],
                                NT["gn"], NT["off"], NT["off"], NT["off"], NT["con"], 0.18, NT["sat"],
                                mix=1.0, clamp_output=True)
    st = _range_stats(hdr, _from_bhwc(out_t))
    st["note"] = "clamp_output=True: clamp is explicitly requested, so [0,1] pinning is correct behaviour"
    results["grade"]["nontrivial_clamp_requested"] = st

    # ---------------------------------------------------------------- defect roll-up
    defects = []
    for k, v in results["convert_pairs"].items():
        if "expected" in k:
            continue
        if v["silent_clamp_count"] > 0:
            defects.append(f"_convert {k}: {v['silent_clamp_count']} value(s) silently clamped to [0,1]")
    if not results["grade"]["identity_noclamp"]["bit_identical_to_input"]:
        defects.append("Grade identity (no clamp) is NOT bit-identical to input")
    if results["grade"]["nontrivial_noclamp"]["silent_clamp_count"] > 0:
        defects.append(f"Grade non-trivial (no clamp): "
                       f"{results['grade']['nontrivial_noclamp']['silent_clamp_count']} silent clamp(s)")
    results["scene_linear_defects"] = defects

    # curve behaviour roll-ups (reported + interpreted; log-encode toe/decode overflow judged in the writeup)
    results["curve_boundary_clamps"] = {k: v["silent_clamp_count"] for k, v in results["curves"].items()
                                        if v["silent_clamp_count"] > 0}
    results["lin_to_log_below_black_sign_loss"] = {
        k: v["neg_input_lost_negative_sign"] for k, v in results["curves"].items()
        if k.endswith("lin_to_log") and v["neg_input_lost_negative_sign"] > 0}
    results["log_to_lin_nonfinite_overflow"] = {
        k: {"inf": v["n_out_inf"], "nan": v["n_out_nan"]} for k, v in results["curves"].items()
        if v["n_out_inf"] or v["n_out_nan"]}

    # ---------------------------------------------------------------- chart
    _plot(hdr, results, N, IO, G)

    p = C.dump("hdr_safety.json", results)
    print("dumped", p)
    print("scene-linear defects:", results["scene_linear_defects"] or "NONE")
    print("boundary clamps (0/1) across curves:", results["curve_boundary_clamps"] or "NONE")
    print("lin_to_log below-black sign loss (encode floors negatives):",
          results["lin_to_log_below_black_sign_loss"])
    print("log_to_lin non-finite (float32 overflow on code>~12):",
          {k: v["inf"] for k, v in results["log_to_lin_nonfinite_overflow"].items()})
    scg = results["convert_pairs"]["Linear Rec.709 (sRGB) -> ACEScg"]
    print(f"Linear Rec.709 -> ACEScg: in [{scg['in_min']:.3f},{scg['in_max']:.3f}] -> "
          f"out [{scg['out_min_finite']:.4f},{scg['out_max_finite']:.4f}], silent clamps "
          f"{scg['silent_clamp_count']}, parity vs raw OCIO {scg['max_abs_vs_raw_ocio']:.2e}")
    g = results["grade"]["nontrivial_noclamp"]
    print(f"Grade non-trivial no-clamp: out [{g['out_min_finite']:.4f},{g['out_max_finite']:.4f}], "
          f"neg survived {g['n_out_neg']}, superwhite survived {g['n_out_superwhite']}, "
          f"silent clamps {g['silent_clamp_count']}")
    return results


def _plot(hdr, results, N, IO, G):
    """Input-vs-output scatter with y=x, showing negatives + super-white preserved across the three paths."""
    fig, axes = C.new_fig(1, 3, figsize=(15, 5))
    xin = hdr[:, 0]

    # panel A: scene-linear _convert (Linear Rec.709 -> ACEScg) + identity, vs y=x
    ax = axes[0]
    out_lr = _from_bhwc(IO._convert(_to_bhwc(hdr), "Linear Rec.709 (sRGB)", "ACEScg"))[:, 0]
    out_id = _from_bhwc(IO._convert(_to_bhwc(hdr), "ACEScg", "ACEScg"))[:, 0]
    lim = [-1, 17]
    ax.plot(lim, lim, "-", color="#556", lw=1, label="y = x")
    ax.scatter(xin, out_id, s=10, c="#5cf", label="ACEScg->ACEScg (identity)")
    ax.scatter(xin, out_lr, s=10, c="#f85", label="LinRec709->ACEScg")
    ax.axhline(0, color="#933", lw=0.7, ls=":"); ax.axvline(0, color="#933", lw=0.7, ls=":")
    ax.axhline(1, color="#396", lw=0.7, ls=":")
    ax.set_title("A. _convert (scene-linear)\nnegatives + >1 preserved, 0 clamps")
    ax.set_xlabel("input (scene-linear)"); ax.set_ylabel("output")
    ax.legend(fontsize=7, loc="upper left")

    # panel B: log curves lin_to_log - the encode; show cineon/logc3 flooring negatives vs slog3 preserving
    ax = axes[1]
    node = N.OCIOLogConvert()
    m = hdr[:, 0] <= 1.5   # zoom on the toe region where the floor shows
    for cname, col in [("cineon", "#f85"), ("logc3", "#fb5"), ("slog3", "#5cf"), ("acescct", "#9c6")]:
        (o,) = node.run(_to_bhwc(hdr), "lin_to_log", cname, 1.0)
        ax.scatter(hdr[m, 0], _from_bhwc(o)[m, 0], s=10, c=col, label=f"{cname} lin_to_log")
    ax.axvline(0, color="#933", lw=0.7, ls=":")
    ax.set_title("B. log ENCODE (lin_to_log), toe zoom\ncineon/logc3 FLOOR neg to black code")
    ax.set_xlabel("input linear (incl. below-black)"); ax.set_ylabel("output code")
    ax.legend(fontsize=7, loc="lower right")

    # panel C: Grade non-trivial, clamp OFF vs clamp ON
    ax = axes[2]
    gnode = G.OCIOGrade()
    P = dict(g=0.9, gn=1.3, off=-0.02, con=1.2, sat=1.1)
    (o_off, _) = gnode.run(_to_bhwc(hdr), 0, 0, 0, P["g"], P["g"], P["g"], P["gn"], P["gn"], P["gn"],
                           P["off"], P["off"], P["off"], P["con"], 0.18, P["sat"], mix=1.0, clamp_output=False)
    (o_on, _) = gnode.run(_to_bhwc(hdr), 0, 0, 0, P["g"], P["g"], P["g"], P["gn"], P["gn"], P["gn"],
                          P["off"], P["off"], P["off"], P["con"], 0.18, P["sat"], mix=1.0, clamp_output=True)
    lim = [-1, 22]
    ax.plot(lim, lim, "-", color="#556", lw=1, label="y = x")
    ax.scatter(xin, _from_bhwc(o_off)[:, 0], s=10, c="#5cf", label="clamp OFF (HDR-safe)")
    ax.scatter(xin, _from_bhwc(o_on)[:, 0], s=10, c="#f85", label="clamp ON (requested)")
    ax.axhline(0, color="#933", lw=0.7, ls=":"); ax.axhline(1, color="#396", lw=0.7, ls=":")
    ax.set_title("C. Grade non-trivial\nclamp OFF keeps neg + >1, 0 silent clamps")
    ax.set_xlabel("input (scene-linear)"); ax.set_ylabel("output")
    ax.legend(fontsize=7, loc="upper left")

    fig.suptitle("HDR-safety: do negatives (-0.25) and super-white (up to 16.0) survive?  hdr_ramp -> pack transforms",
                 color="#e8ecff", fontsize=11)
    path = C.savefig(fig, "hdr_safety.png")
    print("chart", path)
    results["plot_path"] = path


if __name__ == "__main__":
    main()
