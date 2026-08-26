# The seven colour operators and the viewer

Reference for `OCIO ColorSpace`, `OCIO LogConvert`, `OCIO Display`, `OCIO CDLTransform`, `OCIO FileTransform`,
`OCIO LookTransform` and `OCIO Player`. Every input, every allowed value, what to wire into each socket, what
the transform does to your numbers, and the mistakes that produce a wrong-looking result with no error message.

`OCIO Read`, `OCIO Write`, `OCIO VAE Decode` and `OCIO VAE Encode` are documented in the README.

## How to read this file

Two kinds of statement appear here, and they are labelled.

- **Confirmed** means it was read off a live server or produced by a measurement that is named in the text.
  Combo values, defaults and socket types come from `GET /object_info/<NodeType>` on a running ComfyUI. Numbers
  come from running the transform on real pixel values and printing the result.
- **Inferred** means it follows from the code or the spec but was not exercised. It says so in the sentence.

Nothing here is described from a node's name. Where a published spec is cited, the spec is named.

### The setup these numbers came from

Confirmed by `GET /system_stats` and by importing the pack in the ComfyUI interpreter:

| Item | Value |
| --- | --- |
| ComfyUI | 0.32.0, frontend 1.48.7 |
| Python | 3.13.12 |
| PyOpenColorIO | 2.5.2 |
| Active OCIO config | `studio-config-v4.0.0_aces-v2.0_ocio-v2.5` |
| How that config is reached | `Config.CreateFromBuiltinConfig("studio-config-latest")` |
| Colorspaces in it | 55 |
| Displays in it | 9 |
| Looks in it | 1 (`ACES 1.3 Reference Gamut Compression`) |
| `$OCIO` environment variable | not set |

The colorspace, display, view and look lists below are that config's. Point a node at a different `.ocio` file
and the lists change, so re-read them from your own `/object_info` before typing a value into an API call.

Config resolution order, read from the code: an explicit `.ocio` file if one is chosen, then `$OCIO` if it is
set, then the built-in ACES studio config. Confirmed by measurement that the built-in path is what runs here
(the cache key came back as `('builtin', 'studio-config-latest')`).

---

## Part 1: behaviour every one of the seven operators shares

### Two inputs for pixels, and they exclude each other

Every operator carries an `IMAGE` input and a `VIDEO` input, and mirrors that choice on the output side.

| Socket | Type | Label shown in the UI | What it is for |
| --- | --- | --- | --- |
| `image` | `IMAGE` | `OCIO Img/Seq/Vid` | a still (batch of 1), an image sequence, or video frames already in memory |
| `video` | `VIDEO` | `ComfyUI Video` | ComfyUI's native video object, streamed without materialising every frame |

Output 0 is `IMAGE` (named `image/sequence/video`), output 1 is `VIDEO` (named `ComfyUI Video`). Confirmed from
`/object_info` for all six.

Measured behaviour, by calling the node with each combination:

- `VIDEO` in: the `IMAGE` output is `None` and the `VIDEO` output carries the result. Frame rate is preserved
  (a 24.0 fps input came back 24.0 through both `OCIO ColorSpace` and `OCIO LogConvert`).
- `IMAGE` in: the `VIDEO` output is `None`.
- Both in: **the `VIDEO` socket wins** and the `IMAGE` output is `None`. The front end normally prevents this by
  auto-disconnecting whichever input you did not just plug in, so it takes an API call to reach.
- Neither in: the node raises `ValueError: Connect an image / sequence to 'Image Sequence Video', OR a movie to
  'ComfyUI Video'.` That message names the `IMAGE` socket `Image Sequence Video`, which is not the label the UI
  shows any more. The socket it means is the one labelled `OCIO Img/Seq/Vid`.

The same numbers come out either way. Feeding a 3-frame batch as `IMAGE` and as `VIDEO` gave a maximum
difference of 0.0.

### `mix`

`FLOAT`, default `1.0`, minimum `0.0`, maximum `1.0`, step `0.01`. Present on all six. Confirmed from
`/object_info`.

It is a per-channel linear blend between what came in and what the transform produced. Measured on
`OCIO ColorSpace` with ACEScg 0.18 into `sRGB - Display`:

| `mix` | result | `input*(1-mix) + output*mix` |
| --- | --- | --- |
| 0.0 | 0.180000 | 0.180000 |
| 0.5 | 0.320677 | 0.320677 (difference 0) |
| 1.0 | 0.461355 | 0.461355 |

`mix = 0.0` returns the input exactly, not approximately. The value is clamped into 0..1 before use, so a value
outside that range cannot be reached through the widget or through the API.

There is no encoding awareness in that blend. On a display transform it interpolates between a scene-linear
number and a display code, which is not a meaningful intermediate. Measured on `OCIO Display`, ACEScg 0.18
through `sRGB - Display` with the `ACES 2.0 - SDR 100 nits (Rec.709)` view:

| `mix` | result | plain lerp of 0.18 and 0.349187 |
| --- | --- | --- |
| 0.25 | 0.222297 | 0.222297 |
| 0.50 | 0.264594 | 0.264594 |
| 0.75 | 0.306891 | 0.306890 |

Use it to dial back a grade, not to soften a display transform.

### Alpha

The transforms touch channels 0, 1 and 2 only. If a 4-channel `IMAGE` arrives, channel 3 comes out bit for bit
unchanged. Confirmed on `OCIO ColorSpace` and `OCIO CDLTransform`.

ComfyUI's own convention carries alpha in a separate `MASK`, which is what `OCIO Read` emits and what
`OCIO Write` and `OCIO Player` accept. Keep alpha on the `MASK` path.

### Which nodes need PyOpenColorIO

Confirmed by checking each node's own function for the dependency guard:

| Node | Needs the `opencolorio` package |
| --- | --- |
| `OCIO LogConvert` | **no**, the curves are implemented in the pack |
| `OCIO ColorSpace` | yes |
| `OCIO Display` | yes |
| `OCIO CDLTransform` | yes |
| `OCIO FileTransform` | yes |
| `OCIO LookTransform` | yes |

Without the package the five OCIO-backed nodes raise `RuntimeError: This node needs OpenColorIO. Install the
opencolorio package. OCIO LogConvert runs without it.`

### `config_path`, and which nodes ignore it

`OCIO ColorSpace`, `OCIO Display` and `OCIO LookTransform` take an optional `config_path`. Confirmed from
`/object_info` that on this machine the combo holds exactly one entry:

```
(built-in ACES config)
```

Drop a `.ocio` file into the ComfyUI input folder and it joins the list; the node's **upload .ocio config**
button does that for you. Confirmed by reading the scanner: it walks the input folder for `.ocio` files, and it
returned an empty list here, which is why the combo has one entry.

`OCIO CDLTransform` and `OCIO FileTransform` have **no** `config_path` widget. Confirmed from `/object_info` and
from their function signatures. They still resolve a config internally, because OCIO needs one to build a
processor, and they fall back to a raw config if none is found. Neither transform names a colorspace, so this
does not change your result. It does mean a custom config's `search_path` cannot be used to resolve a LUT
filename for `OCIO FileTransform`; give that node the file itself.

### Buttons the front end adds

Read from the front-end sources, not from the node definitions, so they will not appear in `/object_info`:

| Button | On | What it does |
| --- | --- | --- |
| `swap in/out` | `OCIO ColorSpace`, `OCIO LookTransform` | exchanges `in_colorspace` and `out_colorspace` |
| `swap direction` | `OCIO LogConvert` | toggles `operation` between `Linear to Log` and `Log to Linear` |
| `upload .ocio config` | `OCIO ColorSpace`, `OCIO Display`, `OCIO LookTransform` | sends a `.ocio` from your machine to the input folder and selects it |
| `upload LUT file` | `OCIO FileTransform` | same for `.cube`, `.3dl`, `.spi1d`, `.spi3d`, `.csp`, `.ccc`, `.cdl`, `.clf`, `.lut` |

### The 55 colorspaces, verbatim

This is the exact list and exact order returned by `/object_info` for `in_colorspace` and `out_colorspace`. The
two lists were compared and are identical. A value spelled any other way is an HTTP 400 for the whole job, so
copy from here.

```
sRGB - Display
Gamma 2.2 Rec.709 - Display
Display P3 - Display
Display P3 HDR - Display
P3-D65 - Display
Rec.1886 Rec.709 - Display
Rec.2100-HLG - Display
Rec.2100-PQ - Display
ST2084-P3-D65 - Display
ACES2065-1
ACEScc
ACEScct
ACEScg
ADX10
ADX16
Apple Log
ARRI LogC3 (EI800)
Linear ARRI Wide Gamut 3
ARRI LogC4
Linear ARRI Wide Gamut 4
BMDFilm WideGamut Gen5
Linear BMD WideGamut Gen5
DaVinci Intermediate WideGamut
Linear DaVinci WideGamut
CanonLog2 CinemaGamut D55
Linear CinemaGamut D55
CanonLog3 CinemaGamut D55
D-Log D-Gamut
Linear D-Gamut
V-Log V-Gamut
Linear V-Gamut
Log3G10 REDWideGamutRGB
Linear REDWideGamutRGB
S-Log3 S-Gamut3
S-Log3 S-Gamut3.Cine
S-Log3 Venice S-Gamut3
S-Log3 Venice S-Gamut3.Cine
Linear S-Gamut3
Linear S-Gamut3.Cine
Linear Venice S-Gamut3
Linear Venice S-Gamut3.Cine
sRGB Encoded Rec.709 (sRGB)
Gamma 1.8 Encoded Rec.709
Gamma 2.2 Encoded Rec.709
Gamma 2.4 Encoded Rec.709
Camera Rec.709
sRGB Encoded P3-D65
Gamma 2.2 Encoded AdobeRGB
sRGB Encoded AP1
Gamma 2.2 Encoded AP1
Linear AdobeRGB
Linear P3-D65
Linear Rec.2020
Linear Rec.709 (sRGB)
Raw
```

Notes on picking from that list, all confirmed by measurement:

- The nine names ending `- Display` are display colorspaces. They are also the nine entries of the `display`
  combo on `OCIO Display`.
- A name starting `Linear ` is scene-linear in that camera's own primaries. `Linear ARRI Wide Gamut 3` is the
  gamut partner for an ARRI LogC3 plate, `Linear S-Gamut3.Cine` for a Sony one, and so on.
- `Raw` is a pass-through. Converting a ramp of -1.0 to 5000.0 into `Raw` returned the identical ramp.
- `ACEScg` is what to grade in. `ACES2065-1` is the wider archival space and is the default `in_colorspace`.

---

## Part 2: `OCIO ColorSpace`

### What it is for

Converts pixels from one OCIO colorspace to another: gamut matrix, transfer curve, or both, whatever the config
says sits between the two names. This is the node that puts a plate into your working space and takes a render
back out. Nuke equivalent: **OCIOColorSpace**.

### Inputs

| Name | Type | Default | Allowed values |
| --- | --- | --- | --- |
| `in_colorspace` | combo | `ACES2065-1` | the 55 names above, verbatim |
| `out_colorspace` | combo | `ACEScg` | the 55 names above, verbatim |
| `mix` | `FLOAT` | `1.0` | 0.0 to 1.0, step 0.01 |
| `image` | `IMAGE` (optional) | | see below |
| `video` | `VIDEO` (optional) | | see below |
| `config_path` | combo (optional) | `(built-in ACES config)` | `(built-in ACES config)`, plus any `.ocio` in the input folder |

All confirmed from `/object_info`. Widget order in the UI is `in_colorspace`, `out_colorspace`, `mix`, then the
optional sockets, then the two front-end buttons.

**What you wire into each socket.**

- `image`: `Load Image` (its `IMAGE` output), `OCIO Read` (`image/sequence/video`), `VAE Decode` (`IMAGE`),
  `OCIO VAE Decode` (`image/sequence/video`), or the `IMAGE` output of another OCIO operator.
- `video`: `Load Video` (`VIDEO`), `Create Video` (`VIDEO`), `OCIO Read` (`ComfyUI Video`), or the `VIDEO`
  output of another OCIO operator. Wiring this auto-disconnects `image`.
- `config_path` is a widget, not a socket. Pick from the list or press **upload .ocio config**.

### Outputs

| Index | Name | Type | What it connects to |
| --- | --- | --- | --- |
| 0 | `image/sequence/video` | `IMAGE` | the next OCIO operator, `OCIO Write` (`images`), `OCIO Player` (`images`), `Preview Image`, `Save Image`, `Create Video` (`images`), `ImageScale` |
| 1 | `ComfyUI Video` | `VIDEO` | `Save Video` (`video`), `SaveWEBM`, `OCIO Write` (`video`), `OCIO Player` (`video`), the next OCIO operator's `video` |

Only the socket matching the input you used carries data.

### The colour behaviour that decides whether the result is right

**Direction.** Always `in_colorspace` to `out_colorspace`. There is no invert switch; press **swap in/out** or
set the two widgets the other way round.

**Matching colorspaces are a true no-op.** ACEScg into ACEScg over a ramp of -1.0 to 5000.0 returned a
bit-identical tensor (`torch.equal` was true, maximum absolute difference 0.0). Leaving a redundant node in the
graph costs time, not accuracy.

**Values below 0 and above 1 survive.** Nothing in this node clamps. Measured on ACEScg into `ACES2065-1`:

| in | -1.0 | -0.1 | 0.0 | 0.18 | 1.0 | 2.0 | 100.0 | 5000.0 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| out | -1.0 | -0.1 | 0.0 | 0.18 | 1.0 | 2.0 | 100.0 | 4999.9995 |

