"""Regression: the metadata OCIO Write authors onto its own output (run: python tools/test_write_metadata.py).

Until 2026-08-12 a file leaving this pack carried the nine mandatory EXR attributes and nothing else, so every
import into Nuke / Resolve / Premiere needed the colorspace set by hand. Five things are locked down here, each
one a way the wiring can regress into writing something confidently wrong:

1. CHROMATICITIES ARE DERIVED FROM THE LIVE OCIO CONFIG AND ANCHORED TO PUBLISHED VALUES. The derivation picks
   the interchange hub whose white matches the colorspace's own: an OCIO conversion is relative-colorimetric, so
   routing ACEScg through the D65 hub returns Bradford-ADAPTED primaries (green at 0.1595/0.8388 instead of
   AP1's 0.165/0.830) and a white of D65 instead of the ACES white. Those numbers describe a gamut nobody
   encoded. A colorspace no hub reproduces gets NO chromaticities at all rather than a plausible guess.

2. THE TIMECODE ADVANCES PER FRAME. Write emits N files from one settings dict; stamping the start code into
   every header gives a sequence where every frame claims the same instant, which Resolve and Premiere accept
   without a word and conform wrong. Drop-frame counting exists ONLY at 29.97 / 59.94 (SMPTE ST 12-1:2014) -
   23.976 counts as 24 non-drop, and 23.976 is this pack's own default rate, so the case is not hypothetical.

3. STRUCTURED ATTRIBUTES KEEP THEIR TYPES. chromaticities must be a flat 8-float tuple and timeCode an
   OpenEXR.TimeCode; str() on either writes the right text under the wrong type and a standards-aware reader
   then ignores it.

4. PIXEL-STATE CLAIMS DO NOT SURVIVE A COLOUR TRANSFORM. A C2PA manifest, ST 2086 / ST 2094 HDR mastering
   metadata, an ACES AMF and an MHL all describe one specific pixel state. Carried across a conversion they
   become checkable lies, which is worse than silence - so they are dropped, and the drop is REPORTED.

5. NEW WIDGETS AND OUTPUTS ARE APPENDED, NEVER INSERTED. widgets_values is positional and an output link is
   stored by slot index, so a widget or slot added above an existing one silently re-points every saved
   workflow below it.
"""
import importlib.util
import json
import os
import re
import sys
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The ONLY route a start timecode reaches a written file since OCIO Write's `start_timecode` field was removed
# (2026-08-13): it arrives on the `metadata` wire, the way a real plate delivers it. Tests below that assert on
# a timecode hand this in, so what they prove is INHERITANCE, not a value typed into the node.
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


def _exr_header(p):
    import OpenEXR
    with OpenEXR.File(p) as f:
        return dict(f.header())


# Published chromaticities, repeated here INDEPENDENTLY of the module's own table on purpose: if a future edit
# changes _GAMUT_ANCHORS, this test must fail rather than agree with the change. Flat 8: Rx Ry Gx Gy Bx By Wx Wy.
WANT = {
    "ACEScg":                     (0.713,  0.293,  0.165,  0.830,  0.128,  0.044,   0.32168, 0.33767),   # ACES AP1
    "ACES2065-1":                 (0.7347, 0.2653, 0.0,    1.0,    0.0001, -0.0770, 0.32168, 0.33767),   # SMPTE ST 2065-1 AP0
    "sRGB - Display":             (0.640,  0.330,  0.300,  0.600,  0.150,  0.060,   0.3127,  0.3290),    # ITU-R BT.709-6
    "Rec.1886 Rec.709 - Display": (0.640,  0.330,  0.300,  0.600,  0.150,  0.060,   0.3127,  0.3290),
    "Linear Rec.2020":            (0.708,  0.292,  0.170,  0.797,  0.131,  0.046,   0.3127,  0.3290),    # ITU-R BT.2020-2
    "Rec.2100-HLG - Display":     (0.708,  0.292,  0.170,  0.797,  0.131,  0.046,   0.3127,  0.3290),
    "Linear P3-D65":              (0.680,  0.320,  0.265,  0.690,  0.150,  0.060,   0.3127,  0.3290),    # SMPTE RP 431-2 at D65
}
# Registered colour-interop IDs, read off the config's own alias list (the ACES 2.0 studio config carries the
# Color Interop Forum names for exactly this). Confirmed downstream: oiiotool maps colorInteropID to
# oiio:ColorSpace. NOT expected for ACEScct - the config offers only "ocio:acescct_ap1_scene", its own
# non-registered spelling, and passing that off as registered is the guess this attribute exists to avoid.
WANT_IID = {"ACEScg": "lin_ap1_scene", "ACES2065-1": "lin_ap0_scene",
            "sRGB - Display": "srgb_rec709_display", "Rec.2100-HLG - Display": "rec2100_hlg_display",
            "Linear Rec.2020": "lin_rec2020_scene"}


