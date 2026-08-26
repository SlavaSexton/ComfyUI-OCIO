# OCIO VAE Decode and OCIO VAE Encode

Two nodes, `OCIOVAEDecode` and `OCIOVAEEncode`, implemented in `vae_nodes.py`. They sit where a latent
becomes pixels and where pixels become a latent. Everything else in this pack works on pixels, so these
are the two places the pack has to reach into ComfyUI's own VAE object, and that is why they need a page
of their own rather than a bullet in the README.

This page is written so nothing here has to be taken on trust. Every widget name, type, default and combo
value below was read from the running server's `/object_info` for the two nodes, not from the source and
not from memory. Every line number is against ComfyUI's own `comfy/sd.py`, `comfy/utils.py` and `nodes.py`
as installed next to this pack, and each one was opened and read. Numbers that came from a measurement say
whose measurement and under what conditions. Where something was not verified, it says so, in a section at
the end.

---

## 1. Why these nodes exist

### The clamp, and where it actually lives

ComfyUI's default VAE output transform is one line, at `comfy/sd.py:502`:

```python
self.process_output = lambda image: image.add_(1.0).div_(2.0).clamp_(0.0, 1.0)
```

The `add_(1).div_(2)` half is the VAE's own convention. These models are trained to produce values in
`[-1, 1]`, and that mapping moves them to `[0, 1]`. It is not a choice, it is the model's contract. The
`.clamp_(0.0, 1.0)` on the end is a different kind of decision. It is a display decision, and it is
irreversible.

### The magnitude of that decision, measured

The argument above is structural; this section is the measurement. One LTX-2.5 generation, one latent,
decoded twice with the decode path as the only variable. Each frame is 2 703 360 samples, and both files
were read back off disk rather than sampled in memory:

| decode | range | samples below 0 | samples above 1 | distinct values | file |
| --- | --- | --- | --- | --- | --- |
| **OCIO VAE Decode**, float32, no clamp | −1.02126 … +1.08093 | **2 109 178** | 2 021 | **2 387 986** | 10.2 MB |
| stock `VAEDecode` | 0.00000 … 1.00000 | 0 | 0 | 3 301 | 2.7 MB |

**78.0% of the frame lay below zero**, and the clamp maps every one of those samples to exactly 0.0. The
operation is not a soft roll-off or a compression of range; it is a projection onto the boundary, and a
projection is not injective. Once applied, the pre-image cannot be recovered from the result, which is why
no downstream float container restores it: the information is destroyed before the tensor is written, not
after.

The distinct-value counts state the same loss in the domain rather than the range. 3 301 against 2 387 986
is a reduction of roughly 723x in the number of representable states actually present in the frame. File
size follows from that, and is the same fact a third time: an entropy coder compresses the clamped frame to
a quarter of the size because there is materially less information left in it to encode.

That table conflates two independent variables, which is a weakness in it, so they are separated here. The
same latent, three decodes, with precision held constant at the VAE's own bf16:

| decode | range | samples below 0 | samples above 1 |
| --- | --- | --- | --- |
| ours, `clamp` off | −0.01172 … +1.03906 | 16 | 1 695 |
| ours, `clamp` on | 0.00000 … 1.00000 | 0 | 0 |
| stock `VAEDecode` | 0.00000 … 1.00000 | 0 | 0 |

With the clamp reinstated our output is identical to the stock output. This is the controlled comparison:
holding dtype fixed isolates the clamp as the sole cause of the difference in the first two rows, and the
third row establishes that our path introduces no other divergence from the reference implementation. The
residual difference between the two tables is therefore attributable to precision, `float32` against bf16,
which section 3.2 quantifies separately.

The `clamp` widget exists for exactly this reason. A claim of this kind should be falsifiable by the reader
on their own material in a single toggle, rather than accepted on the authority of a table.

Replication across scene content, since a single frame is a sample of one: a city exterior returned
−0.01953 … +1.04297 with 1 355 samples above white, and an interior returned −0.00391 … +1.03906 with 601.
The stock path returned exactly 0.00000 … 1.00000 on both, with zero samples outside the interval on either
side. The magnitude of the out-of-range content is content-dependent, as expected; its presence is not.

One correction to how this is usually stated, including in this pack's own README. The clamp is not in the
body of the stock `VAEDecode` node. That node is nine lines (`nodes.py:313-338`) and all it does is call
`vae.decode(latent)`. The clamp is applied inside, by the VAE object, at `comfy/sd.py:1215`:

```python
self.process_output(pixel_samples[x:x+batch_number])
```

and on the tiled paths at `sd.py:1104`, `:1119`, `:1123` and `:1127`. The net effect for the common case is
exactly what people say it is: by the time a tensor leaves the stock node, everything above 1.0 and below
0.0 is gone. But the distinction matters twice over. It is why this pack could not fix the problem by
wrapping the node, and it is why the node has to ask the VAE what its transform is instead of assuming.
Eleven other places in that same file install the identity instead of the line above, and on those there is
no clamp for this node to remove. See trap 2.

**An identity transform does not prove the values are free**, and that is worth stating plainly because the
sentence above invites the opposite reading. It says only that the *wrapper* is not clamping. A model can
clamp inside its own decoder, before the wrapper is ever reached, and MiniMax H3 does exactly that:
`comfy/ldm/minimax/vae.py:398-401` ends every decode in `.clamp_(0.0, 1.0)`, which is why `sd.py:972` can
set `process_output` to the identity at all - the pixels arrive already in range. Confirmed by following all
three entry points: `decode()` either finalises a single frame itself or hands off to `decode_temporal()`,
which finalises each chunk, and `decode_tiled()` just calls `decode()`. Nothing reaches a caller unclamped.

So on H3 this node cannot give you a range above white, because there is no point in the chain where that
range still exists. What it does give there is the rest of the argument on this page: float32 with no 8-bit
quantisation and no compressor between the model and the file, and colour management on the way out. The
distinction is worth knowing before you go looking for highlights that were never handed over.

What "gone" means: the clamp writes over the tensor in place. There is no second copy, no header flag, no
way to reconstruct the value that was there. Writing a 32-bit float EXR afterwards preserves a number that
has already been flattened to 1.0. The precision and the range both have to exist at decode time or they do
not exist at all.

### How much that is worth, measured, which is less than it sounds

This is the part that is usually oversold, so here it is with the numbers from the measurements recorded in
`vae_nodes.py`'s own docstrings.

On an ordinary SDR generation the clamp costs very little. One LTX-2.5 latent, decoded three ways at the
same precision and written to raw EXR: the clamp changed **0.06% of samples**, and the overshoot reached
**+0.05 above white and -0.03 below black**, which is roughly a twentieth of a stop. A dark interior with a
blown window, a street of glass towers throwing speculars, and Lightricks' own demo prompt all landed inside
that band. The decoded values of an SDR generation are display-referred. They do not carry more than that,
whatever the prompt asked for.

On real HDR film material the clamp costs nothing at all, for an arithmetic reason worth understanding. A
10-bit ADX10 DPX scan carrying **+3.9 stops above diffuse white** (3.15% of samples above 1.0, peaking near
15x white) was encoded to ACEScct, pushed through the VAE, and decoded both ways. The HDR survives the VAE:
**3.128% of samples came back above 1.0 against 3.148% going in**. But unclamped and clamped came out the
same to within the VAE's own reconstruction error (median error 3.24% against 3.21% overall, 5.6% against
5.5% in the highlights, p90 about 20%). The reason is that **ACEScct code 1.0 is roughly linear 222**, so
15x white only reaches code 0.80, and a 0..1 clamp has nothing to remove. The VAE's own error dominates by
an order of magnitude.

So the honest statement of purpose is not "this rescues your dynamic range". It is:

