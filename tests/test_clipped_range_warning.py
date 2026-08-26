"""Does the clipped-range warning fire on the real case and stay quiet on honest work?

A warning that cries on correct operations is worse than none, because it teaches people to ignore it. The first
version of this detector compared output maximum against input maximum and therefore fired on a LOG ENCODE, which
compresses 15.37 down to about 0.5 by design. This checks the replacement, which tests whether distinct values
COLLAPSED onto one rather than whether the range shrank.
"""
import importlib.util
import io
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
spec = importlib.util.spec_from_file_location("p.nodes", os.path.join(ROOT, "nodes.py"))
N = importlib.util.module_from_spec(spec); sys.modules["p.nodes"] = N; spec.loader.exec_module(N)

import torch

buf = io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(buf))
logging.getLogger().setLevel(logging.WARNING)
FAILS = []


def run(label, fn, x):
    buf.truncate(0); buf.seek(0)
    N._dual_io(fn, x, None, label=label)
    return buf.getvalue().strip()


def expect(name, warned_wanted, label, fn, x):
    got = run(label, fn, x)
    ok = bool(got) == warned_wanted
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   {'warned: ' + got[:80] if got else 'stayed silent'}"))
    if not ok:
        FAILS.append(name)


# distinct values above white and below black, so a collapse is unmistakable
hdr = torch.zeros(1, 2, 4, 3)
hdr[0, 0, :, 0] = torch.tensor([15.37, 8.0, 3.0, 1.5])
hdr[..., 1] = 0.5
hdr[0, 1, :, 2] = torch.tensor([-0.06, -0.2, -0.5, -1.0])
sdr = torch.full((1, 2, 2, 3), 0.5)

# THE WIRING IS PART OF THE CONTRACT, so it is asserted rather than assumed. Measurement decided which nodes get
# the label: ColorSpace and CDLTransform never collapse anything, and Display collapses negatives to 0 because
# that is what a display transform does, so a warning there would fire on every correct use.
import re as _re
_src = pathlib.Path(__file__).parent.parent.joinpath("nodes.py").read_text(encoding="utf-8")
_wired = set(_re.findall(r'label="(OCIO [A-Za-z]+)"', _src))
for want in ("OCIO FileTransform", "OCIO LookTransform", "OCIO LogConvert"):
    ok = want in _wired
    print(f"  {'PASS' if ok else 'FAIL'}  {want} is wired to the warning")
    if not ok:
        FAILS.append(f"{want} not wired")
for never in ("OCIO ColorSpace", "OCIO CDLTransform", "OCIO Display"):
    ok = never not in _wired
    print(f"  {'PASS' if ok else 'FAIL'}  {never} is NOT wired (it would be dead or always-true)")
    if not ok:
        FAILS.append(f"{never} wrongly wired")

print("\nit MUST warn when out-of-range values collapse onto one")
expect("a hard clamp on HDR data", True, "OCIO FileTransform", lambda i: i.clamp(0, 1), hdr)
expect("a clamp at the LUT's corner value", True, "OCIO FileTransform", lambda i: i.clamp(0, 1.05), hdr)
expect("a clamp that only kills the highlights", True, "OCIO LookTransform",
       lambda i: torch.clamp(i, min=float(i.min()), max=1.0), hdr)

print("\nand it MUST stay silent on work that is doing its job")
# THE PACK'S REAL CURVES, not a hand-rolled stand-in. The first version of this case used
# `i.clamp(min=1e-6).log10()`, which genuinely DOES clip every negative onto one value, so the detector was right
# to warn and the expectation was wrong. A real camera log curve has a linear segment through zero and keeps
# negatives separated, which is exactly the distinction the detector is built on.
# CALLED THROUGH THE NODE, not wrapped in another _dual_io. Wrapping meant the node's own call ran with the
# specification flag set and stayed silent, while the outer wrapper ran again without it and warned. The warning
# was real and it was the harness talking to itself.
for curve in ("ACEScct", "ACEScc", "ARRI LogC3", "Sony S-Log3", "Cineon"):
    buf.truncate(0); buf.seek(0)
    N.OCIOLogConvert().run(image=hdr, operation="Linear to Log", curve=curve, mix=1.0)
    got = buf.getvalue().strip()
    ok = not got
    print(f"  {'PASS' if ok else 'FAIL'}  a real {curve} encode stays silent"
          + ("" if ok else f"   warned: {got[:80]}"))
    if not ok:
        FAILS.append(f"{curve} encode")
expect("a tone map that keeps values separated", False, "OCIO Display", lambda i: i / (1.0 + i), hdr)
expect("the identity", False, "OCIO ColorSpace", lambda i: i, hdr)
expect("an exposure grade", False, "OCIO CDLTransform", lambda i: i * 0.98, hdr)
expect("a clamp on data with no range to lose", False, "OCIO Display", lambda i: i.clamp(0, 1), sdr)
expect("a gamma, which is monotonic", False, "OCIO ColorSpace",
       lambda i: torch.sign(i) * torch.abs(i) ** (1 / 2.2), hdr)

print("\nedge cases must not raise, and must not warn on nothing")
expect("a single out-of-range sample cannot 'collapse'", False, "OCIO ColorSpace",
       lambda i: i.clamp(0, 1), torch.tensor([[[[2.0, 0.5, 0.5]]]]))
expect("an all-in-range image", False, "OCIO ColorSpace", lambda i: i.clamp(0, 1),
       torch.full((1, 2, 2, 3), 0.25))
for name, fn in (("a transform that returns a different shape", lambda i: i[:, :1, :1, :]),
                 ("a transform that raises inside the report path", lambda i: i * float("nan"))):
    try:
        run("OCIO ColorSpace", fn, hdr)
        print(f"  PASS  {name} does not take the render down")
    except Exception as e:
        print(f"  FAIL  {name} raised {type(e).__name__}: {e}")
        FAILS.append(name)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("the detector fires on clipping only")
