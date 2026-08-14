# -*- coding: utf-8 -*-
"""Metadata has to reach the artist in EVERY format, and it must never carry the machine.

Run:  python tools/test_metadata_all_formats.py      (needs ffmpeg + ffprobe for the video arms; no ComfyUI, no GPU)

WHY THIS FILE EXISTS. The users' complaint was lost metadata, and the answer is a UNIVERSAL SIDECAR: a
<name>.json beside every written file, because no container holds everything. An EXR takes the whole attribute
set, a TIFF a handful of tags plus XMP, a PNG some text chunks, a MOV nearly everything, an MP4 a whitelist, an
MXF one structural field and a set of best-effort user comments, a JPEG nothing. Per-format embedding is an
improvement on top of that, never the rescue.

A 16-bit PNG was in that list as carrying "nothing at all", and it no longer is: neither cv2 nor Pillow can write
16-bit RGB *and* text, so the chunks are spliced in directly - ahead of the first IDAT, which is what makes them
visible to OpenImageIO and therefore to Nuke, Katana, Houdini and Blender.

Everything asserted here was MEASURED first and then frozen, because most of it could not have been guessed:

  - tifffile's default `metadata={}` appends its own shaped JSON to tag 270, so passing `description=` as well
    emitted ImageDescription TWICE in one IFD. Readers disagreed about which was the file's colorspace (oiiotool
    said 'ACEScg', tifffile's tag mapping said the JSON). A duplicate tag is malformed TIFF, so the count is
    asserted, not just the value.
  - OpenEXR types adoptedNeutral as a v2f and REJECTS a list for it, while accepting a 2-tuple. A list would have
    been dropped silently by the survive-a-bad-attribute path, so the type is part of the contract.
  - OpenEXR does NOT type-check whiteLuminance, so a plate value of [1,2,3] lands as a V3f for an attribute the
    specification defines as one float. That is why it is stripped and never inherited.
  - Of eleven identity tags handed to the MXF muxer as plain -metadata, ONE survives: reel_name, and that one is
    structural - ffmpeg writes it as the Physical Source Package Name (local tag 0x4402), the field Avid means by
    Tape Name. The other ten need the `comment_` prefix. That prefix was described here as routing them into
    "ST 377-1 user comments", which is WRONG and was corrected 2026-08-13 by reading ffmpeg's source rather than
    only ffprobe: it writes an AAF-compatible TaggedValue referenced from package local tag 0x4406, not an
    ST 377-1 Comment Marker and not a DM Framework. ffmpeg's own demuxer reads that private construction back, so
    a ffprobe round trip confirms nothing about interoperability, and neither Avid nor Blackmagic documents
    surfacing arbitrary TaggedValue. Asserted here as "they survive a round trip through ffmpeg", which is all
    that is true.
  - MXF OPAtom refuses a second stream outright ("there must be exactly one stream for mxf opatom"), so a wired
    audio track has to go beside the file rather than into it.

And the leak half, which is the reason a sidecar cannot just dump what it was handed: the sidecar is a DELIVERED
FILE. It ships in the same folder as the render. An absolute machine path or ComfyUI's embedded `prompt` graph in
there is the same leak the pack refuses in a MOV, so the guard is asserted on every format including the .json.
"""
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

FAILS = []
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _ascii(s):
    """Console-safe text. THE GATE RUNS WITHOUT PYTHONIOENCODING, so stdout is the host codepage (cp1251 here) and
    printing a plate value with an accent raised UnicodeEncodeError and took the whole file down - a test crashing
    on its own progress output rather than on the thing it tests. Found only by running the gate the way the gate
    runs it; every earlier run had PYTHONIOENCODING=utf-8 set and hid it."""
    return str(s).encode("ascii", "backslashreplace").decode("ascii")


def check(name, cond, detail=""):
    print(_ascii(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else "")))
    if not cond:
        FAILS.append(_ascii(name))


# EVERY path goes to a temp dir. A test that writes into the repository is found only by `git status`, and this
# pack has already had a stray database land in its root that way.
TMP = tempfile.mkdtemp(prefix="ocio_metaall_")


def _load():
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = fp.get_temp_directory = fp.get_input_directory = lambda: TMP
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
import numpy as np                                                          # noqa: E402
import torch                                                                # noqa: E402

# A plate carrying the shot's identity, two things that must never be delivered, and one pixel-state claim.
LEAK_PATH = "D:/secret/project/shots"
LEAK_GRAPH = '{"1": {"class_type": "KSampler"}}'
PLATE = {"source": "plate.0001.dpx", "kind": "dpx", "attrs": {
    "reel_name": "A001C086_171217_R0FR", "scene": "S11", "shot": "0106", "take": "3",
    "cameraMake": "ARRI", "cameraModel": "ALEXA 35", "lens": "Zeiss Master Prime 35mm",
    # THE START TIMECODE ARRIVES WITH THE PLATE. OCIO Write's own `start_timecode` field was removed on
    # 2026-08-13, so this attribute is now the only route by which a code reaches a written file - which turns
    # every timecode assertion below into a test of INHERITANCE rather than of a value typed into the node.
    "timeCode": "01:00:00:00",
    "output_folder": LEAK_PATH, "unc_share": r"\\studio\vault\plates",
    "prompt": LEAK_GRAPH, "workflow": '{"nodes": []}',
    "c2pa.manifest": "signed-blob"}}
IDENT_FIELDS = ("reel", "scene", "shot", "take", "camera", "lens", "timecode")

