"""Regression: the DPX reader (run: python tests/test_dpx_read.py). No external files, no dependencies.

Why this exists. `.dpx` was advertised as supported and did not work. cv2.imread returns None on a real 10-bit
plate (measured on Nuke- and DaVinci-written 2048x1152 files, all three IMREAD flags), PIL cannot open DPX at
all, and the raise in _read_still deliberately excluded `.dpx` - so the pack fell through to PIL and died with
"UnidentifiedImageError" on the single most common plate format in film finishing. imageio does read DPX and
hands back uint8, silently discarding two of the ten bits, which is worse than failing.

The DPX files are BUILT here rather than shipped: a plate is somebody's material, and its path has no business
in a repository. Synthetic files also let the test assert exact code values, which a real plate cannot.

Locks: the 10-bit "filled, method A" unpack (three samples left-aligned in a 32-bit word), 8- and 16-bit,
both endiannesses, RGB and RGBA, exact normalisation by the real code ceiling (code/1023, not value/65535),
and a clear error rather than a confusing one on a non-DPX.
"""
import importlib.util
import os
import struct
import sys
import tempfile
import types

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_io_nodes(tmp):
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = fp.get_temp_directory = fp.get_input_directory = lambda: tmp
    fp.get_filename_list = lambda *a, **k: []
    sys.modules.setdefault("folder_paths", fp)
    pkg = types.ModuleType("ocio_pkg")
    pkg.__path__ = [_ROOT]
    sys.modules["ocio_pkg"] = pkg
    for name in ("nodes", "io_nodes"):
        spec = importlib.util.spec_from_file_location(f"ocio_pkg.{name}", os.path.join(_ROOT, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"ocio_pkg.{name}"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["ocio_pkg.io_nodes"]


def build_dpx(path, w, h, codes, bits=10, channels=3, big=True, packing=1):
    """Write a minimal but valid DPX. `codes` is a flat list of integer samples in file order."""
    E = ">" if big else "<"
    hdr = bytearray(2048)
    hdr[0:4] = b"SDPX" if big else b"XPDS"
    struct.pack_into(E + "I", hdr, 4, 2048)              # image offset
    struct.pack_into(E + "I", hdr, 16, 0)                # file size (readers here do not rely on it)
    struct.pack_into(E + "I", hdr, 772, w)               # pixels per line
    struct.pack_into(E + "I", hdr, 776, h)               # lines per element
    hdr[800] = 50 if channels == 3 else 51               # descriptor: RGB / RGBA
    hdr[801] = 0                                         # transfer: user defined
    hdr[802] = 0                                         # colorimetric
    hdr[803] = bits
    struct.pack_into(E + "H", hdr, 804, packing)
    struct.pack_into(E + "H", hdr, 806, 0)               # encoding: none
    body = bytearray()
    if bits == 10 and packing == 1:
        assert len(codes) % 3 == 0, "10-bit method A needs whole 32-bit words"
        for i in range(0, len(codes), 3):
            a, b, c = codes[i], codes[i + 1], codes[i + 2]
            body += struct.pack(E + "I", ((a & 0x3FF) << 22) | ((b & 0x3FF) << 12) | ((c & 0x3FF) << 2))
    elif bits == 16:
        for v in codes:
            body += struct.pack(E + "H", v & 0xFFFF)
    elif bits == 8:
        body += bytes(v & 0xFF for v in codes)
    else:
        raise AssertionError(f"builder does not make {bits}-bit packing {packing}")
    with open(path, "wb") as f:
        f.write(bytes(hdr))
        f.write(bytes(body))
    return path


def main():
    tmp = tempfile.mkdtemp(prefix="ocio_dpx_test_")
    io_nodes = _load_io_nodes(tmp)

    # ---- 10-bit, filled method A, RGB, big-endian: exact code values including both extremes
    W, H = 4, 2
    codes = [0, 1, 2, 511, 512, 513, 1021, 1022, 1023,
             340, 682, 1023, 95, 685, 1023, 100, 500, 900, 0, 1023, 0, 7, 77, 777]
    codes = codes[:W * H * 3]
    p = build_dpx(os.path.join(tmp, "t10.dpx"), W, H, codes, bits=10)
    a = io_nodes._read_dpx(p)
    assert a.shape == (H, W, 3), f"shape {a.shape}, expected {(H, W, 3)}"
    want = np.array(codes, np.float32).reshape(H, W, 3) / 1023.0
    err = np.abs(a - want).max()
    assert err == 0.0, (f"10-bit values are not exact: max error {err:.9f}\n  got  {a.reshape(-1)[:9]}\n"
                        f"  want {want.reshape(-1)[:9]}")
    print(f"[PASS] 10-bit filled method A, RGB, big-endian: {a.size} samples exact (code/1023)")
    assert abs(float(a.reshape(-1)[8]) - 1.0) < 1e-9, "code 1023 must normalise to exactly 1.0"
    assert float(a.reshape(-1)[0]) == 0.0, "code 0 must normalise to exactly 0.0"
    print("[PASS] the extremes land on 0.0 and 1.0, so the ceiling is 1023 and not 65535")

    # ---- little-endian variant
    p = build_dpx(os.path.join(tmp, "t10le.dpx"), W, H, codes, bits=10, big=False)
    b = io_nodes._read_dpx(p)
    assert np.abs(b - want).max() == 0.0, "little-endian (XPDS) 10-bit decode differs"
    print("[PASS] little-endian XPDS decodes identically")

    # ---- 16-bit
    c16 = [0, 1, 32767, 32768, 65534, 65535, 12345, 23456, 34567,
           1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15][:W * H * 3]
    p = build_dpx(os.path.join(tmp, "t16.dpx"), W, H, c16, bits=16)
    c = io_nodes._read_dpx(p)
    want16 = np.array(c16, np.float32).reshape(H, W, 3) / 65535.0
    assert np.abs(c - want16).max() == 0.0, "16-bit decode is not exact"
    print("[PASS] 16-bit RGB exact (code/65535)")

    # ---- 8-bit
    c8 = [0, 1, 127, 128, 254, 255] * 4
    c8 = c8[:W * H * 3]
    p = build_dpx(os.path.join(tmp, "t8.dpx"), W, H, c8, bits=8)
    d = io_nodes._read_dpx(p)
    assert np.abs(d - np.array(c8, np.float32).reshape(H, W, 3) / 255.0).max() == 0.0
    print("[PASS] 8-bit RGB exact (code/255)")

    # ---- RGBA descriptor. Width 3 because 10-bit method A packs three samples per 32-bit word, so
    # width * channels must be a whole number of words: 3 * 4 = 12 works, 2 * 4 = 8 does not. A width that
    # does not divide is not an error - the reader hands those to ffmpeg on purpose rather than guessing at
    # the line padding, which is also why a real 2048-wide RGBA 10-bit plate takes the ffmpeg route.
    W2, H2 = 3, 2
    ca = [100, 200, 300, 1023, 400, 500, 600, 0, 700, 800, 900, 512,
          1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    p = build_dpx(os.path.join(tmp, "trgba.dpx"), W2, H2, ca, bits=10, channels=4)
    e = io_nodes._read_dpx(p)
    assert e.shape == (H2, W2, 4), f"RGBA shape {e.shape}, expected {(H2, W2, 4)}"
    wanta = np.array(ca, np.float32).reshape(H2, W2, 4) / 1023.0
    assert np.abs(e - wanta).max() == 0.0, "RGBA 10-bit values are not exact"
    assert abs(float(e[0, 0, 3]) - 1.0) < 1e-9, "alpha channel not decoded (expected code 1023 -> 1.0)"
    print("[PASS] RGBA descriptor (51) yields four exact channels with alpha in place")

    # ---- the whole way through _read_still, which is what OCIO Read calls
    p = build_dpx(os.path.join(tmp, "via_still.dpx"), W, H, codes, bits=10)
    rgb, alpha = io_nodes._read_still(p)[..., :3], io_nodes._read_still(p)[..., 3]
    assert np.abs(rgb - want).max() == 0.0, "_read_still changed the values (BGR swap? renormalisation?)"
    assert float(alpha.min()) == 1.0, "a DPX without alpha must come back with alpha 1.0"
    print("[PASS] _read_still returns the same values, RGB order kept, alpha synthesised as 1.0")

    # ---- a non-DPX must fail CLEARLY, not with PIL's UnidentifiedImageError
    bad = os.path.join(tmp, "notadpx.dpx")
    with open(bad, "wb") as f:
        f.write(b"NOPE" + bytes(4000))
    try:
        io_nodes._read_dpx(bad)
    except RuntimeError as ex:
        assert "not a DPX" in str(ex), f"unclear message: {ex}"
        print(f"[PASS] a non-DPX raises a clear RuntimeError: {str(ex)[:60]}...")
    else:
        raise AssertionError("a file with a bad magic was accepted")

    short = os.path.join(tmp, "short.dpx")
    with open(short, "wb") as f:
        f.write(b"SDPX" + bytes(100))
    try:
        io_nodes._read_dpx(short)
    except RuntimeError as ex:
        assert "too short" in str(ex), f"unclear message: {ex}"
        print("[PASS] a truncated file raises a clear RuntimeError")
    else:
        raise AssertionError("a truncated file was accepted")

    print("\nALL CHECKS PASSED - DPX reads exactly, at the real bit depth, without cv2 or PIL")


if __name__ == "__main__":
    main()
