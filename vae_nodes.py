# -*- coding: utf-8 -*-
"""Latent decoding that keeps what the standard path throws away.

RESPONSIBLE FOR: giving a colour pipeline the VAE output as the model produced it (2026-08-12).

ComfyUI's VAEDecode finishes every decode with `image.add_(1).div_(2).clamp_(0, 1)`. The mapping is
the VAE's own [-1, 1] convention; the clamp is a display decision, and this node lets you keep the
values instead.

HOW MUCH THIS ACTUALLY BUYS - measured twice, on a generation and on real film material, because the
honest answer is "less than it sounds" and shipping the louder claim would be a lie.

SDR GENERATION. One LTX-2.5 latent decoded three times at the same precision and written to raw EXR
(ours unclamped, ComfyUI's stock node, and ours with the clamp back on - which comes out BIT-IDENTICAL
to stock, so the node is a safe drop-in): the clamp changed 0.06% of samples, and the overshoot reached
+0.05 above white and -0.03 below black, about a twentieth of a stop. A dark interior with a blown
window and a neon sign, a street of glass towers throwing speculars, and Lightricks' own demo prompt
all landed in that band. The decoded values of an SDR generation are display-referred and do not carry
more, whatever the prompt says.

REAL HDR FILM MATERIAL. A 10-bit ADX10 DPX scan carrying +3.9 stops above diffuse white (3.15% of
samples above 1.0, peaking near 15x white) was encoded to ACEScct, pushed through this VAE, and decoded
both ways. The HDR survives the VAE - 3.128% of samples came back above 1.0 against 3.148% going in.
But ours and stock came out the SAME to within the VAE's own reconstruction error: median error 3.24%
(ours) against 3.21% (stock) overall, and 5.6% against 5.5% in the highlights, p90 about 20%. The
reason is arithmetic: ACEScct code 1.0 is roughly linear 222, so 15x white only reaches code 0.80 and
there is nothing for a 0..1 clamp to remove. The VAE's own error dominates everything else by an order
of magnitude.

SO, PLAINLY: this node does NOT rescue dynamic range on ordinary material, and must not be sold as
doing so. The clamp only removes anything once codes exceed 1.0 - linear above roughly 222 in ACEScct,
which is 200x white, not 15x. What the node is actually for:

  - CONTROL, with a verified floor: clamp=on reproduces the stock decode bit for bit, so switching to
    this node changes nothing until you ask it to. Then you can decline the clamp where the material
    warrants it, instead of the decision being made for you.
  - material that genuinely exceeds code 1.0, and any downstream operation that needs headroom rather
    than a hard ceiling baked in before it runs.
  - float32, because the reference pipeline for LTX-2 does exactly that for HDR
    (`vae_dtype_for_hdr` in ltx-pipelines) and bfloat16 carries only 8 bits of mantissa.

PRECISION, NOW ISOLATED (2026-08-12). An earlier version of this file said the measurement compared
CONFIGURATIONS and therefore could not separate precision from the clamp. It has since been isolated -
same latent, same clamp setting, only the VAE's dtype changed - and the answer is that float32 changes
almost every sample. HOW MUCH IT COSTS DEPENDS ENTIRELY ON TILING, and an earlier version of this
docstring got that wrong - see the time section below.

  The video VAE's default is bfloat16, not float32, so the switch is not a no-op. Confirmed three ways:
  comfy/sd.py:602 lists working_dtypes [bfloat16, float32]; ComfyUI's own log says
  "VAE load device ... dtype: torch.bfloat16"; and the pixels agree - at the VAE's own dtype 100.000% of
  samples are exactly bfloat16-representable with a smallest gap of exactly 2^-8, against 0.012% at
  float32. (The "dtype: torch.float32" line in the log belongs to the AUDIO VAE, sd.py:928. Easy to
  misread as the video one.) The VAE's GPU footprint doubles, 1403.92 MB to 2807.83 MB.

  SDR generated latent, 25 frames at 1280x704, clamp off in both arms: 99.9974% of samples differ, but
  the median difference is 0.457 of a 10-bit code step (p99 1.76, max 14.0). In the brightest 1% the
  median difference is 0.74 MILLIstops and the worst is 6.36 millistops.

  Real ACEScct HDR frame, 2048x1152, where the clamp never fired in either arm so the comparison is
  exact: median 0.230 code steps, p99 1.26, max 6.17. On scene-linear values above diffuse white the
  median difference is 6.48 millistops and the worst is 0.106 stops. HDR survival is unchanged -
  3.128% against 3.129% of samples above 1.0, peak 20.22 against 20.31 linear, both +4.34 stops.

  It is not merely output rounding: only ~23% of samples equal round(float32 -> bfloat16), so the error
  accumulates through the iterative decoder. There is a small systematic bias - float32 reads 8.5e-5
  higher, about 0.09 of a code step, i.e. bfloat16 sits imperceptibly darker.

  THE TIME COST IS ABOUT TILING, NOT ABOUT PRECISION, and an earlier version of this docstring stated
  it wrongly. It claimed "a confirmed floor of 31x", derived from an envelope: one prompt spent 3489.6 s
  in two decodes while a comparable prompt with three bfloat16 decodes plus a cold-cache generation
  finished in 110.39 s. That measurement was of an UNTILED full-resolution decode, which is the
  pathological case, and the ratio said more about VRAM offload than about float32.

  Measured properly by Andrei Orehov on 121 frames at 1280x704:

    tile_size 768, temporal 4096 -> 912 s   float32 exceeds the card, offloads, crawls
    tile_size 768, temporal   32 ->  60 s   BUT a visibly soft frame every 24 frames
    tile_size 384, temporal 4096 ->  60 s   clean

  So float32 costs little when the decode is tiled in SPACE, and a great deal when it is not. What was
  actually observed here on the untiled run is narrower than the old text claimed: it ran long enough
  that the server's listener died ("Accept failed on a socket ... OSError(22)"), leaving a process alive
  with no port. The prompt still finished and wrote every frame, so nothing was lost, but the server
  needed a restart. It was NOT an out-of-memory condition: comfy/sd.py already falls back to tiled
  decoding on OOM, which this node inherits by calling vae.decode - it was simply slow.

  TILING IS EXPOSED ON THE NODE (2026-08-13), which is what makes the row above actionable rather than a
  warning about a setting nobody could reach: `tiled`, `tile_size`, `overlap`, `temporal_size` and
  `temporal_overlap`, appended after the existing widgets. Pixel sizes are converted to the VAE's own latent
  sizes before the call - see _tiling_kwargs. The temporal defaults are chosen so that temporal tiling does
  not happen at all, for the reason in the next paragraph.

  TILE SPACE, NEVER TIME. The 24-frame period above is arithmetic, not a mystery: temporal_size 32 gives
  tile_t = 32/8 = 4 latent frames with overlap_t = 1, so the step is 3 latent = 24 pixel frames. A
  diffusion decoder has no context at a temporal tile edge, and the blend mixes that weak edge into its
  neighbour. Measured sharpness (mean |Laplacian|) dipped to 62% of median at frames 25/49/73/97; with no
  temporal tiling there are no dips. Spatial boundaries show a gradient excess of only 1.03-1.05x, i.e.
  no seam at all. A frame-difference metric does NOT catch the temporal artefact, because it is a smooth
  blur rather than a jump; only per-frame sharpness with a local-dip search finds it.

  WHAT FLOAT32 BUYS, stated by the same measurements: bfloat16 quantises the decode to roughly 10 bits
  (77 distinct levels in the window [0.2, 0.3] of one frame, smallest step 1/1024, against 3.35 million
  at float32), and the two diverge by up to 0.0186 - about five steps of an 8-bit scale. No float
  container recovers that afterwards; the precision has to exist at decode time.

  FLOAT32 IS NOW THE DEFAULT, AND IT IS THE EXPENSIVE ONE. That is a deliberate trade and the price is
  recorded here rather than argued: a colour pipeline should state the precision it ran at instead of
  inheriting whichever dtype the checkpoint's branch of comfy/sd.py happened to list first. What it
  costs, timed per node from the server's own websocket, 25 frames at 1280x704, tile_size 384, overlap
  64, temporal_size 4096: 5.96 s at the model's own dtype against 29.90 s at float32, a FACTOR OF 5.02.
  Every arm decoded one latent that ComfyUI served from its execution cache, so the input was identical
  by the server's own account rather than by assumption, and a repeat of the bfloat16 arm came back at
  5.65 s, which is the run-to-run spread to read the 5.02 against. Repeated in-process on different
  material (a 25-frame pan encoded from a photograph, same tiling): 5.54 s against 28.34 s, a factor of
  5.11, and a third pass gave 5.53 s against 28.84 s, 5.21. Read it as ABOUT FIVE TIMES. Both runs had
  the cards near-idle, about 1.3 GB held, a FRIENDLIER condition than the roughly 6.5 GB behind the
  earlier figures - the ratio did not improve for the extra room.

  An earlier threshold said float32 should become the default only if it landed within 1.5x, and it does
  not; that threshold has been overridden on purpose, not met. Anyone reading these numbers as an
  argument to revert should know the decision was made with them in hand.

  THE CHEAP PATH STILL EXISTS, and this is the part to understand before changing anything. On a VAE
  that does not list float16 - LTX among them - selecting float16 is DECLINED and the decode runs at the
  model's own dtype, producing pixels bit for bit identical to what the old "model default" produced
  there (min -0.082031, max +1.085938, mean +0.679877 on both arms, confirmed through /prompt). So the
  fast path is reached by asking for float16 and being turned down, and the range report says so.

  THE ENCODE HAS ITS OWN COST, instead of borrowing the decode's: same clip, same server path, 2.99 s at
  the model's own dtype against 5.37 s at float32, a factor of 1.80 (repeat 3.08 s). Far cheaper than
  the decode, and still not free.

  FLOAT16 IS OFFERED, and the case for it is about the VAE SOCKET rather than about LTX. An earlier
  version of this docstring declined to offer it, reasoning from a census that answered the wrong
  question: it established that no VAE in comfy/sd.py refuses float32, which says nothing about
  whether an artist ever wants float16. These nodes decode whatever VAE is wired into them, and the
  census read a second time, with each assignment attributed to its branch, says this:

    all 23 working_dtypes lists include float32      (so float32 is always available)
    8 of 23 OMIT float16    503, 584, 602, 729, 786, 883, 928, 1005
    9 of 23 list float16 FIRST   567, 609, 619, 691, 707, 740, 838, 972, 1019
    5 of 23 have NO bfloat16 at all   707, 883, 928, 972, 1005

  model_management.vae_dtype() at :1258-1263 walks that list IN ORDER and returns the first entry the
  device supports, confirmed on this hardware by calling it: vae_dtype([fp16, fp32]) returns
  torch.float16, vae_dtype([bf16, fp32]) returns torch.bfloat16, vae_dtype([fp32]) returns
  torch.float32. Three consequences, and each is a reason the option is not redundant:

    - On those 9 float16-first lists, an inherited dtype ALREADY resolves to float16, by accident of
      list ordering rather than by anyone's choice - which is the strongest argument for naming the
      precision rather than inheriting it, and the reason the inherit option is gone. Upstream can
      reorder a list in any release and an artist's output silently changes dtype between two runs of
      the same graph. sd.py:740 makes the point sharply: it is another Lightricks VideoVAE, and float16 comes
      FIRST there while sd.py:602 has no float16 at all - two variants of one family resolving
      differently.
    - Five lists carry no bfloat16, so on those models "bfloat16 or float32" is not the choice on
      offer at all. MiniMax H3's video VAE (sd.py:972) is float16/float32, and its audio VAE
      (sd.py:1005) is float32 alone.
    - Where bfloat16 does come first, choosing float16 is a quality gain rather than a compromise -
      see the measurement below.

  WHAT FLOAT16 MEASURED, on the LTX VAE, forced past its own list purely to take the number: it
  decoded 25 frames at 1280x704 cleanly, no NaN and no infinity and a correct picture, and sat CLOSER
  to float32 than bfloat16 does - median error 0.000053 against bfloat16's 0.000445, worst 0.006815
  against 0.043412 - at bfloat16's speed, 5.38 s against 5.54 s. It also quantises far less coarsely:
  264 997 distinct values in a frame against bfloat16's 91 991. The weights cast safely, largest
  |weight| 5.16 against float16's ceiling of 65504, with 0.0008% flushing to zero.

  AND THE LIMIT ON THAT NUMBER, which belongs next to it rather than in a footnote: float16's known
  failure mode is EXPONENT RANGE, not mantissa, and it was NOT probed. The attempt proved nothing,
  because this decoder normalises the latent's magnitude away - scaling the latent by 10 through 10000
  moved the output's peak by under 0.2 and produced no non-finite value at any of the three dtypes. So
  "float16 is numerically better here" rests on ONE SDR latent. Anyone shipping an HDR master at
  float16 should check that master, not this docstring.

  WHICH IS WHY THE VAE'S OWN LIST IS RESPECTED AND NEVER OVERRIDDEN. On the 8 lists without float16 -
  the installed LTX-2.5 video VAE included - the request is declined with a message on the range report
  and the decode runs at the VAE's own precision. That is what makes offering the option safe on a model
  nobody here has seen yet, and it is the one behaviour to preserve if this code is ever refactored
  again. See _request_dtype.

  WHICH BRANCH IS "OURS" WAS CITED WRONGLY UNTIL 2026-08-13, and the correction is kept here because the
  mistake is an easy one to repeat. This file used to name sd.py:729 as the VAE the pack is built around.
  The installed ltx-2.5-video-vae-bf16.safetensors does not match that branch at all: it carries
  `decoder.conv_in_x_t.weight`, so it takes the branch at sd.py:587 and builds
  na_diffusion_decoder.CausalDiffusionVAE, whose working_dtypes is the list at sd.py:602. Line 729 is a
  different Lightricks decoder (causal_video_autoencoder.VideoVAE) that this checkpoint never reaches.
  Both lists happen to be [bfloat16, float32], so every measured behaviour above is unaffected and the
  float16 decline is real and for the stated reason - but the citation pointed at the wrong code.

  It was caught by a RUNTIME signal rather than by reading: a traceback naming CausalDiffusionVAE, and a
  memory_used_decode lambda at sd.py:596 that exists only in that branch. Identify a VAE branch by
  loading the checkpoint and reading the class it constructs, never by matching a model's name to a
  comment in the source.

An earlier version of this docstring cited "3.01% of pixels below zero" as the justification. That
figure came from a ROUND-TRIP measurement, a different experiment, and it does not reproduce when
decoding a generation. Replaced with the numbers above 2026-08-12.

WHAT THE OUTPUT MEANS. A latent decodes to whatever the generation encoded. For an ordinary SDR
generation that is display-referred RGB. For LTX-2's HDR path it is ACEScct log codes, which look
flat and grey until the curve is undone - feed those to OCIO LogConvert (operation `Log to Linear`,
curve `ACEScct`, spelled exactly like that: a combo value is matched by string, so the lowercase
`acescct` this line used to carry would be an HTTP 400 for the whole prompt if anyone copied it into
an API graph) to recover scene-linear values, then to OCIO Write. This node deliberately does not guess
which case it is looking at: guessing the encoding is how footage gets silently mangled.
"""

