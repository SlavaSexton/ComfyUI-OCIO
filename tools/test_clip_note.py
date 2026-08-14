"""Does OCIO Write tell the artist when the container ate the range, and stay quiet when it did not?

An integer container cannot represent below-black or above-white AT ALL. Measured through this node with
raw_data on, so only the container was under test: EXR 16f and 32f round-trip -1.5 and +20.0 intact, while
TIFF 16/8, PNG 16/8 and JPEG floor every negative to 0.000000 and cap everything at 1.0. Before 2026-08-13 the
node said nothing about it - no ui text, no log line - which made the one irreversible loss in the pack the only
silent one, because it had already been written to disk.

A warning that fires on correct work is worse than none, so the silent cases are asserted as hard as the loud
ones, including the two that decide whether the check is precise or merely blind.
"""
import importlib.util
import io as _io
import logging
import os
import pathlib
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp()
fp = types.ModuleType("folder_paths")
fp.get_output_directory = fp.get_temp_directory = fp.get_input_directory = lambda: TMP
fp.get_filename_list = lambda *a, **k: []
sys.modules.setdefault("folder_paths", fp)
pkg = types.ModuleType("p"); pkg.__path__ = [ROOT]; sys.modules["p"] = pkg
for mod in ("nodes", "io_nodes"):
    spec = importlib.util.spec_from_file_location(f"p.{mod}", os.path.join(ROOT, f"{mod}.py"))
    m = importlib.util.module_from_spec(spec); sys.modules[f"p.{mod}"] = m; spec.loader.exec_module(m)
io = sys.modules["p.io_nodes"]

import numpy as np
import torch

buf = _io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(buf))
logging.getLogger().setLevel(logging.WARNING)
FAILS = []

W, H = 256, 8      # 256 wide: DNxHD refuses anything under 256x120, so a smaller fixture fails for the wrong reason


def frames(n, lo, hi, where=-1):
    """n frames of mid grey with `lo` and `hi` planted in two columns of ONE frame, `where` by index.

    The default plants them on the LAST frame, which is what proves a sequence or a movie is measured across the
    whole batch instead of by reading frame 0. It is NOT usable for a still-image arm: a still writes exactly one
    frame (`arr[idx]`, resolved from first_frame), so a tail hidden on frame 2 was never written and silence is
    the correct answer. Getting this backwards made four correct cases look like failures while writing this.
    """
    a = np.full((n, H, W, 3), 0.5, dtype=np.float32)
    i = n - 1 if where < 0 else where
    a[i, :, 0, :] = lo
    a[i, :, 1, :] = hi
    return torch.from_numpy(a)


def run(label, images, want, **kw):
    """Write, then read BOTH the node's ui text and the log. An artist driving /prompt from a script never sees
    the canvas, so a warning that exists only in `ui` does not reach them at all."""
    buf.truncate(0); buf.seek(0)
    sub = os.path.join(TMP, str(abs(hash((label, str(sorted(kw.items()))))))); os.makedirs(sub, exist_ok=True)
    a = dict(profile="none", from_colorspace="ACEScg", output_colorspace="ACEScg",
             container="still image", still_format="exr", video_codec="prores_4444", bit_depth="16f",
             auto_range=False, first_frame=1, last_frame=0, start_number=1, source_start=1, raw_data=True,
             output_folder=sub, filename="clip", colorspace_in_name=False, fps=24.0,
             metadata="", images=images)
    a.update(kw)
    try:
        r = io.OCIOWrite().write(**a)
    except Exception as e:
        print(f"  FAIL  {label}: raised {type(e).__name__}: {e}")
        FAILS.append(label)
        return
    meta = ((r.get("ui") or {}).get("meta") or [""])[0]
    in_ui = "clipped" in meta
    in_log = "clipped" in buf.getvalue()
    ok = in_ui == want and in_log == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          + ("" if ok else f"   ui={'warned' if in_ui else 'silent'} log={'warned' if in_log else 'silent'}, wanted "
                           + ("a warning" if want else "silence")))
    if not ok:
        FAILS.append(label)


