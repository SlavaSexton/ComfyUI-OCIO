"""ComfyUI-OCIO - OpenColorIO nodes for ComfyUI, modelled on Nuke's OCIO node set.

@author Slava Sexton
"""
import os

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .io_nodes import (NODE_CLASS_MAPPINGS as _IO_CLASSES,
                       NODE_DISPLAY_NAME_MAPPINGS as _IO_NAMES)

# Read / Write IO nodes live in io_nodes.py; merge their mappings in.
NODE_CLASS_MAPPINGS.update(_IO_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS.update(_IO_NAMES)

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
except Exception:
    # not inside ComfyUI (e.g. a standalone import) - the routes are simply unavailable.
    pass
