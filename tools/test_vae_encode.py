# -*- coding: utf-8 -*-
"""Prove OCIO VAE Encode behaves, against a stand-in VAE that follows the REAL comfy/sd.py contract.

Run:  python tools/test_vae_encode.py        (no pytest, no ComfyUI, no GPU)

The stand-in below is not a convenience mock - it reproduces the two things about the real encode path
that a careless implementation gets wrong, so that this file can actually fail:

  * sd.py:1333 calls `self.process_input(pixel_samples[x:x + n])` on a SLICE and USES the return value.
    That is the opposite of the decode side (sd.py:1215 discards the return and needs in-place ops).
    Getting the direction backwards is the bug that made this pack emit raw [-1, 1] values.
  * sd.py:1315 calls `vae_encode_crop_pixels`, which silently narrows each spatial dimension down to a
    multiple of the compression ratio.

The test that matters most here is the LAST one: encoding must not modify the caller's own IMAGE tensor,
because in ComfyUI that tensor is frequently still wired to another node.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch

from vae_nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, OCIOVAEEncode

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


class _Model:
    def __init__(self):
        self.dtype = torch.bfloat16

    def to(self, dtype):
        self.dtype = dtype
        return self


class StandInVAE:
    """Mimics comfy.sd.VAE closely enough that a wrong implementation fails here."""

    def __init__(self, compression=8):
        self.compression = compression
        self.vae_dtype = torch.bfloat16
        self.first_stage_model = _Model()
        self.process_input = lambda image: image * 2.0 - 1.0     # sd.py:501, out-of-place, return USED
        self.seen = None                                          # what actually reached the VAE
        # The dtype in force AT ENCODE TIME, recorded rather than assumed. An earlier version of this file
        # built its expected tensor in bfloat16 and failed, because the node's default precision is float32
        # and it raises vae_dtype before encoding - so the assertion was testing the author's memory of the
        # default, not the node. The values are what this file is about; the dtype comes from the run.
        self.dtype_at_encode = None

    def vae_encode_crop_pixels(self, pixels):
        # sd.py:1315 - narrow each spatial dim to a multiple of the compression ratio, silently.
        for d, size in enumerate(pixels.shape[1:-1]):
            keep = (size // self.compression) * self.compression
            if keep != size:
                pixels = pixels.narrow(d + 1, (size % self.compression) // 2, keep)
        return pixels

    def encode(self, pixel_samples):
        self.dtype_at_encode = self.vae_dtype
        pixel_samples = self.vae_encode_crop_pixels(pixel_samples)
        pixel_samples = pixel_samples.movedim(-1, 1)
        out = None
        for x in range(0, pixel_samples.shape[0], 2):            # batched, like sd.py:1332
            pixels_in = self.process_input(pixel_samples[x:x + 2]).to(self.vae_dtype)
            self.seen = pixels_in if out is None else torch.cat([self.seen, pixels_in])
            chunk = pixels_in.mean(dim=(2, 3), keepdim=True).expand(-1, -1, 4, 4).clone()
            out = chunk if out is None else torch.cat([out, chunk])
        return out


def img(h=16, w=16, b=2, fill=None):
    if fill is None:
        # every sample distinct, so an accidental in-place write cannot hide behind equal values
        return torch.arange(b * h * w * 3, dtype=torch.float32).reshape(b, h, w, 3) / (b * h * w * 3)
    return torch.full((b, h, w, 3), float(fill), dtype=torch.float32)


node = OCIOVAEEncode()

print("registration")
check("class is registered", NODE_CLASS_MAPPINGS.get("OCIOVAEEncode") is OCIOVAEEncode)
check("display name is set", NODE_DISPLAY_NAME_MAPPINGS.get("OCIOVAEEncode") == "OCIO VAE Encode")
spec = OCIOVAEEncode.INPUT_TYPES()["required"]
check("out_of_range has THREE states, never two",
      len(spec["out_of_range"][0]) == 3, str(spec["out_of_range"][0]))
check("default is 'report only' (cannot break an existing graph)",
      spec["out_of_range"][1]["default"] == "report only")
# THE LATENT MUST STAY AT SLOT 0. An output link is stored by index, so a report added anywhere but the end
# would re-point every saved graph's wire to a STRING. Asserting the whole tuple, not just its length, so the
# order is pinned as well as the contents.
check("the latent is still output slot 0", OCIOVAEEncode.RETURN_TYPES[0] == "LATENT")
check("the report was APPENDED after it, not inserted",
      OCIOVAEEncode.RETURN_TYPES == ("LATENT", "STRING"), str(OCIOVAEEncode.RETURN_TYPES))
check("and it is named so the wire reads clearly",
      OCIOVAEEncode.RETURN_NAMES == ("latent", "input report"), str(OCIOVAEEncode.RETURN_NAMES))
check("every output has a tooltip", len(OCIOVAEEncode.OUTPUT_TOOLTIPS) == len(OCIOVAEEncode.RETURN_TYPES),
      f"{len(OCIOVAEEncode.OUTPUT_TOOLTIPS)} tooltips for {len(OCIOVAEEncode.RETURN_TYPES)} outputs")

print("\nwidget defaults and Python signature defaults agree, on BOTH nodes")
# These are two separate declarations of the same default and they have drifted before: the widget was
# changed while both signatures still said something else. A call that omits the widget - the API path,
# and any caller that does not send every widget - then behaves differently from the same node in the UI,
# and the difference here is a 5x decode. Checked for both nodes at once, because fixing one and leaving
# the sibling is how this class of bug survives.
import inspect

from vae_nodes import OCIOVAEDecode

for cls, fname in ((OCIOVAEDecode, "decode"), (OCIOVAEEncode, "encode")):
    spec_w = cls.INPUT_TYPES()["required"]["precision"]
    widget_default = spec_w[1]["default"]
    sig_default = inspect.signature(getattr(cls, fname)).parameters["precision"].default
    check(f"{cls.__name__}: widget default == signature default",
          widget_default == sig_default, f"widget {widget_default!r} vs signature {sig_default!r}")
    check(f"{cls.__name__}: the declared default is one of the offered options",
          widget_default in spec_w[0], f"{widget_default!r} in {spec_w[0]}")
    # THIS CHECK IS INVERTED FROM WHAT IT USED TO ASSERT, deliberately (2026-08-13). It required the
    # default NOT to be float32, on the ground that float32 costs several times the model's own dtype for
    # a fraction of a 10-bit code step. That cost is unchanged and still measured; what changed is who
    # pays it by default, so that a colour pipeline states the precision it ran at instead of inheriting
    # whichever dtype the checkpoint's branch of comfy/sd.py happens to list first. The check is kept and
    # turned around rather than deleted, so the default cannot drift back unnoticed.
    check(f"{cls.__name__}: the default is float32, which is the stated choice",
          widget_default == "float32", f"default is {widget_default!r}")
    check(f"{cls.__name__}: 'model default' is no longer offered",
          "model default" not in spec_w[0], str(list(spec_w[0])))
    check(f"{cls.__name__}: the combo is exactly the two named precisions",
          list(spec_w[0]) == ["float32", "float16"], str(list(spec_w[0])))

print("\nthe output is a well-formed latent")
vae = StandInVAE()
out = node.encode(img(), vae)
check("returns a 2-tuple: the latent and the report", isinstance(out, tuple) and len(out) == 2,
      f"got {type(out).__name__} of {len(out) if isinstance(out, tuple) else 0}")
check("the report is a string an artist can read", isinstance(out[1], str) and len(out[1]) > 40,
      repr(out[1])[:80])
check("and it states the input range it was handed", "min=" in out[1] and "max=" in out[1],
      out[1][:90])
check("carries a 'samples' key", isinstance(out[0], dict) and "samples" in out[0])
check("samples is a tensor", torch.is_tensor(out[0]["samples"]))

print("\nout_of_range = 'report only' passes values through untouched, like the stock node")
vae = StandInVAE()
p = img(fill=None) * 3.0 - 1.0                                   # spans well outside 0..1
node.encode(p, vae, out_of_range="report only")
expect = (p.movedim(-1, 1) * 2.0 - 1.0).to(vae.dtype_at_encode)
check("the VAE saw the un-clamped values", torch.equal(vae.seen, expect),
      f"vae saw {float(vae.seen.min()):+.3f}..{float(vae.seen.max()):.3f} as {vae.seen.dtype}")

print("\nout_of_range = 'clamp to 0..1' clamps BEFORE the VAE")
vae = StandInVAE()
node.encode(p.clone(), vae, out_of_range="clamp to 0..1")
check("the VAE saw nothing outside [-1, 1]",
      float(vae.seen.min()) >= -1.0 and float(vae.seen.max()) <= 1.0,
      f"vae saw {float(vae.seen.min()):+.3f}..{float(vae.seen.max()):.3f}")

print("\nout_of_range = 'raise an error' actually raises, and only when it should")
vae = StandInVAE()
try:
    node.encode(p.clone(), vae, out_of_range="raise an error")
    check("raises on out-of-range input", False, "no exception")
except ValueError as e:
    check("raises on out-of-range input", True, str(e)[:66] + "...")
vae = StandInVAE()
try:
    node.encode(img(), vae, out_of_range="raise an error")        # already inside 0..1
    check("does NOT raise on legal input", True)
except ValueError as e:
    check("does NOT raise on legal input", False, str(e)[:60])

print("\nthe caller's IMAGE tensor is never modified (the in-place trap, from the opposite direction)")
for mode in ("report only", "clamp to 0..1"):
    vae = StandInVAE()
    p = img(fill=None) * 3.0 - 1.0
    before = p.clone()
    node.encode(p, vae, out_of_range=mode)
    check(f"input unchanged after '{mode}'", torch.equal(p, before),
          "" if torch.equal(p, before) else f"{int((p != before).sum())} samples were overwritten")

print("\nprocess_input is left alone - the node does not monkey-patch it")
vae = StandInVAE()
fn = vae.process_input
node.encode(img(), vae)
check("process_input is the same object afterwards", vae.process_input is fn)

print("\nprecision is raised for the encode and restored afterwards")
vae = StandInVAE()
node.encode(img(), vae, precision="float32")
check("vae_dtype restored to bfloat16", vae.vae_dtype == torch.bfloat16, str(vae.vae_dtype))
check("model dtype restored to bfloat16", vae.first_stage_model.dtype == torch.bfloat16)
# WHAT REPLACED 'model default'. That option used to be the way to say "leave the VAE's own dtype in
# force", and these two checks proved it did. It is no longer offered, so the property is now reached by
# asking for a precision the VAE does not list: the request is declined and the model's own dtype runs.
# The checks are kept in that form rather than deleted, because the guarantee they protect - that the
# node never silently changes the dtype a decode ran at - is unchanged.
vae = StandInVAE()
vae.working_dtypes = [torch.bfloat16]                    # offers neither float32 nor float16
node.encode(img(), vae, precision="float32")
check("a declined precision leaves vae_dtype alone", vae.vae_dtype == torch.bfloat16,
      str(vae.vae_dtype))


rec = StandInVAE()
node.encode(img(), rec, precision="float32")
check("float32 was actually in force DURING the encode", rec.dtype_at_encode == torch.float32,
      str(rec.dtype_at_encode))
rec = StandInVAE()
rec.working_dtypes = [torch.bfloat16]
node.encode(img(), rec, precision="float32")
check("a declined precision really leaves bfloat16 in force during the encode",
      rec.dtype_at_encode == torch.bfloat16, str(rec.dtype_at_encode))

print("\nprecision is restored even when the encode raises")


class Exploding(StandInVAE):
    def encode(self, pixel_samples):
        raise RuntimeError("boom")


vae = Exploding()
try:
    node.encode(img(), vae, precision="float32")
except RuntimeError:
    pass
check("vae_dtype restored after an exception", vae.vae_dtype == torch.bfloat16, str(vae.vae_dtype))
check("model dtype restored after an exception", vae.first_stage_model.dtype == torch.bfloat16)

print("\nA FAILED CAST MUST NOT LEAVE THE VAE CLAIMING A PRECISION IT DOES NOT HAVE")
# The exception above happens inside encode(), AFTER the cast succeeded, so the restore flag is already set and
# the `finally` runs. The case that leaked is narrower and was invisible to that test: the CAST ITSELF raising.
# The old code assigned vae_dtype before calling .to() and set the restore flag after, so a refused cast left
# vae_dtype = float32 over bfloat16 weights with the flag still False, and the finally skipped the restore. The
# VAE then stayed inconsistent for every later graph in the same session. Quantised int8 / fp8 weights refuse the
# cast for real; this stand-in refuses it on purpose.


class RefusesTheCast(StandInVAE):
    class _Model:
        dtype = torch.bfloat16

        def to(self, dtype):
            if dtype == torch.float32:
                raise RuntimeError("quantised weights cannot be cast")
            self.dtype = dtype
            return self

    def __init__(self, **kw):
        super().__init__(**kw)
        self.first_stage_model = RefusesTheCast._Model()


vae = RefusesTheCast()
# The call is wrapped so a regression reports the LEAK BY NAME instead of taking the file down with a traceback.
# Mutation testing found that difference: reverting the fix made this section red either way, but an uncaught
# RuntimeError says only "something broke", while the checks below say which invariant went and what it went to.
out, raised = None, None
try:
    out = node.encode(img(), vae, precision="float32")
except Exception as e:
    raised = f"{type(e).__name__}: {e}"
check("the render still completes rather than failing over a preference",
      raised is None and out is not None and torch.is_tensor(out[0]["samples"]),
      f"raised {raised}" if raised else "")
check("vae_dtype is NOT left claiming float32 after a refused cast",
      vae.vae_dtype == torch.bfloat16, f"left as {vae.vae_dtype}")
check("and the weights are still their own dtype", vae.first_stage_model.dtype == torch.bfloat16)

print("\nfloat32 is declined, not attempted, on a VAE that does not list it")


class NoFloat32(StandInVAE):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.working_dtypes = [torch.bfloat16, torch.float16]


vae = NoFloat32()
out = node.encode(img(), vae, precision="float32")
check("the encode completes at the VAE's own precision", torch.is_tensor(out[0]["samples"]))
check("vae_dtype untouched when float32 is not in working_dtypes",
      vae.vae_dtype == torch.bfloat16, str(vae.vae_dtype))
check("and it really did encode at that precision, not at float32",
      vae.dtype_at_encode == torch.bfloat16, str(vae.dtype_at_encode))

print("\nthe silent crop is detected (16x16 with compression 12 -> 12x12)")
vae = StandInVAE(compression=12)
out = node.encode(img(h=16, w=16), vae)
check("encode still succeeds on a croppable size", torch.is_tensor(out[0]["samples"]))
check("the crop really happened in the stand-in",
      vae.seen.shape[-1] == 12 and vae.seen.shape[-2] == 12, str(tuple(vae.seen.shape)))

print("\nan already-aligned size is not cropped")
vae = StandInVAE(compression=8)
node.encode(img(h=16, w=16), vae)
check("16x16 with compression 8 is untouched",
      vae.seen.shape[-1] == 16 and vae.seen.shape[-2] == 16, str(tuple(vae.seen.shape)))

# EVERY OFFERED PRECISION MUST ACTUALLY DO SOMETHING, ON THIS NODE TOO (2026-08-13).
#
# The equivalent block lives in tools/test_vae_decode_transforms.py, and the duplication is deliberate:
# the precision widget is declared twice, the two implementations drifted for months, and this node was
# the one left without guards. Covering one and trusting the sibling is how that survived.
from vae_nodes import _PRECISION_DTYPES  # noqa: E402


class ListsFloat16(StandInVAE):
    """A VAE that offers float16, as 15 of the 23 working_dtypes lists in comfy/sd.py do."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.working_dtypes = [torch.bfloat16, torch.float16, torch.float32]


