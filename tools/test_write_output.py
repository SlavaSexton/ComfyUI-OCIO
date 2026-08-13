"""Regression: OCIO Write's EXR writer and its video colourspace default (run: python tools/test_write_output.py).

Two things changed on 2026-08-12 and neither had any coverage, so both could regress silently.

1. EXR IS WRITTEN THROUGH OpenEXR, NOT cv2. cv2 refuses EXR unless OPENCV_IO_ENABLE_OPENEXR is set in the
   environment BEFORE cv2 is imported; a ComfyUI Desktop launch does not set it, and a real LTX-2.5 run died on
   "OpenEXR codec is disabled" after two minutes of generation. The switch also flipped the channel order
   requirement: cv2 wanted BGR (rgb[..., ::-1]), OpenEXR wants RGB. Getting that wrong swaps red and blue in
   every delivered frame, silently, which is why the order is asserted with a deliberately unequal test frame.

2. A VIDEO CONTAINER DEFAULTS TO Rec.709, NOT sRGB. The pack used to default a movie to the working space
   (sRGB - Display), tagging every ProRes and DNxHR with trc=iec61966-2-1 - the computer-display curve - where a
   deliverable wants the Rec.709 one. The Python default and the JS mirror in web/ocio_io.js must agree, or the
   node shows one colourspace and the backend writes another; that agreement is checked here too.
"""
import importlib.util
import os
import re
import sys
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

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


def _read_exr(p):
    """RGB(A) float array. The channel structure is owned by the File and goes EMPTY once it closes, so the
    pixels must be copied out inside the with-block - reading them afterwards reports "no channels" on a
    perfectly good file."""
    import OpenEXR
    with OpenEXR.File(p) as f:
        ch = f.channels()
        key = "RGBA" if "RGBA" in ch else "RGB"
        hdr = dict(f.header())
        px = np.array(ch[key].pixels, copy=True)
    return px, hdr


