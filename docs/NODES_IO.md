# OCIO Read / OCIO Write: complete node reference

This document covers the pack's two IO nodes in full: every input, every output, when a widget is
visible, how to wire them for real jobs, and the traps that will cost you a render if you don't know
about them. It does not cover the six grading nodes (`OCIO ColorSpace`, `OCIO CDLTransform`,
`OCIO Display`, `OCIO FileTransform`, `OCIO LookTransform`, `OCIO LogConvert`) or `OCIO Player` beyond
using them as wiring targets in the worked examples. Those are documented elsewhere; this file is
about getting pixels and metadata onto and off of disk correctly.

Every input name, type, default and combo value below was read from a live ComfyUI server's
`GET /object_info/OCIORead` and `GET /object_info/OCIOWrite`, then cross-checked against the node's own
Python (`io_nodes.py`) and its front-end script (`web/ocio_io.js`). A number of the behaviors described
here, especially in the traps sections, were reproduced by actually posting graphs to `/prompt` and
reading the written files back with OpenEXR, Pillow and `ffprobe`, not just read off a tooltip. Where
something is stated as a limitation or a gap rather than a feature, that's a deliberate choice made
while writing this: repeating a tooltip's claim without checking it is how documentation drifts from
the code.

Both nodes live in the `OCIO` category. `OCIO Read` has `output_node: false` (it only runs when
something downstream consumes what it produces). `OCIO Write` has `output_node: true`, and so does its
sibling `OCIO Player`, which is why the `partial_execution_targets` trap described below applies to
both of them and not to `OCIO Read`.

A note on sockets versus widgets, because it matters for every table below: on every OCIO node the
front end relabels an `IMAGE` socket to **OCIO Img/Seq/Vid** and a `VIDEO` socket to **ComfyUI Video**.
This is a label only. The wire underneath is a plain, standard ComfyUI `IMAGE` or `VIDEO` type, the same
type `LoadImage`, `PreviewImage`, `SaveVideo` and every model node in ComfyUI already use. There is no
special OCIO tensor format to learn.

## The colorspace combo (55 entries, shared by four widgets)

`OCIO Read`'s `input_colorspace` and `output_colorspace`, and `OCIO Write`'s `input_colorspace` and
`output_colorspace`, all draw from the exact same list: every colorspace the active OCIO config exposes
(`cfg.getColorSpace().getName()` for each space in `cfg.getColorSpaces()`, live-loaded, not a hard-coded
table in this pack). Confirmed live at exactly 55 entries. In the order the API returns them:

```
 1. sRGB - Display
 2. Gamma 2.2 Rec.709 - Display
 3. Display P3 - Display
 4. Display P3 HDR - Display
 5. P3-D65 - Display
 6. Rec.1886 Rec.709 - Display
 7. Rec.2100-HLG - Display
 8. Rec.2100-PQ - Display
 9. ST2084-P3-D65 - Display
10. ACES2065-1
11. ACEScc
12. ACEScct
13. ACEScg
14. ADX10
15. ADX16
16. Apple Log
17. ARRI LogC3 (EI800)
18. Linear ARRI Wide Gamut 3
19. ARRI LogC4
20. Linear ARRI Wide Gamut 4
21. BMDFilm WideGamut Gen5
22. Linear BMD WideGamut Gen5
23. DaVinci Intermediate WideGamut
24. Linear DaVinci WideGamut
25. CanonLog2 CinemaGamut D55
26. Linear CinemaGamut D55
27. CanonLog3 CinemaGamut D55
28. D-Log D-Gamut
29. Linear D-Gamut
30. V-Log V-Gamut
31. Linear V-Gamut
32. Log3G10 REDWideGamutRGB
33. Linear REDWideGamutRGB
34. S-Log3 S-Gamut3
35. S-Log3 S-Gamut3.Cine
36. S-Log3 Venice S-Gamut3
37. S-Log3 Venice S-Gamut3.Cine
38. Linear S-Gamut3
39. Linear S-Gamut3.Cine
40. Linear Venice S-Gamut3
41. Linear Venice S-Gamut3.Cine
42. sRGB Encoded Rec.709 (sRGB)
43. Gamma 1.8 Encoded Rec.709
44. Gamma 2.2 Encoded Rec.709
45. Gamma 2.4 Encoded Rec.709
46. Camera Rec.709
47. sRGB Encoded P3-D65
48. Gamma 2.2 Encoded AdobeRGB
49. sRGB Encoded AP1
50. Gamma 2.2 Encoded AP1
51. Linear AdobeRGB
52. Linear P3-D65
53. Linear Rec.2020
54. Linear Rec.709 (sRGB)
55. Raw
```

Roughly: 1 to 9 are display-referred view transforms (what a monitor shows), 10 to 13 are the ACES scene
and working spaces, 14 to 41 are camera-native log/gamut pairs (a `LogCurve GamutName` entry and,
usually, a matching `Linear GamutName` entry for the same sensor), 42 to 53 are display-encoded and
linear variants of common delivery gamuts, and 55 is the escape hatch that means "don't touch these
numbers." A field's own table below only says "one of the 55 colorspaces" and points back here; it does
not repeat the list.

Copy these strings exactly. A combo value that doesn't match one of them fails the whole job with an
HTTP 400 and no partial result, whether you're typing into the node or building a prompt by hand.

---

## OCIO Read

### What it's for

