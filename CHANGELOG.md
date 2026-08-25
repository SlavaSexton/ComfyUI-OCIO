# Changelog

## 1.3.3: the ACEScct route into LTX-2.5, which ComfyUI has no other way to reach

LTX-2.5 has a native HDR path. It takes EXR in, works in ACEScct, and writes EXR out plus a
BT.2020 HLG master. The flag that turns it on lives in Lightricks' own command line tool,
`--hdr {SRGB_LINEAR,ACESCG,ACESCCT}`, and there is no route to it from ComfyUI: searched on
2026-08-25, the official Comfy template library holds **1030 workflows and none of them
mentions ACEScct**, while every LTX template in it has no colour management of any kind.

What this release adds is the two ends of that path. The middle is their tool.

    OCIO Read -> OCIO ColorSpace to ACEScg -> OCIO LogConvert, Linear to Log on ACEScct
       [ their CLI ]
    OCIO LogConvert, Log to Linear on ACEScct -> OCIO Write

### Why the curve matters

At code 1.0 ACEScct reaches **222.9** in linear scene units. The ARRI LogC3 curve that the
LTX-2.3 HDR IC-LoRA path is fixed to reaches **55.1**, four times less. That is the whole
reason to care which of the two paths a shot goes down.

Whether a given shot needs the difference is a separate question, and on the one measured here
it did not: the frames top out at code 0.906 with no sample at the ceiling. A bright shot is the
test of the extra range and a dark neon portrait is not.

### That it works, measured

The plate encoded to ACEScct had a code median of **0.3295** and came back from the model at
**0.3298**, so the level is preserved rather than drifting. Decoded, the output peaks at
**71.38** linear. `example_workflows/acescct_native/` holds the plate, two frames of the output,
a ProRes review and the numbers behind each.

The encoder was checked against the published constants before any of that: linear 0.18 gives
code 0.413588 and linear 0.0 gives 0.0729055, and the delivered plate round-trips back to the
linear original with a worst relative error of 3.7e-05.

### New

`example_workflows/OCIO_Example_LTX25_native_ACEScct.json` builds that shape inside ComfyUI, with
a note listing which weights each side needs. They are not the same files: ComfyUI loads the INT8
`convrot` pair at about 34 GiB and their tool cannot read those at all, wanting the bf16 pair at
about 64 GiB.

`docs/assets/ltx25_acescct_pipeline.svg` draws the route, with the ceilings and the weights on it.

### One failure worth recording

The command line half is not part of this pack and is not supported here, but on Windows it
fails most of the time for a reason that has nothing to do with the GPU. Safetensors' default
mmap backend takes a commit charge the size of the whole checkpoint at open, before a tensor is
read: measured on the 39.13 GiB transformer, available commit fell from 147.23 to 106.42 GiB
while free physical memory moved half a gigabyte. When the commit limit runs out the process
dies as a `RuntimeError` about an invalid python storage, as a bare segfault, or as
`OSError 1455`. Passing `backend="pread"` to `safe_open` charges nothing and the run completes.

## 1.3.2: an exposure node, and a reference for the two nodes that did not have one

### OCIO Exposure

Exposure in **stops**, as a pure linear multiply: `out = in * 2**exposure` on RGB, alpha passed through,
`mix` to blend. It ships at 0.0, which is a pass-through.

The pack had no exposure control at all, which is a gap in a colour pack, and every substitute checked
clamps to 0..1 either by default or behind a toggle. A clamp there destroys what the rest of this pack
exists to carry: the values above 1.0 an HDR pass produces and the negatives an unclamped VAE decode
leaves behind. Nothing here is clamped at either end, and `tools/test_exposure.py` holds it to that with
a control showing a clamped result would fail the same checks.

The case it was added for is matching an SDR-to-HDR pass to its plate. The pass re-renders into its own
tonal distribution and comes back below the plate, so the two cannot be compared until that is matched.
Measured on six shots, the lift ran from **0.55 to 1.58 stops** - one number per shot, not transferable.
Inside a shot it holds where the framing holds, varying 3.0% and 4.8% on two such clips, and it does not
where the camera travels onto a different subject, varying 45.3% on a pan and 30.2% on a dolly and tilt.

### Documentation for OCIO Clip Repair

Clip Repair shipped in 1.3.1 with no reference anywhere. `docs/NODES_REPAIR.md` is that reference: every
widget, the band table that decides `highlight_level`, the two wiring mistakes that produce a plausible
but wrong composite, and why it is restoration rather than conversion.

The part worth reading before using it: do not choose the level by comparing the reconstruction to 1.0.
At code 0.85 the plate holds about 0.73, so a pass sitting at 1.81 there is 2.5x the plate. Rim-lit cloud
on the measured shot sits at code 0.83, so a level of 0.90 or 0.97 leaves it untouched and still flat.

`OCIO Exposure` is documented in `docs/NODES_COLOR.md`, which now covers seven operators.

### Where the LTX-2.3 highlight range actually stops

A new README section, because the question came up on every flat highlight. The path is fixed to ARRI
LogC3, whose ceiling is 55.1 linear against 222.9 for ACEScct and 469.8 for LogC4. That looks like the
limit and it is not: the decode clamps codes to 1.0, and the model emits above that. Removing the clamp
took one frame's peak from **55.08 to 197.12**, recovering 1.84 stops, and cost nothing in stability - the
same 49 frames stepped 0.81% with the clamp and 0.82% without.

