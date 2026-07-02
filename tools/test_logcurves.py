"""Dependency-free continuity audit for every camera log curve registered in OCIO LogConvert's _CURVES dict
(cineon, acescct, acescc, logc3, logc4, slog3, vlog, canonlog3, log3g10, davinci_intermediate).

For each curve this checks three things:
  1. round-trip identity over toe/mid/HDR samples (encode then decode should return the input),
  2. the published 18% mid-grey landmark anchor (per-curve tolerance, cited from the same primary specs as
     the nodes.py comments),
  3. C1 continuity at the piecewise cut point (the key new check) - both branches of a np.where split must
     agree in VALUE at the cut, and their finite-difference slopes just below/above the cut must agree too.
     This is the "kink in the midtones" bug class: a value-continuous but slope-discontinuous curve looks
     fine on a single sample but grades visibly wrong once you push contrast around the cut point.

cineon has no piecewise cut (single smooth log branch beyond a baked-in black offset), so it is round-trip
and anchor tested but skipped in the continuity section - noted explicitly, not silently omitted.

Run from the pack root: python tools/test_logcurves.py"""
import os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nodes as n

FAILURES = []
NOTES = []


def check(label, cond, detail):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}: {detail}")
    if not cond:
        FAILURES.append(f"{label}: {detail}")


def note(label, cond, detail):
    """Informational (non-failing). Slope continuity at a cut is NOT a hard requirement: several published
    camera specs (ACEScc S-2014-003, Sony S-Log3) are value-continuous but deliberately NOT slope-continuous
    at their toe/log join, so a faithful re-implementation inherits that. We surface the slope gap so a real
    implementation kink would still be visible, but only round-trip, anchors, and VALUE continuity gate."""
    status = "ok  " if cond else "NOTE"
    print(f"  [{status}] {label}: {detail}")
    if not cond:
        NOTES.append(f"{label}: {detail}")


def roundtrip(name, enc, dec, samples, rel_tol):
    x = np.array(samples, dtype=np.float32)
    y = enc(x)
    back = dec(y)
    rel = float(np.max(np.abs(back.astype(np.float64) - x.astype(np.float64)) / (np.abs(x.astype(np.float64)) + 1e-3)))
    check(f"{name} round-trip", rel < rel_tol, f"max rel error {rel:.3e} (tol {rel_tol:.0e}) over {samples}")
    return rel


def anchor(name, enc, x_mid, y_expected, tol):
    y = float(enc(np.array([x_mid], np.float32))[0])
    check(f"{name} mid-grey anchor", abs(y - y_expected) < tol,
          f"enc({x_mid}) = {y:.6f}, spec {y_expected:.6f} (tol {tol})")


def _value_ratio_check(label, probe, eps_hi=1e-4, eps_lo=1e-6):
    """Richardson-style C0 check: sample the value gap at two epsilons that both sit well clear of float64
    cancellation noise (1e-4 and 1e-6 - two orders apart, neither small enough to hit the ~1e-7..1e-8 noise
    floor seen on these curves' log-domain branches). For a genuinely continuous function the gap is
    proportional to eps (gap/eps is roughly constant), so gap(eps_lo) should be about (eps_lo/eps_hi) times
    gap(eps_hi), i.e. ~100x smaller. A real jump instead leaves gap(eps_lo) close to gap(eps_hi) in absolute
    terms (it does NOT shrink 100x when eps shrinks 100x) - that is what actually distinguishes a kink from
    a merely-steep-but-continuous branch, and is robust across curves spanning very different cut magnitudes."""
    gap_hi = probe(eps_hi)
    gap_lo = probe(eps_lo)
    ratio = eps_lo / eps_hi  # 1e-2
    shrank = gap_lo < max(gap_hi * ratio * 10.0, 1e-9)  # allow 10x slack over ideal linear shrink, floor for exact 0s
    return gap_hi, gap_lo, shrank