`OCIO Read` is this pack's answer to Nuke's Read node: point it at a still, a folder of numbered
frames, one frame of a sequence, or a video file, tell it what colorspace the file is actually encoded
in, and it hands the rest of the graph a color-managed `IMAGE` batch already converted into your working
colorspace (sRGB - Display by default, which is the space ComfyUI's own `LoadImage` assumes). It also
resolves Nuke-style sequence gaps and out-of-range frames, and it carries the plate's own header metadata
(camera, lens, reel, timecode) out on a side wire so `OCIO Write` can put it back into a delivered file
later.

### Every input

| Input | Type | Accepts | Default | What you wire into it |
|---|---|---|---|---|
| `source` | STRING (widget) | A path to a still, a sequence folder, one numbered frame, or a video, anywhere on disk (absolute, e.g. `E:\path\to\shots\LeftGirl.v01`) or relative to the ComfyUI input folder. Use the **Open Files** button or type it. | `""` | This is a plain text widget, not a socket, but like any ComfyUI STRING widget it can be right-click-converted to an input and driven by a STRING output from another node (a path-builder node, for example). Left as a widget, you type or browse to it. |
| `frame_mode` | COMBO | `auto`, `single`, `sequence`, `video` | `auto` | Widget only. `auto`: a numbered file with siblings on disk becomes the whole sequence; `single`: load only the exact file named in `source`; `sequence`: force-collapse the file's siblings into a sequence even if `auto` wouldn't have; `video`: treat `source` as a movie clip. A folder is always read as a sequence and a video file is always read as its full clip regardless of this setting. |
| `input_colorspace` | COMBO (55 colorspaces) | See the shared list above. | `sRGB - Display` | Widget. This is what the *file* is claimed to be in, not what you want out. The live default is `sRGB - Display`; the pack's front end additionally auto-sets it the moment you pick a file, but that auto-set is a JavaScript convenience, not something the server does - build the graph through the API and you must set it yourself. What it picks: `ACEScg` for `.exr`/`.hdr`; for a video, see the table below. **It is a guess from the container, and you can always overrule it** - a log-encoded ProRes (LogC, S-Log, V-Log) carries no tag that says so, and will be guessed as Rec.709. |

How a video's `input_colorspace` is guessed (`_video_input_cs`), in order:

| The file says | Guess | Why |
|---|---|---|
| transfer `smpte2084` (PQ) | `Rec.2100-PQ - Display` | an HDR transfer is unambiguous |
| transfer `arib-std-b67` (HLG) | `Rec.2100-HLG - Display` | same |
| primaries `bt2020`, or a `bt2020nc`/`bt2020c` matrix | `Rec.2100-PQ` / `-HLG` | wide-gamut UHD |
| SDR, and the codec is **ProRes / DNxHD**, or the container is **MXF** | `Rec.1886 Rec.709 - Display` | a post codec in a professional container is a camera or mastering file: it is graded and viewed on a BT.1886 reference display, not on a web browser |
| anything else SDR (h264/hevc `.mp4`, plain `.mov`) | `sRGB - Display` | an internet deliverable, and most viewers are on sRGB |

The ProRes/DNxHD/MXF row exists because the colour tags alone cannot separate the two cases. Measured on
a real DaVinci Resolve MXF of ProRes 4444 XQ: it reports `color_space=bt709` with **both** primaries and
transfer `unknown`, which is indistinguishable from an untagged web clip by tags. The container and codec
do distinguish them - nobody publishes an MXF to the web.
| `output_colorspace` | COMBO (55 colorspaces) | See the shared list above. | `sRGB - Display` | Widget. What you want the `IMAGE` output converted into. `sRGB - Display` is ComfyUI's own working space (plain gamma-encoded, what `LoadImage` produces), so leaving this at default hands downstream nodes exactly what they already expect from any other loader. |
| `raw_data` | BOOLEAN | true / false | `false` | Widget. Nuke's "Raw Data": when on, the file's numbers are passed straight through with no colorspace conversion at all, and `input_colorspace`/`output_colorspace` are ignored (the alpha channel is unaffected either way, colorspace conversion never touches it). |
| `start_frame` | INT | 0 to 100000000 | `0` | Widget. First frame **number** to load (a file's own number, e.g. `86`, not a batch index). `0` means "from the detected start." A number below the sequence's real first frame is filled in according to `edge_mode`. |
| `end_frame` | INT | 0 to 100000000 | `0` | Widget. Last frame number to load. `0` means "to the detected end." Above the real last frame, `edge_mode` fills in. |
| `frame_shift` | INT | 0 to 100000000 | `0` | Widget. Re-bases the numbering *downstream*: the number the first loaded frame becomes for `OCIO Write`'s `first_frame`/`start_number`. `0` keeps the source's own number (a sequence that starts at frame 86 stays labeled 86). This value, not the pixels, is what changes; the batch itself is untouched. |
| `missing_frames` | COMBO | `black`, `hold`, `error` | `black` | Widget. What to do about a genuine gap *inside* the detected range (frame 24 missing between 23 and 25): a black frame, repeat the previous good frame, or raise and stop the whole job. Any gaps found are also listed in the `info` output. |
| `edge_mode` | COMBO | `hold`, `loop`, `bounce`, `black` | `hold` | Widget. What to do for frames *outside* the sequence's real range that `start_frame`/`end_frame` asked for anyway: hold the nearest end frame, loop the whole clip, ping-pong (bounce), or black. Has no visible effect unless your requested range actually extends past what's on disk. |
| `fps` | FLOAT | 0.0 to 240.0, step 0.001 | `0.0` | Widget. `0` reads the rate from the file (a video's own metadata, or an EXR's `framesPerSecond` header attribute if the sequence carries one, falling back to 23.976 for a sequence that carries none). A non-zero value here overrides that and is what flows to `OCIO Write`'s `fps` socket if you wire it. |

### Every output

`RETURN_TYPES = (IMAGE, MASK, FLOAT, STRING, VIDEO, STRING)`. The slot order matters: connections in a
saved graph are stored by index, not by name, so the "metadata" output sits last on purpose (see
Traps).

| Output (slot) | Type | What it connects to |
|---|---|---|
| `image/sequence/video` (0) | IMAGE | Any standard ComfyUI `IMAGE` consumer: `PreviewImage`, `SaveImage`, `ImageScaleBy`, `VAEEncode`'s `pixels` input, `ControlNetApplyAdvanced`'s `image`, `CLIPVisionEncode`, `CreateVideo`'s `images`. Also every other OCIO node's `image` input (`OCIO CDLTransform`, `OCIO ColorSpace`, `OCIO Display`, `OCIO FileTransform`, `OCIO LookTransform`, `OCIO LogConvert`), `OCIO Write`'s `images` input, and `OCIO Player`'s `images` input. It is a plain float `IMAGE` batch, nothing bespoke. |
| `alpha` (1) | MASK | `OCIO Write`'s `alpha` input, or any stock mask node: `MaskPreview`, `GrowMask`, `FeatherMask`, `ImageCompositor`, `MaskComposite`. Always present in the batch (opaque, all-ones, when the source file carries no alpha channel of its own). |
| `fps` (2) | FLOAT | `OCIO Write`'s optional `fps` input (wire it there to carry the source's real rate into the write), `CreateVideo`'s `fps`, or any node that takes a FLOAT. |
| `info` (3) | STRING | A human-readable status line (kind, frame count, resolution, missing frames, colorspace conversion applied). Meant for a text/notes node or just reading in the console; nothing downstream parses it. |
| `ComfyUI Video` (4) | VIDEO | `SaveVideo`'s `video` input, `GetVideoComponents`, `OCIO Write`'s optional `video` input, `OCIO Player`'s `video` input, or any partner video node that takes a native `VIDEO` (Kling, Runway, and similar). Built from the same color-managed batch as output 0, at the resolved `fps`; it exists so nodes that specifically require ComfyUI's native `VIDEO` type (not a raw `IMAGE` batch) have something to plug into. |
| `metadata` (5) | STRING | Only meaningful wired into `OCIO Write`'s `metadata` input (a `forceInput`-only socket, see below). It's a JSON blob: `{"source": <filename>, "kind": <format>, "attrs": {...}}`. Nothing else in the pack reads it, though you can pipe it into any STRING-consuming debug node to inspect it. |

### Widget visibility by source kind

The front end shows a different widget set depending on the *detected kind* of the current `source`
(`still`, `sequence`, or `video`), read straight from `web/ocio_io.js`'s `READ_VIS` table. This is a
canvas-only convenience: posting a prompt straight to the API always requires every `required` field
regardless of what the canvas would have shown or hidden.

| Widget | still | sequence | video |
|---|---|---|---|
| `source` | shown | shown | shown |
| `frame_mode` | hidden | shown | hidden |
| `input_colorspace` | shown | shown | shown |
| `output_colorspace` | shown | shown | shown |
| `raw_data` | shown | shown | shown |
| `start_frame` | hidden | shown | shown |
| `end_frame` | hidden | shown | shown |
| `frame_shift` | hidden | shown | hidden |
| `missing_frames` | hidden | shown | hidden |
| `edge_mode` | hidden | shown | hidden |
| `fps` | hidden | shown | shown |

The `Detect from Source`, `Open Files`, `▾ Viewer` and `▾ Metadata` buttons, plus the panels they open,
are always visible regardless of kind. The `ComfyUI Video` output socket is likewise hidden on
the node face unless the detected kind is `video` (`_setVideoOutput`), but this is a display choice: the
Python side always computes and returns a `VIDEO` object at slot 4 no matter what the source is, so a
prompt built directly against the API always has that value available at that index.

### The two panels on the node face

Each has its own disclosure button, and each remembers nothing between sessions - they are display, not
settings. The chevron follows the state: pointing down when the block is open, right when it is closed,
and the node gives the space back when you close one.

**`▾ Viewer`** folds the picture: the thumbnail, the transport bar and, on `OCIO Player`, the exposure
strip.

**`▾ Metadata`** folds the header read-out - plain text, one `Label: value` line each, no graphs. It
lists what the file itself says, in the order you read it: what the file IS (resolution, format, codec,
pixel format, frame range, missing frames, fps, colorspace, colour primaries and transfer, alpha) and
then which picture it is (reel, scene, shot, take, camera, lens, **timecode**). It is the same read that
travels down the `metadata` wire to `OCIO Write`, so what you see here is what will be delivered.

**A row with nothing to say is not drawn.** A plate with no lens tag costs no line, so the panel is
short on a bare EXR and long on a camera master. Two consequences worth knowing:

- A *value* the file does not carry is simply absent. If `Timecode` is missing from the list, the source
  has none, and nothing downstream will invent one.
- A **UMID is not shown as a reel.** Some applications park a SMPTE ST 330M identifier in the reel field
  (measured: DaVinci Resolve writes `com.apple.proapps.reel=0x060A2B34...` into a ProRes master). That is
  a 32-octet machine identifier, not a name - a reel name is 8 characters in a CMX3600 EDL and up to 32
  on Avid, so it could not travel as one anyway. The value is **not** discarded: it still goes into the
  written file under its own attribute. It is only refused the claim of being the shot's reel.

### Worked wiring examples

**1. Read an EXR sequence, grade it, and hand it to OCIO Write (the most common job).**

```
OCIO Read (source = a sequence folder, input_colorspace = ACEScg, output_colorspace = ACEScg)
  -> OCIO CDLTransform (slope/offset/power/saturation grade, image input/output)
  -> OCIO Write (input_colorspace = ACEScg, output_colorspace = ACEScg, container = sequence, still_format = exr)
```
Wire `OCIO Read`'s `metadata` output (slot 5) into `OCIO Write`'s `metadata` input as a second,
parallel wire alongside the image chain, so the plate's camera/lens/reel identity survives into the
delivered master. Reading and writing in the same colorspace (`ACEScg` in, `ACEScg` out) means the grade
node is working on the plate's own scene-linear values with nothing converted away.

**2. Read a client-supplied QuickTime natively, without ever decoding it to a raw `IMAGE` batch by hand.**

```
OCIO Read (source = a .mov file, frame_mode = video, output_colorspace = sRGB - Display)
  -> [IMAGE output] -> OCIO ColorSpace (a second regrade pass) -> PreviewImage
  -> [ComfyUI Video output] -> GetVideoComponents -> (images, audio, fps as separate outputs)
```
Both outputs come from the identical decoded, color-managed batch: use the `IMAGE` output when the next
node only understands ComfyUI's plain tensor type, and the `ComfyUI Video` output when the next node
specifically wants the native `VIDEO` type (most partner video-to-video nodes do).

**3. Read a single reference still that carries its own alpha channel.**

```
OCIO Read (source = a single PNG or TIFF with an alpha channel, frame_mode = auto -> resolves to single)
  -> [image output] -> PreviewImage
  -> [alpha output] -> MaskPreview
```
A lone file with no numbered siblings resolves to `kind: still` automatically; `frame_mode` never needs
to be touched by hand for this case. The alpha output is a full MASK batch even for a one-frame read, so
it plugs into any core masking node exactly like a mask from `LoadImageMask` or `ImageToMask` would.

**4. Read a plate for an image-to-image generation pass.**

```
OCIO Read (source = a plate frame or sequence, output_colorspace = sRGB - Display)
  -> VAEEncode (pixels = image output, vae = your checkpoint's VAE)
  -> KSampler (latent_image = the encoded latent, plus model/positive/negative/etc.)
  -> VAEDecode -> OCIO Write (input_colorspace = sRGB - Display, output_colorspace = ACEScg, still_format = exr)
```
`VAEEncode`'s image input is literally named `pixels`, not `image`; it accepts the same plain `IMAGE`
type `OCIO Read` emits with no adapter needed. This chain reads a plate, runs it through a diffusion
pass, and writes the regraded result back out as a scene-linear master.

### Traps

> **`frame_mode: "auto"` is real backend behavior, unlike `OCIO Write`'s `auto_range` (see below).**
> `load_source()` itself implements the auto/single/sequence branching in Python
> (`_sequence_siblings`, checked directly against `frame_mode`), so a prompt posted straight to the API
> with `frame_mode: "auto"` and a single frame path correctly picks up the whole sibling sequence with
> no front end involved. This was reproduced directly: posting `source` as one frame of a 3-frame test
> sequence with `frame_mode: "auto"` loaded all 3 frames server-side. Contrast this with `OCIO Write`'s
> `auto_range`, which does nothing at all once a prompt leaves the canvas.

> **The "metadata" output does not ride along on the IMAGE wire, and it can't.** A ComfyUI
> `IMAGE` is strictly a `[N, H, W, C]` float tensor: there is no side channel on that type for arbitrary
> key/value header data, the way a file format's own header can carry one. That's why the plate's
> camera, lens, reel and timecode information travels on its own dedicated STRING output (slot 5) as a
> JSON blob, wired separately into `OCIO Write`'s `metadata` input. It is easy to forget this second
> wire because the graph still runs and writes a file without it; the write just gets none of the
> plate's own identity fields; only what `OCIO Write` authors about its own output.

> **The metadata output is index 5, not adjacent to the image output, and that's deliberate.** A saved
> ComfyUI graph stores a link by *slot index*, not by name. Inserting a new output slot between existing
> ones would silently repoint every saved graph's connections below it (a workflow that used to wire
> `alpha` into a mask node could reload with `fps` wired there instead). The metadata output was added
> at the end for exactly this reason and will stay there.

> **`missing_frames: "error"` stops the whole read, not just the gap.** If a frame really is missing
> from inside the detected range and this is set, `load_source` raises and the graph fails; there is no
> partial result. Use `black` or `hold` if you want the render to proceed with a placeholder instead.

> **`edge_mode` only matters if your requested range extends past what's actually on disk.** Setting it
> to `loop` or `bounce` has no visible effect if `start_frame`/`end_frame` sit entirely inside the
> sequence's real range; there's nothing "outside" to fill.

### Not verified in this pass