def check_derivation(io):
    for cs, want in WANT.items():
        got, gamut = io._derive_chromaticities(cs)
        assert got is not None, (
            f"{cs}: no chromaticities derived. The OCIO derivation must reproduce the published set for this "
            "colorspace; if it cannot, the attribute is omitted and the receiving app is back to a hand-set "
            "colorspace on every import.")
        worst = max(abs(a - b) for a, b in zip(got, want))
        assert worst <= 5e-4, (
            f"{cs}: chromaticities off by {worst:.5f} from the published {gamut} set.\n"
            f"  got  {tuple(round(v, 5) for v in got)}\n  want {want}\n"
            "An error this size is the signature of the WRONG interchange hub: an OCIO conversion is "
            "relative-colorimetric, so crossing a white-point boundary Bradford-adapts every value. Writing an "
            "adapted set into chromaticities describes a gamut nobody encoded.")
    for cs, iid in WANT_IID.items():
        got = io._interop_id(cs)
        assert got == iid, f"{cs}: colorInteropID {got!r}, expected {iid!r} (read from the config's aliases)"
    # ...and the gate that keeps it honest: a colorspace whose gamut no hub reproduces gets NOTHING.
    for cs in ("Linear ARRI Wide Gamut 4", "ARRI LogC4", "Raw", "NoSuchColorspaceAtAll"):
        got, _ = io._derive_chromaticities(cs)
        assert got is None, (
            f"{cs}: chromaticities were written ({got}) for a colorspace with no published anchor that this "
            "config reproduces. Measured: AWG4 comes back ~1.5e-3 off the ARRI numbers through either hub. "
            "Omitting is the requirement - a header that guesses is worse than one that stays quiet.")
        assert io._interop_id(cs) is None, f"{cs}: invented a colorInteropID"
    print("[PASS] chromaticities derived from OCIO + anchored to published sets; unanchored gamuts omitted; "
          "interop IDs read from the config's aliases")


def check_timecode(io):
    S, adv = io._timecode_string, io._tc_advance
    def tc(start, off, fps):
        return S(*adv(io._parse_timecode(start), off, fps))

    # drop-frame exists ONLY at 29.97 / 59.94 (SMPTE ST 12-1:2014)
    for r in (23.976, 24, 25, 30, 48, 50, 60):
        assert not io._is_drop_frame(r), f"{r} must NOT be drop-frame (ST 12-1 lists 29.97 and 59.94 only)"
    for r in (29.97, 59.94):
        assert io._is_drop_frame(r), f"{r} must be drop-frame"
    assert io._tc_nominal_rate(23.976) == 24, "23.976 counts to 24 (timecode stores no rate of its own)"
    assert io._tc_nominal_rate(29.97) == 30 and io._tc_nominal_rate(59.94) == 60

    non_drop = [("01:00:00:00", 0, 23.976, "01:00:00:00"), ("01:00:00:00", 1, 23.976, "01:00:00:01"),
                ("01:00:00:00", 23, 23.976, "01:00:00:23"), ("01:00:00:00", 24, 23.976, "01:00:01:00"),
                ("01:00:00:00", 24 * 60, 23.976, "01:01:00:00"), ("01:00:00:00", 25, 25, "01:00:01:00"),
                ("00:00:00:00", 24 * 60 * 60 * 24 + 5, 24, "00:00:00:05")]        # wraps at 24h, does not overflow
    for start, off, fps, want in non_drop:
        got = tc(start, off, fps)
        assert got == want, f"non-drop {start} +{off} @{fps}: got {got}, want {want}"

    # Drop-frame skips frame LABELS 00 and 01 of every minute except every tenth. Anchors that pin the whole
    # counting scheme: exactly one hour of 29.97 DF is 107892 frames (30*3600 - 2*54), of 59.94 DF 215784.
    # THE START IS WRITTEN WITH ';' - it is what makes these drop-frame counts. Since 2026-08-13 the
    # separator decides, not the frame rate: at 29.97 both counts are legal, and deriving it from the rate
    # was renumbering real drop-frame plates and discarding legal non-drop ones. A ':' start at 29.97 is
    # non-drop and is asserted separately below.
    drop = [("00:00:00;00", 1799, 29.97, "00:00:59;29"), ("00:00:00;00", 1800, 29.97, "00:01:00;02"),
            ("00:00:00;00", 17981, 29.97, "00:09:59;29"), ("00:00:00;00", 17982, 29.97, "00:10:00;00"),
            ("00:00:00;00", 107892, 29.97, "01:00:00;00"),
            ("00:00:00;00", 3599, 59.94, "00:00:59;59"), ("00:00:00;00", 3600, 59.94, "00:01:00;04"),
            ("00:00:00;00", 215784, 59.94, "01:00:00;00")]
    for start, off, fps, want in drop:
        got = tc(start, off, fps)
        assert got == want, f"drop-frame {start} +{off} @{fps}: got {got}, want {want}"
    # A NON-DROP start at the same rate counts straight through: this is the plate that used to lose its
    # timecode altogether, because the rate-derived guess called it drop-frame and then rejected it.
    for start, off, want in (("01:01:00:00", 0, "01:01:00:00"), ("00:00:00:00", 1800, "00:01:00:00"),
                             ("00:00:59:29", 1, "00:01:00:00")):
        got = tc(start, off, 29.97)
        assert got == want, f"non-drop at 29.97 {start} +{off}: got {got}, want {want}"

    seen = [tc("00:00:00;00", i, 29.97) for i in range(30 * 70)]
    assert "00:01:00;00" not in seen and "00:01:00;01" not in seen, "drop-frame emitted a dropped label"
    assert "00:01:00;02" in seen, "drop-frame skipped past the first legal label of minute 1"
    assert len(set(seen)) == len(seen), "timecode repeated inside one run - frames would not be distinguishable"

    for bad in ("1:2", "01-00-00-00", "abc", "99:00:00:00", "01:60:00:00", "01:00:99:00"):
        try:
            io._parse_timecode(bad)
        except ValueError:
            continue
        raise AssertionError(f"_parse_timecode accepted {bad!r}; a silently-wrong start code conforms the whole "
                             "delivery to the wrong place, so it must raise instead of defaulting to zero")
    assert io._parse_timecode("") is None and io._parse_timecode(None) is None, "empty = write no timecode"
    print("[PASS] timecode advances per frame; drop-frame only at 29.97/59.94 and counts per ST 12-1; "
          "malformed codes raise")


