# Attribution

The **ComfyUI-OCIO** node code (`nodes.py`, `io_nodes.py`, `__init__.py`, the `web/` front-end, the example
workflow) is original and licensed under **Apache-2.0** - see [`LICENSE`](LICENSE). Author:
**Slava Sexton**, for **[AI VFX NEWS](https://aivfxnews.com/)**.

Releases up to and including 1.2.6 were published under the MIT license. Those releases stay MIT: a license
already granted cannot be withdrawn. Apache-2.0 applies from the next release onward.

## Reuse and credit

Apache-2.0 lets you reuse this freely, including commercially, and adds an explicit patent grant. In return
it asks for three things when you redistribute: keep this license, state the changes you made, and carry the
[`NOTICE`](NOTICE) file. That file names **`Slava Sexton`** as the author, so the credit travels with the
code **in full, in part, as a derivative, or as the design / idea** - including inside
**[ComfyUI-Agent-Kit](https://github.com/SlavaSexton/ComfyUI-Agent-Kit)** and any other project.

## Contributed code

The Docker test environment and the color-accuracy CI harness (`docker/`, `docker-compose.yml`,
`.dockerignore`, `.github/workflows/docker-tests.yml`) were contributed by **Sam Hodge**
([samhodge-1972](https://github.com/samhodge-1972)) and came in under the MIT license this project carried at
the time. That grant stands and is recorded in [`NOTICE`](NOTICE).

## Built on

The nodes are a thin ComfyUI layer over established color tools. Credit to their authors; each keeps its own
license.

| Component | Author / Org | Source | Role | License |
|-----------|--------------|--------|------|---------|
| **OpenColorIO** (PyOpenColorIO) | Academy Software Foundation | https://github.com/AcademySoftwareFoundation/OpenColorIO | The color engine behind every node except OCIO LogConvert (`pip install opencolorio`) | BSD-3-Clause |
| **ACES config** (`studio-config`) | ACES / Academy Software Foundation | https://github.com/AcademySoftwareFoundation/OpenColorIO-Config-ACES | The built-in config these nodes use by default (colorspaces, displays, views, looks); ships inside OpenColorIO | see repo |
| **The Foundry Nuke** (OCIO node set) | The Foundry | https://www.foundry.com/products/nuke-family/nuke | **Design model only** - the node set is modelled on Nuke's Read / Write / OCIOColorSpace / OCIOLogConvert / OCIODisplay / OCIOCDLTransform / OCIOFileTransform / OCIOLookTransform. No Nuke code is used or redistributed. | proprietary (reference) |
| **ComfyUI** | comfyanonymous / Comfy-Org | https://github.com/comfyanonymous/ComfyUI | The host application | GPL-3.0 |

## Python dependencies (installed by `requirements.txt`, not redistributed here)

| Package | Author | License |
|---------|--------|---------|
| `opencv-python-headless` | OpenCV | Apache-2.0 |
| `tifffile` | Christoph Gohlke | BSD-3-Clause |
| `Pillow` | Jeffrey A. Clark and contributors | HPND (MIT-style) |
| `numpy` | NumPy developers | BSD-3-Clause |

**ffmpeg** (video Read / Write) is used as an external binary on your `PATH`; it is not bundled. ffmpeg is
LGPL-2.1+/GPL depending on the build - install it yourself.

## Log curves (OCIO LogConvert)

The Cineon, ACEScct (S-2016-001) and ACEScc (S-2014-003) log curves are implemented from their **published
specifications**, verified by round-trip and black-point tests. The Cineon black point (`0 -> 0.0928`) matches
Nuke's default OCIOLogConvert.

## Example asset

`example_workflows/nyc_skyline.png` is an image generated locally with **Z-Image** by the author; it ships only
to make the example workflow open with a picture in place.

---

If you redistribute a build that bundles any third-party component, comply with that component's license. This
repo keeps dependencies as install-time packages precisely so it stays clean and license-clear.