- **Control, with a verified floor.** With `clamp` on, the decode reproduces the stock one. Switching your
  graph to this node changes nothing until you ask it to.
- **Material that genuinely exceeds code 1.0**, and any downstream operation that wants headroom rather than
  a ceiling baked in before it runs. In ACEScct that means linear above roughly 222, which is 200x white,
  not 15x.
- **Precision you can choose.** The stock node gives you no say in the VAE's dtype. This one does.
- **Visibility.** A second output tells you what the decode produced, including exactly what a clamp would
  have taken, before you commit it to a file.

The encode node exists for a smaller and more practical reason: so a graph can run OCIO Read, OCIO VAE
Encode, a sampler, OCIO VAE Decode, OCIO Write without leaving the pack halfway. On top of that it reports
two things the stock encode does silently. See section 3.4.

---

## 2. Inputs and outputs

Read from `GET /object_info/OCIOVAEDecode` and `GET /object_info/OCIOVAEEncode` on the running server.
Category for both is `OCIO`. Neither is an output node.

### OCIO VAE Decode, inputs

| Name | Type | Default | Required | Wire in |
| --- | --- | --- | --- | --- |
| `samples` | `LATENT` | none | yes | Anything that emits `LATENT`. A sampler (`KSampler` and its family), an `Empty*Latent*` node, or one of the LTXV latent builders (`LTXVImgToVideo`, `LTXVAddGuide`, `LTXVLatentUpsampler`). Also `OCIO VAE Encode`'s own output, for a round trip. |
| `vae` | `VAE` | none | yes | `VAELoader` for a standalone VAE file, or the `VAE` output of a checkpoint loader (`CheckpointLoaderSimple`, `CheckpointLoader`, `ImageOnlyCheckpointLoader`, `unCLIPCheckpointLoader`, `DiffusersLoader`). Third-party loaders that emit `VAE` work too. It must be the VAE that belongs to the model that made the latent. See trap 1. |
| `precision` | combo `["float32", "float16"]` | `float32` | yes | Widget. Section 3.2. |
| `clamp` | `BOOLEAN` (`clamp to 0..1` / `keep everything`) | `False` | yes | Widget. Section 3.1. |
| `tiled` | `BOOLEAN` (`tiled` / `whole frame`) | `False` | no | Widget. Section 3.3. |
| `tile_size` | `INT`, min 64, max 4096, step 32 | `384` | no | Widget, in pixels. Section 3.3. |
| `overlap` | `INT`, min 0, max 4096, step 32 | `64` | no | Widget, in pixels. Section 3.3. |
| `temporal_size` | `INT`, min 8, max 4096, step 8 | `4096` | no | Widget, in pixel frames. Section 3.3. |
| `temporal_overlap` | `INT`, min 4, max 4096, step 8 | `32` | no | Widget, in pixel frames. Section 3.3. |

The five tiling widgets are declared `optional`, and that is deliberate. `execution.py:901-913` treats a
missing `required` input in a posted prompt as a hard validation failure, while a missing `optional` one
falls through to the function's own default. Any API workflow that was posting this node with two widget
values before tiling existed keeps working. They are also appended after the existing widgets rather than
inserted, because a saved graph stores widget values as an unnamed positional list, so inserting one above
another silently reassigns every value after it.

### OCIO VAE Decode, outputs

| Slot | Name | Type | Wire out |
| --- | --- | --- | --- |
| 0 | `image/sequence/video` | `IMAGE` | Anything taking `IMAGE`. In this pack: `OCIO LogConvert`, `OCIO ColorSpace`, `OCIO Display`, `OCIO CDLTransform`, `OCIO LookTransform`, `OCIO FileTransform`, `OCIO Player`, `OCIO Write` (its `images` input). Stock nodes such as `SaveImage` and `PreviewImage` also accept it, but they expect 0..1 and will clip on display. |
| 1 | `range report` | `STRING` | Any string display or note node. Section 4. |

The report is slot 1, after the image, so adding it did not move an existing wire in any saved graph.

### OCIO VAE Encode, inputs

| Name | Type | Default | Required | Wire in |
| --- | --- | --- | --- | --- |
| `pixels` | `IMAGE` | none | yes | `OCIO Read` (its `image/sequence/video` output), `LoadImage`, any of the OCIO transform nodes, or `OCIO VAE Decode` itself. Values are expected in 0..1. |
| `vae` | `VAE` | none | yes | Same sources as the decode's `vae`. |
| `precision` | combo `["float32", "float16"]` | `float32` | yes | Widget. Section 3.2. |
| `out_of_range` | combo `["report only", "clamp to 0..1", "raise an error"]` | `report only` | yes | Widget. Section 3.4. |

### OCIO VAE Encode, outputs

| Slot | Name | Type | Wire out |
| --- | --- | --- | --- |
| 0 | `latent` | `LATENT` | A sampler's `latent_image` input, an LTXV guide node, or `OCIO VAE Decode`'s `samples` for a round trip. |
| 1 | `input report` | `STRING` | A `PreviewAny`, a note node, or nothing. Carries the out-of-range count and the crop, so the findings reach the canvas rather than only the server log. |

### Where OCIO VAE Encode belongs, and the graphs where it cannot go

The decode belongs in every graph this pack touches, because every generation ends in one. The encode is
narrower, and it is worth saying plainly where it earns its place, because the answer is not "wherever the
decode is".

**It goes wherever a stock `VAEEncode` currently sits.** That is the whole rule. Concretely: video to
video, image to image over a plate you shot, a latent upscale of your own material, inpainting, and any
graph where footage rather than noise is what enters the sampler. There the chain runs
`OCIO Read -> OCIO LogConvert -> OCIO VAE Encode -> sampler -> OCIO VAE Decode -> OCIO LogConvert ->
OCIO Write` and never leaves the family.

**It cannot replace a model's own image-to-video node, and that is about their shape, not ours.** LTX-2.5's
`LTXVImgToVideoInplace` (`comfy_extras/nodes_lt.py:132`) calls `vae.encode`, but it also writes the result
into an existing latent (`samples[:, :, :t.shape[2]] = t`) and builds the `noise_mask` that tells the
sampler which frames to leave unnoised. Take that node out and image-to-video stops working; no stock node
assembles that mask on its own. The same is true of any wrapper that folds an encode into a larger job.

**In those graphs it still has a use, as a measurement beside the encode rather than in place of it.**
Wire the same pixels the model's node receives into `pixels`, the same VAE into `vae`, and `input report`
into a `PreviewAny`. Nothing downstream changes and the sampler still runs on the model's own latent, but
you now get an answer to a question nobody else in the graph asks: did the values handed to the VAE leave
the domain it was trained on. Two notes on doing it: the report has to be wired somewhere or the node
never executes, since ComfyUI only evaluates a node whose output something needs; and the cost is one
extra `vae.encode` per frame, which is negligible for a single still and is not for a long clip.

That question is not academic when this pack is in the graph. `comfy/sd.py:501` maps input with
`image * 2.0 - 1.0` and **no clamp**, so a pixel at 4.0 arrives at the VAE as 7.0, while the model was
trained on `[-1, 1]`. Feeding ACEScct rather than display sRGB is exactly the situation where that can
happen without anyone noticing, and the stock node says nothing at all.

---

## 3. What each widget actually decides

### 3.1 `clamp`, default off

Off, the decode keeps every value the model produced, including below 0 and above 1. On, the decode
reproduces the stock path.

That second setting exists for one job: comparison. Turn it on when you want to measure what the clamp costs
on your own material, or when you suspect this node of changing something it should not have. The
measurement recorded in `vae_nodes.py` says a clamped decode from this node came out bit-identical to the
stock node's output on a VAE using the default transform, which is what makes the node a safe drop-in. That
identity was not re-measured for this page.

