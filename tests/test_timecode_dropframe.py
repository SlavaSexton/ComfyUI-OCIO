# -*- coding: utf-8 -*-
"""Drop-frame timecode: the START the artist typed must be the START that lands in the file.

Run:  python tests/test_timecode_dropframe.py     (no pytest, no ComfyUI, no GPU)

WHY THIS FILE EXISTS. The pack's other timecode assertions all start from 00:00:00:00, where nominal and
drop-frame counts coincide, so they were structurally incapable of seeing the bug they were meant to guard:
`_tc_advance` converted the start LABEL to a frame count with the NOMINAL formula and decoded the count back
with the DROP-FRAME formula. The two directions disagreed by exactly the labels SMPTE ST 12-1 says to skip, so
at 29.97 the widget's own default 01:00:00:00 stamped 01:00:03;18 - 3.60 s adrift - and 10:00:00:00 stamped
10:00:36;00, 36 s adrift. It reached a real ProRes tmcd track. A whole gate stayed green through all of it.

So the checks here are the ones that can actually fail: a NON-ZERO start, and the round trip.

Anchors are the published ones (SMPTE ST 12-1:2014), not values read out of this implementation:
  00:00:59;29 -> 00:01:00;02      frames 00 and 01 are skipped entering a minute
  00:09:59;29 -> 00:10:00;00      except every tenth minute, where nothing is skipped
  one hour  = 107892 frames at 29.97, 215784 at 59.94
"""
import importlib.util
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def _load():
    tmp = tempfile.mkdtemp(prefix="ocio_tc_")
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
adv, parse, tcs = io._tc_advance, io._parse_timecode, io._timecode_string

DROP_RATES = (30000 / 1001, 60000 / 1001)
NON_DROP = (24.0, 25.0, 24000 / 1001, 30.0, 50.0, 60.0)

print("the typed START is preserved at offset 0 - the check the old gate could not make")
for fps in DROP_RATES + NON_DROP:
    for label in ("01:00:00:00", "10:00:00:00", "00:10:00:00", "02:30:00:00", "00:00:00:00"):
        h, mi, se, fr, drop = adv(parse(label), 0, fps)
        got = tcs(h, mi, se, fr, drop)
        want = label.replace(":", ";", -1) if False else label
        # the separator legitimately becomes ';' under drop-frame; compare the NUMBERS
        same = (h, mi, se, fr) == parse(label)[:4]
        check(f"{fps:8.3f} fps  start {label} preserved", same, f"got {got}")

print("\npublished SMPTE boundaries, at 29.97")
fps = 30000 / 1001
# WRITTEN WITH ';' ON PURPOSE. The separator is SMPTE's drop-frame marker, and since 2026-08-13 this
# pack honours it instead of deriving drop status from the rate: at 29.97 both counts are legal, and
# guessing renumbered real drop-frame plates while discarding legal non-drop ones. A ':' start at the
# same rate is non-drop and lands on 00:01:00:00, which is asserted a few lines below.
h, mi, se, fr, d = adv(parse("00:00:59;29"), 1, fps)
check("00:00:59;29 + 1 frame -> 00:01:00;02", (h, mi, se, fr) == (0, 1, 0, 2), tcs(h, mi, se, fr, d))
h, mi, se, fr, d2 = adv(parse("00:00:59:29"), 1, fps)
check("...and the same label written NON-drop lands on 00:01:00:00",
      (h, mi, se, fr, d2) == (0, 1, 0, 0, False), tcs(h, mi, se, fr, d2))
h, mi, se, fr, d = adv(parse("00:09:59;29"), 1, fps)
check("00:09:59;29 + 1 frame -> 00:10:00;00", (h, mi, se, fr) == (0, 10, 0, 0), tcs(h, mi, se, fr, d))
h, mi, se, fr, d = adv((0, 0, 0, 0, True), 107892, fps)
check("offset 107892 from zero -> 01:00:00;00 (one hour at 29.97)",
      (h, mi, se, fr) == (1, 0, 0, 0), tcs(h, mi, se, fr, d))