What is left is the model. On a dragon breathing fire the pass peaks at 43.3 across all 121 frames with no
pixel near the ceiling: not out of range, just not putting structure there.

### A test that keeps the node list honest

`tools/test_node_inventory.py` compares one source of truth, `NODE_CLASS_MAPPINGS`, against every place a
reader or a gate looks: the display-name map, the container smoke test's list of names, the README, the
wiring map, the group references, and the count stated in three files. The smoke test's list is the one
that matters, because it is how that test decides the pack loaded.

### Example workflows

Two graphs for the two-stage LTX path, `OCIO_Example_Generate_then_HDR.json` and
`OCIO_STAGE2_SDR_to_HDR.json`, with six exposure-ramp comparisons, the first and last frame of each
sequence as ACEScg EXR, and the two ProRes reviews.

## 1.3.1: security fix in the upload route

**Affected: every release up to and including 1.3.0. Update to 1.3.1.**

`/ocio/upload` built its destination by joining a caller-supplied `subfolder` onto the ComfyUI input
directory, after passing it through `os.path.basename`. That call strips a directory component but
leaves a relative segment intact, so a crafted value could resolve above the input directory. The
route is unauthenticated, and ComfyUI's local server carries no CSRF protection, so it was reachable
from the browser on the same machine. The Comfy Registry classified this as an arbitrary file write
during review and banned 1.3.0; the same code path is present in every earlier release.

1.3.1 resolves the destination before writing and refuses anything that does not land inside the
input directory, the approach ComfyUI core takes in `server.py`. Resolution goes through `realpath`,
so a symlink inside `input/` that points elsewhere is refused as well, and a comparison that cannot
be made at all is treated as outside. The destination directory is created only after the check
passes, so a refused upload leaves nothing behind.

Covered by `tools/test_upload_confinement.py`, which exercises the guard directly and asserts both
sides: traversal is refused, and an ordinary upload still succeeds.

The read routes are unchanged. They take a path because the disk browser in `OCIO Read` depends on
it, and narrowing that is a separate change with its own trade-off rather than a side effect of this
fix.

## OCIO Clip Repair

A compositing utility rather than a color node, and the first node here that is not one. It takes a
plate and an SDR-to-HDR reconstruction of the same frames, and composites the reconstruction into the
plate's clipped highlights only, leaving everything that was never damaged as the plate had it.

The reason is what a reconstruction pass does to the rest of the frame. Measured on one shot, applied
full-frame it moved colour balance across the 97% that never clipped (R/G 1.30 to 1.70) and produced 72%
more local contrast than the plate carried, which is detail the model invented in place of the plate's
own. Masking the pass to the clipped ends keeps tone, texture and frame-to-frame stability everywhere
the plate still held information.

The two ends are not symmetrical and the defaults say so. Highlights come back with a falloff where
there had been a flat white patch. Shadows, on the same material, came back smoothed: real grain
replaced by a clean gradient, which reads worse than the damage. So `repair_shadows` is off by default.

Thresholds are widgets because detail dies before a code reaches 1.0, and where it dies belongs to the
plate rather than to a constant. `plate_space` is set by hand for the same kind of reason: a
scene-linear plate that came from an 8-bit source peaks at 1.0 exactly like a display-referred one, so
nothing in the pixels can tell them apart.

## What the stock path costs, shown on one clip

Four side-by-side frames in `docs/assets/comparison/`, and a section in the README built around them: the same
LTX-2.5 generation written through ComfyUI's own LTX-2.5 template and through this pack. The model is
identical in both. What differs is what survives the trip out of it.

With the shadows lifted, the stock result carries coloured noise and banding through the hair, the neck and
the shaded armour. In the highlights, neon breaks into coloured fringes, and a light source on water smears
into artefacts with its reflection gone. Through this pack the tone holds, the glow stays whole and the
reflection reads as separate ripples.

The claim the pictures support is a narrow one, and it is the honest one: **the model gives more than the
stock path can carry.** That path puts the picture through 8 bits and clips it at `0..1`. Here the decode
runs in float32 with no clamp and the result lands in a 32-bit float EXR and a 12-bit ProRes 4444, never
passing through 8 bits. Nothing in those frames was recovered afterwards - it was never thrown away.

## OCIO Player says whether there is anything above white before you go looking for it

New `Range check` line in the Info panel: `max 1.017, min -0.008, 0.070% above 1.0`, or
`nothing above 1.0 (no highlights to recover)`.

It answers a question the viewer could not previously distinguish from a fault. Pull exposure down on a
display-referred master and the picture darkens and reveals nothing, which looks exactly like a broken
viewer. It is not: that container's ceiling IS white, and the writer clips there, so the highlights were
gone before the file existed. Measured on a real Rec.709 ProRes from this pack's own writer, reading native
YUV codes with no RGB stage anywhere so nothing could clamp the measurement: the brightest luma across 121
frames is 60304 against a white point of 60160, and 136 samples out of 109 035 520 sit above white, which is
0.000125%. There was nothing to recover.

The same line on a scene-linear batch reports numbers in the thousands of percent, which is the point: it
tells "this material has range" apart from "this viewer is broken" without a measurement.

## The view fills itself in with ACES 1.3, and the sound toggle stops asking questions it has already answered

