"""Dependency-free verification of the ARRI LogC4 transfer used by OCIO LogConvert (curve=logc4).
Round-trips the curve, checks the published ARRI landmark anchors, and confirms parity with oumad
ComfyUI_Gear's LogC4 Decode across the log region (encoded V >= c: all mids/highlights/ceiling),
quantifying the near-black toe difference (where the ARRI spec and oumad's decoder diverge).
Run from the pack root: python tools/test_logc4.py"""
import os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nodes import _lin_to_logc4, _logc4_to_lin, _LC4_A, _LC4_B, _LC4_C, _LC4_S, _LC4_T

# scene-linear values spanning toe, mid grey, and HDR highlights up to the LogC4 ceiling (~469.8)
x = np.array([0.0, 0.0001, 0.01, 0.18, 1.0, 8.0, 55.1, 469.0], dtype=np.float32)
y = _lin_to_logc4(x)
back = _logc4_to_lin(y)
rel = float(np.max(np.abs(back.astype(np.float64) - x.astype(np.float64)) / (np.abs(x) + 1e-3)))
print("scene-linear :", x)
print("LogC4        :", y)
print("round-trip   :", back)
print("max rel error:", rel)

# landmark anchors from the ARRI LogC4 spec (1 May 2022)
mid = float(_lin_to_logc4(np.array([0.18], np.float32))[0])         # mid-gray 0.18 -> ~0.2784
ceil_lin = float(_logc4_to_lin(np.array([1.0], np.float32))[0])     # encoded 1.0 -> ~469.8 linear ceiling
black_lin = float(_logc4_to_lin(np.array([0.0], np.float32))[0])    # encoded 0.0 -> t (~-0.01806)
print("LogC4(0.18)  :", mid, "(spec ~0.2784)")
print("lin(V=1.0)   :", ceil_lin, "(spec ~469.8 ceiling)")
print("lin(V=0.0)   :", black_lin, "(spec t ~-0.01806)")

# parity vs oumad ComfyUI_Gear gear/logc4.py decompress(): identical formula in the log region V >= c,
# a different (non-self-inverse) linear toe below. We follow the ARRI spec (split at V=0) everywhere.
def oumad_decompress(v):
    v = np.clip(np.asarray(v, np.float64), 0.0, 1.0)
    lin_log = (2.0 ** (14.0 * (v - _LC4_C) / _LC4_B + 6.0) - 64.0) / _LC4_A
    lin_lin = (v - _LC4_C) * _LC4_S + _LC4_T
    return np.where(v >= _LC4_C, lin_log, lin_lin)

v = np.linspace(0.0, 1.0, 1001)
ours = _logc4_to_lin(v.astype(np.float32)).astype(np.float64)  # float32 = the real node path
theirs = oumad_decompress(v)                                    # float64 reference
log = v >= _LC4_C
# relative, since the node stores float32: near the ceiling the ~5000 slope amplifies float32 ULP to ~1e-4
# absolute, but the log-region formula is identical, so the relative match is exact to float precision.
parity_log_rel = float(np.max(np.abs(ours[log] - theirs[log]) / (np.abs(theirs[log]) + 1e-3)))
toe_div = float(np.max(np.abs(ours[v < _LC4_C] - theirs[v < _LC4_C])))
print("parity vs oumad, log region V>=c :", parity_log_rel, "(relative; identical formula, float32 storage)")
print("toe divergence  V<c (near-black) :", toe_div, "(ARRI-spec here; oumad toe is not self-inverse)")

assert rel < 1e-3, f"round-trip relative error too high: {rel}"
assert abs(mid - 0.2784) < 0.01, f"mid-gray anchor off: {mid}"
assert abs(ceil_lin - 469.8) < 1.0, f"ceiling anchor off: {ceil_lin}"
assert abs(black_lin - _LC4_T) < 1e-5, f"black anchor off: {black_lin}"
assert parity_log_rel < 1e-5, f"log-region parity vs oumad broken: {parity_log_rel}"
print("PASS: LogC4 round-trips, hits the ARRI spec anchors, and matches oumad across the log region.")
