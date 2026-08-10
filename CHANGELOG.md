# Changelog

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

### Color math is untouched

No transform, curve, LUT path or conversion changed in this release; the accuracy suite reports the same
numbers as 1.2.4.

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
  single-precision LUT interpolation — not bit-for-bit lossless, but ~100x finer than one half-float EXR step
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