**`view` now auto-fills with the ACES 1.3 transform** where that config has one for the resolved display,
instead of the loaded config's own 2.0 default. Which version renders is a statement about who opens the
file rather than about which standard is newer: a Nuke 13 or 14 comp is on ACES 1.2 / 1.3, and in an ACES
project Nuke's Read applies the inverse Output Transform on the way in while the viewer applies the forward
one, so a render has to have been made by the version that comp will view it through. Rendered with 2.0 and
viewed through 1.3, the worst pixel of a real Rec.709 master is off by 35.5%, and nothing in the file says
why. The 2.0 entries are one click away in the same list for anyone delivering into a 2.0 pipeline.

It still falls back to the loaded config's default when the 1.3 config cannot do the job - when it has no
view for that display, or does not know the scene colorspace at all (`D-Log D-Gamut`, `Linear D-Gamut`).

**`write_audio` appears only when sound arrives without a wire.** It was drawn on every OCIO Write, and
almost everywhere the wiring had already answered it: nothing on `audio` writes no sound because there is
none to write, and something on `audio` is a deliberate act with a wire to pull. A toggle that can only hold
the answer it already has is a row of noise on every write node in a graph.

It is hidden rather than removed, because "wire sound and it gets written, wire none and it does not" is the
obvious reading and misses one real case: a native ComfyUI `VIDEO` carries its own track INSIDE the object,
so connecting a movie hands the writer sound that no wire represents. That is what this toggle is for, and
it holds for a sequence as well, where the track lands beside the frames as a sidecar `.wav`. Wire an
explicit `audio` alongside a `video` and the wire wins in the writer, so the toggle steps back out.

## OCIO Write's `from_colorspace` is now `input_colorspace`

Three nodes were asking the same question in two vocabularies: `OCIO Read` and `OCIO Player` took
`input_colorspace`, `OCIO Write` took `from_colorspace`. One word for one idea now.

**A workflow saved from the canvas is unaffected.** Its `widgets_values` is a positional array with no field
names in it, so what matters is the slot, and the input keeps the same one, second in `required`. Checked
against a real graph rather than assumed. `tools/test_input_colorspace_rename.py` asserts the slot, so a later
edit cannot quietly move it and shift every widget after it by one.

**A workflow saved in API format with the old key will be refused, and the fix is one word.** ComfyUI checks
that every required key is present before a node runs, and answers a missing one with HTTP 400
`required_input_missing` naming the key. Both ways around it were measured and both cost more than they save:
a `VALIDATE_INPUTS` naming the old spelling does not lift that check, and moving the input to `optional` to
make it liftable would push every widget after it one slot along, corrupting the canvas workflows that
currently survive untouched. So rename `from_colorspace` to `input_colorspace` in the JSON.

`write()` itself still accepts the old keyword, for the callers that reach it directly rather than through a
prompt: this repo's own `tools/` scripts, `docker/`, and anything driving the node from Python.

## The view list narrows to what a pair can render, the thumbnail stops clamping, and the Player gets a soundtrack

**`view` now offers only what the resolved display actually has.** The combo is built once, when the node is
registered, as the union of every view across every display of two configs, so most of what it offered was
wrong for any given pair. Measured on the configs loaded by default: of the 32 real entries, **24 are invalid
for `Rec.1886 Rec.709 - Display`**, and every one of the nine displays has at least four it cannot use.
Picking one of them is not a soft miss. It raises at render time, after the graph has already spent its time.

The trap became ordinary rather than exotic when the ACES 1.3 entries arrived in the previous release.
`ACES 1.1 - SDR Video (Rec.709 lim)` reads like the obvious choice for a Rec.709 deliverable, and it lives on
`Rec.1886 Rec.2020 - Display`, a display the ACES 2.0 config does not have at all.

The list is narrowed live, as soon as both colorspaces are picked, and it **never changes your pick**. A view
restored from a saved workflow stays selected even when the pair cannot use it, with the widget's label saying
so, because silently swapping a rendering transform would change the picture a finished graph produces.

A second way a pair can be impossible is fixed with it, and it is not about the view at all: the two configs
do not hold the same colorspaces. `D-Log D-Gamut` and `Linear D-Gamut` are in the ACES 2.0 config and not in
the 1.3 one, so an `ACES 1.3: ` view with either of them on the scene side could not be built even though the
view was perfectly valid for the display. That used to surface as OCIO's own `Cannot find source color space`
from inside the processor build, with no mention of the view that caused it. Those entries are now dropped
from the list, and reaching the render with one names the colorspace instead.

**The thumbnail route decoded video differently from the player, and now does not.** `_read_video` moved to
planar YUV with the matrix done in float32 last release, because asking ffmpeg for an RGB pixel format makes
ffmpeg do the matrix into an unsigned integer container, which clamps. `_read_video_frame`, behind
`/ocio/thumb`, was left asking for `rgb48le`, so one file gave the viewport one picture and the thumbnail
another.

Measured on a flat limited-range frame (Y 16, Cb 240, Cr 128, BT.709): the file carries green at **-0.0937**
and the thumbnail decode returned exactly **0.0**. On a real ProRes 4444 written by this pack's own writer the
two paths disagreed on 0.30% of samples. The clip that turns a float picture into an 8-bit PNG still happens,
but now at the end, after the colorspace conversion, rather than before it.

**OCIO Player takes an `AUDIO` input.** Its float path shows half-float frames off disk, and a batch of frames
has no sound in it, so the L/R meters had nothing to read whatever the graph had generated upstream. The track
is written beside the frames and decoded in the browser, and the meters read it through the same graph the
`<video>` path uses.