Two things the switch does not do. It does not add range that the model never produced, for the arithmetic
in section 1. And on a VAE whose output transform is not the default shape it has no effect at all, because
there is no clamp in the path to remove. When that happens the node says so, in the range report and in the
log. See trap 2.

### 3.2 `precision`, default `float32`

This chooses the dtype the VAE's weights run at for the duration of this one node, and restores it
afterwards.

The model's own dtype is not float32. For the LTX-2.5 video VAE installed here, `comfy/sd.py:602` lists
`working_dtypes = [torch.bfloat16, torch.float32]` and ComfyUI loads it at bfloat16. That was confirmed
three ways in the measurement recorded in `vae_nodes.py`: the line above, ComfyUI's own startup log
(`VAE load device ... dtype: torch.bfloat16`), and the pixels, where at that dtype **100.000% of
samples are exactly bfloat16-representable with a smallest gap of exactly 2^-8**, against **0.012% at
float32**. One warning from that same note: the `dtype: torch.float32` line in the startup log belongs to
the *audio* VAE (`sd.py:928`), and is easy to misread as the video one.

**What float32 buys.** bfloat16 has 8 bits of mantissa, which quantises the decode to roughly 10 bits.
Measured on one frame: **77 distinct levels in the window [0.2, 0.3]**, smallest step 1/1024, against
**3.35 million distinct levels at float32**. Over 25 frames at 1280x704 the two arms differ at
**99.9974% of samples**, with a **median difference of 0.457 of a 10-bit code step** (p99 1.76, max 14.0).
On the ACEScct HDR frame at 2048x1152, where the clamp never fired in either arm so the comparison is
exact, the median is **0.230 code steps** (p99 1.26, max 6.17). It is not simply output rounding: only
about **23%** of samples equal `round(float32 to bfloat16)`, so the error accumulates through the iterative
decoder. There is a small systematic bias, with float32 reading **8.5e-5 higher**, about 0.09 of a code
step, so bfloat16 sits imperceptibly darker.

In code values the largest divergence measured is **0.010467** with a per-frame median of **0.007161**,
about 2.7 steps of an 8-bit scale, over 25 frame pairs at 1280x704. Those figures understate it in the
shadows. **58% of samples in that clip sit below 0.05**, and there the mean relative error is about **10%**,
which a log or display transfer magnifies. On different material with more headroom the divergence reached
**0.0186** over 121 frames.

**What float32 costs.** Measured on 25 frames at 1280x704 with `tile_size` 384: **5.2 s at the model's own dtype
against 26.4 s at float32, a factor of 5.1**. Conditions matter here and are part of the number: that run
was on a machine with two 24 GB cards that already had about 6.5 GB held by other processes, so it is not a
clean-card figure. Untiled it is far worse, and section 3.3 has that story.

**Re-measured 2026-08-13, on near-idle cards, and the ratio held.** Timed per node from the server's own
websocket on the same clip and the same tiling, with every arm decoding a single latent that ComfyUI served
from its execution cache (so the input was identical by the server's account, not by assumption):
**5.96 s at the model's own dtype against 29.90 s at float32, a factor of 5.02**. About 1.3 GB was held on each
card this time rather than 6.5 GB, so the friendlier condition bought the ratio nothing.

**Read that factor as "about 5x", not as three digits.** Two separate runs through the server gave
**5.02x** (5.96 s against 29.90 s) and **5.28x** (5.65 s against 29.82 s), and an in-process run on
different material gave **5.11x** (5.54 s against 28.34 s). The float32 arm is the stable one at
28.3-29.9 s; the model-default arm is what moves, over 4.88-5.96 s across six observations, so pairing a
fast bfloat16 run against a slow float32 one can read as high as 6.1x and the reverse as low as 4.8x. What
survives every pairing is that float32 costs several times the model's own dtype.

**The encode's cost, measured separately.** It is much cheaper than the decode and it is not free:
**2.99 s at the model's own dtype against 5.37 s at float32, a factor of 1.80** (repeat 3.08 s), same clip and
same path.

**The decision, and it changed.** `float32` is now the default on both nodes, and it is the expensive
one. The reasoning: a colour pipeline should state the precision it ran at rather than inherit whichever
dtype the checkpoint's branch of `comfy/sd.py` happened to list first - the same ordering trap described
under float16 below. Pick `float32` for anything you are delivering, and turn tiling on when you do. Pick
`float16` for a review pass: where the VAE lists it you get the faster, finer-quantised decode, and where
it does not - LTX included - it is declined and the decode runs at the model's own dtype, which is the old
cheap path by another name.

An earlier threshold said float32 should become the default only if it landed within **1.5x**. It does
not: about **5x** on the decode, **1.80x** on the encode. That threshold was overridden deliberately
rather than met, and these are the numbers it was overridden with.

#### `model default` was removed, and that breaks saved graphs

The combo used to carry a third entry, `model default`, meaning "leave whatever ComfyUI chose alone", and
it was the default. It is gone. Two consequences worth knowing before opening an old workflow:

- **A saved graph storing `model default` is now invalid.** Posting it is rejected with
  `value_not_in_list` for the whole prompt - not a warning, and not a quiet fall back to the default.
  Re-pick the widget. Confirmed against the running server; the rejection names the offending value.
- **Widget ORDER did not change**, so nothing else in a saved graph shifts. `widgets_values` is
  positional across all widgets, and this removed a VALUE from one combo rather than moving, inserting or
  deleting a widget. That one value has to be re-picked, and nothing else.

The behaviour it provided did not leave with the entry: on a VAE that does not list float16, selecting
`float16` is declined and the decode runs at the model's own dtype, producing pixels bit for bit identical
to what `model default` produced there.

### `float16`, the other option

**It exists because these nodes decode whatever VAE is wired into them, not only LTX.** An earlier
version of this page argued against offering it, from a census that answered the wrong question: it
established that no VAE in `comfy/sd.py` refuses float32, which says nothing about whether an artist ever
wants float16. Re-read with every assignment attributed to its own branch, `comfy/sd.py` says:

| Property | Count | Lines |
| --- | --- | --- |
| lists `float32` | **23 of 23** | all of them |
| **omits** `float16` | 8 | 503, 584, **602** (the LTX-2.5 VAE installed here), 729, 786, 883, 928, **1005** (MiniMax H3 audio) |
| lists `float16` **first** | 9 | 567, 609, 619, 691, 707, **740**, 838, **972** (MiniMax H3 video), 1019 |
| has **no** `bfloat16` at all | 5 | 707, 883, 928, 972, 1005 |

`model_management.vae_dtype()` at `:1258-1263` walks that list **in order** and returns the first entry the
device supports. Confirmed by calling it here: `vae_dtype([fp16, fp32])` returns `torch.float16`,
`vae_dtype([bf16, fp32])` returns `torch.bfloat16`, `vae_dtype([fp32])` returns `torch.float32`. Three
consequences, each a reason the option is not redundant:

1. **On those 9 float16-first lists, an inherited dtype already resolves to float16** - by accident of list
   ordering, not by anyone's decision. Upstream can reorder a list in any release and the same graph
   silently changes dtype between two runs. Naming the dtype pins it. `sd.py:740` makes the point sharply:
   it is another Lightricks decoder and float16 comes **first** there, while `sd.py:602` has no float16
   at all - two variants of one family resolving differently.
2. **Five lists carry no bfloat16**, so on those models "bfloat16 or float32" is not the choice on offer.
   MiniMax H3's video VAE (`sd.py:972`) is float16/float32; its audio VAE (`sd.py:1005`) is float32 alone.
3. **Where bfloat16 does come first, float16 is a quality gain**, not a compromise - see below.

**What float16 measured**, on the LTX VAE, forced past its own list purely to take the number: 25 frames at
1280x704 decoded cleanly, no NaN and no infinity, and it sat **closer to float32 than bfloat16 does** -
median error **0.000053** against bfloat16's **0.000445**, worst **0.006815** against **0.043412** - at
bfloat16's speed, **5.38 s against 5.54 s**. It quantises far less coarsely: **264 997** distinct values in
a frame against bfloat16's **91 991**. The weights cast safely, largest `|weight|` **5.16** against
float16's ceiling of 65504, with 0.0008% flushing to zero.

**And the limit on that number, which belongs beside it rather than in a footnote.** float16's known
failure is **exponent range**, not mantissa, and it was **not probed**. The attempt proved nothing, because
this decoder normalises the latent's magnitude away: scaling the latent by 10 through 10000 moved the
output's peak by under 0.2 and produced no non-finite value at any of the three dtypes. So "float16 is
better here" rests on **one SDR latent**. Check an HDR master yourself before delivering one at float16.

**The VAE's own list is respected, never overridden.** On the 8 lists without float16 - LTX included - the
request is declined with a note on the range report and the decode runs at the VAE's own precision.
Verified on the real model through `/prompt`: the server accepted `precision = "float16"`, the node
reported `float16 declined: not listed in this VAE's working_dtypes ([torch.bfloat16, torch.float32]), so
it ran at torch.bfloat16`, and the frames came back **identical** to the model's-own-dtype arm (min
-0.082031, max +1.085938, mean +0.679877 in both). The success path was exercised on the same real weights
by widening that list for one experiment: no decline, finite frames, 92.05% of samples differing from
bfloat16 with a maximum of 0.013062, and the VAE restored to bfloat16 afterwards. That is what makes the
option safe on a model nobody here has seen yet.

