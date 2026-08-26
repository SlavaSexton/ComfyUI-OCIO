# -*- coding: utf-8 -*-
"""The install advice inside error messages must name the same versions the pack actually requires.

Run:  python tests/test_install_advice_matches_deps.py     (no pytest, no ComfyUI, no GPU, no network)

WHY THIS FILE EXISTS. When an EXR read or write fails, the only thing the artist sees is the error message,
and those messages tell them what to install. That advice is a second copy of the dependency list, kept by
hand, and it drifted: `_read_still` told people to `pip install "OpenEXR>=3.2"` while `requirements.txt` had
moved to `>=3.3`. Anyone following the instruction to the letter would have installed a version without the
`OpenEXR.File` API this pack calls in four places, and landed back on the same failure with no idea why.

Nothing caught it. The gate never asserted on the text of an error message, so the copies were free to
disagree, and the one that mattered was the one nobody tested.

This checks agreement, not wording. Rewrite the sentences however you like; just do not name a version the
requirements do not back.
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


# The floor each dependency file declares. Comment lines are stripped first, so the prose around a
# requirement (which quotes versions freely, and should stay free to) cannot be mistaken for the requirement.
def declared_floor(text, pkg):
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    m = re.search(rf'{pkg}\s*>=\s*"?([0-9][0-9.]*)', body, re.I)
    return m.group(1) if m else None


req_floor = declared_floor(read("requirements.txt"), "OpenEXR")
proj_floor = declared_floor(read("pyproject.toml"), "OpenEXR")

print("the two dependency files agree with each other")
check("requirements.txt declares an OpenEXR floor", req_floor is not None, f"found {req_floor}")
check("pyproject.toml declares an OpenEXR floor", proj_floor is not None, f"found {proj_floor}")
check("the two floors are the same version", req_floor == proj_floor, f"{req_floor} vs {proj_floor}")

# Every `pip install "OpenEXR>=X"` the user can be shown, wherever it appears in shipped code.
print("\nevery install instruction the user can be shown names that same floor")
SHIPPED = ("io_nodes.py", "nodes.py", "vae_nodes.py", "grade_nodes.py", "__init__.py")
found_any = False
for rel in SHIPPED:
    src = read(rel)
    for m in re.finditer(r'pip install\s+\\?"?OpenEXR>=([0-9][0-9.]*)', src):
        found_any = True
        line = src[: m.start()].count("\n") + 1
        check(f"{rel}:{line} advises OpenEXR>={m.group(1)}", m.group(1) == req_floor,
              f"requirements say >={req_floor}")

# Without this the file would pass by finding nothing to look at, which is how a check quietly stops checking.
check("at least one install instruction exists to check", found_any,
      "" if found_any else "none found in shipped code - either the advice moved, or this file is now vacuous")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