That row is a neutral ramp, and neutrals are invariant between AP0 and AP1 because both share the same white
point, which is why the numbers are unchanged. Measured on the same node, a neutral 0.18 stays `[0.18, 0.18,
0.18]` while a chromatic patch moves: ACEScg `[1.0, 0.2, 0.1]` becomes `[0.739975, 0.226282, 0.095429]` in
ACES2065-1. The point of the ramp table is the endpoints, and both ends came through.

Even a display encode keeps its extremes. Measured on ACEScg into `sRGB - Display`:

| in | -1.0 | -0.1 | 0.001 | 0.18 | 1.0 | 2.0 | 8.0 | 5000.0 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| out | -1.000007 | -0.349189 | 0.012923 | 0.461355 | 1.000007 | 1.353263 | 2.454241 | 36.630173 |

The curve is mirrored through the origin and extended past 1.0. Nothing was clipped.

Two display-referred names extend differently, and this is measured, not assumed. Linear Rec.709 -1.0 encodes to
-12.92321 through `sRGB Encoded Rec.709 (sRGB)` (the sRGB linear segment, slope 12.92, extended without limit)
but to -1.000007 through `sRGB - Display`. Both preserve the sign, and they are not interchangeable below black.

**Round-trip accuracy.** Encode then decode, over the same -1.0 to 5000.0 ramp:

| Path | maximum relative error |
| --- | --- |
| ACEScg to ACES2065-1 and back | 1.95e-07 |
| Linear Rec.709 to ACEScg and back | 1.66e-07 |
| Linear Rec.709 to `sRGB Encoded Rec.709 (sRGB)` and back | 3.87e-05 |

The pack applies OCIO's default CPU processor to float32 pixels, so these figures are float32 processing error,
not a modelling error. The largest absolute miss in the first row was 0.00098 on an input of 5000.0, which is
two float32 steps at that magnitude.

### Worked chains

**Put a JPEG or PNG plate into ACEScg for grading.**

```
Load Image                                        (IMAGE)
  into OCIO ColorSpace
       in_colorspace  = sRGB Encoded Rec.709 (sRGB)
       out_colorspace = ACEScg
       mix            = 1.0
  into OCIO CDLTransform          (your grade)
  into OCIO Display               (only to look at it)
  into Preview Image
```

`sRGB Encoded Rec.709 (sRGB)` is the right name for an ordinary 8-bit web or camera JPEG: sRGB primaries and the
sRGB transfer curve. `sRGB - Display` describes a display device instead. Both are in the list and they are not
the same transform.

**Take a graded ACEScg render out to a Rec.709 deliverable file.**

```
OCIO ColorSpace   (your ACEScg result)
  into OCIO Display
       in_colorspace   = ACEScg
       display         = Rec.1886 Rec.709 - Display
       view            = ACES 2.0 - SDR 100 nits (Rec.709)
  into OCIO Write
       input_colorspace = Rec.1886 Rec.709 - Display
       raw_data        = true
```

Setting `raw_data` on the Write stops it converting a second time, since `OCIO Display` already did the
display-referred step.

**Move a plate between two camera gamuts without touching the tone curve.**

```
OCIO Read      (a Sony plate already decoded to linear)
  into OCIO ColorSpace
       in_colorspace  = Linear S-Gamut3.Cine
       out_colorspace = ACEScg
  into OCIO Player     (to check it)
```

### Traps

- `sRGB Encoded Rec.709 (sRGB)` and `sRGB - Display` are different entries with different behaviour below black.
  Pick the file-encoding one for files and the display one for displays.
- Setting `in_colorspace` to a camera's log name (`S-Log3 S-Gamut3`, `ARRI LogC3 (EI800)`) does the curve **and**
  the gamut. That is correct for a genuine camera plate and wrong for a generated image that only carries the
  curve. Part 3 has the measurement.
- The node reports no warning when both colorspaces match. It just passes pixels through.

### What could not be verified here

The 55-name list and every default were read from a live server, but only a subset of the 3025 possible pairs was
exercised. The pairs measured are the ones quoted above. Any specific pair you rely on should be checked with a
known patch, and `OCIO ColorSpace` with matching names is the cheapest way to prove a chain is inert.

---

## Part 3: `OCIO LogConvert`

### What it is for

Applies one camera or film log transfer curve, in either direction, and nothing else. No gamut matrix, no
colorspace naming, no OCIO dependency. Use it to decode a log-encoded plate to scene-linear, or to fold
scene-linear HDR into a 0..1 log range for a model that only accepts 0..1. Nuke equivalent: **OCIOLogConvert**,
with a much longer curve list.

### Inputs

| Name | Type | Default | Allowed values |
| --- | --- | --- | --- |
| `operation` | combo | `Linear to Log` | `Linear to Log`, `Log to Linear` |
| `curve` | combo | `Cineon` | the 10 names below, verbatim |
| `mix` | `FLOAT` | `1.0` | 0.0 to 1.0, step 0.01 |
| `image` | `IMAGE` (optional) | | as Part 1 |
| `video` | `VIDEO` (optional) | | as Part 1 |

The 10 curve values, exact order from `/object_info`:

```
Cineon
ACEScct
ACEScc
ARRI LogC3
ARRI LogC4
Sony S-Log3
Panasonic V-Log
Canon Log 3
RED Log3G10
DaVinci Intermediate
```

There is no `config_path`: the curves do not come from a config.

Older machine-readable spellings still work. The node's own validator accepts `cineon`, `acescct`, `acescc`,
`logc3`, `logc4`, `slog3`, `vlog`, `canonlog3`, `log3g10`, `davinci_intermediate` for `curve`, and `lin_to_log`
and `log_to_lin` for `operation`, so a workflow saved before the labels changed still loads. Confirmed by
calling the validator and by running `operation="lin_to_log", curve="cineon"`, which produced the identical
number to the Title Case pair. An unknown value returns a readable message rather than a crash: `curve 'nonsense'
is not a known log curve`.

**What you wire in.** Same sources as `OCIO ColorSpace`. The usual upstream for `Log to Linear` is `OCIO Read`
on a camera file with `raw_data` on, or `VAE Decode` / `OCIO VAE Decode` for a model that emits log codes. The
usual upstream for `Linear to Log` is an ACEScg image on its way into a model.

### Outputs

Identical shape to `OCIO ColorSpace`: `IMAGE` at index 0, `VIDEO` at index 1, only the active one filled.
Downstream, `Log to Linear` almost always goes into an `OCIO ColorSpace` next (see the trap below).

### The colour behaviour that decides whether the result is right

**Direction** is the `operation` widget, and the **swap direction** button toggles it.

**Round-trip, linear into log and back.** Sampled at 1e-5, 1e-4, 1e-3, 0.005, 0.01, 0.05, 0.18, 0.5, 1.0, 2.0,
8.0, 16.0, 55.0, 100.0:

| curve | worst relative error over the whole set | worst over 0.05 to 100 |
| --- | --- | --- |
| Cineon | 1.84e-05 | 2.38e-07 |
| ACEScct | 1.84e-06 | 3.58e-07 |
| ACEScc | 4.29e-07 | 3.58e-07 |
| ARRI LogC3 | 6.42e-05 | 3.81e-07 |
| ARRI LogC4 | 1.00e-04 | 3.05e-07 |
| Sony S-Log3 | 3.27e-05 | 3.81e-07 |
| Panasonic V-Log | 2.57e-05 | 4.16e-07 |
| Canon Log 3 | 1.85e-04 | 2.38e-07 |
| RED Log3G10 | 4.55e-05 | 3.05e-07 |
| DaVinci Intermediate | 1.53e-07 | 1.53e-07 |

Every worst case in the middle column sits at linear 1e-5, deep in the toe where a float32 code value has few
bits of headroom to describe a linear value. From mid grey up, all ten agree with themselves to better than
5e-07 relative.

**Round-trip, log into linear and back**, over codes 0.0 to 1.0: maximum absolute error **2.38e-08 for all ten
curves**. That is the direction a model pipeline actually uses, and it is exact to float32.

**Headroom.** What each curve's 0.0 and 1.0 code decode to in scene-linear. Measured:

| curve | code 0.0 | code 1.0 |
| --- | --- | --- |
| Cineon | -0.005651 | 13.5217 |
| ACEScct | -0.006917 | 222.861 |
| ACEScc | 0.001186 | 222.861 |
| ARRI LogC3 | -0.017290 | 55.0796 |
| ARRI LogC4 | -0.018057 | 469.800 |
| Sony S-Log3 | -0.014024 | 38.4209 |
| Panasonic V-Log | -0.022321 | 46.0855 |
| Canon Log 3 | -0.082015 | 16.2981 |
| RED Log3G10 | -0.010000 | 184.322 |
| DaVinci Intermediate | 0.000000 | 100.000 |

That column decides which curve to use for an HDR fold. A render with 400 in it needs `ARRI LogC4` or
`RED Log3G10`; `Cineon` would run out at 13.5.

**Published-spec anchors.** Each maker states where a landmark lands. Measured against those figures:

| curve | landmark, as the maker states it | spec | measured | difference |
| --- | --- | --- | --- | --- |
| Cineon | black 0.0 at code 95/1023 | 0.092864 | 0.092864 | 8.4e-10 |
| ACEScct | linear 0.0 at 0.0729055341958355 (ACES S-2016-001) | 0.072906 | 0.072906 | 1.2e-09 |
| ARRI LogC3 | 18% grey at 0.391 (LogC3 EI 800) | 0.391000 | 0.391007 | 6.8e-06 |
| ARRI LogC4 | 18% grey at 0.2784 (ARRI LogC4 specification, May 2022) | 0.278400 | 0.278396 | 4.2e-06 |
| Sony S-Log3 | 0% black at code 95/1023 | 0.092864 | 0.092864 | 8.4e-10 |
| Sony S-Log3 | 18% grey at code 420/1023 | 0.410557 | 0.410557 | 4.1e-09 |
| Sony S-Log3 | 90% white at code 598/1023 | 0.584555 | 0.584453 | 1.0e-04 |
| Panasonic V-Log | 0% black at code 128/1023 | 0.125122 | 0.125000 | 1.2e-04 |
| Panasonic V-Log | 18% grey at code 433/1023 | 0.423265 | 0.423311 | 4.7e-05 |
| Panasonic V-Log | 90% white at code 602/1023 | 0.588465 | 0.588167 | 3.0e-04 |
| RED Log3G10 | linear -0.01 at 0.0 | 0.000000 | 0.000000 | 3.4e-09 |
| RED Log3G10 | linear 0.0 at 0.091551 | 0.091551 | 0.091551 | 4.9e-07 |
| RED Log3G10 | 18% grey at 1/3 | 0.333333 | 0.333333 | 4.1e-07 |
| RED Log3G10 | linear 1.0 at 0.493449 | 0.493449 | 0.493449 | 4.7e-07 |
| RED Log3G10 | linear 184.322 at 1.0 | 1.000000 | 1.000000 | 1.8e-07 |
| DaVinci Intermediate | linear 0.0 at 0.0 | 0.000000 | 0.000000 | 0 |
| DaVinci Intermediate | 18% grey at 0.336043 | 0.336043 | 0.336043 | 2.7e-07 |
| DaVinci Intermediate | linear 1.0 at 0.513837 | 0.513837 | 0.513837 | 4.6e-07 |
| DaVinci Intermediate | linear 10.0 at 0.756599 | 0.756599 | 0.756599 | 9.0e-09 |

Where the miss reaches 1e-04 to 3e-04 the cause is the code value being an integer in a 10-bit table: Sony's own
"90% white" row is code 598, and 598/1023 is itself a rounded description of the analytic curve. The formula and
the table disagree by that much before this pack is involved.

`ARRI LogC4` is worth calling out because its mid-grey figure is easy to get wrong from memory. ARRI's own
LogC4 specification puts relative scene linear 0.18 at code value **0.2784**, lower than LogC3's 0.391 precisely
because the extra headroom pushes grey down the code range. The node measures 0.278396, and its 1.0 code decodes
to exactly 469.8000, which matches the headroom figure in the node's own tooltip.

**Canon Log 3 uses a different input convention from the other nine, and it matters.** Canon's own tables index
by "Scene Linear %", with reflection equal to scene linear times 0.9. This node treats its input as direct
scene-linear reflectance, like every other curve in the list. Measured: at linear 0.20, the value Canon's table
calls the 18% grey row, the node returns 0.343389 against Canon's tabulated code 351/1023 = 0.343109, a
difference of 2.8e-04. At direct linear 0.18 it returns 0.330958.

**Continuity at the piecewise cut points.** A log curve made of two or three branches must meet in value and in
slope, or a gradient develops a visible kink in the midtones. Every cut point in all ten curves was checked in
two ways: a finite-difference slope on each side, and a closed-form derivative worked out from the same
constants the code uses. The closed-form result is the one to trust, because a finite difference at a very small
cut runs into float32 output quantisation.

