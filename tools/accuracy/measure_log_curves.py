"""Measure the 10 log curves in nodes._CURVES (2026-07-03).

Answers Sam Hodge's "how did you test accuracy / did you regression test the colour maths?" for the
LogConvert transfer curves specifically:

  (a) ROUND-TRIP identity  lin -> log -> lin  over a wide range (fixtures/hdr_ramp.npy -0.25..16 plus a
      dense scene-linear 0..16 sweep incl. toe, 0.18 mid-grey, HDR highlights). Reported two ways:
        - POSITIVE-domain (0..16): the real accuracy of the invertible curve region.
        - FULL-domain (incl. negatives -0.25..0): exposes that 3 curves (cineon, acescc, logc3) CLAMP
          negative scene-linear at encode, so a negative does not survive lin->log->lin. Reported honestly.
      Plus the reverse log -> lin -> log over the encoded [0,1] range.
  (b) SPEC ANCHORS from each vendor's own published table (cited in nodes.py comments): black point
      (lin 0 -> coded black), 18% mid-grey, and further documented landmarks. our value vs published.
      Every ARRI/RED/BMD/ACES anchor here is cross-checked either against the vendor's own numeric
      statement or against OCIO's independent same-named colorspace on a neutral grey (see notes).
  (c) C1 CONTINUITY at every piecewise breakpoint. Measured ANALYTICALLY in float64 with one-sided,
      branch-restricted finite-difference slopes (the pack casts curve output to float32 for the image
      pipeline, which adds ~1e-7 steps; that quantization is NOT a kink and would masquerade as one if you
      differenced the shipped float32 output, so we differentiate the float64 math the pack uses).
      ACEScc and S-Log3 have a KNOWN spec-inherent non-C1 join (zero-toe / slope mismatch by design) -
      flagged as NOTES, not failures.

Chart: per-curve round-trip max error (log-y bars) + anchor deviation / C1 table.
Run:  E:/ComfyUI/ComfyUI/ComfyUI/.venv/Scripts/python.exe measure_log_curves.py
"""
import math
import numpy as np
import _common as C

N, IO = C.load_pack()

# ----------------------------------------------------------------- sample grids
hdr_ramp = np.load(C.os.path.join(C.FIXT_DIR, "hdr_ramp.npy")).astype(np.float64)   # (256,3) -0.25..16
dense_pos = np.concatenate([
    np.linspace(0.0, 0.02, 400),          # toe
    np.linspace(0.02, 1.0, 800),          # mids incl. 0.18
    np.linspace(1.0, 16.0, 800),          # highlights
    np.geomspace(1e-6, 1.0, 400),         # log-spaced low end
])
dense_pos = np.unique(np.round(dense_pos, 9))
dense_pos = np.unique(np.concatenate([dense_pos, [0.0, 0.18, 1.0, 0.9, 184.322, 10.0]]))  # exact anchors
dense_neg = np.linspace(-0.25, 0.0, 200)
hdr_pos = hdr_ramp.reshape(-1)[hdr_ramp.reshape(-1) >= 0.0]
hdr_neg = hdr_ramp.reshape(-1)[hdr_ramp.reshape(-1) < 0.0]

pos_lin = np.unique(np.concatenate([dense_pos, hdr_pos]))                 # 0..16, positive only
full_lin = np.unique(np.concatenate([pos_lin, dense_neg, hdr_neg]))       # -0.25..16
log_domain = np.linspace(0.0, 1.0, 2000)                                  # encoded [0,1] reverse sweep


def relerr(a, b, floor=1e-4):
    a = np.asarray(a, np.float64); b = np.asarray(b, np.float64)
    return np.abs(a - b) / np.maximum(np.abs(b), floor)


# ----------------------------------------------------------------- analytic (float64) curve models for C1
# These reproduce EXACTLY the math each pack encode function runs, WITHOUT the trailing .astype(float32),
# so the C1 test measures true continuity of the maths, not float32 output quantization. Constants pulled
# live from the loaded module (N.*), never hardcoded, so they can't drift from the shipped pack.
def _cineon(x):  # single curve, no cut
    return (685.0 + 300.0 * math.log10(x * (1.0 - N._CINEON_OFF) + N._CINEON_OFF)) / 1023.0

