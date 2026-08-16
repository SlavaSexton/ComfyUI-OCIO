"""Regression: the thumbnail route decodes video the same way the player does (run: python tools/test_thumb_no_clamp.py).

A video's picture is almost always YUV, and asking ffmpeg for an RGB pixel format makes ffmpeg do the matrix.
Every integer RGB format it can hand back - rgb24, rgb48le - is unsigned and bounded, so it CLAMPS on the way
out. That is not rounding loss: a legal limited-range YUV signal converts to RGB values outside 0..1 whenever
the colour is saturated, because the matrix pushes a channel past the luma it was carried by.

`_read_video` was moved to planar YUV plus a float32 matrix for exactly this reason. `_read_video_frame` - the
decode behind /ocio/thumb - was left asking for rgb48le, so the same file read two ways gave two different
pictures: measured on a ProRes 4444 written by this pack's own writer, the player path returned min -0.0043 /
max +1.0121 with 0.30% of samples out of range, and the thumbnail path returned exactly 0.0 and 1.0 with none.

The fixture here is not a sampled clip, it is a CONSTRUCTED one, so the expected numbers are arithmetic rather
than an observation that could drift. One frame of flat YUV - Y at limited-range black, Cb at its maximum, Cr
neutral - encoded lossless (FFV1, yuv444p, tagged bt709 / tv) so nothing between the write and the read can
move a code. Through the ITU BT.709 matrix that signal is:

    y = (16-16)/219 = 0, cb = (240-128)/224 = 0.5, cr = 0
    R = y + 1.5748*cr                       =  0.0
    G = y - (2*(1-Kb)*Kb/Kg)*cb - ...       = -0.09366
    B = y + 2*(1-Kb)*cb                     =  0.92780

Green is NEGATIVE and blue is inside the range - a value the file legally carries and rgb48le cannot express.
So the check is not "the two functions agree" (they would agree on a clamped picture too); it is that the one
number which cannot survive a clamp comes back.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

W, H = 32, 16
Y_CODE, CB_CODE, CR_CODE = 16, 240, 128      # limited-range black, Cb at maximum, Cr neutral
WANT_G = -0.0936602                          # from the matrix above, not from a measurement
WANT_B = 0.9278000
TOL = 2e-4                                   # lossless container; the slack is for float32 rounding only


def _load(tmp="."):
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


def _make_clip(io, path):
    """One frame of flat YUV, encoded LOSSLESS so the read gets back the codes that were written.

    THE TAGS GO BEFORE -i, AND THAT IS THE WHOLE TRICK. After -i they describe the OUTPUT, so ffmpeg reads
    raw yuv444p as full-range and converts it on the way in: measured here, 16/240/128 was written to the
    file as 30/227/119, and the negative green this fixture exists to carry was gone before any decode ran.
    Before -i they tell ffmpeg how to READ the input, and the codes survive. They are repeated afterwards so
    the stream is tagged too - otherwise color_space/color_range come back empty and the matrix is guessed.

    The codes are then read back off the disk and checked, because a fixture that silently changed is a test
    measuring the wrong thing while reporting green."""
    import numpy as np
    planes = np.concatenate([
        np.full(W * H, Y_CODE, np.uint8),
        np.full(W * H, CB_CODE, np.uint8),
        np.full(W * H, CR_CODE, np.uint8),
    ])
    raw = os.path.join(os.path.dirname(path), "flat.yuv")
    planes.tofile(raw)
    cmd = [io._FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv444p",
           "-s", f"{W}x{H}", "-r", "24", "-colorspace", "bt709", "-color_range", "tv", "-i", raw,
           "-c:v", "ffv1", "-level", "3", "-pix_fmt", "yuv444p",
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-color_range", "tv", "-frames:v", "1", path]
    proc = subprocess.run(cmd, capture_output=True)
    assert proc.returncode == 0, f"could not build the fixture: {proc.stderr.decode('utf-8', 'ignore')[:300]}"
    os.remove(raw)

    back = os.path.join(os.path.dirname(path), "back.yuv")
    dec = subprocess.run([io._FFMPEG, "-v", "error", "-y", "-i", path, "-f", "rawvideo",
                          "-pix_fmt", "yuv444p", back], capture_output=True)
    assert dec.returncode == 0, f"could not read the fixture back: {dec.stderr.decode('utf-8', 'ignore')[:300]}"
    a = np.fromfile(back, dtype=np.uint8)
    os.remove(back)
    got = (int(a[0]), int(a[W * H]), int(a[2 * W * H]))
    assert got == (Y_CODE, CB_CODE, CR_CODE), (
        f"the fixture does not hold the codes it was given: wrote {(Y_CODE, CB_CODE, CR_CODE)}, the file has "
        f"{got}. ffmpeg converted on the way in, and every number below would be measuring that instead.")


def check_the_thumb_decode_keeps_out_of_range(io, path):
    """The defect itself: /ocio/thumb's decode must not flatten what the file carries."""
    rgb = io._read_video_frame(path)
    g, b = float(rgb[..., 1].min()), float(rgb[..., 2].max())
    assert abs(g - WANT_G) < TOL, (
        f"the thumbnail decode returned green {g:.6f}, the file carries {WANT_G:.6f}. "
        f"{'Clamped at zero - ffmpeg did the matrix into an unsigned RGB format.' if g >= -1e-6 else ''}")
    assert abs(b - WANT_B) < TOL, f"blue came back {b:.6f}, expected {WANT_B:.6f}"