Video decoding of a very large clip (the adaptive RAM-based decode budget in `_video_decode_budget`,
and the `capped` flag it can set on `info`) was read in source but not exercised with a clip large
enough to trigger capping. DPX header parsing (`_read_dpx_meta`) and its byte offsets were read and are
extensively commented with their own derivation in source, but no real DPX file was available in this
session to read through `OCIO Read` end to end; the EXR, PNG and video paths were.

---

## OCIO Write

### What it's for

`OCIO Write` is this pack's Nuke-style Write node: take an `IMAGE` batch (or a native ComfyUI `VIDEO`),
convert it from your working colorspace into the file's target colorspace, and save it as a still, an
image sequence, or a video, with the container's real color tags set so the file doesn't gamma-shift
between players. It also authors and carries metadata: every write gets a `<name>.json` sidecar next to
it recording everything the chosen format's header can't hold, and an EXR, TIFF, MOV, MP4 or MXF gets as
much of that same information embedded directly in the file as that format allows.

### Every input, required

All eighteen of these are plain widgets, not `forceInput` sockets (confirmed from the live schema: none
of the `required` fields carry that flag, unlike `metadata` below). Like most ComfyUI widgets they can
still be right-click-converted to an input and driven by a wire; none of them are forced into that shape
the way `metadata` is.

