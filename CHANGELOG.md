# Changelog

## Native ComfyUI VIDEO pipeline

A big feature release: the color nodes now live on ComfyUI's native video wire.

### Added
- Native ComfyUI **VIDEO** pipeline on the six color nodes: each carries a mutually-exclusive IMAGE vs VIDEO input pair, and the output mirrors the active input.
- VIDEO in / VIDEO out using ComfyUI's native `comfy_api` `VideoFromComponents`, the same type Load Video emits, so the nodes interoperate with Load Video, Save Video, Video Combine, Get Video Components and VHS.
- **OCIO Write** records a native ComfyUI video to disk with all of its settings (container / codec / colorspace / bit depth), inheriting the clip's frame rate.
- **OCIO Read** exposes a VIDEO output that feeds ComfyUI-native video nodes downstream.
- Mutually-exclusive Image / Video sockets: connect one input and the other auto-disconnects.
- LogConvert curves now carry readable labels (for example "Sony S-Log3", "ARRI LogC3", "Linear to Log").

### Fixed
- Page reload wiped all node connections (the dual-socket auto-disconnect fired during graph load and corrupted the link map).
- A native `Load Video -> OCIO color node -> Player` chain ignored the color nodes and showed the raw frame.
- OCIO Player showed processed video flat / washed-out.
- OCIO Write video preview failed with "Invalid URL" (now a small, servable, playing H.264 preview).
- Fast video decode, 12-25x quicker than before (temp-file read instead of a subprocess pipe).
- Scene-linear HDR shaper safeguard on the color viewport path.

### Changed
- Slot labels: the IMAGE socket reads "OCIO Img/Seq/Vid", the VIDEO socket reads "ComfyUI Video", across the nodes.
- README refresh.

### Verified
- Bit-exact OCIO parity (worst max-abs error 0.000e+00 across 9 transforms x 4 fixtures) plus the full color-accuracy suite (`tools/accuracy`); charts in `docs/assets/accuracy/`.
