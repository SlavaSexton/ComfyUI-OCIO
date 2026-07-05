# Dockerized ComfyUI test environment

A reproducible, CPU-only ComfyUI with this pack installed, so the OCIO nodes can be driven
programmatically and the color math can be verified end-to-end. Builds **native arm64 on an
Apple-silicon (M1/M2/M3) Mac** and amd64 in CI from the same `docker/Dockerfile`. No GPU and no
ML model downloads are required — the OCIO nodes are pure color math.

**OCIO config:** the image bundles the classic **ACES 1.0.3** config (sparse-cloned from
`imageworks/OpenColorIO-Configs`, config + LUTs, ~a few MB) and sets `$OCIO` to it, so every node
uses it. Set `OCIO=""` (or override the env) to fall back to the `opencolorio` wheel's built-in
ACES 2.x config — the test resolves colorspace/display/view names from whichever config is active, so
both work.

## Quick start

```bash
docker compose build                 # build the image
docker compose run --rm roundtrip     # the flagship round-trip color-accuracy test (below)
docker compose run --rm test          # standalone tools/test_*.py + node-registration smoke
docker compose up comfyui             # interactive headless server at http://localhost:8188
```

`./data` on the host is bind-mounted to `/data` in the container, so the downloaded input and the
written outputs land in `./data/input` and `./data/output` where you can inspect them.

## The round-trip color-accuracy test

`docker compose run --rm roundtrip` downloads the Kodak **"Marcie"** test image, feeds it through a
real ComfyUI graph built from the OCIO nodes, round-trips it back to the starting space, and compares
the output to the input with **OpenCV histograms** (`cv2.calcHist` / `cv2.compareHist`) plus a
max/mean absolute pixel-error backstop. Read and write are done in **raw 32-bit float EXR** (no
implicit colorspace conversion on IO, no fp16 quantization) so the measurement isolates exactly the
node chain under test.

Three chains run by default. Marcie is HDR (scene-linear values up to ~4.35), which is the whole
point: it separates the **color math** (invertible) from the **display-range encodings** (which clip):

| Chain | Path | Expectation |
|-------|------|-------------|
| `colorspace` **(gated)** | ACEScg → ARRI LogC → **linear** Rec.709/sRGB → LogC → ACEScg, via `OCIOColorSpace` | **Perfect** — histogram correlation ≈ 1.0, max pixel error ~1e‑6. The "color math is perfect" proof. Uses the **linear** Rec.709/sRGB primaries (`Utility - Linear - Rec.709`), which invert exactly even for HDR values >1. |
| `srgb` (info) | same, but through the display-range **sRGB texture** encoding (`Input - Generic - sRGB - Texture`) | **Residual expected.** The sRGB texture encoding clamps to [0,1], so HDR values >1 are crushed and can't round-trip. A property of the *encoding*, not the math. |
| `display` (info) | ACEScg → LogC → the `ACES` display / `sRGB` **view** (fwd) → (inverse) → LogC → ACEScg | **Residual expected.** The ACES output view (RRT+ODT) tone-maps and clips, so it's not losslessly invertible. |

Only `colorspace` gates the exit code (`GATE_CHAINS=colorspace`); `srgb` and `display` are reported
to show *where* display-range encodings lose the HDR highlights. Colorspace / display / view names are
resolved at runtime from the live OCIO config (`docker/ocio_names.py`), so the test works on the bundled
ACES 1.0.3 and the built-in ACES 2.x alike.

Representative result (ACES 1.0.3, `marcie_whacked.exr`):

```
[GATED] colorspace : PASS  corr=1.000000  maxpix=4.29e-06
[info ] srgb       : FAIL  corr=0.240     maxpix=3.35
[info ] display    : FAIL  corr=0.244     maxpix=0.031
```

### Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `INPUT_IMAGE_URL` | sobotka `marcie_whacked.exr` | Direct-download URL for the input EXR. |
| `CHAINS` | `colorspace,display` | Which chains to run. |
| `GATE_CHAINS` | `colorspace` | Which chains' PASS/FAIL sets the exit code. |
| `TOL_CORR` | `0.99999` | Min histogram correlation to pass. |
| `TOL_PIX` | `1e-4` | Max absolute pixel error to pass. |
| `RUN_ACCURACY` | `0` | `1` also runs the `tools/accuracy` suite in the `test` service. |
| `OCIO` | bundled `aces_1.0.3/config.ocio` | Path to the OCIO config; set `""` to use the wheel's built-in ACES 2.x. |
| `COMFYUI_REF` (build arg) | `v0.3.68` | Pin a different ComfyUI version. |
| `OCIO_CONFIGS_REF` (build arg) | `master` | Branch/tag of `imageworks/OpenColorIO-Configs` to pull the ACES 1.0.3 config from. |

```bash
# Example: only the invertible control, tighter pixel tolerance
CHAINS=colorspace TOL_PIX=1e-5 docker compose run --rm roundtrip
```

### Using the official AMPAS Marcie

The default is the verified, directly downloadable `marcie_whacked.exr`. The canonical AMPAS
`DigitalLAD.2048x1556.exr` (true ACES2065-1) is only distributed via a Dropbox *folder* link that
can't be `curl`'d cleanly — to use it, download it yourself and drop it in as `./data/input/marcie.exr`
(the job uses an existing input as-is and skips the download).

## Interactive / API use

```bash
docker compose up comfyui
curl -s localhost:8188/object_info | grep -o '"OCIOColorSpace"'   # confirm the nodes are live
```

You can then POST API-format graphs to `http://localhost:8188/prompt` (see `docker/build_workflow.py`
for how the round-trip graphs are constructed).

## Files

- `docker/Dockerfile` — the environment (ComfyUI + pack + ffmpeg + CPU torch).
- `docker/comfyui_client.py` — stdlib-only ComfyUI HTTP-API client.
- `docker/ocio_names.py` — resolves colorspace/display/view names from the live OCIO config.
- `docker/build_workflow.py` — builds the API-format round-trip graphs.
- `docker/compare_histograms.py` — OpenCV histogram + pixel-error comparison.
- `docker/roundtrip_test.py` — the flagship orchestrator.
- `docker/run_tests.sh` — standalone tests + node-registration smoke.
- `docker-compose.yml`, `.github/workflows/docker-tests.yml` — local + CI wiring.