def main():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ocio_write_test_")
    io_nodes = _load_io_nodes(tmp)

    try:
        import OpenEXR  # noqa: F401
    except ImportError:
        print("[SKIP] OpenEXR is not installed; the pack falls back to cv2 and this test cannot check the "
              "preferred path. Install it (see requirements.txt).")
        return

    # ---------------------------------------------------------------- EXR writer
    H, W = 6, 8
    rgb = np.zeros((H, W, 3), np.float32)
    rgb[..., 0], rgb[..., 1], rgb[..., 2] = 0.90, 0.50, 0.10       # deliberately unequal: R > G > B
    rgb[0, 0] = (-0.25, 1.75, 3.5)                                 # outside 0..1 in every channel
    alpha = np.full((H, W), 0.25, np.float32)

    for bit, want_dt in (("16f", np.float16), ("32f", np.float32)):
        for al, want_ch in ((None, 3), (alpha, 4)):
            p = os.path.join(tmp, f"w_{bit}_{want_ch}.exr")
            io_nodes._save_still(p, rgb, "exr", bit, al, "ACEScg", "zip")
            px, _ = _read_exr(p)
            assert px.dtype == want_dt, f"{bit}/{want_ch}ch: dtype {px.dtype}, expected {want_dt}"
            assert px.shape == (H, W, want_ch), f"{bit}/{want_ch}ch: shape {px.shape}"
            r, g, b = (float(px[1, 1, i]) for i in range(3))
            assert r > g > b and abs(r - 0.90) < 0.01 and abs(b - 0.10) < 0.01, (
                f"{bit}/{want_ch}ch: CHANNEL ORDER WRONG - got R={r:.3f} G={g:.3f} B={b:.3f}, expected "
                "0.90/0.50/0.10. The OpenEXR writer must be fed RGB; only the cv2 path needed BGR.")
            if al is not None:
                assert abs(float(px[1, 1, 3]) - 0.25) < 0.01, f"{bit}: alpha not preserved"
            lo, hi = float(px[0, 0, :3].min()), float(px[0, 0, :3].max())
            assert lo < -0.2 and hi > 3.0, f"{bit}/{want_ch}ch: out-of-range clipped, got {lo}..{hi}"
    print("[PASS] EXR: RGB order, 16f/32f, RGB and RGBA, alpha, values outside 0..1 preserved")

    for comp in ("zip", "zips", "piz", "pxr24", "dwaa", "dwab", "rle", "none"):
        p = os.path.join(tmp, f"c_{comp}.exr")
        io_nodes._save_still(p, rgb, "exr", "16f", None, "ACEScg", comp)
        _, hdr = _read_exr(p)
        got = str(hdr.get("compression", "")).split(".")[-1].replace("_COMPRESSION", "").lower()
        want = "no" if comp == "none" else comp
        assert got == want, f"compression {comp} came back as {got}"
    print("[PASS] EXR: all eight compression choices round-trip")

    # attributes prove OpenEXR wrote the file - cv2 cannot write a single custom attribute
    p = os.path.join(tmp, "attrs.exr")
    io_nodes._save_still(p, rgb, "exr", "16f", None, "ACEScg", "zip",
                         {"cameraMake": "TESTCAM", "nominalFocalLength": 35.0})
    _, hdr = _read_exr(p)
    assert hdr.get("cameraMake") == "TESTCAM", (
        "header attributes did not survive, so the file was NOT written by OpenEXR - the pack has fallen back "
        f"to cv2, which writes none. header keys: {sorted(hdr)}")
    assert abs(float(hdr.get("nominalFocalLength", 0)) - 35.0) < 1e-3
    print("[PASS] EXR: header attributes survive, which only the OpenEXR path can do")

    # ---------------------------------------------------------------- video colourspace default
    vid = io_nodes._auto_output_cs("video", "exr")
    assert "709" in vid or "1886" in vid, (
        f"a video container defaults to {vid!r}. It must be a Rec.709 display space: sRGB tags a deliverable "
        "with the computer-display transfer function.")
    assert vid != io_nodes.WORKING, f"video default is still the working space {vid!r}"
    print(f"[PASS] video container defaults to {vid!r}, not the sRGB working space")

    assert io_nodes._auto_output_cs("sequence", "exr") == "ACEScg", "EXR sequence must still default to ACEScg"
    assert io_nodes._auto_output_cs("still image", "png") == io_nodes.WORKING, \
        "PNG must still default to the sRGB working space"
    print("[PASS] still/sequence defaults unchanged (EXR -> ACEScg, PNG -> sRGB - Display)")

    tags = io_nodes._video_color_tags(vid)
    joined = " ".join(tags)
    assert "-color_trc" in tags and tags[tags.index("-color_trc") + 1] == "bt709", (
        f"the Rec.709 default must produce trc=bt709, got: {joined}")
    assert "iec61966-2-1" not in joined, f"the sRGB transfer function is still being written: {joined}"
    assert tags[tags.index("-color_primaries") + 1] == "bt709"
    assert tags[tags.index("-colorspace") + 1] == "bt709"
    print("[PASS] ffmpeg tags for the video default are bt709 primaries / bt709 trc / bt709 matrix")

    # ---------------------------------------------------------------- Python and JS must not drift
    js = open(os.path.join(_ROOT, "web", "ocio_io.js"), encoding="utf-8").read()
    m = re.search(r'CS_REC709_DISPLAY\s*=\s*"([^"]+)"', js)
    assert m, "web/ocio_io.js no longer defines CS_REC709_DISPLAY; the front end and backend can now disagree"
    assert m.group(1) == vid, (
        f"MIRROR DRIFT: io_nodes says {vid!r}, web/ocio_io.js says {m.group(1)!r}. The node would show one "
        "colourspace and the backend would write another.")
    assert re.search(r'container\s*===\s*"video"\s*\)\s*return\s+CS_REC709_DISPLAY', js), \
        "autoOutCs in web/ocio_io.js no longer returns the Rec.709 default for a video container"
    print(f"[PASS] web/ocio_io.js mirrors the same value: {m.group(1)!r}")

    print("\nALL CHECKS PASSED - EXR goes through OpenEXR, and a movie is Rec.709 on both sides")


if __name__ == "__main__":
    main()