# 512x288: DNxHD refuses anything under 256x120 outright, so a smaller fixture would fail the MXF arms for a
# reason that has nothing to do with metadata.
_ramp = np.tile(np.linspace(0, 1, 512, dtype=np.float32)[None, :, None], (288, 1, 3))
IMAGES = torch.from_numpy(np.repeat(_ramp[None], 4, axis=0).copy())


def _exr_header(path):
    """The EXR header as a plain dict. Read through the OpenEXR module rather than cv2, which reports only
    the nine mandatory attributes and needs an environment flag to open the file at all."""
    import OpenEXR
    with OpenEXR.File(path) as f:
        return dict(f.header())


def _png_bitdepth(path):
    """Read the bit depth out of IHDR, in bytes. Pillow CANNOT be the reader here: it has no 48-bit RGB mode
    and silently hands back uint8 for a real 16-bit RGB PNG, so a check that asked Pillow would report 8 for a
    correct file. IHDR is the first chunk; bit depth is the ninth byte of its data, i.e. offset 24 overall."""
    with open(path, "rb") as fh:
        head = fh.read(26)
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return head[24]


def write(**kw):
    a = dict(profile="none", from_colorspace="sRGB - Display", output_colorspace="ACEScg",
             container="sequence", still_format="exr", video_codec="prores_4444", bit_depth="16f",
             auto_range=False, first_frame=1, last_frame=0, start_number=1, source_start=1, raw_data=False,
             fps=24.0, metadata=json.dumps(PLATE), images=IMAGES)
    a.update(kw)
    return io.OCIOWrite().write(**a)


def sidecar_of(path, strip_frame):
    p = io._sidecar_path(path, strip_frame)
    return (json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else None), p


# --------------------------------------------------------------------------- the sidecar, for every container
def check_sidecar_is_universal():
    """One sidecar beside a still, ONE beside a whole sequence, one beside a movie."""
    print("a sidecar is written for every container, and a sequence gets ONE, not one per frame")
    cases = [("still image", "exr", "16f", False), ("still image", "jpeg", "8", False),
             ("sequence", "exr", "16f", True), ("sequence", "tiff", "16", True),
             ("sequence", "png", "8", True), ("sequence", "png", "16", True)]
    for i, (cont, fmt, bd, strip) in enumerate(cases):
        res = write(container=cont, still_format=fmt, bit_depth=bd,
                    output_folder=f"uni{i}", filename="q")
        saved = res["result"][0]
        j, sp = sidecar_of(saved, strip)
        tag = f"{cont}/{fmt}{bd}"
        check(f"{tag}: sidecar exists", j is not None, os.path.basename(sp))
        if j is None:
            continue
        check(f"{tag}: names the file it describes", j.get("file") == os.path.basename(saved))
        check(f"{tag}: kind is stated", j.get("kind") == cont, f"got {j.get('kind')!r}")
        check(f"{tag}: startTimecode present", j.get("startTimecode") == "01:00:00:00",
              f"got {j.get('startTimecode')!r}")
        if cont == "sequence":
            folder = os.path.dirname(saved)
            n_json = len([f for f in os.listdir(folder) if f.endswith(".json")])
            check(f"{tag}: exactly ONE sidecar for {j.get('frames')} frames", n_json == 1, f"found {n_json}")
            check(f"{tag}: sidecar name carries no frame number",
                  not os.path.basename(sp).split(".")[-2].isdigit() if os.path.basename(sp).count(".") > 1 else True,
                  os.path.basename(sp))
            check(f"{tag}: frame count recorded", j.get("frames") == 4, f"got {j.get('frames')}")


