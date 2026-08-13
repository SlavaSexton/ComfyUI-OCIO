# -*- coding: utf-8 -*-
"""OCIO VAE Decode must ask the VAE what its output transform is, never assume the default.

Run:  python tools/test_vae_decode_transforms.py     (no pytest, no ComfyUI, no GPU)

WHY THIS FILE EXISTS. The node used to replace `vae.process_output` unconditionally with `(x+1)/2`. Five
places in comfy/sd.py set that function to the IDENTITY instead, because those decoders already emit [0,1]:
lines 540, 894/895, 906 and 976. Line 894 is TAEHV for `latent_channels in [48, 128]` - Wan 2.2 and **LTX2**,
the fast preview decoder for the very model this pack was built around.

On those VAEs the node applied `(x+1)/2` to data already in [0,1] and produced [0.5, 1.0]: a washed-out,
wrong image, with no error. BOTH branches did it, the clamped one included, so the node's own claim that
"clamp ON reproduces the stock decode exactly" was false there as well.

The existing tools/test_vae_decode.py could not see this, because its stand-in VAE only ever used the
default transform. So this file supplies the transforms that actually occur, and checks the pixels rather
than the settings.

Found by Andrei Orehov on `taeltx2_3`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch

from vae_nodes import (OCIOVAEDecode, _DEFAULT_SHAPE, _IDENTITY_SHAPE, _PRECISION_DTYPES,
                       _probe_process_output)

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


class _Model:
    def __init__(self, dtype=torch.bfloat16, castable=True):
        self.dtype = dtype
        self.castable = castable

    def to(self, dtype):
        if not self.castable:
            raise RuntimeError("quantised weights cannot be re-cast")   # int8 / fp8 in the real world
        self.dtype = dtype
        return self


class StandInVAE:
    """Mimics comfy.sd.VAE for the parts this node touches, with a choosable output transform."""

    def __init__(self, transform="default", working_dtypes=(torch.bfloat16, torch.float32), castable=True):
        self.vae_dtype = torch.bfloat16
        self.first_stage_model = _Model(castable=castable)
        self.working_dtypes = list(working_dtypes)
        self.raw = None                       # what the decoder produced, before process_output
        if transform == "default":            # comfy/sd.py:502
            self.process_output = lambda image: image.add_(1.0).div_(2.0).clamp_(0.0, 1.0)
        elif transform == "identity":         # comfy/sd.py:894 / 906 / 976 / 540 - TAEHV and friends
            self.process_output = lambda image: image
        elif transform == "weird":            # something this node has never seen
            self.process_output = lambda image: image.mul_(3.0).sub_(7.0)
        else:
            raise ValueError(transform)

    def decode(self, latent):
        # sd.py:1215 calls process_output on a slice and DISCARDS the return, so the transform must mutate.
        # The dtype is recorded AS THE DECODE RUNS. Reading it afterwards cannot tell a precision that
        # was really in force from one that was set and restored without ever being used, and "was it
        # actually in force" is the only question a precision widget has to answer.
        self.dtype_at_decode = self.vae_dtype
        self.model_dtype_at_decode = self.first_stage_model.dtype
        out = self.raw.clone()
        self.process_output(out)
        return out


def latent():
    return {"samples": torch.zeros(1, 4, 8, 8)}


node = OCIOVAEDecode()

print("the probe recognises the transforms that actually occur, and refuses the ones it does not")
check("the sd.py:502 default is recognised", _probe_process_output(StandInVAE("default")) == _DEFAULT_SHAPE)
check("the identity (TAEHV, sd.py:894) is recognised",
      _probe_process_output(StandInVAE("identity")) == _IDENTITY_SHAPE)
check("an unfamiliar transform returns None rather than a guess",
      _probe_process_output(StandInVAE("weird")) is None)


class NoProbe(StandInVAE):
    def __init__(self):
        super().__init__("default")
        self.process_output = lambda image: (_ for _ in ()).throw(RuntimeError("boom"))


check("a process_output that raises returns None, not an exception", _probe_process_output(NoProbe()) is None)

print("\nTHE BUG: a VAE that already emits 0..1 must come out UNCHANGED")
# Data a TAEHV-style decoder produces: already in [0,1], with a little over- and undershoot of its own.
native = torch.tensor([[[[-0.101, 0.0, 0.25, 0.5, 0.75, 0.926]]]]).permute(0, 2, 3, 1).contiguous()
for clamp in (False, True):
    vae = StandInVAE("identity")
    vae.raw = native.clone()
    out = node.decode(latent(), vae, precision="float32", clamp=clamp)[0]
    same = torch.allclose(out, native, atol=1e-6)
    check(f"identity VAE, clamp={clamp}: pixels unchanged", same,
          f"in {native.flatten().tolist()} -> out {[round(v, 4) for v in out.flatten().tolist()]}")
    # The specific damage the old code did: everything rescaled into the top half.
    check(f"identity VAE, clamp={clamp}: NOT rescaled into 0.5..1.0",
          not (float(out.min()) >= 0.49 and float(out.max()) <= 1.01 and not same))

print("\nan unrecognised transform is also left alone")
vae = StandInVAE("weird")
vae.raw = native.clone()
out = node.decode(latent(), vae, precision="float32", clamp=False)[0]
check("weird VAE: the VAE's own transform still applied, ours not substituted",
      torch.allclose(out, native * 3.0 - 7.0, atol=1e-5),
      f"out {[round(v, 3) for v in out.flatten().tolist()[:4]]}")

print("\nthe default transform still behaves as before - the fix must not change the case that worked")
raw = torch.tensor([[[[-1.2, -1.0, 0.0, 1.0, 1.2]]]]).permute(0, 2, 3, 1).contiguous()
vae = StandInVAE("default")
vae.raw = raw.clone()
un = node.decode(latent(), vae, precision="float32", clamp=False)[0]
check("clamp OFF keeps values outside 0..1", float(un.min()) < 0.0 and float(un.max()) > 1.0,
      f"{float(un.min()):+.3f}..{float(un.max()):.3f}")
vae = StandInVAE("default")
vae.raw = raw.clone()
cl = node.decode(latent(), vae, precision="float32", clamp=True)[0]
check("clamp ON reproduces the stock range", float(cl.min()) == 0.0 and float(cl.max()) == 1.0,
      f"{float(cl.min()):+.3f}..{float(cl.max()):.3f}")
check("clamp ON equals clip(clamp OFF)", torch.allclose(cl, un.clamp(0, 1), atol=1e-6))

print("\nprocess_output is restored afterwards, whatever branch was taken")
for transform in ("default", "identity", "weird"):
    vae = StandInVAE(transform)
    vae.raw = native.clone()
    before = vae.process_output
    node.decode(latent(), vae, precision="float32", clamp=False)
    check(f"{transform}: process_output restored", vae.process_output is before)

print("\nfloat32 is declined, not forced, when the VAE cannot take it")
vae = StandInVAE("default", working_dtypes=(torch.bfloat16,))          # no float32 offered
vae.raw = raw.clone()
node.decode(latent(), vae, precision="float32", clamp=False)
check("a VAE without float32 in working_dtypes stays at its own dtype",
      vae.vae_dtype == torch.bfloat16 and vae.first_stage_model.dtype == torch.bfloat16,
      f"vae_dtype {vae.vae_dtype}, model {vae.first_stage_model.dtype}")

vae = StandInVAE("default", castable=False)                            # quantised weights refuse the cast
vae.raw = raw.clone()
try:
    node.decode(latent(), vae, precision="float32", clamp=False)
    check("quantised weights that refuse the cast do not fail the render", True)
except Exception as e:
    check("quantised weights that refuse the cast do not fail the render", False, f"{type(e).__name__}: {e}")
check("and the dtype is left as it was", vae.vae_dtype == torch.bfloat16, str(vae.vae_dtype))

vae = StandInVAE("default")
vae.raw = raw.clone()
node.decode(latent(), vae, precision="float32", clamp=False)
check("a VAE that CAN take float32 is restored afterwards",
      vae.vae_dtype == torch.bfloat16 and vae.first_stage_model.dtype == torch.bfloat16)

# EVERY OFFERED PRECISION MUST ACTUALLY DO SOMETHING (2026-08-13).
#
# The combo used to be a hand-written list while the dispatch compared `precision == "float32"` to one
# literal string, so a value added to the list selected NOTHING: no cast, no note, no error, the choice
# silently dropped. That is the defect this block exists to make impossible to reintroduce, and a
# structural check alone will not do it - counting names proves the list, not the wiring. So each
# offered name is EXERCISED and the dtype is read from inside the decode.
print("\nevery value the combo offers is wired to a real dtype, and reaches the decode")
offered = OCIOVAEDecode.INPUT_TYPES()["required"]["precision"][0]
check("the combo is generated from the dispatch mapping, so the two cannot disagree",
      list(offered) == list(_PRECISION_DTYPES), f"{list(offered)} vs {list(_PRECISION_DTYPES)}")
check("float16 is on offer", "float16" in offered, str(offered))

for name in offered:
    want = _PRECISION_DTYPES[name]
    # working_dtypes lists everything, so nothing here is declined for the wrong reason
    vae = StandInVAE("default", working_dtypes=(torch.bfloat16, torch.float16, torch.float32))
    vae.raw = raw.clone()
    node.decode(latent(), vae, precision=name, clamp=False)
    expected = want          # every entry names a real dtype now; there is no pass-through option
    check(f"precision={name!r} was really in force DURING the decode",
          vae.dtype_at_decode == expected and vae.model_dtype_at_decode == expected,
          f"vae_dtype {vae.dtype_at_decode}, weights {vae.model_dtype_at_decode}, wanted {expected}")
    check(f"precision={name!r} restored the VAE afterwards",
          vae.vae_dtype == torch.bfloat16 and vae.first_stage_model.dtype == torch.bfloat16,
          f"vae_dtype {vae.vae_dtype}, weights {vae.first_stage_model.dtype}")

print("\nfloat16 is declined, not forced, on a VAE that does not list it (the LTX profile)")
# comfy/sd.py:602 - the LTX-2.5 video VAE tested against here - is exactly [bfloat16, float32]. Eight of the 23
# working_dtypes lists in that file omit float16, so this is the common case, not a corner.
vae = StandInVAE("default", working_dtypes=(torch.bfloat16, torch.float32))
vae.raw = raw.clone()
_img, rep = node.decode(latent(), vae, precision="float16", clamp=False)
check("a VAE without float16 stays at its own dtype through the decode",
      vae.dtype_at_decode == torch.bfloat16 and vae.model_dtype_at_decode == torch.bfloat16,
      f"vae_dtype {vae.dtype_at_decode}, weights {vae.model_dtype_at_decode}")
check("float16 declined appears in the report, not only in the server log",
      "float16 declined" in rep, rep[-220:])
check("the decline names working_dtypes so the reason is actionable",
      "working_dtypes" in rep, rep[-220:])

print("\nfloat16 falls back rather than failing when quantised weights refuse the cast")
vae = StandInVAE("default", working_dtypes=(torch.bfloat16, torch.float16, torch.float32), castable=False)
vae.raw = raw.clone()
try:
    _img, rep = node.decode(latent(), vae, precision="float16", clamp=False)
    check("a refused float16 cast does not fail the render", True)
    check("and it says so on the wire", "float16 cast refused" in rep, rep[-200:])
except Exception as e:
    check("a refused float16 cast does not fail the render", False, f"{type(e).__name__}: {e}")
check("a refused float16 cast leaves the dtype flag alone",
      vae.vae_dtype == torch.bfloat16, str(vae.vae_dtype))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