def _acescct(x):
    return (math.log2(x) + 9.72) / 17.52 if x > 0.0078125 else 10.5402377416545 * x + 0.0729055341958355

def _acescc(x):
    if x <= 0.0:
        return (math.log2(2.0 ** -16) + 9.72) / 17.52
    if x < 2.0 ** -15:
        return (math.log2(2.0 ** -16 + x * 0.5) + 9.72) / 17.52
    return (math.log2(x) + 9.72) / 17.52

def _logc3(x):
    return N._LC3_C * math.log10(N._LC3_A * x + N._LC3_B) + N._LC3_D if x >= N._LC3_CUT else N._LC3_E * x + N._LC3_F

def _logc4(x):
    return (math.log2(N._LC4_A * x + 64.0) - 6.0) / 14.0 * N._LC4_B + N._LC4_C if x >= N._LC4_T else (x - N._LC4_T) / N._LC4_S

def _slog3(x):
    return (420.0 + math.log10((x + 0.01) / 0.19) * 261.5) / 1023.0 if x >= N._SL3_CUT_LIN \
        else (x * (N._SL3_B171 - N._SL3_B95) / N._SL3_CUT_LIN + N._SL3_B95) / 1023.0

def _vlog(x):
    return N._VLOG_C * math.log10(x + N._VLOG_B) + N._VLOG_D if x >= N._VLOG_CUT1 else 5.6 * x + 0.125

def _canonlog3(x):
    if x < N._CL3_LO:
        return -N._CL3_NEG_A * math.log10(1.0 - N._CL3_NEG_S * x) + N._CL3_NEG_OFF
    if x <= N._CL3_HI:
        return N._CL3_MID_S * x + N._CL3_MID_OFF
    return N._CL3_POS_A * math.log10(N._CL3_POS_S * x + 1.0) + N._CL3_POS_OFF

def _log3g10(x):
    xs = x + N._L3G10_C
    return N._L3G10_A * math.log10(xs * N._L3G10_B + 1.0) if xs >= 0.0 else xs * N._L3G10_G

def _davinci(x):
    return (math.log2(x + N._DI_A) + N._DI_B) * N._DI_C if x > N._DI_LIN_CUT else x * N._DI_M

ANALYTIC = {"cineon": _cineon, "acescct": _acescct, "acescc": _acescc, "logc3": _logc3, "logc4": _logc4,
            "slog3": _slog3, "vlog": _vlog, "canonlog3": _canonlog3, "log3g10": _log3g10,
            "davinci_intermediate": _davinci}


def c1_at_cut(f, cut):
    """One-sided branch-restricted slopes + value gap at a piecewise cut, in float64. h chosen small but
    above 1e-8 so each sample stays on its own branch and log/exp are well-conditioned."""
    scale = max(abs(cut), 1e-3)
    off = scale * 1e-6      # step off the cut onto each branch
    h = scale * 1e-6        # differencing step
    vb = f(cut - 1e-12 * max(abs(cut), 1.0))
    va = f(cut + 1e-12 * max(abs(cut), 1.0))
    s_below = (f(cut - off) - f(cut - off - h)) / h
    s_above = (f(cut + off + h) - f(cut + off)) / h
    val_gap = abs(va - vb)
    denom = max(abs(s_below), abs(s_above), 1e-12)
    return val_gap, s_below, s_above, abs(s_above - s_below) / denom


# ----------------------------------------------------------------- breakpoints (lin cut, log cut, label, known-non-C1)
BREAKS = {
    "cineon":  [],
    "acescct": [(0.0078125, 0.155251141552511, "toe/log", False)],
    "acescc":  [(2.0 ** -15, (np.log2(2.0 ** -16) + 9.72) / 17.52, "near-zero join", True)],  # spec zero-toe
    "logc3":   [(N._LC3_CUT, N._LC3_E * N._LC3_CUT + N._LC3_F, "toe/log", False)],
    "logc4":   [(N._LC4_T, 0.0, "toe/log (V=0)", False)],
    "slog3":   [(N._SL3_CUT_LIN, N._SL3_CUT_LOG, "toe/log", True)],  # Sony spec linear toe: slope-mismatched
    "vlog":    [(N._VLOG_CUT1, N._VLOG_CUT2, "toe/log", False)],
    "canonlog3": [(N._CL3_LO, N._CL3_Y_LO, "neg/mid", False),
                  (N._CL3_HI, N._CL3_Y_HI, "mid/pos", False)],
    "log3g10": [(-N._L3G10_C, 0.0, "toe/log (x+c=0)", False)],
    "davinci_intermediate": [(N._DI_LIN_CUT, N._DI_LOG_CUT, "lin/log", False)],
}