def check_sidecar_split_is_honest():
    """container_keeps must list what the FILE really carries - checked against the file, not against a table."""
    print("\ncontainer_keeps / sidecar_only agree with what the artefact actually holds")
    import OpenEXR
    res = write(container="sequence", still_format="exr", bit_depth="16f", output_folder="hon_exr", filename="q")
    saved = res["result"][0]
    j, _ = sidecar_of(saved, True)
    with OpenEXR.File(saved) as f:
        hdr = dict(f.header())
    for k in (j or {}).get("container_keeps", []):
        check(f"exr: sidecar says '{k}' is in the file, and it is", k in hdr)
    for k in (j or {}).get("sidecar_only", []):
        check(f"exr: sidecar says '{k}' is NOT in the file, and it is not", k not in hdr)

    # A 16-BIT PNG NOW CARRIES THE IDENTITY SET TOO. These two checks previously asserted the opposite - that
    # the file held nothing - which was true while neither library could write it: cv2 writes 16-bit RGB and no
    # text, Pillow writes text and cannot represent 16-bit RGB. _png_splice_text writes the chunks itself, so
    # the assertions are inverted rather than deleted; a regression to "no text" now fails here.
    #
    # AND THE POSITION IS ASSERTED, because it is what makes the chunks visible to anything but Pillow. Text
    # placed before IEND (after IDAT) is legal PNG that OpenImageIO cannot see: its reader takes text out of
    # png_read_info, before the pixels. Measured with a control - identical chunks before IDAT are listed by
    # oiiotool, after IDAT are not. OIIO is the reader behind Nuke, Katana, Houdini and Blender.
    res = write(container="sequence", still_format="png", bit_depth="16", output_folder="hon_p16", filename="q")
    p16 = res["result"][0]
    j16, _ = sidecar_of(p16, True)
    from PIL import Image
    with Image.open(p16) as im:
        t16 = dict(getattr(im, "text", None) or {})
    check("png16: the file really does carry text now", bool(t16), f"got {sorted(t16)}")
    check("png16: every identity field reached the file", all(f in t16 for f in IDENT_FIELDS),
          f"got {sorted(t16)}")
    check("png16: sidecar reports them as embedded rather than sidecar-only",
          bool((j16 or {}).get("container_keeps")), f"got {(j16 or {}).get('container_keeps')}")
    blob16 = open(p16, "rb").read()
    first_idat, first_itxt = blob16.find(b"IDAT"), blob16.find(b"iTXt")
    check("png16: the chunks sit BEFORE the first IDAT, or no OIIO-based reader sees them",
          0 < first_itxt < first_idat, f"iTXt at {first_itxt}, IDAT at {first_idat}")
    check("png16: XMP is written under the keyword PNG Third Edition defines for it",
          b"XML:com.adobe.xmp" in blob16)
    check("png16: and it is still a 16-bit file after splicing",
          _png_bitdepth(p16) == 16, f"bit depth {_png_bitdepth(p16)}")
    # THE NODE'S OWN STATUS TEXT HAS TO AGREE WITH THE FILE. It did not: the splice landed, the sidecar listed
    # seven kept keys, and the on-node line still read "16-bit PNG carries no text chunks" - the old branch
    # shadowed the new one for exactly the case the feature was built for. Found by review, and the first fix had
    # NO test, so a mutation putting the wrong text back stayed green. A status line contradicting the artefact is
    # worse than none: it sends someone hunting for metadata that is already in their hand.
    ui_meta = "; ".join((res.get("ui") or {}).get("meta") or [])
    check("png16: the node does not claim the file carries no text",
          "carries no text" not in ui_meta, f"ui.meta={ui_meta!r}")
    check("png16: it reports the identity fields it actually embedded",
          "identity field" in ui_meta and "iTXt" in ui_meta, f"ui.meta={ui_meta!r}")
    check("png16: and the count it reports matches what is in the file",
          f"{len(t16 & set(IDENT_FIELDS)) if isinstance(t16, set) else len([k for k in IDENT_FIELDS if k in t16])}"
          " identity field(s)" in ui_meta, f"ui.meta={ui_meta!r}, file has {sorted(t16)}")

    # an 8-bit PNG carries the identity set, so the sidecar must say so
    res = write(container="sequence", still_format="png", bit_depth="8", output_folder="hon_p8", filename="q")
    j8, _ = sidecar_of(res["result"][0], True)
    with Image.open(res["result"][0]) as im:
        text = dict(getattr(im, "text", None) or {})
    check("png8: sidecar claims some keys ARE embedded", bool((j8 or {}).get("container_keeps")))
    check("png8: every identity field reached the file", all(f in text for f in IDENT_FIELDS),
          f"got {sorted(text)}")


def check_nothing_delivered_carries_the_machine():
    """The path / graph guard, on every format INCLUDING the sidecar .json itself."""
    print("\nno delivered file carries a machine path or the embedded workflow - the sidecar included")
    import OpenEXR
    from PIL import Image
    cases = [("still image", "exr", "16f", False), ("sequence", "exr", "16f", True),
             ("sequence", "tiff", "16", True), ("sequence", "png", "8", True),
             ("still image", "jpeg", "8", False)]
    for i, (cont, fmt, bd, strip) in enumerate(cases):
        res = write(container=cont, still_format=fmt, bit_depth=bd, output_folder=f"leak{i}", filename="q")
        saved = res["result"][0]
        tag = f"{cont}/{fmt}{bd}"
        # the sidecar: read as raw text, so a value nested anywhere is caught
        _, sp = sidecar_of(saved, strip)
        blob = open(sp, encoding="utf-8").read() if os.path.isfile(sp) else ""
        check(f"{tag}: sidecar has no absolute path", LEAK_PATH not in blob)
        check(f"{tag}: sidecar has no UNC path", "studio" not in blob or "vault" not in blob)
        check(f"{tag}: sidecar has no embedded graph", "KSampler" not in blob)
        j = json.loads(blob) if blob else {}
        check(f"{tag}: the withholding is NAMED, not silent",
              "output_folder" in (j.get("withheld", {}).get("keys") or [])
              or "output_folder" in (j.get("source", {}).get("withheld_keys") or []),
              f"withheld={j.get('withheld', {}).get('keys')}")
        # the image itself
        raw = open(saved, "rb").read()
        for bad, label in ((b"D:/secret", "absolute path"), (b"KSampler", "embedded graph")):
            check(f"{tag}: the image bytes carry no {label}", bad not in raw)
        if fmt == "exr":
            with OpenEXR.File(saved) as f:
                hdr = dict(f.header())
            for k in ("output_folder", "prompt", "workflow", "unc_share", "c2pa.manifest"):
                check(f"{tag}: '{k}' is not an EXR header attribute", k not in hdr)
            check(f"{tag}: whiteLuminance is not inherited from the plate", "whiteLuminance" not in hdr)
        if fmt == "png" and bd == "8":
            with Image.open(saved) as im:
                text = dict(getattr(im, "text", None) or {})
            check(f"{tag}: no leak in the PNG text chunks",
                  not any(LEAK_PATH in v or "KSampler" in v for v in text.values()))


