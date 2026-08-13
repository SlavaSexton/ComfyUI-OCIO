# -*- coding: utf-8 -*-
"""Both LTX HDR profiles: they exist, they mirror in the front-end, and 2.5's maths is the reference maths.

Run:  python tools/test_ltx_hdr_profiles.py      (no pytest, no ComfyUI server, no GPU)

WHY BOTH, AND WHY THEY MUST NOT BE MERGED. LTX-2.3 and LTX-2.5 both have an HDR mode and they arrive in
DIFFERENT transfers, so one preset cannot serve both:

  2.3  HDR IC-LoRA on the ARRI LogC3 (EI 800) curve. Lightricks' own ComfyUI node for it -
       LTXVHDRDecodePostprocess, in Lightricks/ComfyUI-LTXVideo, hdr.py, category "Lightricks/HDR" - already
       undoes the curve, so what arrives at OCIO Write is LINEAR. Their example workflow for it is
       example_workflows/2.3/LTX-2.3_ICLoRA_HDR_Distilled.json.
  2.5  HDR via the --hdr {SRGB_LINEAR,ACESCG,ACESCCT} flag in their reference CLI. Their reference rotates
       source primaries to ACEScg BEFORE compressing (ltx-core hdr.py:126-138), so the VAE hands out ACEScct
       LOG CODES that are already in AP1 primaries. Nothing in ComfyUI undoes that curve: their ComfyUI pack
       has no HDR workflow under example_workflows/2.5 and no acescct/acescg anywhere in it (checked
       2026-08-12), and stock comfy_extras/nodes_lt.py has no HDR code at all.

Feed 2.5 material through the 2.3 preset and log is treated as linear: the frame comes out flat and grey.
That is the failure this file exists to prevent.

THE 2.3 PRESET NAME IS FROZEN. A combo value is matched by string and ComfyUI answers an unknown one with
HTTP 400 and no fallback, so renaming "LTX 2.3 HDR" would break every saved graph that uses it. Asserted.
"""
import importlib.util
import os
import re
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import numpy as np

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def _load():
    tmp = tempfile.mkdtemp(prefix="ocio_ltxhdr_")
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
    return sys.modules["ocio_pkg.io_nodes"], sys.modules["ocio_pkg.nodes"]


io_nodes, core = _load()
REQ = io_nodes.OCIOWrite.INPUT_TYPES()["required"]
PROFILES = list(REQ["profile"][0])
CS_VALUES = set(REQ["from_colorspace"][0])

P23, P25 = "LTX 2.3 HDR", "LTX 2.5 HDR (ACEScct)"

print("both profiles are offered, and the 2.3 name is frozen")
check("the 2.3 profile still exists under its EXACT original name", P23 in PROFILES)
check("the 2.5 profile exists", P25 in PROFILES, repr(P25))
check("they are distinct entries, not one merged option", P23 != P25 and len(set(PROFILES)) == len(PROFILES))
print(f"         full list: {PROFILES}")

# ------------------------------------------------------------------ read the REAL backend mapping, by AST
# An earlier version of this file compared the front-end against a hardcoded table of what the mapping was
# expected to be. That made the whole "front-end mirrors the backend" section a lie: mutating the Python
# mapping to the wrong colorspace left every check green, because no check ever read the Python mapping.
# Both mutations that slipped through did so for that one reason. The mapping is therefore parsed out of the
# source here, and the front-end and the combo are both compared against THAT.
import ast


