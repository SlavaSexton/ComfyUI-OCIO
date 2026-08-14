# Changelog

## The timecode now comes from the plate, and the reader shows you what the plate says

**OCIO Write's `start_timecode` field is gone.** A code typed into the writer is a code invented at delivery.
The one that has to survive a round trip is the one that arrived with the footage, so the start is now read
from whatever you wire into `metadata` and advanced per written frame, into every EXR header and into a
movie's own timecode track. Nothing wired means no timecode written, which is the honest outcome. The field
never shipped in a release, so no saved workflow is holding a value where it used to sit.

Removing it exposed a defect on a real camera master. The set of fields this node re-authors for itself was
matched by exact spelling, and a DaVinci Resolve MXF calls its start code `timecode` where that set says
`timeCode`. The plate's value sailed past the strip, and the delivered EXR came out carrying **two**
timecodes: ours, correctly typed and advancing, beside a stale string frozen at the start. Whichever one a
downstream tool read first decided how the shot conformed. Matching now ignores case and separators.

**OCIO Read has a second disclosure button, `▾ Metadata`,** beside the viewer. It lists the file's own header
as plain text, one line per field, and a row with nothing to say is not drawn, so a bare EXR shows a few
lines and a camera master shows many. What it lists is the same read that travels down the `metadata` wire,
so the panel answers "what will be delivered" rather than offering a separate opinion. It used to fold away
with the viewer, which tied wanting to see the picture to wanting to read the header.

**A UMID is no longer reported as a reel name.** Some applications park one in the reel field: measured on a
ProRes 4444 XQ master, Resolve writes `com.apple.proapps.reel=0x060A2B34...`. That is a 32-octet SMPTE ST
330M identifier, and a reel name is 8 characters in a CMX3600 EDL or up to 32 on Avid, so it could not travel
as one even if you wanted it to. The value is not discarded; it is written to the output file under its own
attribute and simply refused the claim of being the shot's reel.

## EXR now defaults to DWAA, and a professional container is read as Rec.709

**`compression` defaults to `dwaa`** where it used to default to `zip`. DWAA is lossy and far smaller, which
suits a review or comp copy. A workflow you already saved keeps whatever it stored; the default only reaches
a node you create fresh.

Two consequences the node now reports rather than leaving you to find. DWA **quantises float32 to half before
compressing** - that is OpenEXR's own behaviour, stated in `ImfDwaCompressor` - so `32f` + DWAA writes half
precision under a header that still declares `float`. Measured: a 32f/zip file carried 49086 values no half
can represent, the same data at 32f/dwaa carried none, and distinct values collapsed from 49071 to 4765. And
DWA destroys data passes: a depth pass came back with a maximum error of 172 units, normals lost unit length,
an ID pass was unrecognisable. Pick `zip` for a 32-bit master and for any data pass.

**An SDR ProRes / DNxHD, or anything in an MXF, is now guessed as `Rec.1886 Rec.709 - Display`** instead of
`sRGB - Display`. The tags alone cannot separate a camera master from a web clip: a Resolve MXF of ProRes
4444 XQ reports `color_space=bt709` with primaries and transfer both `unknown`. The container and the codec
can, because nobody publishes an MXF to the web. Ordinary h264 / hevc keeps sRGB, which is what it was and
why. A log-encoded ProRes carries no tag saying so and is still guessed as Rec.709, so that one is yours to
set.

## Reading an EXR no longer depends on an environment variable