On the encode side the same widget does the same thing, and the code path is now the same shape: it
consults `working_dtypes` and it catches a refused cast, exactly as the decode does. Both guards were
confirmed here by mutation - break either one and `tests/test_vae_encode.py` goes red naming it.

### 3.3 `tiled`, `tile_size`, `overlap`, `temporal_size`, `temporal_overlap`

#### Tiling is not a free trade

Read this before the settings. Tiling changes the picture, and it changes it by considerably more than the
precision choice does.

Measured on this machine, on 25 real frames at 1280x704, decoding the same latent through this node twice
with the same VAE and the same clamp setting, tiled at `tile_size` 384 with `overlap` 64 against untiled:

| Metric | Value |
| --- | --- |
| per-frame max absolute difference | 0.1467 at frame 1, rising to 0.3171 at frame 25 |
| worst single frame | 0.3239 |
| per-frame mean absolute difference | 2.43e-03 rising to 4.22e-03 |
| share of samples differing by more than 1e-6 | 58% to 62% |

Put that next to the float32 figure from section 3.2, where the largest divergence against the model's own dtype
was 0.0105. **Tiling changes the picture roughly thirty times more than the precision choice does.** If you
were thinking of float32 as the quality decision and tiling as the enabler, that is backwards.

Two things were checked, and are stated as checked rather than assumed:

- **It is not a seam.** The worst pixel in each frame was located and its distance to the nearest tile step
  measured (384 minus 64 overlap gives a 320 px step). On five sampled frames those distances were 9, 142,
  25, 50 and 58 px. Scattered, not aligned to boundaries. This is what a diffusion decoder doing genuinely
  different work per tile looks like, not a blend artefact. The separate seam measurement recorded in
  `vae_nodes.py` agrees from the other direction: gradient excess at a spatial boundary is only
  **1.03x to 1.05x**, meaning no visible seam. Both statements are true and they are different claims. No
  seam does not mean the same picture.
- **The growth across the clip is real.** As a control, the untiled frame's own mean grew 1.07x across the
  25 frames while the max error grew 2.16x, so content brightness does not account for it.

**The cause of that growth is measured and unexplained.** It is not temporal tiling, because at the default
`temporal_size` the whole clip is a single temporal tile. Calling it error accumulation would be a guess and
is not made here.

Practically: choose tiling for VRAM headroom knowing you are also choosing a different decode. Make that
choice once per delivery and keep it. Do not switch it mid-shot, or two halves of the same shot will not
match.

#### When to turn it on

**`tiled` is ON by default since v1.3.0**, and the reasoning that used to put it off is worth keeping rather
than deleting. Off was chosen so that adding this node to a saved graph could not change what that graph
produced, which is a sound instinct. What it missed is which default fails *worse*: whole-frame is the
setting that runs out of memory on a real clip, and it is slower even when it fits - 912 s against 60 s over
121 frames at float32. A default that works and can be switched off beats one that is safe and stops the
render. Saved graphs are unaffected either way, since `widgets_values` carries the value chosen when the node
was added; only newly created nodes pick this up.

Note that off does not mean "never
tiled": `vae.decode` falls back to tiled decoding by itself when it runs out of memory
(`comfy/sd.py:1216-1223`), and this node inherits that because it calls `vae.decode`. So off means "tile
only if forced, at ComfyUI's own choice of sizes", and on means "tile to these sizes". `vae.decode_tiled`,
which the node calls when `tiled` is on, has no such safety net, because it already is the fallback.

Turn it on when the decode does not fit. The measured table, on 121 frames at 1280x704 at float32, from the
measurement recorded in `vae_nodes.py` and credited there to Andrei Orehov:

| `tile_size` | `temporal_size` | Time | Result |
| --- | --- | --- | --- |
| 768 | 4096 | 912 s | float32 exceeds the card, spills into offload, crawls |
| 768 | 32 | 60 s | fast, but a visibly soft frame every 24 frames |
| 384 | 4096 | 60 s | clean |

That was on a 32 GB card. What was observed on the untiled full-resolution float32 run on the 24 GB machine
is narrower and worth stating exactly: it ran long enough that the server's socket listener died
(`Accept failed on a socket ... OSError(22)`), leaving a live process with no port. The prompt still
finished and wrote every frame, so nothing was lost, but the server needed a restart. It was **not** an
out-of-memory condition. It was simply slow.

#### Spatial sizes

`tile_size` and `overlap` are in **pixels**. The node divides them by the VAE's own spatial compression
ratio before calling `decode_tiled`, which counts in latent samples. It asks the VAE for that ratio via
`spacial_compression_decode()` (`sd.py:1426`) rather than assuming one, and the LTX-2 VAE's is 32
(`sd.py:725`).

Verified by running the node's `_tiling_kwargs` against a VAE reporting 32x spatial and 8x temporal:

```
  tile_size 384, overlap 64   gives   tile_x = tile_y = 12 latent, overlap = 2 latent
  tile_size 768, overlap 64   gives   tile_x = tile_y = 24 latent, overlap = 2 latent
```

`overlap` is held to a quarter of the tile, in latent space, which is ComfyUI's own convention
(`sd.py:1234`, `:1240`, `:1256` all derive theirs the same way). The bound exists because
`comfy/utils.py` builds tile positions as `range(0, size - overlap, tile - overlap)`. An overlap equal to
the tile gives a step of zero and a `ValueError`. An overlap larger than the tile gives a negative step, an
empty range, no tile decoded at all, and then a division of two all-zero buffers, which is a whole clip of
NaN with no error raised anywhere. The silent NaN is the one to fear, and the bound closes it. The guard's
failure is exercised in `tests/test_vae_decode_tiling.py`.

Spatial seams are not the thing to worry about, per the 1.03x to 1.05x gradient measurement above. If a
decode crawls, lower `tile_size` before blaming `precision`.

#### Temporal sizes, and why the defaults switch time tiling off

`temporal_size` defaults to **4096 pixel frames**, which is longer than any clip anyone is decoding. That is
the point. It makes the whole sequence one temporal tile, so no time tiling happens.