# --------------------------------------------------------------------------- adoptedNeutral
def check_adopted_neutral():
    """Authored, and equal to the white point of the chromaticities written beside it."""
    print("\nadoptedNeutral is authored from the SAME anchor as chromaticities, as a v2f")
    import OpenEXR
    for cs, want in (("ACEScg", (0.32168, 0.33767)), ("sRGB - Display", (0.3127, 0.3290))):
        res = write(container="still image", still_format="exr", output_colorspace=cs,
                    output_folder="an_" + io._cs_tag(cs), filename="q")
        with OpenEXR.File(res["result"][0]) as f:
            hdr = dict(f.header())
        an, ch = hdr.get("adoptedNeutral"), hdr.get("chromaticities")
        check(f"{cs}: adoptedNeutral present", an is not None)
        if an is None:
            continue
        got = tuple(float(x) for x in np.asarray(an).reshape(-1))
        check(f"{cs}: it is a 2-vector", len(got) == 2, f"got {got}")
        check(f"{cs}: equals the published white point {want}",
              max(abs(a - b) for a, b in zip(got, want)) <= 1e-6, f"got {got}")
        if ch is not None:
            w = tuple(float(x) for x in np.asarray(ch).reshape(-1)[6:8])
            check(f"{cs}: agrees with chromaticities[6:8] - one anchor, not two derivations",
                  max(abs(a - b) for a, b in zip(got, w)) <= 1e-9, f"an={got} chroma={w}")
    # THE TYPE IS PART OF THE CONTRACT. OpenEXR types this name as a v2f and refuses a list for it outright, so
    # the author side must emit a tuple - a list there would be dropped by the survive-a-bad-attribute path and
    # the attribute would go missing with no error at all. Asserted at the LIBRARY, because that is where the
    # rule lives; our writer then coerces a numeric list to a tuple as a safety net, which is checked after.
    try:
        with OpenEXR.File({"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage,
                           "adoptedNeutral": [0.32168, 0.33767]},
                          {"RGB": np.zeros((2, 2, 3), np.float16)}) as f:
            f.write(os.path.join(TMP, "an_list.exr"))
        check("OpenEXR refuses a LIST for adoptedNeutral (hence the tuple)", False, "it was accepted")
    except Exception:
        check("OpenEXR refuses a LIST for adoptedNeutral (hence the tuple)", True)
    p = os.path.join(TMP, "an_type.exr")
    io._save_exr_with_meta(p, np.zeros((4, 4, 3), np.float32), "16f", None, "zip",
                           {"adoptedNeutral": [0.32168, 0.33767]})
    with OpenEXR.File(p) as f:
        got = dict(f.header()).get("adoptedNeutral")
    check("but a plate's numeric LIST off the JSON wire is coerced, not lost", got is not None,
          f"got {got!r}")
    a = io._authored_attrs("ACEScg", 24.0, 1, None, False)
    check("_authored_attrs emits a tuple", isinstance(a.get("adoptedNeutral"), tuple),
          f"got {type(a.get('adoptedNeutral')).__name__}")
    # raw_data claims no colorimetry at all, and that must include this
    r = io._authored_attrs("ACEScg", 24.0, 1, None, True)
    check("raw_data authors no adoptedNeutral (it claims no gamut either)", "adoptedNeutral" not in r)


# --------------------------------------------------------------------------- TIFF
def check_tiff():
    print("\nTIFF: one well-formed ImageDescription, the identity in standard tags + XMP")
    import tifffile
    res = write(container="sequence", still_format="tiff", bit_depth="16", output_folder="tif", filename="q")
    saved = res["result"][0]
    with tifffile.TiffFile(saved) as tf:
        codes = [t.code for t in tf.pages[0].tags]
        tags = {t.name: t.value for t in tf.pages[0].tags}
    # THE DEFECT THIS FREEZES: default metadata={} + description= wrote tag 270 twice, and readers disagreed.
    check("ImageDescription appears exactly ONCE (a duplicate tag is malformed TIFF)",
          codes.count(270) == 1, f"tag 270 x{codes.count(270)}")
    check("and it is the colorspace, not tifffile's shaped JSON", tags.get("ImageDescription") == "ACEScg",
          f"got {tags.get('ImageDescription')!r}")
    check("Software names this pack, not tifffile", tags.get("Software") == "ComfyUI-OCIO",
          f"got {tags.get('Software')!r}")
    check("reel -> DocumentName (269)", tags.get("DocumentName") == PLATE["attrs"]["reel_name"],
          f"got {tags.get('DocumentName')!r}")
    # NEVER SPLIT OUT OF ONE STRING: cameraModel 'ALEXA 35' once became Make 'ALEXA' / Model '35'.
    check("Make comes from a make key", tags.get("Make") == "ARRI", f"got {tags.get('Make')!r}")
    check("Model is the model, whole", tags.get("Model") == "ALEXA 35", f"got {tags.get('Model')!r}")
    check("an XMP packet is present (tag 700)", 700 in codes, f"codes={sorted(set(codes))}")
    xmp = next((t.value for t in tifffile.TiffFile(saved).pages[0].tags if t.code == 700), b"")
    txt = xmp.decode("utf-8", "replace") if isinstance(xmp, (bytes, bytearray)) else str(xmp)
    for f in ("scene", "shot", "take", "lens", "timecode"):
        check(f"XMP carries ocio:{f}", f"<ocio:{f}>" in txt)
    check("XMP names OUR namespace, not a term invented inside Dublin Core", io._XMP_NS in txt)
    check("the shot code keeps its leading zero in the file", "<ocio:shot>0106</ocio:shot>" in txt)
    # XML escaping: a lens name with & or < must not corrupt the packet
    kw = io._tiff_meta_kwargs({"scene": "S1 & S2", "lens": "<Cooke>"})
    pk = next((v for (code, _t, _c, v, _w) in kw.get("extratags", []) if code == 700), b"")
    body = pk.decode("utf-8")
    check("XMP escapes & and <", "&amp;" in body and "&lt;Cooke&gt;" in body)
    import xml.dom.minidom as _mdom
    try:
        _mdom.parseString(body.split("?>", 1)[1].rsplit("<?xpacket", 1)[0])
        check("the XMP packet is well-formed XML", True)
    except Exception as e:
        check("the XMP packet is well-formed XML", False, str(e)[:120])