The metadata panel read EXR dimensions through OpenCV, whose EXR codec is disabled unless
`OPENCV_IO_ENABLE_OPENEXR` is set **before** cv2 is imported. When it is not, `cv2.imread` raises rather than
returning nothing, so the fallback beside it could never catch the failure and the whole panel came back as
an error. Writing and reading pixels had already been moved off cv2 for this reason; this path had not. It
now reads the header through the OpenEXR module, which needs no flag, and stopped pulling a 49 MB frame off
disk to learn its width. Reported independently as
[#4](https://github.com/SlavaSexton/ComfyUI-OCIO/issues/4) by
[@Sudhzpatil](https://github.com/Sudhzpatil), out of the source, before the fix shipped.

`tools/test_exr_read_no_envflag.py` now runs a fourth arrangement, cv2 not importable at all, which is how the
pack behaves on an OpenCV build with no EXR codec. The three existing cases all had cv2 present, so nothing
exercised the path where the OpenEXR module is the only reader. The case reports whether cv2 was really absent,
because otherwise a green result cannot be told from an import block that failed to take.

## OCIO VAE Encode and Decode refuse an input they cannot take, in words

A frame whose height or width is not a multiple of the VAE's block size used to die inside einops with
`Shape mismatch, can't divide axis of length 791 in chunks of 2`: an internal axis, at a size that is the
frame's height divided by four, with no mention of the frame. A 4-dimensional latent handed to a video VAE
died in the memory estimate with `IndexError: tuple index out of range`, which says nothing about latents.
Both now name the number that is wrong. Neither fixes any arithmetic: the stock ComfyUI nodes fail on exactly
the same inputs in exactly the same way, confirmed by running them. Both guards fail open, so a VAE that will
not say what it needs gets the input it would have got before.

## Colorspace filename tags are now spelled out in full

**This renames output files, once, and it is the only breaking change here.** The short tag was ambiguous: 31 of
the 55 colorspaces collided onto a name another colorspace already used, so two different deliveries could land
on `shot_rec709.mov` and the second silently replaced the first. Full names cannot collide, measured across all
55: `shot_rec709.mov` becomes `shot_rec_1886_rec_709_display.mov`, `shot_srgb.exr` becomes
`shot_srgb_display.exr`. `ACEScg` was already unambiguous and is unchanged at `_acescg`.

There is deliberately **no switch** to keep the old scheme. A permanent widget for a transitional problem is a
positional widget value forever, it doubles the filename surface every future test has to cover, and it leaves a
button whose only function is to re-enable silent overwriting. Nothing anywhere reads a tag back out of a
filename, so the cost is one rename on your side.

## Two new Avid options, and the depths are measured rather than quoted

`video_codec` gains `dnxhr_hqx` and `dnxhr_444`. Offering Avid only at 8-bit contradicted the point of the pack.
Every depth in the README table now comes from writing a file and reading it back with `ffprobe`, and one figure
changed as a result: **`dnxhr_444` is 10-bit here, not 12**. The DNx encoder ffmpeg ships advertises exactly
`yuv422p yuv422p10le yuv444p10le gbrp10le` and no 12-bit format at all, so 444 buys full chroma rather than more
bits. For 12-bit the route is ProRes 4444.

The README no longer states what those names mean as Avid *formats*, because Avid's own sources disagree: its
historical guide calls both HQX and 444 12-bit, while its current naming page says that after the 2025 revision of
ST 2019-1 the families are unified as Avid DNx with every level admitting 8 to 16 bits. The table describes the
files this pack writes and claims nothing beyond that.

## Metadata reaches every format, and a sidecar covers what no container can

The complaint was metadata being rubbed out. Every write now puts a `.json` beside the file, one per sequence
rather than one per frame, listing what the container kept and what only the sidecar holds. On top of that:

- **TIFF** carries the shot identity in real tags plus an XMP packet, and a duplicate `ImageDescription` bug is
  fixed. Passing `description=` while tifffile also wrote its own shaped JSON emitted tag 270 **twice**, and
  readers disagreed about which one was the file's colorspace.
- **PNG** carries it as `iTXt` chunks at **both** bit depths. 16-bit was the hard case, because OpenCV writes
  16-bit RGB and no text while Pillow writes text and cannot represent 16-bit RGB at all. The chunks are written
  directly, ahead of the first `IDAT` - and that position is the whole point: text after `IDAT` is legal PNG that
  OpenImageIO cannot see, because its reader takes text before the pixels. Verified with a control, and with
  `oiiotool` listing all seven fields.
- **MXF** is available for DNxHR, in OP1a and OPAtom. Colour tags survive with `range=tv`, better than ProRes in a
  MOV, and so does the timecode. Of the identity, only the reel name is a documented interchange field: ffmpeg
  writes it as the Physical Source Package Name, which is what Avid means by Tape Name. The rest travels as
  ffmpeg's own tagged values and is dependable in the sidecar.
- **`adoptedNeutral`** is now authored from the derived chromaticities instead of being stripped on a promise to
  re-author that was never kept. **`whiteLuminance`** is still stripped and deliberately not authored: it is an
  absolute display quantity nobody knows for a scene-linear ACES file, and OpenEXR does not type-check the name,
  so an inherited value can land as the wrong type for an attribute the specification defines as one float.
- **No delivered file carries the machine.** Absolute paths, UNC shares and ComfyUI's embedded `prompt` graph are
  withheld from every format including the sidecar, and the withheld keys are **named** rather than dropped in
  silence.

## OCIO VAE Decode and OCIO VAE Encode

Decode a latent without the `0..1` clamp, which the stock node applies unconditionally, and encode back with a
report when the input falls outside the range the stock path folds in silently.

The decode no longer rewrites the VAE's output transform unconditionally, which was a wrong-output bug: eleven
places in `comfy/sd.py` set that transform to the identity, five of them image decoders that already emit `0..1`
including the fast preview decoder for LTX2 itself, and rewriting it there turns a correct frame into a
washed-out one with no error. It now probes the transform, replaces only a shape it recognises, and reports
anything unfamiliar. Found by Andrei Orehov.

Also: spatial tiling on the decode, a range report as a second output, and float32 as an opt-in rather than a
default. Measured on 25 frames at 1280x704 with tiling at 384, float32 cost **5.1x** the model default (26.4 s
against 5.2 s) for a difference of at most 0.010467, about 2.7 steps of an 8-bit scale. Worth it for a
deliverable master, not for a look-see.

## Smaller things

- **`write_audio`** on OCIO Write, appended as the last widget, default on. It covers what the `audio` socket
  cannot: a native `Video` input brings its own track and there is no wire to disconnect in order to decline it.
- **`SDR Rec.709 delivery`** profile, the first display-referred preset: sRGB to Rec.1886 Rec.709 at unchanged
  primaries. It forces no format, so choose a video container or PNG / TIFF rather than leaving it on EXR.
- **The node's filename preview now agrees with the file it writes.** The extension was decided twice, once in
  Python and once by a name-prefix test in the front end, and the two disagreed for the MXF entries: the node
  previewed `.mov` while the write produced `.mxf`.
- **A hidden widget comes back.** Switching `container` to video and back left `compression` invisible while every
  flag said it was showing, because the layout restore was keyed on a value that is normally undefined. The node
  lost 176 pixels of controls across one round trip.
- **The overwrite warning cannot under-warn on a still image.** A still taken from a multi-frame batch gets its
  frame number stamped into the name, and the dialog used to check only the un-stamped one, so it never warned and
  a repeat render silently replaced the previous file.
- **A drop-frame timecode bug** at 29.97 and 59.94: the correction dropped frames on the wrong minutes. Also a
  raise on an illegal drop-frame label instead of writing a code that does not exist.
- **EXR reading** goes through the OpenEXR module first, with OpenCV as a caught fallback, so reading no longer
  depends on an environment variable set before ComfyUI started.
- `OpenEXR>=3.3` in both `requirements.txt` and `pyproject.toml`. The `OpenEXR.File` API this pack calls in four
  places arrived in 3.3.0; 3.2.x has only the older `InputFile` / `OutputFile` bindings.

## EXR write wrote nothing, and said it did

Every EXR write from OCIO Write was a silent no-op. The node reported `saved ocio_out_acescg.0001.exr`,
the count was right, the preview rendered - and the output folder stayed empty. Sequence or single frame,
16f or 32f, all of it.

The cause is not in this pack. OpenCV ships the OpenEXR codec compiled in but **disabled**: it registers
only when `OPENCV_IO_ENABLE_OPENEXR=1` is in the environment *before* `cv2` is imported
([opencv/opencv#21326](https://github.com/opencv/opencv/issues/21326), the gate added after the 2022 EXR
CVEs). No ComfyUI launcher sets it - not Desktop, not a plain `python main.py` - so `cv2.imwrite()` on an
`.exr` path either raises `-213 imgcodecs: OpenEXR codec is disabled` or returns without writing, depending
on the build. Setting the variable at runtime does not help, because the codec table is built at import
time and `cv2` is usually already imported by another node pack.

`_save_still` now writes EXR through the **OpenEXR python module**, which has no such gate, and keeps the
cv2 path as a fallback for installs that do export the variable. That fallback now also verifies the file
exists and is non-empty before returning, so a failed write can never again be reported as a success.

`OpenEXR>=3.3` joins the dependencies (3.3 is where the `OpenEXR.File` API landed). Alpha is written as a
separate `A` channel, and `bit_depth` still selects half (`16f`) or float (`32f`).

Verified on a 121-frame LTX-2.5 render at 1280x704: 121 files on disk, `float32`, values preserved exactly
including the out-of-range ones a video codec would clip (`min=-0.046`, `max=+1.056`), round-trip through
`OpenEXR.File` bit-identical to the tensor that went in.

## Follow-ups to 1.2.5

Three things 1.2.5 got wrong, found by re-reviewing it, plus documentation that was describing buttons and
outputs the nodes do not have.

### An empty source was only half-refused

1.2.5 rejected an empty `source` by looking at the string, so `.`, `./` or the path of a folder walked
straight past the guard and produced the same fiction: every numbered file in that folder reported as one clip
spanning the lowest to the highest number, everything in between counted as missing. On the ComfyUI input
folder that was 2015 frames with 1980 of them "missing".

The scan now groups a folder's files by frame pattern, the way it already did when you pick one frame of a
sequence, and answers with the sequence that is actually in there. A real sequence folder is unaffected.

### OCIO Player kept a trim across a change of clip

The 1.2.5 rule was "keep the in/out if it still fits", and a trim of 10..50 fits any clip of 50 frames or
more, so it followed the artist onto the next video. The stream path now keeps a trim only while the clip
itself is unchanged, and a trim restored from a workflow is still honoured.

### A fresh OCIO Read showed a VIDEO output it should not

Cosmetic, introduced by 1.2.5: with no source there is no video, and the slot is hidden again. A connected
slot is never removed.

### Documentation that did not match the nodes

- Two tooltips still told you to use a "browse button" that has not existed for some time. They now name
  **Open Files** and **Output Folder**, which are the buttons on the nodes.
- The same stale label with an emoji survived in the README's OCIO Write section.
- OCIO Player's docstring and five of its tooltips described outputs, a conversion and a trim applied to
  them. The node returns nothing and never has: those settings drive the viewer. Its frame fields were also
  documented as 0-based batch indices while they hold source frame numbers, which is what the timeline shows.
- `read_meta` now trims whitespace around a path like the other two entry points already did.
- The workflow file's header still described the Registry push trigger as live while the block below it
  explains that it is paused.

### Installing it from ComfyUI Manager

In Manager's **Select Version** dialog pick **Nightly**, which tracks this repository and is what Manager
offers by default. The numbered entries come from the Comfy Registry, which still holds only 1.2.3 and 1.2.4
because publishing is paused while those sit flagged, so choosing `Latest` or a number there installs code
from before these fixes.

## Values you set by hand stay set

Reported as issue #3: OCIO Read re-detected the source during ordinary graph work and put the detected
defaults back over widgets that had been edited. Opening a saved workflow was enough to lose a trimmed
frame range, a conformed fps, or a deliberately chosen input colorspace - silently, with a valid render
and wrong color.

The same class turned out to be present in the neighbouring nodes, so all of it is fixed here.

### OCIO Read

Detected metadata is now applied only when the source actually changes, or when you press the new
**Detect from Source** button. Loading a workflow, pasting a node and undoing still re-derive everything
the workflow does not store - the detected kind, the per-kind widget visibility, the preview - but they no
longer touch a value.

A scan that finishes late no longer lands on top of an edit made while it was running: the fields you
touched keep your values and the rest take the detected ones, so the node cannot end up holding a range
from one clip and a colorspace from another.

**Detect from Source** sits directly under the source parameters it re-reads, separated from the file and
viewer buttons. Use it when you have edited the fields and want the file's own numbers back.

### OCIO Write

A deliberate `output_colorspace` is no longer reset to the container default when a workflow is opened,
and the `profile` that a colorspace write used to clear along with it now survives too. That one mattered:
losing a log profile sends log-encoded frames into the conversion as if they were linear.

Editing `fps` by hand now turns `auto_range` off, which is what its tooltip has always said it does. Before
this, a hand-set fps was pulled back to the source fps on the next sync.

### OCIO Player

A saved in/out trim is no longer replaced by the full clip on the first render after loading a workflow.
Switching to a genuinely different clip still snaps the range to the new one.

### Empty source

An empty `source` resolved to the ComfyUI input folder, and the sequence scan then reported every numbered
file sitting in it as one invented clip - thousands of frames, nearly all of them "missing". Both the
detect endpoint and the loader now say what is wrong instead.

### No change to the color code

`nodes.py` and `grade_nodes.py` are untouched, and in `io_nodes.py` the diff is two guards on an empty source
plus tooltip wording. No transform, curve, LUT path or conversion was edited, so the accuracy suite has
nothing new to measure and was not re-run for this version.

Worth stating plainly, because it cuts the other way too: the same saved workflow can render differently than
it did under 1.2.4, since the values it feeds the transforms are now the ones you set rather than the ones a
re-detect put back.

### About the Registry

Automatic publishing to the Comfy Registry is paused for this release, so 1.2.5 is a GitHub release only.
Versions 1.2.3 and 1.2.4 are both sitting `Flagged` there on false positives from the automated scan, which
leaves ComfyUI-Manager with no installable version and makes it fall back to a git clone of this repository -
which is how you get this fix. Publishing another version while the flag stands would only add a third
flagged row. It will be re-enabled once the review clears.

## Re-publish after the Registry flagged 1.2.3

**No node behavior changes.** The Python AST of every node file is identical to 1.2.3 apart from a single
string constant, and the JavaScript is untouched.

### Why this version exists

1.2.3 uploaded cleanly and then came back from the Registry's automated scan as `Flagged`, with no reason
given in the API and none shown on the publisher page. The pack was audited against all three published
security standards and violates none of them: zero `eval` or `exec` calls in any shipped file, no runtime
package installation, no obfuscation. The `subprocess` calls the scanner can see are `ffmpeg` and `ffprobe`,
which is the pack decoding and encoding video, its actual job.

A pack with the same symptom has an issue open on the Registry since 2026-05-13 with no reply, yet its
publisher kept releasing and today has 88 active versions beside 20 flagged ones. That suggests the flag
lands per version rather than per pack, and that it is not always deterministic. This release tests that
reading: same code, new number.

### Changed

- Two strings in `nodes.py` no longer spell out a `pip install` command: a comment in the header and the
  `RuntimeError` raised when OpenColorIO is absent. The message still tells the user exactly what to
  install, it just names the package instead of quoting a shell command, on the theory that a scanner
  weighting `subprocess` in one file against `pip install` in another may be reading them together. That is
  a hypothesis about the scanner, not a diagnosis, and it is recorded as one.
- The README keeps its installation commands. Stripping install instructions out of a README to appease a
  scanner would cost a real user more than the flag costs.
- The licence is declared by name, `{ text = "MIT License" }`, so the Registry page shows a licence instead
  of a raw table. The licence itself is unchanged: MIT, and the copyright notice still travels with every
  copy.

## Code polish, and a compatibility check against current ComfyUI

A polish release. **No node behavior changes**: the Python AST of every node file and the JavaScript with
comments stripped are byte-identical to v1.2.2, so nothing that runs has moved.

### Verified

- **Compatible with ComfyUI v0.30.0**, the current release at the time of writing, checked by loading the pack
  against a clean v0.30.0 checkout rather than by reading release notes: all **9 nodes register**, and every
  ComfyUI API this pack depends on is present with a matching signature. `comfy_api.latest` still exports
  `InputImpl` and `Types`; `VideoFromComponents(components, bit_depth=8)` is unchanged; `VideoComponents` still
  carries `images`, `frame_rate` and `audio`, with `metadata` and `alpha` added alongside them, so the additions
  are additive rather than breaking. `folder_paths` and `server` import clean.
- **Still running on ComfyUI 0.25.1**, confirmed on a live server: `/object_info` lists all 9 OCIO nodes and the
  boot log carries no import error from this pack.
- Honest limit: both checks cover import and node registration. Neither runs a full render on v0.30.0, and the
  front-end JavaScript was not exercised against frontend 1.47.11 (v0.30.0's pin) as opposed to 1.45.15. The
  extension hooks this pack uses are unchanged in the frontend releases across that range.

### Changed

- Code comments now explain decisions by what the code does rather than by how the decision was arrived at.
  Every technical fact is kept: 1-based video frame numbering, the 23.976 default for stills, why an ordinary
  bt709 mp4 is treated as an internet deliverable, why the Player looked flat without `allow_shaper`, and the
  warning against mutating links while a graph is loading.
- Removed a shorthand task taxonomy that read like a code convention but resolved to nothing in this repository.
- `tools/accuracy/gen_fixtures.py` reads the EXR sequence path from `OCIO_ACCURACY_EXR_SEQ` instead of a
  hardcoded absolute path, so the accuracy suite runs anywhere rather than only on one machine.
- `tools/accuracy/measure_ocio_parity.py` resolves `nyc_skyline.png` from its own location. That image ships in
  this repository, so the script now works on a fresh clone.
- The two README screenshots have their path fields redacted. The graphs, nodes and viewer frames are unchanged.

## Corrected accuracy wording (round-trip vs half-float)

A docs-only patch: no node or test changes. Fixes an inaccurate line in the v1.2.1 accuracy write-up.

### Fixed
- The round-trip was described as sitting "above half-float EXR storage precision, so lossless for any real
  delivery." That is wrong: half-float is 16 bits and the `ACEScg -> LogC -> Rec.709 -> back` round-trip
  resolves ~14.6, so it is neither above half-float nor lossless. Corrected after Sam Hodge's review on PR #1:
  light is non-negative, so half-float's sign bit is unused and its usable range is ~15 bits; the round-trip
  holds ~14.6 of them (a sub-half-bit shortfall), and the per-pixel error (4.5e-6, ~2^-17.8) is ~100x finer than
  one half-float step near 1.0 (~2^-11), so a 16f EXR delivery never resolves it. Not bit-for-bit lossless;
  small enough not to matter for delivery. Also softened the `ocio_names` `srgb_linear` docstring ("losslessly"
  -> "without clipping, invertible to float precision ~1e-7, not bit-exact").

## Independently-verified accuracy and a reproducible test harness

A patch release: no node behavior changes. Adds a reproducible, cross-platform test environment and CI that
verify the color math end-to-end through a real ComfyUI, plus an honest accuracy write-up.

### Added
- **Dockerized, CPU-only ComfyUI test environment** (`docker/`, `docker-compose.yml`): builds native arm64 on
  Apple Silicon and amd64 in CI, installs the pack, and drives the nodes headless through the real ComfyUI HTTP
  API. No GPU, no model downloads. (PR #1, Sam Hodge.)
- **End-to-end round-trip color-accuracy test** (`docker/roundtrip_test.py`): round-trips the Kodak "Marcie" EXR
  `ACEScg -> ARRI LogC -> Rec.709 -> back` in raw 32-bit float and compares output vs input. Gates on the
  genuinely-invertible chain (max abs error 4.5e-6, reversible to floating-point precision) and reports the
  display-range encoding chains as informational (they clip HDR by design).
- **GitHub Actions CI** (`.github/workflows/docker-tests.yml`): builds the image and runs the standalone tests +
  node-registration smoke + the round-trip on every push and PR.
- `docs/DOCKER.md`: the test-environment and round-trip design.

### Changed
- **Honest accuracy story in the README.** The per-transform bit-exact parity (0.000e+00) is stated as the
  accuracy number; the end-to-end round-trip figure is added (4.5e-6 max, the residual being OCIO's
  single-precision LUT interpolation - not bit-for-bit lossless, but ~100x finer than one half-float EXR step
  near 1.0, i.e. below the storage grid a half-float delivery resolves); `histogram_compare.png` is
  recaptioned as a distribution shape sanity-check, not an accuracy proof.

### Verified
- CI-confirmed on every commit: all 9 OCIO nodes register in ComfyUI; every standalone test passes; the gated
  round-trip chain returns to source at 4.5e-6 max / 3.1e-8 mean absolute pixel error.

## Native ComfyUI VIDEO pipeline

A big feature release: the color nodes, Read, Write and Player now live on ComfyUI's native video wire.

### Added
- Native ComfyUI **VIDEO** pipeline on all nine nodes. The six color nodes (ColorSpace, LogConvert, Display,
  CDLTransform, FileTransform, LookTransform) each carry a mutually-exclusive IMAGE vs VIDEO input pair; only
  the socket matching the live input carries data, so a VIDEO in gives a VIDEO out and an IMAGE in gives an
  IMAGE out. **OCIO Write** and **OCIO Player** gained a VIDEO input, mutually exclusive with their image input.
- VIDEO in / VIDEO out using ComfyUI's native `comfy_api` `VideoFromComponents`, the same type Load Video emits,
  so the nodes interoperate with Load Video, Save Video, Video Combine, Get Video Components and VHS.
- **OCIO Write** records a native ComfyUI video to disk with all of its settings (container / codec / colorspace
  / bit depth), inheriting the clip's frame rate.
- **OCIO Read** exposes a VIDEO output that feeds ComfyUI-native video nodes downstream.
- Mutually-exclusive Image / Video sockets on all nine nodes: connect one input and the other auto-disconnects.
- LogConvert curves now carry readable labels (for example "Sony S-Log3", "ARRI LogC3", "Linear to Log").
- A scene-linear HDR log shaper on the OCIO Player viewport, so highlights above 1.0 don't clip in the display LUT.
- A new 3D gamut-volume chart (`docs/assets/accuracy/gamut_volume_3d.png`) comparing sRGB, ACEScg, ACES2065-1
  and ARRI Wide Gamut 3 as hulls in CIE Lab, via OCIO's own colorspace conversions.
- **OCIO Player** is now documented in the README for the first time (nine nodes, not eight).

### Fixed
- Page reload wiped all node connections (the dual-socket auto-disconnect fired during graph load and corrupted
  the link map).
- A native `Load Video -> OCIO color node -> Player` chain ignored the color nodes and showed the raw frame.
- OCIO Player's own Image / Video inputs did not auto-disconnect each other (it was missing from the
  mutual-exclusion list the other nodes already had).
- OCIO Player showed processed video flat / washed-out.
- OCIO Write video preview failed with "Invalid URL" (now a small, servable, playing H.264 preview).
- Fast video decode, 12-25x quicker than before (temp-file read instead of a subprocess pipe).

### Changed
- Slot labels: the IMAGE socket reads "OCIO Img/Seq/Vid", the VIDEO socket reads "ComfyUI Video", across the nodes.
- README refresh: node count corrected to nine, new node screenshots, a "Color accuracy, measured" section.

### Verified
- Bit-exact OCIO parity (worst max-abs error 0.000e+00 across 9 transforms x 4 fixtures) plus the full
  color-accuracy suite (`tools/accuracy`); charts in `docs/assets/accuracy/`.
