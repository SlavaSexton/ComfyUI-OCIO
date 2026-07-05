# ComfyUI + ComfyUI-OCIO test environment.
#
# Builds a self-contained, CPU-only ComfyUI with this custom-node pack installed, so the OCIO
# color nodes can be driven programmatically (headless, via the ComfyUI HTTP API) and the pack's
# standalone tests can run. Arch-agnostic: builds native arm64 on an Apple-silicon Mac and amd64
# in CI from the same file.
FROM python:3.12-slim

# ffmpeg: full build for the VIDEO Read/Write nodes (ProRes/DNxHR/h264/hevc).
# libglib2.0-0 + libgl1: runtime libs opencv (even -headless) dlopens.
# git: to clone ComfyUI.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ffmpeg libglib2.0-0 libgl1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --- ComfyUI (pinned for reproducibility; override with --build-arg COMFYUI_REF=<tag/sha>) ---
ARG COMFYUI_REF=v0.3.68
RUN git clone --depth 1 --branch "${COMFYUI_REF}" https://github.com/comfyanonymous/ComfyUI /opt/ComfyUI

WORKDIR /opt/ComfyUI

# --- CPU-only torch ---
# The default PyPI torch/torchvision/torchaudio wheels bundle CUDA on BOTH amd64 and arm64 (many GB).
# The PyTorch CPU index publishes manylinux CPU wheels for x86_64 AND aarch64, so pull all three from
# there. torchaudio is included because ComfyUI's requirements.txt lists it — installing the CPU wheel
# here means the later `-r requirements.txt` finds it already satisfied and never pulls the CUDA build.
RUN pip install --no-cache-dir torch torchvision torchaudio \
        --index-url https://download.pytorch.org/whl/cpu

# ComfyUI's own deps (torch already satisfied above, so this won't re-pull a CUDA build).
# `requests` is imported by ComfyUI's app/frontend_management.py but missing from its requirements.txt
# (normally present transitively); add it explicitly so the server starts in this clean image.
RUN pip install --no-cache-dir -r requirements.txt requests

# --- The OCIO pack + its deps ---
# matplotlib is only needed by the optional tools/accuracy suite; kept here so RUN_ACCURACY=1 works.
# opencv-python-headless is pinned <5 because the opencv 5.0 wheels are built with OpenEXR: NO (no EXR
# codec at all); the 4.x wheels bundle OpenEXR, enabled by OPENCV_IO_ENABLE_OPENEXR=1 (set below).
COPY requirements.txt /tmp/ocio-requirements.txt
RUN pip install --no-cache-dir -r /tmp/ocio-requirements.txt matplotlib "opencv-python-headless<5"

# --- ACES 1.0.3 OCIO config ---
# Sparse-clone just aces_1.0.3/ (config.ocio + its luts/, ~a few MB; NOT the 456 MB whole repo) and
# point $OCIO at it. The pack's _resolve_config honors $OCIO, so every node uses this config with no
# graph changes. OCIO 2.x reads this v1 config fine.
ARG OCIO_CONFIGS_REF=master
RUN git clone --depth 1 --branch "${OCIO_CONFIGS_REF}" --filter=blob:none --sparse \
        https://github.com/imageworks/OpenColorIO-Configs /tmp/ocio-configs \
    && git -C /tmp/ocio-configs sparse-checkout set aces_1.0.3 \
    && mkdir -p /opt/ocio-configs \
    && cp -r /tmp/ocio-configs/aces_1.0.3 /opt/ocio-configs/aces_1.0.3 \
    && rm -rf /tmp/ocio-configs \
    && test -f /opt/ocio-configs/aces_1.0.3/config.ocio

# Install the pack into ComfyUI's custom_nodes (standard location).
COPY . /opt/ComfyUI/custom_nodes/ComfyUI-OCIO

# EXR read/write in OpenCV must be enabled before import; unbuffered logs for docker.
# OCIO -> the bundled ACES 1.0.3 config (override at run time to use a different config, or set
# OCIO="" plus nothing to fall back to the wheel's built-in ACES 2.x config).
ENV OPENCV_IO_ENABLE_OPENEXR=1 \
    PYTHONUNBUFFERED=1 \
    COMFYUI_DIR=/opt/ComfyUI \
    OCIO_PACK_DIR=/opt/ComfyUI/custom_nodes/ComfyUI-OCIO \
    OCIO=/opt/ocio-configs/aces_1.0.3/config.ocio

# Default: headless ComfyUI on the CPU. The compose `test`/`roundtrip` services override this.
CMD ["python", "main.py", "--cpu", "--listen", "0.0.0.0", "--port", "8188"]