| curve | cut (scene-linear) | slope below | slope above | mismatch | verdict |
| --- | --- | --- | --- | --- | --- |
| ACEScct | 0.0078125 | 10.540238 | 10.540238 | 2.5e-15 | C1 |
| ACEScc | 2^-15 | 1349.150431 | 2698.300862 | 0.5 | **kink, by the spec** |
| ACEScc | 0.0 | 0.000000 | 2698.300862 | 1.0 | **kink, by the spec** |
| ARRI LogC3 | 0.010591 | 5.367655 | 5.367674 | 3.5e-06 | C1 |
| ARRI LogC4 | -0.018057 | 8.803033 | 8.803033 | 0 | C1, exactly |
| Sony S-Log3 | 0.01125 | 6.621944 | 5.224220 | 0.2111 | **kink, by the spec** |
| Panasonic V-Log | 0.01 | 5.600000 | 5.600011 | 1.9e-06 | C1 |
| Canon Log 3 | -0.014 | 1.975481 | 1.975480 | 3.9e-07 | C1 |
| Canon Log 3 | +0.014 | 1.975480 | 1.975481 | 3.9e-07 | C1 |
| RED Log3G10 | -0.01 | 15.192700 | 15.192689 | 7.5e-07 | C1 |
| DaVinci Intermediate | 0.00262409 | 10.444269 | 10.444267 | 1.6e-07 | C1 |

`Cineon` has no cut point at all; it is a single analytic branch.

Value continuity holds everywhere. Evaluating each cut at plus and minus a step of 1e-9 relative gave a worst
value gap of 2.98e-07 across all 20 cut points, which is float32 resolution.

Three of those mismatches are real, and none of them belong to this pack.

- **Sony S-Log3** is not C1 at 0.01125 in Sony's own published definition. Deriving the two slopes straight from
  the constants in Sony's *Technical Summary for S-Gamut3.Cine/S-Log3 and S-Gamut3/S-Log3* gives 6.621944 on the
  linear branch and 5.224220 on the log branch, a 21.1% step. The node measures 6.622738 and 5.231963, matching
  the published slopes to 8e-04 and 8e-03. The two branches meet in value to 3.85e-14. So the kink is Sony's,
  the node reproduces it faithfully, and any correct S-Log3 implementation has it. It sits at linear 0.01125,
  roughly four stops under mid grey, so it lands in deep shadow rather than in the midtones.
- **ACEScc** breaks C1 twice by definition in ACES S-2014-003: below linear 0.0 the spec returns a constant, so
  the slope is 0 on one side, and at 2^-15 the middle branch carries exactly half the slope of the log branch.
  Both were derived from the spec's own three-part formula and both match what the node does. If a smooth toe
  matters, use `ACEScct`, which exists for that reason and measured C1 to 2.5e-15.
- Nothing else. The other eight curves meet in slope to better than 4e-06 relative, and the residual is the
  makers' own rounding of their published constants, not arithmetic in this pack.

**HDR safety, and which two curves are the exception.** There is no `max(x, 0)` anywhere in the curve code -
where a logarithm needs a positive argument, it is the ARGUMENT that is clamped, never the input, so below-black
survives wherever the curve is still defined for it. Measured on linear -1.0, -0.2, -0.05, -0.01, -0.001, 0.0
through `Linear to Log`:

| curve | below-black on that set | floor |
| --- | --- | --- |
| `ACEScct`, `ARRI LogC3`, `ARRI LogC4`, `Sony S-Log3`, `Panasonic V-Log`, `Canon Log 3`, `RED Log3G10`, `DaVinci Intermediate` | strictly increasing, all six distinct | none on this set |
| **`Cineon`** | 4 of 6 distinct | every input at or below linear **-0.010916** returns **-2.262952** |
| **`ACEScc`** | **1 of 6** - every negative lands on one code | **-0.358447**, for all x <= 0 |

Encoded values below 0 and above 1 also decode on every curve: `Log to Linear` on codes -0.2, -0.05, 1.05 and
1.5 returns finite, monotonic linear values.

**Both exceptions are the published definitions, not a shortcut in this pack.** `Cineon` takes a base-10
logarithm of an affine function of the input, and that argument is floored at 1e-10 because the logarithm is
undefined below it; the argument reaches the floor at linear -0.010916, and above that the curve is live for
negatives too (linear -0.0109 encodes to -0.741296). `ACEScc` is defined with a separate branch for x <= 0 that
evaluates `log2(2^-16)`, a constant, so **every** negative encodes to the same code by specification - which is
precisely why `ACEScct` exists and is the curve to use when below-black has to survive a log round trip.

Neither is invertible in its floored region, and the pack's clipped-range warning is suppressed by name for
these two (`_FLOORS_NEGATIVES` in `nodes.py`) so that a correct `ACEScc` or `Cineon` encode does not raise an
alarm on every use. The other eight have a finite linear toe segment through zero and no such floor.

### Worked chains

**Undo a camera log curve. The full and correct form.**

```
OCIO Read
     source           = <path to your Sony clip>
     raw_data         = true            (do not let Read convert; LogConvert will)
  into OCIO LogConvert
     operation        = Log to Linear
     curve            = Sony S-Log3
  into OCIO ColorSpace
     in_colorspace    = Linear S-Gamut3.Cine
     out_colorspace   = ACEScg
  into OCIO Player
```

The second step is not optional. Measured on a chromatic patch, both routes below agree to 4.29e-06 absolute:

- `OCIO LogConvert` (Log to Linear, Sony S-Log3) then `OCIO ColorSpace` (`Linear S-Gamut3` into `ACEScg`)
- `OCIO ColorSpace` (`S-Log3 S-Gamut3` into `ACEScg`) on its own

Stopping after `OCIO LogConvert` and calling the result ACEScg is off by **0.3791 absolute** on the same patch.
For an S-Log3 triplet of `[0.65, 0.35, 0.30]` the correct ACEScg answer is `[1.7722, 0.0584, 0.0550]` and the
curve-only answer is `[1.6323, 0.1001, 0.0602]`. The error is a gamut error, so it shows as a hue and saturation
shift that no exposure correction fixes.

**Fold an HDR render into 0..1 for a model that only takes 0..1.**

```
OCIO Read           (an ACEScg EXR sequence, values above 1.0 present)
  into OCIO LogConvert
     operation      = Linear to Log
     curve          = ACEScct
  into <the model>
  into OCIO VAE Decode        precision = float32, clamp = false
  into OCIO LogConvert
     operation      = Log to Linear
     curve          = ACEScct
  into OCIO Write             input_colorspace = ACEScg, EXR 16f
```

`ACEScct` reaches 222.861 at code 1.0, so it covers an HDR render, and it is C1 at its cut. The log into linear
into log round trip on this curve measured 2.38e-08 absolute, so the fold costs nothing.

**Decode an ARRI LogC3 image that is not from an ARRI camera.** A generated image carrying the LogC3 curve on
Rec.709 primaries needs the curve undone and the primaries left alone:

```
VAE Decode
  into OCIO LogConvert    operation = Log to Linear, curve = ARRI LogC3
  into OCIO ColorSpace    in_colorspace = Linear Rec.709 (sRGB), out_colorspace = ACEScg
  into OCIO Write
```

Using the config's `ARRI LogC3 (EI800)` colorspace here would apply an ARRI Wide Gamut 3 matrix that the data
never had.

### Traps

- **`OCIO LogConvert` against `OCIO ColorSpace` is the choice people get wrong.** `OCIO LogConvert` applies the
  transfer curve only. The config's same-named colorspace applies the curve **and** the camera's gamut matrix.
  For a real camera plate in its native gamut, one `OCIO ColorSpace` step does the whole job and is simpler. For
  data that only carries the curve, which is the normal case for a generated image, `OCIO LogConvert` plus a
  separate gamut step is the only correct route. Measured cost of getting it wrong: 0.3791 absolute on an
  ordinary patch.
- **`Canon Log 3` here and `CanonLog3 CinemaGamut D55` in the config disagree by exactly 1/0.9.** Measured over
  codes 0.2 to 1.0, this node's decode is 1.11111111 times the config's, at every sample, and multiplying this
  node's output by 0.9 matches the config to 6.68e-06 absolute, which is float32 quantisation at the linear
  magnitudes involved rather than a difference in the maths. That is Canon's 0.9 reflectance convention. Pick
  one convention for a shot and stay on it; mixing them puts a 0.152 stop exposure error into the plate (a factor of 1/0.9) with no
  error message. The other eight comparable curves agree with the config to between 2.3e-06 and 5.1e-05
  relative (see the parity table in Part 10).
- Decoding with the wrong curve is silent. A LogC3 plate decoded as LogC4 is dark and flat, not broken.
- `Cineon` `Linear to Log` floors below linear -0.010916 and is not invertible there.

### What could not be verified here

The ARRI LogC4 mid-grey figure of 0.2784 was read from ARRI's published LogC4 specification material through a
web search restricted to ARRI's own domain. The specification PDF itself could not be machine-extracted in this
environment, so the number is confirmed against ARRI's published statement rather than against a line quoted out
of the PDF body. The LogC3 0.391 figure has the same status. Every other spec value in the anchor table was
checked against the maker's own tabulated code value as cited in the pack's source comments, and the pack's
arithmetic was independently rederived here.

---

## Part 4: `OCIO Display`

### What it is for

Runs the scene-referred to display-referred step: a display device plus a view transform, both named by the
config. This is what makes a linear render look correct on a monitor. Nuke equivalent: **OCIODisplay**.

### Inputs

| Name | Type | Default | Allowed values |
| --- | --- | --- | --- |
| `in_colorspace` | combo | `ACES2065-1` | the 55 names, verbatim |
| `display` | combo | `sRGB - Display` | the 9 names below |
| `view` | combo | `ACES 2.0 - SDR 100 nits (Rec.709)` | the 14 names below |
| `invert_direction` | `BOOLEAN` | `false` | off reads `Forward (scene -> display)`, on reads `Inverse (display -> scene)` |
| `mix` | `FLOAT` | `1.0` | 0.0 to 1.0 |
| `image` | `IMAGE` (optional) | | as Part 1 |
| `video` | `VIDEO` (optional) | | as Part 1 |
| `config_path` | combo (optional) | `(built-in ACES config)` | as Part 1 |

The 9 `display` values, exact order from `/object_info`:

```
sRGB - Display
Display P3 - Display
Display P3 HDR - Display
Gamma 2.2 Rec.709 - Display
P3-D65 - Display
Rec.1886 Rec.709 - Display
Rec.2100-HLG - Display
Rec.2100-PQ - Display
ST2084-P3-D65 - Display
```

The 14 `view` values, exact order from `/object_info`:

```
ACES 2.0 - SDR 100 nits (Rec.709)
Un-tone-mapped
Video (colorimetric)
Raw
ACES 2.0 - SDR 100 nits (P3 D65)
ACES 2.0 - HDR 1000 nits (P3 D65)
ACES 2.0 - HDR 1000 nits (Rec.2020)
ACES 2.0 - HDR 2000 nits (P3 D65)
ACES 2.0 - HDR 2000 nits (Rec.2020)
ACES 2.0 - HDR 4000 nits (P3 D65)
ACES 2.0 - HDR 4000 nits (Rec.2020)
ACES 2.0 - HDR 500 nits (P3 D65)
ACES 2.0 - HDR 500 nits (Rec.2020)
ACES 2.0 - HDR 108 nits (P3 D65)
```

**That view list is the union across all nine displays, and most pairs are invalid.** The combo does not narrow
when you pick a display. Choosing an unsupported pair fails the job with `Exception: DisplayViewTransform error.
The display 'sRGB - Display' does not have view 'ACES 2.0 - HDR 1000 nits (P3 D65)'.` Confirmed by running it.

Valid pairs, queried from the live config:

| display | views it actually has |
| --- | --- |
| `sRGB - Display` | `ACES 2.0 - SDR 100 nits (Rec.709)`, `Un-tone-mapped`, `Video (colorimetric)`, `Raw` |
| `Display P3 - Display` | `ACES 2.0 - SDR 100 nits (P3 D65)`, `Un-tone-mapped`, `Video (colorimetric)`, `Raw` |
| `Display P3 HDR - Display` | `ACES 2.0 - HDR 1000 nits (P3 D65)`, `ACES 2.0 - SDR 100 nits (Rec.709)`, `Un-tone-mapped`, `Video (colorimetric)`, `Raw` |
| `Gamma 2.2 Rec.709 - Display` | `ACES 2.0 - SDR 100 nits (Rec.709)`, `Un-tone-mapped`, `Video (colorimetric)`, `Raw` |
| `P3-D65 - Display` | `ACES 2.0 - SDR 100 nits (P3 D65)`, `ACES 2.0 - SDR 100 nits (Rec.709)`, `Un-tone-mapped`, `Video (colorimetric)`, `Raw` |
| `Rec.1886 Rec.709 - Display` | `ACES 2.0 - SDR 100 nits (Rec.709)`, `Un-tone-mapped`, `Video (colorimetric)`, `Raw` |
| `Rec.2100-HLG - Display` | `ACES 2.0 - HDR 1000 nits (P3 D65)`, `Un-tone-mapped`, `Video (colorimetric)`, `Raw` |
| `Rec.2100-PQ - Display` | all six `ACES 2.0 - HDR *` views, plus `ACES 2.0 - SDR 100 nits (Rec.709)`, `Un-tone-mapped`, `Video (colorimetric)`, `Raw` |
| `ST2084-P3-D65 - Display` | the four P3 D65 HDR views, `ACES 2.0 - HDR 108 nits (P3 D65)`, `ACES 2.0 - SDR 100 nits (Rec.709)`, `Un-tone-mapped`, `Video (colorimetric)`, `Raw` |

**What you wire in.** The same `IMAGE` and `VIDEO` sources as the other operators, but the upstream should be
scene-linear and already graded. This node belongs at the end.

### Outputs

