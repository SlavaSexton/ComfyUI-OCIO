"""Dependency-free verification of the ACEScct transfer used by OCIO LogConvert (method=acescct).
Run from the pack root: python tools/test_acescct.py"""
import os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nodes import _lin_to_acescct, _acescct_to_lin

# scene-linear test values spanning toe, mid grey, and HDR highlights
x = np.array([0.0, 0.0001, 0.0078125, 0.05, 0.18, 1.0, 4.0, 16.0], dtype=np.float32)
y = _lin_to_acescct(x)
back = _acescct_to_lin(y)
err = float(np.max(np.abs(back.astype(np.float64) - x.astype(np.float64))))

print("scene-linear :", x)
print("ACEScct      :", y)
print("round-trip   :", back)
print("max abs error:", err)
# mid grey 0.18 should land near ACEScct 0.4135 (sanity anchor from the ACES spec)
mid = float(_lin_to_acescct(np.array([0.18], dtype=np.float32))[0])
print("ACEScct(0.18):", mid, "(spec ~0.4135)")
assert err < 1e-4, f"round-trip error too high: {err}"
assert abs(mid - 0.4135) < 0.01, f"mid-grey anchor off: {mid}"
print("PASS: ACEScct round-trip < 1e-4 and mid-grey anchor matches the spec.")
