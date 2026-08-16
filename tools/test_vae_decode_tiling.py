# -*- coding: utf-8 -*-
"""OCIO VAE Decode: PIXEL tile sizes must reach the VAE as LATENT tile sizes, and never degenerate.

Run:  python tools/test_vae_decode_tiling.py     (no pytest, no ComfyUI, no GPU)

WHY THIS FILE EXISTS. `vae.decode_tiled` counts in latent samples; the widgets are in pixels. The
divisors are the VAE's own - `spacial_compression_decode()` (comfy/sd.py:1426) and
`temporal_compression_decode()` (:1438) - and getting that conversion wrong is invisible: the decode
still runs, it just tiles at the wrong size. So the stand-in VAE here RECORDS what it was handed and
the assertions read those numbers, rather than checking that the node "called tiling".

THE DEGENERATE CASE IS NOT A CRASH, WHICH IS WHY IT NEEDS A TEST. comfy/utils.py
`tiled_scale_multidim` builds tile positions as `range(0, size - overlap, tile - overlap)` on the
LATENT numbers:

    overlap == tile  ->  step 0    ->  ValueError("range() arg 3 must not be zero")
    overlap  > tile  ->  step < 0  ->  an EMPTY range, so no tile is ever decoded, and the function
                                       still ends with `out.div_(out_div)` over two all-zero
                                       buffers: a whole clip of NaN with no error raised.

So the invariant every case below is held to is `1 <= tile` and `0 <= overlap < tile`, per axis,
after conversion.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch

import vae_nodes as vn
from vae_nodes import OCIOVAEDecode, _range_report, _tiling_kwargs

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


class _Model:
    def __init__(self, dtype=torch.bfloat16):
        self.dtype = dtype

    def to(self, dtype):
        self.dtype = dtype
        return self


class TiledVAE:
    """comfy.sd.VAE as far as this node reaches it, with the real ratio methods and a recorder.

    `comp` / `t_comp` mimic what sd.py's own methods return:
      - 32 and 8 for the LTX-2 video VAE (upscale_ratio set at sd.py:725)
      - 8 and None for a still-image VAE, whose upscale_ratio is a plain int so the tuple index in
        temporal_compression_decode() raises and it returns None (sd.py:1441-1442)
    """

    def __init__(self, comp=32, t_comp=8, frames=5, ratios_raise=False):
        self.vae_dtype = torch.bfloat16
        self.first_stage_model = _Model()
        self.working_dtypes = [torch.bfloat16, torch.float32]
        self.process_output = lambda image: image.add_(1.0).div_(2.0).clamp_(0.0, 1.0)
        self._comp = comp
        self._t_comp = t_comp
        self._frames = frames
        self._ratios_raise = ratios_raise
        self.tiled_kwargs = None          # what decode_tiled was handed
        self.calls = []                   # which entry point ran

    def spacial_compression_decode(self):
        if self._ratios_raise:
            raise AttributeError("no upscale_ratio on this VAE")
        return self._comp

    def temporal_compression_decode(self):
        if self._ratios_raise:
            raise AttributeError("no upscale_ratio on this VAE")
        return self._t_comp

    def _out(self):
        # [B, C, T, H, W] as the real VAE produces before movedim; the node folds time into batch.
        raw = torch.linspace(-1.2, 1.2, self._frames * 4).reshape(1, 1, self._frames, 2, 2)
        raw = raw.repeat(1, 3, 1, 1, 1).movedim(1, -1).contiguous()
        self.process_output(raw)
        return raw

    def decode(self, latent):
        self.calls.append("decode")
        return self._out()

    def decode_tiled(self, latent, tile_x=None, tile_y=None, overlap=None, tile_t=None, overlap_t=None):
        self.calls.append("decode_tiled")
        self.tiled_kwargs = dict(tile_x=tile_x, tile_y=tile_y, overlap=overlap,
                                 tile_t=tile_t, overlap_t=overlap_t)
        return self._out()


class MissingTiledVAE(TiledVAE):
    """A VAE with no decode_tiled at all - an older ComfyUI, or a custom wrapper.

    `hasattr` has to come back False. Setting the attribute to None would not do it (None is still
    an attribute, so hasattr stays True and the node would call None(...)), and `del` cannot remove
    an INHERITED attribute. A property that raises AttributeError is what hasattr actually tests.
    """

    @property
    def decode_tiled(self):
        raise AttributeError("decode_tiled")


def latent(frames=5):
    return {"samples": torch.zeros(1, 128, frames, 8, 8)}


node = OCIOVAEDecode()

print("the node's shape: new inputs and the new output are APPENDED, never inserted")
it = OCIOVAEDecode.INPUT_TYPES()
check("required widgets unchanged and in the same order",
      list(it["required"]) == ["samples", "vae", "precision", "clamp"], str(list(it["required"])))
check("the five tiling inputs are all optional, in order, after the required ones",
      list(it.get("optional", {})) == ["tiled", "tile_size", "overlap", "temporal_size",
                                       "temporal_overlap"], str(list(it.get("optional", {}))))
# execution.py:901-913: a REQUIRED input absent from a posted prompt is a hard validation error,
# while an absent OPTIONAL one falls through to the signature default. Any API workflow that already
# posts this node with two widget values must keep working.
import inspect

sig = inspect.signature(OCIOVAEDecode.decode).parameters
check("every tiling input has a signature default, so an omitted one cannot fail validation",
      all(sig[k].default is not inspect.Parameter.empty
          for k in ("tiled", "tile_size", "overlap", "temporal_size", "temporal_overlap")))
for k in ("tiled", "tile_size", "overlap", "temporal_size", "temporal_overlap"):
    spec = it["optional"][k]
    check(f"{k}: widget default == signature default", spec[1]["default"] == sig[k].default,
          f"widget {spec[1]['default']!r} vs signature {sig[k].default!r}")
# A default outside its own min..max is not an error anywhere - the UI just quietly moves the widget
# to a different number than the signature uses, and the node then behaves differently in the canvas
# and on the API path.
out_of_range = []
for scope in ("required", "optional"):
    for k, spec in it.get(scope, {}).items():
        o = spec[1] if len(spec) > 1 else {}
        if "default" in o and "min" in o and not (o["min"] <= o["default"] <= o["max"]):
            out_of_range.append(f"{k}={o['default']} outside {o['min']}..{o['max']}")
check("every numeric default sits inside its own declared min..max", not out_of_range,
      "; ".join(out_of_range))
check("IMAGE is still the FIRST output and STRING was appended after it",
      OCIOVAEDecode.RETURN_TYPES == ("IMAGE", "STRING"), str(OCIOVAEDecode.RETURN_TYPES))
check("RETURN_NAMES and OUTPUT_TOOLTIPS have one entry per output",
      len(OCIOVAEDecode.RETURN_NAMES) == 2 and len(OCIOVAEDecode.OUTPUT_TOOLTIPS) == 2,
      f"{len(OCIOVAEDecode.RETURN_NAMES)} names, {len(OCIOVAEDecode.OUTPUT_TOOLTIPS)} tooltips")
check("temporal tiling is OFF by default (a tile far longer than any clip)",
      it["optional"]["temporal_size"][1]["default"] >= 4096,
      str(it["optional"]["temporal_size"][1]["default"]))
# Changed to ON in v1.3.0, and the old assertion is kept in the history rather than deleted because the
# reasoning it encoded was sound and simply lost an argument to practice. Off was chosen so that adding this
# node to a saved graph could not change what that graph produced. What that missed is which default fails
# WORSE: whole-frame is the path that runs out of memory on a real clip, and it is slower even when it fits
# (912 s against 60 s over 121 frames at float32). A default that works and can be turned off beats a
# default that is safe and stops the render.
#
# Saved graphs are unaffected either way: widgets_values carries the value chosen when the node was added,
# so only newly created nodes pick this up. That is the part worth asserting, and it is asserted below.
check("tiled defaults ON, because whole-frame is the setting that runs out of memory",
      it["optional"]["tiled"][1]["default"] is True)
check("   and the signature agrees, so the API path and the canvas cannot disagree",
      sig["tiled"].default is True, f"signature says {sig['tiled'].default!r}")

print("\nwhich entry point runs")
vae = TiledVAE()
node.decode(latent(), vae, tiled=False)
check("tiled OFF -> vae.decode", vae.calls == ["decode"], str(vae.calls))
vae = TiledVAE()
node.decode(latent(), vae, tiled=True)
check("tiled ON  -> vae.decode_tiled", vae.calls == ["decode_tiled"], str(vae.calls))

print("\nTHE CONVERSION: pixels in, LATENT samples out, using the VAE's own ratios")
# LTX-2 video VAE: spatial 32x, temporal 8x (comfy/sd.py:725).
#   tile   384 px / 32 = 12 latent
#   overlap 64 px / 32 =  2 latent
#   temporal 4096 px / 8 = 512 latent frames  -> longer than any clip, so ONE temporal tile
#   temporal_overlap 32 px / 8 = 4 latent, and 4 <= 512//2 so it is kept as is
vae = TiledVAE(comp=32, t_comp=8)
node.decode(latent(), vae, tiled=True, tile_size=384, overlap=64,
            temporal_size=4096, temporal_overlap=32)
check("384px / 32 -> tile_x = tile_y = 12 latent", vae.tiled_kwargs["tile_x"] == 12
      and vae.tiled_kwargs["tile_y"] == 12, str(vae.tiled_kwargs))
check("64px / 32 -> overlap = 2 latent", vae.tiled_kwargs["overlap"] == 2, str(vae.tiled_kwargs))
check("4096px / 8 -> tile_t = 512 latent frames", vae.tiled_kwargs["tile_t"] == 512,
      str(vae.tiled_kwargs))
check("32px / 8 -> overlap_t = 4 latent frames", vae.tiled_kwargs["overlap_t"] == 4,
      str(vae.tiled_kwargs))
check("every value handed over is an int, not a float",
      all(isinstance(v, int) for v in vae.tiled_kwargs.values()), str(vae.tiled_kwargs))

# A DIFFERENT VAE MUST GIVE DIFFERENT NUMBERS. If the divisor were hardcoded to 32 this passes above
# and fails here, which is the whole point of asking the VAE.
vae = TiledVAE(comp=8, t_comp=4)
node.decode(latent(), vae, tiled=True, tile_size=384, overlap=64,
            temporal_size=4096, temporal_overlap=32)
check("the same 384px on an 8x VAE -> 48 latent, not 12", vae.tiled_kwargs["tile_x"] == 48,
      str(vae.tiled_kwargs))
check("the same 64px on an 8x VAE -> overlap 8 latent", vae.tiled_kwargs["overlap"] == 8,
      str(vae.tiled_kwargs))
check("the same 4096px on a 4x temporal VAE -> tile_t 1024", vae.tiled_kwargs["tile_t"] == 1024,
      str(vae.tiled_kwargs))

print("\na still-image VAE has no time axis: tile_t / overlap_t must be None, not a guess")
# sd.py:1441-1442 returns None when upscale_ratio is a plain int. sd.py:1287-1292 forwards only the
# arguments that are not None, so None is how you say "do not tile time".
vae = TiledVAE(comp=8, t_comp=None)
node.decode(latent(), vae, tiled=True, tile_size=256, overlap=64)
check("tile_t is None", vae.tiled_kwargs["tile_t"] is None, str(vae.tiled_kwargs))
check("overlap_t is None", vae.tiled_kwargs["overlap_t"] is None, str(vae.tiled_kwargs))
check("spatial tiling still happened", vae.tiled_kwargs["tile_x"] == 32, str(vae.tiled_kwargs))

print("\nDEGENERATE CASES: overlap must never reach the tile, or the tiler NaNs the whole clip")
vae = TiledVAE(comp=32, t_comp=8)
node.decode(latent(), vae, tiled=True, tile_size=64, overlap=4096)      # overlap 64x the tile
k = vae.tiled_kwargs
check("spatial overlap far past the tile is pulled back below it",
      0 <= k["overlap"] < k["tile_x"], f"overlap {k['overlap']} vs tile {k['tile_x']}")
check("the tile is at least 1 latent sample", k["tile_x"] >= 1, str(k))

# THE SPATIAL BOUND MUST DEMONSTRABLY FIRE, not merely be present. On a 32x VAE a 384 px tile is 12
# latent samples whose quarter is 3, so a 4096 px overlap - which converts to a raw 128 - has to come
# back to exactly 3. An implementation that bounded the PIXEL values before dividing would also reach
# a safe number here, but its latent check could never fire at all: searched exhaustively, the
# condition has no solution once the pixel rule has run. Pinning the value keeps this honest.
vae = TiledVAE(comp=32, t_comp=8)
node.decode(latent(), vae, tiled=True, tile_size=384, overlap=4096)
check("a 4096px overlap on a 12-sample tile is bounded to exactly a quarter of it, 3",
      vae.tiled_kwargs["overlap"] == 3, str(vae.tiled_kwargs))

vae = TiledVAE(comp=32, t_comp=8)
node.decode(latent(), vae, tiled=True, tile_size=384, overlap=-64)
check("a negative overlap cannot reach the tiler as a negative step",
      vae.tiled_kwargs["overlap"] == 0, str(vae.tiled_kwargs))

vae = TiledVAE(comp=32, t_comp=8)
node.decode(latent(), vae, tiled=True, temporal_size=8, temporal_overlap=4096)
k = vae.tiled_kwargs
# 8/8 = 1, floored to 2 (sd.py:1308 does the same); 4096/8 = 512, which MUST come back to 1.
check("temporal overlap far past the temporal tile is pulled back below it",
      1 <= k["overlap_t"] < k["tile_t"], f"overlap_t {k['overlap_t']} vs tile_t {k['tile_t']}")
check("temporal overlap is at most half the temporal tile",
      k["overlap_t"] <= max(1, k["tile_t"] // 2), str(k))
check("tile_t is at least 2, as sd.py:1308 also requires", k["tile_t"] >= 2, str(k))

# A compression ratio LARGER than the whole tile would divide to zero, and a zero tile with a zero
# overlap is step 0 -> ValueError. sd.py has ratios of 800, 2048 and 4096 (lines 1000, 669, 848).
vae = TiledVAE(comp=4096, t_comp=8)
node.decode(latent(), vae, tiled=True, tile_size=384, overlap=64)
k = vae.tiled_kwargs
check("a ratio bigger than the tile still yields a tile of at least 1", k["tile_x"] >= 1, str(k))
check("...and an overlap strictly below it", 0 <= k["overlap"] < k["tile_x"], str(k))

# sd.py:878 sets upscale_ratio to 512 * (44100 / sample_rate), i.e. a FLOAT ratio.
vae = TiledVAE(comp=512.0, t_comp=None)
node.decode(latent(), vae, tiled=True, tile_size=4096, overlap=512)
k = vae.tiled_kwargs
check("a float compression ratio still produces int tile sizes",
      isinstance(k["tile_x"], int) and isinstance(k["overlap"], int), str(k))

print("\nthe invariant `0 <= overlap < tile` over a sweep, per axis")
bad = []
for comp in (1, 3, 8, 16, 32, 100, 800, 4096):
    for t_comp in (None, 1, 4, 8, 32):
        for tile_size in (64, 96, 128, 384, 768, 4096):
            for ov in (0, 32, 64, 512, 4096):
                for t_size in (8, 32, 128, 4096):
                    for t_ov in (4, 32, 512, 4096):
                        kw, _ = _tiling_kwargs(TiledVAE(comp=comp, t_comp=t_comp),
                                               tile_size, ov, t_size, t_ov)
                        if kw is None:
                            bad.append(("returned None", comp, t_comp, tile_size, ov))
                            continue
                        if not (kw["tile_x"] >= 1 and 0 <= kw["overlap"] < kw["tile_x"]):
                            bad.append(("spatial", comp, tile_size, ov, kw))
                        if kw["tile_t"] is not None:
                            if not (kw["tile_t"] >= 2 and 1 <= kw["overlap_t"] < kw["tile_t"]):
                                bad.append(("temporal", t_comp, t_size, t_ov, kw))
                        for key in ("tile_x", "tile_y", "overlap"):
                            if not isinstance(kw[key], int):
                                bad.append(("not an int", key, kw))
check("no combination produces a step of zero or less, and none produces a float",
      not bad, f"{len(bad)} bad: {bad[:3]}")

print("\nthe VAE that cannot be asked is decoded UNTILED, not guessed at")
vae = MissingTiledVAE(comp=32, t_comp=8)
out, rep = node.decode(latent(), vae, tiled=True)
check("no decode_tiled -> falls back to vae.decode", vae.calls == ["decode"], str(vae.calls))
check("...and says so in the report", "no decode_tiled" in rep, rep.splitlines()[-1][:90])

vae = TiledVAE(ratios_raise=True)
out, rep = node.decode(latent(), vae, tiled=True)
check("ratio methods that raise -> falls back to vae.decode", vae.calls == ["decode"], str(vae.calls))
check("...and says so rather than assuming a divisor",
      "would not report its compression ratios" in rep, rep.splitlines()[-1][:90])

print("\nstate is restored on the TILED path too, not only the untiled one")
for tiled in (False, True):
    vae = TiledVAE()
    before_out, before_dtype = vae.process_output, vae.vae_dtype
    node.decode(latent(), vae, precision="float32", clamp=False, tiled=tiled)
    check(f"tiled={tiled}: process_output restored", vae.process_output is before_out)
    check(f"tiled={tiled}: vae_dtype restored", vae.vae_dtype == before_dtype, str(vae.vae_dtype))
    check(f"tiled={tiled}: weights cast back", vae.first_stage_model.dtype == torch.bfloat16,
          str(vae.first_stage_model.dtype))


class RaisingTiledVAE(TiledVAE):
    def decode_tiled(self, latent, **kw):
        raise RuntimeError("tiled decode blew up")


vae = RaisingTiledVAE()
before_out = vae.process_output
try:
    node.decode(latent(), vae, precision="float32", tiled=True)
except RuntimeError:
    pass
check("a tiled decode that RAISES still restores process_output", vae.process_output is before_out)
check("...and the dtype", vae.vae_dtype == torch.bfloat16 and
      vae.first_stage_model.dtype == torch.bfloat16, str(vae.vae_dtype))

print("\nthe range report says what the clamp would have cost")
img = torch.tensor([-0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.5]).reshape(1, 2, 2, 2)
rep = _range_report(img, "t")
check("min and max are the real ones", "min=-0.500000" in rep and "max=+1.500000" in rep, rep)
check("2 of 8 samples below 0 -> 25%", "25.0000% below 0" in rep, rep)
check("1 of 8 samples above 1 -> 12.5%", "12.5000% above 1" in rep, rep)
check("and the total the clamp would remove", "37.5000% would be lost" in rep, rep)
check("an empty tensor does not raise", _range_report(torch.zeros(0), "t") == "t: empty")

# The percentile subsample must actually subsample, or a long clip pays for a full sort.
big = torch.rand(1, 2000, 2000, 3)                 # 12e6 samples, over the 4e6 cap
rep = _range_report(big, "big")
check("a big tensor reports percentiles from a subsample, and says how many",
      "of 12000000 samples" in rep, [l for l in rep.splitlines() if "samples)" in l])
small = torch.rand(1, 4, 4, 3)
check("a small tensor uses every sample", "all 48 samples" in _range_report(small, "s"),
      _range_report(small, "s"))

# THE OUT-OF-RANGE SHARE MUST COME FROM THE WHOLE ARRAY, never from the percentile subsample. That
# share is the number the report exists for, and subsampling it would make it wrong in a way nobody
# could see. Built so the two answers cannot agree: every third sample is negative, at the offsets
# the subsample steps straight over, so a subsampled count reads 0.0000% while the truth is a third.
n = 12_000_003
a = torch.zeros(n)
a[1::3] = -1.0                                  # exactly one third, none of them at index % 3 == 0
rep = _range_report(a.reshape(1, 1, 1, n), "biased")
check("the below-0 share is counted over every sample, not the subsample",
      "33.3333% below 0" in rep, [l for l in rep.splitlines() if "outside" in l])

# THE SUBSAMPLE MUST STRIDE WHOLE PIXELS, NOT FLAT SAMPLES. images is [B,H,W,C] and a C-order ravel makes
# index % C the channel, so a flat a[::step] collapses to ONE CHANNEL whenever step is a multiple of C - which
# it usually is, because step derives from a size that is itself a multiple of C. Found by review, not by any
# test here: the subsample checks above only ever asked HOW MANY samples, never WHICH. The fixture gives each
# channel a distinct constant so a one-channel subsample cannot produce the right median by luck: with 3
# channels the true median is the green value, and a red-only subsample reports the red one.
for h, w in ((1080, 1920), (704, 1280)):
    x = torch.empty(8, h, w, 3)
    x[..., 0], x[..., 1], x[..., 2] = 0.20, 0.50, 0.80
    rep = _range_report(x, "chan")
    p50 = float(rep.split("p50=")[1].split()[0])
    check(f"{h}x{w}x8: the median is the frame's, not one channel's",
          abs(p50 - 0.50) < 1e-4, f"p50={p50:+.5f}, true 0.50000")
    check(f"{h}x{w}x8: and the report says how many channels it sampled", "3 channels" in rep,
          [l for l in rep.splitlines() if "samples," in l])
    del x

# NON-FINITE VALUES ARE COUNTED, not folded into "in range". NaN is neither < 0 nor > 1, so it used to vanish
# from both shares while making min/max/mean print nan - a corrupted decode showing a believable figure.
nf = torch.tensor([[[[0.5, -0.2, 1.4], [float("nan"), float("inf"), 0.3]]]])
rep = _range_report(nf, "nf")
check("a NaN is reported rather than silently counted as in-range", "NOT FINITE" in rep, rep)
check("the NaN and the inf are told apart", "1 NaN, 1 inf" in rep,
      [l for l in rep.splitlines() if "FINITE" in l])
check("and the finite figures are still real numbers, not nan",
      "min=-0.200000" in rep and "max=+1.400000" in rep,
      [l for l in rep.splitlines() if "nf:" in l])

print("\nthe report is the SECOND output and the image is untouched by it")
vae = TiledVAE()
out, rep = node.decode(latent(), vae, tiled=True)
check("output 0 is a tensor", torch.is_tensor(out), type(out).__name__)
check("output 1 is a str", isinstance(rep, str), type(rep).__name__)
check("the image is [B, H, W, C] with time folded into batch", out.ndim == 4 and out.shape[-1] == 3,
      str(tuple(out.shape)))
check("the report names the tiled path", "tiled" in rep.splitlines()[0], rep.splitlines()[0])
check("the report carries the converted latent sizes",
      "tile_x=tile_y=12 latent" in rep, [l for l in rep.splitlines() if "latent" in l][:1])

print("\nthe report says WHICH transform case the VAE fell into, and they must not read alike")
# The identity case and the unrecognised case both leave the VAE alone, so no pixel test can tell
# them apart - but they mean opposite things to whoever reads the report. On an identity VAE there
# was never a clamp to remove and nothing is wrong; on an unrecognised one the node gave up and the
# decode is the stock, clamped one. Anyone chasing "why is my output still clamped" needs that
# difference, so it is asserted here rather than left to the log.
vae = TiledVAE()
vae.process_output = lambda image: image                      # TAEHV-style, sd.py:894
out, rep = node.decode(latent(), vae, tiled=False)
check("an identity VAE is reported as a pass-through, not as unrecognised",
      "pass-through" in rep and "not a shape this node recognises" not in rep,
      [l for l in rep.splitlines() if "note:" in l][:1])
# The note used to read "emits 0..1 natively", and this check asserted that exact phrase. It was corrected
# 2026-08-13 because it is FALSE for the six audio VAEs in comfy/sd.py that also set process_output to the
# identity (689, 851, 881, 926, 952, 1003) and take the same VAE socket. Asserting the old wording would pin the
# untrue version in place, so the phrase is asserted absent as well as the new one present.
check("and it no longer claims 0..1 for a transform that is merely a pass-through",
      "emits 0..1" not in rep, [l for l in rep.splitlines() if "note:" in l][:1])
# Nor may it claim the DECODER did not clamp, which is a different statement from "the wrapper did not"
# and one a pass-through cannot support (corrected 2026-08-14). MiniMax H3 clamps inside itself at
# comfy/ldm/minimax/vae.py:398-401 and then installs the identity, so on that model the old wording told
# the artist the opposite of the truth and sent them hunting for highlights nothing could return. The
# replacement has to carry the caveat, not merely drop the false half, or the note reads as reassurance.
check("and it does not claim the decoder left the values alone, which it cannot know",
      "nothing was clamping" not in rep, [l for l in rep.splitlines() if "note:" in l][:1])
check("and it warns that a decoder may have clamped before this node saw the tensor",
      "clamp internally" in rep, [l for l in rep.splitlines() if "note:" in l][:1])

vae = TiledVAE()
vae.process_output = lambda image: image.mul_(3.0).sub_(7.0)   # nothing this node knows
out, rep = node.decode(latent(), vae, tiled=False)
check("an unrecognised transform is reported as unrecognised",
      "not a shape this node recognises" in rep,
      [l for l in rep.splitlines() if "note:" in l][:1])

print("\na declined float32 is reported on the wire, not only in the server log")
vae = TiledVAE()
vae.working_dtypes = [torch.bfloat16]
out, rep = node.decode(latent(), vae, precision="float32", tiled=False)
check("float32 declined appears in the report", "float32 declined" in rep,
      [l for l in rep.splitlines() if "note:" in l][:1])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