h, mi, se, fr, d = adv((0, 0, 0, 0, True), 215784, 60000 / 1001)
check("offset 215784 from zero -> 01:00:00;00 (one hour at 59.94)",
      (h, mi, se, fr) == (1, 0, 0, 0), tcs(h, mi, se, fr, d))

print("\nround trip: label -> +N frames -> label must equal advancing from zero by the same total")
fps = 30000 / 1001
for label, extra in (("01:00:00:00", 0), ("01:00:00:00", 1), ("01:00:00:00", 47),
                     ("00:10:00:00", 100), ("02:30:00:02", 1), ("10:00:00:00", 0)):
    a = adv(parse(label), extra, fps)[:4]
    # Feeding the RESULT back in at offset 0 must return the same label. That is the property the bug broke:
    # the forward direction and the inverse disagreed, so a label fed back in came out somewhere else.
    b = adv(parse(tcs(*a, True)), 0, fps)[:4]
    check(f"{label} +{extra:3} frames is a fixed point of the inverse", a == b, f"{a} vs {b}")

# WRITTEN WITH ';' THROUGHOUT, and that is the change of 2026-08-13. Frames 00 and 01 do not exist at a
# non-tenth minute in a DROP-FRAME count, so a label claiming one is illegal - but only as drop-frame. The
# identical digits with ':' are an ordinary non-drop label at the same rate, asserted right below. Until
# this pack read the separator, it derived drop status from the frame rate, so a legal 29.97 NON-drop plate
# had its timecode rejected and then silently dropped from every written header.
print("\nan illegal DROP-FRAME start is rejected, not silently moved")
for bad in ("00:01:00;00", "00:01:00;01", "01:11:00;00"):
    try:
        adv(parse(bad), 0, 30000 / 1001)
        check(f"{bad} rejected at 29.97", False, "accepted")
    except ValueError as e:
        check(f"{bad} rejected at 29.97", True, str(e)[:70] + "...")
for ok in ("00:10:00;00", "00:00:00;00", "01:20:00;01", "00:01:00;02"):
    try:
        adv(parse(ok), 0, 30000 / 1001)
        check(f"{ok} accepted at 29.97", True)
    except ValueError as e:
        check(f"{ok} accepted at 29.97", False, str(e)[:70])
# The same digits as NON-drop are legal at 29.97, including every one rejected above. This is the case that
# used to lose its timecode entirely.
for ndf in ("00:01:00:00", "00:01:00:01", "01:11:00:00"):
    try:
        h, mi, se, fr, d = adv(parse(ndf), 0, 30000 / 1001)
        check(f"{ndf} is a legal NON-drop label at 29.97",
              not d and tcs(h, mi, se, fr, d) == ndf, tcs(h, mi, se, fr, d))
    except ValueError as e:
        check(f"{ndf} is a legal NON-drop label at 29.97", False, str(e)[:70])

print("\nthe same labels are all legal at a NON-drop rate")
for lab in ("00:01:00:00", "00:01:00:01", "01:11:00:00"):
    try:
        h, mi, se, fr, d = adv(parse(lab), 0, 25.0)
        check(f"{lab} accepted at 25 fps and preserved", (h, mi, se, fr) == parse(lab)[:4] and not d,
              tcs(h, mi, se, fr, d))
    except ValueError as e:
        check(f"{lab} accepted at 25 fps and preserved", False, str(e)[:70])

print("\ndrop-frame is applied ONLY at 29.97 and 59.94")
for fps, want in ((30000 / 1001, True), (60000 / 1001, True), (24.0, False), (25.0, False),
                  (24000 / 1001, False), (30.0, False), (60.0, False)):
    d = adv((0, 0, 0, 0), 0, fps)[4]
    check(f"{fps:8.3f} fps drop_frame == {want}", bool(d) == want, f"got {bool(d)}")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
