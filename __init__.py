"""ComfyUI-OCIO - OpenColorIO nodes for ComfyUI, modelled on Nuke's OCIO node set.

@author Slava Sexton
"""
import os

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .io_nodes import (NODE_CLASS_MAPPINGS as _IO_CLASSES,
                       NODE_DISPLAY_NAME_MAPPINGS as _IO_NAMES)
from .grade_nodes import (NODE_CLASS_MAPPINGS as _GRADE_CLASSES,
                          NODE_DISPLAY_NAME_MAPPINGS as _GRADE_NAMES)

# Read / Write IO nodes live in io_nodes.py; merge their mappings in.
NODE_CLASS_MAPPINGS.update(_IO_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS.update(_IO_NAMES)

# Grade / Grade Match / Apply Grade nodes live in grade_nodes.py; merge their mappings in too, before
# the version-suffix pass below so the Grade nodes' titles also get " - v<version>".
NODE_CLASS_MAPPINGS.update(_GRADE_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS.update(_GRADE_NAMES)

# Front-end assets: the 'swap' button, the 'upload' buttons, and the Read/Write IO helpers (auto-colorspace,
# Render button, folder browse, colorspace label).
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]


def _pack_version():
    """pyproject.toml [project] version is the single source of truth for the front-end badge."""
    try:
        import tomllib
        with open(os.path.join(os.path.dirname(__file__), "pyproject.toml"), "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except ImportError:
        # tomllib is stdlib only on Python 3.11+; this pack targets >=3.9 (pyproject.toml), so fall back
        # to a minimal regex parse of the version line rather than requiring a TOML dependency.
        import re
        try:
            with open(os.path.join(os.path.dirname(__file__), "pyproject.toml"), "r", encoding="utf-8") as f:
                m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
            return m.group(1) if m else "0.0.0"
        except Exception:
            return "0.0.0"
    except Exception:
        return "0.0.0"


__version__ = _pack_version()

# Show the pack version on every node, in the display name ("OCIO Write - v1.0.1"): a front-end corner badge
# is invisible on Vue-nodes frontends (they do not draw onDrawForeground); the title renders everywhere.
# The node TYPE (class key) stays stable, so saved workflows and node search are unaffected.
NODE_DISPLAY_NAME_MAPPINGS = {k: f"{v} - v{__version__}" for k, v in NODE_DISPLAY_NAME_MAPPINGS.items()}


# --- server routes (only inside ComfyUI) --------------------------------------------------------------------
try:
    import server
    from aiohttp import web
    import folder_paths

    _OCIO_SUBDIR = "ocio_assets"

    @server.PromptServer.instance.routes.get("/ocio/version")
    async def _ocio_version(request):
        """Report the installed pack version, for the front-end's per-node version badge."""
        return web.json_response({"version": __version__})

    @server.PromptServer.instance.routes.post("/ocio/upload")
    async def _ocio_upload(request):
        """Save a browser-picked file into the input folder so a node dropdown can select it. Optional
        'subfolder' field groups an uploaded image sequence into its own folder (one POST per frame)."""
        reader = await request.multipart()
        subfolder, saved, size = "", None, 0
        while True:
            field = await reader.next()
            if field is None:
                break
            if field.name == "subfolder":
                subfolder = os.path.basename((await field.text()).strip())
            elif field.name == "file":
                filename = os.path.basename(field.filename or "")
                if not filename:
                    continue
                sub = subfolder or _OCIO_SUBDIR
                dest_dir = os.path.join(folder_paths.get_input_directory(), sub)
                os.makedirs(dest_dir, exist_ok=True)
                with open(os.path.join(dest_dir, filename), "wb") as f:
                    while True:
                        chunk = await field.read_chunk()
                        if not chunk:
                            break
                        size += len(chunk)
                        f.write(chunk)
                saved = (sub + "/" + filename) if sub != _OCIO_SUBDIR else os.path.join(_OCIO_SUBDIR, filename).replace("\\", "/")
        if not saved:
            return web.json_response({"error": "no file"}, status=400)
        return web.json_response({"path": saved, "subfolder": subfolder, "size": size})

    @server.PromptServer.instance.routes.post("/ocio/list_dirs")
    async def _ocio_list_dirs(request):
        """List sub-folders of a server path (for the OCIO Write folder browser). Starts at the ComfyUI
        output directory; navigation goes up via 'parent'. Read-only, directory names only."""
        try:
            data = await request.json()
        except Exception:
            data = {}
        root = folder_paths.get_output_directory()
        path = (data.get("path") or "").strip()
        base = path if (path and os.path.isabs(path)) else (os.path.join(root, path) if path else root)
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            base = root
        media = (".exr", ".hdr", ".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".dpx",
                 ".mov", ".mp4", ".mkv", ".avi", ".webm", ".mxf", ".m4v")
        dirs, files = [], []
        try:
            for e in sorted(os.listdir(base)):
                p = os.path.join(base, e)
                if os.path.isdir(p):
                    dirs.append(e)
                elif data.get("files") and os.path.splitext(e)[1].lower() in media:
                    files.append(e)
        except Exception:
            pass
        parent = os.path.dirname(base)
        return web.json_response({"path": base, "parent": parent if parent != base else "", "dirs": dirs,
                                  "files": files, "output_root": os.path.abspath(root)})

    @server.PromptServer.instance.routes.post("/ocio/seq_range")
    async def _ocio_seq_range(request):
        """Detect a source's frame range + fps for the OCIO Read auto-fill (start_frame / end_frame / fps)."""
        try:
            data = await request.json()
        except Exception:
            data = {}
        from .io_nodes import _seq_range
        try:
            return web.json_response(_seq_range(data.get("source", "")))
        except Exception as e:
            return web.json_response({"kind": "still", "start": 0, "end": 0, "count": 0, "fps": 0.0,
                                      "error": str(e)[:200]})

    @server.PromptServer.instance.routes.get("/ocio/thumb")
    async def _ocio_thumb(request):
        """Instant on-node preview: frame 1 of any source (still / sequence / video), in_cs -> out_cs, as an
        8-bit PNG. Powers the front-end re-render on colorspace change WITHOUT queueing the graph - this is
        exactly why EXR (the browser cannot decode it) needs a server-rendered thumb.

        SECURITY: reads any local path, same trust level as /ocio/list_dirs and OCIORead itself (local
        single-user tool) - no allowlist here on purpose, that would break the disk-browse UX.
        """
        import numpy as np
        import torch
        from .io_nodes import thumb_frame, _convert
        try:
            import cv2
        except Exception:
            return web.json_response({"error": "server is missing OpenCV (cv2), needed to encode the thumb"},
                                      status=400)
        src = request.rel_url.query.get("src", "")
        in_cs = request.rel_url.query.get("in_cs", "")
        out_cs = request.rel_url.query.get("out_cs", "")
        raw = request.rel_url.query.get("raw", "0") == "1"
        try:
            frame = int(request.rel_url.query.get("frame", ""))   # sequence flipbook: a specific frame NUMBER
        except (TypeError, ValueError):
            frame = None                                          # no/blank frame -> first frame (still/video/seq)
        if not src:
            return web.json_response({"error": "missing 'src'"}, status=400)
        try:
            # thumb_frame decodes a full frame (a 4K EXR is tens of MB); run it in the default thread pool so a
            # sequence flipbook's rapid frame requests do NOT block the aiohttp event loop (which also serves
            # /prompt, /system_stats). cv2 / ffmpeg release the GIL during the heavy read, so this parallelises.
            import asyncio
            rgb = await asyncio.get_event_loop().run_in_executor(None, thumb_frame, src, 512, frame)
        except FileNotFoundError as e:
            return web.json_response({"error": f"not found: {e}"}, status=404)
        except Exception as e:
            return web.json_response({"error": str(e)[:300]}, status=400)
        try:
            if not raw and in_cs and out_cs and in_cs != out_cs:
                try:
                    t = torch.from_numpy(np.ascontiguousarray(rgb.astype(np.float32)))[None]
                    rgb = _convert(t, in_cs, out_cs)[0].numpy()
                except RuntimeError:
                    pass   # OCIO lib/config unavailable (_require_ocio) - pass the pixels through, same as raw=1
            png8 = (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)
            ok, buf = cv2.imencode(".png", png8[..., ::-1])   # RGB -> BGR for cv2
            if not ok:
                return web.json_response({"error": "png encode failed"}, status=400)
        except Exception as e:
            return web.json_response({"error": f"convert/encode failed: {str(e)[:250]}"}, status=400)
        return web.Response(body=buf.tobytes(), content_type="image/png",
                            headers={"Cache-Control": "no-store"})

    @server.PromptServer.instance.routes.get("/ocio/meta")
    async def _ocio_meta(request):
        """Read-only metadata panel for OCIO Read: resolution, format, frame range + count, fps, the
        auto-detected input colorspace, and alpha presence, for the current source (still / sequence / video).

        SECURITY: reads any local path, same trust level as /ocio/thumb and /ocio/list_dirs (local
        single-user tool) - no allowlist here on purpose, that would break the disk-browse UX.
        """
        from .io_nodes import read_meta
        src = request.rel_url.query.get("src", "")
        if not src:
            return web.json_response({"error": "missing 'src'"}, status=400)
        try:
            return web.json_response(read_meta(src))
        except Exception as e:
            return web.json_response({"error": str(e)[:250]})

    @server.PromptServer.instance.routes.get("/ocio/lut")
    async def _ocio_lut(request):
        """3D LUT (RGBA8, N^3, raw bytes) baking the input -> output colorspace, for the OCIO Read WebGL
        video viewport. The <video> plays raw and the shader samples this LUT, so colorspace edits show on
        moving video. Same trust level as /ocio/thumb (local single-user tool). X-Lut-Size header carries N.
        """
        from .io_nodes import _lut_rgba8
        q = request.rel_url.query
        in_cs, out_cs = q.get("in_cs", ""), q.get("out_cs", "")
        raw = q.get("raw", "0") == "1"
        try:
            size = max(2, min(65, int(q.get("size", "33"))))
        except (TypeError, ValueError):
            size = 33
        try:
            n, data = _lut_rgba8(in_cs, out_cs, size, raw)
        except Exception as e:
            return web.json_response({"error": str(e)[:250]}, status=400)
        return web.Response(body=data, content_type="application/octet-stream",
                            headers={"X-Lut-Size": str(n), "Cache-Control": "no-store"})

    @server.PromptServer.instance.routes.get("/ocio/stream")
    async def _ocio_stream(request):
        """Stream a local video file for the OCIO Read on-node player (FileResponse handles Range requests,
        so the <video> element can seek). Same trust level as /ocio/thumb (local single-user tool)."""
        from .io_nodes import _input_dir, VIDEO_EXTS
        src = request.rel_url.query.get("src", "")
        p = src if os.path.isabs(src) else os.path.join(_input_dir(), src)
        if not (p and os.path.isfile(p) and os.path.splitext(p)[1].lower() in VIDEO_EXTS):
            return web.json_response({"error": "not a video file"}, status=404)
        return web.FileResponse(p)
except Exception:
    # not inside ComfyUI (e.g. a standalone import) - the routes are simply unavailable.
    pass