`IMAGE` at index 0, `VIDEO` at index 1. Downstream: `Preview Image`, `Save Image`, `Save Video`, `SaveWEBM`, or
`OCIO Write` with `raw_data = true` so the Write does not convert again. Do not send this output into another
grade.

### The colour behaviour that decides whether the result is right

**Direction.** `invert_direction = false` runs `in_colorspace` into the display and view. `true` runs the
display back to `in_colorspace`. Verified rather than assumed: ACEScg 0.18 forward through `sRGB - Display` with
`ACES 2.0 - SDR 100 nits (Rec.709)` gives 0.349187, and the inverse of 0.349187 returns 0.179996.

**This transform is lossy, and that is the single most important fact about it.** Measured on `sRGB - Display`
with `ACES 2.0 - SDR 100 nits (Rec.709)`:

| ACEScg in | display out |
| --- | --- |
| -5.0 | 0.0 |
| -1.0 | 0.0 |
| -0.1 | 0.0 |
| -0.001 | 0.0 |
| 0.0 | 0.0 |
| 0.18 | 0.349187 |
| 1.0 | 0.706683 |
| 8.0 | 0.947917 |
| 100.0 | 0.998954 |
| 128.0 | 1.000007 |
| 200.0 | 1.000007 |
| 1000.0 | 1.000007 |

Every negative lands on 0.0, a hard floor. A binary search found the highest input still below the ceiling:
**above ACEScg 127.8616 the forward output no longer changes.** So below black is gone and above 128 is
compressed to one value. That is the ACES 2.0 Output Transform doing its job, not a pack defect, and it is why
this node cannot sit in the middle of a chain.

The round trip proves the loss. Encoding the ramp -1.0 to 5000.0 forward and inverting it back gave a maximum
absolute error of **4872.0**: 5000.0 came back as 128.0, and -1.0 came back as 0.0. In the range the view
actually covers the inverse is good, with 0.18 recovered as 0.179996 and 8.0 as 7.999804.

**Three views do not tone map, and behave completely differently.** Measured on `sRGB - Display` over the same
ramp:

| view | -1.0 | -0.1 | 0.18 | 5000.0 | clamps? |
| --- | --- | --- | --- | --- | --- |
| `ACES 2.0 - SDR 100 nits (Rec.709)` | 0.0 | 0.0 | 0.349187 | 1.000007 | yes, both ends |
| `Un-tone-mapped` | -1.00001 | -0.34919 | 0.461355 | 36.6302 | no |
| `Video (colorimetric)` | -1.00001 | -0.34919 | 0.461355 | 36.6302 | no |
| `Raw` | -1.0 | -0.1 | 0.18 | 5000.0 | no, exact pass-through |

`Un-tone-mapped` and `Video (colorimetric)` on `sRGB - Display` produced numbers identical to `OCIO ColorSpace`
into `sRGB - Display` on this neutral ramp, with a maximum deviation of exactly 0 for both. `Raw` returns the
input untouched, which makes it a quick way to prove the node is wired but inert.

**`mix` interpolates across the two encodings.** See the table in Part 1. Half a display transform is not a
softer display transform.

### Worked chains

**Look at a linear render through a Rec.709 display transform.**

```
VAE Decode  or  OCIO Read       (scene-linear ACEScg)
  into OCIO ColorSpace          in_colorspace = Linear Rec.709 (sRGB), out_colorspace = ACEScg
  into OCIO CDLTransform        (grade here, in ACEScg)
  into OCIO Display
       in_colorspace     = ACEScg
       display           = sRGB - Display
       view              = ACES 2.0 - SDR 100 nits (Rec.709)
       invert_direction  = false
  into Preview Image
```

Nothing after `OCIO Display` except a viewer or a writer.

**Recover scene-linear from a graded Rec.709 file, to work on it in ACEScg.**

```
Load Image                      (a Rec.709 display-referred still)
  into OCIO Display
       in_colorspace     = ACEScg
       display           = Rec.1886 Rec.709 - Display
       view              = ACES 2.0 - SDR 100 nits (Rec.709)
       invert_direction  = true
  into OCIO CDLTransform        (now grading in scene-linear ACEScg)
  into OCIO Display             (a second, forward instance to view it)
  into Preview Image
```

The inverse cannot invent what the forward transform discarded. Anything that was clipped to 0 or to 1 in the
file comes back as 0 or as roughly 128.

**Master for an HDR PQ deliverable.**

```
OCIO ColorSpace   (graded ACEScg)
  into OCIO Display
       in_colorspace = ACEScg
       display       = Rec.2100-PQ - Display
       view          = ACES 2.0 - HDR 1000 nits (Rec.2020)
  into OCIO Write        input_colorspace = Rec.2100-PQ - Display, raw_data = true
```

Check the pair against the table above before queueing, since `Rec.2100-PQ - Display` accepts that view and
`sRGB - Display` does not.

### Traps

- **A display transform ends a chain.** Grading after it is wrong. The measurements above show why: negatives are
  already 0 and highlights above 128 are already one value, so a lift or a gain after this node works on
  destroyed data.
- **The view combo lists views your display does not have.** Nine of the fourteen are invalid on
  `sRGB - Display`. There is no warning until the job fails.
- Sending this output into `OCIO Write` without `raw_data = true` converts twice.
- The `invert_direction` toggle names its own two states in the UI, and the inverse is only ever as good as the
  range the forward view kept.

### What could not be verified here

The tone-mapped views were measured on `sRGB - Display` with `ACES 2.0 - SDR 100 nits (Rec.709)`. The clipping
points of the HDR views (500, 1000, 2000, 4000 nits) were not measured, and they will be higher. If you master
HDR, measure the ceiling of your own display and view pair with the same binary search rather than assuming
127.86 carries over.

---

## Part 5: `OCIO CDLTransform`

### What it is for

An ASC CDL primary grade: slope, offset and power per channel, plus a saturation term. This is the interchange
format a colourist hands over, so it is the node to type a supplied CDL into. Nuke equivalent:
**OCIOCDLTransform**.

### Inputs

| Name | Type | Default | Range |
| --- | --- | --- | --- |
| `slope_r`, `slope_g`, `slope_b` | `FLOAT` | `1.0` | -10.0 to 10.0, step 0.001 |
| `offset_r`, `offset_g`, `offset_b` | `FLOAT` | `0.0` | -10.0 to 10.0, step 0.001 |
| `power_r`, `power_g`, `power_b` | `FLOAT` | `1.0` | -10.0 to 10.0, step 0.001 |
| `saturation` | `FLOAT` | `1.0` | 0.0 to 4.0, step 0.001 |
| `direction` | combo | `forward` | `forward`, `inverse` |
| `mix` | `FLOAT` | `1.0` | 0.0 to 1.0 |
| `image` | `IMAGE` (optional) | | as Part 1 |
| `video` | `VIDEO` (optional) | | as Part 1 |

All confirmed from `/object_info`. There is no `config_path` and no colorspace widget: the grade is applied to
whatever numbers arrive, in whatever encoding they are in.

**What you wire in.** Any `IMAGE` or `VIDEO` source. Put it after your working-space conversion and before any
display transform. The nine float widgets accept a connected `FLOAT` output too, so a `PrimitiveNode` or any
node with a `FLOAT` output can drive them.

### Outputs

`IMAGE` at index 0, `VIDEO` at index 1. Downstream: another grade, `OCIO Display`, `OCIO Player`, `OCIO Write`.

### The colour behaviour that decides whether the result is right

**Direction** is the `direction` combo, `forward` or `inverse`. Round trip on a ramp of -1.0 to 100.0 with
slope 1.2/1.0/0.85, offset 0.02/0.0/-0.01, power 0.95/1.0/1.05, saturation 1.15: maximum absolute error
**0.00132** (on the input of 100.0), maximum relative error **2.32e-05** over inputs above 1e-3.

**All defaults is a true no-op.** Slope 1, offset 0, power 1, saturation 1 returned a bit-identical tensor.

**Values below 0 and above 1 survive.** No clamping was found anywhere. An identity CDL on -10.0, -1.0, 1.0,
10.0, 1000.0 returned those five values unchanged. An offset of +0.5 on the same set returned -9.5, -0.5, 1.5,
10.5, 1000.5. A slope of 2.0 on negatives returned exactly double.

**Power leaves negatives alone. It does not mirror them.** This is the sign behaviour to know, and it is
measured, not inferred from the operator name:

| in | -1.0 | -0.5 | -0.1 | 0.0 | 0.1 | 0.5 | 1.0 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `power = 0.45` | -1.0 | -0.5 | -0.1 | 0.0 | 0.354813 | 0.732044 | 1.000007 |
| `power = 2.2` | -1.0 | -0.5 | -0.1 | 0.0 | 0.006310 | 0.217642 | 1.000022 |

So the power stage is `x` for `x <= 0` and `x ** p` above, which is OCIO's non-clamping CDL behaviour. Compare
the two candidates a hand-rolled implementation might use:

- `clamp(x, 0) ** p` would have returned 0.0 for every negative. It did not, so negatives are safe.
- `sign(x) * abs(x) ** p` would have returned -0.732044 for -0.5 at power 0.45. It did not.

The practical consequence: for any power other than 1.0 the operator is continuous in value at 0 but changes
slope there. On scene-linear data with real below-black values, a strong power adjustment treats the two sides of
zero differently. It does not destroy them.

**Saturation weights, measured.** Setting `saturation = 0.0` collapses to luma, so the result on a pure primary
is the weight itself:

| input | `saturation = 0.0` output |
| --- | --- |
| `[1, 0, 0]` | `[0.2126, 0.2126, 0.2126]` |
| `[0, 1, 0]` | `[0.7152, 0.7152, 0.7152]` |
| `[0, 0, 1]` | `[0.0722, 0.0722, 0.0722]` |

Those are the Rec.709 luma coefficients that the ASC CDL specifies. Confirmed by measurement rather than read
from a name.

Saturation above 1.0 creates negatives out of nothing. Measured: `saturation = 2.0` turns `[1, 0, 0]` into
`[1.7874, -0.2126, -0.2126]`. That is correct arithmetic and it is a reason to keep an eye on the low end after
a saturation push.

**Precision note.** OCIO's default CPU processor is not exact at large magnitudes. `power = 2.0` on an input of
1000.0 returned 999988.5625 rather than 1000000.0, a relative error of 1.14e-05. Fine for a grade, worth knowing
if you are diffing pixel values.

### Worked chains

**Grade with a CDL supplied by the colourist.**

```
OCIO Read
     source          = <path to your plate>
     output_colorspace = ACEScg
  into OCIO CDLTransform
     slope_r/g/b     = 1.032  0.998  0.954     (from the .cdl the colourist sent)
     offset_r/g/b    = 0.004  0.000 -0.006
     power_r/g/b     = 1.000  1.010  1.000
     saturation      = 1.020
     direction       = forward
     mix             = 1.0
  into OCIO Display     display = sRGB - Display, view = ACES 2.0 - SDR 100 nits (Rec.709)
  into Preview Image
```

Apply a CDL in the space the colourist graded in. A CDL authored on log codes gives a different result on
scene-linear ACEScg, because slope is a multiply and multiplying a log code is not a gain.

**Remove a baked-in CDL from a plate.**

```
OCIO Read       (the already-graded plate)
  into OCIO CDLTransform
       (the same nine numbers and the same saturation)
       direction  = inverse
  into OCIO Player
```

The inverse recovered the original to 2.32e-05 relative in the measurement above, so this is a real undo, not an
approximation, provided every number matches.

**Grade a CDL on log codes, the way an on-set CDL was authored.**

```
OCIO Read           raw_data = true          (leave the camera log alone)
  into OCIO CDLTransform    (the CDL as authored, on log codes)
  into OCIO LogConvert      operation = Log to Linear, curve = ARRI LogC3
  into OCIO ColorSpace      in_colorspace = Linear ARRI Wide Gamut 3, out_colorspace = ACEScg
  into OCIO Write
```

### Traps

- The node has no idea what encoding its input is in. The same nine numbers give different pictures on log codes
  and on scene-linear. Match the CDL to the space it was authored in.
- `saturation` runs 0.0 to 4.0 while slope, offset and power run -10.0 to 10.0. A negative saturation cannot be
  entered here even though the arithmetic would accept it.
- `saturation` above 1.0 pushes saturated colours below 0.
- A power other than 1.0 changes slope at zero, so a heavy power on data with below-black values is not
  symmetric about zero.
- `mix` blends toward the ungraded input, so `mix = 0.5` is not "half the grade" in any perceptual sense, just
  half the numeric difference.

### What could not be verified here

`direction = inverse` was tested with one parameter set. A CDL with a slope near 0 or a negative slope is
mathematically hard to invert, and that case was not measured. The widget allows slope down to -10.0.

---

## Part 6: `OCIO FileTransform`

### What it is for

Applies a LUT or a grade file from disk: `.cube`, `.3dl`, `.spi1d`, `.spi3d`, `.csp`, `.ccc`, `.cdl`, `.clf`,
`.lut`. Use it for a show look, a camera vendor LUT, or a `.cdl` file rather than typed numbers. Nuke
equivalent: **OCIOFileTransform**.

### Inputs

| Name | Type | Default | Allowed values |
| --- | --- | --- | --- |
| `file_path` | combo | **no default declared** | every LUT-like file found in the ComfyUI input folder |
| `interpolation` | combo | `linear` | `linear`, `nearest`, `tetrahedral`, `best` |
| `direction` | combo | `forward` | `forward`, `inverse` |
| `mix` | `FLOAT` | `1.0` | 0.0 to 1.0 |
| `image` | `IMAGE` (optional) | | as Part 1 |
| `video` | `VIDEO` (optional) | | as Part 1 |