Verified by running `_tiling_kwargs` at the shipped defaults against a VAE reporting an 8x temporal ratio:
`temporal_size` 4096 converts to `tile_t = 512` latent frames with `overlap_t = 4`. Then `comfy/utils.py`
inside `tiled_scale_multidim` short-circuits: at line 1170 it decodes the whole input in one call when every
dimension fits inside its tile, and at line 1180 it builds positions as
`range(0, size - overlap, tile - overlap) if size > tile else [0]`. With a 512-frame latent tile, any real
clip takes the single-tile path. Confirmed by reading both lines.

**Tile space, never time.** Time tiling costs picture, and the artefact has an arithmetic explanation rather
than being a mystery. The period between soft frames is:

```
  period in pixel frames = (tile_t - overlap_t) x temporal ratio
```

where `tile_t = temporal_size / ratio` (floored, minimum 2) and
`overlap_t = min(tile_t / 2, temporal_overlap / ratio)` (floored, minimum 1). On the LTX-2 VAE the ratio is
8, from `temporal_compression_decode()` (`sd.py:1438`), which returns `round(upscale_ratio[0](8192) / 8192)`
against the LTX-2 `upscale_ratio` of `(lambda a: max(0, a * 8 - 7), 32, 32)` set at `sd.py:725`.

Worked through, by running `_tiling_kwargs`:

| `temporal_size` | `temporal_overlap` | `tile_t` | `overlap_t` | Step | Soft frame every |
| --- | --- | --- | --- | --- | --- |
| 4096 (default) | 32 (default) | 512 | 4 | 508 latent | 4064 pixel frames, so never |
| 32 | 8 | 4 | 1 | 3 latent | 24 pixel frames |
| 32 | 32 (default) | 4 | 2 | 2 latent | 16 pixel frames |
| 64 | 32 (default) | 8 | 4 | 4 latent | 32 pixel frames |

The measured artefact matches the second row: per-frame sharpness, as mean absolute Laplacian, fell to
**62% of the clip median at frames 25, 49, 73 and 97**, which is every 24th frame starting from 25. Note
that the 24-frame figure holds at `temporal_overlap` 8 or lower, which is ComfyUI's stock default for its
own tiled decode node, and **not** at this node's own default of 32, where the same `temporal_size` of 32
puts the soft frame every 16 pixel frames instead. This pack's tooltip and README both state the 24-frame
period without that condition, which is a documentation gap and is listed in section 7. Use the formula, not
the anecdote.

Why the edge is soft: a diffusion decoder has no context at a temporal tile edge, and the blend mixes that
weak edge into its neighbour. `temporal_overlap` cannot repair it. A wider blend spreads the weak edge over
more frames instead of removing it.

If you ever have to tile time, check per-frame sharpness afterwards. A frame-difference metric will not
catch this, because the artefact is a smooth blur and not a jump. Only per-frame sharpness with a local-dip
search finds it.

On a still-image VAE there is no time axis to tile. `temporal_compression_decode()` returns `None` for
anything whose `upscale_ratio` is not a tuple of callables, and `None` is a normal answer, not a failure.
Verified: against a VAE reporting 8x spatial and no temporal ratio, `_tiling_kwargs` returned
`tile_t = None, overlap_t = None` and the note "no temporal tiling (this VAE reports no temporal compression
ratio, so it has no time axis to tile)". `decode_tiled` forwards only the arguments that are not `None`
(`sd.py:1287-1292`), which is what a still VAE wants.

### 3.4 `out_of_range` on the encode

ComfyUI's default input transform is `comfy/sd.py:501`:

```python
self.process_input = lambda image: image * 2.0 - 1.0
```

No clamp. A pixel value of 4.0 arrives at the VAE as 7.0. The VAE was trained on `[-1, 1]`; anything past
that is outside its distribution, and what comes back is not defined by anything. The stock encode passes it
through without a word. This node counts it and tells you.

Three settings, and the guard fails open by design:

- **`report only`** (default). Behaves exactly like the stock node and logs what it found. It is the default
  because it cannot break a graph that works today: the stock path already passes out-of-range values
  through, so reporting adds information and removes nothing.
- **`clamp to 0..1`**. Keeps the VAE inside its training domain at the cost of the overshoot. The clamp is
  applied out of place, so the caller's own `IMAGE` tensor, which may still be wired elsewhere in the graph,
  is not touched.
- **`raise an error`**. Stops the job. Pick this when you want the strictness of a reference implementation
  and would rather lose the job than the evening. The message, captured from a run with one sample at 4.0:

  ```
  ValueError: OCIO VAE Encode: 1 of 192 samples (0.5208%) are outside 0..1
  (range +0.0039 .. 4.0000; 0 below 0, 1 above 1). comfy/sd.py:501 maps them with
  image*2-1 and does NOT clamp, so the VAE receives values outside the [-1, 1] it was
  trained on. Set 'out_of_range' to 'clamp to 0..1' or 'report only' to continue.
  ```

The node also reports a second thing the stock path does silently. `sd.py:1315` calls
`vae_encode_crop_pixels`, which narrows each spatial dimension down to a multiple of the VAE's compression
ratio without saying so. This node compares the dimensions before and after and logs the crop if there was
one. It reports rather than prevents, because preventing it would mean resizing the artist's image, which is
a bigger decision than a VAE node gets to make.

What this node deliberately does not do is convert colour. Encoding to ACEScct before the VAE is a colour
operation and belongs in `OCIO LogConvert`, where it is visible in the graph and can be inspected. Hiding a
curve inside a VAE node is how footage gets mangled without a trace. The decode makes the same choice in the
other direction.

---

## 4. The range report

Slot 1 of the decode is a `STRING`. It exists so you can read what the decode produced, and specifically
what a clamp would have cost, before you commit anything to a file.

Here is a real one, produced by running `_range_report` on a 64x64x3 frame spanning -0.1 to 1.1:

```
OCIO VAE Decode (float16, no clamp, whole frame): min=-0.099904 max=+1.099932 mean=+0.501843
  p0.1=-0.09878 p1=-0.08986 p50=+0.50409 p99=+1.08829 p99.9=+1.09907   (all 12288 samples, 3 channels)
  outside 0..1: 8.7402% below 0, 8.2926% above 1 (17.0329% would be lost to the standard clamp)
```

Reading it line by line.

**The header** repeats the settings you ran with, in parentheses: the `precision` choice, whether the clamp
was applied, and whether the decode was tiled or whole-frame. Then `min`, `max` and `mean`.

**Which figures are exact and which are sampled.** This is the part to get right.

| Figure | Basis |
| --- | --- |
| `min`, `max`, `mean` | **Exact.** Single pass over every finite sample. |
| `% below 0`, `% above 1`, and their sum | **Exact.** Single pass over every finite sample. |
| `NOT FINITE` counts | **Exact.** |
| `p0.1`, `p1`, `p50`, `p99`, `p99.9` | **Subsampled** when the array is large. Percentiles need a sort, and sorting a 121-frame HD clip's 3.3e8 samples to print five numbers would cost more than the decode. |

The out-of-range shares are the reason the report exists, so they are never approximated. The cap on the
percentile subsample is 4,000,000 samples (`_PERCENTILE_SAMPLE_CAP`, `vae_nodes.py:131`), which on 3-channel
data is a budget of 1,333,333 pixels. The trailing parenthesis tells you which case you got: `all N samples`
when nothing was subsampled, or `N of M samples` when it was.

The stride is an integer division of pixel rows by that budget, so subsampling only starts once you have at
least twice the budget. Verified by running it: a 2x1024x1024x3 array (2,097,152 pixels) still reports
`all 6291456 samples`, while a 1x2048x2048x3 array (4,194,304 pixels) gives a stride of 3 and reports
`4194306 of 12582912 samples`. For the 121-frame clip at 1280x704 that the file's own docstrings cite, the
arithmetic gives a stride of 81 over 327,106,560 samples, which matches the step recorded there.