def check_sequence_write(io, tmp):
    """The real thing: OCIOWrite.write() on a 4-frame batch, headers read back off disk."""
    import torch
    imgs = torch.zeros((4, 6, 8, 3))
    imgs[..., 0], imgs[..., 1], imgs[..., 2] = 0.40, 0.60, 0.10
    w = io.OCIOWrite()
    res = w.write(profile="none", from_colorspace="sRGB - Display", output_colorspace="ACEScg",
                  container="sequence", still_format="exr", video_codec="prores_4444", bit_depth="16f",
                  auto_range=False, first_frame=1, last_frame=0, start_number=1001, source_start=1,
                  raw_data=False, output_folder="$OUTPUT/seq", filename="meta", fps=23.976,
                  metadata=TC_META, images=imgs)
    folder = os.path.join(tmp, "seq")
    files = sorted(f for f in os.listdir(folder) if f.endswith(".exr"))
    assert len(files) == 4, f"expected 4 frames, got {files}"
    codes, counters = [], []
    for f in files:
        h = _exr_header(os.path.join(folder, f))
        ch = h.get("chromaticities")
        assert isinstance(ch, tuple) and len(ch) == 8, (
            f"{f}: chromaticities came back {type(ch).__name__} {ch!r}. It must be written as a flat 8-float "
            "tuple; a str()-ed one has the right text and the wrong type, and a standards-aware reader skips it.")
        worst = max(abs(a - b) for a, b in zip(ch, WANT["ACEScg"]))
        assert worst <= 1e-3, f"{f}: chromaticities {tuple(round(v, 5) for v in ch)} != AP1 (off {worst:.5f})"
        assert h.get("colorInteropID") == "lin_ap1_scene", f"{f}: colorInteropID {h.get('colorInteropID')!r}"
        assert abs(float(h.get("framesPerSecond", 0)) - 23.976) < 1e-3, f"{f}: framesPerSecond {h.get('framesPerSecond')}"
        assert abs(float(h.get("captureRate", 0)) - 23.976) < 1e-3, f"{f}: captureRate {h.get('captureRate')}"
        tc = h.get("timeCode")
        assert type(tc).__name__ == "TimeCode", (
            f"{f}: timeCode came back {type(tc).__name__}, not an OpenEXR.TimeCode - a string-typed timecode is "
            "not the standard type and readers ignore it")
        assert tc.dropFrame is False, f"{f}: dropFrame set at 23.976, which counts as 24 NON-drop per ST 12-1"
        codes.append((tc.hours, tc.minutes, tc.seconds, tc.frame))
        counters.append(int(h.get("imageCounter", -1)))
        for k in io._EXR_STRUCTURAL:
            if k in ("channels", "compression", "dataWindow", "displayWindow", "lineOrder", "pixelAspectRatio",
                     "screenWindowCenter", "screenWindowWidth", "type"):
                continue                                    # the nine mandatory ones are written by the library
            assert k not in h or k in ("version",), f"{f}: structural attribute {k} was authored"
    assert codes == [(1, 0, 0, 0), (1, 0, 0, 1), (1, 0, 0, 2), (1, 0, 0, 3)], (
        f"TIMECODE DID NOT ADVANCE: {codes}. Every frame carrying the same code is the bug this exists to "
        "prevent - Resolve and Premiere accept it silently and conform the sequence wrong.")
    assert counters == [1001, 1002, 1003, 1004], f"imageCounter did not follow the frame number: {counters}"
    ui = (res.get("ui") or {}).get("meta") or [""]
    assert "lin_ap1_scene" in ui[0] and "01:00:00:00" in ui[0], f"ui.meta does not report what was written: {ui}"

    # raw_data writes NO colorimetry: unconverted pixels are of unknown gamut and must not claim one.
    w.write(profile="none", from_colorspace="sRGB - Display", output_colorspace="ACEScg", container="sequence",
            still_format="exr", video_codec="prores_4444", bit_depth="16f", auto_range=False, first_frame=1,
            last_frame=0, start_number=1, source_start=1, raw_data=True, output_folder="$OUTPUT/raw",
            filename="r", fps=24.0, metadata=TC_META, images=imgs)
    h = _exr_header(os.path.join(tmp, "raw", sorted(os.listdir(os.path.join(tmp, "raw")))[0]))
    assert "chromaticities" not in h and "colorInteropID" not in h, (
        "raw_data claimed a gamut. 'Raw' means the pixels were not converted, so we do not know what they are.")
    assert h.get("timeCode") is not None, "raw_data dropped the timecode, which is true either way"
    print("[PASS] real sequence write: chromaticities/colorInteropID/rate authored, timecode and imageCounter "
          "advance per frame, raw_data claims no gamut")