The track is cut to the frames the viewer CACHED rather than to its own length. The cache stops at 240 frames
and the transport is built from what it holds, so a full-length track against a truncated picture drifts by
exactly the frames that were dropped, silently. Two limits are deliberate and worth stating: reverse plays
silent, because a decoded buffer has no negative rate, and the meters fall to zero with it; and a malformed
`AUDIO` does not stop the picture, unlike everywhere else in this pack, because a viewer that refuses to show
frames over a bad track is the worse trade. The problem is named in the node's report instead.

While the meters were being wired up it turned out they had never drawn on this node at all, in either mode:
the one call that draws them lived in OCIO Read's animation loop, and neither of the Player's two loops made
it. Both do now, so a streamed movie meters as well.

Three test files are added, and two of them run the front-end JavaScript rather than reading it:
`tools/test_view_narrowing.py` lifts the narrowing function out of `web/ocio_io.js` and executes it under node
against the real config data, then puts every entry it keeps through the real conversion;
`tools/test_player_audio.py` does the same with the clock that keeps the soundtrack on the picture, including
the loop case, where the frame clock wraps by modulo and never re-anchors itself; and
`tools/test_thumb_no_clamp.py` builds its own lossless fixture and checks the codes came back off the disk
unchanged before believing a single number it measures.

**`tools/check_deploy_sync.py` can now fix the drift it reports**, with `--apply`. It answered "these two
copies differ" and left the copying to whoever read it, which on a dev box is a hand-copy per edit and a
question of which files were missed. It copies only: files that exist just in the install are still reported
and never removed, because this script cannot tell a stale leftover from something somebody put there on
purpose. Files written but not yet `git add`ed are copied too, which the comparison itself deliberately
ignores - the manifest is what ships, but a module written five minutes ago is exactly what someone is
trying to get in front of a running server. `--quiet` prints one line, for driving it from an editor hook.

## The view picks its ACES version, a ProRes preview has sound again, and OCIO Write stops trying to be a player

**`view` now offers ACES 1.3 as well as the loaded config's own transforms**, prefixed so the version is
visible rather than implied.

The Output Transform is not one transform, it is a version of one, and two versions do not agree. Measured on
a real Rec.709 master: take it to ACEScg through the ACES 2.0 inverse Output Transform and view it back
through the SAME 2.0 transform, and the round trip is exact to 0.157% of full scale. View that identical file
through ACES 1.3 instead and the worst pixel is off by **35.5%**, the mean by 1.2%. Nothing in the file says
which version made it; it simply expects a matching viewer, and when it does not get one the picture reads as
"close, but the blacks and the highlights are wrong".

That is not a corner case. A Nuke 13 or 14 comp is on ACES 1.2 / 1.3, and in an ACES project Nuke's own Read
applies the inverse Output Transform on the way in while the viewer applies the forward one - so an EXR
written here has to have been rendered by the version that comp will view it with. Hence the choice.

No download and no repository weight: OCIO 2.5 carries eight configs internally, four on the ACES 1.3 line and
four on 2.0, and this reads the 1.3 studio config straight out of the library. Asked of
`BuiltinConfigRegistry` rather than remembered, and worth saying plainly - **the oldest built-in line is 1.3,
not 1.2**. A 1.2 config would have to ship as a file.

The 1.3 entries are prefixed because `Raw` and `Un-tone-mapped` exist in both configs and would otherwise
collide silently, and because choosing a rendering transform without seeing its version is how a mismatched
EXR gets made in the first place. The default is untouched.

**A ProRes preview has its audio back.** The H.264 proxy that OCIO Read and OCIO Player build for codecs a
browser cannot decode was assembled with `-map 0:v:0` and `-an`, on the stated reasoning that "the Player is
muted". That reasoning expired: the viewer grew a real audio path - a Web Audio graph, a gain node, a mute
button and dBFS meters - and the proxy was never told. The result was a split with no reason behind it. An
h264 .mp4 needs no proxy, so it streamed directly and its track played; a ProRes .mov went through the proxy
and arrived silent. Same node, same viewer, same button.

The proxy now carries the first audio track, re-encoded to AAC (`0:a:0?`, optional, so a clip with no audio
still transcodes). Verified on a real ProRes 4444 master: the rebuilt proxy comes back with
`aac, 48000 Hz, stereo, 5.000 s` against a silent one before. The proxy cache key gained a recipe version,
because it otherwise describes only the SOURCE - every proxy already on disk would have stayed valid and kept
being served, making the fix invisible to exactly the people who had viewed the clip before.

**OCIO Player shows its audio meters on a video source.** The row was force-hidden in both of the Player's
paths, while OCIO Read's identical path showed it. On the float-frame path hiding it is right - half-float
textures are pictures with no track at all - but on the video path the Player is driving a `<video>` element
over the file, and that element carries the sound. It starts muted, so nothing makes noise unasked.

**OCIO Write has no preview at all any more.** No flipbook, no player, no transport, no Viewer toggle; the
last widget on the node is `▶ Render`. It reports in text - frames written, colour transform, metadata
verdict, audio verdict.

This reverses a direction taken one step at a time over two days, and the reversal is the right call. The
pack already has a viewer and it is a better one: OCIO Player is a float viewport with in / out points,
reverse, an exposure strip, audio metering and a GPU frame cache. A second, smaller player on every Write
node turned a graph of four writes into a column of four video players, none of them the one built for
looking at pictures. `_video_preview` and `_save_preview_png` went with it, 49 lines that no longer had a
caller.