The subsample strides whole pixels rather than flat samples, and that is a fix rather than a nicety. An
`[B, H, W, C]` array raveled in C order makes `index % C` the channel, so a flat `a[::step]` collapses to a
single channel whenever the step is a multiple of `C`, which it usually is because the step derives from a
size that is itself a multiple of `C`. On the 121-frame clip the flat step was 81 and every sampled value
came from channel 0. On a frame with genuinely different channels (R 0.20, G 0.50, B 0.80) that reported a
median of 0.20 against a true 0.50. Striding whole pixels keeps all three channels in proportion at the same
cost.

**Non-finite values are counted, not folded into "in range".** A NaN is neither below 0 nor above 1, so
without this it would vanish from both shares while making `min`, `max` and `mean` print `nan`. Verified on
a 16x16x3 array with one NaN and one inf:

```
  NOT FINITE: 2 of 768 samples (0.2604%) - 1 NaN, 1 inf. The figures above cover the finite samples only.
```

If you see that line, stop and find out why before writing a file. A NaN clip is the failure mode the tiling
bounds exist to prevent, and it is silent everywhere else.

**Notes are appended after the report**, one per line, each prefixed `note:`. They carry anything the node
declined to do, which would otherwise appear only in the server log. Verified by running each path:

```
  note: this VAE's process_output is a pass-through (the identity), so this node found no clamp to remove
        and 'clamp' had no effect - note that some decoders clamp internally before this point, so
        unclamped output is not guaranteed
  note: this VAE's process_output is not a shape this node recognises, so it was left untouched and 'clamp'
        had no effect - the decode is exactly the stock one
  note: float32 declined: not listed in this VAE's working_dtypes ([torch.bfloat16]), so the decode stayed
        at torch.bfloat16
  note: float32 cast refused (RuntimeError), decode stayed at torch.bfloat16 - quantised weights cannot be
        re-cast on the fly
  note: tiled: 384px -> tile_x=tile_y=12 latent (ratio 32x), overlap 64px -> 2 latent; temporal 4096px ->
        tile_t=512 latent (ratio 8x), overlap_t=4
  note: tiling requested, but this VAE has no decode_tiled(); decoded untiled instead
```

**How to read it before writing a file.** Three checks, in order.

1. Is there a `NOT FINITE` line? If yes, nothing else in the report means anything. Fix that first.
2. What is the `above 1` share, and does it match what you expect from the material? On an SDR generation
   expect a fraction of a percent. On an ACEScct HDR decode expect roughly what went in, and remember that
   ACEScct code 1.0 is already about linear 222, so a genuinely bright frame can still read 0% above 1.
3. Do the notes say the node did what you asked? A note about an unrecognised transform means your decode
   was the stock one regardless of the clamp switch, and a note about a declined float32 means you got
   bfloat16.

Then pick your container. If the `above 1` or `below 0` share is non-zero and you are writing an integer
format, you are about to lose exactly that share.

---

## 5. Worked chains

Node types are the internal names as returned by `/object_info`; the display names are in brackets where
they differ. Combo values are copied from `/object_info` verbatim, because a value that does not match the
combo exactly is an HTTP 400 on the whole prompt, not a soft fallback.

### 5.1 A plain latent to an unclamped image

The smallest useful graph. Use it to see what your model actually produced.

```
  CheckpointLoaderSimple
      MODEL          into  KSampler.model
      VAE            into  OCIOVAEDecode.vae

  KSampler
      LATENT         into  OCIOVAEDecode.samples

  OCIOVAEDecode  [OCIO VAE Decode]
      precision          = float16                (declined on LTX, so the decode runs at bfloat16)
      clamp              = keep everything      (off)
      tiled              = whole frame          (off)

      image/sequence/video   into  OCIOWrite.images
      range report           into  any string display
```

```
  OCIOWrite  [OCIO Write]
      profile            = none
      input_colorspace    = sRGB Encoded Rec.709 (sRGB)
      output_colorspace  = ACEScg
      container          = still image
      still_format       = exr
      bit_depth          = 32f
      compression        = zip
      filename           = decode_raw
      output_folder      = <your delivery folder, or blank for the ComfyUI output dir>
```

Read the range report before anything else. On an ordinary SDR generation expect a small `above 1` share, on
the order of the 0.06% measured in section 1. If it is 0.0000% on both sides, the clamp was never going to
cost you anything on this material and you can stop worrying about it.

`bit_depth` is `32f` here rather than the `16f` default only because this is a diagnostic write. For
delivery, `16f` is what LTX's own reference pipeline writes.

### 5.2 The LTX-2.5 ACEScct round trip

This is the chain the node was built for. LTX-2.5's HDR path is ACEScct, reached through the `--hdr` flag in
Lightricks' reference CLI. Their pipeline rotates the source primaries to AP1 before compressing, so the VAE
hands out ACEScct log codes already in ACEScg primaries and only the transfer has to be undone.

LTX-2.3 is a different mechanism and the two are not interchangeable. 2.3's HDR is an IC-LoRA trained on
ARRI LogC3, and Lightricks' own ComfyUI node already undoes that curve, so what reaches this pack is
linear. Feeding 2.5 material through a 2.3 preset does not error. It just comes out flat and grey.

Encode side, if you are conditioning on your own plate:

```
  OCIORead  [OCIO Read]
      image/sequence/video   into  OCIOLogConvert.image
      (a folder of EXR frames in ACEScg scene-linear)

  OCIOLogConvert  [OCIO LogConvert]
      operation          = Linear to Log
      curve              = ACEScct
      mix                = 1.0

      image/sequence/video   into  OCIOVAEEncode.pixels

  VAELoader
      vae_name           = <your LTX-2.5 video VAE file>
      VAE                into  OCIOVAEEncode.vae
      VAE                into  OCIOVAEDecode.vae

  OCIOVAEEncode  [OCIO VAE Encode]
      precision          = float32
      out_of_range       = report only

      latent             into  the sampler's latent input
```

Decode side:

```
  <the LTX-2.5 sampler>
      LATENT         into  OCIOVAEDecode.samples

  OCIOVAEDecode  [OCIO VAE Decode]
      precision          = float32
      clamp              = keep everything      (off)
      tiled              = tiled                (on, for anything longer than a few frames)
      tile_size          = 384
      overlap            = 64
      temporal_size      = 4096                 (leave it, see 3.3)
      temporal_overlap   = 32                   (leave it)

      image/sequence/video   into  OCIOLogConvert.image
      range report           into  any string display

  OCIOLogConvert  [OCIO LogConvert]
      operation          = Log to Linear
      curve              = ACEScct
      mix                = 1.0

      image/sequence/video   into  OCIOWrite.images

  OCIOWrite  [OCIO Write]
      input_colorspace    = ACEScg
      output_colorspace  = ACEScg
      container          = sequence
      still_format       = exr
      bit_depth          = 16f
      compression        = zip
```

Two notes on the curve. The combo value is exactly `ACEScct`, with that capitalisation; `vae_nodes.py`'s own
docstring writes it lowercase, which would be rejected on the API path. And the explicit `OCIO LogConvert`
after the decode is now the only way to do this, so wire it: `operation = Log to Linear`, `curve = ACEScct`,
between `OCIO VAE Decode` and `OCIO Write`, exactly as the chain above shows. An `OCIOWrite.profile` shortcut
for it used to exist and was removed - see CHANGELOG.md for why, and for what it breaks in saved workflows.
Set `bit_depth` to `16f` yourself if you want the half-float EXR the reference writes; the chain above does.