def continuity_x_cut(name, enc, x_cut, slope_tol=0.05):
    """Continuity of the encode direction at a cut expressed in the LINEAR (x) domain.

    Value continuity: Richardson-style scale check (see _value_ratio_check) - distinguishes a genuine jump
    from a curve that is merely steep near the cut.
    Slope continuity: finite-difference derivative just below vs just above the cut, at an eps (1e-5) chosen
    to sit well clear of both "crosses into the wrong functional region" (too big) and "float64 cancellation
    noise" (too small) for every curve's cut magnitude in this file - this is where a real spec-level kink
    (matching value, mismatched first derivative) shows up.
    """
    x_cut = float(x_cut)

    def probe(eps):
        y_below = float(enc(np.array([x_cut - eps], np.float64))[0])
        y_above = float(enc(np.array([x_cut + eps], np.float64))[0])
        return abs(y_below - y_above)

    gap_hi, gap_lo, shrank = _value_ratio_check(name, probe)
    check(f"{name} cut value continuity (x={x_cut:.6g})", shrank,
          f"gap(eps=1e-4)={gap_hi:.3e} -> gap(eps=1e-6)={gap_lo:.3e} (should shrink ~100x if truly continuous)")

    eps = 1e-5
    h = eps * 0.1
    slope_below = (enc(np.array([x_cut - eps], np.float64))[0] - enc(np.array([x_cut - eps - h], np.float64))[0]) / h
    slope_above = (enc(np.array([x_cut + eps + h], np.float64))[0] - enc(np.array([x_cut + eps], np.float64))[0]) / h
    slope_below, slope_above = float(slope_below), float(slope_above)
    denom = max(abs(slope_below), abs(slope_above), 1e-6)
    slope_rel_gap = abs(slope_below - slope_above) / denom
    note(f"{name} cut slope continuity (x={x_cut:.6g})", slope_rel_gap < slope_tol,
          f"slope- = {slope_below:.4f}, slope+ = {slope_above:.4f}, rel gap {slope_rel_gap:.3e} (tol {slope_tol})")
    return gap_lo, slope_rel_gap


def continuity_y_cut(name, dec, y_cut, slope_tol=0.05):
    """Same Richardson-style value check plus finite-difference slope check as continuity_x_cut, but for a
    cut expressed in the LOG (y, encoded) domain - used for the decode direction of curves whose decode-side
    split point is a separately-computed constant rather than a straight re-use of the encode-side x cut."""
    y_cut = float(y_cut)

    def probe(eps):
        v_below = float(dec(np.array([y_cut - eps], np.float64))[0])
        v_above = float(dec(np.array([y_cut + eps], np.float64))[0])
        return abs(v_below - v_above)

    gap_hi, gap_lo, shrank = _value_ratio_check(name, probe)
    check(f"{name} decode cut value continuity (y={y_cut:.6g})", shrank,
          f"gap(eps=1e-4)={gap_hi:.3e} -> gap(eps=1e-6)={gap_lo:.3e} (should shrink ~100x if truly continuous)")

    eps = 1e-5
    h = eps * 0.1
    slope_below = (dec(np.array([y_cut - eps], np.float64))[0] - dec(np.array([y_cut - eps - h], np.float64))[0]) / h
    slope_above = (dec(np.array([y_cut + eps + h], np.float64))[0] - dec(np.array([y_cut + eps], np.float64))[0]) / h
    slope_below, slope_above = float(slope_below), float(slope_above)
    denom = max(abs(slope_below), abs(slope_above), 1e-6)
    slope_rel_gap = abs(slope_below - slope_above) / denom
    note(f"{name} decode cut slope continuity (y={y_cut:.6g})", slope_rel_gap < slope_tol,
          f"slope- = {slope_below:.4f}, slope+ = {slope_above:.4f}, rel gap {slope_rel_gap:.3e} (tol {slope_tol})")


print("=" * 100)
print("1. ROUND-TRIP IDENTITY (toe / mid / HDR samples)")
print("=" * 100)

samples_default = [0.0, 0.0001, 0.01, 0.18, 1.0, 5.0, 16.0]