# --------------------------------------------------------------------------- PNG
def check_png():
    print("\nPNG: iTXt (UTF-8) traceability, at BOTH bit depths since 2026-08-12")
    from PIL import Image
    res = write(container="sequence", still_format="png", bit_depth="8", output_folder="png8", filename="q")
    saved = res["result"][0]
    check("the file really has an iTXt chunk, not tEXt", b"iTXt" in open(saved, "rb").read())
    with Image.open(saved) as im:
        text = dict(getattr(im, "text", None) or {})
    check("colorspace is still written", text.get("colorspace") == "ACEScg", f"got {text.get('colorspace')!r}")
    for f in IDENT_FIELDS:
        check(f"identity field '{f}' is in the PNG", f in text, f"got {sorted(text)}")
    check("the shot code keeps its leading zero", text.get("shot") == "0106", f"got {text.get('shot')!r}")
    # iTXt is the UTF-8 chunk; tEXt would mangle these
    p = os.path.join(TMP, "utf8.png")
    s = u"Cooke S4/i 32mm \u2013 T2.0 caf\u00e9"
    io._save_still(p, np.zeros((4, 4, 3), np.float32), "png", "8", None, "sRGB - Display", "zip", {"lens": s})
    with Image.open(p) as im:
        got = dict(im.text).get("lens")
    check("a non-ASCII lens name round-trips byte-exactly", got == s, _ascii(repr(got)))