# ----------------------------------------------------------------- published spec anchors (vendor's own numbers)
# Each: (linear_input, published_encoded_value, source_note). All cross-checked 2026-07-03 (see notes).
ANCHORS = {
    "cineon": [
        (0.0, 0.09286412, "Nuke default Cineon black 0->0.0928 (OCIO nuke-default; nodes.py:484)"),
    ],
    "acescct": [
        (0.0, 0.0729055341958355, "ACEScct(0)=0.07290553 toe intercept, S-2016-001"),
        (0.18, (np.log2(0.18) + 9.72) / 17.52, "18% grey via S-2016-001 log branch = 0.4135884"),
    ],
    "acescc": [
        (0.18, (np.log2(0.18) + 9.72) / 17.52, "18% grey ACEScc, S-2014-003 = 0.4135884"),
    ],
    "logc3": [
        # cross-checked vs OCIO 'ARRI LogC3 (EI800)' neutral grey: lin0->0.092809, lin0.18->0.391006. Both match.
        (0.0, N._LC3_F, "LogC3 EI800 lin0 -> f=0.092809 (OCIO 'ARRI LogC3 (EI800)' neutral = 0.092809)"),
        (0.18, 0.391007, "LogC3 EI800 18% grey = 0.391 (ARRI '39% signal'; OCIO neutral = 0.391006)"),
    ],
    "logc4": [
        (0.0, N._LC4_C, "ARRI LogC4 lin0 -> c=0.0928641 (OCIO 'ARRI LogC4' neutral = 0.092865)"),
        # Confirmed 2026-07-03: ARRI states 18% grey -> ~28% signal; OCIO 'ARRI LogC4' neutral 0.18 = 0.278396;
        # from-scratch recompute of the spec formula = 0.278396. (An earlier draft's 0.271359 was wrong.)
        (0.18, 0.278396, "ARRI LogC4 18% grey = 0.278396 (ARRI '28% signal'; OCIO 'ARRI LogC4' neutral)"),
    ],
    "slog3": [
        (0.0, 95.0 / 1023.0, "S-Log3 0% black -> CV95 = 0.0928641 (Sony Tech Summary table)"),
        (0.18, 420.0 / 1023.0, "S-Log3 18% grey -> CV420 = 0.4105572 (Sony table)"),
        (0.90, 598.0 / 1023.0, "S-Log3 90% white -> CV598 = 0.5845552 (Sony table)"),
    ],
    "vlog": [
        (0.0, 128.0 / 1023.0, "V-Log 0% black -> CV128 = 0.1251222 (Panasonic Fig.2.2 table)"),
        (0.18, 433.0 / 1023.0, "V-Log 18% grey -> CV433 = 0.4232649 (Panasonic table)"),
        (0.90, 602.0 / 1023.0, "V-Log 90% white -> CV602 = 0.5884653 (Panasonic table)"),
    ],
    "canonlog3": [
        # Canon tabulates by 'Scene Linear %' with reflection = SceneLinear*0.9; this code's x is DIRECT
        # scene-linear reflectance. Black-point (x=0) is the tabulated mid intercept; 18%-direct is
        # inferred-consistent with the same formula, not separately tabulated by Canon (nodes.py:396).
        (0.0, 0.12512219, "Canon Log3 x=0 -> mid intercept 0.12512219 (Canon white paper Appendix)"),
    ],
    "log3g10": [
        (-0.01, 0.0, "Log3G10 lin -0.01 -> 0.0 (RED white paper table)"),
        (0.0, 0.091551, "Log3G10 lin 0.0 -> 0.091551 (RED table)"),
        (0.18, 1.0 / 3.0, "Log3G10 lin 0.18 -> 1/3 (RED table, '3G10' name)"),
        (1.0, 0.493449, "Log3G10 lin 1.0 -> 0.493449 (RED table)"),
        (184.322, 1.0, "Log3G10 lin 184.322 -> 1.0 (RED table, 10 stops over grey)"),
    ],
    "davinci_intermediate": [
        (0.0, 0.0, "DaVinci Intermediate lin 0 -> 0.0 (BMD white paper table)"),
        (0.18, 0.336043, "DaVinci Intermediate 18% grey -> 0.336043 (BMD table)"),
        (1.0, 0.513837, "DaVinci Intermediate lin 1.0 -> 0.513837 (BMD table)"),
        (10.0, 0.756599, "DaVinci Intermediate lin 10.0 -> 0.756599 (BMD table)"),
    ],
}
ANCHOR_TOL = 1.0e-3   # ~one 10-bit code value; inside this is a vendor-table-rounding match