HDR = frames(3, -1.5, 20.0)                  # both tails on the LAST frame
HDR0 = frames(3, -1.5, 20.0, where=0)        # both tails on the frame a still actually writes
SDR = frames(3, 0.0, 1.0, where=0)           # the endpoints exactly: writing them loses nothing
# where=0 IS LOAD-BEARING here. With the tails on the last frame this arm exercises a still that writes frame 0,
# so it cannot catch anything - and it did not: mutating the threshold to zero left it green until this was fixed.
TINY = frames(3, -1.0 / 200000.0, 1.0 + 1.0 / 200000.0, where=0)     # inside half a 16-bit code

print("it MUST warn when an integer container ate the range")
run("still / tiff 16", HDR0, True, container="still image", still_format="tiff", bit_depth="16")
run("still / png 16", HDR0, True, container="still image", still_format="png", bit_depth="16")
run("still / png 8", HDR0, True, container="still image", still_format="png", bit_depth="8")
run("still / jpeg", HDR0, True, container="still image", still_format="jpeg", bit_depth="8")
run("sequence / tiff 16, tails on the last frame", HDR, True, container="sequence", still_format="tiff", bit_depth="16")
run("video / prores_4444", HDR, True, container="video", video_codec="prores_4444")

print("\nand it MUST stay silent where nothing was lost")
run("still / EXR 16f carries both tails", HDR0, False, container="still image", still_format="exr", bit_depth="16f")
run("still / EXR 32f carries both tails", HDR0, False, container="still image", still_format="exr", bit_depth="32f")
run("sequence / EXR 16f", HDR, False, container="sequence", still_format="exr", bit_depth="16f")
run("still / tiff 16, data already 0..1", SDR, False, container="still image", still_format="tiff", bit_depth="16")
# A FLOAT TIFF IS A FULL-RANGE CONTAINER, not an integer one. `_save_still`'s tiff branch writes
# `rgb.astype(np.float32)` with no clip when bit_depth is '32f', and reading the file back returns -1.5 and
# +20.0 with all six test negatives distinct. Warning here would be a false alarm about a file that kept
# everything - and the first version of this feature did exactly that, because it exempted EXR by name instead
# of asking which targets store floats.
run("still / TIFF 32f carries both tails", HDR0, False, container="still image", still_format="tiff", bit_depth="32f")
run("sequence / TIFF 32f", HDR, False, container="sequence", still_format="tiff", bit_depth="32f")
run("still / png 8, data already 0..1", SDR, False, container="still image", still_format="png", bit_depth="8")
run("still / tiff 16, overshoot under half a code", TINY, False, container="still image", still_format="tiff", bit_depth="16")

print("\nand the still branch must measure the frame it WRITES, not the batch")
# A tail on frame 2 of a three-frame batch never reaches a still, so warning would be a false alarm about a
# perfectly good file. Asking for that same frame by number must warn - which is what makes the silence above
# precision rather than blindness. Without this pair, a check that measured nothing at all would pass.
run("still / tiff 16, tails on a frame it does not write", HDR, False,
    container="still image", still_format="tiff", bit_depth="16")
run("still / tiff 16, asked for frame 3, where the tails are", HDR, True,
    container="still image", still_format="tiff", bit_depth="16", first_frame=3)

print("\nand the front end must raise it where no corner text is drawn")
# THE CONSUMER, not the table: on a Vue frontend onDrawForeground never runs, so the toast is the only channel.
# It fires on a regex, and a regex that lost "clipped" would silence this warning there completely while every
# check above stayed green - the exact shape of defect that has escaped this pack's tests before.
js = pathlib.Path(ROOT, "web", "ocio_io.js").read_text(encoding="utf-8")
for needle, why in ((r"/dropped|clipped/.test(this._ocioMeta)", "the toast regex matches a clip note"),
                    ('severity: "warn", summary: "OCIO Write metadata"', "and raises it as a warning, not info")):
    ok = needle in js
    print(f"  {'PASS' if ok else 'FAIL'}  {why}")
    if not ok:
        FAILS.append(why)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("the writer warns exactly when the container lost range, on both channels")
