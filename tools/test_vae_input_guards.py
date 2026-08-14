# -*- coding: utf-8 -*-
"""Prove the OCIO VAE nodes refuse an incompatible input in words, instead of crashing inside the model.

Run:  python tools/test_vae_input_guards.py        (no pytest, no ComfyUI, no GPU)

TWO INPUTS, BOTH CONFIRMED TO FAIL ON THE STOCK NODES TOO. Neither guard fixes any arithmetic, and
these checks would be dishonest if they implied one did. What is being tested is that the artist is
told which number is wrong, rather than being handed an error about an internal axis:

  * a frame whose height or width is not a multiple of the VAE's block size, on a VAE that declines
    ComfyUI's crop (comfy/sd.py:1069-1078 runs only `if self.crop_input`, and sd.py:575, :595, :884
    switch it off). It reaches the model and dies in einops.
  * a 4-dimensional latent handed to a 5-dimensional video VAE. It dies in the memory estimate
    before any decoding starts, because those lambdas index shape[4] (sd.py:596).

THE STAND-INS REPRODUCE THOSE TWO FAILURES FOR REAL rather than describing them. The encode one runs
the actual einops pattern from the reported traceback, and the decode one runs the actual lambda
copied from sd.py:596. Both are exercised directly, below, so this file can prove its own premise:
if the stand-ins stopped failing on the bad input, the guards would be guarding nothing and these
checks would still pass. That is the failure mode this section exists to close.

THE THIRD AND FOURTH GROUPS MATTER MOST. A guard that refuses work which would have succeeded is
worse than the unhelpful error it replaces, so: a VAE that DOES crop must never be refused, and a
VAE that will not say what it needs must be let through in silence.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import einops
import torch

from vae_nodes import (OCIOVAEDecode, OCIOVAEEncode, _decode_shape_problem,
                       _encode_alignment_problem, _spatial_alignment)

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


class LTXStandInVAE:
    """The LTX-2.5 encode profile, field for field out of comfy/sd.py.

    `crop_input = False` is the whole point (sd.py:595, whose own comment gives the reason: a generic
    crop would narrow the FRAME axis by the 32x spatial ratio). With it off, `vae_encode_crop_pixels`
    is a pass-through and an unaligned frame reaches the model intact.
    """

    def __init__(self, align=32, crop=False, latent_dim=3):
        # sd.py:600 - the callable maps a frame count, the two ints are the spatial ratio.
        self.downscale_ratio = (lambda a: max(0, math.floor((a + 7) / 8)), align, align)
        self.downscale_index_formula = (8, align, align)      # sd.py:601
        self.crop_input = crop                                # sd.py:595
        self.latent_dim = latent_dim                          # sd.py:593
        self.extra_1d_channel = None                          # sd.py:510
        self.vae_dtype = torch.bfloat16
        self.working_dtypes = [torch.bfloat16, torch.float32]  # sd.py:602
        self.first_stage_model = _Model()
        self.process_input = lambda image: image * 2.0 - 1.0   # sd.py:501
        self.encode_called = False

    def spacial_compression_encode(self):
        # sd.py:1432, behaviour copied rather than paraphrased: the subscript answers for the tuple
        # form, the fallback answers for a bare int.
        try:
            return self.downscale_ratio[-1]
        except Exception:
            return self.downscale_ratio

    def vae_encode_crop_pixels(self, pixels):
        # sd.py:1069-1078, including the `if self.crop_input` that decides whether anything happens.
        if self.crop_input:
            r = self.spacial_compression_encode()
            dims = pixels.shape[1:-1]
            for d in range(len(dims)):
                keep = (dims[d] // r) * r
                if keep != dims[d]:
                    pixels = pixels.narrow(d + 1, (dims[d] % r) // 2, keep)
        return pixels

    def encode(self, pixels):
        """The real failure, not a raise of a chosen message.

        sd.py:1318-1322 gives the video branch a frame axis, then the model patchifies. The pattern
        string is the one from the reported traceback; einops refuses an axis it cannot halve.

        THE CROP IS THE FIRST LINE HERE because it is the first line of the real one (sd.py:1315),
        and leaving it out made this stand-in lie. The node hands `vae.encode` the caller's ORIGINAL
        tensor and lets the VAE crop it, which is correct and matches sd.py; a stand-in that skipped
        the crop turned a legal input into an einops crash and blamed the node for it.
        """
        self.encode_called = True
        pixels = self.vae_encode_crop_pixels(pixels)
        x = pixels.movedim(-1, 1).unsqueeze(2)          # [B, C, 1, H, W]
        x = x[:, :, :, ::4, ::4]                        # the model's own first 4x downscale
        return einops.rearrange(x, "b c (d p1) (h p2) (w p3) -> b (c p1 p2 p3) d h w",
                                p1=1, p2=2, p3=2)


class VideoStandInVAE:
    """The video decode profile: 5-dimensional latents, and a memory estimate that reads shape[4]."""

    def __init__(self, latent_dim=3):
        self.latent_dim = latent_dim                          # sd.py:593
        self.extra_1d_channel = None
        self.vae_dtype = torch.bfloat16
        self.working_dtypes = [torch.bfloat16, torch.float32]
        self.first_stage_model = _Model()
        self.process_output = lambda image: image.add_(1.0).div_(2.0).clamp_(0.0, 1.0)   # sd.py:502
        # sd.py:596 verbatim, with dtype_size folded to 2 for bfloat16.
        self.memory_used_decode = lambda shape, dtype: (1700 * shape[2] * shape[3] * shape[4] * (8 * 8 * 8)) * 2
        self.decode_called = False

    def decode(self, latent):
        self.decode_called = True
        self.memory_used_decode(latent.shape, self.vae_dtype)      # sd.py:1192, before anything else
        b, _c, t, h, w = latent.shape
        out = torch.zeros(b, t, h * 32, w * 32, 3)
        self.process_output(out)                                   # sd.py:1215 discards the return
        return out


def frame(h, w, b=1):
    return torch.rand(b, h, w, 3)


enc, dec = OCIOVAEEncode(), OCIOVAEDecode()

# =================================================================================================
print("the stand-ins really do fail the way the report says - otherwise nothing below means anything")
# =================================================================================================
# Called directly, bypassing the node, so these assert the PREMISE of every check that follows: the
# bad inputs are genuinely bad. Without this the guards could be refusing inputs that always worked.
raw = LTXStandInVAE()
try:
    raw.encode(frame(100, 96))          # 100 % 32 = 4; 100/4 = 25, which einops cannot halve
    check("an unaligned frame really breaks the stand-in encode", False, "no exception")
except Exception as e:
    check("an unaligned frame really breaks the stand-in encode",
          type(e).__name__ == "EinopsError", f"{type(e).__name__}: {str(e)[:60]}")
raw = LTXStandInVAE()
out = raw.encode(frame(96, 96))         # 96 % 32 = 0
check("and an aligned frame goes through it cleanly", torch.is_tensor(out), str(tuple(out.shape)))

raw = VideoStandInVAE()
try:
    raw.decode(torch.zeros(1, 4, 12, 16))         # 4-D: the estimate reaches shape[4]
    check("a 4-D latent really breaks the stand-in decode", False, "no exception")
except IndexError as e:
    check("a 4-D latent really breaks the stand-in decode", "tuple index out of range" in str(e),
          f"IndexError: {e}")
raw = VideoStandInVAE()
check("and a 5-D latent goes through it cleanly",
      torch.is_tensor(raw.decode(torch.zeros(1, 128, 2, 4, 6))))

# =================================================================================================
print("\n(a) a height that is not a multiple of the VAE's block is refused, by name and by number")
# =================================================================================================
vae = LTXStandInVAE()
raised = None
try:
    enc.encode(frame(100, 96), vae)
except ValueError as e:
    raised = str(e)
check("the encode is refused with a ValueError", raised is not None,
      "" if raised else "no exception - the frame went to the VAE")
check("the VAE was never called, so nothing was half-done", vae.encode_called is False)
check("and its dtype was never touched", vae.vae_dtype == torch.bfloat16 and
      vae.first_stage_model.dtype == torch.bfloat16)
if raised:
    check("it names the offending axis", "height" in raised, raised[:70])
    check("it quotes the size that was fed", "100" in raised)
    check("it quotes the block size required", "32" in raised)
    check("it offers both nearest legal sizes", "96 and 128" in raised,
          [w for w in raised.split() if w.strip(",;.") in ("96", "128")])
    check("it says what to do about it", "resize" in raised.lower() and "crop" in raised.lower())
    check("it does not blame this node for the arithmetic", "not a limit of this node" in raised)
    check("it says the stock node fails the same way", "stock VAE Encode" in raised)
    print(f"\n----- the message an artist sees -----\n{raised}\n-------------------------------------")

print("\nthe real reported plate, 4608x3164 on a 32-block VAE")
msg = _encode_alignment_problem(LTXStandInVAE(), 3164, 4608)
check("it is refused", msg is not None)
check("it names the height and not the width, because only the height is wrong",
      msg is not None and "height 3164" in msg and "width" not in msg, (msg or "")[:120])
check("it offers 3136 and 3168, the multiples of 32 either side",
      msg is not None and "3136 and 3168" in msg)
check("it shows the arithmetic: 3164 = 32 x 98 + 28",
      msg is not None and "32 x 98 + 28" in msg)

# =================================================================================================
print("\n(b) a width that is not a multiple of the block is refused too, and both at once")
# =================================================================================================
vae = LTXStandInVAE()
raised = None
try:
    enc.encode(frame(96, 100), vae)
except ValueError as e:
    raised = str(e)
check("the encode is refused", raised is not None)
check("the VAE was never called", vae.encode_called is False)
if raised:
    check("it names the width", "width 100" in raised, raised[:70])
    check("it does not accuse the height, which is fine", "height" not in raised)

msg = _encode_alignment_problem(LTXStandInVAE(), 100, 140)
check("both axes wrong: both are named", msg is not None and "height 100" in msg and "width 140" in msg,
      (msg or "")[:130])
check("with the nearest sizes for each", msg is not None and "96 and 128" in msg and "128 and 160" in msg)

print("\na frame smaller than one block is not offered a floor of zero")
msg = _encode_alignment_problem(LTXStandInVAE(), 20, 96)
check("only the ceiling is offered", msg is not None and "nearest height it accepts is 32" in msg,
      (msg or "")[:130])
check("and 0 is never offered as one", msg is not None
      and "accepts is 0" not in msg and "accepts are 0" not in msg)

# =================================================================================================
print("\n(c) an aligned frame is NOT refused - the guard stays out of the way")
# =================================================================================================
vae = LTXStandInVAE()
out = enc.encode(frame(96, 96), vae)
check("the encode runs", isinstance(out, tuple) and torch.is_tensor(out[0]["samples"]))
check("the VAE really was called", vae.encode_called is True)
check("no alignment problem was found", _encode_alignment_problem(vae, 96, 96) is None)
for h, w in ((32, 32), (64, 128), (320, 1280), (704, 1280)):
    check(f"{w}x{h} passes", _encode_alignment_problem(LTXStandInVAE(), h, w) is None)

print("\nA VAE THAT CROPS IS NEVER REFUSED, however odd the size it is handed")
# The expensive mistake this guard could make. sd.py:511 sets crop_input True by default, so most
# VAEs narrow the frame themselves and go on to encode perfectly well. Refusing those would break
# graphs that work today. The guard reads the size AFTER the crop, which is why it cannot.
for h, w in ((100, 96), (96, 100), (3164, 4608), (37, 41)):
    vae = LTXStandInVAE(crop=True)
    out = enc.encode(frame(min(h, 200), min(w, 200)), vae)
    check(f"cropping VAE encodes {min(w, 200)}x{min(h, 200)} without complaint",
          vae.encode_called is True and torch.is_tensor(out[0]["samples"]))
vae = LTXStandInVAE(crop=True)
_lat, rep = enc.encode(frame(100, 96), vae)
check("and the silent crop is still reported, as it was before this guard existed",
      "CROPPED" in rep, rep[-160:])

# =================================================================================================
print("\n(d) a VAE that will not say what it needs is let through in silence")
# =================================================================================================


class Bare:
    """No downscale_ratio, no accessor, no latent_dim. What a stand-in or an old ComfyUI looks like."""

    def __init__(self):
        self.vae_dtype = torch.bfloat16
        self.first_stage_model = _Model()
        self.process_input = lambda image: image * 2.0 - 1.0
        self.process_output = lambda image: image.add_(1.0).div_(2.0).clamp_(0.0, 1.0)
        self.encode_called = False
        self.decode_called = False

    def vae_encode_crop_pixels(self, pixels):
        return pixels

    def encode(self, pixels):
        self.encode_called = True
        return torch.zeros(pixels.shape[0], 4, 8, 8)

    def decode(self, latent):
        self.decode_called = True
        out = torch.zeros(latent.shape[0], 64, 64, 3)
        self.process_output(out)
        return out


check("no alignment can be determined", _spatial_alignment(Bare()) is None)
vae = Bare()
out = enc.encode(frame(100, 97), vae)        # both axes odd sizes, and it still runs
check("the encode runs anyway, exactly as before the guard existed", vae.encode_called is True)
check("and it says nothing about alignment in the report",
      "multiple of" not in out[1] and "blocks of" not in out[1], out[1][-120:])
vae = Bare()
out = dec.decode({"samples": torch.zeros(1, 4, 8, 8)}, vae)
check("the decode runs anyway", vae.decode_called is True and torch.is_tensor(out[0]))

print("\nevery half-answer is a skip, not a guess")
for label, patch in (
    ("downscale_ratio is None", {"downscale_ratio": None}),
    ("downscale_ratio is a string", {"downscale_ratio": "32"}),
    ("downscale_ratio is an empty tuple", {"downscale_ratio": ()}),
    ("the ratio is 1, which divides everything", {"downscale_ratio": 1}),
    ("the ratio is a non-integral float (sd.py:879, an audio VAE)", {"downscale_ratio": 470.4}),
):
    vae = LTXStandInVAE()
    vae.__dict__.update(patch)
    # Overriding the accessor on the INSTANCE is the whole substitution - `del` would raise here, because
    # the stand-in defines it on the class and there is nothing to delete off the instance.
    vae.spacial_compression_encode = lambda: vae.downscale_ratio
    check(f"skipped: {label}", _spatial_alignment(vae) is None, str(_spatial_alignment(vae)))

print("\na 1-D VAE has no spatial axis, so it is skipped rather than answered")
vae = LTXStandInVAE(latent_dim=1)
check("latent_dim 1 means no alignment to enforce", _spatial_alignment(vae) is None)

print("\na bare int downscale_ratio is read correctly, through the same accessor")
# sd.py:495, :534, :543 and others set a plain int. spacial_compression_encode's subscript raises on
# it and its fallback returns the int itself - the reason the accessor is called instead of parsed.
for ratio, want in ((8, 8), (16, 16), (32, 32), (4, 4)):
    vae = LTXStandInVAE()
    vae.downscale_ratio = ratio
    check(f"downscale_ratio = {ratio} reads as {want}", _spatial_alignment(vae) == want,
          str(_spatial_alignment(vae)))
vae = LTXStandInVAE()
check("the tuple form reads its LAST element, not its callable",
      _spatial_alignment(vae) == 32, str(_spatial_alignment(vae)))

print("\nand with no accessor at all, the field is read directly")
# An OLDER ComfyUI is what this stands for: spacial_compression_encode has not always existed, and the guard
# has to fall back to reading the field. The accessor lives on the stand-in's CLASS, so it is masked on the
# instance rather than deleted - `del` reaches only instance attributes and raises here.
vae = LTXStandInVAE()
vae.spacial_compression_encode = None            # not callable -> the fallback path
check("tuple form still reads 32 with no accessor present", _spatial_alignment(vae) == 32,
      str(_spatial_alignment(vae)))
vae = LTXStandInVAE()
vae.spacial_compression_encode = None
vae.downscale_ratio = 8
check("int form still reads 8 with no accessor present", _spatial_alignment(vae) == 8)

# =================================================================================================
print("\nDECODE: a 4-D latent in a 5-D video VAE is refused before the memory estimate can trip")
# =================================================================================================
vae = VideoStandInVAE()
raised = None
try:
    dec.decode({"samples": torch.zeros(1, 4, 12, 16)}, vae)
except ValueError as e:
    raised = str(e)
check("the decode is refused with a ValueError", raised is not None,
      "" if raised else "no exception - the latent went to the VAE")
check("the VAE was never called", vae.decode_called is False)
if raised:
    check("it says how many dimensions were expected", "5-dimensional" in raised, raised[:70])
    check("it spells the axes out rather than saying 5-D",
          "[batch, channels, frames, height, width]" in raised)
    check("it says how many arrived", "has 4" in raised)
    check("it quotes the shape it was handed", "1x4x12x16" in raised)
    check("it names the likely cause", "different models" in raised)
    check("it says what to do", "Wire the latent from the model this VAE was loaded with" in raised)
    check("it does not blame this node", "not a limit of this node" in raised)
    check("it says the stock node fails the same way", "stock VAE Decode" in raised)
    print(f"\n----- the message an artist sees -----\n{raised}\n-------------------------------------")

print("\na matching latent is not refused")
vae = VideoStandInVAE()
out = dec.decode({"samples": torch.zeros(1, 128, 2, 4, 6)}, vae)
check("the decode runs", torch.is_tensor(out[0]) and vae.decode_called is True)
check("the image comes back as [B, H, W, C]", out[0].ndim == 4 and out[0].shape[-1] == 3,
      str(tuple(out[0].shape)))
check("no problem was found", _decode_shape_problem(vae, torch.zeros(1, 128, 2, 4, 6)) is None)

print("\nthe one mismatch ComfyUI fixes itself is left alone (sd.py:1188)")
# latent_dim 2 given a 5-D latent: sd.py:1188 drops the frame axis before anything else runs, so
# refusing it here would break a pairing that works today. The REVERSE has no such line.
vae = VideoStandInVAE(latent_dim=2)
check("a still VAE handed a 5-D latent is NOT refused",
      _decode_shape_problem(vae, torch.zeros(1, 4, 2, 8, 8)) is None)
check("but a still VAE handed a 3-D latent still is",
      _decode_shape_problem(vae, torch.zeros(1, 4, 8)) is not None)
check("and a still VAE handed its own 4-D latent is fine",
      _decode_shape_problem(vae, torch.zeros(1, 4, 8, 8)) is None)

print("\nthe decode guard skips whatever it cannot describe")
vae = VideoStandInVAE()
del vae.latent_dim
check("no latent_dim: skipped", _decode_shape_problem(vae, torch.zeros(1, 4, 12, 16)) is None)
vae = VideoStandInVAE()
vae.latent_dim = "3"
check("latent_dim is not an int: skipped", _decode_shape_problem(vae, torch.zeros(1, 4, 12, 16)) is None)
vae = VideoStandInVAE()
vae.extra_1d_channel = 16                      # sd.py:855, :930 - an axis latent_dim does not count
check("extra_1d_channel is set: skipped",
      _decode_shape_problem(vae, torch.zeros(1, 4, 12, 16)) is None)
vae = VideoStandInVAE()
check("a latent with no ndim at all: skipped", _decode_shape_problem(vae, object()) is None)

print("\nan audio VAE's own 3-D latent is fine, and a 5-D one is refused")
vae = VideoStandInVAE(latent_dim=1)
check("latent_dim 1 accepts [batch, channels, samples]",
      _decode_shape_problem(vae, torch.zeros(1, 64, 512)) is None)
msg = _decode_shape_problem(vae, torch.zeros(1, 128, 2, 4, 6))
check("and refuses a 5-D video latent", msg is not None and "[batch, channels, samples]" in msg,
      (msg or "")[:110])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
