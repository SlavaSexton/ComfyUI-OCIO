# -*- coding: utf-8 -*-
"""Every video codec: the node's preview extension, the written extension and the advertised bit depth agree.

Run:  python tools/test_codec_ext_parity.py       (no pytest, no ComfyUI server, no GPU; ffmpeg for the depths)

WHY THIS FILE EXISTS. The rule "which container does this codec go in" was written down TWICE - once in
io_nodes.py and once as a name-prefix test in web/ocio_io.js - and the two disagreed the moment a codec was
added. 'dnxhr_hq_mxf'.startswith('dnxhr') is true, so the front end previewed shot.mov on the node while the
backend wrote shot.mxf. Nothing failed; the artist simply could not find the file the node had named.

So this reads BOTH SIDES and compares them: the codec list and the extension come from the imported module by
its production entry points (INPUT_TYPES and video_ext), the front-end table is parsed out of the real JS. A
codec added to one side and forgotten on the other fails here.

THE BIT DEPTHS ARE MEASURED, not declared. Each codec is encoded for real and read back with ffprobe, and the
depth is compared against the string the node shows the artist. This is the check that catches the specific
mistake made on 2026-08-12, when DNxHR 444 was described as 12-bit from Avid's specification while this
encoder writes 10 - it advertises no 12-bit pixel format at all.
"""
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


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def _load():
    tmp = tempfile.mkdtemp(prefix="ocio_codecext_")
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
    return sys.modules["ocio_pkg.io_nodes"], tmp


io_nodes, TMP = _load()
CODECS = list(io_nodes.OCIOWrite.INPUT_TYPES()["required"]["video_codec"][0])
print(f"the backend offers {len(CODECS)} video codecs: {CODECS}")
check("the list has no duplicate entries", len(set(CODECS)) == len(CODECS))
check("prores_4444 is still the default",
      io_nodes.OCIOWrite.INPUT_TYPES()["required"]["video_codec"][1].get("default") == "prores_4444")


# ------------------------------------------------------------------ the front-end table, from the real JS
def _js_table(name):
    """Pull `const <name> = { ... };` out of the front end and read its entries. Brace-counted rather than
    regex-terminated, because a value containing a brace would otherwise truncate the block silently."""
    src = open(os.path.join(_ROOT, "web", "ocio_io.js"), encoding="utf-8").read()
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*\{", src)
    if not m:
        return None, src
    i = m.end() - 1
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1], src
    return None, src


INFO_BLOCK, JS = _js_table("CODEC_INFO")
LABEL_BLOCK, _ = _js_table("CODEC_LABEL")
check("CODEC_INFO was found in the front end", INFO_BLOCK is not None)
check("CODEC_LABEL was found in the front end", LABEL_BLOCK is not None)

JS_EXT, JS_BITS, JS_LABEL = {}, {}, {}
if INFO_BLOCK:
    for key, body in re.findall(r"(\w+)\s*:\s*\{([^}]*)\}", INFO_BLOCK):
        b = re.search(r'bits\s*:\s*"([^"]*)"', body)
        e = re.search(r'ext\s*:\s*"([^"]*)"', body)
        if e:
            JS_EXT[key] = e.group(1)
        if b:
            JS_BITS[key] = b.group(1)
if LABEL_BLOCK:
    JS_LABEL = dict(re.findall(r'(\w+)\s*:\s*"([^"]*)"', LABEL_BLOCK))
print(f"the front end names {len(JS_EXT)} extensions and {len(JS_LABEL)} labels")

print("\nevery codec is known to BOTH sides")
missing_info = [c for c in CODECS if c not in JS_EXT]
missing_label = [c for c in CODECS if c not in JS_LABEL]
extra_info = [c for c in JS_EXT if c not in CODECS]
check("every backend codec has a front-end extension", not missing_info, f"missing: {missing_info}")
check("every backend codec has a front-end label", not missing_label, f"missing: {missing_label}")
check("the front end names no codec the backend does not offer", not extra_info, f"stale: {extra_info}")

print("\nthe extension the node PREVIEWS is the extension the backend WRITES")
for c in CODECS:
    want = io_nodes.video_ext(c)
    got = JS_EXT.get(c)
    check(f"{c}", got == want, f"backend {want}   front end {got}")

# The bug this file was written for, asserted by name so a regression is unmistakable.
check("dnxhr_hq_mxf really resolves to .mxf on both sides",
      io_nodes.video_ext("dnxhr_hq_mxf") == ".mxf" and JS_EXT.get("dnxhr_hq_mxf") == ".mxf")
check("an unknown codec falls back to .mp4 rather than raising", io_nodes.video_ext("no_such_codec") == ".mp4")

# ------------------------------------------------------------------ and the CONSUMER really reads the table
# Everything above compares the TABLE with the backend, and a mutation proved that insufficient: putting the old
# prefix ternary back into exampleName() left every check above green, because none of them exercise the
# function that draws the string. A correct table an indifferent function ignores is the original defect exactly.
#
# STRUCTURAL, and said so plainly: this reads the source of the drawing function rather than executing it, since
# the gate must run with no browser and no server. exampleName() depends on the canvas widget helpers, so the
# behavioural form of this check belongs to the front-end pass, where the string is read off the drawn node.
def _js_function(name):
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", JS)
    if not m:
        return None
    i = m.end() - 1
    depth = 0
    for j in range(i, len(JS)):
        if JS[j] == "{":
            depth += 1
        elif JS[j] == "}":
            depth -= 1
            if depth == 0:
                return JS[i:j + 1]
    return None


