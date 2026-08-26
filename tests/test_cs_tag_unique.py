# -*- coding: utf-8 -*-
"""No two colorspaces may produce the same filename tag, and the front-end must agree with the backend.

Run:  python tests/test_cs_tag_unique.py     (no pytest, no ComfyUI server, no GPU)

WHY. `_write_output_paths` builds the DELIVERED PATH from `_cs_tag(output_colorspace)`. Under the previous
short-tag table, 31 of the config's 55 colorspaces shared a tag with at least one other - thirteen gamuts
became 'linear', eight transfers became 'rec709', six became 'p3' - so two writes differing only in colorspace
produced the same path and the second silently overwrote the first. Nothing in the gate noticed, because no
test compared two colorspaces against each other.

This file is that comparison. It also pins the exact strings for the colorspaces people use daily, so a future
"tidy-up" of the tag function cannot quietly rename deliverables, and it asserts that no truncation has been
reintroduced - a fixed-width cut is precisely how prefix-sharing names would start colliding again.
"""
import importlib.util
import os
import re
import sys
import tempfile
import types
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def _load():
    tmp = tempfile.mkdtemp(prefix="ocio_tag_")
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


io = _load()
tag = io._cs_tag
SPACES = list(io.OCIOWrite.INPUT_TYPES()["required"]["output_colorspace"][0])
print(f"{len(SPACES)} colorspaces offered by the live config")

print("\nevery colorspace gets its OWN tag - this is the whole point of the file")
by = defaultdict(list)
for s in SPACES:
    by[tag(s)].append(s)
dupes = {t: v for t, v in by.items() if len(v) > 1}
check("no two colorspaces share a filename tag", not dupes,
      "" if not dupes else "; ".join(f"{t!r} <- {v}" for t, v in list(dupes.items())[:3]))
check("every tag is non-empty", all(by), f"{sum(1 for t in by if not t)} empty")

print("\nthe tags people see every day are pinned, so a tidy-up cannot rename deliverables silently")
PINNED = {
    "ACEScg": "acescg",
    "ACEScct": "acescct",
    "ACEScc": "acescc",
    "sRGB - Display": "srgb_display",
    "Rec.1886 Rec.709 - Display": "rec_1886_rec_709_display",
    "Rec.2100-HLG - Display": "rec_2100_hlg_display",
    "Rec.2100-PQ - Display": "rec_2100_pq_display",
    "Linear ARRI Wide Gamut 4": "linear_arri_wide_gamut_4",
    "ST2084-P3-D65 - Display": "st2084_p3_d65_display",
    "Raw": "raw",
}
for name, want in PINNED.items():
    if name not in SPACES:
        print(f"  ....  {name!r} is not in this config, skipped")
        continue
    got = tag(name)
    check(f"{name!r} -> {want!r}", got == want, f"got {got!r}")

print("\nno truncation: a fixed-width cut would put prefix-sharing names back into collision")
longest = max(SPACES, key=lambda s: len(tag(s)))
check("the longest tag is not cut at a round number", len(tag(longest)) not in (16, 20, 24, 32),
      f"{tag(longest)!r} is {len(tag(longest))} chars ({longest})")
for s in SPACES:
    exp = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", s.lower())).strip("_")
    if tag(s) != exp:
        check(f"{s!r} is spelled out in full", False, f"got {tag(s)!r}, full form {exp!r}")
        break
else:
    check("every tag is the full spelled-out name", True, f"checked all {len(SPACES)}")

print("\nthe front-end computes the SAME tag as the backend (it previews the filename to the artist)")
js = open(os.path.join(_ROOT, "web", "ocio_io.js"), encoding="utf-8").read()
check("the old short-tag table is gone from the front-end", "CS_TAG_RULES" not in js)
m = re.search(r"function csCore\(name\)\s*\{(.*?)\n\}", js, re.S)
check("csCore is present in the front-end", m is not None)
if m:
    body = m.group(1)
    # The detail is only meaningful on failure - printing "found a .slice() call" next to a PASS reads as a
    # contradiction, which is how a report stops being trusted.
    check("csCore no longer truncates", ".slice(" not in body,
          "" if ".slice(" not in body else "a .slice() call is still there")
    check("csCore lowercases, substitutes and collapses",
          "toLowerCase" in body and "[^a-z0-9]+" in body and "_+" in body, body.strip()[:90])

print("\ntwo writes that differ ONLY in colorspace now land on DIFFERENT paths")
folder = os.path.join(tempfile.mkdtemp(prefix="ocio_paths_"), "out")
pairs = [("Linear ARRI Wide Gamut 4", "Linear REDWideGamutRGB"),
         ("Rec.1886 Rec.709 - Display", "Gamma 2.2 Rec.709 - Display"),
         ("ST2084-P3-D65 - Display", "Display P3 - Display")]
for a, b in pairs:
    if a not in SPACES or b not in SPACES:
        print(f"  ....  {a!r} / {b!r} not both in this config, skipped")
        continue
    pa = io._write_output_paths(folder, "shot", "sequence", "exr", "prores_4444", a, False, True, 1001, 1)
    pb = io._write_output_paths(folder, "shot", "sequence", "exr", "prores_4444", b, False, True, 1001, 1)
    check(f"{a} vs {b}", pa != pb,
          f"{os.path.basename(pa[0])} vs {os.path.basename(pb[0])}")

print("\nraw_data still wins over the colorspace tag, as before")
pr = io._write_output_paths(folder, "shot", "sequence", "exr", "prores_4444", "ACEScg", True, True, 1001, 1)
check("raw_data writes _raw", os.path.basename(pr[0]).startswith("shot_raw."), os.path.basename(pr[0]))
pn = io._write_output_paths(folder, "shot", "sequence", "exr", "prores_4444", "ACEScg", False, False, 1001, 1)
check("colorspace_in_name off writes no tag", os.path.basename(pn[0]).startswith("shot."),
      os.path.basename(pn[0]))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