import numpy as np
import torch


_DEFAULT_SHAPE = "default"      # comfy/sd.py:502, (x+1)/2 with a clamp
_IDENTITY_SHAPE = "identity"    # already [0,1]-native, nothing clamps


# THE COMBO AND THE DISPATCH ARE THE SAME OBJECT, and that is the whole point of this mapping
# (2026-08-13). Both nodes used to decide precision with `if precision == "float32"`, a literal string
# compare. Appending a third value to the combo therefore produced a widget that selected NOTHING: no
# cast, no note, no error, the artist's choice silently discarded. A mapping cannot half-wire, because
# the combo is generated FROM it - a name that has no dtype cannot appear in the list to be chosen, and
# a name that appears in the list necessarily has one.
#
# THE PRECISION IS ALWAYS NAMED NOW: "model default" is gone (2026-08-13). The widget used to carry a
# third entry meaning "leave whatever ComfyUI chose alone", and it was the default. It was removed on a
# deliberate call - a colour pipeline should state the dtype it ran at rather than inherit one that
# depends on which branch of comfy/sd.py matched the checkpoint and in what order that branch happens to
# list its dtypes. The cost of that clarity is real, and it is named in the tooltips rather than argued.
#
# THIS IS A BREAKING CHANGE FOR SAVED GRAPHS, written down here rather than left to be discovered: a
# stored widget value of "model default" is no longer in the list, so a posted prompt carrying it is
# rejected with `value_not_in_list` for the whole prompt. Widget ORDER is untouched, so nothing else in a
# saved graph shifts - this one value has to be re-picked, and nothing more.
#
# The fast path did not leave with the entry. On a VAE that does not list float16 - LTX among them -
# selecting float16 is DECLINED and the decode runs at the model's own dtype, which is bit for bit what
# "model default" produced there. That is now how the cheap path is reached. See _request_dtype.
_PRECISION_DTYPES = {
    "float32": torch.float32,
    "float16": torch.float16,
}
_PRECISION_CHOICES = list(_PRECISION_DTYPES)