Do not expect the unclamped decode to add stops here. Section 1 has the measurement: on real ACEScct
material at +3.9 stops, unclamped and clamped were the same, because code 1.0 is already about linear 222.
What you are getting from this node on this chain is float32, tiling that makes float32 possible, and a
report that tells you what came out.

### 5.3 A long clip with tiling on

For a clip that does not fit. The point of this configuration is that it completes at all.

```
  <sampler or latent upsampler>
      LATENT         into  OCIOVAEDecode.samples

  VAELoader
      VAE            into  OCIOVAEDecode.vae

  OCIOVAEDecode  [OCIO VAE Decode]
      precision          = float32               (or float16 for a review pass, declined on LTX)
      clamp              = keep everything       (off)
      tiled              = tiled                 (on)
      tile_size          = 384
      overlap            = 64
      temporal_size      = 4096
      temporal_overlap   = 32

      image/sequence/video   into  OCIOWrite.images
      range report           into  any string display

  OCIOWrite  [OCIO Write]
      container          = sequence
      still_format       = exr
      bit_depth          = 16f
      compression        = zip
      first_frame        = 1
      start_number       = 1
```

Four things to hold in mind.

Tiling changes the picture, by roughly thirty times what `precision` changes it. The numbers are in section
3.3. Decide once for the delivery and do not switch partway through a shot.

Leave `temporal_size` at 4096. Lowering it is the last thing to try, after every other way of fitting the
decode, and if you do lower it, check per-frame sharpness afterwards.

Lower `tile_size` before blaming `precision`. 384 was measured clean at 60 s for 121 frames at 1280x704 on a
32 GB card, and 768 was the value that overflowed at 912 s. That default has not been measured on a 24 GB
card.

Read the range report's tiling note to confirm the sizes you got. It prints the pixel value, the latent value
and the ratio it divided by, so a surprising number is visible rather than silent.

---

## 6. Traps

### Trap 1: a VAE cannot be swapped

A VAE is trained together with its transformer. The latent space is theirs jointly, and a VAE from another
model does not decode this model's latents into anything meaningful. Both nodes' `vae` tooltips say so, and
that is the whole enforcement: **nothing in either node checks it**. There is no fingerprint, no channel-count
assertion, no warning. A mismatched VAE will either raise somewhere inside ComfyUI on a shape mismatch or,
worse, decode to plausible-looking nonsense.

The practical rule: take the `VAE` from the checkpoint that made the latent, or load the VAE file that ships
with that model.

### Trap 2: the node probes the output transform, and does not assume one

This node does not assume the VAE's output transform, it probes it. Assuming the default at `sd.py:502` and
replacing it with `(x+1)/2` is wrong for most VAEs in the file. Counted by grep over the installed
`comfy/sd.py`, there are twelve `process_output = lambda` assignments, of which 502 is the default and the
other eleven install the identity: lines 540, 689, 851, 881, 894, 895, 906, 926, 952, 976 and 1003.

Five of those eleven are image decoders that already emit `[0, 1]`: 540, 894, 895, 906 and 976. Line 894 is
TAEHV for `latent_channels in [48, 128]`, which covers Wan 2.2 and LTX2, so it is the fast preview decoder
for the very model family this pack was built around. Line 976 is a VAE that finalises straight to `[0, 1]`
while streaming chunks out. On any of them, applying `(x+1)/2` to data already in `[0, 1]` produces
`[0.5, 1.0]`: a washed-out, wrong image, with no error anywhere. The old code did that on both branches,
including the clamped one, which means "clamp on reproduces stock exactly" was false for those VAEs too.

So the node asks. `_probe_process_output` feeds -1, 0 and 1 through the VAE's real function and reads where
they land. It clones the tensor first, because the default transform works in place and would otherwise
consume its own input. Three outcomes, all three confirmed by running the probe against stub VAEs:

| Probe result | What the node does |
| --- | --- |
| `[-1, 0, 1]` becomes `[0, 0.5, 1]` | Recognised as the default. Replaces it with the same mapping minus the clamp, or with the clamp if you asked for it. |
| `[-1, 0, 1]` unchanged | Recognised as a pass-through. Changes nothing, because nothing was clamping. Says so in the report and the log, and tells you `clamp` had no effect. |
| Anything else, or the call raises | Not recognised. **Leaves the VAE completely alone.** Your decode is the stock one, the `clamp` switch does nothing, and the report carries a note saying exactly that. |

That third row is the one to know about. The node does not guess, and it does not fail. It degrades to the
stock behaviour and tells you, on a wire you can read, not only in the log. If you expected unclamped output
from a VAE and got that note, the VAE is worth reporting.

One implementation detail that explains an old bug, and is worth knowing if you ever patch this yourself:
the replacement transform must be in place (`add_` and `div_`, not `add` and `div`). `sd.py:1215` calls
`self.process_output(...)` and throws the return value away, relying on the stock lambda to mutate. An
out-of-place version therefore left the raw VAE output untouched, and the node emitted values in the VAE's
native `[-1, 1]`: three quarters of a dark frame came out negative, blacks crushed, highlights burnt. Nothing
caught it but looking at the picture. Other call sites (1104, 1119, 1123, 1127) do use the return value, and
in-place operations satisfy both, since they mutate and return the same tensor. The encode side is the exact
opposite: `sd.py:1333` calls `process_input` on a slice and uses the return value, so an in-place operation
there would corrupt the caller's own `IMAGE`. That is why the encode node inspects the image directly and
patches nothing.

### Trap 3: an audio VAE will connect

ComfyUI has one `VAE` socket type for every kind of VAE. `LTXVAudioVAELoader` emits `VAE` (its output is
labelled `Audio VAE`), and a plain `VAELoader` will happily load an audio VAE file: on the machine this page
was written on, `VAELoader`'s list contains both `ltx-2.5-video-vae-bf16.safetensors` and
`ltx-2.5-audio-vae-bf16.safetensors`, and the graph cannot tell you which is which. So yes, it connects, and
no, the canvas will not stop you.

What the node says about it: six of the eleven identity assignments in `sd.py` are audio VAEs (689, 851, 881,
926, 952, 1003), so the probe lands on the pass-through row of the table above, and the report note says the
transform is a pass-through, that this node found no clamp to remove, and that some decoders clamp
internally before this point. The wording is deliberate on both counts. It does not say "emits 0..1",
because telling somebody decoding audio that their data is in 0..1 would be false. And it no longer says
"nothing was clamping its output", because that is a claim about the decoder which a pass-through wrapper
cannot support: MiniMax H3 clamps inside itself and then installs the identity, so the old wording was
exactly backwards on that model. What is true of all eleven is only that the transform is a pass-through.

What the node does **not** do is stop you or fix up the result. An audio decode does not produce an
`[B, H, W, C]` image, and the node's shape helper only folds a 5-dimensional video tensor into ComfyUI's
frame-as-batch layout, passing anything else through untouched. The `IMAGE` output would then be a tensor of
the wrong rank flowing down an `IMAGE` wire. Demonstrated by running the report on an audio-shaped
`[1, 2, 65536]` tensor: it printed `65536 channels` and `50.0801% below 0`, both meaningless, because the
channel count is read from the last axis. So the report will not read as obviously broken. It will read as
plausible and wrong.

The rule: video latent with the video VAE, audio latent with the audio VAE, and if a report says something
absurd like tens of thousands of channels, check which VAE you wired.

### Trap 4: the node restores the VAE's state, with one exception

Both nodes mutate the VAE object that is shared across the whole session, so restoring it matters. The
decode's mutations, the patched `process_output` and the raised dtype, both sit inside one `try` with a
`finally` that puts the original back. Confirmed by running: after a decode where the float32 cast was
refused, `vae.vae_dtype` was unchanged at `torch.bfloat16` and `vae.process_output` was identical to the
original object. After a decode that ran with float32, the dtype and the weights are both put back. The next
graph in the session is unaffected either way.

