"""Regression: the NCLC colour tags and metadata OCIO Write puts on a movie (run: python tools/test_video_color_tags.py).

THE BUG THIS LOCKS DOWN. _video_color_tags used to branch on `"2100" in cs or "pq" in cs` -> PQ. The OCIO config
names its HLG display colorspace "Rec.2100-HLG - Display", so it matched on "2100" and every HLG master was
written claiming transfer=smpte2084. Measured before the fix: an HLG pick produced trc=smpte2084. A player that
trusts the tag then applies the PQ EOTF to an HLG signal - not a subtle shift, a broken image. HLG's transfer
characteristic is arib-std-b67 (ARIB STD-B67, ITU-R BT.2100 HLG).

Two more of the same predicate flaw went with it, because they are the same defect one row down:
  - "ST2084-P3-D65 - Display" contains neither "2100" nor "pq", so a PQ HDR master fell through to the sRGB
    default and shipped tagged trc=iec61966-2-1 - a computer-display curve on an HDR deliverable.
  - Display P3 / P3-D65 were tagged bt709 primaries, describing a narrower gamut than the pixels occupy.
    smpte432 is SMPTE ST 432-1, the P3-D65 set (ffmpeg's help text misprints it as "SMPTE 422-1").

The table is checked as a table so a future edit cannot fix one row and quietly move another, and then the tags
are checked ON A REAL ENCODE read back with ffprobe: the generic -color_trc output option is a silent no-op for
libx264/libx265/prores_ks/dnxhd on this build, so only a written file proves the tag landed.

Also checked: ffmpeg's -timecode lands a real timecode track (as a -metadata tag it is dropped), the container
keeps only the tags it can represent, and the full set ships in a sidecar .json because the rest is dropped
without a word.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

import numpy as np

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


def _tags(io, cs):
    """(primaries, transfer, matrix) as _video_color_tags would hand them to ffmpeg, plus the setparams filter -
    both matter: on this build only the -vf lands primaries/transfer for these encoders."""
    args = io._video_color_tags(cs)
    d = {}
    for i, a in enumerate(args):
        if isinstance(a, str) and a.startswith("-") and i + 1 < len(args):
            d.setdefault(a, args[i + 1])
    return d.get("-color_primaries"), d.get("-color_trc"), d.get("-colorspace"), d.get("-vf", "")


# colorspace -> (primaries, transfer, matrix). Every row is a real entry in the ACES 2.0 studio config.
WANT = {
    # HLG: the row this test exists for.
    "Rec.2100-HLG - Display":     ("bt2020",   "arib-std-b67",   "bt2020nc"),
    # PQ must be untouched by the HLG fix.
    "Rec.2100-PQ - Display":      ("bt2020",   "smpte2084",      "bt2020nc"),
    "ST2084-P3-D65 - Display":    ("smpte432", "smpte2084",      "bt709"),
    # Rec.709 and sRGB must be untouched.
    "Rec.1886 Rec.709 - Display": ("bt709",    "bt709",          "bt709"),
    "Gamma 2.4 Encoded Rec.709":  ("bt709",    "bt709",          "bt709"),
    "sRGB - Display":             ("bt709",    "iec61966-2-1",   "bt709"),
    # ACEScg USED TO BE ASSERTED HERE AS ("bt709", "iec61966-2-1", "bt709"), with the comment "not a display
    # space: falls to the default". The assertion was correct about the code and wrong about the intent: it
    # pinned a ProRes full of linear AP1 declaring itself sRGB, which is the mistake this pack warns about
    # everywhere else. Moved to SILENT below (2026-08-15).
    "":                           ("bt709",    "iec61966-2-1",   "bt709"),
    # P3 primaries.
    "Display P3 - Display":       ("smpte432", "iec61966-2-1",   "bt709"),
    "P3-D65 - Display":           ("smpte432", "iec61966-2-1",   "bt709"),
}

# SPACES THAT MUST BE WRITTEN WITH NO COLOUR TAGS AT ALL, because CICP has no way to describe them and a wrong
# description is worse than none. ITU-T H.273 (V4, 07/2024) Table 3 defines TransferCharacteristics 0-18 with
# 19-255 reserved, and its only log entries are 9 and 10, which are not any camera curve; Table 2 defines
# ColourPrimaries 0-12 and 22, with no AP0, AP1 or camera wide gamut.
#
# Measured before the fix: 39 of the config's 55 colorspaces fell through to the sRGB default, so an ACEScg
# ProRes shipped tagged primaries=bt709 transfer=iec61966-2-1 - the file declaring itself sRGB while holding
# linear AP1. A player that trusts the tag then applies the sRGB EOTF to linear data.
#
# Asserted per ENCODING rather than per name, which is also how the fix decides: name matching is what made
# "Linear Rec.709 (sRGB)" pick up trc=bt709 from the substring "rec.709".
SILENT = ["ACEScg", "ACES2065-1", "ACEScc", "ACEScct", "ADX10", "ADX16",
          "ARRI LogC3 (EI800)", "ARRI LogC4", "S-Log3 S-Gamut3", "Log3G10 REDWideGamutRGB",
          "Linear Rec.709 (sRGB)", "Linear Rec.2020", "Linear ARRI Wide Gamut 4"]


def check_silent(io, cfg):
    # EVERY NAME IS CONFIRMED TO EXIST FIRST. A typo here would make the lookup return None, the fix would fall
    # through to name matching, and the row would be testing the old behaviour while looking like it tested the
    # new one. Caught for real on the first run: "Sony S-Log3 S-Gamut3" is not a name in this config, the space
    # is called "S-Log3 S-Gamut3".
    missing = [cs for cs in SILENT if cfg.getColorSpace(cs) is None]
    assert not missing, f"these names are not in the config, so they test nothing: {missing}"
    for cs in SILENT:
        enc = cfg.getColorSpace(cs).getEncoding() or ""
        assert enc in ("log", "scene-linear"), f"{cs!r} is encoding {enc!r}, not the kind this list is about"
        tags = io._video_color_tags(cs)
        assert tags == [], (
            f"{cs!r} must be written with NO colour tags, got {tags}.\n"
            "CICP cannot describe log or scene-linear, so any tag here is a false statement the file makes "
            "about itself. If this fires, the encoding lookup in _video_color_tags stopped working and the "
            "name-matching branches below it are answering instead.")
    print(f"[PASS] {len(SILENT)} log / scene-linear spaces are written untagged rather than mislabelled")


def check_table(io):
    for cs, want in WANT.items():
        prim, trc, spc, vf = _tags(io, cs)
        assert (prim, trc, spc) == want, (
            f"{cs!r}: got ({prim}, {trc}, {spc}), want {want}.\n"
            "If this row is HLG, the predicate has fallen back to matching \"2100\" before \"hlg\" and every HLG "
            "master is being written as PQ.")
        # the setparams filter has to agree with the output options, or the file gets one set and the frames another
        for key, val in (("color_primaries", prim), ("color_trc", trc), ("colorspace", spc)):
            assert f"{key}={val}" in vf, f"{cs!r}: setparams -vf disagrees with the output options ({vf!r})"
    assert _tags(io, "Rec.2100-HLG - Display")[1] != _tags(io, "Rec.2100-PQ - Display")[1], (
        "HLG and PQ resolved to the SAME transfer characteristic - they are different curves and a player cannot "
        "recover from the wrong one")
    print("[PASS] colour-tag table: HLG -> arib-std-b67, PQ -> smpte2084 (incl. ST2084-P3), Rec.709 and sRGB "
          "unchanged, P3 primaries smpte432")


def check_tag_whitelist(io):
    """Checked on the ARGS, not on the file, and that is the point. ffmpeg accepts -metadata <anything> and drops
    what the container cannot hold WITHOUT a warning, so a file-level assertion that an unmappable tag is absent
    passes whether we filtered it or not - it can never fail. Mutation proved exactly that (2026-08-12): removing
    the whitelist left the file-level check green. The filter has to be checked where it acts."""
    attrs = {"title": "T", "comment": "C", "artist": "A", "make": "ARRI", "model": "ALEXA 35",
             "location": "+51.5-0.1", "description": "D", "reel_name": "A001R2XY", "lensModel": "Signature 47",
             "shot": "010", "scene": "12A", "take": "3", "timecode": "10:00:00:00", "imageCounter": 7,
             "chromaticities": (0.713, 0.293, 0.165, 0.83, 0.128, 0.044, 0.32168, 0.33767)}
    # UPDATED 2026-08-12, after measuring instead of assuming. The .mov row used to assert that the container
    # "cannot represent" artist and description, and that reel_name / lensModel / shot / scene / take "survive
    # in NO container". Both claims are false, and a real ProRes 4444 encode read back with ffprobe says so:
    # `artist` survives with no flag at all, and with `-movflags use_metadata_tags` the file keeps 14 of 14
    # tags against 5 of 14 without it - description, lens, take, reel and all six com.apple.proapps.* keys
    # included. QuickTime's udta box takes arbitrary keys; the old nine-tag whitelist was ffmpeg's default, not
    # the container's limit, and it was dropping the shot's identity out of every delivered movie.
    #
    # A .mov therefore has no "unmappable" set left to police. What it must NEVER carry is machine paths and
    # embedded graph JSON, which is asserted against a real encode in tools/test_mov_metadata.py. MP4's ilst
    # box IS restrictive, so its whitelist stands and is still enforced below.
    for path, want, absent, never_keys in (
            ("x.mov", {"title", "comment", "make", "model", "location", "artist", "description",
                       "reel_name", "shot", "scene", "take"}, set(), ("chromaticities",)),
            ("x.mp4", {"title", "comment", "artist", "description"}, {"make", "model", "location"},
             ("reel_name", "lensModel", "shot", "scene", "take", "chromaticities"))):
        args = io._video_tag_args(path, attrs)
        keys = {args[i + 1].split("=", 1)[0] for i, a in enumerate(args) if a == "-metadata"}
        assert want <= keys, f"{path}: mappable tags missing from the args: {sorted(want - keys)}"
        assert not (keys & absent), (
            f"{path}: sent tags this container cannot represent ({sorted(keys & absent)}). ffmpeg drops them "
            "silently, so the written file looks the same either way and only the args reveal it.")
        for never in never_keys:
            assert never not in keys, (
                f"{path}: {never!r} was sent as a container tag. It does not survive here, so sending it hides "
                "the fact that it needs the sidecar.")
        if path.endswith(".mov"):
            assert "use_metadata_tags" in args, (
                "a .mov was written WITHOUT -movflags use_metadata_tags. Measured: the container then keeps 5 "
                "of 14 tags instead of 14, and lens, take, reel and camera vanish from the deliverable.")
        assert "timecode" not in keys, (
            f"{path}: timecode was sent as a generic -metadata tag. It has its own ffmpeg option; two routes for "
            "one value is how they drift apart.")
    print("[PASS] container tag whitelist checked on the ARGS: only mappable keys are sent per container")


def _probe(path):
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                         "stream=index,codec_type,codec_tag_string,color_primaries,color_transfer,color_space"
                         ":stream_tags:format_tags", "-of", "json", path],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert pr.returncode == 0, f"ffprobe failed on {path}: {pr.stderr[:300]}"
    d = json.loads(pr.stdout or "{}")
    streams = d.get("streams") or [{}]
    vid = next((s for s in streams if s.get("codec_type") == "video"), {})
    tags = dict((d.get("format") or {}).get("tags") or {})
    for s in streams:
        tags.update(s.get("tags") or {})
    # A real tmcd TRACK, not merely a timecode tag: that is what a post tool conforms from.
    tmcd = any(s.get("codec_tag_string") == "tmcd" for s in streams)
    return vid.get("color_primaries"), vid.get("color_transfer"), vid.get("color_space"), tags, tmcd


def check_real_encode(io, tmp):
    """A written file is the only proof: the generic -color_trc option is a silent no-op for these encoders here."""
    import torch
    imgs = torch.rand((6, 32, 48, 3)) * 0.5 + 0.2
    w = io.OCIOWrite()
    src = json.dumps({"source": "plate.0001.exr", "kind": "exr", "attrs": {
        "title": "SHOT TITLE", "comment": "a note", "reel_name": "A001R2XY", "lensModel": "Signature 47mm",
        # The start code arrives WITH THE PLATE - OCIO Write's own `start_timecode` field was removed on
        # 2026-08-13. The tmcd-track assertion below therefore now proves that a MOVIE CONTAINER inherits the
        # plate's timecode, which is the thing a post tool actually conforms from.
        "timeCode": "10:00:00:00",
        "c2pa.manifest": "<blob>", "masteringDisplayLuminance": "1000", "amf": "s.amf.xml"}})
    for cs, tag, want in (("Rec.2100-HLG - Display", "hlg", ("bt2020", "arib-std-b67", "bt2020nc")),
                          ("Rec.2100-PQ - Display", "pq", ("bt2020", "smpte2084", "bt2020nc")),
                          ("Rec.1886 Rec.709 - Display", "r709", ("bt709", "bt709", "bt709")),
                          ("sRGB - Display", "srgb", ("bt709", "iec61966-2-1", "bt709"))):
        res = w.write(profile="none", input_colorspace="sRGB - Display", output_colorspace=cs, container="video",
                      still_format="exr", video_codec="prores_422hq", bit_depth="16f", auto_range=False,
                      first_frame=1, last_frame=0, start_number=1, source_start=1, raw_data=False,
                      output_folder="$OUTPUT/vid", filename="v_" + tag, fps=25.0,
                      metadata=src, images=imgs)
        mov = res["result"][0]
        assert os.path.isfile(mov), f"{tag}: no file written"
        prim, trc, spc, tags, tmcd = _probe(mov)
        assert (prim, trc, spc) == want, (
            f"{tag}: the WRITTEN FILE reports ({prim}, {trc}, {spc}), want {want}. The colour tags did not land - "
            "check that the setparams -vf is still being placed before the output path.")
        assert tmcd, (
            f"{tag}: no tmcd timecode TRACK in the file, only (at best) a tag. A post tool conforms from the "
            "track, so this is the thing that has to be present.")
        assert tags.get("timecode") == "10:00:00:00", f"{tag}: timecode tag is {tags.get('timecode')!r}"
        assert tags.get("title") == "SHOT TITLE" and tags.get("comment") == "a note", (
            f"{tag}: mappable container tags did not land: {tags}")
        # INVERTED 2026-08-12. This used to assert reel_name and lensModel were ABSENT, on the belief that they
        # "survive in NO container". Measured on real ProRes 4444 encodes: with -movflags use_metadata_tags the
        # file keeps 14 of 14 tags instead of 5, these two included. So their absence was never a property of
        # QuickTime, it was ffmpeg's default - and the old assertion was protecting a deliverable that could not
        # say which reel or lens it came from. The check now demands the opposite, and is a real guard rather
        # than one that could not fail.
        assert tags.get("reel_name") == "A001R2XY", (
            f"{tag}: the movie does not carry its reel. -movflags use_metadata_tags missing? tags={tags}")
        assert (tags.get("lensmodel") or tags.get("lensModel")) == "Signature 47mm", (
            f"{tag}: the movie does not carry its lens: tags={tags}")
        assert tags.get("com.apple.proapps.reel") == "A001R2XY", (
            f"{tag}: no com.apple.proapps.reel, so Resolve and Final Cut will not see the reel natively")
        # And the leak side of the same widening: a delivered movie must never carry a machine path.
        blob = json.dumps(tags)
        for bad in ("D:\\", "D:/", "C:\\", "\\\\", "/mnt/", "class_type"):
            assert bad not in blob, f"{tag}: a path or graph fragment reached the container: {bad!r} in {tags}"
        side = os.path.splitext(mov)[0] + ".json"
        assert os.path.isfile(side), f"{tag}: no sidecar .json beside the movie - the non-mappable half of the " \
                                     "metadata would be lost entirely"
        j = json.load(open(side, encoding="utf-8"))
        assert j.get("startTimecode") == "10:00:00:00", f"{tag}: sidecar startTimecode {j.get('startTimecode')!r}"
        assert "reel_name" in j["source"]["attrs"] and "lensModel" in j["source"]["attrs"], (
            f"{tag}: the sidecar dropped what the container could not carry: {sorted(j['source']['attrs'])}")
        assert set(j["source"].get("dropped_pixel_state_claims") or []) >= {"c2pa.manifest",
                                                                           "masteringDisplayLuminance", "amf"}, (
            f"{tag}: the sidecar carried pixel-state claims forward: {j['source']}")
        for bad in ("c2pa.manifest", "masteringDisplayLuminance", "amf"):
            assert bad not in j["source"]["attrs"], f"{tag}: {bad} survived into the sidecar's attrs"
            assert bad not in (j.get("attributes") or {}), f"{tag}: {bad} survived into our authored attributes"
        if tag == "hlg":
            print(f"       hlg written file: primaries={prim} transfer={trc} matrix={spc} "
                  f"timecode={tags.get('timecode')} sidecar={os.path.basename(side)}")
    print("[PASS] real encode + ffprobe: HLG file reports trc=arib-std-b67; PQ / Rec.709 / sRGB unchanged; "
          "-timecode lands; container keeps only mappable tags; sidecar carries the rest")


def check_preview_has_no_sidecar(io, tmp):
    """The node's throwaway H.264 preview must not litter a .json next to itself - save_video only writes one when
    it is actually given metadata."""
    arr = np.clip(np.random.rand(4, 16, 24, 3).astype(np.float32), 0, 1)
    out = os.path.join(tmp, "prev.mp4")
    got = io.save_video(arr, out, "h264", 24.0, None, None)
    assert got is None, f"save_video returned {got!r} with no metadata; it must return None (no sidecar)"
    assert not os.path.isfile(os.path.splitext(out)[0] + ".json"), "a sidecar was written for a bare encode"
    print("[PASS] a bare encode (the node preview path) writes no sidecar")


def main():
    import tempfile
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("[SKIP] ffmpeg / ffprobe not on PATH; the colour tags can only be proven on a written file.")
        return
    tmp = tempfile.mkdtemp(prefix="ocio_vidtag_test_")
    io = _load_io_nodes(tmp)
    check_table(io)
    import importlib.util as _iu, sys as _sys
    _cfg, _ = _sys.modules["ocio_pkg.nodes"]._resolve_config_keyed("")
    check_silent(io, _cfg)
    check_tag_whitelist(io)
    check_real_encode(io, tmp)
    check_preview_has_no_sidecar(io, tmp)
    print("\nALL VIDEO COLOUR-TAG CHECKS PASSED")


if __name__ == "__main__":
    main()