# Curves whose encode intentionally CLAMPS negative scene-linear (documented, not a round-trip bug per se).
NEG_CLAMP = {"cineon", "acescc", "logc3"}


results = {"curves": {}, "meta": {
    "python": C.sys.version.split()[0],
    "ocio": __import__("PyOpenColorIO").__version__,
    "n_pos_samples": int(pos_lin.size), "n_full_samples": int(full_lin.size),
    "n_log_samples": int(log_domain.size), "anchor_tol": ANCHOR_TOL,
}}

print(f"{'curve':22s} {'rt_pos_max':>10s} {'rt_pos_rms':>10s} {'rt_full_max':>11s} {'rt_log_max':>10s} "
      f"{'anchor_worst':>12s} {'C1_relslope':>11s} {'neg_clamp':>9s}")

for name, (f_l2x, f_x2l) in N._CURVES.items():
    cur = {}

    # (a1) round-trip lin->log->lin, POSITIVE domain (the real accuracy of the invertible region)
    enc_p = f_l2x(pos_lin); dec_p = f_x2l(enc_p).astype(np.float64)
    rt_p = np.abs(dec_p - pos_lin)
    cur["roundtrip_pos"] = {
        "max_abs": float(np.max(rt_p)), "rms_abs": float(np.sqrt(np.mean(rt_p ** 2))),
        "max_rel": float(np.max(relerr(dec_p, pos_lin))),
        "worst_input": float(pos_lin[int(np.argmax(rt_p))]),
        "range": [float(pos_lin.min()), float(pos_lin.max())], "n": int(pos_lin.size)}

    # (a2) round-trip FULL domain incl negatives (exposes clamps)
    enc_f = f_l2x(full_lin); dec_f = f_x2l(enc_f).astype(np.float64)
    rt_f = np.abs(dec_f - full_lin)
    cur["roundtrip_full"] = {
        "max_abs": float(np.max(rt_f)), "rms_abs": float(np.sqrt(np.mean(rt_f ** 2))),
        "worst_input": float(full_lin[int(np.argmax(rt_f))]),
        "range": [float(full_lin.min()), float(full_lin.max())], "n": int(full_lin.size)}

    # (a3) reverse log->lin->log over [0,1]
    lin = f_x2l(log_domain); enc2 = f_l2x(lin).astype(np.float64)
    rlog = np.abs(enc2 - log_domain)
    cur["roundtrip_log"] = {"max_abs": float(np.max(rlog)), "rms_abs": float(np.sqrt(np.mean(rlog ** 2))),
                            "worst_input": float(log_domain[int(np.argmax(rlog))])}

    # (b) spec anchors
    arows = []; worst_anchor = 0.0
    for lin_in, pub, note in ANCHORS.get(name, []):
        got = float(f_l2x(np.array([lin_in]))[0]); dev = abs(got - pub)
        worst_anchor = max(worst_anchor, dev)
        arows.append({"lin_in": lin_in, "published": float(pub), "ours": got, "abs_dev": float(dev),
                      "within_tol": bool(dev <= ANCHOR_TOL), "note": note})
    cur["anchors"] = arows; cur["anchor_worst_dev"] = float(worst_anchor)

    # (c) C1 continuity - analytic float64
    fa = ANALYTIC[name]
    crows = []; worst_relslope = 0.0
    for lin_cut, log_cut, label, known in BREAKS.get(name, []):
        val_gap, sb, sa, rel = c1_at_cut(fa, lin_cut)
        if not known:
            worst_relslope = max(worst_relslope, rel)
        crows.append({"label": label, "lin_cut": float(lin_cut), "log_cut": float(log_cut),
                      "value_gap": float(val_gap), "slope_below": float(sb), "slope_above": float(sa),
                      "rel_slope_jump": float(rel), "known_noncontinuous_by_spec": bool(known),
                      "C0_ok": bool(val_gap < 1e-6), "C1_ok": bool(rel < 1e-3)})
    cur["breakpoints"] = crows; cur["C1_worst_relslope_enforced"] = float(worst_relslope)

    # HDR-safety: does encode->decode preserve negatives & super-white without a silent 0..1 clip?
    neg_in = np.array([-0.1, -0.05, -0.01]); hi_in = np.array([2.0, 8.0, 16.0])
    neg_rt = f_x2l(f_l2x(neg_in)).astype(np.float64); hi_rt = f_x2l(f_l2x(hi_in)).astype(np.float64)
    cur["hdr_safety"] = {
        "neg_in": neg_in.tolist(), "neg_roundtrip": [float(v) for v in neg_rt],
        "neg_preserved": bool(np.max(np.abs(neg_rt - neg_in)) < 1e-3),
        "hi_in": hi_in.tolist(), "hi_roundtrip": [float(v) for v in hi_rt],
        "hi_preserved": bool(np.max(np.abs(hi_rt - hi_in) / hi_in) < 1e-3),
        "neg_clamped_by_design": bool(name in NEG_CLAMP)}

    results["curves"][name] = cur
    print(f"{name:22s} {cur['roundtrip_pos']['max_abs']:10.2e} {cur['roundtrip_pos']['rms_abs']:10.2e} "
          f"{cur['roundtrip_full']['max_abs']:11.2e} {cur['roundtrip_log']['max_abs']:10.2e} "
          f"{worst_anchor:12.2e} {worst_relslope:11.2e} {str(name in NEG_CLAMP):>9s}")


