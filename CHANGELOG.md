# Changelog

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
  single-precision LUT interpolation, above half-float EXR storage precision); `histogram_compare.png` is
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