**The sidecar can be declined.** A `<name>.json` landed beside every render with no way to say no. The new
`write_sidecar` widget turns it off, and it defaults to ON, so nothing a saved graph does changes.

Whether that is a loss depends entirely on the format, and the written file has always said so itself. Beside
a MOV, `sidecar_only` lists the four attributes the container physically cannot hold, and the .json is their
only home. Beside an EXR that list is **empty**: the header took all eight, and the .json is a second copy of
what is already in the frames. So it is worth keeping for a movie and worth declining for an EXR sequence
going to a client who did not ask for it. Read `sidecar_only` in the file before deciding.

The switch touches the sidecar and nothing else. The pixels, the container tags, the EXR header and the .wav
that ships beside a sequence are all unaffected, which `tools/test_write_sidecar_toggle.py` asserts by reading
the header back off a frame written with the sidecar turned off.

**The node now draws all three of its previews itself** - the sequence flipbook, the movie and the still - in
one widget, with a transport and a collapsible Viewer.

This started as a UI request and turned out to be structural. A preview handed back as `images` is rendered by
the FRONT END, in markup this pack does not own; on the Vue frontend it is a Vue-managed element whose classes
are its own business and change between versions. Nothing in an extension can reliably collapse it. That is
why the movie had a player while a sequence had none, and why neither could be folded away the way OCIO Read's
Viewer can. Under keys core does not know (`mov`, `still`) the node draws them itself, and one toggle and one
transport then cover every container.

- **Transport**: play / pause, stop (rewind to the first frame), a scrub, and a frame counter. Deliberately
  smaller than OCIO Read's, which has in / out points, reverse, an exposure strip and a GPU frame cache
  because it is a grading viewport over a plate it has never touched. This looks at a write that just
  finished, on a local disk, and answers "is that the clip I meant to make".
- **`▾ Viewer`**: the same chevron toggle OCIO Read carries. Collapsing gives the height back - measured, 876
  px to 622 on a test node - and stops the flipbook's clock, so a hidden strip is not still asking the server
  for frames.
- A still gets no transport, because one frame has nothing to scrub.

The cost, named rather than glossed: a preview under these keys does not appear in ComfyUI's output gallery or
queue history. For an output node whose product is the file on disk, that is the right trade; it is not free.

Verified in the canvas rather than reasoned about. The sequence scrub was checked by reading the PIXEL at each
position, not the label: position three returns blue and stop returns red, from a source whose three frames
were deliberately different colours. On the movie, a 2 s clip advances to 1.291 s with the element unpaused.
An earlier 0.125 s clip looked like a broken play button and was not - three frames at 24 fps finish and loop
before a probe can look.

## Files stop making claims about themselves that are not true

Three defects with one shape, found in one pass: the writer describing a file as something it is not. None of
them changed a pixel, and all three would have been read as fact by whatever opened the file next.

**A movie was told it was sRGB whatever it held.** `_video_color_tags` had five branches and a fall-through,
and 39 of this config's 55 colorspaces fell through. So an ACEScg ProRes left with
`primaries=bt709 transfer=iec61966-2-1 matrix=bt709` - the file declaring itself sRGB while holding linear
AP1, and a player that trusts the tag applying the sRGB EOTF to linear data.

It is not fixable by adding branches, because the codes do not exist. ITU-T H.273 (V4, 07/2024) Table 3
defines TransferCharacteristics 0-18 with 19-255 reserved, and its only logarithmic entries are 9 (100:1) and
10 - neither is any camera curve. Table 2 defines ColourPrimaries 0-12 and 22, with no AP0, AP1 or camera
wide gamut. ARRI LogC, S-Log3, V-Log, Log3G10, Apple Log, ACEScc, ACEScct and ADX are untaggable by
construction.

So log and scene-linear are now written with **no colour tags at all**: 36 spaces silent, 19 described. The
principle was already in this file, written for the `raw_data` branch - an untagged file leaves a player
guessing, which is honest; a confidently mistagged one makes it guess wrong and believe it is right - and had
been applied to one branch out of six. Decided by asking the config for each colorspace's `encoding`, not by
matching names, which is what had made `Linear Rec.709 (sRGB)` pick up `trc=bt709` from the substring
"rec.709".

**A DPX said `Linear` no matter what it was.** ffmpeg's dpx encoder stamps one answer regardless of content:
measured, an ADX10 write and a Rec.709 write both came back with transfer descriptor 2. Here the remedy is
the opposite of the one above, because SMPTE ST 268 **has** the codes - 1 printing density, 3 logarithmic, 6
ITU-R 709 - so the honest answer is to write the right one. Verified by reading the bytes back with this
pack's own DPX reader: ADX10 to 1, ARRI LogC3 to 3, Rec.1886 Rec.709 to 6, ACEScg to 2.

**And a frame too large for HEVC said nothing at all.** HEVC levels top out at 35 651 584 luma samples
(H.265 Table A.8), and x265 does not implement the 6.3 and 7.x levels added in 2023 - past the ceiling it
stamps **Level 8.5**, the "decoder, work it out yourself" value, which hardware is not obliged to play.
Measured here: 8192x4320 gives Level-6, 16384x8192 gives Level-8.5. A warning rather than a refusal, since
the file decodes in software and a large-format plate may be exactly what was meant, but not something to
discover at playback.