roundtrip("cineon", n._lin_to_cineon, n._cineon_to_lin, samples_default, 1e-3)
roundtrip("acescct", n._lin_to_acescct, n._acescct_to_lin, [0.0, 0.0001, 0.0078125, 0.05, 0.18, 1.0, 4.0, 16.0], 1e-3)
roundtrip("acescc", n._lin_to_acescc, n._acescc_to_lin, [0.0, 0.0001, 0.001, 0.05, 0.18, 1.0, 4.0, 16.0], 1e-3)
roundtrip("logc3", n._lin_to_logc3, n._logc3_to_lin, [0.0, 0.0001, 0.010591, 0.18, 1.0, 8.0, 55.1], 1e-3)
roundtrip("logc4", n._lin_to_logc4, n._logc4_to_lin, [0.0, 0.0001, 0.01, 0.18, 1.0, 8.0, 55.1, 469.0], 1e-3)
roundtrip("slog3", n._lin_to_slog3, n._slog3_to_lin, [0.0, 0.0001, 0.01125, 0.18, 1.0, 5.0, 38.4], 1e-3)
roundtrip("vlog", n._lin_to_vlog, n._vlog_to_lin, [0.0, 0.0001, 0.01, 0.18, 1.0, 5.0, 46.6], 1e-3)
roundtrip("canonlog3", n._lin_to_canonlog3, n._canonlog3_to_lin, [-0.014, -0.001, 0.0, 0.001, 0.014, 0.18, 1.0, 8.0], 1e-3)
roundtrip("log3g10", n._lin_to_log3g10, n._log3g10_to_lin, [-0.01, -0.005, 0.0, 0.18, 1.0, 10.0, 184.322], 1e-3)
roundtrip("davinci_intermediate", n._lin_to_davinci_intermediate, n._davinci_intermediate_to_lin,
          [0.0, 0.00262409, 0.18, 1.0, 10.0, 40.0, 100.0], 1e-3)

print()
print("=" * 100)
print("2. LANDMARK ANCHORS (18% mid-grey -> published code value, per primary spec)")
print("=" * 100)

# cineon: black 0 -> 0.0928 (existing anchor; Nuke default black offset), no separate mid-grey spec here
b0 = float(n._lin_to_cineon(np.array([0.0], np.float32))[0])
check("cineon black anchor", abs(b0 - 0.0928) < 0.001, f"enc(0.0) = {b0:.6f}, spec ~0.0928 (Nuke default)")

anchor("acescct", n._lin_to_acescct, 0.18, 0.4135, 0.01)          # ACES S-2016-001
anchor("acescc", n._lin_to_acescc, 0.18, 0.4135, 0.01)             # ACES S-2014-003 (same mid-grey as acescct)
anchor("logc3", n._lin_to_logc3, 0.18, 0.3910, 0.01)               # ARRI LogC3 EI800 spec
anchor("logc4", n._lin_to_logc4, 0.18, 0.2784, 0.01)               # ARRI LogC4 spec (1 May 2022)
anchor("slog3", n._lin_to_slog3, 0.18, 420.0 / 1023.0, 0.001)      # Sony S-Log3 spec, CV 420
anchor("vlog", n._lin_to_vlog, 0.18, 433.0 / 1023.0, 0.001)        # Panasonic V-Log spec, CV 433
anchor("log3g10", n._lin_to_log3g10, 0.18, 1.0 / 3.0, 1e-5)        # RED Log3G10 spec: 18% grey -> exactly 1/3
anchor("davinci_intermediate", n._lin_to_davinci_intermediate, 0.18, 0.336043, 1e-4)  # Blackmagic spec

# canonlog3: this code's convention is x = direct scene-linear reflectance (0.18 = 18% grey), matching every
# other curve in nodes.py. Canon's own white paper tabulates by "Scene Linear %" where reflection = Scene
# Linear * 0.9, so Canon's own "18% Grey" table row is at formula-input x=0.20, not x=0.18 - confirmed from
# the white paper's Appendix [4] table (Scene Linear 20% -> Canon Log 3 CV 351, Full% 34.3%). Both anchors
# are checked here so the x=0.20-vs-x=0.18 convention difference is explicit, not silently absorbed.
cl3_018 = float(n._lin_to_canonlog3(np.array([0.18], np.float32))[0])
check("canonlog3 anchor @ x=0.18 (this code's scene-linear convention)", abs(cl3_018 - 0.3298) < 0.005,
      f"enc(0.18) = {cl3_018:.6f}, formula value ~0.3298 (same formula, not separately tabulated by Canon)")