The encode used to be the exception, and it no longer is (fixed in `vae_nodes.py`, verified here
2026-08-13). Its restore was conditional on the cast having fully succeeded *and* it set the dtype flag
before casting the weights, so a refused cast left the flag at float32 over bfloat16 weights with no
restore. The order is now cast first, bookkeeping second, so nothing is recorded that did not happen.
Confirmed by mutation rather than by reading: put the two lines back in the old order and
`tests/test_vae_encode.py` fails on **"vae_dtype is NOT left claiming float32 after a refused cast"**,
reporting `left as torch.float32`; restore the order and it passes. The earlier advice to reload the VAE
after a failed encode cast is no longer needed.

### Trap 5: off does not mean untiled

`tiled` off means "tile only if ComfyUI is forced to", because `vae.decode` falls back to tiled decoding on
an out-of-memory condition at `sd.py:1216-1223` and picks its own sizes. If a decode you ran with `tiled`
off does not match a previous one, that fallback is a candidate. The range report's header says `whole frame`
based on which entry point the node called, not on what ComfyUI did internally after that, so it will say
`whole frame` in this case.

---

## 7. What is not verified, and what looks like a defect

### Not verified for this page

- **No GPU decode was run for the FIRST version of this page.** Its numbers in sections 1, 3.2 and 3.3
  came either from the measurements recorded in `vae_nodes.py`'s docstrings and tooltips, or from CPU-only
  work on this machine against stub VAEs: the tiling arithmetic, the range report on synthetic arrays, the
  transform probe, and the state-restore paths. **Amended 2026-08-13:** the timing and float16 figures added
  to section 3.2 since then WERE measured on the GPU with the real LTX-2.5 video VAE, both in-process and
  through the server's `/prompt` path with per-node timing from its websocket. The tiling and clamp numbers
  in sections 1 and 3.3 were not re-measured and still stand on the earlier sources.
- **`tile_size` 384 is now measured on a 24 GB card, for 25 frames only.** 25 frames at 1280x704 ran in
  5.96 s at the model's own dtype and 29.90 s at float32 on a 24 GB card with about 1.3 GB held. The **121-frame**
  60 s figure is still from a 32 GB card and has not been reproduced on 24 GB, so a long clip at this tile
  size remains unverified here. The older 5.2 s and 26.4 s figures are from the same 24 GB pair with about
  6.5 GB held. Do not assume a tile size fits your card because it fits somebody's.
- **"clamp on is bit-identical to stock" was not re-measured.** It is the claim recorded in the file's
  docstring for a VAE using the default transform. It is also known to be false for the eleven identity
  VAEs, where this node clamps on neither path. Whether the *decoder* clamped before either path was
  reached is a separate question, answered per model: MiniMax H3 does, at
  `comfy/ldm/minimax/vae.py:398-401`.
- **The cause of the tiled-against-untiled divergence is unknown.** The size, growth and non-alignment to
  tile boundaries are measured. Why the error roughly doubles across 25 frames is not explained, and the
  obvious explanations were not confirmed. It is not temporal tiling, since the whole clip is one temporal
  tile at the defaults.
- **The exact tensor shape an audio VAE decode returns was not observed.** No audio VAE was loaded. What was
  confirmed is that the report's channel count comes from the last axis and prints nonsense on a
  non-image-shaped tensor, and that the shape helper only folds 5-dimensional input.
- **The `ACEScct` chain in section 5.2 was not executed end to end here.** The node types, combo values and
  defaults are from `/object_info` on the running server. The wiring follows the pack's own documented
  recipe. Nobody rendered it as part of writing this page.

### Things that look like real defects

1. ~~**The encode leaks the VAE's dtype when the float32 cast fails.**~~ **FIXED, verified 2026-08-13.**
   The order was `vae.vae_dtype = torch.float32`, then `vae.first_stage_model.to(torch.float32)`, then
   `raised_precision = True`, so a raising middle line left the flag changed with `raised_precision` still
   `False` and the `finally` skipping the restore. `vae_nodes.py` now casts first and does the bookkeeping
   after. Verified by mutation, not by reading: restoring the old order makes
   `tests/test_vae_encode.py` fail on "vae_dtype is NOT left claiming float32 after a refused cast" with
   `left as torch.float32`.

2. ~~**The encode does not consult `working_dtypes`, and does not catch a refused cast.**~~ **FIXED,
   verified 2026-08-13.** The encode now declines float32 with a note when the VAE does not list it and
   falls back with a note when the weights refuse, the same as the decode. Verified by mutation: disabling
   the `working_dtypes` test on the encode makes `tests/test_vae_encode.py` fail on "and it really did
   encode at that precision, not at float32"; disabling the decode's makes
   `tests/test_vae_decode_tiling.py` fail on "float32 declined appears in the report". Worth knowing that
   the decline branch cannot fire for any **core** VAE, since all 23 `working_dtypes` lists in
   `comfy/sd.py` include float32 - it is there for third-party VAE classes, and the tests reach it with a
   stand-in.

3. ~~**The encode has no report output.**~~ **Fixed, and this page said otherwise for longer than the
   defect lasted.** The encode has carried a second `STRING` output, `input report`, since
   `RETURN_NAMES = ("latent", "input report")`; two places on this page went on describing the state
   before that, one of them telling readers the findings reach "the server log only". Corrected
   2026-08-14. The lesson is the one this pack keeps relearning: a page that documents a gap has to be
   re-read when the gap is closed, or it becomes the most convincing kind of wrong, a specific claim
   about your own software.

4. **The 24-frame temporal artefact is documented without its condition.** The `temporal_size` tooltip and
   the README both say the soft frame lands every 24 pixel frames at `temporal_size` 32. That is true at
   `temporal_overlap` 8, which is ComfyUI's stock default, and false at this node's own default of 32, where
   the period is 16. Verified by running `_tiling_kwargs` at both settings. Section 3.3 gives the general
   formula.

5. **The docstring writes the ACEScct curve name in lowercase.** `vae_nodes.py`'s module docstring says to
   use `OCIO LogConvert` with curve `acescct`. The combo value on the running server is `ACEScct`. In the
   canvas this does not matter, since you pick from a list. On the API path a combo value that does not
   match exactly is rejected for the whole prompt.

None of the five changes what the decode produces on a normal graph. Items 1 and 2 are on the encode's
float32 path, item 3 is a visibility gap, and items 4 and 5 are wrong words in the right place.

---

## Where the numbers came from

- **`/object_info` on the running server** for every input name, type, default, combo value, min, max and
  step, and for every output name and type, on both nodes and on `OCIOLogConvert`, `OCIOWrite`, `VAELoader`,
  `LTXVAudioVAELoader` and `KSampler`.
- **ComfyUI's own source as installed**, read directly: `comfy/sd.py` lines 501, 502, 540, 689, 725, 729,
  851, 878, 881, 894, 895, 906, 926, 952, 976, 1000, 1003, 1104, 1119, 1123, 1127, 1215, 1216-1223, 1281,
  1287-1292, 1304, 1315, 1333, 1426, 1438; `comfy/utils.py` lines 1109, 1170, 1180; `nodes.py` lines
  313-338 and 354-372.
- **`vae_nodes.py`'s own docstrings**, for the measurements on clamp cost, HDR survival, bfloat16 against
  float32, the sharpness dips and the spatial gradient excess. Those docstrings state their own conditions
  and are quoted with them.
- **Measured on this machine while writing this page**: the tiled-against-untiled divergence table in
  section 3.3, on 25 frames at 1280x704 through this node.
- **Run on this machine while writing this page**, against stub VAEs on CPU: the tiling conversions and the
  temporal period table, the range report samples, the three probe outcomes, and the state-restore results
  in traps 4 and section 7.