Two tests were pinning the old behaviour and are corrected rather than deleted: one asserted ACEScg should be
tagged sRGB ("falls to the default" - right about the code, wrong about the intent), and the MXF block wrote
ACEScg while testing whether the *container* carries tags, which would now come back untagged for a reason
having nothing to do with MXF.

## OCIO Write can reach the ACES Output Transform, and DPX starts on ADX10

Crossing between scene-referred and display-referred colour has two correct answers, and this node offered
one with no control that could produce the other.

`ACEScg -> Rec.1886 Rec.709` maps scene-linear 0.18 to 0.489436 and 4.0 to **1.781807**. A Rec.709 container
encodes to 1.0, so 1.78 and 3.17 both land on white and every highlight above diffuse white flattens onto the
same value. Through the ACES Output Transform they become 0.383116 and 0.905089, and nothing reaches 1.0
until scene-linear around 128. Reading the other way, mid-grey 0.5 from Rec.709 to ACEScg gives 0.189468
against 0.324827, which is the difference between our EXR and one exported from Nuke.

The current behaviour is not a bug: OCIO crosses the boundary through the config's `default_view_transform`
(`Un-tone-mapped` here), and the result is bit-identical to Nuke's own `Utility - Rec.709 - Display`. It is
one of two legitimate operations, and the other was unreachable.

The new `view` widget picks between them. **Optional, appended, and defaulting to a do-nothing sentinel**, so
no saved workflow moves: ComfyUI fills a missing widget with the node's current default, a new *required*
input is a hard validation error for API callers, and a widget inserted mid-list shifts every value in every
saved graph. All three were caught by `tools/test_write_metadata.py` on the way in. Choices are read from the
loaded config, so an ACES 1.3 user sees their own view names. Whether the fork exists is asked of OCIO via
`getReferenceSpaceType()`, so a view is inert on camera logs, scene-to-scene and display-to-display pairs -
exercised across all 414 crossing pairs and all 9 displays.

`docs/NODES_IO.md` carries a section on it rather than a table row: which pairs it touches, the numbers for
both answers in both directions, and when `(none)` is the right choice.

**DPX now starts on ADX10** instead of falling through to a display space. DPX is the film-scan container; it
exists to carry printing density, and ADX10 is the ACES encoding of exactly that. Changed on both sides
together, since the front end and `_auto_output_cs` must agree or the node shows one colorspace while the
backend writes another.

## OCIO VAE Decode tiles by default, and a sequence write plays

`tiled` now defaults to ON. Whole-frame is the setting that runs out of memory on a real clip and is slower
even when it fits: 912 s against 60 s over 121 frames at float32. Saved graphs are unaffected, they carry
their own value.

A `sequence` write showed one still frame, because every format that branch writes is one a browser cannot
animate. It now plays back **the written frames themselves**: the node gets their path and range, and the
front end flips through them the way OCIO Read already does, one server-rendered frame at a time through
`/ocio/thumb`. `thumb_frame` reads them with `_read_still`, the same reader OCIO Read uses, so every format
this branch can write is one the flipbook can serve back.

Measured on all five, in the canvas, from a source whose three frames are deliberately different colours - a
strip that quietly fell back to frame one would otherwise look perfectly correct. EXR, TIFF, PNG, JPEG and DPX
each returned three distinct frames over the written range, with no proxy beside them. The colour is right,
not merely present: a DPX written in ADX10 and another in ARRI LogC3 both came back within one code value of
the sRGB the graph started from, which they could not do if the strip were showing log data as though it were
display-referred.

There is a persistent `↻` in the strip's top-left corner, the same square OCIO Player carries, for when the
frames on disk change under a preview that is already drawn - a re-render into the same folder, a retake from
another graph. It re-reads them from disk rather than from the browser's cache. If a frame will not load, the
strip is replaced by the folder path instead of freezing on the last good frame.

**One preview, not two.** The first cut of this shipped the flipbook *and* the H.264 proxy, so a written
sequence came back with two previews on one node - the real frames, and a darker 8-bit copy of them
disagreeing about colour, with nothing to say which was the master. An artist reading colour off the wrong one
is the whole failure this pack exists to prevent. The proxy is now the fallback it was meant to be: it ships
only when the flipbook cannot be described. A movie still gets the proxy and no flipbook, because a container
file has no frame range to scrub; a single frame still gets its own PNG thumb and nothing that pretends to
move. `tools/test_write_preview_single.py` reads the counts off a real write of each of the three.

## A tool that checks the installed copy against the repository

`tools/check_deploy_sync.py` compares the repository with the copy ComfyUI actually loads and exits non-zero
when they differ, reporting per file: identical, differs, missing, or extra. It exists because live runs were
being made against a stale installed copy while the repository's own gate was green, and nothing could tell
the difference. Verified by mutation on the real pair - a differing file, a missing file, an extra file and a
dirty tree are each detected, and a CRLF-only change correctly is not.

One environment fact learned the hard way and worth repeating: **the gate must be run with ComfyUI stopped.**
A running server holds resources and fails three or four random tests per run.

## The `LTX 2.5 HDR (ACEScct)` write profile is removed, and that will break saved workflows

**Read this first if you used it.** A COMBO value is matched by string, and ComfyUI rejects an unknown one with
HTTP 400 and no fallback. So a workflow saved with `profile = LTX 2.5 HDR (ACEScct)` selected on `OCIO Write`
will now fail validation *before any code in this pack runs*. There is no migration, no silent fallback, and no
warning in the node: the graph simply will not queue. The fix in an affected graph is to set `profile` back to
`none` and wire the conversion explicitly, as below. This is a breaking change and it is being called one.