`file_path` is generated from your own input folder, so its contents differ on every machine. On the server used
here, `/object_info` returned exactly two entries, and they show the two accepted path forms:

```
ocio_assets/warm_demo.cube
warm_demo.cube
```

A path in a subfolder is written with a forward slash relative to the input folder. Both forms were run and gave
bit-identical results.

Two behaviours of this widget are worth knowing.

- `/object_info` shows **no `default` key** for `file_path`, only a tooltip. The effective default is therefore
  the first entry in the scanned list, which changes as you add files. Confirmed from the raw `/object_info`
  response. A saved workflow keeps whatever value it stored, so this only affects a freshly dropped node.
- On an install with **no** LUT in the input folder, the widget is not a combo at all. The code falls back to a
  free-text `STRING` widget whose tooltip begins `No LUT in the input folder yet. Drop a .cube/.3dl/.spi1d there,
  or type a path.` Confirmed by reading the builder. This is why the widget type can differ between two machines.
  That tooltip is being extended in the source tree with a warning about the LUT domain, so expect more text after
  that sentence once the next build is deployed. The widget behaviour is unchanged.

**What you wire in.** Any `IMAGE` or `VIDEO` source. The LUT file itself is not a socket: pick it from the combo
or press **upload LUT file**.

### Outputs

`IMAGE` at index 0, `VIDEO` at index 1. A creative look LUT is display-referred, so its output usually goes
straight to `Preview Image`, `Save Image`, `Save Video` or `OCIO Write` with `raw_data = true`.

### The colour behaviour that decides whether the result is right

Measurements below use the demo `.cube` shipped in the input folder here: a 2x2x2 3D LUT that multiplies R by
1.05, G by 0.95 and B by 0.90.

**Direction** is the `direction` combo. `forward` applies the file, `inverse` asks OCIO to invert it.

**The LUT domain clamps, at both ends.** This is the behaviour that quietly destroys HDR:

| input | R out | G out | B out |
| --- | --- | --- | --- |
| -0.5 | 0.0 | 0.0 | 0.0 |
| 0.0 | 0.0 | 0.0 | 0.0 |
| 0.25 | 0.2625 | 0.2375 | 0.225 |
| 0.5 | 0.525 | 0.475 | 0.45 |
| 1.0 | 1.05 | 0.95 | 0.90 |
| 1.5 | 1.05 | 0.95 | 0.90 |
| 4.0 | 1.05 | 0.95 | 0.90 |
| 100.0 | 1.05 | 0.95 | 0.90 |

Inputs of 1.5, 4.0 and 100.0 produce the identical triplet. Everything above the LUT's input domain is one
colour, and every negative is floored. A `.cube` file declares a domain, and for creative look LUTs that domain
is almost always 0..1. So this node is the one place in the pack where scene-linear HDR is lost by design, and it
is the file's design rather than the node's.

Feed a LUT log codes or display codes, both of which live in 0..1, and there is nothing to lose.

**The inverse works inside the domain and not outside it.** Measured, forward then inverse:

| in | 0.0 | 0.1 | 0.25 | 0.5 | 0.75 | 0.9 | 0.95 | 1.0 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| forward | 0.0 | 0.105 | 0.2625 | 0.525 | 0.7875 | 0.945 | 0.9975 | 1.05 |
| inverse | 0.0 | 0.1 | 0.25 | 0.5 | 0.75 | 0.9 | 0.95 | 0.942586 |

Where the forward output stayed at or below 1.0 the worst round-trip error was **1.19e-07**. Where the forward
output went above 1.0, the error was **0.0574**. So the inverse of a LUT is trustworthy only over the part of the
range whose forward output stays inside 0..1.

**Interpolation.** All four modes produced identical output on this LUT. That is expected for a 2x2x2 lattice,
where trilinear and tetrahedral interpolation of a linear ramp agree exactly, so the measurement shows the
widget is wired and says nothing about the modes' relative accuracy. `tetrahedral` is the usual choice for a
33-cube creative LUT.

**A missing file fails loudly**, before any pixels move: `RuntimeError: LUT file not found: nope.cube`.

**Alpha and `mix`** behave as in Part 1.

### Worked chains

**Apply a `.cube` show look at the end of a chain.**

```
OCIO ColorSpace     (graded, ACEScg)
  into OCIO Display
       in_colorspace = ACEScg
       display       = sRGB - Display
       view          = ACES 2.0 - SDR 100 nits (Rec.709)
  into OCIO FileTransform
       file_path     = show_look.cube          (pick from the combo, or upload it)
       interpolation = tetrahedral
       direction     = forward
       mix           = 1.0
  into Preview Image
```

The display transform comes first on purpose. The look LUT expects display-referred 0..1 codes, and after
`OCIO Display` that is what it gets. Reverse the order and the LUT clamps your scene-linear highlights to one
value.

**Dial a look back without re-authoring the file.** Set `mix = 0.6` on the same node. Measured to be an exact
lerp between input and LUT output.

**Undo a LUT that a vendor baked into a delivered file.**

```
Load Image           (the file with the look already in it)
  into OCIO FileTransform
       file_path  = the same vendor LUT
       direction  = inverse
  into OCIO Display     invert_direction = true       (back to scene-linear)
  into OCIO CDLTransform
```

Only trust this where the forward LUT output stayed inside 0..1. See the inverse table above.

**Apply a `.cdl` or `.ccc` file instead of typing numbers into `OCIO CDLTransform`.** Drop the file in the input
folder and select it here. That path was not measured, since no `.cdl` file was present on this machine; it is
listed because the node's own file scanner accepts those extensions.

### Traps

- **A 0..1 LUT throws away HDR.** Inputs of 1.5, 4.0 and 100.0 all came out identical. Put the LUT after the
  display transform, or accept the clip.
- The LUT is applied to whatever numbers arrive. A creative look fed scene-linear data is wrong even before the
  clip.
- `interpolation = nearest` on a coarse LUT produces banding. It is in the list because OCIO offers it.
- The combo contents depend on your input folder, so a workflow shared with someone else can arrive with a
  `file_path` value that does not exist on their machine, and the job will fail with the `LUT file not found`
  error above.
- With no LUT in the input folder the widget is a text field rather than a combo, which changes how an API call
  must supply it.

### What could not be verified here

Only a 2x2x2 `.cube` was available, so no measurement distinguishes the four interpolation modes, and no
measurement covers `.3dl`, `.spi1d`, `.spi3d`, `.csp`, `.ccc`, `.cdl`, `.clf` or `.lut`. A 1D LUT (`.spi1d`) has
different domain behaviour from the 3D case measured above and should be checked on your own file. A LUT that
declares a domain wider than 0..1 was also not tested; the clamp measured here is at 0..1 because that is what
this file declares.

---

## Part 7: `OCIO LookTransform`

### What it is for

Runs a named look from the config as part of a colorspace conversion: input space, then the look, then output
space, as one processor. In this config the only look is the ACES 1.3 Reference Gamut Compression, which pulls
out-of-gamut values back into a usable range. Nuke equivalent: **OCIOLookTransform**.

### Inputs

| Name | Type | Default | Allowed values |
| --- | --- | --- | --- |
| `in_colorspace` | combo | `ACES2065-1` | the 55 names, verbatim |
| `out_colorspace` | combo | `sRGB - Display` | the 55 names, verbatim |
| `look` | combo | `(none)` | `(none)`, `ACES 1.3 Reference Gamut Compression` |
| `invert_direction` | `BOOLEAN` | `false` | off reads `Forward (in -> out)`, on reads `Inverse (out -> in)` |
| `mix` | `FLOAT` | `1.0` | 0.0 to 1.0 |
| `image` | `IMAGE` (optional) | | as Part 1 |
| `video` | `VIDEO` (optional) | | as Part 1 |
| `config_path` | combo (optional) | `(built-in ACES config)` | as Part 1 |

The `look` list is exactly two entries here, confirmed from `/object_info`. Load a config with more looks and
the list grows; the first entry is always `(none)`.

Note the different default from `OCIO ColorSpace`: `out_colorspace` defaults to `sRGB - Display`, so a freshly
dropped node is already configured as a display-referred step.

**What you wire in.** Any `IMAGE` or `VIDEO` source. For gamut compression the input should be scene-linear.

### Outputs

`IMAGE` at index 0, `VIDEO` at index 1. With `look = (none)` this node is interchangeable with
`OCIO ColorSpace`, so the same downstream applies.

### The colour behaviour that decides whether the result is right

**With `look = (none)` this node is exactly `OCIO ColorSpace`.** Measured: ACEScg into `sRGB - Display` through
both nodes gave a maximum difference of **0**, and ACEScg into ACEScg with `(none)` returned a bit-identical
tensor. So `(none)` means no look, with no hidden extra step.

**Direction, verified rather than assumed.** `invert_direction = true` runs `out_colorspace` into
`in_colorspace`, which means it swaps the two colorspaces as well as inverting the look. Proof: with
`look = (none)`, `in_colorspace = ACEScg`, `out_colorspace = sRGB - Display` and `invert_direction = true`, the
output was identical (maximum difference 0) to `OCIO ColorSpace` configured `sRGB - Display` into `ACEScg`.

**What the Reference Gamut Compression actually does.** Measured with `in_colorspace` and `out_colorspace` both
ACEScg, so only the look is active:

| in | out |
| --- | --- |
| `[0.18, 0.18, 0.18]` | `[0.18, 0.18, 0.18]` |
| `[1.0, 0.2, 0.1]` | `[1.0, 0.2, 0.1014]` |
| `[0.05, 1.4, 0.2]` | `[0.1001, 1.4, 0.2]` |
| `[-0.30, 0.60, 1.80]` | `[-0.0095, 0.60, 1.80]` |
| `[8.0, 2.0, 0.5]` | `[8.0, 2.0, 0.5961]` |

Neutrals pass through untouched. Out-of-gamut negatives are pulled most of the way back to 0 without being
clipped there, which is the point of the look: the -0.30 became -0.0095, not 0.0. Highlights above 1.0 are kept
(1.80 and 8.0 are unchanged).

**Values below 0 and above 1 survive.** The look does not clamp. The minimum over that patch set was -0.0095 and
the maximum was 8.0.

**Round-trip.** With `in_colorspace` equal to `out_colorspace`, forward then inverse recovered the patch set to
**4.77e-07** absolute. With `in_colorspace = ACEScg` and `out_colorspace = sRGB - Display`, forward then inverse
recovered it to **2.11e-04** absolute, the larger figure coming from the display encoding's float32 error rather
than from the look.

### Worked chains

**Tame out-of-gamut colour before a display transform.**

```
VAE Decode  or  OCIO Read      (scene-linear ACEScg, saturated emissive elements)
  into OCIO LookTransform
       in_colorspace    = ACEScg
       out_colorspace   = ACEScg
       look             = ACES 1.3 Reference Gamut Compression
       invert_direction = false
       mix              = 1.0
  into OCIO CDLTransform
  into OCIO Display     display = sRGB - Display, view = ACES 2.0 - SDR 100 nits (Rec.709)
  into Preview Image
```

Keeping both colorspaces the same isolates the look, which is what you want when its job is gamut mapping and
you have your own conversion downstream.

**Look and display step in one node.**

```
OCIO ColorSpace    (graded ACEScg)
  into OCIO LookTransform
       in_colorspace  = ACEScg
       out_colorspace = sRGB - Display
       look           = ACES 1.3 Reference Gamut Compression
  into OCIO Write     input_colorspace = sRGB - Display, raw_data = true
```

Fewer nodes, and one processor, so this is the faster form when you do not need to grade between the look and
the conversion.

**Undo the gamut compression.** Same node, `invert_direction = true`, and remember that it swaps the two
colorspaces too. To invert the isolated form above, leave both colorspaces at ACEScg and flip the boolean.

### Traps

- `invert_direction` reverses the colorspaces as well as the look. It is not a look-only bypass. For that, set
  `look = (none)`.
- The default `out_colorspace` is `sRGB - Display`, so a freshly dropped node is a display-referred step. If you
  meant to isolate the look, set `out_colorspace` to match `in_colorspace`.
- When `out_colorspace` is a display space this node ends the chain, for the same reason `OCIO Display` does.
- `(none)` plus matching colorspaces is a complete no-op, which makes a mis-set node look like a wiring problem.

### What could not be verified here

Only one look exists in this config, so nothing is known here about a multi-look config, about the comma
separated look syntax OCIO supports, or about a look whose own process space differs from the input. The
inverse of a look that OCIO cannot invert would fail at processor build time; that case was not reachable with
this config.

---

## Part 8: `OCIO Exposure`

### What it is for

An exposure move in **stops**, as a pure linear multiply. Nuke's *Exposure*, or the multiply half of *Grade*.

It exists because the pack had no exposure control at all, which is an odd gap in a colour pack, and because
the obvious substitutes clamp. Every candidate outside this pack that was checked clamps to 0..1 either by
default or behind a toggle, and a clamp here destroys exactly what the rest of the pack exists to carry: the
values above 1.0 an HDR pass produces, and the negatives an unclamped VAE decode leaves behind.

### Inputs

| Widget | Type | Default | What it does |
| --- | --- | --- | --- |
| `exposure` | FLOAT, -20 to 20 | **0.0** | Stops. `out = in * 2**exposure`. At 0.0 the node is a pass-through. |
| `mix` | FLOAT, 0 to 1 | 1.0 | Blend with the original. 0.0 is a bit-exact bypass. |
| `image` | IMAGE, optional | - | One pixel input, mutually exclusive with `video` (Part 1). |
| `video` | VIDEO, optional | - | The other one. |