class NoFloat16(StandInVAE):
    """The LTX profile: comfy/sd.py:602 is exactly [bfloat16, float32]. Eight lists omit float16."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.working_dtypes = [torch.bfloat16, torch.float32]


print("\nevery value the encode combo offers is wired, and reaches the encode")
offered = OCIOVAEEncode.INPUT_TYPES()["required"]["precision"][0]
dec_offered = OCIOVAEDecode.INPUT_TYPES()["required"]["precision"][0]
check("the combo is generated from the dispatch mapping, so the two cannot disagree",
      list(offered) == list(_PRECISION_DTYPES), f"{list(offered)} vs {list(_PRECISION_DTYPES)}")
check("float16 is on offer", "float16" in offered, str(offered))
check("both nodes offer the SAME precisions - one widget, declared twice",
      list(offered) == list(dec_offered), f"encode {list(offered)} vs decode {list(dec_offered)}")

for name in offered:
    want = _PRECISION_DTYPES[name]
    vae = ListsFloat16()
    node.encode(img(), vae, precision=name)
    expected = want          # every entry names a real dtype now; there is no pass-through option
    check(f"precision={name!r} was really in force DURING the encode",
          vae.dtype_at_encode == expected, f"{vae.dtype_at_encode}, wanted {expected}")
    check(f"precision={name!r} restored the VAE afterwards",
          vae.vae_dtype == torch.bfloat16 and vae.first_stage_model.dtype == torch.bfloat16,
          f"vae_dtype {vae.vae_dtype}, weights {vae.first_stage_model.dtype}")

print("\nfloat16 is declined, not forced, on a VAE that does not list it (the LTX profile)")
vae = NoFloat16()
_lat, rep = node.encode(img(), vae, precision="float16")
check("the encode completes at the VAE's own precision", vae.dtype_at_encode == torch.bfloat16,
      str(vae.dtype_at_encode))
check("vae_dtype untouched when float16 is not in working_dtypes",
      vae.vae_dtype == torch.bfloat16, str(vae.vae_dtype))
check("float16 declined appears in the input report, not only in the server log",
      "float16 declined" in rep, rep[-200:])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