BODY = _js_function("exampleName")
check("exampleName() was found in the front end", BODY is not None)
if BODY:
    check("the drawn filename takes its extension from CODEC_INFO, not from a name prefix",
          "CODEC_INFO[" in BODY, BODY.strip()[:150].replace("\n", " "))
    check("and it derives no extension from a codec-name prefix",
          not re.search(r'startsWith\(\s*"(prores|dnxhr)', BODY))
check("no extension is decided by a prefix ternary anywhere in the front end",
      not re.search(r'startsWith\(\s*"(prores|dnxhr)[^)]*\)[^;]*"\.mo[vp]', JS))

# ------------------------------------------------------------------ the OTHER front/back naming axis
# The extension axis above was the one that had drifted. A reviewer then found a SECOND disagreement on the same
# node, in still-image naming, which every check above passes straight over: a still gets its source frame number
# stamped in, and the BACKEND'S RULE IS THE BATCH SIZE - write() passes `still_frame=(first_frame if n > 1 else
# None)`. The front end asked findUpstreamRead instead, so a multi-frame batch from anything other than an OCIO
# Read - a generation wired straight into Write - predicted `shot.png` while the write produced `shot.0005.png`.
# Reproduced live before the fix. The overwrite dialog therefore probed a file that never exists, never warned,
# and a repeat render silently replaced the previous one.
print("\nstill-image frame stamping: the backend's rule, and a front end that cannot under-warn")
FIRST = 5
p_plain = io_nodes._write_output_paths(TMP, "q", "still image", "png", "h264", "ACEScg", True, False, 1, 1)[0]
p_stamp = io_nodes._write_output_paths(TMP, "q", "still image", "png", "h264", "ACEScg", True, False, 1, 1,
                                      still_frame=FIRST)[0]
check("without a frame number the name is plain", os.path.basename(p_plain) == "q.png",
      os.path.basename(p_plain))
check("with one, it is stamped four digits wide", os.path.basename(p_stamp) == f"q.{FIRST:04d}.png",
      os.path.basename(p_stamp))
check("so the two names really are different files, which is what made the gap invisible",
      p_plain != p_stamp)

# The rule itself, from the real write(), not from a restatement of it.
import re as _re
_src = open(os.path.join(_ROOT, "io_nodes.py"), encoding="utf-8").read()
check("write() still decides the stamp by BATCH SIZE, not by an upstream node",
      _re.search(r"still_frame\s*=\s*\(\s*first_frame\s+if\s+n\s*>\s*1\s+else\s+None\s*\)", _src) is not None,
      "if this rule moves, the front-end probe below has to move with it")

# And the front end must probe BOTH candidates when nothing settles which it will be. Structural, because the
# gate runs no browser: the behavioural form is a click in the canvas.
_render = _js_function("ocioWriteRender") if INFO_BLOCK else None
check("ocioWriteRender was found", _render is not None)
if _render:
    check("it probes MORE THAN ONE candidate name rather than a single guess",
          "probes" in _render and _re.search(r"for\s*\(\s*const\s+\w+\s+of\s+probes\s*\)", _render) is not None)
    check("it unions the results, so either candidate existing counts as a conflict",
          ".add(" in _render and "Array.from(" in _render)
    check("and it still special-cases a settled sequence grab",
          '"sequence"' in _render and "still_frame" in _render)

# ------------------------------------------------------------------ the depths, measured
print("\nthe advertised bit depth, MEASURED by encoding and reading back")
FF = shutil.which("ffmpeg")
FP = shutil.which("ffprobe")
if not (FF and FP):
    print("  ffmpeg/ffprobe not on PATH - DEPTHS NOT MEASURED, and that half of this file did not run")
else:
    src = os.path.join(TMP, "src.mov")
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=512x288:rate=24:duration=0.25",
                    "-c:v", "ffv1", src], check=True, timeout=180)
    for c in CODECS:
        args = io_nodes.save_video.__wrapped__ if hasattr(io_nodes.save_video, "__wrapped__") else None
        # Take the encoder arguments from the module rather than restating them: a test that spells out
        # "-profile:v dnxhr_hqx" is testing its own copy of the mapping, which is the very defect above.
        enc = io_nodes._video_encoder_args(c) if hasattr(io_nodes, "_video_encoder_args") else None
        if enc is None:
            print(f"  (skipped {c}: the encoder arguments are not reachable without running save_video)")
            continue
        out = os.path.join(TMP, "d_" + c + io_nodes.video_ext(c))
        muxer = io_nodes._MXF_MUXER.get(c)
        cmd = [FF, "-hide_banner", "-loglevel", "error", "-y", "-i", src] + list(enc)
        if muxer:
            cmd += ["-f", muxer]
        cmd += [out]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=300)
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            check(f"{c}: encodes at all", False, (r.stderr or "").strip()[:120])
            continue
        pr = subprocess.run([FP, "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=pix_fmt,bits_per_raw_sample,profile", "-of", "json", out],
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
        st = (json.loads(pr.stdout or "{}").get("streams") or [{}])[0]
        pix = st.get("pix_fmt", "?")
        bits = st.get("bits_per_raw_sample")
        if bits is None:                      # ProRes does not report it; infer from the pixel format instead
            bits = 12 if "12" in pix else (10 if "10" in pix else 8)
        want = JS_BITS.get(c, "")
        check(f"{c}: the node says {want or '(nothing)'}", want == f"{int(bits)}-bit",
              f"measured {bits}-bit, pix_fmt {pix}, profile {st.get('profile')}")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS - the preview, the written file and the advertised depth agree for every codec")