def backend_mapping(path):
    """{profile name: (from_colorspace, output_colorspace)} as actually assigned inside OCIOWrite.write."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    write = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "OCIOWrite":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "write":
                    write = item
    if write is None:
        return {}
    found = {}
    for node in ast.walk(write):
        if not isinstance(node, ast.If):
            continue
        # the branch guard: `profile == "<name>"`, possibly inside a BoolOp with `not raw_data`
        names = [c.comparators[0].value for c in ast.walk(node.test)
                 if isinstance(c, ast.Compare) and isinstance(c.ops[0], ast.Eq)
                 and isinstance(c.comparators[0], ast.Constant) and isinstance(c.comparators[0].value, str)
                 and isinstance(c.left, ast.Name) and c.left.id == "profile"]
        if not names:
            continue
        assigns = {}
        for st in node.body:
            if isinstance(st, ast.Assign) and isinstance(st.value, ast.Constant) \
                    and isinstance(st.targets[0], ast.Name) \
                    and st.targets[0].id in ("from_colorspace", "output_colorspace"):
                assigns[st.targets[0].id] = st.value.value
        if "from_colorspace" in assigns and "output_colorspace" in assigns:
            for nm in names:
                found[nm] = (assigns["from_colorspace"], assigns["output_colorspace"])
    return found


BACKEND = backend_mapping(os.path.join(_ROOT, "io_nodes.py"))
print(f"\nbackend mapping read from the source by AST: "
      f"{ {k: v for k, v in BACKEND.items() if 'LTX' in k} }")
check("the 2.3 mapping was found in write()", P23 in BACKEND)
check("the 2.5 mapping was found in write()", P25 in BACKEND)
check("the two profiles do NOT map to the same source colorspace",
      BACKEND.get(P23, ("a",))[0] != BACKEND.get(P25, ("b",))[0],
      f"2.3 from={BACKEND.get(P23, ('?',))[0]!r}, 2.5 from={BACKEND.get(P25, ('?',))[0]!r}")
check("2.5 arrives as a LOG encoding, as its own reference produces",
      BACKEND.get(P25, ("",))[0] == "ACEScct", f"got {BACKEND.get(P25, ('?',))[0]!r}")
check("2.3 arrives as LINEAR, because their own node already undid LogC3",
      BACKEND.get(P23, ("",))[0] == "Linear Rec.709 (sRGB)", f"got {BACKEND.get(P23, ('?',))[0]!r}")

print("\nthe colorspaces each profile selects are values the combo actually offers (the HTTP 400 trap)")
PSDR = "SDR Rec.709 delivery"
# The SDR row was added to PROFILE_CS on 2026-08-13 with a source comment claiming THIS FILE guarded it. It did
# not: the filter below read `k in (P23, P25)`, so mutating either side of the SDR mapping to garbage left the
# run green - proven by mutation, with a sanity mutation of the LTX 2.5 mapping going red to calibrate the probe.
# Every mapped profile is covered now, and the EXR-16f expectation became CONDITIONAL, because that profile
# deliberately forces no format: it is display-referred, and forcing scene-linear EXR would contradict its point.
EXPECT = {k: v for k, v in BACKEND.items() if k in (P23, P25, PSDR)}
FORCES_EXR = (P23, P25)
check("the SDR delivery profile is offered", PSDR in PROFILES)
check("and it is MAPPED in the backend, not merely listed", PSDR in BACKEND,
      f"backend maps: {sorted(BACKEND)}")
check("the SDR profile is display-referred on both sides, which is what makes it not an HDR preset",
      BACKEND.get(PSDR, ("", ""))[0] == "sRGB - Display"
      and BACKEND.get(PSDR, ("", ""))[1] == "Rec.1886 Rec.709 - Display",
      f"got {BACKEND.get(PSDR)}")
for prof, (src, dst) in sorted(EXPECT.items()):
    check(f"{prof}: from={src!r} is a real combo value", src in CS_VALUES)
    check(f"{prof}: out={dst!r} is a real combo value", dst in CS_VALUES)

print("\nthe front-end mirrors the backend for BOTH profiles")
js = open(os.path.join(_ROOT, "web", "ocio_io.js"), encoding="utf-8").read()
block = re.search(r"const PROFILE_CS\s*=\s*\{(.*?)\n\};", js, re.S)
check("PROFILE_CS is present in the front-end", block is not None)
if block:
    body = block.group(1)
    for prof, (src, dst) in EXPECT.items():
        row = re.search(r'"' + re.escape(prof) + r'"\s*:\s*\{([^}]*)\}', body)
        check(f"{prof} has a front-end row", row is not None)
        if row:
            r = row.group(1)
            got_from = re.search(r'from:\s*"([^"]*)"', r)
            got_out = re.search(r'out:\s*"([^"]*)"', r)
            got_fmt = re.search(r'fmt:\s*"([^"]*)"', r)
            got_bit = re.search(r'bit:\s*"([^"]*)"', r)
            check(f"{prof} front-end from matches the backend",
                  got_from and got_from.group(1) == src,
                  f"js={got_from.group(1) if got_from else None!r} py={src!r}")
            check(f"{prof} front-end out matches the backend",
                  got_out and got_out.group(1) == dst,
                  f"js={got_out.group(1) if got_out else None!r} py={dst!r}")
            if prof in FORCES_EXR:
                check(f"{prof} front-end forces EXR 16f, as the backend does",
                      got_fmt and got_fmt.group(1) == "exr" and got_bit and got_bit.group(1) == "16f",
                      f"fmt={got_fmt.group(1) if got_fmt else None} bit={got_bit.group(1) if got_bit else None}")
            else:
                # ABSENT, not merely different. applyProfile assigns these straight into two COMBO widgets, and
                # a row carrying fmt/bit that the backend does not force would push a format the artist did not
                # choose; a row carrying a JS `undefined` used to be written in verbatim and made the graph
                # unqueueable (400 value_not_in_list). Both failure modes are excluded by requiring no key.
                check(f"{prof} front-end forces NO format, matching a backend that forces none",
                      got_fmt is None and got_bit is None,
                      f"fmt={got_fmt.group(1) if got_fmt else None} bit={got_bit.group(1) if got_bit else None}")

print("\napplyProfile GUARDS its format writes, so a profile without fmt/bit cannot write `undefined`")
# Asserting the SDR row simply has no fmt/bit is NOT enough, and leaving it there was the same blind spot twice
# in one night: with the guard removed the row-shape checks above stay green, because they inspect the TABLE and
# never the function that consumes it. setWSilent is a bare `w.value = value`, so an unguarded assignment writes
# JavaScript `undefined` into two COMBO widgets - measured against the live backend, both serialisations are a
# hard reject (400 value_not_in_list for null, 400 required_input_missing for an absent key), i.e. a graph that
# cannot be queued at all. Structural, and said so: the gate runs no browser, so this reads the function's source.
m = re.search(r"function\s+applyProfile\s*\([^)]*\)\s*\{", js)
check("applyProfile was found in the front end", m is not None)
if m:
    i, depth = m.end() - 1, 0
    body = ""
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                body = js[i:j + 1]
                break
    for w in ("still_format", "bit_depth"):
        key = {"still_format": "fmt", "bit_depth": "bit"}[w]
        guarded = re.search(r"if\s*\(\s*p\." + key + r"\s*\)\s*setWSilent\(\s*node\s*,\s*[\"']" + w,
                            body) is not None
        bare = re.search(r"(?<!\)\s)^\s*setWSilent\(\s*node\s*,\s*[\"']" + w, body, re.M) is not None
        check(f"the {w} write is guarded on the profile actually carrying it", guarded,
              f"guarded={guarded} unguarded-assignment-present={bare}")
    check("the colorspace writes are NOT guarded, since every mapped profile carries both",
          re.search(r"setWSilent\(\s*node\s*,\s*[\"']from_colorspace", body) is not None)

print("\nthe backend really maps each profile, and forces EXR 16f for both")
src_txt = open(os.path.join(_ROOT, "io_nodes.py"), encoding="utf-8").read()
for prof, (src, dst) in EXPECT.items():
    # The mapping must be reachable from OCIOWrite.write, not merely mentioned in a comment. Requiring the
    # profile name to appear in an `elif profile ==` line is what distinguishes a live branch from prose.
    live = re.search(r'elif\s+profile\s*==\s*"' + re.escape(prof) + r'"', src_txt) is not None
    check(f"{prof} has a live branch in write(), not just a comment", live)
# Anchored on the ASSIGNMENT and searched backwards, deliberately not with a paren-matching regex. A regex
# of the form `if profile in \((.*?)\)` cannot work here, because the profile name itself contains
# parentheses: the non-greedy group closes on the ")" inside "LTX 2.5 HDR (ACEScct)" and the captured text
# then excludes the very name being looked for. The first version of this check failed for exactly that
# reason, on a list that was correct - the name I chose broke the probe that inspects it.
ANCHOR = 'still_format, bit_depth = "exr", "16f"'
pos = src_txt.find(ANCHOR)
window = src_txt[max(0, pos - 500):pos] if pos >= 0 else ""
check("the EXR-16f forcing block exists", pos >= 0)
check("both HDR profiles are in the EXR-16f forcing list",
      P23 in window and P25 in window,
      f"2.3 present: {P23 in window}, 2.5 present: {P25 in window}")
check("the forcing block is still gated on the profile, not applied unconditionally",
      "if profile in (" in window)
# The SDR profile must be ABSENT from that list. It is display-referred, so forcing EXR 16f would hand the
# artist a scene-linear container for display-referred codes - and this pack's own _auto_input_cs then reads any
# .exr back as ACEScg, which is how such a file becomes a wrong-looking plate two nodes later.
check("the SDR delivery profile is NOT in the EXR-16f forcing list", PSDR not in window,
      "it forces a format it has no business forcing")

print("\nTHE 2.5 MATHS IS THE REFERENCE MATHS: OCIO's ACEScct -> ACEScg must equal the published curve")
# This is the check that makes the 2.5 preset trustworthy. The preset applies no curve of its own - it asks
# the OCIO config to go ACEScct -> ACEScg, which should be the transfer decode and nothing else, because
# ACEScct's native primaries ARE AP1. If that holds, the preset reproduces Lightricks' decode
# (ltx-core hdr.py:75-79) using the colour path the community has already vetted, with no new maths.
A_LIN, B_LIN, Y_BRK, LOG_M, LOG_B = 10.5402377416545, 0.0729055341958355, 0.155251141552511, 17.52, 9.72


def ref_decompress(y):
    y = np.asarray(y, np.float64)
    return np.where(y > Y_BRK, np.power(2.0, y * LOG_M - LOG_B), (y - B_LIN) / A_LIN)


codes = np.linspace(0.0, 1.0, 257, dtype=np.float32)
img = np.repeat(codes[None, :, None], 3, axis=2)[None]          # [1, 1, 257, 3]
try:
    import torch

    # io_nodes._convert is the function OCIOWrite.write actually calls (io_nodes.py:2147), so this exercises
    # the production path rather than a lower-level helper that the node never reaches directly.
    out = io_nodes._convert(torch.from_numpy(img.copy()), "ACEScct", "ACEScg")
    got = out.detach().cpu().numpy().astype(np.float64)
    check("the conversion returned a changed tensor, i.e. it was not a silent identity",
          float(np.max(np.abs(got - img.astype(np.float64)))) > 1e-3,
          f"max change {float(np.max(np.abs(got - img.astype(np.float64)))):.4g}")
except Exception as e:                                          # pragma: no cover - reported, never swallowed
    got = None
    check("OCIO could apply ACEScct -> ACEScg", False, f"{type(e).__name__}: {e}")

if got is not None:
    want = ref_decompress(codes.astype(np.float64))
    per_ch = [np.abs(got[0, 0, :, c] - want) for c in range(3)]
    worst = max(float(p.max()) for p in per_ch)
    # Mixed tolerance, stated: relative alone is meaningless where the curve passes through zero (code
    # 0.0729055 decodes to exactly 0), absolute alone is meaningless where the curve reaches ~222.
    ok = all(np.allclose(got[0, 0, :, c], want, rtol=1e-4, atol=1e-5) for c in range(3))
    i = int(np.argmax(per_ch[0]))
    check("OCIO's ACEScct -> ACEScg IS the published decode (rtol 1e-4, atol 1e-5)", ok,
          f"max abs {worst:.3e} at code {codes[i]:.4f} where the reference gives {want[i]:.4f}")
    check("the transform is neutral across channels, i.e. transfer only and no gamut rotation",
          float(np.max(np.abs(got[0, 0, :, 0] - got[0, 0, :, 1]))) < 1e-6
          and float(np.max(np.abs(got[0, 0, :, 1] - got[0, 0, :, 2]))) < 1e-6,
          "a grey ramp stays grey")
    top = float(got[0, 0, -1, 1])
    check("code 1.0 lands on the reference ceiling ~222.86", abs(top - 222.8609442038076) < 0.05,
          f"{top:.4f}")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