def _request_dtype(vae, precision, node_label, notes):
    """Put the VAE at the dtype `precision` names, for the duration of one node.

    RESPONSIBLE FOR: honouring a precision request without ever forcing one the model rejects
    (2026-08-13).

    Returns True only when the weights were really cast, so the caller's `finally` knows whether it
    has anything to put back. False covers every other outcome: not asked for, already there,
    declined, or refused - each of which leaves the VAE exactly as it was found.

    ONE implementation for BOTH nodes, deliberately. They used to carry a copy each and the copies
    drifted: the encode went months without the `working_dtypes` consultation or the refused-cast
    fallback, so the same VAE could finish a decode with a note and fail an encode outright. A single
    function cannot drift from itself, and that is the only reliable fix for a defect this project has
    now hit several times.

    WHY THE VAE'S OWN LIST IS RESPECTED RATHER THAN OVERRIDDEN. `working_dtypes` is the model's
    declaration of what it was built to run at, and it is not uniform: comfy/sd.py sets it in 23
    places and eight of those omit float16 - including sd.py:602, the VAE this pack is tested on, and
    sd.py:1005, the MiniMax H3 audio VAE, which lists float32 alone. So a float16 request is a real
    request on some models and an impossible one on others, and the node cannot know which without
    asking. Declining with a message on the wire beats both alternatives: forcing the cast produces
    garbage or a crash on a model that never supported it, and refusing to offer the option at all
    penalises every model that does.

    THE ORDER OF THE CAST AND THE FLAG IS LOAD-BEARING. The weights are cast FIRST and `vae_dtype` is
    set only after that returns. The opposite order left `vae_dtype` claiming a precision the weights
    did not have whenever the cast raised, with no restore, poisoning every later graph in the
    session. Nothing is recorded here that did not happen.
    """
    import logging

    want = _PRECISION_DTYPES[precision]      # KeyError on an unknown name, and loudly: see the mapping
    have = getattr(vae, "vae_dtype", None)
    # Every entry in the mapping now names a real dtype, so there is no "leave it alone" branch left to
    # take: it went with "model default" rather than being left behind as unreachable code. What remains
    # is the VAE that does not report a dtype at all, and the one already at the dtype asked for.
    if have is None or have == want:
        return False

    working = getattr(vae, "working_dtypes", None)
    if working and want not in working:
        logging.warning(f"{node_label}: this VAE does not list {precision} in working_dtypes "
                        f"({working}), so it stays at {have}. A precision the model was not built to "
                        f"run at cannot be forced on it.")
        notes.append(f"{precision} declined: not listed in this VAE's working_dtypes ({working}), so "
                     f"it ran at {have}")
        return False

    try:
        vae.first_stage_model.to(want)
        vae.vae_dtype = want
        return True
    except Exception as e:
        # Quantised weights (int8 / fp8) refuse the cast. Falling back beats failing the render:
        # precision is a preference, the frames are the job.
        logging.warning(f"{node_label}: could not cast this VAE to {precision} "
                        f"({type(e).__name__}: {e}); staying at {have}. Quantised weights cannot be "
                        f"re-cast on the fly.")
        notes.append(f"{precision} cast refused ({type(e).__name__}), it ran at {have} - quantised "
                     f"weights cannot be re-cast on the fly")
        return False

# Percentiles are the only part of the range report that needs a sort, so they are taken from a
# subsample. min / max / mean and the two out-of-range shares are single passes and use everything.
# Approach taken from Andrei Orehov's `_describe`.
_PERCENTILE_SAMPLE_CAP = 4_000_000


def _probe_process_output(vae):
    """Which SHAPE is this VAE's output transform? Asked, never assumed.

    WHY THIS EXISTS (2026-08-13). This node used to replace `vae.process_output` unconditionally with
    `(x+1)/2`, on the assumption that every VAE uses the default at comfy/sd.py:502. ELEVEN places in that
    file set it to the IDENTITY instead (counted: twelve `process_output = lambda` assignments, of which 502
    is the default itself). Five are image decoders that already emit [0,1]: lines
    540, 894/895, 906 and 976. Line 894 is TAEHV for `latent_channels in [48, 128]`, which is Wan 2.2 AND
    LTX2 - the fast preview decoder for the very model this pack was built around. Line 976 is a VAE that
    finalises straight to [0,1] while streaming chunks out.

    The other six are AUDIO VAEs (689, 851, 881, 926, 952, 1003). They matter here because they take the
    same VAE socket and reach this probe, and because they are the reason the identity note says
    "pass-through" rather than "emits 0..1" - the first census of this file listed only the five image
    sites, and the note written from it told anyone decoding audio something untrue about their data.

    On any of those, applying `(x+1)/2` to data already in [0,1] produces [0.5, 1.0]: a washed-out, wrong
    image, with no error anywhere. Both branches of the old code did it, the clamped one included, so
    "clamp ON reproduces stock exactly" was false for those VAEs too.

    Found by Andrei Orehov, who hit it on `taeltx2_3` and wrote the probe this one follows.

    The probe feeds -1 / 0 / 1 through the real function and reads where they land. The tensor is CLONED
    because the default transform is in-place and would otherwise consume its own input. An unrecognised
    result returns None, and the caller then leaves the VAE alone rather than guessing.
    """
    probe = torch.tensor([-1.0, 0.0, 1.0], dtype=torch.float32)
    try:
        got = vae.process_output(probe.clone()).float()
    except Exception:
        return None
    if torch.allclose(got, torch.tensor([0.0, 0.5, 1.0]), atol=1e-4):
        return _DEFAULT_SHAPE
    if torch.allclose(got, torch.tensor([-1.0, 0.0, 1.0]), atol=1e-4):
        return _IDENTITY_SHAPE
    return None


def _to_comfy_image(x):
    """VAE output -> ComfyUI IMAGE layout [B, H, W, C].

    A video VAE returns [B, T, H, W, C]; ComfyUI carries frames as a batch, so the time axis is
    folded into the batch. A still VAE already returns the right shape and is passed through.
    """
    if x.ndim == 5:
        return x.reshape(-1, x.shape[-3], x.shape[-2], x.shape[-1])
    return x


