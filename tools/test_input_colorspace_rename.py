"""Regression: OCIO Write's input is called `input_colorspace`, like everywhere else (run: python tools/test_input_colorspace_rename.py).

Three nodes asked the same question in two vocabularies. OCIO Read and OCIO Player took `input_colorspace`;
OCIO Write took `from_colorspace`. Renamed on 2026-08-16, and the rename is the kind that can go wrong
quietly, so what makes it safe is pinned down here rather than remembered.

**Position, not name, is what keeps saved GUI workflows working.** A `.json` from the canvas stores
`widgets_values` as a positional array with no field names in it at all, so a widget that stays in the same
slot keeps its value across a rename. Move it - to `optional`, or behind another new input - and every widget
after it shifts by one, which is how a colorspace silently becomes a container and a finished graph starts
rendering something else. So the slot is asserted, not just the name.

**An API-format workflow saved before the rename does not survive it, and no version of this could make it.**
There the keys ARE the names, and ComfyUI refuses a prompt missing a required key before the node runs
(HTTP 400 `required_input_missing` - measured against a live server, twice: once to establish it, once to
confirm that a `VALIDATE_INPUTS` naming both spellings does not lift it). The only way to make the old key
liftable is to make the new one optional, which breaks the GUI workflows that currently survive. That trade
was refused. The fix for an old API workflow is one word in the JSON, and the refusal names the key.

`write()` itself still answers to the old name, because the callers that reach it directly - this repo's own
tools/ scripts, docker/, anything driving the node from Python - are not prompts and never went through that
validation. That alias is exercised below; without a test it is a mechanism with no reader.
"""
import importlib.util
import inspect
import os
import sys
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JS = os.path.join(_ROOT, "web", "ocio_io.js")

NEW = "input_colorspace"
OLD = "from_colorspace"


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


def check_all_three_nodes_agree(io):
    """The whole point of the rename: one word for one idea across the pack."""
    for cls in (io.OCIORead, io.OCIOWrite, io.OCIOPlayer):
        it = cls.INPUT_TYPES()
        names = list(it.get("required", {})) + list(it.get("optional", {}))
        assert NEW in names, f"{cls.__name__} does not have `{NEW}`; it offers {[n for n in names if 'colorspace' in n]}"
        assert OLD not in names, f"{cls.__name__} still offers the old `{OLD}` as an input"


def check_it_kept_its_slot(io):
    """A GUI workflow restores by POSITION. Second in `required` is where the value in every saved graph is."""
    req = list(io.OCIOWrite.INPUT_TYPES()["required"])
    assert req.index(NEW) == 1, (
        f"`{NEW}` is at slot {req.index(NEW)} of `required`, was slot 1. Every widget after it in a saved "
        f"workflow now reads the value of its neighbour.")


def check_the_default_did_not_move(io):
    it = io.OCIOWrite.INPUT_TYPES()["required"]
    spec = it[NEW]
    default = spec[1].get("default") if len(spec) > 1 else None
    assert default == io.WORKING, f"the default became {default!r}, was {io.WORKING!r}; saved graphs would move"


def check_write_still_answers_to_the_old_name(io):
    """For direct Python callers, which never went through prompt validation."""
    sig = inspect.signature(io.OCIOWrite.write).parameters
    assert NEW in sig, f"write() lost `{NEW}`"
    assert OLD in sig and sig[OLD].default is None, (
        f"write() no longer accepts `{OLD}`; the repo's own tools/ and docker/ scripts call it by that name")


def check_the_old_name_wins_only_over_a_default(io):
    """The alias must not overwrite a real choice - that is the failure this arm exists to prevent."""
    import torch
    img = torch.zeros((1, 2, 2, 3), dtype=torch.float32)
    kw = dict(profile="none", output_colorspace="ACEScg", container="sequence", still_format="exr",
              video_codec="prores_4444", bit_depth="16f", auto_range=False, first_frame=1, last_frame=1,
              start_number=1, source_start=1, raw_data=False, output_folder=".", filename="rename_probe",
              colorspace_in_name=False, auto_colorspace=False, images=img)
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        kw["output_folder"] = d
        # the old name alone: it is the only choice on offer, so it must be used
        ui = io.OCIOWrite().write(input_colorspace=io.WORKING, from_colorspace="Linear Rec.709 (sRGB)", **kw)["ui"]
        assert "Linear Rec.709 (sRGB) ->" in ui["ocio"][0], f"the alias was ignored: {ui['ocio']}"
        # both names, disagreeing: the NEW one is the deliberate pick and must win
        ui = io.OCIOWrite().write(input_colorspace="ACEScct", from_colorspace="Linear Rec.709 (sRGB)", **kw)["ui"]
        assert ui["ocio"][0].startswith("ACEScct ->"), (
            f"the old name overrode an explicit `{NEW}`: {ui['ocio']}. A saved choice was replaced.")


def check_the_front_end_reads_the_new_name(io):
    """Six lookups in web/ocio_io.js read this widget by name. One missed rename leaves a feature dead -
    the auto-view fill, the narrowing, the profile sync - with nothing raising anywhere."""
    src = open(_JS, encoding="utf-8").read()
    assert f'"{OLD}"' not in src, f"web/ocio_io.js still looks up the widget as `{OLD}`"
    assert src.count(f'"{NEW}"') >= 6, (
        f"only {src.count(f'\"{NEW}\"')} lookups of `{NEW}` in the front end; there were six of the old name")


CHECKS = (check_all_three_nodes_agree, check_it_kept_its_slot, check_the_default_did_not_move,
          check_write_still_answers_to_the_old_name, check_the_old_name_wins_only_over_a_default,
          check_the_front_end_reads_the_new_name)


def main():
    io = _load()
    failures = []
    for fn in CHECKS:
        try:
            fn(io)
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            failures.append(fn.__name__)
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            failures.append(fn.__name__)
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nthe three nodes name their input the same way: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