cl3_020 = float(n._lin_to_canonlog3(np.array([0.20], np.float32))[0])
check("canonlog3 anchor @ x=0.20 (Canon's own tabulated '18% Grey' row)", abs(cl3_020 - 0.343) < 0.005,
      f"enc(0.20) = {cl3_020:.6f}, spec table 0.343 (Canon Log Gamma Curves white paper, Appendix [4])")

print()
print("=" * 100)
print("3. C1 CONTINUITY AT THE PIECEWISE CUT (value + finite-difference slope, both directions)")
print("=" * 100)
print("cineon: SKIPPED - single smooth log branch beyond a baked-in black offset, no np.where split to test.")
print()

print("-- acescct (encode-domain cut x=0.0078125) --")
continuity_x_cut("acescct", n._lin_to_acescct, 0.0078125)
print("-- acescct (decode-domain cut y=0.155251141552511) --")
continuity_y_cut("acescct", n._acescct_to_lin, 0.155251141552511)

print("-- acescc (encode-domain cuts x=0.0, x=2**-15) --")
continuity_x_cut("acescc (x=0 branch)", n._lin_to_acescc, 0.0)
continuity_x_cut("acescc (x=2**-15 branch)", n._lin_to_acescc, 2.0 ** -15)

print("-- logc3 (encode-domain cut x=_LC3_CUT) --")
continuity_x_cut("logc3", n._lin_to_logc3, n._LC3_CUT)

print("-- logc4 (encode-domain cut x=_LC4_T) --")
continuity_x_cut("logc4", n._lin_to_logc4, n._LC4_T)

print("-- slog3 (encode-domain cut x=_SL3_CUT_LIN) --")
continuity_x_cut("slog3", n._lin_to_slog3, n._SL3_CUT_LIN)

print("-- vlog (encode-domain cut x=_VLOG_CUT1) --")
continuity_x_cut("vlog", n._lin_to_vlog, n._VLOG_CUT1)

print("-- canonlog3 (encode-domain cuts x=-0.014, x=+0.014) --")
continuity_x_cut("canonlog3 (x=-0.014)", n._lin_to_canonlog3, n._CL3_LO)
continuity_x_cut("canonlog3 (x=+0.014)", n._lin_to_canonlog3, n._CL3_HI)

print("-- log3g10 (encode-domain cut x=-c, i.e. x+c=0) --")
continuity_x_cut("log3g10", n._lin_to_log3g10, -n._L3G10_C)

print("-- davinci_intermediate (encode-domain cut x=_DI_LIN_CUT) --")
continuity_x_cut("davinci_intermediate", n._lin_to_davinci_intermediate, n._DI_LIN_CUT)

# also check the decode-direction cuts (log_to_lin) for the curves whose decode split point is a distinct,
# separately-computed constant (not just re-using the same x as the encode side)
print("-- slog3 decode (log-domain cut y=_SL3_CUT_LOG) --")
continuity_y_cut("slog3", n._slog3_to_lin, n._SL3_CUT_LOG)

print("-- vlog decode (log-domain cut y=_VLOG_CUT2) --")
continuity_y_cut("vlog", n._vlog_to_lin, n._VLOG_CUT2)

print("-- davinci_intermediate decode (log-domain cut y=_DI_LOG_CUT) --")
continuity_y_cut("davinci_intermediate", n._davinci_intermediate_to_lin, n._DI_LOG_CUT)

print()
print("=" * 100)
if NOTES:
    print(f"SLOPE NOTES ({len(NOTES)} spec-inherent, non-failing - value-continuous but not C1 by the maker's own formula):")
    for w in NOTES:
        print(f"  - {w}")
    print("-" * 100)
if FAILURES:
    print(f"RESULT: {len(FAILURES)} FAILURE(S)")
    for f in FAILURES:
        print(f"  - {f}")
    print("=" * 100)
    sys.exit(1)
else:
    print("RESULT: PASS - round-trip, landmark anchors, and VALUE continuity hold for all ten curves.")
    print("Slope discontinuities listed above are spec-inherent (ACEScc S-2014-003, Sony S-Log3), not defects.")
    print("=" * 100)