def _tiling_kwargs(vae, tile_size, overlap, temporal_size, temporal_overlap):
    """PIXEL tile sizes in, LATENT tile sizes out - the conversion `vae.decode_tiled` expects.

    RESPONSIBLE FOR: never handing the tiler a tile it cannot step through (2026-08-13).

    An artist thinks in pixels; `decode_tiled` counts in latent samples. The divisors are the VAE's
    own, asked for rather than assumed. Every name here was read in comfy/sd.py before use:

      - `spacial_compression_decode()`   :1426, returns `upscale_ratio[-1]` (32 for the LTX-2 VAE,
                                          whose `upscale_ratio` is set at :725)
      - `temporal_compression_decode()`  :1438, returns `round(upscale_ratio[0](8192) / 8192)` - 8 for
                                          that same VAE - and **None** for anything whose
                                          `upscale_ratio` is not a tuple of callables, i.e. every
                                          still-image VAE. None is a normal answer, not a failure.
      - `decode_tiled(samples, tile_x=None, tile_y=None, overlap=None, tile_t=None, overlap_t=None)`
                                         :1281. For a 3-dimensional latent it rebuilds `overlap` as
                                          `(overlap_t or 1, overlap, overlap)` at :1304-1306, so
                                          `overlap` must never arrive as None there, and it floors
                                          `tile_t` at 2 and `overlap_t` at 1 itself.

    WHAT THE BOUNDS ARE FOR. comfy/utils.py `tiled_scale_multidim` builds its tile positions as
    `range(0, size - overlap, tile - overlap)`, on the LATENT numbers passed in. So:

        overlap == tile  ->  step 0      ->  ValueError("range() arg 3 must not be zero")
        overlap  > tile  ->  step < 0    ->  the range is EMPTY, no tile is ever decoded, and the
                                             function still finishes with `out.div_(out_div)` on two
                                             all-zero buffers: an entire clip of NaN, with no error.

    The silent NaN is the one to fear, and a tile of ZERO reaches it by a second route: a compression
    ratio larger than the tile divides to 0, and 0 with an overlap of 0 is step 0 again. Searched over
    ratios 1..3000 against tile sizes 1..3000, that happens in 36 million of those combinations, and
    ratios of 800, 2048 and 4096 are real (comfy/sd.py:1000, :669, :848). Hence `max(1, ...)` on the
    tile, which the reference implementation does not have.

    The OVERLAP bound, by contrast, is sufficient in either space - over that same sweep a pixel-space
    quarter rule never lets the converted overlap reach the tile. It is applied to the converted value
    anyway, and only once, for a reason worth stating: apply it in pixels and then repeat it in latent
    space and the latent check can never fire at all (searched exhaustively, with a ceiling of either
    half or a quarter of the tile: zero cases). That is a guard in name only, and a guard whose failure
    cannot be observed is not a guard. One bound, on the number the tiler actually steps with, is
    reachable - and its failure IS observed, in tools/test_vae_decode_tiling.py.

    Returns `(kwargs, notes)`, or `(None, notes)` when the VAE will not say what its ratios are - in
    which case the caller decodes untiled rather than inventing a divisor. Three states, not two:
    tiled, not tiled, and could-not-determine, which is reported instead of guessed.
    """
    notes = []
    if not hasattr(vae, "decode_tiled"):
        notes.append("tiling requested, but this VAE has no decode_tiled(); decoded untiled instead")
        return None, notes
    try:
        comp = vae.spacial_compression_decode()
        t_comp = vae.temporal_compression_decode()
    except Exception as e:
        notes.append(f"tiling requested, but this VAE would not report its compression ratios "
                     f"({type(e).__name__}: {e}); decoded untiled rather than guessing a divisor")
        return None, notes

    # The ratio is not always an integer: comfy/sd.py:878 sets upscale_ratio to
    # `512 * (44100 / sample_rate)`, a float. Left as one it makes the tile size a float, which the
    # tiler then uses in index arithmetic.
    try:
        comp = max(1, int(comp))
    except (TypeError, ValueError):
        notes.append(f"tiling requested, but this VAE's spatial compression ratio is not a number "
                     f"({comp!r}); decoded untiled rather than guessing a divisor")
        return None, notes

    tile_lat = max(1, int(tile_size) // comp)

    # CONVERT FIRST, THEN BOUND - see the docstring for why the bound is here and not on the pixel
    # values. The rule itself is Andrei Orehov's and ComfyUI's alike: an overlap may be at most a
    # quarter of the tile, which is how sd.py:1234, :1240 and :1256 all derive their own. Converting
    # first changes no measured configuration - 384 px on a 32x VAE is a 12-sample tile whose quarter
    # is 3, and a requested 64 px overlap converts to 2, under it and untouched.
    ov_lat = max(0, min(int(overlap) // comp, tile_lat // 4))

    if t_comp:
        t_comp = max(1, int(t_comp))
        tile_t = max(2, int(temporal_size) // t_comp)
        # The temporal overlap clamped to half the temporal TILE, in latent samples. This is the
        # clamp that keeps `tile_t - overlap_t` positive; the reference implementation's pixel-space
        # line halves `temporal_overlap` instead of bounding it, which does not.
        ov_t = max(1, min(tile_t // 2, int(temporal_overlap) // t_comp))
        notes.append(f"tiled: {int(tile_size)}px -> tile_x=tile_y={tile_lat} latent (ratio {comp}x), "
                     f"overlap {int(overlap)}px -> {ov_lat} latent; temporal {int(temporal_size)}px -> "
                     f"tile_t={tile_t} latent (ratio {t_comp}x), overlap_t={ov_t}")
    else:
        # No temporal axis to tile. Passing None leaves both out of the call (sd.py:1287-1292 only
        # forwards what is not None), which is what a still-image VAE wants.
        tile_t = ov_t = None
        notes.append(f"tiled: {int(tile_size)}px -> tile_x=tile_y={tile_lat} latent (ratio {comp}x), "
                     f"overlap {int(overlap)}px -> {ov_lat} latent; no temporal tiling (this VAE "
                     f"reports no temporal compression ratio, so it has no time axis to tile)")

    return {"tile_x": tile_lat, "tile_y": tile_lat, "overlap": ov_lat,
            "tile_t": tile_t, "overlap_t": ov_t}, notes


def _range_report(images, label):
    """What the decode actually produced, as text, so the range is visible on a wire.

    RESPONSIBLE FOR: telling you what a clamp would have cost before you write the file (2026-08-13).

    Every figure but the percentiles is a single pass over the whole array, so the count of samples
    below 0 and above 1 is exact rather than sampled - that number is the point of the report and is
    not worth approximating. The percentiles need a sort, so they come from a subsample capped at
    _PERCENTILE_SAMPLE_CAP; a 121-frame HD clip is 3.3e8 samples and sorting it to print five numbers
    would cost more than the decode. Subsampling approach taken from Andrei Orehov's `_describe`.

    THE SUBSAMPLE STRIDES OVER PIXELS, NOT OVER SAMPLES, and that is a fix rather than a nicety
    (2026-08-13). `images` is [B, H, W, C] and a C-order ravel makes `index % C` the channel, so a flat
    `a[::step]` collapses to a SINGLE CHANNEL whenever step is a multiple of C - and it usually is, because
    step comes from a size that is itself a multiple of C. Measured on the clip this node's own tooltip
    cites, 121 frames of 1280x704: step 81, and every sampled value was channel 0. On a frame with
    genuinely different channels (R 0.20, G 0.50, B 0.80) the reported median was 0.20 against a true
    0.50, an error of 0.58 - a wrong number that looks entirely plausible. Striding whole pixels keeps
    all three channels in proportion at the same cost.

    NON-FINITE VALUES ARE COUNTED, not folded into "in range". NaN is neither < 0 nor > 1, so it used to
    vanish from both shares while making min/max/mean print nan; a NaN-corrupted decode would have shown a
    believable out-of-range figure. They are excluded from the percentiles too, since one NaN makes
    np.percentile return NaN for every one of them.
    """
    a = images.detach().float().cpu().numpy().ravel()
    n = a.size
    if n == 0:
        return f"{label}: empty"
    chans = int(images.shape[-1]) if images.ndim >= 2 else 1
    if chans < 1 or n % chans:
        chans = 1                                  # not a channel-last layout; treat it as one column
    px = a.reshape(-1, chans)
    per_px = max(1, _PERCENTILE_SAMPLE_CAP // chans)
    row_step = max(1, px.shape[0] // per_px)
    sample = (px if row_step == 1 else px[::row_step]).ravel()
    finite = np.isfinite(a)
    n_bad = int(n - int(finite.sum()))
    s_fin = sample[np.isfinite(sample)]
    p = np.percentile(s_fin, [0.1, 1, 50, 99, 99.9]) if s_fin.size else [float("nan")] * 5
    af = a[finite]
    below = float((af < 0.0).mean() * 100.0) if af.size else 0.0
    above = float((af > 1.0).mean() * 100.0) if af.size else 0.0
    head = (f"{label}: min={af.min():+.6f} max={af.max():+.6f} mean={af.mean():+.6f}" if af.size
            else f"{label}: no finite samples")
    bad = ""
    if n_bad:
        n_nan = int(np.isnan(a).sum())
        bad = (f"\n  NOT FINITE: {n_bad} of {n} samples ({n_bad / n * 100.0:.4f}%) - "
               f"{n_nan} NaN, {n_bad - n_nan} inf. The figures above cover the finite samples only.")
    return (
        f"{head}\n"
        f"  p0.1={p[0]:+.5f} p1={p[1]:+.5f} p50={p[2]:+.5f} p99={p[3]:+.5f} p99.9={p[4]:+.5f}"
        f"   ({'all ' + str(n) if row_step == 1 else f'{sample.size} of {n}'} samples, "
        f"{chans} channel{'s' if chans != 1 else ''})\n"
        f"  outside 0..1: {below:.4f}% below 0, {above:.4f}% above 1 "
        f"({below + above:.4f}% would be lost to the standard clamp){bad}"
    )


# =================================================================================================
# INPUT GUARDS - a sentence naming the wrong number, instead of a crash from inside the model.
#
# RESPONSIBLE FOR: refusing an input the VAE cannot take, in words that name the size (2026-08-13).
#
# NEITHER OF THESE FIXES ANY ARITHMETIC, and the messages must not pretend otherwise. Both inputs
# below fail on the STOCK ComfyUI nodes in exactly the same way; what they lack there is any hint of
# which number is wrong. Confirmed for both, by running the real mechanism rather than by reading:
#
#   * A frame whose height or width is not a multiple of the VAE's block size reaches the model and
#     dies inside einops with `Shape mismatch, can't divide axis of length 791 in chunks of 2` - an
#     internal axis, at a size that is the frame's height divided by four, and no mention of the
#     frame. Most VAEs never get there because comfy/sd.py:1069-1078 narrows the frame for them, but
#     that runs only `if self.crop_input`, and sd.py:575, :595 and :884 switch it off (at :595 with
#     the reason: a generic crop would narrow the FRAME axis by the 32x spatial ratio).
#   * A 4-dimensional latent handed to a 5-dimensional video VAE dies in the memory estimate, before
#     any decoding starts, because those lambdas index shape[4] - e.g. sd.py:596. `IndexError: tuple
#     index out of range` says nothing about latents at all. Both entry points reach it: sd.py:1192
#     inside a try whose handler re-raises anything that is not an OOM (model_management.py:393-395),
#     and sd.py:1282 with no try around it.
#
# BOTH GUARDS FAIL OPEN, deliberately and in the same direction. A VAE that will not say what it
# needs gets the input it would have got before, and the old unhelpful error with it. That is the
# cheaper side of the trade by a wide margin: an unclear error costs an artist a search, while a
# false refusal costs a job that would have rendered - and the field these read is set in over
# twenty places in one file, none of which this pack controls. So every uncertain answer is None.
# =================================================================================================


def _spatial_alignment(vae):
    """The pixel block this VAE encodes in, or None when it will not say plainly.

    ASKED THROUGH THE SAME ACCESSOR THE CROP USES, not by parsing the field. comfy/sd.py:1432
    `spacial_compression_encode` returns `downscale_ratio[-1]`, falling back to `downscale_ratio`
    itself, so it already answers for both shapes the field takes: a bare int (sd.py:495, :534,
    :543 and others) and the tuple `(callable, 32, 32)` that the video VAEs use (sd.py:570, :600,
    :727 and others), whose callable maps a FRAME COUNT and is not a spatial divisor at all.
    Reading the field here instead would be a second implementation of that rule, free to disagree
    with the crop at sd.py:1071 that this guard exists to sit beside. The field is read directly
    only when the accessor is missing, which is what a stand-in or an older ComfyUI looks like.

    A 1-D VAE is skipped rather than answered. sd.py:879 sets `downscale_ratio` to
    `512 * (44100 / sample_rate)` on an audio VAE, a number about a sample rate with no spatial
    meaning whatever; the neighbouring `latent_dim` (sd.py:880) is how that case is recognised.
    A non-integral ratio is refused for the same reason - see `_tiling_kwargs`, which documents the
    same float trap from the decode side.
    """
    dim = getattr(vae, "latent_dim", None)
    if isinstance(dim, int) and not isinstance(dim, bool) and dim < 2:
        return None                       # no spatial axis to align

    ratio = None
    getter = getattr(vae, "spacial_compression_encode", None)
    if callable(getter):
        try:
            ratio = getter()
        except Exception:
            ratio = None
    if ratio is None:
        ratio = getattr(vae, "downscale_ratio", None)
        if isinstance(ratio, (tuple, list)):
            ratio = ratio[-1] if len(ratio) else None

    # A NUMBER OR NOTHING. int("32") succeeds, so accepting a string here would let a foreign implementation's
    # text be read as a block size - exactly the guess this function exists to refuse. bool is an int in Python
    # and is excluded for the same reason: True would arrive as a ratio of 1.
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        return None
    if isinstance(ratio, float) and not ratio.is_integer():
        return None
    ratio = int(ratio)
    # 1 divides everything, so it can never fail this check and is not worth a branch downstream.
    return ratio if ratio >= 2 else None


def _encode_alignment_problem(vae, height, width):
    """Why this VAE cannot encode a frame of this size, or None when it can.

    THE SIZE CHECKED IS THE ONE AFTER ComfyUI'S OWN CROP, and that is what keeps this guard from
    refusing work that would have succeeded. `vae_encode_crop_pixels` (sd.py:1069) narrows both
    spatial dimensions to a multiple of this same ratio whenever `crop_input` is set, which is the
    default at sd.py:511 and true of most VAEs. Feed those an odd size and the post-crop size is
    already aligned, so nothing below fires - correctly, because the encode goes on to work. Only a
    VAE that declines the crop arrives here still unaligned, which is exactly the failing case.
    Asking the real function beats reading `crop_input` and re-deriving what it implies.
    """
    align = _spatial_alignment(vae)
    if align is None:
        return None
    bad = [(name, int(v)) for name, v in (("height", height), ("width", width)) if int(v) % align]
    if not bad:
        return None

    parts = []
    for name, v in bad:
        lo, rem = (v // align) * align, v % align
        # A floor of 0 is not a size anyone can use, so it is not offered as one.
        near = (f"the nearest {name}s it accepts are {lo} and {lo + align}" if lo >= align
                else f"the nearest {name} it accepts is {lo + align}")
        parts.append(f"the {name} {v} is not ({v} = {align} x {v // align} + {rem}; {near})")

    return (
        f"OCIO VAE Encode: this VAE encodes in blocks of {align} pixels, and " + ", and ".join(parts)
        + f". Crop or resize the frame to a multiple of {align} on both axes before it reaches this "
        f"node, then encode.\n"
        f"This is the VAE's own requirement and not a limit of this node: the stock VAE Encode "
        f"fails on this same frame. ComfyUI trims an unaligned frame for you only when the VAE asks "
        f"it to (comfy/sd.py:1069-1078), and this one does not - its vae_encode_crop_pixels handed "
        f"the size back unchanged. Without this message the frame reaches the model and fails inside "
        f"einops with \"Shape mismatch, can't divide axis of length ... in chunks of ...\", which "
        f"names an internal axis and never mentions the size you fed it."
    )


# What each latent axis is called, so the message can spell the shape out rather than say "5-D".
# Keyed by the expected rank; anything not listed is described without naming its axes.
_LATENT_AXES = {
    3: "[batch, channels, samples]",
    4: "[batch, channels, height, width]",
    5: "[batch, channels, frames, height, width]",
}


def _decode_shape_problem(vae, latent):
    """Why this VAE cannot decode this latent, or None when it can.

    A latent carries a batch axis and a channel axis on top of the ones `latent_dim` counts, so the
    rank this VAE expects is `latent_dim + 2`: 5 for the video VAEs (sd.py:563, :593, :700 and the
    rest), 4 for a still image (the default, sd.py:498), 3 for audio (sd.py:688).

    TWO CASES ARE LEFT ALONE, both because comfy/sd.py handles them itself and refusing them would
    be a false block:

      * `latent_dim == 2` given a 5-dimensional latent. sd.py:1188 drops the frame axis before
        anything else runs. The reverse has no such line, which is the whole reason for this guard.
      * any VAE with `extra_1d_channel` set (sd.py:855, :930). Those carry an extra axis that
        `latent_dim` does not count - `encode_tiled_1d` reshapes to
        [batch, latent_channels, extra, T] at sd.py:1158 - so the rank rule above does not describe
        them. Both current sites happen to agree with it anyway; the skip is here because the rule
        was not confirmed for them, not because it was confirmed wrong.
    """
    dim = getattr(vae, "latent_dim", None)
    if not isinstance(dim, int) or isinstance(dim, bool):
        return None
    if getattr(vae, "extra_1d_channel", None) is not None:
        return None
    ndim = getattr(latent, "ndim", None)
    if not isinstance(ndim, int):
        return None

    want = dim + 2
    if ndim == want or (dim == 2 and ndim == 5):
        return None

    axes = _LATENT_AXES.get(want)
    wants = f"{want}-dimensional latents {axes}" if axes else f"latents of {want} dimensions"
    shape = "x".join(str(int(s)) for s in latent.shape) if getattr(latent, "shape", None) else "?"
    return (
        f"OCIO VAE Decode: this VAE decodes {wants}, and the latent handed to it has {ndim} "
        f"({shape}). The latent and the VAE do not belong together, which almost always means they "
        f"came from different models. Wire the latent from the model this VAE was loaded with: a "
        f"VAE is trained alongside its own transformer and cannot be paired with another one's "
        f"latents.\n"
        f"This is a mismatch in the graph and not a limit of this node: the stock VAE Decode fails "
        f"on this same pair. comfy/sd.py sizes the decode from the latent's shape before any "
        f"decoding starts (sd.py:1192 whole-frame, sd.py:1282 tiled), and on a video VAE that "
        f"estimate reads shape[4] (sd.py:596), so without this message the job stops at "
        f"\"IndexError: tuple index out of range\", which never mentions the latent at all."
    )


class OCIOVAEDecode:
    """Decode a latent without the 0..1 clamp, optionally in float32."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "samples": ("LATENT", {"tooltip": "The latent to decode, same input the stock VAE Decode takes."}),
                "vae": ("VAE", {"tooltip": "The VAE that belongs to the model. A VAE is trained together with its "
                                          "transformer and cannot be swapped for another one."}),
                "precision": (_PRECISION_CHOICES, {"default": "float32",
                              "tooltip": "float32 is the default and costs 5.2x the model's own dtype: 28.8 s "
                                         "against 5.5 s, 25 frames at 1280x704 tiled 384. float16 applies only "
                                         "where the VAE lists it; LTX does not, so it falls back and the report "
                                         "says so. docs/NODES_VAE.md 3.2."}),
                "clamp": ("BOOLEAN", {"default": False, "label_on": "clamp to 0..1", "label_off": "keep everything",
                          "tooltip": "OFF (default) passes values through as the model produced them, including "
                                     "anything below 0 or above 1. ON reproduces the stock VAE Decode exactly - use "
                                     "it only to compare against the standard path."}),
            },
            # EVERYTHING BELOW IS APPENDED, AND THAT IS NOT TIDINESS. A saved graph stores widget values as a
            # positional, unnamed list, so inserting a widget above an existing one silently reassigns every
            # value after it. They are `optional` for a second reason, read in execution.py:901-913: a
            # `required` input absent from a posted prompt is a hard "Required input is missing" validation
            # error, while an absent `optional` one falls through to this function's own default. Any API
            # workflow already posting this node with two widget values keeps working; as `required` they
            # would all have started failing.
            "optional": {
                "tiled": ("BOOLEAN", {"default": False, "label_on": "tiled", "label_off": "whole frame",
                          "tooltip": "Decode in tiles. Off by default so no saved graph changes. A long clip needs "
                                     "it on: 121 frames untiled at float32 took 912 s against 60 s tiled. Tiling "
                                     "also changes the picture, ~30x what precision does. docs/NODES_VAE.md 3.3."}),
                "tile_size": ("INT", {"default": 384, "min": 64, "max": 4096, "step": 32,
                              "tooltip": "Spatial tile in PIXELS, divided by the VAE's compression ratio (32x on "
                                         "LTX, so 384 becomes 12). 384 measured clean at 60 s for 121 frames; 768 "
                                         "overflowed. If a decode crawls, lower this first. docs/NODES_VAE.md 3.3."}),
                "overlap": ("INT", {"default": 64, "min": 0, "max": 4096, "step": 32,
                            "tooltip": "Spatial overlap between tiles in PIXELS, feathered by the blend. Held to a "
                                       "quarter of the tile, ComfyUI's own convention (comfy/sd.py:1234). Spatial "
                                       "seams are not the problem: measured gradient excess is 1.03-1.05x."}),
                "temporal_size": ("INT", {"default": 4096, "min": 8, "max": 4096, "step": 8,
                                  "tooltip": "TILE SPACE, NEVER TIME. The 4096 default exceeds any clip on purpose, "
                                             "making the whole sequence one temporal tile so no time tiling "
                                             "happens. Lowering it leaves a visibly soft frame at a fixed period. "
                                             "docs/NODES_VAE.md 3.3."}),
                "temporal_overlap": ("INT", {"default": 32, "min": 4, "max": 4096, "step": 8,
                                     "tooltip": "Temporal overlap in PIXEL frames, used only when temporal_size is "
                                                "small enough to split the clip. Held to half the temporal tile. It "
                                                "cannot repair the soft frame: a wider blend spreads the weak edge "
                                                "over more frames instead of removing it."}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image/sequence/video", "range report")
    OUTPUT_TOOLTIPS = ("The decoded frames, unclamped unless you asked otherwise. What the values MEAN depends on "
                       "how the generation encoded them: display RGB for SDR, ACEScct log codes on LTX-2's HDR path.",
                       "min, max, mean, percentiles, and the exact share of samples below 0 and above 1 - the range "
                       "the standard clamp would have destroyed. Also carries a note for anything the node declined "
                       "to do, which would otherwise reach the server log only.")
    FUNCTION = "decode"
    CATEGORY = "OCIO"
    DESCRIPTION = ("Decode a latent without the 0..1 clamp, optionally with the VAE in float32, with spatial "
                   "tiling for long clips and a range report on a second output. With clamp ON it reproduces the "
                   "stock VAE Decode bit for bit, so it is a safe drop-in. Measured honestly: on an SDR "
                   "generation the clamp costs 0.06% of samples (+0.05 above white), and on real ACEScct HDR "
                   "material carrying +3.9 stops it costs nothing at all, because 15x white only reaches code "
                   "0.80. The clamp removes range only above code 1.0, i.e. linear past ~222. Use this for "
                   "control over the decode, not as a rescue for dynamic range the model never produced. Tile "
                   "SPACE and not time: a temporal tile edge leaves a visibly soft frame, a spatial one does not.")

    def decode(self, samples, vae, precision="float32", clamp=False, tiled=False, tile_size=384,
               overlap=64, temporal_size=4096, temporal_overlap=32):
        latent = samples["samples"]
        # Nested latents (audio-video models) carry the video track first, same as the stock node.
        if getattr(latent, "is_nested", False):
            latent = latent.unbind()[0]

        # CHECKED ON THE TENSOR THAT WILL BE DECODED, hence after the unbind above and not before it:
        # the nested wrapper's own rank is not the rank the VAE will see. Raised here, before the
        # `try` below, so there is no patched transform or raised precision to put back - nothing has
        # been touched yet.
        problem = _decode_shape_problem(vae, latent)
        if problem is not None:
            raise ValueError(problem)

        import logging

        saved_output = vae.process_output
        saved_dtype = getattr(vae, "vae_dtype", None)
        raised_precision = False
        notes = []
        try:
            # ASK THE VAE WHAT ITS TRANSFORM IS, do not assume the default. See _probe_process_output: five
            # VAEs in comfy/sd.py emit [0,1] natively and set process_output to the identity, and replacing
            # that with (x+1)/2 rescales an already-correct image into [0.5, 1.0]. Only the shape we
            # recognise is replaced; anything else is left exactly as the VAE set it.
            shape = _probe_process_output(vae)
            if shape == _DEFAULT_SHAPE:
                # The stock post-step is add(1)/div(2) followed by clamp(0, 1). Keep the mapping, drop the
                # clamp: the [-1, 1] convention is how the VAE was trained, the clamp is a display decision.
                #
                # THE OPERATIONS MUST BE IN-PLACE (add_ / div_), and this is not style. comfy/sd.py:1215
                # calls `self.process_output(pixel_samples[x:x+batch_number])` and THROWS THE RETURN VALUE
                # AWAY - it relies on the stock lambda mutating the tensor. An out-of-place `add(1).div(2)`
                # therefore left the raw VAE output untouched, so the node emitted values in the VAE's native
                # [-1, 1] instead of [0, 1]: three quarters of a dark frame came out negative, blacks crushed
                # and highlights burnt. Nothing caught it but looking at the picture. Other call sites
                # (sd.py:1104/1119/1123/1127) DO use the return value, and in-place ops satisfy both, since
                # they mutate and return the same tensor. Fixed 2026-08-12.
                if clamp:
                    vae.process_output = lambda image: image.add_(1.0).div_(2.0).clamp_(0.0, 1.0)
                else:
                    vae.process_output = lambda image: image.add_(1.0).div_(2.0)
            elif shape == _IDENTITY_SHAPE:
                # Nothing to remove: this decoder passes its output through untouched, so there is no clamp in
                # the way and the values already arrive as they were produced. Left alone deliberately.
                #
                # THE WORDING IS DELIBERATELY NOT "emits 0..1", which is what it used to say (corrected
                # 2026-08-13). comfy/sd.py sets process_output to the identity in at least eleven places, and
                # only five are image decoders that really do finalise to [0,1] (540, 894, 895, 906, 976). The
                # other six are AUDIO VAEs (689, 851, 881, 926, 952, 1003), which take the same VAE socket and
                # land here too - and telling someone their audio "emits 0..1 natively" is simply false. What is
                # true of every one of them is that the transform is a pass-through.
                logging.info("OCIO VAE Decode: this VAE's process_output is a pass-through (the identity - e.g. "
                             "TAEHV at comfy/sd.py:894, and every audio VAE). Nothing was clamping its output, "
                             "so nothing was changed and the 'clamp' switch has no effect here.")
                notes.append("this VAE's process_output is a pass-through (the identity); nothing was clamping "
                             "its output, so 'clamp' had no effect")
            else:
                logging.warning("OCIO VAE Decode: this VAE's process_output is not a shape this node "
                                "recognises, so it was left untouched and the 'clamp' switch had no effect. "
                                "The decode is exactly the stock one. Report the VAE if you expected "
                                "unclamped output from it.")
                notes.append("this VAE's process_output is not a shape this node recognises, so it was left "
                             "untouched and 'clamp' had no effect - the decode is exactly the stock one")

            # Precision is dispatched through _PRECISION_DTYPES, never by comparing the widget's string
            # to one literal - see that mapping for why. Both nodes call this same function.
            raised_precision = _request_dtype(vae, precision, "OCIO VAE Decode", notes)

            # TILED OR WHOLE-FRAME, THROUGH THE SAME BRANCHES ABOVE. Both calls run with the same
            # process_output and the same dtype, and both are inside the one `finally`, so neither can
            # leak a patched transform or a raised precision into the next graph in this session.
            #
            # The two entry points are not interchangeable in one respect worth knowing: `vae.decode`
            # falls back to tiled decoding by itself when it runs out of memory (comfy/sd.py:1216-1223),
            # while `vae.decode_tiled` has no such safety net - it is already the fallback. So OFF is
            # not "no tiling ever", it is "tile only if forced"; ON is "tile to these sizes".
            tile_kwargs = None
            if tiled:
                tile_kwargs, tile_notes = _tiling_kwargs(vae, tile_size, overlap, temporal_size,
                                                         temporal_overlap)
                notes.extend(tile_notes)
                for n in tile_notes:
                    logging.info("OCIO VAE Decode: %s", n)
            if tile_kwargs is not None:
                images = vae.decode_tiled(latent, **tile_kwargs)
            else:
                images = vae.decode(latent)
        finally:
            vae.process_output = saved_output
            if raised_precision:
                vae.vae_dtype = saved_dtype
                vae.first_stage_model.to(saved_dtype)

        images = _to_comfy_image(images)
        report = _range_report(images, f"OCIO VAE Decode ({precision}, "
                                      f"{'clamped to 0..1' if clamp else 'no clamp'}, "
                                      f"{'tiled' if tile_kwargs is not None else 'whole frame'})")
        for n in notes:
            report += f"\n  note: {n}"
        logging.info("OCIO VAE Decode: %s", report.replace("\n", " "))
        return (images, report)


class OCIOVAEEncode:
    """Encode an image to a latent, reporting what the standard path does silently.

    RESPONSIBLE FOR: the entry point of the OCIO family into a sampler (2026-08-12).

    WHY THIS EXISTS. It is the counterpart of OCIO VAE Decode, so a graph can go
    OCIO Read -> OCIO VAE Encode -> sampler -> OCIO VAE Decode -> OCIO Write without dropping out of
    the family halfway. That consistency is the stated reason it was asked for, and it is a real one:
    the stock node sits in a different category, has different widgets, and gives no control over
    precision. But a node that only re-badges an existing one would be dead weight, so here is
    everything it adds that the stock VAEEncode does not do, each of them confirmed by reading
    comfy/sd.py rather than assumed:

      1. IT TELLS YOU WHEN YOUR VALUES ARE OUTSIDE THE VAE'S DOMAIN. comfy/sd.py:501 sets
         `process_input = lambda image: image * 2.0 - 1.0`, with NO clamp, so a value of 4.0 arrives
         at the VAE as 7.0. The VAE was trained on [-1, 1]; anything past that is outside its
         distribution and what comes back is not defined by anything. The stock node passes it
         through without a word. This one counts it and says so.
      2. IT TELLS YOU WHEN YOUR IMAGE WAS CROPPED. sd.py:1315 calls `vae_encode_crop_pixels`, which
         narrows each spatial dimension down to a multiple of the VAE's compression ratio - silently.
         Same class of defect as comfy_extras/nodes_lt.py:82 turning a requested 720 into 704.
      3. IT LETS YOU RUN THE ENCODE AT float32, which the stock node cannot. Mirrors the decode node.

    WHAT IT DELIBERATELY DOES NOT DO. It does not convert colour. Encoding to ACEScct before the VAE
    is a colour operation and belongs in OCIO LogConvert, where it is visible in the graph and can be
    inspected; hiding a curve inside a VAE node is how footage gets mangled without a trace. The
    decode node makes the same choice in the other direction, and for the same reason.

    THE RANGE GUARD FAILS OPEN, AND THAT IS A DECISION, NOT AN OVERSIGHT. The three states are
    report / clamp / raise, never two. 'report only' is the default because it is strictly better than
    the status quo and cannot break a graph that works today: the stock node already passes
    out-of-range values through, so reporting them adds information and removes nothing. Raising by
    default would change behaviour for existing graphs and, worse, would push people back to the stock
    node - defeating the point of having a consistent family at all. Pick 'raise an error' when you
    want the strictness of a reference implementation and would rather lose the job than the evening.

    HOW THE GUARD IS IMPLEMENTED, AND WHY NOT THE OBVIOUS WAY. It inspects the IMAGE directly before
    calling `vae.encode`, and does NOT monkey-patch `process_input`. Two reasons, both from the source:
    `process_input` is not the same function for every VAE (sd.py:539, 894 and 906 install an identity
    instead of the [-1, 1] mapping), so a patch would have to know which one it replaced; and sd.py
    calls it on a SLICE of the caller's tensor at line 1333, `self.process_input(pixel_samples[x:x + n])`,
    USING the return value. That is the exact opposite of the decode contract, where sd.py:1215 throws
    the return away and the lambda must mutate in place. Getting that backwards is not theoretical: it
    is the bug that made this pack emit raw [-1, 1] values, and nothing caught it but looking at the
    picture. So on the encode side an in-place operation would corrupt the caller's own IMAGE tensor,
    which may still be wired somewhere else in the graph. Touching nothing avoids both traps.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "pixels": ("IMAGE", {"tooltip": "The image or sequence to encode. Values are expected in 0..1; "
                                                "anything outside that is outside the VAE's training domain and "
                                                "is handled according to 'out_of_range'."}),
                "vae": ("VAE", {"tooltip": "The VAE that belongs to the model. A VAE is trained together with its "
                                          "transformer and cannot be swapped for another one."}),
                "precision": (_PRECISION_CHOICES, {"default": "float32",
                              "tooltip": "float32 is the default and costs 1.8x the model's own dtype: 5.4 s "
                                         "against 3.0 s, 25 frames at 1280x704. float16 applies only where the VAE "
                                         "lists it; LTX does not, so it falls back and the report says so. "
                                         "docs/NODES_VAE.md 3.2."}),
                "out_of_range": (["report only", "clamp to 0..1", "raise an error"], {"default": "report only",
                                 "tooltip": "Values outside 0..1 are outside the VAE's training domain. 'report "
                                            "only' matches the stock node but tells you. 'clamp to 0..1' keeps the "
                                            "VAE in domain. 'raise an error' stops the job. docs/NODES_VAE.md 3.4."}),
            }
        }

    # THE REPORT IS APPENDED, never inserted: an output link is stored by SLOT INDEX, so the latent stays at 0
    # and every saved graph keeps its wire. It exists because this node's findings used to reach the server log
    # only, where an artist working in the canvas never sees them, while the decode has carried the same kind of
    # report on a wire since it was written. Same information, same place, both directions.
    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("latent", "input report")
    OUTPUT_TOOLTIPS = ("The encoded latent, ready for a sampler.",
                       "What the encoder was handed: the input range, the share outside the VAE's 0..1 training "
                       "domain and what was done about it, any silent crop, and the precision actually used.")
    FUNCTION = "encode"
    CATEGORY = "OCIO"
    DESCRIPTION = ("Encode an image to a latent as the entry point of the OCIO family, so a graph does not have to "
                   "leave it between Read and Write. Adds three things the stock VAE Encode does not: it reports "
                   "values outside the VAE's 0..1 training domain instead of passing them through in silence "
                   "(comfy/sd.py:501 applies image*2-1 with no clamp), it reports the silent crop to the VAE's "
                   "compression ratio (sd.py:1315), and it can run the encode at float32. It does NOT convert "
                   "colour - put OCIO LogConvert in front of it for that, where the curve is visible in the graph.")

    def encode(self, pixels, vae, precision="float32", out_of_range="report only"):
        import logging

        notes = []

        n = int(pixels.numel())
        lo, hi = float(pixels.min()), float(pixels.max())
        below = int((pixels < 0.0).sum())
        above = int((pixels > 1.0).sum())
        if below or above:
            msg = (f"OCIO VAE Encode: {below + above} of {n} samples ({(below + above) / max(n, 1) * 100:.4f}%) "
                   f"are outside 0..1 (range {lo:+.4f} .. {hi:.4f}; {below} below 0, {above} above 1). "
                   f"comfy/sd.py:501 maps them with image*2-1 and does NOT clamp, so the VAE receives values "
                   f"outside the [-1, 1] it was trained on.")
            if out_of_range == "raise an error":
                raise ValueError(msg + " Set 'out_of_range' to 'clamp to 0..1' or 'report only' to continue.")
            if out_of_range == "clamp to 0..1":
                pixels = pixels.clamp(0.0, 1.0)
                logging.warning(msg + " Clamped to 0..1 before encoding, as asked.")
                notes.append(f"{below + above} of {n} samples were outside 0..1 and were CLAMPED before encoding, "
                             f"as asked")
            else:
                logging.warning(msg + " Passing through unchanged, same as the stock node.")
                notes.append(f"{below + above} of {n} samples were outside 0..1 and were passed through unchanged, "
                             f"same as the stock node - sd.py:501 maps them with image*2-1 and does not clamp")

        # The crop is reported, not prevented: preventing it would mean resizing the artist's image, which is a
        # bigger decision than this node gets to make. sd.py:1315 does it either way; the point is that you know.
        in_h, in_w = int(pixels.shape[-3]), int(pixels.shape[-2])
        cropped = vae.vae_encode_crop_pixels(pixels)
        out_h, out_w = int(cropped.shape[-3]), int(cropped.shape[-2])
        if (out_h, out_w) != (in_h, in_w):
            notes.append(f"{in_w}x{in_h} was CROPPED to {out_w}x{out_h} to reach a multiple of the VAE's "
                         f"compression ratio; sd.py:1315 does this silently either way")
            logging.warning(f"OCIO VAE Encode: {in_w}x{in_h} was cropped to {out_w}x{out_h} to reach a multiple of "
                            f"the VAE's compression ratio (comfy/sd.py:1315 does this silently). Feed dimensions "
                            f"that are already a multiple if you need every pixel.")

        # THE POST-CROP SIZE IS THE ONE THAT REACHES THE MODEL, so it is the one checked - see
        # _encode_alignment_problem. A VAE that crops has already fixed its own alignment by this
        # line and cannot be refused here; only one that declines the crop can still be unaligned.
        # Raised before the precision block below, so no dtype has been changed to restore.
        problem = _encode_alignment_problem(vae, out_h, out_w)
        if problem is not None:
            raise ValueError(problem)

        saved_dtype = getattr(vae, "vae_dtype", None)
        raised_precision = False
        try:
            # The decode's precision handling used to be duplicated here, and the copy drifted badly
            # enough that this node lacked both of the decode's guards for months. There is now ONE
            # implementation, shared, dispatched through _PRECISION_DTYPES rather than by comparing the
            # widget to a literal string. Read _request_dtype for the cast-before-flag ordering, which
            # is what stops a refused cast leaving the VAE claiming a precision it does not have.
            raised_precision = _request_dtype(vae, precision, "OCIO VAE Encode", notes)
            samples = vae.encode(pixels)
        finally:
            if raised_precision:
                vae.vae_dtype = saved_dtype
                vae.first_stage_model.to(saved_dtype)

        report = _range_report(pixels, "OCIO VAE Encode, input handed to the VAE")
        if raised_precision:
            notes.append("the encode ran at float32; the VAE was restored to its own precision afterwards")
        if notes:
            report += "\n  " + "\n  ".join("note: " + t for t in notes)
        return ({"samples": samples}, report)


NODE_CLASS_MAPPINGS = {"OCIOVAEDecode": OCIOVAEDecode, "OCIOVAEEncode": OCIOVAEEncode}
NODE_DISPLAY_NAME_MAPPINGS = {"OCIOVAEDecode": "OCIO VAE Decode", "OCIOVAEEncode": "OCIO VAE Encode"}