No config is read and PyOpenColorIO is not needed: a multiply is a multiply.

### Outputs

`image/sequence/video:IMAGE` and `ComfyUI Video:VIDEO`, the same pair every operator in this file returns.

### The colour behaviour that decides whether the result is right

**It is linear gain, so it belongs in a linear space.** Applied to display codes it is a gamma-space stretch,
not an exposure, and the result looks crushed or burnt in a way that is an artefact of where it was applied
rather than of the number. Put it before a display transform, never after one.

**Nothing is clamped at either end, and that is the whole point.** RGB is scaled and alpha is passed through
untouched. A multiply preserves sign, so a negative stays negative and scales with everything else.
`tests/test_exposure.py` holds the node to that with a control: 12.0 at +1 stop must come out 24.0 and -0.25
must come out -0.5, and the same test shows that a clamped result would fail both.

### Worked chain: matching an HDR pass to its plate

The case this was added for. An SDR-to-HDR pass does not return the plate's level, because it re-renders into
its own tonal distribution: shadows and mid-tones come down, highlights go up. Compare the two at the same
exposure and the pass simply looks darker, which is a comparison of exposures rather than of reconstructions.

Find the number as `log2(median luminance of the plate / median luminance of the pass)` and set it here.

Measured on six shots from the LTX-2.3 HDR path, at five frames spread through each clip:

| shot | plate median | pass median | lift |
| --- | --- | --- | --- |
| cyberpunk portrait, neon | 0.0647 | 0.0219 | **1.56 stops** |
| the same shot, camera panned onto the city | 0.0688 | 0.0230 | **1.58 stops** |
| dragon in a cave, fire from the mouth | 0.0665 | 0.0291 | **1.23 stops** |
| city street, traffic, sun down the street | 0.0896 | 0.0449 | **0.98 stops** |
| cave mouth, a figure, a ship beyond | 0.1254 | 0.0645 | **0.97 stops** |
| glass towers, sun in the gap | 0.1697 | 0.1157 | **0.55 stops** |

### Traps

**A number from one shot is wrong on the next.** The six above span 0.55 to 1.58, over a full stop.

**One number per shot is not always safe either.** Where the framing is held it is steady: the portrait varied
3.0% across its clip and the dragon 4.8%. Where the camera travels onto a different subject it is not: a pan
off the figure onto the city varied 45.3% and a dolly-and-tilt up a street 30.2%. On a moving shot, measure at
several frames and either animate the value or accept the drift knowingly.

**Leave it at 0.0 in a master.** Exposure belongs in the grade, where it can still be changed. Baking it into a
scene-linear master is a viewing decision written into a deliverable that other people will match to.

### What could not be verified here

The lifts above were measured on one model's HDR pass. Nothing here says another pass would land in the same
range, and the band comparison in the Clip Repair reference shows two passes disagreeing about where they gain
at all. Measure yours.

## Part 9: `OCIO Player`

### What you are actually looking at

Read this before you judge a shot on it.

**The picture on your screen is 8-bit SDR, and the data behind it is not.** The frame is loaded as half float and
keeps everything the render produced, including values above white and below black. The exposure control multiplies
those real values on the GPU before anything is drawn. Only the last step, the composite to the screen, is 8-bit.

Three practical consequences:

- **You cannot see the whole range at once.** No browser canvas shows you 20 stops in one look, and this one does
  not pretend to. What you see at any exposure setting is one 8-bit window onto the real values.
- **You CAN find out whether a highlight is there.** Pull exposure down. If detail appears in a blown area, the
  data was always there and the exposure window was simply somewhere else. If nothing appears, the highlight really is flat, and that
  is a genuine answer about your render rather than a limitation of the viewer.
- **The exposure window reaches linear 222.86, which is 7.8 stops over diffuse white, and stops there.** That
  ceiling is set by the viewer's own lookup axis, not by your data. Past it the viewport stops separating values.

**If your monitor is a calibrated HDR panel, this viewer will still look SDR on it today.** That is this viewer's
implementation, not a browser limit, and the distinction is measured rather than assumed: Chrome does ship a
16-bit float drawing buffer for WebGL2, this viewer does not ask for it, and Part 9's accuracy section carries the
exact call, the two preconditions it needs, and the read-back that proves values above one survive in it. Treat
the viewport as a reliable instrument for judging RANGE and colour decisions, and your grading monitor as the
authority on how the picture looks.

### What it is for

An on-node float viewport for looking at what your graph produced: full resolution, half float, with a
view-only exposure control and a transport bar. Nuke equivalent: the **Viewer**. It has no outputs at all, so
adding one can never change what the rest of the graph does.

Confirmed from `/object_info`: `"output": []`, `"output_name": []`, `"output_node": true`. It runs on queue so
that its viewport refreshes, and it passes nothing on.

### Inputs

| Name | Type | Default | Allowed values |
| --- | --- | --- | --- |
| `input_colorspace` | combo | `sRGB - Display` | the 55 names, verbatim |
| `output_colorspace` | combo | `sRGB - Display` | the 55 names, verbatim |
| `raw_data` | `BOOLEAN` | `false` | off reads `color-managed`, on reads `raw (no convert)` |
| `start_frame` | `INT` | `0` | 0 to 100000000 |
| `end_frame` | `INT` | `0` | 0 to 100000000, where 0 means through the end |
| `fps` | `FLOAT` | `24.0` | 0.0 to 1000.0, step 0.001 |
| `images` | `IMAGE` (optional) | | any batch: still (N=1), sequence or video frames (N>1) |
| `video` | `VIDEO` (optional) | | a `Load Video` output, streamed rather than materialised |
| `alpha` | `MASK` (optional) | | alpha to view alongside the RGB |
| `base` | `STRING` (optional) | `"0"` | source first-frame number, filled in by the front end |
| `audio` | `AUDIO` (optional) | | a soundtrack for the frames, played with them and metered |

**What you wire into each socket.**

- `images`: `Load Image` (`IMAGE`), `OCIO Read` (`image/sequence/video`), `VAE Decode`, `OCIO VAE Decode`, or
  any OCIO operator's `IMAGE` output.
- `video`: `Load Video` (`VIDEO`), `OCIO Read` (`ComfyUI Video`), `Create Video` (`VIDEO`), or an OCIO
  operator's `VIDEO` output. Mutually exclusive with `images`, exactly as on the six operators. A `VIDEO` that
  resolves to a real file on disk streams in the browser; a `VIDEO` built in memory by an upstream OCIO node is
  unwrapped to frames and shown through the float path instead.
- `alpha`: `OCIO Read` (`alpha`), `Load Image` (`MASK`), `ImageToMask`, `LoadImageMask`.
- `audio`: `Load Audio`, an audio VAE decode from a video model, or anything else emitting `AUDIO`. A batch of
  frames carries no sound, so without this the L/R meters have nothing to read no matter what the graph
  generated upstream. The track is cut to the frames the viewer cached, which matters because the cache stops
  at 240 frames: a full-length track against a truncated picture drifts by exactly the frames that were
  dropped. Ignored when a movie is streamed through `video`, since that clip brings its own track.

  Two limits worth knowing. Reverse plays silent, because a decoded buffer has no negative rate and the
  meters fall to zero with it, which is honest rather than broken. And a bad `AUDIO` does not stop the
  picture: everywhere else in this pack a malformed track raises, because there the result is a delivered
  file that is silently silent, while here the frames still play and the node's report says the track was
  dropped.
- `base` is filled in by the front end from an upstream `OCIO Read` so the timeline shows real source frame
  numbers. It is a `STRING` on purpose, so a blank value cannot fail prompt validation. Leave it alone.

### Outputs

None. Nothing to connect. If you want the pixels you are looking at, branch the same upstream output into
`OCIO Write` or `Save Image` as well.

### What it shows

Read from the backend and the front-end shader, not from the node's name.

The backend writes the incoming batch to a temporary folder as **full-resolution half-float RGBA `.npy` frames**,
one per frame, capped at **240 frames** per node, clearing that node's previous cache first. It is not a proxy:
resolution is untouched and the values stay float. A per-node status line reports frame count, resolution and the
colorspace pair, and reports the cap when it applies.

The browser fetches each frame as raw float16 bytes, uploads it into an `RGBA16F` texture, and the fragment
shader does three things in this order:

1. multiply RGB by `2 ** exposure`, in the input colorspace
2. optionally reshape the coordinate into ACEScct code space, then clamp it into 0..1
3. look the result up in a 33-cube RGBA8 LUT baked by the server for `input_colorspace` into `output_colorspace`

Confirmed live against the server's LUT route:

| request | `X-Shaper` | `X-Lut-Size` | bytes |
| --- | --- | --- | --- |
| `in_cs=ACEScg`, scene-linear path | 1 | 33 | 143748 |
| `in_cs=sRGB - Display`, scene-linear path | 0 | 33 | 143748 |
| `in_cs=ACEScg`, video-streaming path | 0 | 33 | 143748 |
| `raw=1` (that is, `raw_data` on) | 0 | 33 | 143748 |

143748 is 33 cubed times 4 bytes, so the LUT is what it claims to be. The shaper turns on only when the input
colorspace is tagged scene-linear in the config, `raw_data` is off, and the float viewport is asking, which is
the case that needs it.

The metadata panel under the viewport shows six rows: Resolution, Frames, Range, FPS, Input CS, Output CS.

### What the controls do

The transport bar, read from the front-end source, left to right:

| Control | Effect |
| --- | --- |
| reset | set the range back to the whole clip |
| set IN | move the in point to the current frame |
| go to first | jump to the in point |
| step back | one frame back |
| play reverse | play backwards; not a toggle |
| stop | pause where you are; this is the only pause |
| frame field | type a source frame number to jump there |
| play forward | play forwards; not a toggle |
| step forward | one frame forward |
| go to last | jump to the out point |
| set OUT | move the out point to the current frame |

Above the timeline, on this node only, is the **Exposure** strip: a slider, an editable field and a reset
button. Range is -16 to +16 stops. Type a signed value such as `+2.5` and press Enter, or double-click the field
to reset to 0.

Two facts about exposure that decide whether you can trust what you see.

- **It is view only.** It is applied in the shader, never sent to the backend, and never baked into anything. The
  node has no outputs, so there is nothing for it to leak into.
- **It is a true stop only on scene-linear input.** The shader multiplies before the display LUT, so on ACEScg or
  a `Linear ` colorspace, `+1` is one stop of light. On a display-encoded input such as `sRGB - Display`,
  multiplying an already-encoded signal is an approximate viewer gain, not a stop.

Dragging the in and out handles on the timeline writes the `start_frame` and `end_frame` widgets, and those two
widgets are read back to position the handles. They are the same setting seen two ways.

`start_frame` and `end_frame` **never reach the backend transform**. Confirmed by scanning the node's own
function body: each name appears exactly once, in the signature, and nowhere in the code. The whole batch is
cached and the two numbers are consumed by the front end as the playback range and the prefetch range. That
matches a viewer with no outputs: they change what you watch, not what exists.

`raw_data` on shows the pixels with no colorspace conversion, which is how to check whether an odd look comes
from the data or from the display transform.

A small refresh square sits at the top left of the viewport. It turns amber when something upstream is rewired,
because the cached frames are then stale, and clicking it re-queues this node.

### What it can and cannot tell you about values above 1

Measured, not assumed. Three separate ceilings sit in this path.

**1. The cache is half float.** Frames are stored as float16. Largest representable value 65504.0; anything
above becomes infinity. A linear 469.8 is stored as 469.75. For scene-linear work this is not a practical limit,
and it is the same precision an EXR half file uses.

**2. With the shaper on, the viewport separates linear values up to 222.86 and no further.** The shader clamps
the LUT coordinate into 0..1 after reshaping it into ACEScct code space. ACEScct code 1.0 decodes to linear
**222.8609**, measured, so that is the ceiling:

| scene-linear | ACEScct code | distinguishable in the viewport |
| --- | --- | --- |
| 0.18 | 0.413588 | yes |
| 1.0 | 0.554795 | yes |
| 8.0 | 0.726027 | yes |
| 55.0 | 0.884781 | yes |
| 100.0 | 0.934010 | yes |
| 222.0 | 0.999681 | yes, just |
| 222.8609 | 1.000000 | no, this and everything above it look the same |
| 1000.0 | 1.123618 | no |
| 10000.0 | 1.313226 | no |

So a specular hit of 300 and one of 3000 are the same pixel on screen at exposure 0. Pull exposure down and both
move back under the ceiling, which is exactly how you check them.

**3. With the shaper off, anything above 1.0 looks identical.** On a display-encoded input, or on a streamed
video, the LUT coordinate is the value itself and the clamp bites at 1.0. Linear 1.0, 2.0 and 100.0 all sample
LUT coordinate 1.0.

**Negatives are shown as black in both modes.** The same clamp floors the coordinate at 0. The values are still
in the cache and in the graph; the viewport cannot draw them.

**The canvas itself is 8-bit**, measured in a live Chromium WebGL 2.0 context. Asked directly, the default drawing buffer reports `RED_BITS`, `GREEN_BITS`, `BLUE_BITS` and
`ALPHA_BITS` all equal to 8. **That is this viewer's limit rather than the browser's.** A WebGL2 drawing buffer can hold 16-bit float, and
`drawingBufferStorage` is the call that asks for it (Chrome 122 onward). It has two preconditions and returns
`INVALID_ENUM` or `INVALID_OPERATION` if either is missing, which is why the default 8-bit buffer is easy to
mistake for a hard ceiling. With both preconditions in place, measured, it works:

```js
const gl = canvas.getContext("webgl2", { alpha: true });   // precondition 1
gl.getExtension("EXT_color_buffer_float");                 // precondition 2, BEFORE the call
gl.drawingBufferStorage(gl.RGBA16F, canvas.width, canvas.height);
// gl.drawingBufferFormat === gl.RGBA16F, and RED_BITS goes 8 -> 16
```

Clearing that buffer to `(4.0, 0.5, -0.25)` and reading it back returns exactly `4.0, 0.5, -0.25`, so values above
one and below zero survive in the DEFAULT framebuffer, not only in an offscreen float target. Both preconditions
are necessary and each was isolated: neither gives `INVALID_ENUM`, `alpha: true` alone gives `INVALID_ENUM`, the
extension alone gives `INVALID_OPERATION`. Chrome shipped `drawingBufferStorage` in 122 for exactly this purpose.

So this viewer composites at 8 bits because it does not make that call, which is a change of two lines at context
creation plus a hook on resize, not a port to WebGPU. WebGPU is also available and accepts `rgba16float` with
`toneMapping: { mode: "extended" }`, and it is the heavier route.

TWO THINGS THAT MEASUREMENT STILL DOES NOT SHOW. A 16-bit buffer is not a lit HDR panel: presentation depends on
the display, the OS and GPU compositing, and `(dynamic-range: high)` reports false on the machine this was run on,
so nothing here was seen driving a calibrated HDR monitor. And a float drawing buffer is NOT a general
improvement: on a display that is not HDR it buys nothing on screen. The final conversion still has to land in
the SDR range, and how it gets there is left to the browser compositor, the operating system and the output
surface, which may simply clip. An explicit SDR transform in the shader is the better answer there, because it
controls where the highlights go instead of hoping. So the float buffer belongs behind a
`(dynamic-range: high)` check, as an addition to the SDR path rather than a replacement for it.

**A browser is not limited to SDR in general, and it is worth knowing which routes exist.** Measured in the same session, same browser: a 2D canvas requested with `colorType: "float16"` round-trips
4.0, 0.5 and -0.25 back byte for byte, so values above one and below zero really are stored. And WebGPU accepts a
canvas configured as `rgba16float` together with `toneMapping: { mode: "extended" }`, which is the extended-range
presentation path. Both are routes this viewer does not take today.

WHAT THAT MEASUREMENT DOES NOT SHOW, and the distinction matters: the API ACCEPTED the configuration. It was not
seen driving an HDR display, because `(dynamic-range: high)` reports false on the machine it was run on. Accepting
a format and lighting up a calibrated HDR panel are different claims, and only the second one answers the
complaint that the viewer looks SDR on good hardware.

The 8-bit composite also matters less than it sounds, which is why the exposure control is not a consolation prize. Float is available on this context: `EXT_color_buffer_float` and `OES_texture_float_linear` are both
present, an `RGBA16F` texture upload is accepted, and a float framebuffer reports complete. So the frame keeps its
half-float precision from disk into the texture and through the shader, and only the final composite to the screen
is 8-bit. That is exactly why pulling exposure down reveals detail above white instead of revealing nothing: the
values are really there, and each exposure setting is a different 8-bit window onto them.

What the float path buys you is therefore precise: an accurate exposure sweep and an accurate colorspace check
on the real values. It does not buy you a brighter display, and it does not let you see the whole range at once.

**There is no pixel-value readout.** A search of the front-end source found no per-pixel probe, sampler or
numeric readout. To read an actual number, write the frame with `OCIO Write` and inspect the file, or watch the
range report on `OCIO VAE Decode`.

### Worked chains

**Check a graded ACEScg render at several exposures.**

```
OCIO Read            output_colorspace = ACEScg
  into OCIO CDLTransform
  into OCIO Player
       input_colorspace  = ACEScg
       output_colorspace = sRGB - Display
       raw_data          = false
       fps               = 24.0
```

Queue once, then sweep the exposure strip. Nothing re-renders, because exposure is a shader uniform and the
redraw needs no fetch. Put a second Player on the same output at a different exposure to compare two settings
side by side.

**Prove whether a strange look is the data or the display transform.**

```
OCIO LogConvert      (Log to Linear, whatever curve you think it is)
  into OCIO Player       raw_data = true
```

With `raw_data` on there is no conversion at all. If it still looks wrong, the numbers are wrong, and the curve
choice is the first suspect.

**Watch a movie file without materialising it.**

```
Load Video           (VIDEO)
  into OCIO Player       (the video socket; the images socket auto-disconnects)
```

The clip streams in the browser and is drawn through the same exposure and LUT shader. On this path the shaper
is off, so values above 1.0 are not separated.

**View alpha alongside the RGB.**

```
OCIO Read
     image/sequence/video  into  OCIO Player.images
     alpha                 into  OCIO Player.alpha
```

### Traps

- **The Player is input only.** There is no output to wire onward, by design, so that a viewer can never sit in
  the middle of the flow.
- The `images` and `video` sockets exclude each other, as on the six operators, and `video` wins if an API call
  supplies both.
- Only the first 240 frames are cached for viewing. The panel says so when the cap applies.
- The viewport is empty until the graph has run. That is what the **Refresh** button in the empty state is for.
- `input_colorspace` is a claim about the data, not a conversion of it. Set it wrong and the picture is wrong
  while the numbers are fine. The front end guesses ACEScg once, on the first frame, if it finds any RGB value
  above 1.001 and you have not touched the widget yourself. That is a heuristic, and it is honest about being
  one.
- Exposure is not saved into anything. Two Players at different exposures showing the same data are both showing
  the same data.
- Without WebGL2 the viewport does not appear and the node says `WebGL2 unavailable - cannot show float
  viewport`. The metadata panel still fills in.

### What could not be verified here

The viewport was not exercised in a browser during this pass. Everything in the sections above about the shader,
the clamps, the exposure range and the control set was read from the front-end source, and the LUT behaviour and
the 222.86 ceiling were measured against the live server route and the pack's own ACEScct functions. The
statement that a given button produces a given picture on screen is therefore inferred from the source, not
observed. The 8-bit canvas claim WAS since settled in a live browser: the default drawing buffer really does report 8 bits
per channel, while float textures and float render targets are both available on the same context. What remains
unobserved is whether an HDR-capable display path in a future browser changes the composite.

The `alpha` input was not measured. `raw_data` was confirmed to switch the LUT to an identity ramp through the
live route, but the on-screen result was not observed.

---

## Part 10: traps that cross more than one node

Ranked by how much damage they do quietly.

### 1. A display transform is the end of a chain

`OCIO Display` with a tone-mapped view, and `OCIO LookTransform` or `OCIO ColorSpace` targeting a `- Display`
colorspace, all produce display-referred values. Measured on `sRGB - Display` with
`ACES 2.0 - SDR 100 nits (Rec.709)`: every negative becomes 0.0, and every input above ACEScg 127.8616 becomes
the same 1.000007. Grading after that works on data that no longer exists. Grade first, then display, then write
or view.

### 2. `OCIO LogConvert` against `OCIO ColorSpace` for a camera log curve

The mistake that produces the most convincing wrong answer.

- `OCIO LogConvert` applies the transfer curve and nothing else.
- The config's same-named colorspace, used through `OCIO ColorSpace`, applies the curve **and** the gamut matrix.

Both routes below were measured to agree to 4.29e-06 on a chromatic patch:

```
OCIO LogConvert (Log to Linear, Sony S-Log3)  then  OCIO ColorSpace (Linear S-Gamut3 into ACEScg)
OCIO ColorSpace (S-Log3 S-Gamut3 into ACEScg)
```

Stopping after the curve and calling the result ACEScg is wrong by **0.3791 absolute** on the same patch. Pick
by what the data actually is: a real camera plate in its native gamut wants the one-node colorspace route; data
that only carries the curve, which is the normal case for a generated image, wants `OCIO LogConvert` plus a
separate gamut step.

### 3. Canon Log 3 has two conventions in this pack, differing by exactly 1/0.9

`OCIO LogConvert` with `curve = Canon Log 3` and `OCIO ColorSpace` with `CanonLog3 CinemaGamut D55` do not agree.
Measured over codes 0.2 to 1.0, the LogConvert decode is 1.11111111 times the config's, at every sample;
multiplying by 0.9 matches the config to 6.68e-06, which is float32 quantisation at these magnitudes. Seen from
the other direction the same fact reads as an input scale: encoding, this node's output equals the config's on
`x * 0.9` to 1.19e-07. Canon's own tables index by "Scene Linear %" with reflection
equal to scene linear times 0.9, and this node uses direct reflectance. Mixing the two puts a 0.152 stop exposure
error into a shot with no error message.

For reference, here is how every comparable curve in `OCIO LogConvert` compares against the config's own
curve-only colorspace pair. The config processor was built fresh from the built-in config rather than through
the node, so a wiring mistake in the node could not hide:

| `OCIO LogConvert` curve | config pair used | max absolute deviation | max relative deviation |
| --- | --- | --- | --- |
| ARRI LogC3 | `ARRI LogC3 (EI800)` into `Linear ARRI Wide Gamut 3` | 5.68e-04 | 5.07e-05 |
| ARRI LogC4 | `ARRI LogC4` into `Linear ARRI Wide Gamut 4` | 1.22e-03 | 3.64e-05 |
| Sony S-Log3 | `S-Log3 S-Gamut3` into `Linear S-Gamut3` | 8.01e-05 | 3.32e-06 |
| Panasonic V-Log | `V-Log V-Gamut` into `Linear V-Gamut` | 5.34e-05 | 1.92e-05 |
| **Canon Log 3** | `CanonLog3 CinemaGamut D55` into `Linear CinemaGamut D55` | **1.63** | **0.1111** |
| RED Log3G10 | `Log3G10 REDWideGamutRGB` into `Linear REDWideGamutRGB` | 2.44e-04 | 2.89e-06 |
| DaVinci Intermediate | `DaVinci Intermediate WideGamut` into `Linear DaVinci WideGamut` | 2.44e-04 | 4.81e-06 |
| ACEScct | `ACEScct` into `ACES2065-1` | 1.22e-04 | 2.33e-06 |
| ACEScc | `ACEScc` into `ACES2065-1` | 5.80e-04 | 3.59e-06 |

Eight of nine agree to between 2.3e-06 and 5.1e-05 relative. One does not, for a documented reason.

### 4. A 0..1 LUT destroys HDR

`OCIO FileTransform` on a `.cube` with a 0..1 domain clamps both ends. Inputs of 1.5, 4.0 and 100.0 all came out
as the same triplet, and -0.5 came out as 0.0. Every other node in this set passes values below 0 and above 1
through untouched. Put the LUT after the display transform.

### 5. The `view` list on `OCIO Display` includes views your display does not have

The combo is the union across all nine displays. Nine of the fourteen views are invalid on `sRGB - Display`, and
the job fails at run time with `DisplayViewTransform error. The display '...' does not have view '...'`. The
valid pairs are tabulated in Part 4.

### 6. Two pixel sockets, and one of them wins silently

Every operator, plus `OCIO Player` and `OCIO Write`, carries an `IMAGE` input labelled `OCIO Img/Seq/Vid` and a
`VIDEO` input labelled `ComfyUI Video`. The front end makes them exclusive by auto-disconnecting whichever you
did not just plug in, and the output side mirrors the active input, so the unused output is `None`. Through the
API both can be supplied, and the **`VIDEO` socket wins**. If a downstream node sees nothing, check which of the
two outputs you connected.

### 7. `mix` is a numeric lerp with no colour awareness

Confirmed exact on both `OCIO ColorSpace` and `OCIO Display`. On a grade it does what you expect. Across a
display transform it interpolates between a scene-linear number and a display code, which is not a
half-strength display transform.

### 8. Silence is the normal failure mode

Nothing in this set warns about a wrong colorspace choice. A no-op is bit-identical, a wrong curve looks flat,
and a wrong gamut looks like a grade note. The cheap checks are: `raw_data` on `OCIO Player` to see the numbers
untouched, matching in and out colorspaces to prove a node is inert, and `view = Raw` on `OCIO Display` to prove
it is wired but doing nothing.

---

## Part 11: what could not be verified, all in one place

- **Nothing was run through the ComfyUI queue.** The node classes were imported from the deployed pack folder
  and called directly in the ComfyUI interpreter, which exercises the same `run` and `convert` functions the
  server calls with the same OCIO version and the same config. The queue was deliberately left alone because
  another job could have been running. What this does not cover: prompt validation, combo membership checks, and
  the front-end wiring. All combo values, defaults and socket types in this file were read from the live
  `/object_info`, which is the same source prompt validation uses.
- **Everything was measured against the deployed copy of the pack**, the one the running server imported, and its
  MD5 sums were recorded at the start and re-checked at the end: unchanged throughout. The source tree moved
  during the pass, so at the end the two differed for `nodes.py` and `io_nodes.py`. Both diffs were read and both
  are comment and tooltip text only: no transform logic, and the `OCIO Player` region of `io_nodes.py` hashed
  byte-identical between the two. So every number here describes the code as deployed, and nothing pending in the
  source tree changes it. One consequence is noted at the `file_path` tooltip in Part 6.
- **The browser was not opened.** Every statement about the `OCIO Player` viewport, the transport buttons and the
  exposure strip comes from the front-end source plus the live LUT route, not from watching the screen.