def check_reports_only_what_it_wrote(io, tmp):
    """PNG / TIFF / JPEG have no header for chromaticities or a timecode. The node must not list them anyway.

    It did, until 2026-08-12: the on-node text was built from the authored dict regardless of format, so writing a
    PNG reported "chromaticities Rec.709; srgb_rec709_display; tc 01:00:00:00" and none of it was in the file. An
    artist reads that as a delivery fact. It is the same class of error as writing a false attribute, aimed at the
    person instead of the file."""
    import torch
    w = io.OCIOWrite()
    for fmt, bd in (("png", "8"), ("tiff", "16"), ("jpeg", "8")):
        res = w.write(profile="none", from_colorspace="sRGB - Display", output_colorspace="sRGB - Display",
                      container="still image", still_format=fmt, video_codec="prores_4444", bit_depth=bd,
                      auto_range=False, first_frame=1, last_frame=0, start_number=1, source_start=1,
                      raw_data=False, output_folder="$OUTPUT/nohdr", filename="n_" + fmt, fps=24.0,
                      metadata=TC_META, images=torch.zeros((1, 4, 4, 3)))
        assert os.path.isfile(res["result"][0]), f"{fmt}: nothing written"
        note = ((res.get("ui") or {}).get("meta") or [""])[0]
        for claim in ("chromaticities", "colorInteropID", "lin_", "srgb_rec709_display", "tc 01:00:00:00"):
            assert claim not in note, (
                f"{fmt}: the node reported {claim!r} but a {fmt} has nowhere to put it. Reported text: {note!r}")
        assert fmt in note and "no colour metadata header" in note, (
            f"{fmt}: the node should say plainly that this format carries no header. Got: {note!r}")
    # EXR is the format that DOES carry it, and it must still say so.
    res = w.write(profile="none", from_colorspace="sRGB - Display", output_colorspace="ACEScg",
                  container="still image", still_format="exr", video_codec="prores_4444", bit_depth="16f",
                  auto_range=False, first_frame=1, last_frame=0, start_number=1, source_start=1, raw_data=False,
                  output_folder="$OUTPUT/hdr", filename="y", fps=24.0, metadata=TC_META,
                  images=torch.zeros((1, 4, 4, 3)))
    note = ((res.get("ui") or {}).get("meta") or [""])[0]
    assert "chromaticities" in note and "lin_ap1_scene" in note and "tc 01:00:00:00" in note, (
        f"EXR must report the metadata it really wrote. Got: {note!r}")
    print("[PASS] the node reports metadata only for formats that can carry it")


def check_passthrough(io, tmp):
    import torch
    src = json.dumps({"source": "plate.0001.exr", "kind": "exr", "attrs": {
        "reel_name": "A001R2XY", "cameraMake": "ARRI", "lensModel": "Signature 47mm",
        # The plate's OWN colorimetry and rate. All three must LOSE to ours: they describe the file that came in.
        "chromaticities": (0.64, 0.33, 0.3, 0.6, 0.15, 0.06, 0.3127, 0.329),
        "colorInteropID": "srgb_rec709_display", "framesPerSecond": 25.0,
        "dataWindow": "(0 0 639 351)",                                            # a 640x352 window on a 1280x704 render
        "masteringDisplayPrimaries": "G(0.265,0.690)B(0.150,0.060)", "c2pa.manifest": "<blob>",
        "amf": "shot_v01.amf.xml", "asc_mhl_hash": "deadbeef", "hdr10plus": "{...}",
        "Content Credentials": "signed", "st2094_40": "{}"}})
    w = io.OCIOWrite()
    res = w.write(profile="none", from_colorspace="sRGB - Display", output_colorspace="ACEScg",
                  container="sequence", still_format="exr", video_codec="prores_4444", bit_depth="16f",
                  auto_range=False, first_frame=1, last_frame=0, start_number=1, source_start=1, raw_data=False,
                  output_folder="$OUTPUT/pt", filename="pt", fps=24.0,
                  metadata=src, images=torch.zeros((1, 4, 4, 3)))
    h = _exr_header(res["result"][0])
    assert h.get("reel_name") == "A001R2XY" and h.get("lensModel") == "Signature 47mm", (
        f"editorial / lens attributes did not travel: {sorted(h)}")
    for bad in ("masteringDisplayPrimaries", "c2pa.manifest", "amf", "asc_mhl_hash", "hdr10plus",
                "Content Credentials", "st2094_40"):
        assert bad not in h, (
            f"{bad} was copied across a colour transform. It describes ONE specific pixel state, so after a "
            "conversion it is a checkable claim that is false - worse than writing nothing.")
    assert h.get("dataWindow") != "(0 0 639 351)", "a plate dataWindow was stamped onto a different-sized render"
    ch = h.get("chromaticities")
    assert isinstance(ch, tuple), f"chromaticities came back {type(ch).__name__}, not a tuple"
    assert max(abs(a - b) for a, b in zip(ch, WANT["ACEScg"])) <= 1e-3, (
        f"the PLATE's chromaticities won over ours ({tuple(round(v, 5) for v in ch)}). Ours describe the file "
        "being written; the plate's describe the file that came in.")
    assert h.get("colorInteropID") == "lin_ap1_scene", (
        f"colorInteropID is {h.get('colorInteropID')!r} - the plate's value overwrote ours")
    assert abs(float(h.get("framesPerSecond", 0)) - 24.0) < 1e-3, (
        f"framesPerSecond is {h.get('framesPerSecond')} - a plate value overwrote the rate we are writing at")
    assert h.get("com.ocio.sourceFile") == "plate.0001.exr", "provenance of the plate was not recorded"
    note = ((res.get("ui") or {}).get("meta") or [""])[0]
    assert "dropped" in note and "c2pa.manifest" in note, (
        f"the drop was silent. ui.meta must NAME what did not travel: {note!r}")
    # malformed JSON on the wire must not take the render down
    res2 = w.write(profile="none", from_colorspace="sRGB - Display", output_colorspace="ACEScg",
                   container="sequence", still_format="exr", video_codec="prores_4444", bit_depth="16f",
                   auto_range=False, first_frame=1, last_frame=0, start_number=1, source_start=1, raw_data=False,
                   output_folder="$OUTPUT/pt2", filename="pt2", fps=24.0,
                   metadata="{not json at all", images=torch.zeros((1, 4, 4, 3)))
    assert os.path.isfile(res2["result"][0]), "malformed source_meta stopped the render"

    # AND NEITHER MAY A HOSTILE ONE. Found by mutation: OpenEXR type-checks its own standard attribute names and
    # RAISES on a value of the wrong shape, so one bad incoming attribute could kill a finished render. The
    # colorspace here has no published anchor on purpose, so we author no chromaticities of our own and the
    # plate's values are the only candidates - which is the path that used to raise.
    hostile = json.dumps({"source": "bad.exr", "kind": "exr", "attrs": {
        "chromaticities": "not even a number", "adoptedNeutral": {"nested": "dict"},
        "timeCode": "01:00:00:00", "whiteLuminance": [1, 2, 3], "reel_name": "C003"}})
    res3 = w.write(profile="none", from_colorspace="sRGB - Display", output_colorspace="ARRI LogC4",
                   container="sequence", still_format="exr", video_codec="prores_4444", bit_depth="16f",
                   auto_range=False, first_frame=1, last_frame=0, start_number=1, source_start=1, raw_data=False,
                   output_folder="$OUTPUT/pt3", filename="pt3", fps=24.0,
                   metadata=hostile, images=torch.zeros((1, 4, 4, 3)))
    assert os.path.isfile(res3["result"][0]), (
        "an incoming attribute of the wrong shape stopped the render. Metadata must never be the reason a render "
        "dies - a rejected attribute is skipped, not raised.")
    h3 = _exr_header(res3["result"][0])
    assert h3.get("reel_name") == "C003", "the good attributes were lost along with the bad one"
    for k in ("chromaticities", "whiteLuminance", "adoptedNeutral"):
        assert k not in h3, (
            f"{k} came from the plate and was written anyway. These describe the INCOMING file's colour; we "
            "re-author them, and when we cannot they must still not be inherited.")
    # timeCode is the ONE of the four that is deliberately taken FROM the plate (2026-08-13, when OCIO Write's
    # own field was removed) - but taken as a START and re-authored per frame, never copied across. The
    # difference is visible in the TYPE: our re-authored value is an OpenEXR.TimeCode object, whereas the
    # plate's raw attribute here is the string "01:00:00:00". A string in this header would mean the value was
    # inherited wholesale, which is the bug this line still guards.
    tc3 = h3.get("timeCode")
    assert tc3 is not None, (
        "the plate carried a start timecode and none was written. Since the node's own field was removed, the "
        "`metadata` wire is the only route a code has - dropping it here loses it for good.")
    assert not isinstance(tc3, str), (
        f"timeCode came back as {type(tc3).__name__} {tc3!r} - the plate's raw value copied through rather than "
        "re-authored. A standards-aware reader ignores a string-typed timecode, and a copied one does not "
        "advance per frame.")
    print("[PASS] pass-through: shot metadata travels, pixel-state claims (C2PA / ST 2086 / ST 2094 / AMF / MHL) "
          "are dropped and named, our own colorimetry wins, bad JSON does not stop a render")