# ----------------------------------------------------------------- chart
fig, (ax1, ax2) = C.new_fig(1, 2, figsize=(14, 5.8), gridspec_kw={"width_ratios": [1.1, 1.05]})
names = list(results["curves"].keys())
rt_pos = [results["curves"][n]["roundtrip_pos"]["max_abs"] for n in names]
rt_log = [results["curves"][n]["roundtrip_log"]["max_abs"] for n in names]
x = np.arange(len(names)); w = 0.4
ax1.bar(x - w / 2, np.maximum(rt_pos, 1e-12), w, label="lin->log->lin max abs (0..16)", color="#5cc8ff")
ax1.bar(x + w / 2, np.maximum(rt_log, 1e-12), w, label="log->lin->log max abs ([0,1])", color="#ffb74d")
ax1.set_yscale("log")
ax1.axhline(1e-5, color="#4a4", ls="--", lw=1, alpha=0.7)
ax1.text(len(names) - 0.5, 1.2e-5, "1e-5 (float32 grade)", color="#6c6", fontsize=7, ha="right", va="bottom")
ax1.set_xticks(x); ax1.set_xticklabels(names, rotation=40, ha="right", fontsize=7.5)
ax1.set_ylabel("round-trip max abs error (positive scene-linear)")
ax1.set_title("Round-trip identity per curve  (lower is better, log y)")
ax1.legend(fontsize=7.5, loc="upper left")
ax1.grid(True, which="both", axis="y", alpha=0.25)

ax2.axis("off")
rows = []
for n in names:
    c = results["curves"][n]
    ok = sum(1 for a in c["anchors"] if a["within_tol"])
    c1 = c["C1_worst_relslope_enforced"]
    known = any(b["known_noncontinuous_by_spec"] for b in c["breakpoints"])
    c1txt = f"{c1:.1e}" + (" +spec toe" if known else "")
    negtxt = "clamp" if n in NEG_CLAMP else "keeps"
    rows.append([n, f"{c['anchor_worst_dev']:.2e}", f"{ok}/{len(c['anchors'])}", c1txt, negtxt])