# --------------------------------------------------------------------------- video: colour range + MXF
def check_color_range():
    """Settled by control experiment: it is prores_ks IN a MOV, not the flag and not the container."""
    print("\n-color_range: kept, because it lands for every codec except ProRes in a MOV")
    if not HAVE_FFMPEG:
        print("  SKIP (no ffmpeg)")
        return
    check("the flag is still passed", "-color_range" in io._video_color_tags("sRGB - Display"))
    arr = np.asarray(IMAGES.numpy(), np.float32)
    for codec, want in (("prores_4444", None), ("dnxhr_hq", "tv"), ("h264", "tv"), ("hevc", "tv")):
        ext = ".mov" if codec.startswith(("prores", "dnxhr")) else ".mp4"
        p = os.path.join(TMP, f"cr_{codec}{ext}")
        io.save_video(arr, p, codec, 24.0, "sRGB - Display", None, meta_attrs={"title": "t"},
                      timecode="01:00:00:00", source_meta={})
        pr = subprocess.run([io._FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=color_range", "-of", "json", p], capture_output=True, text=True)
        got = (json.loads(pr.stdout or "{}").get("streams") or [{}])[0].get("color_range")
        check(f"{codec}{ext}: color_range is {want!r}", got == want, f"got {got!r}")


def check_mxf():
    print("\nMXF: DNxHR in OP1a and OPAtom, measured before being offered")
    if not HAVE_FFMPEG:
        print("  SKIP (no ffmpeg)")
        return
    check("dnxhr_hq_mxf is offered by the node",
          "dnxhr_hq_mxf" in io.OCIOWrite.INPUT_TYPES()["required"]["video_codec"][0])
    # the extension must be .mxf, and MXF has to be tested BEFORE the dnxhr prefix test
    for codec in ("dnxhr_hq_mxf", "dnxhr_hq_mxf_opatom"):
        p = io._write_output_paths(TMP, "q", "video", "exr", codec, "ACEScg", False, False, 1, 1)[0]
        check(f"{codec} -> .mxf (not .mov, though it also startswith 'dnxhr')", p.lower().endswith(".mxf"), p)

    audio = {"waveform": torch.zeros((1, 2, 48000)), "sample_rate": 48000}
    for codec, op, muxed in (("dnxhr_hq_mxf", "OP1a", True), ("dnxhr_hq_mxf_opatom", "OPAtom", False)):
        res = write(container="video", video_codec=codec, output_folder="mxf_" + op, filename="q", audio=audio)
        saved = res["result"][0]
        check(f"{op}: the file was written", os.path.isfile(saved) and os.path.getsize(saved) > 0,
              f"{os.path.basename(saved)} {os.path.getsize(saved) if os.path.isfile(saved) else 0} bytes")
        pr = subprocess.run([io._FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", saved],
                            capture_output=True, text=True)
        d = json.loads(pr.stdout or "{}")
        check(f"{op}: the container really is MXF", d.get("format", {}).get("format_name") == "mxf",
              f"got {d.get('format', {}).get('format_name')!r}")
        # WHICH OPERATIONAL PATTERN, not just "an MXF". ffprobe reports format_name 'mxf' for both patterns, so
        # that check alone cannot tell them apart - and it did not: dropping `-f <muxer>` let ffmpeg guess the
        # muxer from the .mxf extension, silently writing OP1a for an OPAtom request, and every other assertion
        # here stayed green. Found by mutation. The SMPTE operational-pattern UL is the label a downstream MXF
        # tool actually keys on, and the two differ in it.
        want_ul = {"OP1a": "060e2b34.04010101.0d010201.01010900",
                   "OPAtom": "060e2b34.04010102.0d010201.10030000"}[op]
        got_ul = (d.get("format", {}).get("tags") or {}).get("operational_pattern_ul")
        check(f"{op}: the operational pattern really is {op}", got_ul == want_ul, f"got {got_ul!r}")
        allt = dict(d.get("format", {}).get("tags") or {})
        for s in d.get("streams", []):
            allt.update(dict(s.get("tags") or {}))
        vs = next((x for x in d.get("streams", []) if x.get("codec_type") == "video"), {})
        auds = [x for x in d.get("streams", []) if x.get("codec_type") == "audio"]
        check(f"{op}: DNxHR picture", vs.get("codec_name") == "dnxhd", f"got {vs.get('codec_name')!r}")
        check(f"{op}: colour tags survive", vs.get("color_primaries") == "bt709", f"got {vs.get('color_primaries')!r}")
        check(f"{op}: MXF carries the range ProRes/MOV cannot", vs.get("color_range") == "tv",
              f"got {vs.get('color_range')!r}")
        check(f"{op}: timecode survives", allt.get("timecode") == "01:00:00:00", f"got {allt.get('timecode')!r}")
        check(f"{op}: reel_name survives as the source package name",
              allt.get("reel_name") == PLATE["attrs"]["reel_name"], f"got {allt.get('reel_name')!r}")
        # the ten identity tags a plain -metadata drops: they need the comment_ prefix
        for f in ("scene", "shot", "take", "lens"):
            check(f"{op}: comment_{f} survives an ffmpeg round trip", allt.get(f"comment_{f}"),
                  f"got {allt.get(f'comment_{f}')!r}")
        check(f"{op}: comment_shot keeps its leading zero", allt.get("comment_shot") == "0106",
              f"got {allt.get('comment_shot')!r}")
        check(f"{op}: no machine path reached the container",
              not any(LEAK_PATH in str(v) or "KSampler" in str(v) for v in allt.values()))
        # OPAtom holds ONE essence per file, so the track must go beside it - and the encode must NOT fail
        wav = os.path.splitext(saved)[0] + ".wav"
        if muxed:
            check(f"{op}: the audio track is muxed in", len(auds) == 1, f"{len(auds)} audio stream(s)")
        else:
            check(f"{op}: no second stream (ffmpeg refuses one)", len(auds) == 0, f"{len(auds)} audio stream(s)")
            check(f"{op}: the track is written beside it instead", os.path.isfile(wav),
                  os.path.basename(wav))
            note = ((res.get("ui") or {}).get("audio") or [""])[0]
            check(f"{op}: and the artist is told", "sidecar" in note.lower(), f"ui.audio={note!r}")


# --------------------------------------------------------------------------- write_audio actually refuses
def check_write_audio():
    """The toggle must CHANGE BEHAVIOUR, not merely be accepted.

    This pack has already shipped the other outcome: `resend` was added to six methods and three of them never
    read it, so the signature took the argument and the behaviour ignored it, with the whole gate green. So both
    states are exercised here and the verdict comes from the artefacts on disk.

    Why a widget is needed at all, when there is already an AUDIO socket: a native ComfyUI Video input carries
    its own track and write() adopts it when nothing is wired, so with a socket alone there is no wire to
    disconnect in order to decline it. A socket can add a track; only a widget can refuse one."""
    print("\nwrite_audio: OFF really means no sound, in both the muxed and the sidecar case")
    audio = {"waveform": torch.zeros((1, 2, 48000)), "sample_rate": 48000}
    if io._FFMPEG:
        on = write(container="video", video_codec="prores_4444", output_folder="wa_on",
                   filename="q", audio=audio, write_audio=True)["result"][0]
        off = write(container="video", video_codec="prores_4444", output_folder="wa_off",
                    filename="q", audio=audio, write_audio=False)["result"][0]

        def n_audio(p):
            r = subprocess.run([io._FFPROBE, "-v", "error", "-select_streams", "a", "-show_streams",
                                "-of", "json", p], capture_output=True, text=True, encoding="utf-8")
            return len((json.loads(r.stdout or "{}").get("streams") or []))

        check("ON: the track is muxed into the movie", n_audio(on) == 1, f"{n_audio(on)} audio stream(s)")
        check("OFF: the movie has NO audio stream", n_audio(off) == 0, f"{n_audio(off)} audio stream(s)")
        check("OFF: and the picture was still written",
              os.path.isfile(off) and os.path.getsize(off) > 0, f"{os.path.getsize(off)} bytes")
    else:
        print("  SKIP the muxed half (no ffmpeg)")

    # a sequence cannot hold audio, so the track goes beside it - and OFF must suppress that too, not just the mux
    son = write(container="sequence", still_format="exr", bit_depth="16f", output_folder="wa_seq_on",
                filename="q", audio=audio, write_audio=True)["result"][0]
    soff = write(container="sequence", still_format="exr", bit_depth="16f", output_folder="wa_seq_off",
                 filename="q", audio=audio, write_audio=False)["result"][0]

    def wavs(p):
        return sorted(glob.glob(os.path.join(os.path.dirname(p), "*.wav")))

    check("ON: a sidecar .wav is written beside the frames", len(wavs(son)) == 1, f"{[os.path.basename(x) for x in wavs(son)]}")
    check("OFF: NO sidecar .wav is written", not wavs(soff), f"{[os.path.basename(x) for x in wavs(soff)]}")
    check("OFF: and the frames were still written", os.path.isfile(soff) and os.path.getsize(soff) > 0)
    check("the default is ON, so a graph saved before this widget existed keeps its sound",
          io.OCIOWrite.INPUT_TYPES()["optional"]["write_audio"][1].get("default") is True)

    # None IS NOT False. An old saved graph does not OMIT this key - it sends null, because widgets_values is
    # positional over all widgets including this pack's two BUTTONS (which serialise as null), and write_audio
    # was appended exactly where the first button's null used to sit. Reproduced in the live canvas on a 23-value
    # graph: filename, fps and timecode all restored correctly, write_audio came back null. Falsy, so the sound
    # would have been dropped from every pre-existing workflow - the failure the "default is True" check above
    # was believed to prevent and does not, because the default only applies to an ABSENT key.
    won = write(container="sequence", still_format="exr", bit_depth="16f", output_folder="wa_none",
                filename="q", audio=audio, write_audio=None)["result"][0]
    check("write_audio=None is read as ON, not as OFF", len(wavs(won)) == 1,
          f"{[os.path.basename(x) for x in wavs(won)]}")
    check("and False is still honoured, so the repair did not just pin it on",
          not wavs(write(container="sequence", still_format="exr", bit_depth="16f",
                         output_folder="wa_off2", filename="q", audio=audio,
                         write_audio=False)["result"][0]))
    # The front end has to repair the widget too, or the artist sees a null toggle and re-saves the same null.
    _js = open(os.path.join(io.__file__.rsplit(os.sep, 1)[0], "web", "ocio_io.js"), encoding="utf-8").read()
    check("the front end repairs a null write_audio on load",
          "write_audio" in _js and re.search(
              r'W\(node,\s*"write_audio"\)[\s\S]{0,240}?===\s*null[\s\S]{0,80}?=\s*true', _js) is not None,
          "onConfigure must coerce null/undefined back to true")


# --------------------------------------------------------------------------- must never stop a render
def check_forbidden_substring_guard():
    """The pixel-state guard is tested AT THE WRITERS, because the node strips those keys before they get there.

    That gap is exactly why the original bug survived: _video_tag_args tested `kl in _META_FORBIDDEN`, an EXACT
    membership test against a tuple of SUBSTRINGS, so it matched a key spelled exactly 'c2pa' and let a real
    'c2pa.manifest' through - and no test noticed, because OCIOWrite.write() had already removed it upstream. A
    guard that cannot fire is not a guard, so it is exercised directly here. Found by mutation."""
    print("\nthe pixel-state guard fires on real spellings, at every writer, not just via the node")
    import OpenEXR
    spellings = ["c2pa.manifest", "C2PA:manifest", "jumbf.box", "xmp:ContentCredentials",
                 "MasteringDisplayPrimaries", "smpte2086.max_luminance", "hdr10plus.info",
                 "DolbyVision.rpu", "aces:AMF", "asc_mhl.hash"]
    for k in spellings:
        check(f"_meta_is_private({k!r}) is True", io._meta_is_private(k, "x"))
    for k in ("reel_name", "scene", "lens", "cameraModel", "timeCode", "com.ocio.gamut"):
        check(f"and a legitimate key {k!r} is NOT refused", not io._meta_is_private(k, "A001"))
    hostile = {k: "signed-blob" for k in spellings}
    hostile["reel_name"] = "A001R2XY"
    hostile["title"] = "S11 v01"
    for ext, legit in ((".mov", "A001R2XY"), (".mxf", "A001R2XY"), (".mp4", "S11 v01")):
        # the legitimate key differs per container ON PURPOSE: reel_name is not in the MP4 whitelist, because an
        # ilst box really is restrictive, so 'title' is the one that is allowed to travel there.
        args = " ".join(io._video_tag_args(os.path.join(TMP, "x" + ext), hostile))
        for k in spellings:
            check(f"{ext}: {k!r} is not passed to the container", k.lower() not in args.lower())
        check(f"{ext}: the legitimate key still travels", legit in args)
    p = os.path.join(TMP, "forbidden.exr")
    io._save_exr_with_meta(p, np.zeros((4, 4, 3), np.float32), "16f", None, "zip", hostile)
    with OpenEXR.File(p) as f:
        hdr = dict(f.header())
    for k in spellings:
        check(f"exr: {k!r} is not an attribute", k not in hdr)
    check("exr: the legitimate attribute is still written", hdr.get("reel_name") == "A001R2XY")
    payload = io._sidecar_payload(os.path.join(TMP, "x.exr"), hostile, None, {}, 24.0, "exr 16f")
    blob = json.dumps(payload)
    for k in spellings:
        check(f"sidecar: {k!r} carries no value", f'"{k}": "signed-blob"' not in blob)


def check_metadata_never_stops_a_render():
    print("\nmetadata is never the reason a render dies")
    hostile = json.dumps({"source": "bad.exr", "kind": "exr", "attrs": {
        "reel_name": {"nested": "dict"}, "scene": [1, 2, 3], "lens": None,
        "shot": "0106", "timeCode": "not a timecode"}})
    for fmt, bd in (("exr", "16f"), ("tiff", "16"), ("png", "8"), ("jpeg", "8")):
        try:
            res = write(container="sequence", still_format=fmt, bit_depth=bd,
                        output_folder=f"hostile_{fmt}", filename="q", metadata=hostile)
            ok = os.path.isfile(res["result"][0])
        except Exception as e:
            ok, res = False, None
            print(_ascii(f"      {fmt} raised {type(e).__name__}: {str(e)[:140]}"))
        check(f"{fmt}: a hostile plate does not stop the write", ok)
    for fmt, bd in (("exr", "16f"), ("tiff", "16"), ("png", "8")):
        res = write(container="sequence", still_format=fmt, bit_depth=bd,
                    output_folder=f"nometa_{fmt}", filename="q", metadata="")
        check(f"{fmt}: an unconnected source_meta is fine", os.path.isfile(res["result"][0]))
    # A LOWER-CASE `timecode` IS THE SAME FIELD. Found by a mutation pass: reverting the strip to exact-case
    # matching left every test green, because this file's PLATE spells it `timeCode` and so does the set of
    # fields the writer re-authors. A real DaVinci Resolve MXF spells it `timecode`, and with exact matching
    # the plate's string sailed through beside our re-authored one - two disagreeing timecodes in one header,
    # ours advancing per frame and the plate's frozen. Whichever a downstream tool reads first wins.
    lower_tc = {"source": PLATE["source"], "kind": PLATE["kind"],
                "attrs": {**{k: v for k, v in PLATE["attrs"].items() if k != "timeCode"},
                          "timecode": "02:00:00:00"}}
    res = write(container="sequence", still_format="exr", bit_depth="16f",
                output_folder="lowertc", filename="q", metadata=json.dumps(lower_tc))
    hdr = _exr_header(res["result"][0])
    tc_keys = sorted(k for k in hdr if k.lower() == "timecode")
    check("a lower-case plate timecode leaves exactly ONE timecode in the header",
          tc_keys == ["timeCode"], f"header carries {tc_keys}")
    check("...and it is the re-authored, correctly typed one, not the plate's string",
          not isinstance(hdr.get("timeCode"), str), f"got {type(hdr.get('timeCode')).__name__}")
    # An OpenEXR.TimeCode is not subscriptable and carries no h/m/s attributes worth relying on; its repr is
    # the field tuple, e.g. "(2, 0, 0, 0, 0, 0, 0, 0, 0, 0)", so the start is read off that.
    tc_repr = repr(hdr.get("timeCode"))
    check("...and the start is the plate's own, 02:00:00:00", tc_repr.startswith("(2, 0, 0, 0"), f"got {tc_repr}")

    # A PLATE THAT CARRIES NO TIMECODE writes normally, and simply carries none. Until 2026-08-13 this was a
    # node field left empty; the field is gone, so the case now arrives as a plate without the attribute.
    no_tc = {"source": PLATE["source"], "kind": PLATE["kind"],
             "attrs": {k: v for k, v in PLATE["attrs"].items() if k != "timeCode"}}
    res = write(container="sequence", still_format="tiff", bit_depth="16",
                output_folder="notc", filename="q", metadata=json.dumps(no_tc))
    check("tiff: a plate with no timecode is fine", os.path.isfile(res["result"][0]))
    side = os.path.splitext(res["result"][0])[0].rsplit(".", 1)[0] + ".json"
    if os.path.isfile(side):
        j = json.load(open(side, encoding="utf-8"))
        check("no timecode in, no timecode out", not j.get("startTimecode"),
              f"got {j.get('startTimecode')!r}")
    # A MALFORMED CODE IN THE PLATE MUST NOT STOP THE DELIVERY. This reverses the old rule deliberately: the
    # code used to be typed by hand, where silence would conform the whole delivery to the wrong place, so it
    # raised. It now arrives inside someone else's file, and a foreign header we cannot parse is not a reason
    # to fail a render - the frame is written, without a timecode.
    # THE HARDER CASE FIRST, and a mutation pass is what exposed it: "banana" never reaches the advance at
    # all, because it fails to parse and the start comes back as None - so removing the try/except around
    # tc_text left the gate green. A code that PARSES and is still illegal does reach it: 00:01:00:02 is a
    # legal label at 24 fps and an illegal drop-frame one at 29.97, where frames 00 and 01 do not exist at a
    # minute that is not a multiple of ten (SMPTE ST 12-1). The advance rejects it loudly, which is right for
    # a code a human typed and wrong for one a foreign plate carried - the delivery must still be written.
    illegal_df = {"source": PLATE["source"], "kind": PLATE["kind"],
                  "attrs": dict(PLATE["attrs"], timeCode="00:01:00:00")}
    try:
        res = write(container="sequence", still_format="exr", bit_depth="16f", output_folder="dftc",
                    filename="q", fps=29.97, metadata=json.dumps(illegal_df))
        check("an illegal drop-frame code in the plate does not stop the write",
              os.path.isfile(res["result"][0]))
    except Exception as e:
        check("an illegal drop-frame code in the plate does not stop the write", False,
              f"{type(e).__name__}: {str(e)[:140]}")

    bad_tc = {"source": PLATE["source"], "kind": PLATE["kind"],
              "attrs": dict(PLATE["attrs"], timeCode="banana")}
    try:
        res = write(container="sequence", still_format="tiff", bit_depth="16", output_folder="badtc",
                    filename="q", metadata=json.dumps(bad_tc))
        check("a malformed timecode in the plate does not stop the write", os.path.isfile(res["result"][0]))
    except Exception as e:
        check("a malformed timecode in the plate does not stop the write", False,
              f"{type(e).__name__}: {str(e)[:120]}")
    # ...and the hand-typed guard itself is still armed for anything that DOES parse a human's code.
    try:
        io._parse_timecode("banana")
        check("_parse_timecode still refuses a malformed code", False, "it was accepted")
    except ValueError:
        check("_parse_timecode still refuses a malformed code", True)


def main():
    check_sidecar_is_universal()
    check_sidecar_split_is_honest()
    check_nothing_delivered_carries_the_machine()
    check_adopted_neutral()
    check_tiff()
    check_png()
    check_color_range()
    check_mxf()
    check_write_audio()
    check_forbidden_substring_guard()
    check_metadata_never_stops_a_render()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: " + "; ".join(FAILS[:12]))
        sys.exit(1)
    print("ALL PASS - metadata reaches every format, and no delivered file carries the machine")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
