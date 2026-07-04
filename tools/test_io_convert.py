"""Regression: io_nodes._convert must actually convert (run: python tools/test_io_convert.py). Needs opencolorio.

_convert is the colorspace conversion used by OCIORead (io_nodes.py load), OCIOWrite, AND the /ocio/thumb
on-node preview. The M1 processor-cache refactor changed _apply_processor to require a CPUProcessor, but left
_convert passing a raw Processor (getProcessor) - which has no .apply - so every in != out conversion raised
"'Processor' object has no attribute 'apply'". The color-node tests never caught it because the ColorSpace node
goes through _cached_cpu_processor directly, not _convert. This locks the io_nodes path in.
"""
import importlib.util
import os
import sys
import types

import numpy as np
import torch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_io_nodes():
    """Load io_nodes with its relative 'from .nodes import ...' satisfied, without a real package install."""
    pkg = types.ModuleType("ocio_pkg")
    pkg.__path__ = [_ROOT]
    sys.modules["ocio_pkg"] = pkg
    for name in ("nodes", "io_nodes"):
        spec = importlib.util.spec_from_file_location(f"ocio_pkg.{name}", os.path.join(_ROOT, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"ocio_pkg.{name}"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["ocio_pkg.io_nodes"]


def main():
    io_nodes = _load_io_nodes()
    rng = np.random.default_rng(7)
    img = torch.from_numpy(rng.random((1, 16, 16, 3)).astype(np.float32))

    # 1) in == out is identity, no OCIO call
    same = io_nodes._convert(img, "ACEScg", "ACEScg")
    assert torch.equal(same, img), "in==out must be identity"

    # 2) real conversion in != out: must NOT raise (the .apply regression), must change pixels
    out = io_nodes._convert(img, "sRGB - Display", "ACEScg")
    assert out.shape == img.shape, f"shape changed: {out.shape}"
    assert torch.isfinite(out).all(), "non-finite output"
    diff = float((out - img).abs().max())
    assert diff > 1e-4, f"conversion had no effect (max abs diff {diff}) - processor likely not applied"
    print(f"[PASS] sRGB - Display -> ACEScg applied, max abs pixel delta {diff:.4f}")

    # 3) a display-target conversion (the case the preview hit first) also works
    out2 = io_nodes._convert(img, "ACEScg", "Gamma 2.2 Rec.709 - Display")
    assert torch.isfinite(out2).all() and float((out2 - img).abs().max()) > 1e-4, "display-target convert failed"
    print("[PASS] ACEScg -> Gamma 2.2 Rec.709 - Display applied")

    # 4) round-trip returns close to the original (sanity that it is a real, invertible transform)
    back = io_nodes._convert(out, "ACEScg", "sRGB - Display")
    rt = float((back - img).abs().max())
    assert rt < 1e-3, f"round-trip error too high: {rt}"
    print(f"[PASS] round-trip sRGB<->ACEScg max abs error {rt:.2e}")

    # 5) _lut_rgba8: the 3D LUT baked for the WebGL video viewport. Identity ramp + axis order + non-identity.
    n, data, _shaper = io_nodes._lut_rgba8("sRGB - Display", "sRGB - Display", 33, False)
    lut = np.frombuffer(data, np.uint8).reshape(n * n * n, 4)
    assert n == 33 and lut.shape[0] == 33 ** 3, f"bad LUT size {n}"
    assert (lut[:, 3] == 255).all(), "LUT alpha must be 255"
    assert lut[0, :3].tolist() == [0, 0, 0], "identity black corner"
    assert lut[-1, :3].tolist() == [255, 255, 255], "identity white corner"
    # texImage3D layout: R (x) fastest, so index n-1 is pure-red max, index (n-1)*n is pure-green max
    assert lut[n - 1, :3].tolist() == [255, 0, 0], f"axis order: pure-red texel wrong {lut[n - 1, :3].tolist()}"
    assert lut[(n - 1) * n, :3].tolist() == [0, 255, 0], f"axis order: pure-green texel wrong {lut[(n - 1) * n, :3].tolist()}"
    _, dconv, _ = io_nodes._lut_rgba8("sRGB - Display", "ACEScg", 33, False)
    lconv = np.frombuffer(dconv, np.uint8).reshape(-1, 4)
    assert (lut[:, :3] != lconv[:, :3]).any(), "sRGB->ACEScg LUT must differ from identity"
    _, draw, _ = io_nodes._lut_rgba8("sRGB - Display", "ACEScg", 33, True)  # raw=True -> identity even if in!=out
    assert (np.frombuffer(draw, np.uint8).reshape(-1, 4)[:, :3] == lut[:, :3]).all(), "raw=True must be identity"
    print("[PASS] _lut_rgba8 identity ramp, axis order (R fastest), non-identity, raw passthrough")

    print("\nALL CHECKS PASSED - io_nodes._convert converts through a CPU processor")


if __name__ == "__main__":
    main()
