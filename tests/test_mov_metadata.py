# -*- coding: utf-8 -*-
"""A ProRes / DNxHR .mov must carry the shot's identity, and must NOT carry the machine's paths.

Run:  python tests/test_mov_metadata.py     (needs ffmpeg + ffprobe; no ComfyUI, no GPU)

WHY. A .mov out of this node is a DELIVERABLE. One that identifies itself only by filename is a support ticket
waiting to happen, and the pack used to pass a nine-tag whitelist into the container and drop lens, focal
length, take, camera and reel on the floor. That whitelist turned out not to be a QuickTime limit at all -
QuickTime's udta box takes arbitrary keys, but ffmpeg needs `-movflags use_metadata_tags` to write them.
Measured on real ProRes 4444 encodes read back with ffprobe: 5 of 14 tags survived without the flag, 14 of 14
with it, and 20 of 20 once the Apple ProApps keys were added.

The other half of the file is the reason the whitelist existed in the first place: with everything passing
through, an absolute machine path or an embedded graph JSON would travel inside a delivered file. That is the
leak ComfyUI's own SaveVideo has, and this pack must not acquire it. The path guard is therefore tested with
BOTH separators, because the first version checked only os.path.sep and let `D:/secret/x` straight through on
Windows.
"""
import importlib.util
import json
import os
import shutil
import subprocess
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


if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP: ffmpeg / ffprobe not on PATH")
    sys.exit(0)


def _load():
    tmp = tempfile.mkdtemp(prefix="ocio_movmeta_")
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
TMP = tempfile.mkdtemp(prefix="ocio_movmeta_out_")

SHOT = {
    "reel_name": "A001C086_171217_R0FR",
    "scene": "S11", "shot": "0106", "take": "3",
    "lens": "Zeiss Master Prime 35mm", "focal_length": 35,
    "make": "ARRI", "model": "ALEXA", "title": "S11 source v01",
}
LEAKS = {
    "output_folder": "D:/secret/project/shots",      # forward slashes - the case the first guard missed
    "input_dir": r"D:\secret\project\in",            # backslashes
    "unc_share": r"\\studio\vault\plates",
    "posix_root": "/mnt/studio/vault",
    "prompt": '{"1": {"class_type": "KSampler"}}',
    "workflow": '{"nodes": []}',
}

print("the path guard fires on every separator convention, not just this host's")
for k, v in LEAKS.items():
    if k in ("prompt", "workflow"):
        continue
    check(f"{k}={v!r} is recognised as a path", io._looks_like_a_path(v))
for ok in ("ALEXA", "S11", "35", "Zeiss Master Prime 35mm", "14:56:16:10", "Printing density"):
    check(f"{ok!r} is NOT mistaken for a path", not io._looks_like_a_path(ok))

print("\nthe MOV argument list carries the shot and refuses the leaks")
args = io._video_tag_args(os.path.join(TMP, "x.mov"), {**SHOT, **LEAKS})
sent = {a.split("=", 1)[0]: a.split("=", 1)[1] for i, a in enumerate(args)
        if i and args[i - 1] == "-metadata" and "=" in a}
check("-movflags use_metadata_tags is present", "use_metadata_tags" in args)
for k in SHOT:
    check(f"{k} is passed to the container", k in sent, f"got {sent.get(k)!r}")
for k in LEAKS:
    check(f"{k} is NOT passed to the container", k not in sent, f"leaked as {sent.get(k)!r}")

print("\nApple ProApps keys are derived, so Resolve and Final Cut see reel / scene / shot natively")
for key, want in (("com.apple.proapps.reel", SHOT["reel_name"]), ("com.apple.proapps.scene", SHOT["scene"]),
                  ("com.apple.proapps.shot", SHOT["shot"]),
                  ("com.apple.proapps.cameraName", SHOT["model"])):
    check(f"{key} == {want!r}", sent.get(key) == str(want), f"got {sent.get(key)!r}")

print("\nMP4 keeps its whitelist - its ilst box really is restrictive")
mp4 = io._video_tag_args(os.path.join(TMP, "x.mp4"), {**SHOT, **LEAKS})
mp4_sent = {a.split("=", 1)[0] for i, a in enumerate(mp4) if i and mp4[i - 1] == "-metadata" and "=" in a}
check("use_metadata_tags is NOT applied to mp4", "use_metadata_tags" not in mp4)
check("mp4 keeps only whitelisted keys", mp4_sent <= set(io._VIDEO_TAGS_MP4), f"got {sorted(mp4_sent)}")
check("no leak reaches the mp4 either", not (mp4_sent & set(LEAKS)))

print("\nA REAL ENCODE, read back with ffprobe - the only proof that matters")
out = os.path.join(TMP, "deliverable.mov")
cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
       "-i", "testsrc2=size=128x72:rate=24:duration=0.5",
       "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuv444p10le",
       "-timecode", "14:56:16:10"] + args + [out]
r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
check("the encode succeeded", r.returncode == 0, (r.stderr or "").strip()[:160])
if r.returncode == 0:
    pr = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", out],
                        capture_output=True, text=True, encoding="utf-8")
    d = json.loads(pr.stdout)
    tags = dict(d.get("format", {}).get("tags") or {})
    for st in d.get("streams", []):
        for k, v in (st.get("tags") or {}).items():
            tags.setdefault(k, v)
    for k, v in SHOT.items():
        check(f"{k} survived in the file", str(tags.get(k)) == str(v), f"read back {tags.get(k)!r}")
    check("com.apple.proapps.reel survived in the file",
          tags.get("com.apple.proapps.reel") == SHOT["reel_name"], f"{tags.get('com.apple.proapps.reel')!r}")
    check("a tmcd timecode track is present",
          any(st.get("codec_tag_string") == "tmcd" for st in d.get("streams", [])))
    check("the timecode is the one asked for", tags.get("timecode") == "14:56:16:10", f"{tags.get('timecode')!r}")
    blob = json.dumps(tags)
    for k, v in LEAKS.items():
        check(f"{k}'s value is absent from the finished file", str(v) not in blob)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