def check_bad_attribute_survivable(io, tmp):
    """One unwritable attribute must not cost a finished render.

    Checked at _save_exr_with_meta rather than through the node, because the node's drop list already removes the
    eight attributes it re-authors - so the realistic case is a standard name it does NOT cover. keyCode is that
    case and it is not exotic: a film-scanned plate carries one, the EXR reader hands it back as a list or a repr
    string, and OpenEXR refuses both for that name. Before this path existed, such a plate would have taken a
    render down at the write step, after all the compute was already spent."""
    rgb = np.full((4, 4, 3), 0.5, np.float32)
    hostile = {"keyCode": "1, 2, 3, 4, 5, 4, 64",           # a real keyCode, in the shape a reader returns
               "chromaticities": "not even a number",
               "framesPerSecond": {"nested": "dict"},
               "reel_name": "A001R2XY", "cameraMake": "ARRI", "imageCounter": 42}
    p = os.path.join(tmp, "hostile.exr")
    try:
        io._save_exr_with_meta(p, rgb, "16f", None, "zip", hostile)
    except Exception as e:
        raise AssertionError(
            f"an unwritable attribute stopped the render: {type(e).__name__}: {str(e)[:200]}\n"
            "OpenEXR type-checks its own standard attribute names and raises AT WRITE TIME, so the shape cannot "
            "be vetted by building a header - it needs a real probe write. And both dicts handed to OpenEXR.File "
            "are EMPTIED on __exit__, so the retry must be given copies or it writes a file with no pixels.") from e
    assert os.path.isfile(p), "an unwritable attribute stopped the render (no file on disk)"
    h = _exr_header(p)
    assert h.get("reel_name") == "A001R2XY" and h.get("cameraMake") == "ARRI", (
        f"the good attributes were dropped along with the bad ones: {sorted(h)}")
    assert int(h.get("imageCounter", -1)) == 42, "a valid numeric attribute was lost in the retry"
    assert not isinstance(h.get("chromaticities"), str), "a string got written under the chromaticities name"
    px = None
    import OpenEXR
    with OpenEXR.File(p) as f:
        px = np.array(f.channels()["RGB"].pixels, copy=True)
    assert px.shape == (4, 4, 3) and abs(float(px[0, 0, 0]) - 0.5) < 1e-3, (
        f"the pixels did not survive the retry: shape {px.shape}, first {px[0, 0, 0] if px.size else None}")
    print("[PASS] an unwritable attribute is skipped, the good ones and the PIXELS still ship")


