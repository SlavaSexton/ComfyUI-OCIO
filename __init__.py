"""ComfyUI-OCIO - OpenColorIO nodes for ComfyUI, modelled on Nuke's OCIO node set.

@author Slava Sexton
"""
import os

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .io_nodes import (NODE_CLASS_MAPPINGS as _IO_CLASSES,
                       NODE_DISPLAY_NAME_MAPPINGS as _IO_NAMES)
# 2026-07-04: OCIO Grade / Grade Match / Apply Grade DISABLED per owner (not in use yet). The code stays in
# grade_nodes.py; re-enable by uncommenting this import AND the two .update() calls below.
# from .grade_nodes import (NODE_CLASS_MAPPINGS as _GRADE_CLASSES,
#                           NODE_DISPLAY_NAME_MAPPINGS as _GRADE_NAMES)

# Read / Write IO nodes live in io_nodes.py; merge their mappings in.
NODE_CLASS_MAPPINGS.update(_IO_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS.update(_IO_NAMES)

# OCIO Grade / Grade Match / Apply Grade disabled 2026-07-04 (see above) - NOT registered:
# NODE_CLASS_MAPPINGS.update(_GRADE_CLASSES)
# NODE_DISPLAY_NAME_MAPPINGS.update(_GRADE_NAMES)

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

# 2026-07-04 (owner): node display names carry NO version suffix - clean titles ("OCIO Write", not
# "OCIO Write - v1.2.0"). __version__ stays available for the /ocio/version route; the node TYPE (class key)
# is unchanged, so saved workflows and node search are unaffected.


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
        resp = {"path": base, "parent": parent if parent != base else "", "dirs": dirs,
                "files": files, "output_root": os.path.abspath(root)}
        # Sequence mode (owner E1): collapse numbered frames in this folder into ONE entry per name-prefix, so PBR
        # passes (Diffuse.####, Normal.####, Depth.####) each show as a single named sequence with its frame range,
        # Nuke-style. Non-numbered files stay as singles. The entry's src is the first frame; OCIO Read's frame_mode
        # grabs the whole sequence from it.
        if data.get("sequence") and files:
            from .io_nodes import _split_frame
            groups, singles = {}, []
            for e in files:
                sp = _split_frame(e)                       # basename -> (prefix, num, pad, ext, suffix)
                if sp:
                    prefix, num, pad, ext, suffix = sp
                    groups.setdefault((prefix, suffix, ext, pad), []).append((num, e))
                else:
                    singles.append(e)
            seqs = []
            for (prefix, suffix, ext, pad), items in sorted(groups.items()):
                items.sort()
                nums = [n for n, _ in items]
                seqs.append({"label": f"{prefix}{'#' * pad}{suffix}{ext}  [{nums[0]}-{nums[-1]}]  ({len(nums)})",
                             "src": items[0][1]})
            for s in sorted(singles):
                seqs.append({"label": s, "src": s, "single": True})
            resp["seqs"] = seqs
        return web.json_response(resp)

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
        full = request.rel_url.query.get("full", "0") == "1"      # original mode: full-res (proxy = downscaled 512)
        max_side = 8192 if full else 512                          # thumb_frame never upscales, so 8192 = "as-is" up to 8K
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
            rgb = await asyncio.get_event_loop().run_in_executor(None, thumb_frame, src, max_side, frame)
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
        allow_shaper = q.get("float", "0") == "1"          # only the FLOAT OCIO Player asks for the scene-linear log shaper
        try:
            size = max(2, min(65, int(q.get("size", "33"))))
        except (TypeError, ValueError):
            size = 33
        try:
            n, data, shaper = _lut_rgba8(in_cs, out_cs, size, raw, allow_shaper=allow_shaper)
        except Exception as e:
            return web.json_response({"error": str(e)[:250]}, status=400)
        return web.Response(body=data, content_type="application/octet-stream",
                            headers={"X-Lut-Size": str(n), "X-Shaper": "1" if shaper else "0", "Cache-Control": "no-store"})

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

    _PROXY_JOBS = {}   # proxy_path -> subprocess.Popen of the running H.264 transcode (non-blocking)

    @server.PromptServer.instance.routes.get("/ocio/proxy")
    async def _ocio_proxy(request):
        """Resolve a streamable URL for the OCIO Player. A browser-decodable source (h264/vp8/vp9/av1) streams
        directly. A ProRes / DNxHR / MXF (undecodable in a <video>) is transcoded ONCE to a cached H.264 proxy
        (downscaled to 1920) and the proxy is streamed instead. Non-blocking: the first call for an undecodable
        source kicks off the ffmpeg transcode and returns {building:true}; the front end polls until {ready:true}.
        Same local-single-user trust as /ocio/stream. Added 2026-07-03 (owner: ProRes/MXF won't play)."""
        import subprocess as _sp
        from urllib.parse import quote as _quote
        from .io_nodes import _input_dir, VIDEO_EXTS, _needs_proxy, _proxy_path, _proxy_transcode_cmd
        src = request.rel_url.query.get("src", "")
        p = src if os.path.isabs(src) else os.path.join(_input_dir(), src)
        if not (p and os.path.isfile(p) and os.path.splitext(p)[1].lower() in VIDEO_EXTS):
            return web.json_response({"error": "not a video file"}, status=404)

        def _stream_url(x):
            return "/ocio/stream?src=" + _quote(x)
        try:
            if not _needs_proxy(p):
                return web.json_response({"ready": True, "proxy": False, "url": _stream_url(p)})
            proxy = _proxy_path(p)
            job = _PROXY_JOBS.get(proxy)
            if job is not None:                                        # a transcode is (or was) running for this proxy
                rc = job.poll()
                if rc is None:
                    return web.json_response({"ready": False, "building": True})
                _PROXY_JOBS.pop(proxy, None)                          # finished - clear the job slot
                if rc != 0 or not (os.path.isfile(proxy) and os.path.getsize(proxy) > 0):
                    return web.json_response({"ready": False, "error": "proxy transcode failed"}, status=500)
            if os.path.isfile(proxy) and os.path.getsize(proxy) > 0:  # cached (or just-finished) proxy is ready
                return web.json_response({"ready": True, "proxy": True, "url": _stream_url(proxy)})
            try:                                                      # not started yet -> kick off a non-blocking transcode
                _PROXY_JOBS[proxy] = _sp.Popen(_proxy_transcode_cmd(p, proxy),
                                               stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            except Exception as e:
                return web.json_response({"ready": False, "error": str(e)[:200]}, status=500)
            return web.json_response({"ready": False, "building": True})
        except Exception as e:
            return web.json_response({"ready": False, "error": str(e)[:200]}, status=500)

    @server.PromptServer.instance.routes.post("/ocio/write_paths")
    async def _ocio_write_paths(request):
        """The output file(s) an OCIO Write would create for the given params + which already EXIST on disk - drives
        the Render button's overwrite confirmation. Same local single-user trust as /ocio/thumb. Added 2026-07-04."""
        import glob as _glob
        import re as _re
        from .io_nodes import _write_output_paths
        try:
            d = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        root = folder_paths.get_output_directory() if folder_paths else os.getcwd()
        of = str(d.get("output_folder", "") or "").strip()
        folder = root if not of else (of if os.path.isabs(of) else os.path.join(root, of))
        sf = d.get("still_frame")
        sf = int(sf) if (sf is not None and str(sf) != "") else None       # still grabbed from a seq/video -> stamp the frame number
        try:
            first = _write_output_paths(folder, d.get("filename", ""), d.get("container", "sequence"),
                                        d.get("still_format", "exr"), d.get("video_codec", "prores_4444"),
                                        d.get("output_colorspace", ""), bool(d.get("raw_data")),
                                        bool(d.get("colorspace_in_name", True)), int(d.get("start_number", 1) or 1), 1,
                                        still_frame=sf)[0]
        except Exception as e:
            return web.json_response({"error": str(e)[:200]}, status=400)
        existing = []
        if d.get("container") == "sequence":
            pattern = _re.sub(r"\.\d{4}(\.[^.]+)$", r".*\1", first)         # <stem>.0001.exr -> <stem>.*.exr (all existing frames of this stem)
            existing = sorted(p for p in _glob.glob(pattern) if os.path.isfile(p))
        elif os.path.isfile(first):
            existing = [first]
        return web.json_response({"first": first, "existing": existing})

    @server.PromptServer.instance.routes.get("/ocio/floatframe")
    async def _ocio_floatframe(request):
        """One cached HALF-float RGBA frame for the OCIO Player's WebGL float viewport: raw half-float bytes +
        dims in headers, NO color applied (the shader does exposure + the OCIO display transform). Local
        single-user trust, same as /ocio/thumb. Added 2026-07-03."""
        import numpy as np
        q = request.rel_url.query
        d = q.get("dir", "")
        try:
            frame = int(q.get("frame", "0"))
        except (TypeError, ValueError):
            frame = 0
        p = os.path.join(d, f"f.{frame:05d}.npy") if d else ""
        if not (p and os.path.isfile(p)):
            return web.json_response({"error": "frame not found"}, status=404)
        try:
            arr = np.ascontiguousarray(np.load(p))                # float16 [H,W,4]
            h, w = int(arr.shape[0]), int(arr.shape[1])
            return web.Response(body=arr.tobytes(), content_type="application/octet-stream",
                                headers={"X-Width": str(w), "X-Height": str(h), "X-Type": "float16",
                                         "Cache-Control": "no-store"})
        except Exception as e:
            return web.json_response({"error": str(e)[:200]}, status=400)
except Exception:
    # not inside ComfyUI (e.g. a standalone import) - the routes are simply unavailable.
    pass
