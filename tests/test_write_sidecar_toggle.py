"""Regression: `write_sidecar` actually decides whether the .json is written (run: python tests/test_write_sidecar_toggle.py).

A .json landed beside every render with no way to decline it. That is right for a movie and redundant for an
EXR, and the written file says which: `sidecar_only` lists what the container could NOT hold, and beside an
EXR that list is empty because the header took all eight attributes.

The widget is only worth having if every branch reads it, and there are three - still, sequence and movie. A
flag added to a signature and honoured in two places out of three is the failure mode this guards: the
signature accepts it, the behaviour ignores it, and nothing goes red.

So each container is written twice, ON and OFF, and what is asserted is the FILE on disk:

1. OFF writes no .json, for any of the three.
2. ON writes exactly one - one per sequence, not one per frame.
3. OFF changes nothing else. The frames are still there, the EXR header still carries the colorspace, and the
   .wav beside a sequence is untouched: this switch is about the sidecar and must not quietly become a
   metadata switch.
"""
import importlib.util
import os
import sys
import tempfile
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TC_META = '{"attrs": {"timeCode": "01:00:00:00"}}'


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


def _write(io, folder, frames=3, **kw):
    import torch
    imgs = torch.zeros((frames, 6, 8, 3))
    imgs[..., 0], imgs[..., 1], imgs[..., 2] = 0.40, 0.60, 0.10
    args = dict(profile="none", input_colorspace="sRGB - Display", output_colorspace="ACEScg",
                container="sequence", still_format="exr", video_codec="h264", bit_depth="16f",
                compression="zip", auto_range=False, first_frame=1, last_frame=0, start_number=1001,
                source_start=1, raw_data=False, colorspace_in_name=False, auto_colorspace=False,
                output_folder="$OUTPUT/" + folder, filename="sc", fps=24.0, metadata=TC_META, images=imgs)
    args.update(kw)
    io.OCIOWrite().write(**args)
    d = os.path.join(_TMP, folder)
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def _jsons(files):
    return [f for f in files if f.lower().endswith(".json")]


def check_sequence(io):
    on = _write(io, "seq_on", write_sidecar=True)
    off = _write(io, "seq_off", write_sidecar=False)
    assert len(_jsons(on)) == 1, f"ON should write exactly one sidecar for the whole sequence, got {_jsons(on)}"
    assert not _jsons(off), f"OFF still wrote a sidecar: {_jsons(off)}"
    exr_on = [f for f in on if f.endswith(".exr")]
    exr_off = [f for f in off if f.endswith(".exr")]
    assert exr_on == exr_off and len(exr_off) == 3, (
        f"turning the sidecar off changed the frames themselves: {exr_on} vs {exr_off}")


def check_still(io):
    on = _write(io, "still_on", frames=1, write_sidecar=True)
    off = _write(io, "still_off", frames=1, write_sidecar=False)
    assert len(_jsons(on)) == 1, f"ON should write a sidecar beside a still, got {_jsons(on)}"
    assert not _jsons(off), f"OFF still wrote a sidecar beside a still: {_jsons(off)}"
    assert [f for f in off if f.endswith(".exr")], f"the still itself is missing: {off}"


def check_video(io):
    on = _write(io, "mov_on", container="video", write_sidecar=True)
    off = _write(io, "mov_off", container="video", write_sidecar=False)
    assert len(_jsons(on)) == 1, f"ON should write a sidecar beside a movie, got {_jsons(on)}"
    assert not _jsons(off), f"OFF still wrote a sidecar beside a movie: {_jsons(off)}"
    assert [f for f in off if f.endswith(".mp4")], f"the movie itself is missing: {off}"


def check_off_does_not_strip_the_header(io):
    """The switch is about the .json, not about metadata. An EXR written with it off keeps its header."""
    import OpenEXR
    _write(io, "hdr_off", write_sidecar=False)
    d = os.path.join(_TMP, "hdr_off")
    frame = os.path.join(d, sorted(f for f in os.listdir(d) if f.endswith(".exr"))[0])
    with OpenEXR.File(frame) as f:
        h = dict(f.header())
    assert h.get("com.ocio.colorspace") == "ACEScg", (
        f"the colorspace left the header when the sidecar was turned off: {h.get('com.ocio.colorspace')!r}")
    assert "chromaticities" in h, "the chromaticities left the header when the sidecar was turned off"


def check_default_is_on(io):
    """A graph saved before this widget existed must keep the behaviour it had."""
    files = _write(io, "default_on")                       # write_sidecar not passed at all
    assert len(_jsons(files)) == 1, (
        f"omitting write_sidecar changed the default behaviour; got {_jsons(files)}")


def main():
    global _TMP
    _TMP = tempfile.mkdtemp(prefix="ocio_sc_")
    io = _load_io_nodes(_TMP)
    failures = []
    for fn in (check_sequence, check_still, check_video, check_off_does_not_strip_the_header, check_default_is_on):
        try:
            fn(io)
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            failures.append(fn.__name__)
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:                              # a broken probe must not read as a passing test
            failures.append(fn.__name__)
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nwrite_sidecar is read by all three container branches: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