def check_read_output(io, tmp):
    """OCIO Read's 6th output carries the metadata as JSON, and its own EXR is readable back through it."""
    import torch
    io._save_still(os.path.join(tmp, "plate.0001.exr"), np.full((4, 4, 3), 0.5, np.float32), "exr", "16f",
                   None, "ACEScg", "zip", {"reel_name": "B002", "cameraMake": "Sony"})
    got = io.read_source_meta(os.path.join(tmp, "plate.0001.exr"))
    assert got.get("kind") == "exr", f"kind {got.get('kind')!r}"
    assert got["attrs"].get("reel_name") == "B002", f"attrs did not round-trip: {got['attrs']}"
    assert json.loads(json.dumps(got, default=str)), "read_source_meta output is not JSON-serialisable"
    for missing in ("", "no_such_file_anywhere.exr"):
        io.read_source_meta(missing)          # must not raise: a missing tag cannot be why a render fails
    print("[PASS] read_source_meta round-trips real attributes, is JSON-serialisable, never raises")


def check_append_only(io):
    """Widget order and slot indices are POSITIONAL. This is the invariant that protects saved workflows."""
    ins = io.OCIOWrite.INPUT_TYPES()
    req, opt = list(ins["required"]), list(ins["optional"])
    # The FULL order, not just the tail: a widget inserted in the MIDDLE leaves the last element untouched and
    # still shifts every value below it. Checking only req[-1] missed exactly that (found by mutation, 2026-08-12).
    # Changing this list is allowed - but it has to be a deliberate edit here, with the knowledge that every
    # already-saved workflow reads its widget values by position.
    WANT_REQUIRED = [
        "profile", "from_colorspace", "output_colorspace", "container", "still_format", "video_codec",
        "bit_depth", "compression", "auto_range", "first_frame", "last_frame", "start_number", "source_start",
        "raw_data", "colorspace_in_name", "output_folder", "filename", "auto_colorspace",
    ]
    assert req == WANT_REQUIRED, (
        "OCIO Write's REQUIRED widget order changed.\n"
        f"  got  {req}\n  want {WANT_REQUIRED}\n"
        "widgets_values is positional and follows this order, so an inserted or reordered widget makes every "
        "saved workflow read the wrong value into every widget below it (fps would take render_nonce's string). "
        "New widgets go at the END of 'optional'.")
    assert req[-1] == "auto_colorspace", f"a required widget was appended after auto_colorspace ({req[-1]})"
    # `start_timecode` sat between 'audio' and 'metadata' until 2026-08-13, when it was removed: the start now
    # arrives with the plate through the `metadata` wire instead of being typed here. Removing a widget is
    # otherwise forbidden for the positional reason spelled out above, and was allowed only because that one
    # never reached a release - no published graph holds a value at its index.
    assert opt[-3:] == ["audio", "metadata", "write_audio"], (
        f"the last three optional inputs are {opt[-3:]}, expected "
        "['audio', 'metadata', 'write_audio']. Each was appended so it could only add a trailing "
        "widgets_values slot / input slot; moving any of them up re-points saved links and values.")
    assert ins["optional"]["metadata"][1].get("forceInput") is True, (
        "source_meta must be forceInput: as a plain STRING it would render a widget and occupy a "
        "widgets_values slot, which is the thing being avoided.")
    # The tail above is the DICT order; what actually indexes widgets_values is the WIDGET order, which skips
    # the sockets. Asserting the dict tail alone would pass with a widget inserted ahead of write_audio as long
    # as it stayed inside the last three, so the widget sequence is asserted directly.
    _SOCKETS = {"images", "video", "audio", "alpha", "mask"}
    opt_widgets = [k for k in opt
                   if k not in _SOCKETS and not (isinstance(ins["optional"][k][1], dict)
                                                 and ins["optional"][k][1].get("forceInput"))]
    assert opt_widgets[-2:] == ["render_nonce", "write_audio"], (
        f"the last two optional WIDGETS are {opt_widgets[-2:]}, expected ['render_nonce', 'write_audio']. "
        "widgets_values is positional over widgets only, so this - not the dict tail - is what a saved workflow "
        "reads by index.")
    assert "start_timecode" not in opt_widgets, (
        "start_timecode is back. It was removed on 2026-08-13: a code typed into the writer is a code invented "
        "at delivery, and the start now arrives with the plate through the `metadata` wire. Re-adding it as a "
        "widget anywhere but the very end would also shift every saved workflow's values by one.")
    assert ins["optional"]["write_audio"][0] == "BOOLEAN" and \
        ins["optional"]["write_audio"][1].get("default") is True, (
        "write_audio must default to True: every graph saved before it existed has no value for it and falls "
        "through to this default, so False here would silently strip the sound from all of them.")
    assert io.OCIORead.RETURN_TYPES[:5] == ("IMAGE", "MASK", "FLOAT", "STRING", "VIDEO"), (
        f"OCIO Read's first five outputs changed: {io.OCIORead.RETURN_TYPES}. An output link is stored by SLOT "
        "INDEX, so inserting above index 5 re-points every saved connection below it.")
    assert io.OCIORead.RETURN_TYPES[5] == "STRING" and io.OCIORead.RETURN_NAMES[5] == "metadata"
    assert len(io.OCIORead.RETURN_TYPES) == len(io.OCIORead.RETURN_NAMES) == 6
    print("[PASS] new widget and new output slots are APPENDED, not inserted (saved workflows keep their indices)")