**Why it goes.** The preset mapped `ACEScct` to `ACEScg` on the premise that an LTX-2.5 decode inside ComfyUI
hands out ACEScct log codes. It does not. That encoding is reached through the
`--hdr {SRGB_LINEAR,ACESCG,ACESCCT}` flag in Lightricks' *reference CLI*, which is a different program.
Lightricks' own ComfyUI pack ships no 2.5 HDR workflow, and ComfyUI's core has no ACEScct path at all: greps
for `acescct` across `comfy/` and `comfy_extras/` return zero, run with a control grep alongside so the result
is a statement about the code rather than about the probe. A 2.5 graph in ComfyUI is ordinarily SDR, so for
almost everyone the preset was a widget that quietly declared their frames to be log when they were not, and
the failure it produced is the one this pack keeps warning about: log read as linear, flat and grey.

The second reason is process, and it is the one worth recording. Nothing backing this preset was ever measured
on a real generation through this pack. What backed it was documentation, and accurate documentation at that -
of a pipeline this pack was not running. Reading a vendor's spec correctly is not the same as confirming the
path your own code is on, and a preset is a claim about the second. Every other profile here rests on a
measurement; this one rested on a reading.

**What to do instead**, which the docs already diagrammed as the clearer route: after `OCIO VAE Decode`, put an
`OCIO LogConvert` with `operation = Log to Linear` and `curve = ACEScct`, then write. Set `still_format = exr`
and `bit_depth = 16f` on `OCIO Write` yourself to match the half-float EXR the reference produces, since the
removed preset was what used to force those two. Mirror it on the input side with `Linear to Log`, `ACEScct`
before `OCIO VAE Encode`. The maths is identical; nothing about the colour changes, only where you can see it.

`LTX 2.3 HDR` is untouched and still correct for 2.3 - its IC-LoRA is the ARRI LogC3 curve, and Lightricks'
`LTXVHDRDecodePostprocess` already undoes it, so linear is what arrives. The two LumiPic presets and
`SDR Rec.709 delivery` are untouched as well.

`tools/test_ltx_hdr_profiles.py` now asserts the removed value is absent from all four surfaces it occupied:
the combo, the backend branch in `write()`, the front-end `PROFILE_CS` table, and the EXR-16f forcing list.
Verified by mutation rather than by reading: putting the value back into all three files turned six assertions
red, and restoring turned them green. The same rewrite closed a hole found on the way past - the two LumiPic
rows in the front-end table were guarded by nothing, so mutating one to garbage had been leaving the file
green. That mutation is now red too.

## "Nothing was clamping its output" was a claim the node could not make

When `OCIO VAE Decode` meets a VAE whose `process_output` is the identity, it reported that nothing had been
clamping the output and that the `clamp` switch therefore made no difference. The second half is true. The
first half is a statement about the *decoder*, and a pass-through wrapper is no evidence for it.

MiniMax H3 is the case that breaks it. `comfy/ldm/minimax/vae.py:398-401` ends every decode in
`.clamp_(0.0, 1.0)`, which is precisely why `comfy/sd.py:976` can install the identity: the pixels arrive
already in range. Checked against all three entry points, since one clamped path proves nothing about the
others: `decode()` either finalises a single frame itself or hands off to `decode_temporal()`, which
finalises each chunk, and `decode_tiled()` calls `decode()`. Nothing reaches a caller unclamped.

So on that model the note told the artist the opposite of the truth, and sent them looking for highlights
that had been discarded two layers below anything this pack can reach. It now says what it can support: the
wrapper was a pass-through, this node found no clamp to remove, and some decoders clamp internally before
this point, so unclamped output is not guaranteed. `docs/NODES_VAE.md` carries the same correction, including
one sentence there that drew the wrong conclusion outright, that the eleven identity VAEs are models "where
neither path clamps".

Two assertions in `tools/test_vae_decode_tiling.py` hold the wording now: the false half must be absent and
the caveat must be present. Verified by restoring the old sentence and watching both go red, since a check
written after the fix proves nothing until it has failed once.

The distinction is worth carrying into how the pack is described. Removing the clamp is what these nodes do
on LTX-2.5, where it lives in the wrapper. On a model that clamps inside its own decoder, what they offer is
the rest of it: float32 with no 8-bit quantisation and no compressor in the way, and colour management.

## The install advice in our own error messages was wrong about OpenCV 5

Measuring the two OpenCV majors side by side settled a question this pack had been answering from memory, and
answering incorrectly. `cv2.getBuildInformation()` reports `OpenEXR: build (ver 2.3.0)` on
`opencv-python-headless 4.13.0.92` and `OpenEXR: NO` on `5.0.0.93`, where `IlmImf` has also left the
third-party list. A write to a `.exr` path on 5.0.0.93 raises `could not find a writer for the specified
extension` with `OPENCV_IO_ENABLE_OPENEXR=1` set before the import. The codec is not gated there; it is gone.