def check_both_decode_paths_agree(io, path):
    """Two routes onto the same frame that disagree is the shape of the original report - one picture in the
    player, another in the thumbnail, from one file."""
    import numpy as np
    a = io._read_video_frame(path)
    b = io._read_video(path, 0, 1)[0][0]
    d = float(np.abs(a - b).max())
    assert d < 1e-6, f"the thumbnail and the player decoded the same frame differently; worst channel {d:.6f}"


def check_an_rgb_source_is_untouched(io, tmp):
    """A stream that is ALREADY RGB has no matrix to do, so it must keep the old path - and keep working. The
    narrow fix would have been to always ask for planar YUV, which would make ffmpeg convert RGB -> YUV -> RGB
    for nothing."""
    import numpy as np
    path = os.path.join(tmp, "rgb.mkv")
    px = np.zeros((H, W, 3), np.uint16)
    px[..., 0] = 65535                                        # pure red, unambiguous through any path
    raw = os.path.join(tmp, "flat.rgb")
    px.tofile(raw)
    cmd = [io._FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb48le",
           "-s", f"{W}x{H}", "-r", "24", "-i", raw,
           "-c:v", "ffv1", "-level", "3", "-pix_fmt", "gbrp16le", "-frames:v", "1", path]
    proc = subprocess.run(cmd, capture_output=True)
    assert proc.returncode == 0, f"could not build the RGB fixture: {proc.stderr.decode('utf-8', 'ignore')[:300]}"
    os.remove(raw)
    rgb = io._read_video_frame(path)
    assert abs(float(rgb[..., 0].max()) - 1.0) < 1e-4, f"red came back {float(rgb[..., 0].max()):.6f}"
    assert float(rgb[..., 1].max()) < 1e-4, "an RGB source picked up a matrix it should not have"


def main():
    io = _load()
    if not io._FFMPEG:
        print("  SKIP: ffmpeg is not available, so the decode paths were NOT exercised")
        return 0
    failures = []
    tmp = tempfile.mkdtemp(prefix="ocio_thumb_")
    try:
        clip = os.path.join(tmp, "flat.mkv")
        _make_clip(io, clip)
        for fn, args in ((check_the_thumb_decode_keeps_out_of_range, (io, clip)),
                         (check_both_decode_paths_agree, (io, clip)),
                         (check_an_rgb_source_is_untouched, (io, tmp))):
            try:
                fn(*args)
                print(f"  ok  {fn.__name__}")
            except AssertionError as e:
                failures.append(fn.__name__)
                print(f"  FAIL {fn.__name__}: {e}")
            except Exception as e:
                failures.append(fn.__name__)
                print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nthe thumbnail decode keeps what the file carries: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