def check_frontend_slot_safety(io):
    """The front end must not mutate the OUTPUT array of OCIO Read, and this is not a style point.

    A link is serialised as an output INDEX and the backend resolves that index through RETURN_TYPES, so the two
    arrays have to agree position for position. web/ocio_io.js used to REMOVE the VIDEO output for a non-video
    source, which was safe only while VIDEO was the last slot. 'metadata' now sits behind it at index 5, so
    a removal would slide it into index 4 on the client while the server still answers index 4 with a VIDEO object:
    a wire that looks connected and delivers the wrong type, with nothing on screen to say so. The slot now stays
    and its label carries the hint instead."""
    js = open(os.path.join(_ROOT, "web", "ocio_io.js"), encoding="utf-8").read()
    assert "removeOutput" not in js, (
        "web/ocio_io.js calls removeOutput again. Removing any output above the last one renumbers every slot "
        "below it on the client only - the backend still maps by its own RETURN_TYPES order.")
    # An append is only safe when it lands on the backend's index for that type.
    for m in re.finditer(r"addOutput\(", js):
        window = js[max(0, m.start() - 260):m.start()]
        assert "outputs.length === 4" in window, (
            "an addOutput in web/ocio_io.js is not guarded to land at the backend's index for that slot. "
            "addOutput always appends, so on any other array length it places the slot at the wrong index.")
    assert "ComfyUI Video (video sources only)" in js, (
        "the VIDEO slot's not-applicable state is no longer conveyed by its label; if the slot is being hidden "
        "some other way, check that it does not renumber 'metadata' at index 5")
    print("[PASS] the front end never renumbers OCIO Read's outputs (no removeOutput; appends are index-guarded)")

    # A HIDDEN WIDGET HAS TO COME BACK. Both visibility helpers stash the widget's own computeSize before zeroing
    # it, and MOST of these widgets have none - litegraph lays them out from the prototype - so the stash holds
    # `undefined`. The restore used to read `if (w._ocioCompute)`, which is falsy for exactly that case, so the
    # zeroed function was never removed: hidden once, invisible forever, with `hidden` and `options.hidden` both
    # reading false. Measured in the live canvas before the fix - switch container to video and back to sequence
    # and `compression` returned with every flag saying "shown" and computeSize()[1] === 0, the node's height
    # dropping 666 -> 490 across one round trip. An artist could not set EXR compression again without reloading.
    #
    # Structural, because the gate runs no browser: the restore must be keyed on the PROPERTY EXISTING, never on
    # its value being truthy. Both helpers are checked - the same idiom lives in OCIO Read's setVisibleWidgets,
    # and fixing only the one that was caught is how this class of defect survives.
    # CODE ONLY. The first version of this assertion matched the COMMENT four lines above the fix, which quotes the
    # broken pattern verbatim to explain it - a test reading prose as code, and it failed on a correct file. Lines
    # whose first non-space characters open a comment are dropped before any of these patterns are applied.
    js_code = "\n".join(ln for ln in js.splitlines() if not ln.lstrip().startswith(("//", "*", "/*")))
    assert not re.search(r"if\s*\(\s*w\._ocioCompute\s*\)", js_code), (
        "a visibility helper restores computeSize behind `if (w._ocioCompute)`. That is FALSY when the widget had "
        "no computeSize of its own - the common case - so the zeroed layout is never undone and the row stays "
        "invisible while every flag says it is showing. Key the restore on `\"_ocioCompute\" in w`.")
    assert re.search(r'"_ocioCompute"\s+in\s+w', js_code), (
        "no property-existence test for the computeSize stash. Without it the restore cannot tell 'this widget "
        "had no computeSize' from 'nothing was stashed'.")
    assert re.search(r"delete\s+w\.computeSize", js_code), (
        "the no-own-computeSize case must be undone with `delete w.computeSize`, which re-exposes the prototype's "
        "layout. Assigning undefined leaves an own property shadowing it.")
    # and the zeroing side must use the same key, or hiding twice overwrites the real stash with the zeroed one
    assert not re.search(r"if\s*\(\s*!\s*w\._ocioCompute\s*\)\s*w\._ocioCompute", js_code), (
        "the stash is still written behind a truthiness test. Hiding an already-hidden widget would then replace "
        "the stashed original with the zeroed function, making the loss permanent even after the restore is fixed.")
    n_restore = len(re.findall(r"_ocioRestoreCompute\(", js_code))
    assert n_restore >= 3, (
        f"expected the shared restore helper to be defined once and called by BOTH visibility helpers, found "
        f"{n_restore} occurrences. The same idiom exists in showWidget and setVisibleWidgets.")
    print("[PASS] a hidden widget's layout is restored by property, not by truthiness (both visibility helpers)")


def check_output_folder(io, tmp):
    r = io.resolve_output_folder
    n = os.path.normcase
    assert n(r("")) == n(tmp), f'"" must resolve to the ComfyUI output dir, got {r("")}'
    assert n(r("$OUTPUT")) == n(tmp), f"$OUTPUT alone -> the output dir, got {r('$OUTPUT')}"
    for spec in ("$OUTPUT/shot_010", "$OUTPUT\\shot_010", "$output/shot_010", "$OUTPUT//shot_010"):
        assert n(r(spec)) == n(os.path.join(tmp, "shot_010")), f"{spec} -> {r(spec)}"
    assert n(r("$OUTPUT/a/b")) == n(os.path.join(tmp, "a", "b")), "a nested token path did not resolve"
    assert n(r("shot_010")) == n(os.path.join(tmp, "shot_010")), "a plain relative path changed behaviour"
    # An absolute path stays absolute: pointing a Write at a NAS is deliberate, not a mistake to be corrected.
    for absolute in (os.path.join("D:" + os.sep, "shots", "out"), r"\\nas\vfx\out"):
        assert r(absolute) == absolute, f"absolute path {absolute} was rewritten to {r(absolute)}"
    # And the default the node ships with must not be an absolute path in the first place - it is stored in
    # widgets_values, and core SaveVideo / SaveImage embed the whole workflow JSON inside the files they write.
    default = io.OCIOWrite.INPUT_TYPES()["required"]["output_folder"][1]["default"]
    assert not os.path.isabs(default) and "$OUTPUT" not in default.upper()[1:] or default == "", (
        f"output_folder default {default!r} carries a machine path")
    assert default == "", f"output_folder default is {default!r}, expected empty"
    print("[PASS] output_folder: $OUTPUT token resolves under the output dir, relative unchanged, absolute "
          "(NAS) untouched, default carries no path")