That makes the original report in [#4](https://github.com/SlavaSexton/ComfyUI-OCIO/issues/4) by
[@Sudhzpatil](https://github.com/Sudhzpatil) right on the point it was corrected on. Both failures are real
and land on the same line: on OpenCV 4 the codec is present but locked off by an import order this pack does
not control, and on OpenCV 5 there is nothing to unlock.

Two messages told the artist otherwise. `OCIO Read` advised `pip install "OpenEXR>=3.2"` while the
requirements had moved to `>=3.3`, so following it to the letter installed a version without the
`OpenEXR.File` API this pack calls in four places, and landed back on the same failure. Both it and the EXR
write error offered `OPENCV_IO_ENABLE_OPENEXR=1` as a general remedy, which does nothing on OpenCV 5. Both now
say which major each remedy applies to.

Nothing was checking either message, which is how the two versions drifted apart in the first place.
`tools/test_install_advice_matches_deps.py` asserts that every `pip install "OpenEXR>=X"` in shipped code names
the floor the requirements actually declare, and that `requirements.txt` and `pyproject.toml` agree with each
other. It checks agreement, not wording, so the sentences stay free to change.

The rest of what this pack calls from cv2 was re-run against 5.0.0.93 while the environment was up: the eight
EXR compression constants, both EXR type constants, `INTER_AREA`, a bit-exact 16-bit PNG round trip and the
preview encoder all pass. EXR is the only thing OpenCV 5 cannot do here, and it is the one thing that no longer
goes through it.

## EXR compression goes back to zip, because the measurement said so

`compression` defaulted to `dwaa` for one day. The argument was that lossy-and-small is the house default
across VFX for anything that is not an archival master, and it sounded right. Measuring it on a real camera
frame at 1920x1318, with `raw_data` on so only the compressor was under test:

| | file | distinct green values | max abs error |
| --- | --- | --- | --- |
| `16f` + `zip` | 6342 KB | 1843 | 0.000118 |
| `16f` + `dwaa` | 1164 KB | 855 | 0.009525 |
| `32f` + `zip` | 14267 KB | 19118 | 0.000000 |
| `32f` + `dwaa` | 1164 KB | 855 | 0.009525 |

DWA keeps its promise on size, 5.4x smaller. It also costs 54% of the distinct values and multiplies the error
by eighty, **and it does that at `16f` too** - which this pack never said, because the caveat it documented was
only about `32f` being quantised to half. A pack whose argument is that it does not throw information away
should not ship a lossy default, and Nuke, whose Write node this one is modelled on, defaults to Zip.

`dwaa` stays one pick away, which is what it is for: a review or a comp copy. Never a master, never a data
pass. A graph saved with either value keeps it.

## OCIO Write stops answering a bad input with something that looks like success

Five defects from an adversarial pass over the write path. They share a failure mode rather than a mechanism:
each turned a situation the node could not honour into a result that read as fine. All five were reproduced
before being touched, and each fix carries a check that goes red without it.

**Metadata could kill a render.** A `metadata` wire whose `attrs` was valid JSON but not an object crashed
`OCIO Write` outright, with a bare `TypeError`, `ValueError` or `AttributeError` depending on the shape. The
socket is `forceInput` and takes a wire from any source, and a JavaScript `Object.entries()` serialisation
produces exactly the list-of-pairs form that survived the first reader and died in the second. The pack's rule
is that metadata never stops a render: a plate description we cannot read is dropped with a note and the pixels
still get written. The existing tests only varied hostile values *inside* a proper `attrs` dictionary, so the
shape itself had no coverage.

**MXF refused the pack's own default frame rate.** `str(23.976)` makes ffmpeg parse `2997/125`, which is not
`24000/1001`. MOV and MP4 accept the odd rational and carry it into the file; the MXF muxer is strict and
rejects it, so both MXF codecs failed at 23.976 and 29.97 - `_SEQ_FPS_DEFAULT` and two of the pack's own
`_DROP_FRAME_RATES`. Confirmed against raw ffmpeg outside the pack, so it was the argument form and not our
encoder options. `-r` now goes through `_fps_arg`, which recognises the NTSC family by round trip rather than
by lookup table, on a relative tolerance: an absolute one silently dropped 119.88 while appearing to cover it.

**A still could be written under a name that lied.** `container = still image` with `first_frame` past the end
of the batch clamped to the nearest frame: asking for frame 999 of a 3-frame batch wrote `name.0999.exr`
containing frame 3, reported success, and said nothing. A filename is a claim about which frame it holds. Out
of range now names both the frame asked for and the range available, matching what the `sequence` branch has
always done. The same clamp underflowed to `-1` on an empty batch and died on `arr[-1]`.

**`raw_data` claimed a colourspace on movies.** With `raw_data` on, a still correctly wrote no colorimetry at
all, while a movie went down a path with no "unspecified" branch and came out tagged `bt709` /
`iec61966-2-1` / `bt709`. Measured with identical pixels and only the flag flipped. Unconverted pixels have no
delivery space to name; an untagged file leaves a player guessing, a mistagged one makes it guess wrong and
believe it is right. The docs had described the correct behaviour for some time - it was verified on a PNG and
generalised to "the output" - so this brings the code up to what was already written.

Gate 32/32, one new file. Mutation matrix over the five fixes: 0 survivors of 7.

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
a node you create fresh. *(Reverted before this release shipped: measured on a real frame, DWA costs 54% of
the distinct values and eighty times the error at `16f` as well as `32f`, so the default went back to `zip`.
See the entry at the top of this file.)*

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
bits. For 12-bit the route is ProRes 4444. *(Corrected later in this same release: it is not. ffmpeg's ProRes
encoders top out at 10-bit as well, and the 12 that `ffprobe` reports comes from the format's definition rather
than from the samples in the file. The genuine 12-bit route is `hevc_444_12`; the lossless one is `ffv1`.)*

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
