"""Regression: OCIO Write returns NO preview, for any container (run: python tools/test_write_preview_single.py).

This node writes files and reports in text. It does not show pictures, and that is a decision rather than an
omission: the pack already has a viewer, and OCIO Player is a better one - a float viewport with in / out
points, reverse, an exposure strip, audio metering and a GPU frame cache. A second, smaller player living on
every Write node turned a graph of four writes into a column of four video players.

It got there the long way. The node has carried, at different times, a still PNG, an animated H.264 copy of
the clip, a flipbook of the real written frames, its own transport and a collapsible Viewer. Each was added
for a good local reason and the sum was wrong, which is exactly the kind of thing that creeps back one
harmless-looking key at a time. So the rule under test is about what is ABSENT:

1. No preview key of any kind, whatever the container - not `images`, `animated`, `mov`, `still`, or the
   `seq_*` set that described a frame range to flip through.
2. The text report survives, because that is not a preview. `count`, `ocio`, `saved` and `meta` carry the
   frame count, the colour transform, the file written and the metadata verdict - including anything DROPPED
   or CLIPPED, which is a delivery fact an artist has to be told.

A regression here is quiet by nature: a preview reappearing looks like a feature, not a fault.
"""
import importlib.util
import os
import sys
import tempfile
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every key the node has ever used to hand a picture to the front end. Named individually rather than as
# "anything unexpected", so the test says WHICH old preview came back.
PREVIEW_KEYS = ("images", "animated", "mov", "still", "seq_src", "seq_first", "seq_last", "seq_fps")


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


def _write(io, frames, **kw):
    import torch
    imgs = torch.zeros((frames, 6, 8, 3))
    imgs[..., 0], imgs[..., 1], imgs[..., 2] = 0.40, 0.60, 0.10
    args = dict(profile="none", input_colorspace="sRGB - Display", output_colorspace="ACEScg",
                container="sequence", still_format="exr", video_codec="prores_4444", bit_depth="16f",
                compression="zip", auto_range=False, first_frame=1, last_frame=0, start_number=1001,
                source_start=1, raw_data=False, colorspace_in_name=False, auto_colorspace=False,
                filename="prev", fps=23.976, images=imgs)
    args.update(kw)
    res = io.OCIOWrite().write(**args)
    return (res or {}).get("ui", {}) or {}


def _assert_no_preview(ui, label):
    back = [k for k in PREVIEW_KEYS if k in ui]
    assert not back, (
        f"the {label} branch is handing back a preview again ({', '.join(back)}); ui keys were {sorted(ui)}. "
        "OCIO Write reports in text; OCIO Player is the viewer.")


def check_sequence(io):
    _assert_no_preview(_write(io, 3, output_folder="$OUTPUT/seq3"), "sequence")


def check_video(io):
    _assert_no_preview(_write(io, 3, container="video", video_codec="h264",
                              output_folder="$OUTPUT/mov"), "movie")


def check_single_frame(io):
    _assert_no_preview(_write(io, 1, output_folder="$OUTPUT/one"), "still")


def check_the_text_report_survives(io):
    """Removing the picture must not remove the delivery report, which is the node's actual output."""
    ui = _write(io, 3, output_folder="$OUTPUT/report")
    for k in ("count", "ocio", "saved"):
        assert k in ui, f"the text report lost '{k}'; ui keys were {sorted(ui)}"
    assert ui["count"] == ["3"], f"the frame count is wrong: {ui.get('count')}"
    assert "ACEScg" in ui["ocio"][0], f"the colour transform is not reported: {ui.get('ocio')}"


def main():
    tmp = tempfile.mkdtemp(prefix="ocio_prev_")
    io = _load_io_nodes(tmp)
    failures = []
    for fn in (check_sequence, check_video, check_single_frame, check_the_text_report_survives):
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
    print("\nOCIO Write returns no preview, and still reports what it wrote: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