| Input | Type | Accepts | Default | Notes |
|---|---|---|---|---|
| `profile` | COMBO | `none`, `auto`, `LTX 2.3 HDR`, `LumiPic LogC3 (Flux/Qwen)`, `LumiPic V10 LogC4`, `Seedance 4K 10-bit`, `SDR Rec.709 delivery` | `none` | A source preset that silently sets `input_colorspace`/`output_colorspace` (and, for the HDR presets, forces `still_format = exr`, `bit_depth = 16f`). See the dedicated section below; several of these values do nothing at all on the server. |
| `input_colorspace` | COMBO (55 colorspaces) | See the shared list above. | `sRGB - Display` | What your `IMAGE` batch is actually in right now (ComfyUI's own working space by default). Should match whatever colorspace the upstream chain, usually an `OCIO Read`'s `output_colorspace`, left the pixels in. |
| `output_colorspace` | COMBO (55 colorspaces) | See the shared list above. | `ACEScg` | What the *file* should be encoded in. Defaults to `ACEScg` because the default `still_format` is `exr`, and an EXR is expected to be scene-linear render data. |
| `container` | COMBO | `still image`, `sequence`, `video` | `sequence` | Controls which of `still_format`/`video_codec` is used and which frame-range widgets are visible (see the table below). |
| `still_format` | COMBO | `exr`, `tiff`, `png`, `jpeg`, `dpx` | `exr` | Used for `still image` and `sequence`; hidden for `video`. `dpx` was added after the pack was found to *read* DPX with no way to write it, which for the format that exists to move plates between a film pipeline and everyone else is the wrong asymmetry. Integer only, and it takes `bit_depth` 10 or 16; anything else raises and names those two. Written through ffmpeg's `dpx` encoder (`gbrp10le` at 10, `rgb48le` at 16) rather than by a second hand-rolled SMPTE ST 268 header, since this pack already maintains one of those on the reading side and two copies of a header layout drift. Verified by reading each file back with this pack's own DPX reader on a 4096-step ramp: 1024 distinct levels at 10-bit, 4096 at 16-bit. Netflix's Non-Graded Archival Master specification names 16-bit DPX first for log-encoded material. |
| `video_codec` | COMBO (17) | `prores_4444`, `prores_422hq`, `prores_422`, `dnxhr_hq`, `h264`, `hevc`, `dnxhr_hq_mxf`, `dnxhr_hq_mxf_opatom`, `dnxhr_hqx`, `dnxhr_444`, `prores_4444_mxf`, `prores_4444xq_mxf`, `dnxhr_hqx_mxf`, `dnxhr_444_mxf`, `hevc_444_12`, `prores_4444xq`, `ffv1` | `prores_4444` | Used for `video`; hidden otherwise. Fixes both the real bit depth and the container extension. **The full measured table is in README.md under "What each codec actually writes"** - every row there comes from encoding a file and reading it back with `ffprobe`, not from restating this list. The four things that decide a choice: **`ffv1`** (`.mkv`, `gbrp16le`) is the only entry that returns this pack's own 16-bit input unchanged, confirmed by md5, and is a Library of Congress Preferred Format for preservation; **`hevc_444_12`** (`.mp4`, `yuv444p12le`) is the only genuine 12-bit encode ffmpeg can produce; every **ProRes** entry is 10-bit data however it reads back, because all three ProRes encoders in ffmpeg advertise only 10-bit pixel formats and decline a 12-bit request out loud; every **DNxHR** entry tops out at 10-bit for the same reason. `h264`/`hevc` write 8-bit 4:2:0 for an SDR delivery and **move to 10-bit when `output_colorspace` is a BT.2100 space**, since HLG and PQ are not defined below 10 bits; the three 8-bit-by-profile DNxHR entries refuse that combination and name a codec that can carry it. MXF entries are OP1a except `dnxhr_hq_mxf_opatom`, which is OPAtom and therefore writes a wired audio track beside the picture as a `.wav` rather than muxing it in. |
| `bit_depth` | COMBO | `16f`, `32f`, `16`, `8`, `10` | `16f` | The full combo the server accepts; the canvas narrows the visible choices to what the current `still_format` supports (EXR: 16f/32f; TIFF: 8/16/32f; PNG: 8/16; JPEG: 8 only; DPX: 10/16) but the **server does not enforce this narrowing**, except for DPX, which raises on anything but 10 or 16 because there is no float DPX to fall back to. `10` is appended at the end rather than placed next to `8`, because a combo's saved value is matched by string and reordering the list would re-point workflows that stored an index. See Traps. |
| `compression` | COMBO | `zip`, `zips`, `piz`, `pxr24`, `dwaa`, `dwab`, `rle`, `none` | `zip` | EXR-only. **`zip` is the default and it is lossless.** It was `dwaa` for one day, on the reasonable-sounding argument that lossy-and-small is the house default across VFX for anything that is not a master; measuring it on a real camera frame at 1920x1318 ended that. `16f` at `zip` came back with 1843 distinct green values and a maximum error of 0.000118 in 6342 KB; the same frame at `dwaa` gave 855 values and an error of 0.009525 in 1164 KB. DWA does deliver its side of the deal, 5.4x smaller, and it costs 54% of the distinct values and eighty times the error - **including at `16f`**, which nothing here said before, because the documented caveat only covered `32f`. A pack whose argument is that it does not throw information away should not have a lossy default, and Nuke, whose Write node this one is modelled on, defaults to Zip. Two things still worth knowing when you do pick DWA: (1) it **quantises float32 to half before compressing** - OpenEXR's own behaviour, stated in `ImfDwaCompressor` - so `32f` + DWAA writes half precision under a header that still declares `float`; measured on 49152 pixels, the 32f/zip file carried 49086 values no half can represent and the same data at 32f/dwaa carried none, distinct values collapsing from 49071 to 4765. The node reports this in its own note and in the log. (2) DWA destroys data passes: a depth pass came back with a maximum error of **172 units**, normals lost unit length, an ID pass was unrecognisable, and `raw_data` does not exempt them. `zip`/`zips`/`rle` are lossless; `piz` is lossless and suits grain; `pxr24` is lossy at a fixed 24-bit float precision. **Pick `dwaa` for a review or comp copy, never for a master or a data pass.** Your choice is saved in the workflow, so a graph set either way reopens that way - a default only ever reaches a node you create fresh. |
| `auto_range` | BOOLEAN | true / false | `true` | Canvas-only. See Traps: this does nothing when a prompt is posted directly to the API. |
| `first_frame` | INT | 0 to 100000000 | `1` | For `still image`: which single frame of the incoming batch to save. A number outside the batch is **refused by name** (`frame 999 is not in the input (frames 1-3, 3 frame(s))`), not rounded to the nearest one. It used to clamp: asking for frame 999 of a 3-frame batch wrote `name.0999.exr` containing frame 3 and reported success, so the filename stated which frame it held and the statement was false. For a deliberate hold or loop past the end, `OCIO Read`'s `edge_mode` does it as a named choice. For `sequence`/`video`: the first frame **number** to write, matched against `source_start` to find the right slice of the batch. |
| `last_frame` | INT | 0 to 100000000 | `0` | For `sequence`/`video`: the last frame number to write; `0` means "to the end of the batch." Ignored for `still image`. |
| `start_number` | INT | 0 to 100000000 | `1` | The output file's own numbering start (the delivered name's frame number), independent of the source's numbering. This is a re-base, not a retime: it renames frames, it does not resample them. |
| `source_start` | INT | 0 to 100000000 | `1` | The source batch's own first frame number, used to translate `first_frame`/`last_frame` into a 0-based slice of the incoming tensor. Despite the tooltip's "set by the wire" language, there is no data wire for this: it's a plain widget the canvas fills in by tracing the graph topology back to an `OCIO Read` (see Traps). |
| `raw_data` | BOOLEAN | true / false | `false` | Skips the `input_colorspace -> output_colorspace` pixel conversion entirely, **and** skips authoring any colorimetry (chromaticities, `adoptedNeutral`, `colorInteropID`, `com.ocio.colorspace`) into the output. Frame rate, frame counter and timecode are still written. Confirmed on a real write: with `raw_data` on, the PNG carried a `timecode` text chunk but no `colorspace` chunk and no chromaticities. **This now holds for movies too, and it did not before.** Video went down a separate path that had no "unspecified" branch, so a `raw_data` ProRes came out tagged `bt709` / `iec61966-2-1` / `bt709` while the same flag on an EXR correctly wrote nothing (measured, identical pixels, only the flag flipped). Unconverted pixels have no delivery space to name: an untagged file leaves a player guessing, which is honest, while a confidently mistagged one makes it guess wrong and believe it is right. |
| `colorspace_in_name` | BOOLEAN | true / false | `true` | Puts the sanitized `output_colorspace` (or literally `raw` when `raw_data` is on) into the filename before the frame number, e.g. `name_acescg.0001.exr` or `name_rec_1886_rec_709_display.0086.mov`. See Traps for why this exists and what turning it off actually costs you. |
| `output_folder` | STRING | Empty for the ComfyUI output directory; `$OUTPUT` or `$OUTPUT/sub` for a path under it; a plain relative path for the same; an absolute path (e.g. `E:\path\to\shots\out` or `//nas/vfx/out`) is written there verbatim. | `""` | Use the **Output Folder** button to browse, or type it. Prefer `$OUTPUT/...` or a relative path over an absolute one: this string is stored in the saved *workflow*, not embedded in the delivered media file, but a workflow you share (as a `.json`, or a graph embedded in someone else's PNG) then reveals your local machine's folder layout. |
| `filename` | STRING | Any base name. | `"ocio_out"` | Numbering and extension are added automatically per the rules in the outputs section below. |
| `auto_colorspace` | BOOLEAN | true / false | `true` | A legacy, narrower ancestor of `profile: "auto"`: front-end only, and only fires when the immediate upstream node is Lightricks' `LTXVHDRDecodePostprocess`. Superseded by the `profile` widget; kept for old saved graphs. Has no server-side effect at all. |

### Every input, optional

These can be sockets. Several of them (`images`, `video`, `alpha`, `audio`, `metadata`) have no widget at
all and only exist as wires; the rest (`fps`, `render_nonce`, `write_audio`) start as widgets that a
direct wire will override.

**There is no timecode field here, on purpose.** A code typed into the writer is a code invented at
delivery; the one that has to survive the round trip is the one the plate arrived with. Wire `metadata`
and the start comes from the source file - then this node advances it per written frame, which is the
only form that conforms correctly downstream. Nothing wired, no timecode written.

| Input | Type | Accepts | Default | What you wire into it |
|---|---|---|---|---|
| `images` | IMAGE | A batch of one or more frames. | none | `OCIO Read`'s image output, any grading node's image output, `VAEDecode`, `EmptyImage`, `GetVideoComponents`' `images` output, or a generation node's own `IMAGE` output. Mutually exclusive with `video`: if both are wired, `video` silently wins and the `images` value is discarded with no warning (confirmed by reading `write()`: the `video is not None` branch unconditionally overwrites `images`). |
| `video` | VIDEO | A native ComfyUI `VIDEO` object. | none | `LoadVideo`, `CreateVideo`, `GetVideoComponents` does not itself emit VIDEO (it consumes one) but any node that emits native `VIDEO` works: partner video-to-video nodes, `OCIO Read`'s `ComfyUI Video` output. Renders the whole clip out using every other Write setting (container, codec, colorspace, bit depth); a `video` container additionally inherits the clip's own frame rate rather than the `fps` widget. |
| `alpha` | MASK | A single mask, or a batch matching the image batch. | none | `OCIO Read`'s `alpha` output, `SolidMask`, `ImageToMask`, `LoadImageMask`, or any MASK-producing node. Produces RGBA for EXR/TIFF/PNG; silently ignored for JPEG (no alpha channel exists in that format). |
| `fps` | FLOAT | 1.0 to 240.0, step 0.001 | `24.0` | `OCIO Read`'s `fps` output, to carry the true source rate into the write, or type a number directly. Only used for a `video` container (and only as a fallback when no `video` object with its own rate is wired). **It sets the time base; it does not resample.** The same frames are written whatever you put here, so the clip's duration moves and its speed with it. Measured, 24 frames in every case: at 24 the file is 1.000s, at 25 it is 0.960s, at 48 it is 0.500s (twice as fast, half as long), at 12 it is 2.000s. That is a *conform*, the same operation as the standard 24 to 25 PAL speedup, and not a *frame-rate conversion*, which would invent frames by duplication, 3:2 pulldown or optical flow to hold the duration. This node does not do the second, by design; the timecode it writes is counted in the base you pick here. NTSC rates are handed to the muxer as their exact rational (`23.976` goes out as `24000/1001`), because the decimal parses to `2997/125`, which MXF rejects outright and MOV silently carries. |
| `render_nonce` | STRING | Any string; a plain widget the canvas hides. | `""` | Internal. The on-node **Render** button bumps this to a fresh timestamp so a repeat render to the same path actually rewrites the file. See Traps: this exists because ComfyUI's own execution cache otherwise skips a second identical Write outright. |
| `audio` | AUDIO | A ComfyUI `AUDIO` dict (`{"waveform": [B,C,T] tensor, "sample_rate": int}`). | none | `LoadAudio`, `LTXVAudioVAEDecode` (named explicitly in the tooltip), or any AUDIO-producing node. A `video` container muxes it in (24-bit PCM for `.mov`/MXF OP1a, AAC for `.mp4`), trimmed to exactly the frame range being written. A `sequence` gets a sidecar `.wav` instead, because EXR/TIFF/PNG hold no audio track, and so does an MXF OPAtom write, which by design holds exactly one essence per file. |
| `metadata` | STRING | JSON from `OCIO Read`'s "metadata" output. | none | **`forceInput: true`**: unlike every other widget on this node, this field never renders as a text box, only as a wire-only socket. Wire `OCIO Read`'s slot-5 output here to carry the plate's camera/lens/editorial identity **and its start timecode** into the written file - this wire is the only route a timecode has, since the node has no field for one. Attributes describing a specific pixel state (C2PA manifests, ST 2086/2094 HDR mastering data, an ACES AMF, an MHL hash list) are dropped rather than copied, because a colorspace conversion makes them false; container attributes (`dataWindow`, `channels`, `compression`) are never copied either, since the writer recomputes those from the real pixels. The fields this node re-authors for itself (chromaticities, frame rate, frame counter, timecode) are stripped from the incoming set in **any spelling** and written fresh, so a plate that calls its code `timecode` where we call it `timeCode` cannot leave two conflicting timecodes in one header. Confirmed on a real write: a test attribute named to match the "mastering" filter was silently removed from both the EXR header and the sidecar JSON, while unrecognized custom attributes and the seven identity fields (reel, scene, shot, take, camera, lens, timecode) passed through intact. Confirmed on a real camera master (DaVinci Resolve MXF, ProRes 4444 XQ): all twelve of its attributes reached the EXR header, with a single, correctly typed timecode advancing per frame. |
| `write_audio` | BOOLEAN | true / false | `true` | Off: no audio at all is written, not even as a sidecar `.wav`, regardless of what's wired or what a native `Video` input carries. On (default): a wired `audio` input wins over a native `Video`'s own track. This is the only way to *decline* a `Video` input's own audio, since there's no wire to disconnect for it. |
| `view` | COMBO (the config's views, then the ACES 1.3 views, plus a do-nothing entry) | `(none) colorimetric, no tone map` first, then every view the loaded config offers, then the same list from the built-in ACES 1.3 config prefixed `ACES 1.3: ` | `(none)` | **Only does anything when one of your two colorspaces is display-referred and the other is scene-referred.** On every other pair it is ignored outright. **The version matters as much as the view:** an EXR rendered through the ACES 2.0 transform and viewed through 1.3 is off by 35.5% on the worst pixel (measured), which reads as "close, but the blacks and highlights are wrong". Match the version to the application that will open the file - Nuke 13 / 14 are on the 1.x line. Read the section below before using it. |
| `write_sidecar` | BOOLEAN | true / false | `true` | The `<name>.json` written beside the render, holding the FULL metadata set. Whether turning it off loses anything depends on the format, and the file says so itself under `sidecar_only`: beside a MOV that list names the attributes the container cannot hold, so the .json is their only home; beside an EXR it is **empty**, because the header already took all eight. Off is safe for an EXR sequence, and for any delivery that must be the picture files and nothing else. It never affects the pixels, the container tags, the EXR header or the `.wav` beside a sequence. |

### `view`: the one control that changes what the picture looks like

Every other widget on this node decides where the file goes, what wraps it, or how many bits it has. This one
decides what the image *is*, so it gets its own section.

**When it applies.** Only on a pair that crosses between scene-referred and display-referred. The node asks
OCIO which side each colorspace sits on, so this is not a list of names anyone maintains:

| pair | crosses? | does `view` do anything? |
| --- | --- | --- |
| `ACEScg` -> `Rec.1886 Rec.709 - Display` | yes, scene to display | **yes** |
| `Rec.1886 Rec.709 - Display` -> `ACEScg` | yes, display to scene | **yes** |
| `ARRI LogC3 (EI800)` -> `ACEScg` | no, both scene-referred | no, ignored |
| `ACEScg` -> `ACEScct` | no, both scene-referred | no, ignored |
| `sRGB - Display` -> `Rec.1886 Rec.709 - Display` | no, both display-referred | no, ignored |

So every camera-log conversion, everything between ACES spaces, and every display-to-display re-encode is
untouched by this widget. If your work is one of those, you never need to think about it.

**What the two answers are, in numbers.** Writing a movie from an `ACEScg` render:

| scene-linear in | `(none)` | `ACES 2.0 - SDR 100 nits (Rec.709)` |
| --- | --- | --- |
| 0.18 (mid grey) | 0.489436 | 0.383116 |
| 1.0 | 1.000006 | 0.722 |
| 4.0 | **1.781807** | 0.905089 |
| 16.0 | **3.174806** | 0.976046 |

The bold numbers are the problem. A Rec.709 container encodes up to 1.0, so 1.78 and 3.17 are both written as
white and every highlight above diffuse white lands on the same value. With a view they roll off instead, and
nothing reaches 1.0 until scene-linear around 128. **For a review movie out of a scene-linear render, pick a
view.**

Reading the other way, a Rec.709 picture taken back to `ACEScg` for a master: mid-grey 0.5 becomes 0.189468
with `(none)` and 0.324827 with a view, and the ceiling goes from 1.0 to about 128. The second is what Nuke
produces when its Read node is set to a display colorspace in an ACES project, and it is what a compositor
expects to receive.

**Why `(none)` is still the default, and when it is right.** It is what this pack has always done, and it is a
real named operation, not a mistake: OCIO calls the transform behind it `Un-tone-mapped`, and the result is
bit-identical to Nuke's own `Utility - Rec.709 - Display`. Keep it for a technical re-encode where the values
must not move, for material that is already scene-referred but tagged as display, and any time you have
already applied an output transform yourself with `OCIO Display` - applying it twice is worse than not
applying it at all.

**What the entries mean.** `Un-tone-mapped` and `Video (colorimetric)` produce exactly the same numbers as
`(none)`; they appear because the list is read from the config rather than curated. `Raw` leaves values alone.
The `ACES 2.0 - SDR 100 nits (Rec.709)` entry is the ordinary cinema render for a normal monitor; the `P3 D65`
and `HDR ... nits` entries target other displays and are wrong for a Rec.709 deliverable.

**A view belongs to a display, and the list now says so.** The combo is built once, when the node is
registered, as the union of every view across every display of both configs - so most of what it offers is
wrong for any given pair. Measured on the configs loaded by default: of its 32 real entries, 24 are invalid
for `Rec.1886 Rec.709 - Display`. The node narrows the list live as soon as both colorspaces are picked,
leaving the ones that display actually has. `ACES 1.3: ACES 1.1 - SDR Video (Rec.709 lim)` is the entry that
made this necessary: it reads like the obvious pick for a Rec.709 deliverable and it lives on
`Rec.1886 Rec.2020 - Display`, which the ACES 2.0 config does not have at all.

The narrowing never CHANGES your pick. A view restored from a saved workflow stays selected even when it is
not on the list, with the widget's label saying so, because silently swapping a rendering transform would
change the picture a finished graph produces. Render it anyway and it is refused, with a message naming what
that display does offer.

One more pairing is refused for a different reason: the two configs do not hold the same colorspaces.
`D-Log D-Gamut` and `Linear D-Gamut` exist in the ACES 2.0 config and not in the 1.3 one, so an `ACES 1.3: `
view with either of them on the scene side cannot be built at all - the view is fine for the display, the
source space simply is not in that config. Those entries are dropped from the list too, and reaching the
render with one names the colorspace rather than passing OCIO's own message through.

**On another config the names differ, and that is intended.** The list comes from whichever OCIO config is
loaded. On ACES 2.0 the SDR view is `ACES 2.0 - SDR 100 nits (Rec.709)`; on an ACES 1.3 config it is
`ACES 1.0 - SDR Video`, and the numbers differ too - the 1.x output transform tops out near 16.29 where 2.0
reaches 128. Neither is wrong; they are different versions of the standard.

### Every output

`RETURN_TYPES = (STRING,)`, `RETURN_NAMES = ("path",)`. `OCIO Write` is an `OUTPUT_NODE`, meaning it
runs as a side effect (it writes the file) whether or not this output is wired to anything.

### What appears on the node after a write

The node draws its own viewport, the same one for every container, with a transport under it and a `▾ Viewer`
chevron that folds the whole thing away and gives the height back - the same toggle `OCIO Read` carries.

That is worth a sentence of why, because it is the reason the node behaves consistently at all. A preview
handed back to ComfyUI as `images` is drawn by the FRONT END, in markup this pack does not own; nothing in an
extension can collapse it. So this node hands nothing back to be drawn. The cost is named rather than hidden:
its previews do not appear in ComfyUI's output gallery or queue history.

**The transport** is play / pause, stop (rewind to the first frame), a scrub and a frame counter. It is
smaller than `OCIO Read`'s on purpose - no in / out points, no reverse, no exposure strip - because this looks
at a write that has just finished on a local disk, and the question is "is that the clip I meant to make",
not "how does this grade".

**A `video` container** shows a small H.264 copy. The master can sit anywhere on disk, and ComfyUI's native
preview only serves `output`, `temp` and `input`; a still PNG also renders broken inside a video player. So a
browser-playable proxy goes to the temp dir instead. It carries the audio too, trimmed to the same frame cap,
so lip sync can be checked without opening the master.

**A `sequence` of more than one frame** plays as **its own frames**, not as a proxy. Every format that branch
writes - EXR, DPX, TIFF, PNG, JPEG - is one a browser either cannot decode or cannot animate, so a frame range
used to come back as a single still. It now flips through the written files, each rendered server-side through
OCIO by the same `/ocio/thumb` route `OCIO Read`'s viewer uses. That matters for a pack whose argument is
that it does not throw information away: an 8-bit re-encode is a poor way to show frames just written at 16
or 32 bits.

All five formats were checked in the canvas, from a source whose three frames are deliberately different
colours, so a strip that fell back to frame one could not pass. Each returned three distinct frames over the
written range, and the colour came back within one code value of what the graph started from - including a
DPX written in ADX10 and one in ARRI LogC3, which is only possible if the strip is decoding through the
colorspace the file actually holds.

The H.264 proxy is the **fallback**, not a companion: it ships only when the frame range cannot be described,
so exactly one preview appears on the node. Both at once meant two pictures of the same write disagreeing
about colour, with nothing on the node to say which was the master.

The `↻` in the viewport's top-left re-reads what was written, from disk, bypassing the browser's cache. Use it
when something else has written into that folder since - another graph, a retake, a re-render - and you want
to be sure you are looking at what is there now. The colorspace the strip renders through is the one that was
on the node when the write ran, so a later widget edit cannot make the picture disagree with the files. If a
frame cannot be read back, the strip is replaced by the folder path rather than freezing on the last good
frame.

**A single still** shows that one frame and no transport, because one frame has nothing to scrub.

No audio on the sequence path even when a track is wired, because a frame sequence carries none, and a
preview that played sound the files do not have would misrepresent what was produced.

Every preview is shown in the output colorspace, naively - so a wrong colorspace pick looks visibly wrong
rather than quietly wrong.

| Output | Type | What it connects to |
|---|---|---|
| `path` | STRING | The absolute path of the first file actually written (frame one of a sequence, the still, or the movie file). Useful wired into a notes/logging node, or into a downstream automation step that needs to know exactly where the render landed. Most graphs leave it unconnected, since the write already happened by the time this value exists. |

Naming, from `_write_output_paths`, the single function both the write and the "does this already
exist" overwrite-check dialog use, so they can never disagree:

- `still image` -> `<output_folder>/<filename>[_<colorspace_tag>].<ext>`, or with the source frame
  number stamped in when the batch has more than one frame and you grabbed a specific one:
  `<filename>[_<tag>].<source_frame:04d>.<ext>`.
- `sequence` -> `<output_folder>/<filename>[_<tag>].<start_number:04d>.<ext>`, one file per frame,
  4-digit, counting up from `start_number`.
- `video` -> `<output_folder>/<filename>[_<tag>]<ext>`, where the extension is decided once, in one
  place (`video_ext`), from the codec: `.mxf` for the two MXF codecs, `.mov` for every other
  `prores_*`/`dnxhr_*` codec, `.mp4` for everything else (`h264`, `hevc`).

### Which widgets appear only in which state

Confirmed directly from `web/ocio_io.js`'s `applyContainer`/`applyCompressionVis` functions, which run
on node creation and on every `container`/`still_format` change.

| Widget | `still image` | `sequence` | `video` |
|---|---|---|---|
| `still_format` | shown | shown | hidden |
| `video_codec` | hidden | hidden | shown |
| `bit_depth` | shown | shown | hidden (the real depth is shown instead as part of the `video_codec` widget's own label) |
| `compression` | shown only if `still_format = exr` | shown only if `still_format = exr` | hidden |
| `auto_range` | hidden | shown | shown |
| `first_frame` | shown, relabeled "frame to save" | shown | shown |
| `last_frame` | hidden | shown | shown |
| `start_number` | hidden | shown | hidden |
| `fps` (as a widget, when not wired) | hidden | hidden | shown |
| `source_start` | always hidden (internal) | always hidden | always hidden |
| `auto_colorspace` | always hidden (legacy) | always hidden | always hidden |
| `render_nonce` | always hidden (internal) | always hidden | always hidden |
| `write_audio` | shown only while `video` is connected and `audio` is not | same | same |

`write_audio` is the one widget whose visibility has nothing to do with the container. It appears for
exactly one situation: **sound arriving without a wire.**

Everywhere else the wiring has already answered the question. Nothing on `audio` writes no sound at all,
because there is none to write. Something on `audio` is a deliberate act, and pulling the wire is how you
undo it. A toggle that can only hold the answer it already has is a row of noise on every write node in the
graph.

The exception is a native ComfyUI `VIDEO`, which carries its own track inside the object: connect a movie
and the writer adopts that track, with no wire to represent it. That is the only case this widget exists
for, and it holds for a sequence too, where the track lands beside the frames as a sidecar `.wav`. Wire an
explicit `audio` alongside a `video` and the wire wins in the writer, so the toggle steps back out of the
way.

Where a timecode lands is unchanged, even though there is no longer a field for one. Measured by writing one
short sequence per format and reading each file back with a third-party reader rather than trusting the
node's own report:

| format | shot identity | timecode | how it is carried |
|---|---|---|---|
| **EXR** 16f / 32f | yes | yes | header attributes; the timecode is a typed `OpenEXR.TimeCode`, advancing per frame |
| **TIFF** 8 / 16 / 32f | yes | yes | real TIFF tags plus an XMP packet |
| **PNG** 8 / 16 | yes | yes | `iTXt` chunks, written ahead of the first `IDAT` |
| **JPEG** 8 | **no** | **no** | only the colorspace, as a JFIF comment |
| **ProRes / DNxHR / h264 / hevc / MXF** | reel where the container defines one | yes | a real `tmcd` timecode track, not a tag |

JPEG is the one that carries nothing, so nothing is promised for it. Every format gets the sidecar `.json`
regardless, which is where the full set always survives.

**Drop-frame comes from the plate, not from the frame rate.** At 29.97 and 59.94 both counts are legal and
both are in daily use, and the source says which it is: `;` before the frames is SMPTE's drop-frame marker,
`:` is non-drop, and an EXR `timeCode` attribute carries the flag as its fifth field. This pack reads that
and re-authors the sequence in the same count. Deriving it from the rate instead was wrong in both
directions, and both were live until 2026-08-13:

- a legal **non-drop** plate at 29.97 (say `01:01:00:00`) was declared drop-frame, then rejected by the
  drop-frame validator, and its timecode vanished from every written header with nothing said about why;
- a real **drop-frame** plate had its own flag parsed and thrown away, so a `00:00:59;29` start came back as
  `00:01:00;02` where the non-drop truth is `00:01:00;00` - a two-frame conform error produced from a signal
  that was already in the file.

Drop-frame is still only applied at 29.97 and 59.94, per SMPTE ST 12-1: a `;` on a 25 fps clip does not
invent a count that does not exist there.

### `profile`, in full

| Value | `input_colorspace` | `output_colorspace` | Forces format? | Resolved where |
|---|---|---|---|---|
| `none` | untouched | untouched | no | nowhere; this is the inert default |
| `auto` | untouched | untouched | no | **front end only** (see Traps) |
| `LTX 2.3 HDR` | `Linear Rec.709 (sRGB)` | `ACEScg` | EXR 16f | server, in `write()` |
| `LumiPic LogC3 (Flux/Qwen)` | `Linear Rec.709 (sRGB)` | `ACEScg` | EXR 16f | server; also decodes the ARRI LogC3 curve on the pixels themselves before the colorspace convert |
| `LumiPic V10 LogC4` | `Linear Rec.709 (sRGB)` | `ACEScg` | EXR 16f | server; decodes the ARRI LogC4 curve on the pixels first |
| `Seedance 4K 10-bit` | untouched | untouched | no | nowhere; a named placeholder, not implemented on either side |
| `SDR Rec.709 delivery` | `sRGB - Display` | `Rec.1886 Rec.709 - Display` | no (deliberately; this preset's whole point is a display-referred deliverable, so it must not push you onto a scene-linear EXR) | server, in `write()` |

`LTX 2.3 HDR` is a 2.3-only preset, and the version number is load-bearing. LTX 2.3's HDR path is an
IC-LoRA on the ARRI LogC3 curve, and Lightricks' own `LTXVHDRDecodePostprocess` node already undoes that
curve before you ever reach `OCIO Write`, so the 2.3 preset correctly expects linear values.

**There is no LTX 2.5 preset, and there used to be one.** LTX 2.5's HDR is a different mechanism: their
`--hdr` flag encodes ACEScct log codes directly, and it lives in their reference CLI. Nothing in ComfyUI
reaches it - their ComfyUI pack has no 2.5 HDR workflow, and ComfyUI's own core has no ACEScct path at
all - so a 2.5 graph here does not hand `OCIO Write` those codes to begin with. `LTX 2.5 HDR (ACEScct)`
was removed for that reason; CHANGELOG.md has the full account, including what it breaks. If you do have
genuine ACEScct material, undo the curve explicitly with `OCIO LogConvert` (`operation = Log to Linear`,
`curve = ACEScct`) before the write. Point the 2.3 preset at it instead and log gets treated as linear:
the image comes out flat and grey.

`auto` can only detect the 2.3 case, by finding `LTXVHDRDecodePostprocess` upstream. It has never had a
guess for 2.5, and now has no preset to guess toward either.

**Rechecked 2026-08-17, against their pack at `ac4d998`** (their update of 2026-08-11, which is what
finally made the pack import again after a core RoPE change had broken it). That update added nine new
workflows for 2.5 - `T2V_I2V` in one and two stages, `V2V`, `T2A`, and IC-LoRAs for Ingredients, Inpaint,
Outpaint, Motion Track and Union Control - and **none of them is an HDR workflow**. The only HDR graph in
the pack is still `example_workflows/2.3/LTX-2.3_ICLoRA_HDR_Distilled.json`, and the only HDR weights it
names are `ltx-2.3-22b-ic-lora-hdr-0.9.safetensors`. So the version number on this preset is not a
leftover; it is still the whole of what Lightricks ship for ComfyUI.

**Nothing is log-encoded on the way in.** Their `hdr.py` defines a `LogC3` class with both `compress` and
`decompress`, but `compress` is never called anywhere in the pack - the node only ever decompresses. Their
own HDR graph wires the source straight from `LoadVideo` into `LTXAddVideoICLoRAGuide` with no colour
conversion at all. The IC-LoRA takes ordinary display-referred SDR as its guide and emits LogC3; the curve
exists only on the way out. This matters for what you put in front of it: hand it a scene-linear EXR and
you are feeding it something it was never trained on. Convert to a display-referred encoding first with
`OCIO Color Space`.

**Their linear output cannot carry negatives.** `LTXVHDRDecodePostprocess` ends on
`torch.clamp(hdr, min=0.0, max=1e4)`, so out-of-gamut colour is not representable in what reaches
`OCIO Write`, whatever the write's own settings are. Worth knowing before reading a gamut plot of this
material and concluding something about the model.

**The range is real, and it is checkable without trusting anyone.** LogC3 puts code 1.0 at 55.08, so a
decode of codes confined to [0, 1] lands under 55.08. Read the condition, not just the number: the decode
is a plain function with no domain limit, and codes above 1.0 decode above 55.08 - which is exactly what
happens once the stock clamp is out of the chain (see the sun measurement further down, where the same
material reaches code 1.16 and 246 linear). So this check confirms the curve is being applied; it is
**not** evidence of a ceiling in the material.

Measured across all 25 frames of a two-stage run through
`example_workflows/OCIO_WORKFLOW_LTX_2.5_to_2.3_HDR.json` (LTX-2.5 generate -> LTX-2.3 HDR IC-LoRA ->
OCIO VAE Decode unclamped -> OCIO LogConvert, ARRI LogC3): peak 43.37 in ACEScg, 43.04 once converted
back to linear Rec.709, with 3.11% of pixels above 1.0 and p99.9 at 9.17. Under 55.08 and close enough to
it to show the curve is being used rather than nominally applied. That is the arithmetic saying the chain
is what it claims to be, rather than a preset being taken on faith.

Measure the whole sequence, not a few frames. The first four frames of that same run peak at 32.77, which
is 25% low: the brightest speculars arrive later in the clip, and a short sample simply does not contain
them.

### The two HDR mechanisms, from their own source

Read from Lightricks' reference repository (`Lightricks/LTX-2` at `fd4ded7`, 2026-08-11). They ship two
HDR paths, keep them in separate pipelines, and their code will not let you mix them. Knowing which one
produced your frames is what decides the settings on this node.

**`--hdr {SRGB_LINEAR,ACESCG,ACESCCT}` declares a colour space; it does not switch on an HDR model.**
`HDRColorSpace` in `ltx_pipelines/utils/media_io/color_config.py` documents itself as the "Explicit HDR
source / working colour space", where `None` means SDR. `SRGB_LINEAR` and `ACESCG` are scene-linear and get
compressed via ACEScct on load; `ACESCCT` is taken as already being log working codes, with no load-time
transfer. Following it through `distilled.py`, `distilled_mgpu.py`, `ic_lora.py` and `a2vid_two_stage.py`,
the flag reaches exactly three places: the check that an EXR input declared its space, `vae_dtype_for_hdr`
(float32 for HDR instead of the bf16 the pipelines otherwise use), and the `color_space` passed to
conditioning load and to the writer. It selects no checkpoint and loads no LoRA. It is also mandatory
rather than optional for EXR input: `resolve_hdr_color_space` raises if EXR frames arrive without it.

**The IC-LoRA path is a different pipeline and refuses that flag.** `ltx_pipelines/hdr_ic_lora.py` is its
own entry point, requires `--hdr-lora`, and describes itself as extending the standard IC-LoRA pipeline
"with HDR decode via LogC3 inverse transform". Its argument parser deliberately blocks `--hdr` from
abbreviating into `--hdr-lora`, and the comment on that line says why in their words: the native HDR flag
is not supported there. So LogC3 belongs to the LoRA path, ACEScct belongs to the native path, and nothing
in their code produces both at once.

**Which means for this node:** ACEScct material can only have come from their CLI, and LogC3 material can
only have come from the IC-LoRA - which is the one ComfyUI can run. That is the whole reason the preset
list carries a 2.3 entry and no 2.5 one.

### What an ACEScct read of LTX-2.5 actually buys

`example_workflows/OCIO_WORKFLOW_LTX_2.5_ACEScct_HDR_probe.json` builds their native-HDR shape here
(`sRGB -> ACEScg -> Linear to Log (ACEScct) -> LTX-2.5 -> decode -> Log to Linear (ACEScct) -> EXR 32f`),
so the claim can be measured rather than argued about. Measured 2026-08-17 over 49 frames, the codes
leaving the VAE ran p50 `0.343`, p99 `0.998`, max `1.041`. Reading those same numbers two ways:

| reading | peak | p99 | range p99/p50 |
|---|---|---|---|
| ACEScct | 368.7 | 217.2 | 2847:1, 11.5 stops |
| Rec.1886 display | 1.10 | 0.995 | 13:1, 3.7 stops |

Identical numbers both times. Nothing asks the model which reading is intended, so the range in the file
comes from the curve, not from the weights.

**And the two readings are not equally believable.** ACEScct codes diffuse white at `0.5548`, so the share
of codes above that number is the share of frame a reading claims is brighter than white paper in sunlight.
Under the ACEScct reading that share is **30.5%**. A lit exterior puts 1-10% of frame above diffuse white -
the sun disc, speculars, bright sky - and a third of the frame is not a physical picture. Read as display,
the same pixels put **0.55%** above white, which is ordinary. Rendered side by side the verdict is visible
rather than statistical: the display reading holds its cloud contrast and specular structure, while the
ACEScct reading goes milky across the sky even with an honest tonemap instead of a clip.

So on this evidence LTX-2.5 emits codes distributed like an ordinary display picture, and reading them as
ACEScct stretches the range rather than revealing one.

**The decisive measurement is taken on the finished picture, not on intermediate data.** Convert properly
first - undo the write's Rec.709 to AP1 matrix, recover the model's display codes, take BT.1886 to linear -
and only then apply exposure. Done in that order on a 1280x704 run of a deliberately punishing scene (a
figure leaving a dark cave onto a sunlit ocean, sun flaring into the lens), the headroom above diffuse
white is **2.21% of samples with a peak of 1.0544**. That is **0.08 of a stop**, not eleven. Step the
exposure down and the sun is gone by -4 EV, where genuine HDR would still hold a disc at -10. Downward
range is real: at +4 EV the cave rock still carries texture, so the shadows are not crushed to zero.

Run the exposure ladder before that conversion and it measures the curve instead of the picture, which is
how the same frames can look like eleven stops of latitude and 0.08 of a stop depending only on where the
ladder is applied. One scene, one seed, text-to-video - but the gap is three orders of magnitude, not a
judgement call.

### What the 2.3 IC-LoRA does at the sun, measured without the clamp

An earlier revision of this page reported that the IC-LoRA's peak lands on LogC3 code 1.0 exactly and
that no pixel exists above code 1.01, and concluded that the model paints the sun as a flat white
patch. **That was measured through the stock clamp described in the next section, so it could not have
shown anything above code 1.0 whatever the model did.** Re-measured on the same shot with the clamp
removed, the numbers are different enough to change the conclusion:

| same shot, 49 frames | top code | linear | spread, brightest 0.5% | distinct (3 dp) | px above code 1.01 |
|---|---|---|---|---|---|
| through the stock clamp | 1.0000 | 55.08 | 0.0000 | 1 | 0 |
| `OCIO VAE Decode`, `clamp = False` | **1.1607** | **246.1** | 0.1616 | 117 | **12 014** |

Code 1.1607 is **2.16 stops above** the code-1.0 value the old text called a ceiling. And the extra
range is not scattered noise: on one frame, pixels above code 1.01 make up 1.086% of a 121x121 box
centred on the disc against 0.005% of the rest of the frame, so they are concentrated where the sun is
by a factor of about 200.

**What survives from the old reading.** The disc still carries a broad near-flat shoulder just under
code 1.0 - a horizontal profile through it sits between 0.995 and 1.002 over roughly half its width -
so the model's gradation there is shallow, and a sun pointed into the lens clips on a real sensor too.
What is not true is that nothing exists above the curve's code-1.0 value; 12 014 pixels do, and the
stock path threw all of them away.

The lesson generalises past this model: a peak landing exactly on a known constant is evidence about
the pipeline before it is evidence about the generator. Count distinct values in the top band - a real
clip puts nearly every pixel on one value, as the top row above shows.

### Two stock clamps sit between the model and an HDR file

This is the case `OCIO VAE Decode` was written for, and it is worth stating precisely because that
node's own docstring correctly says it does **not** rescue range on ordinary material.

`comfy/sd.py` builds every VAE with
`process_output = lambda image: image.add_(1.0).div_(2.0).clamp_(0.0, 1.0)`, so `VAEDecodeTiled` cuts
at 1.0. Lightricks' `LTXVHDRDecodePostprocess` then clamps again
(`logc = torch.clamp((z + 1.0) / 2.0, 0.0, 1.0)`), though by then there is nothing left to catch.
Measured on the two-stage graph, the decoder actually emits up to **1.16**, and 0.08 to 0.12% of
samples sit above 1.0.

On display-referred material that overshoot is worth about a twentieth of a stop, which is why the
node's docstring plays it down. On **log-encoded** material it is worth far more, because LogC3 is
exponential near code 1.0: the ceiling at code 1.0 is 55.08, while code 1.16 decodes past 240. The
same 0.08% therefore carries **1.84 stops**.

Replacing both stock nodes with the pack's own - `OCIO VAE Decode` at `clamp = False` and
`OCIO LogConvert` (`Log to Linear`, `ARRI LogC3`, identical published constants, no clamp on either
end) - measured on one frame at 1280x704, across the brightest 0.1%:

| | stock chain | pack chain |
|---|---|---|
| distinct values | 46 | 334 |
| range | 0.03 stops | 0.82 stops |
| pixels at the maximum | 259, a plateau | 1, a real peak |

What it does not fix: the sun disc stays flat. A profile through it reads 54.0 across ninety pixels
in both chains, which is code 0.998 - the model painted the disc as a fill, and no decode invents
gradation that was never generated. What returns is the specular structure around it.

`example_workflows/OCIO_WORKFLOW_LTX_2.5_to_2.3_HDR.json` ships wired this way.


### Which display colorspace to read the output as

The rule is short and it decides several widgets at once: **the output is read back the way the input was
converted.**

Image-to-video converts the plate `sRGB - Display` -> `Rec.1886 Rec.709 - Display` before the model, so
every node treating the decode as a display signal uses `Rec.1886 Rec.709 - Display`. Text-to-video has no
plate and no conversion, so the model emits its own sRGB-like signal and those same nodes must say
`sRGB - Display`. Linear spaces are unaffected: `Linear Rec.709 (sRGB)` names both, since Rec.709 and sRGB
share primaries and differ only in transfer.

`bypass` on `LTXVImgToVideoInplace` is what switches the mode, and their code makes it total:
`if bypass: return (latent,)` in `comfy_extras/nodes_lt.py` skips the input image and every colour
conversion in front of it.

Getting it wrong reads as a grading fault rather than a settings one. Measured: a text-to-video sequence
read as Rec.1886 needed x8.52 to reach mid grey where its reference needed x2.97, about 1.5 stops adrift.
Read correctly as sRGB the same pair came out at x1.21 and x0.96.

One trap that graph documents, because it silently voids the input half of the test: switching it to
text-to-video sets `bypass` on `LTXVImgToVideoInplace`, and `comfy_extras/nodes_lt.py` then returns the
latent untouched. The input image, and any colour conversion in front of it, is skipped entirely.

The two `LumiPic` presets do something the `LTX 2.3 HDR` preset doesn't: they run a curve decode on the pixel
values themselves (`_logc3_to_lin`/`_logc4_to_lin`, published ARRI constants applied directly, not an
OCIO colorspace lookup) before setting the colorspaces. This is deliberately not the same as picking
`ARRI LogC3 (EI800)` from the colorspace combo directly: that combo entry assumes ARRI Wide Gamut
primaries, while the LumiPic LoRA family's data stays in Rec.709 primaries with only the curve
resembling LogC3/LogC4.

### Worked wiring examples

**1. Hand a generation straight to an editor, with no `OCIO Read` anywhere in the graph.**

```
<any image or video generation node's IMAGE output> -> OCIO Write
  (profile = SDR Rec.709 delivery, container = video, video_codec = h264)
```
This is the most common real-world shape: most generation graphs never touch `OCIO Read` at all, because
there's no plate to read, only a render to deliver. `SDR Rec.709 delivery` sets
`sRGB - Display -> Rec.1886 Rec.709 - Display` for you (a transfer-curve change at the same primaries,
exactly "make this correct for a broadcast monitor") and leaves the container alone, so pick `video`
plus whichever codec your editor wants, or `still image`/`sequence` plus PNG or TIFF. Do not leave
`still_format` on `exr` with this profile: this pack always reads a `.exr` back as scene-linear ACEScg,
so a display-referred SDR image written into one would come back wrong if reloaded through `OCIO Read`.

**2. Write a ProRes for editorial from a graded plate.**

```
OCIO Read (source = an EXR sequence, ACEScg -> ACEScg)
  -> OCIO CDLTransform (the grade)
  -> OCIO Write
       (input_colorspace = ACEScg, output_colorspace = Rec.1886 Rec.709 - Display,
        container = video, video_codec = prores_422hq,
        first_frame/last_frame/start_number/source_start matching the Read's real frame numbers,
        source_meta wired from the Read, audio wired from LoadAudio if there's a temp mix)
```
Confirmed end to end on a real 3-frame test sequence: the written `.mov` reported `codec_name=prores`,
`pix_fmt=yuv422p10le`, `bits_per_raw_sample=10`, and `color_primaries=bt709`,
`color_transfer=bt709`, `color_space=bt709` under `ffprobe`, matching the `Rec.1886 Rec.709 - Display`
pick exactly. The identity metadata (camera, lens, reel, scene, shot, take) also showed up as real
QuickTime metadata tags on the same file, plus the Apple ProApps aliases (`com.apple.proapps.reel`,
`com.apple.proapps.cameraName`, and so on) that Resolve and Final Cut read natively.

**3. Write an EXR master that carries the plate's own metadata forward.**

```
OCIO Read (source = an EXR sequence) -> OCIO Write
  (source_meta wired from the Read's slot-5 output,
   container = sequence, still_format = exr, output_colorspace = ACEScg)
```
Confirmed end to end: the plate's `cameraModel`, `lensModel`, `reel_name`, `scene`, `shot` and `take`
attributes appeared verbatim in the written EXR's header, alongside this node's own authored
`chromaticities` (the AP1 primaries for ACEScg), `adoptedNeutral`, `colorInteropID`
(`lin_ap1_scene`, read directly off the OCIO config's own registered Color Interop Forum alias, not a
hand-built table in this pack) and `com.ocio.sourceFile`. A test attribute matching the "mastering
display" pixel-state filter was dropped from both the header and the sidecar, and reported as dropped in
both places. The `<name>.json` sidecar lists exactly which of the written attributes the EXR itself kept
(`container_keeps`) versus which only exist in the sidecar (`sidecar_only`); for an EXR that second list
is empty, because an EXR header can hold the entire attribute set.

**4. Pull one review still out of the middle of a graded sequence.**

```
OCIO Read (source = an EXR sequence) -> OCIO Write
  (container = still image, first_frame = <a frame number in the middle of the range>,
   still_format = png, source_start matching the Read's first frame)
```
Confirmed: grabbing frame 1002 out of a 3-frame batch produced `grab.1002.png`, not `grab.0001.png` or
a plain `grab.png`: the write() function stamps the *source* frame number into a still's filename
whenever the incoming batch has more than one frame, specifically so a client review frame is never
ambiguous about which frame of the sequence it actually is.

**5. Re-encode a native ComfyUI Video end to end, without ever converting it to a raw `IMAGE` batch.**

```
LoadVideo -> OCIO Write
  (video = LoadVideo's VIDEO output, container = video, video_codec = dnxhr_hqx,
   input_colorspace/output_colorspace set to match the clip's real colorspace)
```
Wiring a clip into the `video` socket (instead of `images`) renders the *whole* clip out using every
other Write setting, and a native `Video` input's own audio track rides along automatically unless
`write_audio` is turned off. This is the only supported way to feed `OCIO Write` a clip you never
decoded through `OCIO Read` in the first place.

### Which containers carry below-black and above-white

Scene-linear footage has values on both sides of the 0..1 window a viewable image occupies. Above 1 is
highlight: the sun, a specular, a bright reflection. **Below 0 is real data too** - film grain crosses
zero, a blue cast in a black corner is negative in the working space, and a saturated colour is
genuinely out of range in narrower primaries. Whether either tail survives is decided by the container,
not by the colour maths.

Measured through this node with `raw_data` on, so only the container was under test, writing values from
-1.5 to +20.0 and reading the files back off disk:

| container | below zero | above one |
| --- | --- | --- |
| **EXR 16f (half)** | survives, every value distinct, floor -1.5 intact | survives to +20.0 |
| **EXR 32f (float)** | survives, every value distinct | survives to +20.0 |
| **TIFF 32f** | survives, every value distinct, floor -1.5 intact | survives to +20.0 |
| TIFF 16 and TIFF 8 | **all of it floors to 0.000000** | **all of it caps at 1.0** |
| PNG 16 and PNG 8 | **all of it floors to 0.000000** | **all of it caps at 1.0** |
| JPEG | **all of it floors to 0.000000** | **all of it caps at 1.0** |
| every video codec | **floors** | **caps** |

**The line is float versus integer, not one format versus the rest.** TIFF is the format that sits on
both sides of it: at `32f` it stores IEEE floats and behaves exactly like EXR, while TIFF 16 and TIFF 8
round into an unsigned integer. Raising an integer TIFF from 8 to 16 bits buys precision inside 0..1 and
changes nothing at all outside it, because an unsigned integer has no code for a negative number - so
this is a property of the encoding rather than something a setting can fix. PNG and JPEG are unsigned at
every depth they offer. Video lands in a limited- or full-range YUV with the same result.

**If either tail matters to the delivery, the container is EXR at either depth, or TIFF at 32f.**

Because that loss is the only one in the pack that cannot be undone - it has already been written to
disk - `OCIO Write` reports it instead of letting it pass in silence. When an integer container actually
ate something, the node states how much and how far:

```
tiff 16 clipped 12.40% below black (down to -1.5000) and 3.11% above white (up to +20.0000) - EXR keeps both
```

It goes out on three channels, because no single one reaches everybody: the node's own status line, a
warning toast (the only one of the three that appears when the Vue node renderer is enabled, because
that renderer draws no on-canvas node text at all), and a `WARNING` in the server log, which is what
somebody driving `/prompt` from a script sees.

Nothing is reported when nothing was lost. A value within half a code value of the endpoint rounds to it
anyway and is not counted; an image already inside 0..1 is silent; EXR is silent whatever the data. The
figures describe the frames **actually written**: a still writes exactly one frame, so a tail sitting on
frame 3 of a batch is not reported for a still that wrote frame 1 - ask for frame 3 by number and it is.
A sequence and a movie are measured across every frame.

### Traps

> **`auto_range`, and the frame-number widgets it's supposed to fill in, do nothing once a prompt
> leaves the canvas.** `auto_range`, `source_start`, `first_frame`, `last_frame` and `start_number` are
> ordinary widgets read straight from the posted prompt JSON at execution time; the code that makes
> `auto_range` mean anything (`syncWriteFromUpstream` in `web/ocio_io.js`, which traces the graph back
> to an `OCIO Read` and copies its frame range) is JavaScript that only runs inside the browser canvas.
> Confirmed directly: posting a prompt with a 3-frame sequence numbered 1001-1003 through `OCIO Read`,
> wired straight into `OCIO Write` left at its pure defaults (`auto_range: true`, `first_frame: 1`,
> `last_frame: 0`, `start_number: 1`, `source_start: 1`), wrote files named `probe_out_acescg.0001.exr`
> through `.0003.exr`, not `.1001.exr` through `.1003.exr`. Setting `first_frame`/`last_frame`/
> `start_number`/`source_start` explicitly to 1001/1003/1001/1001 in the same prompt produced
> `probe_out_acescg.1001.exr` as expected. If you build or replay a prompt outside the canvas (a script,
> a saved apiformat export re-posted after changing the source), you must set these numbers yourself;
> `auto_range` being `true` in the JSON is not a promise that anything will be recomputed.

> **A repeat render to the same path can silently do nothing, and this is ComfyUI's own execution cache,
> not a bug in this node.** Posting the exact same `OCIO Write` inputs a second time is served entirely
> from cache: confirmed with a real file's mtime unchanged across a second, identical POST (the
> execution log listed the write node itself under `execution_cached`), and confirmed again that
> changing only `render_nonce` to a new string forced a real re-execution and a new mtime on the same
> path. The on-node **Render** button already does this for you; a script posting straight to `/prompt`
> and expecting a rewrite on every call needs to change something in the payload each time, most simply
> `render_nonce`.

> **`profile: "auto"` (and `"none"`, and `"Seedance 4K 10-bit"`) are no-ops on the server.** The upstream
> detection that makes `"auto"` resolve to a real preset (`resolveAutoProfile`/`findUpstreamSource` in
> `web/ocio_io.js`, which walks the graph looking for `LTXVHDRDecodePostprocess` or a `LoraLoader` with a
> telling filename) is front-end only. `write()`'s own `profile` handling has explicit branches for the
> five real preset names and nothing else; the comment in the source is direct about it: `"Seedance 4K
> 10-bit" and "none"/"auto": no backend mapping`. Confirmed live: posting `profile: "auto"` alongside
> explicit, mismatched `input_colorspace`/`output_colorspace` values executed exactly those values with
> no substitution at all. If you're driving this node from outside the canvas, set `input_colorspace`/
> `output_colorspace` (and, for the HDR presets, `still_format`/`bit_depth`) yourself; `profile` is
> convenience for a human clicking a dropdown, not a server-side feature.

> **`partial_execution_targets` can report success over an empty output folder, and there's nothing
> this node can do about it.** ComfyUI's `/prompt` endpoint accepts an optional top-level
> `partial_execution_targets` field: a list of node IDs. Any `OUTPUT_NODE` not in that list is dropped
> from the run before execution starts, with no error and no warning, and the job still reports
> `"success"` if at least one named output node ran. `OCIO Write` (and `OCIO Player`) are both
> `output_node: true`, so both are droppable this way, while `OCIO Read` (`output_node: false`) is
> never a target itself. Confirmed with a 3-way live test: the identical graph (an image generator into
> both `OCIO Write` and a `PreviewImage`) wrote its file with no `partial_execution_targets` field at
> all; wrote nothing at all, while still reporting `"success"`, when the field named only the
> `PreviewImage` node; and wrote the file again when the field named the `OCIO Write` node. A skipped
> node never runs, so it has no way to report its own absence. If a render reports success over an
> empty folder, check whether the request that queued it included this field, or queue the graph
> normally from the canvas instead of through a partial run.

> **`bit_depth` is not validated against `still_format` on the server.** The canvas narrows the visible
> `bit_depth` choices to what the current format supports, but `_save_still` itself only checks for
> exact matches (`if bit_depth == "16":` for PNG's 16-bit path, `if bit_depth == "32f":` /
> `elif bit_depth == "8":` for TIFF) and falls through to that format's most basic depth for anything
> else, silently. Confirmed: requesting `still_format: "png"` with `bit_depth: "32f"` (not a value the
> canvas would ever offer for PNG) produced an ordinary 8-bit PNG with no error at all. If you build
> prompts by hand, match `bit_depth` to `still_format` yourself: EXR takes `16f`/`32f`; TIFF takes
> `8`/`16`/`32f`; PNG takes `8`/`16`; JPEG is always 8-bit regardless of what you send.

> **Writing an HDR value into an 8-bit or `raw_data` path clips rather than errors.** A value above 1.0
> (a legitimate scene-linear highlight) written to an 8-bit integer format, or through `raw_data` mode
> generally, is clamped to `1.0` before quantizing, so it lands at pure white with no warning. Confirmed
> by arithmetic on a real write: a source pixel of `(0.3, 1.8, 0.05)` written raw as 8-bit PNG came back
> as `(76, 255, 13)`, exactly `round(0.3*255)`, `round(1.0*255)` (the clipped value, not `1.8*255`), and
> `round(0.05*255)`. Only EXR (`16f`/`32f`) and TIFF `32f` preserve a value above 1.0 without clipping.

> **`colorspace_in_name` exists because the alternative silently destroys renders, and turning it off
> brings that risk back.** A short, truncated tag cannot name a colorspace uniquely: 31 of the
> config's 55 collapse onto a tag shared with at least one other (every ARRI/BMD/DaVinci/RED
> gamut variant became `linear`, for instance). Two writes to the same folder differing only in
> `output_colorspace` produced the identical filename, and the second silently overwrote the first with
> no error. The tag is now the colorspace name spelled out in full (`ACEScg` -> `acescg`,
> `Rec.1886 Rec.709 - Display` -> `rec_1886_rec_709_display`), confirmed unique across the live config,
> at the cost of a longer filename. `raw_data` on forces the tag to the literal string `raw` regardless
> of `output_colorspace`. Turning `colorspace_in_name` off removes the tag entirely and brings the
> original collision risk back for anyone writing the same base name in more than one colorspace to the
> same folder; it is not a purely cosmetic toggle.

> **Not every `output_colorspace` gets authored chromaticities, and an EXR silently gets none rather
> than a wrong guess.** `_derive_chromaticities` only stamps `chromaticities`/`adoptedNeutral`/
> `colorInteropID` when the active config's own primaries for that colorspace land within a tight
> tolerance of a published anchor. The source names a real failure case directly: `Linear ARRI Wide
> Gamut 4` comes back roughly 1.5e-3 off the true AWG4 primaries through the only hub available, so it
> gets nothing rather than a number the code says it "does not have a hub that reproduces." `ACEScg`
> and `Rec.1886 Rec.709 - Display` both received the full set in testing; a less common camera-native
> gamut may not, and a missing `chromaticities` attribute on such a file is this safeguard working as
> intended, not a defect to report.

> **`images` and `video` are mutually exclusive, and `video` wins silently if both are wired.**
> `write()` unconditionally overwrites the `images` parameter whenever `video` is not `None`, with no
> check for whether `images` was also connected and no message if it was. If a graph somehow has both
> wired (an editing mistake while rewiring, for instance) the `images` wire is discarded without a trace
> in the output.

> **A workflow saved before `write_audio` existed loads the widget as `null`, not as a missing key, and
> that specific value is repaired rather than treated as false.** `widgets_values` is positional over every widget
> including the node's buttons, which serialize as `null`, so a graph saved before this widget existed posts
> `"write_audio": null` explicitly rather than omitting the key. `None` is not the same as `False`: the node checks
> for it explicitly (`if write_audio is None: write_audio = True`) and repairs it to the default rather
> than letting Python's usual falsy-value handling silently strip the audio out of every graph saved
> before this widget existed.

### Not verified in this pass

The full ten-codec bit-depth and container matrix in the `video_codec` table above is stated from the
pack's own measured comments in source (`_video_encoder_args`, each entry documented as measured against
a specific `ffmpeg` build by encoding and reading back with `ffprobe`); only `prores_422hq` was
independently re-measured here. The MXF OPAtom audio sidecar path (an OPAtom write with a
wired `AUDIO` track producing a `.wav` beside it, since ffmpeg refuses a second stream in that muxer) was
read in source but not executed live in this pass. The 16-bit PNG identity-chunk path (`_png_splice_text`,
which writes iTXt chunks directly before `IDAT` so both 8-bit and 16-bit PNGs now carry the same identity
set) was read in source; only the 8-bit PNG path was independently confirmed by writing and reading a
real file.

## OCIO Clip Repair

Not a color node - a compositing utility. It takes two IMAGE inputs, a `plate` and a
`reconstruction` (the output of any SDR-to-HDR pass over the same frames), and returns the plate with
the reconstruction composited into its clipped ends only. Outputs `(IMAGE, MASK, STRING)`: the result,
the repair mask, and a text report of what it did.

**Why it exists.** An SDR-to-HDR model rewrites the whole frame. Measured on one shot, applying the pass
full-frame shifted colour balance across the 97% that was never clipped (R/G 1.30 -> 1.70) and invented
72% more local contrast than the plate carried - detail the model made up, replacing the plate's own.
Compositing the pass through a mask of the clipped ends keeps tone, texture and frame-to-frame stability
everywhere the plate still held information, and takes recovered range only where the plate ran out.

**The two ends are not symmetrical, and the defaults say so.** Highlights genuinely reconstruct - a sun
that was a flat white patch comes back with a falloff. Shadows, on the same material, came back
*smoothed*: the plate's grainy-but-real texture replaced by a clean gradient, which reads as worse. So
`repair_shadows` is OFF by default.

**Thresholds are widgets, not constants.** Detail dies before a code reaches 1.0, so the highlight level
lives in the 0.90-0.99 band, and the right value is a property of the plate. Choose it by where the
reconstruction holds *more than the plate could*, not by where the plate happens to clip.

| widget | default | what it does |
|---|---|---|
| `repair_highlights` | True | rebuild blown highlights - the end that reconstructs well |
| `highlight_level` | 0.97 | repair above this display code |
| `repair_shadows` | False | off on purpose; the pass smooths blacks rather than restoring them |
| `shadow_level` | 0.010 | repair below this display code |
| `grow` | 6 | expand the mask outward, so repair starts just before the damage |
| `feather` | 24 | soften the mask edge so the composite reads no seam |
| `match_levels` | True | scale the reconstruction to the plate's mid-tones before compositing |
| `plate_space` | display codes | what the plate is in; a scene-linear plate from an 8-bit source peaks at 1.0 like a display one, so it cannot be auto-detected and must be set |
