# Changelog

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
