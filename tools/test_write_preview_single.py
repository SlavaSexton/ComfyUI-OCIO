"""Regression: OCIO Write shows exactly ONE preview per write (run: python tools/test_write_preview_single.py).

A written sequence got TWO previews on the node (2026-08-15): the flipbook of the real frames, and above/below
it an H.264 proxy of the same frames. They do not agree - the proxy is 8-bit and carries no colour conversion,
so it reads darker than the frames it claims to preview - and nothing on the node says which one is the master.
An artist reading colour off the wrong one is the whole failure this pack exists to prevent.

Neither preview was wrong on its own; shipping both was. So the rule under test is a COUNT, not a value:

1. A SEQUENCE ships the flipbook (seq_src + range + fps) and NO proxy. thumb_frame reads the written frames
   with _read_still, the same reader OCIO Read uses, so every still format this branch writes is one the
   flipbook can serve back - the proxy has nothing left to add.

2. A VIDEO still ships the proxy, and no flipbook. A movie is one file, not a numbered range; /ocio/thumb can
   only hand back its FIRST frame, so a flipbook there would be a still pretending to be a clip.

3. A SINGLE-FRAME sequence ships neither. One frame is a still: the node's own thumb already shows it, and an
   H.264 clip of one frame is a video that cannot move.

The counts are read off the real return of OCIOWrite.write(), not from the source text - a preview key that is
merely PRESENT in the file proves nothing about which branch sets it.
"""
import importlib.util
import os
import sys
import tempfile
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

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


def _write(io, frames, **kw):
    import torch
    imgs = torch.zeros((frames, 6, 8, 3))
    imgs[..., 0], imgs[..., 1], imgs[..., 2] = 0.40, 0.60, 0.10
    args = dict(profile="none", from_colorspace="sRGB - Display", output_colorspace="ACEScg",
                container="sequence", still_format="exr", video_codec="prores_4444", bit_depth="16f",
                auto_range=False, first_frame=1, last_frame=0, start_number=1001, source_start=1,
                raw_data=False, filename="prev", fps=23.976, images=imgs)
    args.update(kw)
    res = io.OCIOWrite().write(**args)
    return (res or {}).get("ui", {}) or {}


def check_sequence_is_flipbook_only(io):
    """A multi-frame sequence: the real frames, and nothing standing beside them."""
    ui = _write(io, 3, output_folder="$OUTPUT/seq3")
    assert "seq_src" in ui, f"a 3-frame sequence lost its flipbook; ui keys were {sorted(ui)}"
    assert "mov" not in ui, (
        f"TWO previews on one node: the flipbook AND the H.264 proxy; ui keys were {sorted(ui)}")
    # The range has to describe the files that exist, or the flipbook asks /ocio/thumb for frames it will 404 on.
    first, last = int(ui["seq_first"][0]), int(ui["seq_last"][0])
    assert (first, last) == (1001, 1003), f"flipbook range {first}..{last} does not match the 3 frames written"
    folder = ui["seq_src"][0]
    on_disk = sorted(f for f in os.listdir(folder) if f.endswith(".exr"))
    assert len(on_disk) == 3, f"flipbook points at {folder}, which holds {on_disk}"
    assert float(ui["seq_fps"][0]) > 0, "a flipbook with no rate plays at whatever the browser feels like"


def check_video_is_proxy_only(io):
    """A movie: the proxy, and no flipbook - /ocio/thumb cannot scrub a single container file."""
    ui = _write(io, 3, container="video", video_codec="h264", output_folder="$OUTPUT/mov")
    assert "mov" in ui, f"a movie lost its playable preview; ui keys were {sorted(ui)}"
    assert "seq_src" not in ui and "still" not in ui, (
        f"a movie was handed a second preview it cannot serve; ui keys were {sorted(ui)}")


def check_single_frame_has_no_clip(io):
    """One frame is a still: its own PNG, and nothing that pretends to move."""
    ui = _write(io, 1, output_folder="$OUTPUT/one")
    assert "seq_src" not in ui, f"a single frame was described as a flipbook; ui keys were {sorted(ui)}"
    assert "mov" not in ui, f"a single frame was given a moving preview; ui keys were {sorted(ui)}"
    assert "still" in ui, f"a single frame lost its preview; ui keys were {sorted(ui)}"


def check_nothing_is_handed_to_the_front_end_to_draw(io):
    """No container returns `images`, and that is the point.

    `images` is rendered by the front end, in markup this pack does not own - a Vue-managed element on the new
    frontend. Nothing in an extension can collapse it, so as long as any container used it, OCIO Write could
    not offer the Viewer toggle OCIO Read has, and a movie had transport controls while a sequence had none.
    A regression here is silent: the preview still appears, so it looks fine, and only the toggle stops working.
    """
    for label, kw in (("sequence", dict(frames=3, output_folder="$OUTPUT/nf_seq")),
                      ("movie", dict(frames=3, container="video", video_codec="h264",
                                     output_folder="$OUTPUT/nf_mov")),
                      ("still", dict(frames=1, output_folder="$OUTPUT/nf_one"))):
        ui = _write(io, **kw)
        assert "images" not in ui and "animated" not in ui, (
            f"the {label} branch handed its preview back as `images` for the front end to draw; "
            f"ui keys were {sorted(ui)}")


def main():
    tmp = tempfile.mkdtemp(prefix="ocio_prev_")
    io = _load_io_nodes(tmp)
    failures = []
    for fn in (check_sequence_is_flipbook_only, check_video_is_proxy_only, check_single_frame_has_no_clip,
               check_nothing_is_handed_to_the_front_end_to_draw):
        try:
            fn(io)
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  FAIL {fn.__name__}: {e}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nOCIO Write shows exactly one preview per container: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