tbl = ax2.table(cellText=rows,
                colLabels=["curve", "worst anchor dev", "anchors OK", "C1 rel-slope", "neg"],
                loc="center", cellLoc="left", colWidths=[0.30, 0.20, 0.14, 0.22, 0.14])
tbl.auto_set_font_size(False); tbl.set_fontsize(7.6); tbl.scale(1, 1.35)
for (r, cc), cell in tbl.get_celld().items():
    cell.set_edgecolor("#445")
    if r == 0:
        cell.set_facecolor("#242433"); cell.get_text().set_color("#dfe6ff"); cell.get_text().set_weight("bold")
    else:
        cell.set_facecolor("#191922"); cell.get_text().set_color("#cdd6e6")
ax2.set_title("Spec-anchor deviation + analytic C1 rel-slope-jump  (tol 1e-3)", fontsize=9)
fig.suptitle("ComfyUI-OCIO  -  10 LogConvert curves: round-trip + spec anchors + C1 continuity",
             color="#eef", fontsize=11)
plot_path = C.savefig(fig, "log_curves.png")

# ----------------------------------------------------------------- summary rollup
worst_rt = max(results["curves"][n]["roundtrip_pos"]["max_abs"] for n in names)
worst_rt_curve = max(names, key=lambda n: results["curves"][n]["roundtrip_pos"]["max_abs"])
worst_rt_log = max(results["curves"][n]["roundtrip_log"]["max_abs"] for n in names)
anchor_fails = [{"curve": n, "lin_in": a["lin_in"], "published": a["published"], "ours": a["ours"],
                 "abs_dev": a["abs_dev"], "note": a["note"]}
                for n in names for a in results["curves"][n]["anchors"] if not a["within_tol"]]
c1_fails = [{"curve": n, **{k: b[k] for k in ("label", "value_gap", "slope_below", "slope_above", "rel_slope_jump")}}
            for n in names for b in results["curves"][n]["breakpoints"]
            if not b["known_noncontinuous_by_spec"] and (not b["C1_ok"] or not b["C0_ok"])]
neg_clamp_curves = [n for n in names if results["curves"][n]["hdr_safety"]["neg_clamped_by_design"]]
superwhite_fail = [n for n in names if not results["curves"][n]["hdr_safety"]["hi_preserved"]]
results["summary"] = {
    "n_curves": len(names),
    "worst_roundtrip_pos_max_abs": worst_rt, "worst_roundtrip_curve": worst_rt_curve,
    "worst_roundtrip_log_max_abs": worst_rt_log,
    "anchor_failures_outside_tol": anchor_fails,
    "enforced_C1_failures": c1_fails,
    "negative_clamp_curves": neg_clamp_curves,
    "superwhite_clip_curves": superwhite_fail,
}
json_path = C.dump("log_curves.json", results)

print("\n================ SUMMARY ================")
print(f"curves tested            : {len(names)}")
print(f"worst RT (pos) max_abs   : {worst_rt:.3e}  ({worst_rt_curve})")
print(f"worst RT (log) max_abs   : {worst_rt_log:.3e}")
print(f"anchor failures (>tol)   : {len(anchor_fails)}")
for a in anchor_fails:
    print(f"   - {a['curve']:20s} lin {a['lin_in']:<9} pub {a['published']:.6f} ours {a['ours']:.6f} "
          f"dev {a['abs_dev']:.2e}  [{a['note']}]")
print(f"analytic-C1 failures     : {len(c1_fails)}")
for cfl in c1_fails:
    print(f"   - {cfl['curve']:20s} {cfl['label']:12s} rel_slope {cfl['rel_slope_jump']:.2e} "
          f"gap {cfl['value_gap']:.2e}")
print(f"neg-clamp curves (design): {neg_clamp_curves}")
print(f"super-white clip curves  : {superwhite_fail if superwhite_fail else 'none (all preserve >1)'}")
print(f"\nplot : {plot_path}")
print(f"json : {json_path}")