def check_plate_dropframe_flag(io):
    """The plate's own drop-frame flag survives the read, in both spellings it arrives under.

    Found by a mutation pass: dropping the flag left every other test green, and the cost is a two-frame
    conform error - a 00:00:59;29 drop-frame start renumbered to 00:01:00;02 where the non-drop truth is
    00:01:00;00. The flag is in the data both ways: as SMPTE's ';' separator in a string, and as the fifth
    field of OpenEXR's TimeCode tuple, which is what an EXR plate hands over.
    """
    # 1) the string spelling
    assert io._parse_timecode("00:00:59;29")[4] is True, "the ';' drop-frame separator was thrown away"
    assert io._parse_timecode("00:00:59:29")[4] is False, "a ':' start was reported as drop-frame"

    # 2) the OpenEXR tuple spelling, which is how it actually arrives from a plate
    df = io._timecode_from_source({"timeCode": "(1, 1, 0, 2, 1, 0, 0, 0, 0, 0)"})
    ndf = io._timecode_from_source({"timeCode": "(1, 1, 0, 2, 0, 0, 0, 0, 0, 0)"})
    assert df is not None and df[4] is True, f"the plate's dropFrame flag was lost: {df}"
    assert ndf is not None and ndf[4] is False, f"a non-drop plate was reported as drop-frame: {ndf}"

    # 3) and it reaches the arithmetic, which is where the two-frame error came from
    got_df = io._timecode_string(*io._tc_advance(df, 0, 29.97))
    got_ndf = io._timecode_string(*io._tc_advance(ndf, 0, 29.97))
    assert ";" in got_df, f"a drop-frame plate was written non-drop: {got_df}"
    assert ";" not in got_ndf, f"a non-drop plate was written drop-frame: {got_ndf}"
    print("[PASS] the plate's own drop-frame flag survives, in both the ';' and the EXR-tuple spelling")


def check_umid_is_not_a_reel(io):
    """A UMID parked in the reel field is not reported as the shot's reel - and is not destroyed either.

    Measured on a real ProRes 4444 XQ master: DaVinci Resolve writes
    `com.apple.proapps.reel=0x060A2B34...`, a 32-octet SMPTE ST 330M identifier opening with the SMPTE
    Universal Label prefix. A reel name is 8 characters in a CMX3600 EDL and up to 32 on Avid, so a UMID
    cannot BE one; reporting it as the reel puts a 64-character hash where an assistant editor expects
    A001R2XY. The far worse failure would be the opposite - a real reel name refused as a hash - so the
    honest names are asserted first and in numbers.
    """
    for name in ("A001R2XY", "B_0059C005", "A001", "CAM_A_001", "A001_20230224", "deadbeef", "R1", "0910"):
        assert not io._looks_like_umid(name), (
            f"{name!r} was taken for a UMID. A reel name refused as a hash is worse than a hash shown as a "
            "reel: the shot loses its identity in every delivered file.")
    for umid in ("0x060A2B340101010501010D4313000000F4D4E5CAB3B011EDBFDA8F313F3F0F07",
                 "060A2B340101010501010D4313000000F4D4E5CAB3B011EDBFDA8F313F3F0F07"):
        assert io._looks_like_umid(umid), f"a SMPTE UMID was not recognised: {umid[:24]}..."
    # ...and the identity reduction refuses it while a real name goes through.
    hashed = {"com.apple.proapps.reel": "0x060A2B340101010501010D4313000000F4D4E5CAB3B011EDBFDA8F313F3F0F07",
              "timeCode": "19:35:48:13"}
    ident = io._identity_meta(hashed)
    assert "reel" not in ident, f"the UMID was reported as a reel: {ident.get('reel')!r}"
    assert ident.get("timecode"), "the rest of the identity was lost along with it"
    named = io._identity_meta({"reel_name": "A001R2XY", "shot": "0106"})
    assert named.get("reel") == "A001R2XY" and named.get("shot") == "0106", f"a real reel was dropped: {named}"
    print("[PASS] a UMID is not reported as a reel name; real reel names are untouched")


def main():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ocio_meta_test_")
    io = _load_io_nodes(tmp)
    try:
        import OpenEXR  # noqa: F401
    except ImportError:
        print("[SKIP] OpenEXR is not installed; header authoring cannot be checked. See requirements.txt.")
        return
    # Cheap structural invariants FIRST: when the folder resolver or the widget order is broken, every later
    # end-to-end check dies on a raw FileNotFoundError that says nothing about the cause.
    check_append_only(io)
    check_frontend_slot_safety(io)
    check_output_folder(io, tmp)
    check_derivation(io)
    check_timecode(io)
    check_plate_dropframe_flag(io)
    check_umid_is_not_a_reel(io)
    check_sequence_write(io, tmp)
    check_reports_only_what_it_wrote(io, tmp)
    check_passthrough(io, tmp)
    check_bad_attribute_survivable(io, tmp)
    check_read_output(io, tmp)
    print("\nALL METADATA CHECKS PASSED")


if __name__ == "__main__":
    main()