- **Only a subset of colorspace pairs was measured.** 55 names give 3025 ordered pairs; the pairs quoted here
  are the ones exercised.
- **Only a 2x2x2 `.cube` was available**, so the four interpolation modes are indistinguishable in these
  measurements, and no other LUT format was exercised.
- **HDR display and view pairs were not measured.** The 127.86 ceiling is for the SDR 100-nit Rec.709 view.
- **ARRI's LogC4 and LogC3 mid-grey figures** were confirmed against ARRI's published statements found through a
  domain-restricted search of ARRI's own site. The specification PDF could not be machine-extracted here, so
  those two numbers are cited from ARRI's published material rather than quoted from the document body. The
  pack's own arithmetic for both curves was independently rederived and is reported above.
- **`OCIO CDLTransform` with `direction = inverse`** was measured with one parameter set. A slope at or below 0
  was not tested.
- **The `alpha` input on `OCIO Player`** was not measured.

## Part 12: reproducing any number in this file

Two commands cover everything.

Combo values, defaults, socket types and output names:

```
curl -s http://127.0.0.1:8188/object_info/OCIOColorSpace
curl -s http://127.0.0.1:8188/object_info/OCIOLogConvert
curl -s http://127.0.0.1:8188/object_info/OCIODisplay
curl -s http://127.0.0.1:8188/object_info/OCIOCDLTransform
curl -s http://127.0.0.1:8188/object_info/OCIOFileTransform
curl -s http://127.0.0.1:8188/object_info/OCIOLookTransform
curl -s http://127.0.0.1:8188/object_info/OCIOPlayer
```

Colour behaviour, using the interpreter that runs your ComfyUI so the OCIO version and the config match. Load
the deployed `nodes.py` and call the node the way the server does:

```python
import importlib.util, os, sys
import numpy as np, torch

CU   = os.environ["COMFYUI_ROOT"]                      # e.g. E:\path\to\ComfyUI
PACK = os.path.join(CU, "custom_nodes", "comfyui-ocio")
sys.path.insert(0, CU)                                 # so folder_paths imports

spec = importlib.util.spec_from_file_location("probe", os.path.join(PACK, "nodes.py"))
N = importlib.util.module_from_spec(spec); spec.loader.exec_module(N)

def img(vals):                                         # [1,1,len,3]
    a = np.array(vals, np.float32).reshape(1, 1, -1, 1).repeat(3, axis=3)
    return torch.from_numpy(np.ascontiguousarray(a))

t = img([-1.0, 0.0, 0.18, 1.0, 2.0, 100.0, 5000.0])
out = N.OCIOColorSpace().convert(image=t, in_colorspace="ACEScg",
                                out_colorspace="sRGB - Display", mix=1.0)[0]
print(out[0, 0, :, 0].tolist())
```

Swap in `N.OCIOLogConvert().run(image=t, operation="Log to Linear", curve="Sony S-Log3", mix=1.0)` or any other
node. For a parity check against OCIO, build the processor yourself instead of going through the node, so a
wiring mistake cannot hide:

```python
import PyOpenColorIO as OCIO
cfg  = OCIO.Config.CreateFromBuiltinConfig("studio-config-latest")
proc = cfg.getProcessor("S-Log3 S-Gamut3", "Linear S-Gamut3").getDefaultCPUProcessor()
arr  = np.full((1, 1, 3), 0.41, np.float32)
proc.apply(OCIO.PackedImageDesc(arr, 1, 1, 3))
print(arr[0, 0])
```

The Player's display LUT, live:

```
curl -s -D - -o /dev/null "http://127.0.0.1:8188/ocio/lut?in_cs=ACEScg&out_cs=sRGB%20-%20Display&raw=0&size=33&float=1"
```

Read `X-Shaper` and `X-Lut-Size` off the response headers.

### If you want to take this viewport to real HDR

Everything needed to do it, and the reasons each step is not optional. Sourced from the WebGL specification and its
extension registry, the HTML predefined-colour-space list, Media Queries Level 5, CSS Color HDR, and Chromium's own
`DrawingBuffer` implementation. What is measured here is marked as measured.

**It is one feature, not two, and the parts are useless apart.** A float drawing buffer with the display LUT on
gains nothing, because the LUT texture is `RGBA8` and its contents are clipped to 0..1 before upload. A float LUT
with an 8-bit drawing buffer gains nothing, because the buffer clips. Both together on a display that is not HDR
gain nothing on screen, because the composite still has to land in the SDR range. So all of it lands together,
behind one capability gate, or none of it is worth doing.

**What the 0..1 clip on the LUT currently costs.** Measured on the real 33-cube for `ACEScg` to `sRGB - Display`
with the log shaper on: the grid's own output runs from **-8.62 to +12.48**, and the clip touches **38.6 % of
entries above 1 and 47.3 % below 0**. Those two halves are not the same thing. Above 1 is highlight information,
lost because the sampling axis reaches linear 222.86. Below 0 is **out of gamut**, which a saturated ACEScg colour
genuinely is in sRGB primaries, and clipping it is one of the defensible answers rather than a bug. Preserve the
high side deliberately; treat the low side as its own gamut decision.

**The drawing buffer, in the order the calls must happen.**

1. Create the context with `alpha: true` (the default) and `premultipliedAlpha: false`. The second is not tidiness:
   with premultiplied alpha the specification requires colour values handed to the compositor to be no greater than
   alpha, and it explicitly allows the result of a violation to be anything at all, red arriving as green among
   them. An HDR value above 1 at alpha 1 violates exactly that.
2. `gl.getExtension("EXT_color_buffer_float")`. This is what makes `RGBA16F` colour-renderable and what permits
   `RGBA` with `FLOAT` in `readPixels`. Without it the next call fails with `INVALID_ENUM`.
3. Set `gl.drawingBufferColorSpace` **before** the storage call, because assigning it reallocates the buffer and
   destroys its contents. Then read the property back: it existing does not mean a given value was accepted.
4. `gl.drawingBufferStorage(gl.RGBA16F, width, height)`, then check `gl.getError()`, `gl.drawingBufferFormat`, and
   `gl.drawingBufferWidth` / `Height`.
5. `gl.bindFramebuffer(gl.FRAMEBUFFER, null)` and `gl.viewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight)`.
   A resize does not restore the viewport for you.

**Which colour space, and it depends on the mode.** The value describes the numbers the shader emits, not an
intention. With the display LUT ON, the shader emits display-referred sRGB, because the OCIO display transform
already applied the encoding: the correct value is `srgb`. Declaring that `srgb-linear` makes the compositor decode
it linearly and the picture comes out **wrong**, not merely flat. With the LUT off and a scene-linear source the
shader emits linear light, and `srgb-linear` is correct. The list in the specification is `srgb`, `srgb-linear`,
`display-p3` and `display-p3-linear`; `rec2100-hlg` and `rec2100-pq` are not in it, and they name non-linear HDR
signal encodings rather than arbitrary scene-linear values, so feeding linear 4.0 into a buffer declared PQ is a
category error.

**Being in the specification is not being accepted, and this is the trap that matters here.** Measured in Chromium
by assigning each value and reading it back: `srgb` and `display-p3` are accepted, while **`srgb-linear` and
`display-p3-linear` throw a `TypeError`**, as do both Rec.2100 values. The float buffer allocates and reports 16
bits regardless, so the two are independent: you can get the storage and still not be able to declare it as
linear light. Without that declaration the compositor reads the numbers through the sRGB transfer function, which
is not what extended linear light means. So on a build like that, a float drawing buffer is storage without a
correct interpretation, and the honest options are to emit display-encoded values and declare `srgb`, or to wait
for the linear space, or to use WebGPU where the contract is explicit. Always assign and read back.

**Resize.** Chromium keeps a non-default drawing-buffer format across a plain `canvas.width` write, confirmed both
in its `DrawingBuffer::Resize` source and by measurement here. The specification does not promise it. So compute
the size, set `canvas.width` and `canvas.height`, then call `drawingBufferStorage` again and re-read the actual
dimensions: the requested size is not guaranteed either, and rendering must use `drawingBufferWidth` / `Height`
rather than what was asked for.

**Context loss drops the extensions.** A restored context is rebuilt from the original creation parameters and
does not carry `EXT_color_buffer_float` with it. Without re-requesting the extension, re-setting the colour space
and re-calling the storage, a restored canvas silently returns to `RGBA8`. That silence is the failure mode.

**When to enable it, and when not to.** Every one of these has to hold: `(dynamic-range: high)` matches, WebGL2
exists, alpha really is on, `drawingBufferStorage` exists, the extension was obtained, the colour space read back
as intended, the storage call raised no error, the format really is `RGBA16F`, and the dimensions are usable. The
media query reports a **capability rather than an active state**, and a window can be dragged to another monitor,
so subscribe to its `change` event and switch the output transform when it fires. There is no web API for the
display's headroom in stops, deliberately: CSS Color HDR explains that a real-time value would be a tracking
vector. An application may offer a target-headroom control, but it cannot measure the display.

**On a display that is not HDR, keep the SDR transform.** The float buffer stays float up to the compositor, and
the monitor gains no headroom from that. The final conversion has to reach the SDR range, by component clipping,
gamut projection or an implementation-dependent tone map, and none of those is specified. An explicit SDR transform
in the shader is the better answer, because it keeps highlight detail, controls hue and saturation, and can
gamut-compress and dither. A float buffer on an SDR display buys nothing on screen and costs the memory.

**The costs and the reach.** `RGBA16F` is 8 bytes per pixel against 4. One 3840x2160 buffer is about 63 MiB, front
and back about 127 MiB, and a full 4K write at 60 Hz is roughly 4 GB/s of traffic, which matters more than shader
cost on integrated and mobile GPUs. `preserveDrawingBuffer: true` forces the browser to keep an extra copy and is
expensive, especially with antialiasing; `antialias` is a request rather than a guarantee, and multisample float
render buffers are not supported on every backend. `readPixels` from a float buffer takes `RGBA` with `FLOAT` into
a `Float32Array`, in the same callback as the draw. And `drawingBufferStorage` is a Chromium-family API: Firefox
and Safari do not have it.

**WebGPU is the cleaner target for guaranteed extended range**, through `configure({ format: "rgba16float",
toneMapping: { mode: "extended" } })`, where `standard` is normatively limited to 0..1 and `extended` may use the
display's headroom. Detection is `navigator.gpu`, an adapter, a device, a successful `configure`, then
`getConfiguration()?.toneMapping?.mode === "extended"`. The cost is WGSL instead of GLSL, new pipelines and bind
groups, and no standard way to share a `WebGLTexture` with a `GPUTexture`. PlayCanvas, a production engine, ships
HDR output only on WebGPU and falls back to LDR otherwise, which is a fair signal about where this ends up.

**The failure to expect.** Treating a successful allocation and a successful `readPixels` as proof of HDR output.
They prove storage and rendering. They say nothing about what the compositor or the monitor did, and on an ordinary
or inactive-HDR display the result is lost highlights instead of a controlled SDR tone map.

### The two Player claims, which need a browser

Everything above runs in Python. The Player's two load-bearing numbers do not, so here they are as checks you can
run yourself rather than statements you have to take.

**Is the composite really 8-bit, and could it be 16?** Open the ComfyUI page, then the browser console. Both preconditions are
necessary, and the four arms below show what each one contributes, so a single negative result cannot be
mistaken for a browser limit.

```js
const mk = o => { const c = document.createElement("canvas"); c.width = c.height = 64;
                  return c.getContext("webgl2", o); };
const ask = gl => { while (gl.getError() !== gl.NO_ERROR) {}
                    gl.drawingBufferStorage(gl.RGBA16F, 64, 64);
                    return { err: gl.getError(), redBits: gl.getParameter(gl.RED_BITS),
                             isRGBA16F: gl.drawingBufferFormat === gl.RGBA16F }; };

console.log("neither precondition:", ask(mk(undefined)));                     // INVALID_ENUM, 8 bits
console.log("alpha only         :", ask(mk({ alpha: true })));                // INVALID_ENUM, 8 bits
const g3 = mk({ alpha: false }); g3.getExtension("EXT_color_buffer_float");
console.log("extension only     :", ask(g3));                                 // INVALID_OPERATION
const g4 = mk({ alpha: true });  g4.getExtension("EXT_color_buffer_float");
console.log("both               :", ask(g4));                                 // accepted, 16 bits, RGBA16F

g4.clearColor(4.0, 0.5, -0.25, 1.0); g4.clear(g4.COLOR_BUFFER_BIT);
const px = new Float32Array(4); g4.readPixels(0, 0, 1, 1, g4.RGBA, g4.FLOAT, px);
console.log("read back:", Array.from(px));                                    // 4, 0.5, -0.25, 1

console.log("does this display even claim HDR:",
            matchMedia("(dynamic-range: high)").matches);                     // false on an SDR panel
```

The last line matters as much as the rest. A 16-bit buffer is not a lit HDR panel, and if that query is false then
nothing you do to the buffer will make the screen brighter. Report the value with any HDR claim.

**Where does the exposure window stop separating values?** The ceiling comes from the viewer's lookup axis being
ACEScct-coded, so it is the pack's own function that answers it, not the browser:

```python
import numpy as np
# io_nodes.py, loaded the same way as nodes.py above
print(float(IO._acescct_to_lin(np.array([1.0]))[0]))   # 222.860977
print(np.log2(222.860977))                             # 7.80 stops over diffuse white
```
