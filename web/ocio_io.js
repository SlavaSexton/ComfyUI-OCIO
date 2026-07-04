// ComfyUI-OCIO - front-end helpers for the Read / Write IO nodes.
// @author Slava Sexton
//
//  OCIO Read  : "upload image / sequence" button (single file, or multi-select -> grouped sequence folder);
//               the input colorspace auto-follows the file type (EXR/HDR -> ACEScg, else sRGB - Display).
//  OCIO Write : the output colorspace auto-follows the format (EXR -> ACEScg, PNG/TIFF/video -> sRGB);
//               a "browse output folder" button (server folder picker); a "Render" button (queues the graph);
//               a colorspace label drawn in the title bar (from -> out), so a wrong pick is visible.
import { app } from "../../scripts/app.js";

function W(node, name) { return (node.widgets || []).find((w) => w.name === name); }
function setW(node, name, value) {
    const w = W(node, name);
    if (!w) return;
    if (w.options && Array.isArray(w.options.values) && !w.options.values.includes(value)) {
        w.options.values.push(value);
    }
    w.value = value;
    if (w.callback) try { w.callback(value); } catch (e) {}
    node.setDirtyCanvas(true, true);
}
// set a widget value WITHOUT firing its callback (so an auto-sync isn't mistaken for a manual edit)
function setWSilent(node, name, value) {
    const w = W(node, name);
    if (!w) return;
    w.value = value;
    node.setDirtyCanvas(true, true);
}
function extOf(name) { return (String(name || "").toLowerCase().split(".").pop() || ""); }
function isExr(name) { const e = extOf(name); return e === "exr" || e === "hdr"; }
function shorten(cs) { return String(cs || "").replace(" - Display", "").replace(" - Texture", ""); }

// ---- "Processing…" busy overlay + CSS spinner (owner spec 2026-07-03) -----------------------------------------
// A centered spinner + message painted OVER a preview viewport (OCIO Read + OCIO Player) while a refresh / queue /
// proxy-build is in flight, so the user sees work is happening. Pure CSS spinner (no external gif, self-contained).
// The overlay sits on the viewport box only (position:absolute inset:0), so the timeline + its cache/buffer bar
// stay visible underneath it.
(function _ocioInjectSpinnerCss() {
    if (typeof document === "undefined" || document.getElementById("ocio-spinner-css")) return;
    const s = document.createElement("style"); s.id = "ocio-spinner-css";
    s.textContent = ".ocio-spinner{width:30px;height:30px;border:3px solid rgba(120,140,170,0.25);"
        + "border-top-color:#4cc3ff;border-radius:50%;animation:ocio-spin .8s linear infinite}"
        + "@keyframes ocio-spin{to{transform:rotate(360deg)}}";
    (document.head || document.documentElement).appendChild(s);
})();
function _ocioBusy(box, on, text) {
    if (!box) return;
    let ov = box._ocioBusyEl;
    if (on) {
        if (!ov) {
            ov = document.createElement("div");
            ov.style.cssText = "position:absolute;inset:0;z-index:9;display:flex;flex-direction:column;"
                + "align-items:center;justify-content:center;gap:10px;background:rgba(8,10,14,0.5);"
                + "color:#dfe8f2;font:12px sans-serif;pointer-events:none;text-shadow:0 1px 3px rgba(0,0,0,0.8);";
            const sp = document.createElement("div"); sp.className = "ocio-spinner";
            const tx = document.createElement("div"); tx.className = "ocio-busy-txt";
            ov.append(sp, tx); box.appendChild(ov); box._ocioBusyEl = ov;
        }
        ov.lastChild.textContent = text || "Processing…";
        ov.style.display = "flex";
    } else if (ov) {
        ov.style.display = "none";
    }
}
// the viewport box of either OCIO node (Player or Read), for the busy overlay
function _ocioNodeBox(node) { return node && ((node._ocioPlayer && node._ocioPlayer.box) || (node._ocioPrev && node._ocioPrev.box)) || null; }
function _ocioBusyNode(node, on, text) { _ocioBusy(_ocioNodeBox(node), on, text); }

const CS_SRGB = "sRGB - Display";
const CS_ACESCG = "ACEScg";
function autoInCs(filename) { return isExr(filename) ? CS_ACESCG : CS_SRGB; }
function autoOutCs(container, stillFormat) {
    if (container === "video") return CS_SRGB;
    return stillFormat === "exr" ? CS_ACESCG : CS_SRGB;
}

// bit-depth options + default per still format
const BITS = { exr: ["16f", "32f"], tiff: ["8", "16", "32f"], png: ["8", "16"], jpeg: ["8"] };
const BIT_DEF = { exr: "16f", tiff: "16", png: "8", jpeg: "8" };
const STILL_EXT = { exr: "exr", tiff: "tif", png: "png", jpeg: "jpg" };

// video codec -> real bit depth + extension (mirrors io_nodes.py save_video's codec->pix_fmt map). bit_depth
// stays hidden for video (still-format 16f/32f/16/8 don't map to video 8/10/12) - this footer shows the real,
// codec-fixed depth instead.
const CODEC_INFO = {
    prores_4444: { bits: "12-bit", ext: ".mov" }, prores_422hq: { bits: "10-bit", ext: ".mov" },
    prores_422: { bits: "10-bit", ext: ".mov" }, dnxhr_hq: { bits: "8-bit", ext: ".mov" },
    h264: { bits: "8-bit", ext: ".mp4" }, hevc: { bits: "8-bit", ext: ".mp4" },
};
const CODEC_LABEL = {
    prores_4444: "ProRes 4444", prores_422hq: "ProRes 422 HQ", prores_422: "ProRes 422",
    dnxhr_hq: "DNxHR HQ", h264: "H.264", hevc: "HEVC",
};

// hide / show ONE widget with a TRUE collapse (no blank row). 2026-07-04: switched off the old OCIO_HIDDEN
// type-swap - it left a blank row on Vue-nodes frontends (they drop a row only via options.hidden / v-if, not a
// type change) AND risked blanking the widget value on serialize. Now identical to setVisibleWidgets' per-widget
// logic: widget.hidden + options.hidden (dual-set - canvas reads .hidden, Vue reads options.hidden) + a zeroed
// computeSize, NO type swap (so the value keeps serializing). Used by OCIO Write's per-container visibility.
function showWidget(node, w, visible) {
    if (!w) return;
    if (!w.options) w.options = {};
    if (visible) {
        w.hidden = false; w.options.hidden = false;
        if (w._ocioCompute) { w.computeSize = w._ocioCompute; delete w._ocioCompute; }
    } else {
        w.hidden = true; w.options.hidden = true;
        if (!w._ocioCompute) w._ocioCompute = w.computeSize;
        w.computeSize = () => [0, 0];
    }
}

// OCIO Read only: true collapse of hidden widgets, no blank row - WITHOUT removing them from node.widgets.
//
// IMPORTANT (confirmed against ComfyUI_frontend source, src/utils/executionUtil.ts graphToPrompt): Queue
// Prompt serializes a node's inputs by iterating node.widgets LIVE, by widget name, at the moment the graph
// is queued - there is no separate positional widgets_values cache that survives a widget being spliced out.
// OCIORead declares every field ("required" in INPUT_TYPES, io_nodes.py) with no backend-side fallback for a
// missing prompt key, so physically removing a widget from node.widgets (the first design tried here) would
// drop that field from the /prompt payload and fail prompt validation the moment the hidden kind is queued -
// confirmed as the wrong mechanism, not used.
//
// Instead this sets widget.hidden = true (litegraph's own visibility flag, not a fake type-swap) and a
// zeroed computeSize, leaving the widget in node.widgets (so it keeps serializing) while excluding it from
// layout. node._ocioAllWidgets is the full ordered widget list captured once, right after every
// widget/button/DOM-widget is added in onNodeCreated - kept so the ORDER is stable across visibility changes
// (node.widgets itself is never reordered, only each widget's hidden/computeSize is toggled in place).
function setVisibleWidgets(node, isVisible) {
    if (!node._ocioAllWidgets) return;
    for (const w of node._ocioAllWidgets) {
        const visible = isVisible(w);
        if (!w.options) w.options = {};
        if (visible) {
            w.hidden = false;
            w.options.hidden = false;                       // Vue-nodes read options.hidden; canvas reads .hidden
            if (w._ocioCompute) { w.computeSize = w._ocioCompute; delete w._ocioCompute; }
        } else {
            w.hidden = true;
            w.options.hidden = true;                        // dual-set so Vue drops the row (v-if), no blank gap
            if (!w._ocioCompute) w._ocioCompute = w.computeSize;
            w.computeSize = () => [0, 0];
        }
    }
    pokeWidgets(node);                                  // Vue re-render (see pokeWidgets below)
    node.setSize([node.size[0], node.computeSize()[1]]);
    node.setDirtyCanvas(true, true);
}

// Vue-nodes frontends (ComfyUI 1.45+ new node UI) re-read the widget list only on a REAL array mutation:
// property changes on the raw widget objects (type/label/hidden) are not reactive, and reassigning
// node.widgets breaks the binding entirely. A pop+push of the same tail element is the minimal mutation
// that forces the re-render which applies our type-swap hides and label changes. Verified live on 1.45.15.
function pokeWidgets(node) {
    if (node.widgets && node.widgets.length) { const d = node.widgets.pop(); node.widgets.push(d); }
}

// the "_colorspace" the Write node injects before the frame number (mirrors io_nodes.py _cs_tag)
const CS_TAG_RULES = [
    ["acescct", "acescct"], ["acescc", "acescc"], ["acescg", "acescg"], ["aces2065", "aces2065"],
    ["logc", "logc"], ["canon log", "clog"], ["clog", "clog"], ["slog", "slog"], ["v-log", "vlog"], ["vlog", "vlog"],
    ["rec.2020", "rec2020"], ["rec2020", "rec2020"],
    ["rec.709", "rec709"], ["rec709", "rec709"], [" 709", "rec709"],
    ["display p3", "p3"], ["p3-d", "p3"], ["p3", "p3"],
    ["srgb", "srgb"],
    ["linear", "linear"],
];
function csCore(name) {
    const low = (name || "").toLowerCase();
    for (const [needle, tag] of CS_TAG_RULES) if (low.includes(needle)) return tag;
    return low.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 24);
}
function csTag(node) {
    if (!(W(node, "colorspace_in_name")?.value)) return "";
    if (W(node, "raw_data")?.value) return "_raw";
    const cs = csCore(W(node, "output_colorspace")?.value || "");
    return cs ? "_" + cs : "";
}

// the output filename example shown on the Write node
function exampleName(node) {
    const name = (W(node, "filename")?.value || "ocio_out").trim() || "ocio_out";
    const t = csTag(node);
    const c = W(node, "container")?.value;
    if (c === "video") {
        const v = W(node, "video_codec")?.value || "";
        return name + t + (v.startsWith("prores") || v.startsWith("dnxhr") ? ".mov" : ".mp4");
    }
    const ext = STILL_EXT[W(node, "still_format")?.value] || "exr";
    if (c === "still image") return `${name}${t}.${ext}`;
    const s = W(node, "start_number")?.value ?? 1;
    const pad = (n) => String(n).padStart(4, "0");
    return `${name}${t}.${pad(s)}.${ext}, ${name}${t}.${pad(s + 1)}.${ext} ...`;
}

// ---- instant on-node preview (OCIO Read): a DOM widget (addDOMWidget renders on Vue and legacy frontends
// alike; node.imgs / canvas draws do not on Vue). Still / sequence: an <img> from /ocio/thumb (server render,
// so EXR works and the input -> output colorspace is applied). Video: a WebGL2 viewport - a hidden <video>
// plays the raw file (/ocio/stream) and a <canvas> shader samples each frame through a 3D LUT baked from the
// same input -> output transform (/ocio/lut), so a MOVING video reacts to a colorspace change. The browser
// cannot apply OCIO to a <video> itself; the LUT is the bridge. Its input is the browser-decoded (display
// 8-bit) frame, so the viewport is transform-accurate but input-approximate - the reference-exact path stays
// the still /ocio/thumb. No WebGL2, or the stream / LUT failing, falls back to the static thumb frame.
const _VP_VERT = `#version 300 es
in vec2 p; out vec2 uv;
void main(){ uv = vec2(p.x * 0.5 + 0.5, 0.5 - p.y * 0.5); gl_Position = vec4(p, 0.0, 1.0); }`;
const _VP_FRAG = `#version 300 es
precision highp float; precision highp sampler3D;
in vec2 uv; out vec4 o;
uniform sampler2D uVid; uniform sampler3D uLut; uniform float uN; uniform float uOn;
void main(){
  vec3 c = texture(uVid, uv).rgb;
  if (uOn > 0.5) { vec3 s = c * ((uN - 1.0) / uN) + 0.5 / uN; c = texture(uLut, s).rgb; }
  o = vec4(c, 1.0);
}`;
function _vpCompile(gl, type, src) {
    const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { console.error("[OCIO] shader:", gl.getShaderInfoLog(s)); return null; }
    return s;
}
// Known media extensions the OCIO Read viewport can actually decode (stills, sequence frames, video). A source
// whose extension is not here (a .txt, a code file, an unknown container) is surfaced as "No media - unsupported
// format" up front, instead of firing a 404/400 that blanks the box silently. Added 2026-07-03 (Task F).
const READ_STILL_EXTS = new Set(["exr", "hdr", "tif", "tiff", "png", "jpg", "jpeg", "bmp", "dpx"]);
const READ_VIDEO_EXTS = new Set(["mov", "mp4", "mkv", "avi", "webm", "mxf", "m4v"]);
function isKnownMediaPath(src) {
    const s = String(src || "").trim();
    if (!s) return false;
    if (/[\\/]$/.test(s)) return true;                 // a folder path (sequence dir) - the server resolves the frames
    const e = extOf(s);
    return READ_STILL_EXTS.has(e) || READ_VIDEO_EXTS.has(e);
}
// show / hide the "No media" placeholder in the Read preview box (hides the img/video/canvas while it is up).
function _showReadMsg(p, text) {
    if (!p || !p.msg) return;
    p.msg.textContent = text || "No media - unsupported format";
    p.msg.style.display = "";
    p.img.style.display = "none"; p.video.style.display = "none"; p.canvas.style.display = "none";
}
function _hideReadMsg(p) { if (p && p.msg) p.msg.style.display = "none"; }
// Fully blank the preview <img>: hide it, drop the src, AND reset the fill sizing back to intrinsic. Without the
// size reset a no-src <img> that still carries width/height:100% (from a prior onload) renders the broken-image
// icon. Use this everywhere the src is cleared. Added 2026-07-03 (fix: broken viewport on empty / guarded source).
function _blankReadImg(p) {
    if (!p || !p.img) return;
    p.img.style.display = "none"; p.img.style.width = ""; p.img.style.height = ""; p.img.removeAttribute("src");
}
// Preview/viewport height that SCALES with the node width, keeping the media's aspect - so stretching the node
// stretches the image instead of pinning it to a fixed-height letterbox (like the native Load Image node). p.aspect =
// mediaW/mediaH, set when media loads (default 16:9 until known); clamped so it never collapses or runs away.
// Shared by OCIO Read (preview) and OCIO Player (viewport).
function _previewH(node, p, width) {
    const aspect = (p && p.aspect && isFinite(p.aspect) && p.aspect > 0.05) ? p.aspect : (16 / 9);
    const w = (width && width > 0) ? width : ((node.size && node.size[0]) || 300);
    return Math.max(120, Math.min(2000, Math.round(w / aspect)));
}
// Learn the media's aspect (from a decoded still / video / float frame) and refit the node once, so the viewport
// keeps the real proportions and the image scales with the node width. Skips unchanged aspects (every seq frame
// reports the same one -> no resize churn) and guards against the setSize -> onResize -> _adoptAspect recursion.
function _adoptAspect(node, p, mw, mh) {
    if (!p || !(mw > 0) || !(mh > 0)) return;
    const a = mw / mh;
    if (Math.abs((p.aspect || 0) - a) < 0.002) return;
    p.aspect = a;
    if (p._aspectFitting) return;
    p._aspectFitting = true;
    try { node.setSize([node.size[0], node.computeSize()[1]]); } finally { p._aspectFitting = false; }
}
// R1 self-determining output: OCIO Read/Player always declare BOTH an IMAGE and a VIDEO output (backend
// RETURN_TYPES), but we SHOW the VIDEO slot only when the loaded content is a video, and hide it otherwise - so a
// still/sequence looks IMAGE-only and a video exposes the VIDEO output. VIDEO is the LAST output, so add/remove it
// never shifts the IMAGE/MASK/FLOAT/STRING indices (backend maps by index). A wired VIDEO slot is kept (don't yank a
// saved / user connection). Owner spec 2026-07-03 (R1; the true single self-determining slot would be a V3 MatchType port).
function _setVideoOutput(node, show) {
    if (node && node.type === "OCIOPlayer") return;   // 2026-07-04: the Player is INPUT-ONLY (no outputs), so never add/remove a VIDEO slot on it
    if (!node || !node.outputs) return;
    let idx = -1;
    for (let i = 0; i < node.outputs.length; i++) if (node.outputs[i].type === "VIDEO") { idx = i; break; }
    if (show) {
        if (idx < 0) { node.addOutput("video", "VIDEO"); node.setDirtyCanvas(true, true); }
    } else if (idx >= 0) {
        const o = node.outputs[idx];
        if (!(o.links && o.links.length)) { node.removeOutput(idx); node.setDirtyCanvas(true, true); }   // keep a connected slot
    }
}
function ensureReadPreview(node) {
    if (node._ocioPrev) return node._ocioPrev;
    const box = document.createElement("div");
    box.style.cssText = "width:100%;height:100%;position:relative;display:flex;justify-content:center;align-items:center;overflow:hidden;";
    const img = document.createElement("img");
    img.style.cssText = "max-width:100%;max-height:100%;object-fit:contain;display:none;";   // default INTRINSIC sizing so an empty / pending / broken src shows NOTHING (not a broken-image icon); onload switches to 100%/100% so a small proxy still upscales to fill the node
    // a still/frame that fails to decode (server 404/400, or a non-media path that got through) shows the readable
    // "No media" message instead of a blank box. Added 2026-07-03 (Task F: format guard).
    img.onerror = () => { _ocioBusy(node._ocioPrev && node._ocioPrev.box, false); img.style.display = "none"; img.style.width = ""; img.style.height = ""; _showReadMsg(node._ocioPrev, "No media - unsupported format"); };   // reset to intrinsic sizing so a later empty state cannot show a broken-image icon
    img.onload = () => { _ocioBusy(node._ocioPrev && node._ocioPrev.box, false); img.style.width = "100%"; img.style.height = "100%"; _adoptAspect(node, node._ocioPrev, img.naturalWidth, img.naturalHeight); };   // valid image loaded -> fill (upscale a small proxy) + learn aspect so the node refits
    const video = document.createElement("video");
    video.muted = true; video.loop = true; video.playsInline = true; video.setAttribute("playsinline", "");
    video.style.display = "none";
    video.addEventListener("loadedmetadata", () => { _ocioBusy(node._ocioPrev && node._ocioPrev.box, false); _adoptAspect(node, node._ocioPrev, video.videoWidth, video.videoHeight); });
    const canvas = document.createElement("canvas");
    canvas.style.cssText = "width:100%;height:100%;object-fit:contain;display:none;";   // FILL the box (see img note): scale the color-managed video to the node size
    const msg = document.createElement("div");   // "No media - unsupported format" placeholder (hidden by default)
    msg.style.cssText = "display:none;color:#889;font:12px sans-serif;text-align:center;padding:24px;";
    // Proxy / Original tag (top-left, faint): the preview is a downscaled 512px PROXY by default (fast); click to
    // read the source at full resolution (ORIGINAL, as-is), click again to go back. Owner spec 2026-07-03.
    const proxyTag = document.createElement("div");
    proxyTag.textContent = "proxy";
    proxyTag.title = "Preview resolution - proxy (fast, 512px) or original (full-res, as-is). Click to toggle.";
    proxyTag.style.cssText = "position:absolute;top:3px;left:5px;z-index:4;font:10px sans-serif;color:rgba(180,200,220,0.5);cursor:pointer;user-select:none;text-shadow:0 1px 2px rgba(0,0,0,0.85);";
    proxyTag.onclick = () => {
        const pp = node._ocioPrev; if (!pp) return;
        pp.original = !pp.original;                       // proxy (512px) <-> original (full-res)
        proxyTag.textContent = pp.original ? "original" : "proxy";
        proxyTag.style.color = pp.original ? "rgba(120,230,170,0.85)" : "rgba(180,200,220,0.5)";
        _seqClearCache(pp);                              // resolution changed -> cached frames are stale
        if (pp.seq) _seqShow(pp); else updateReadPreview(node);   // re-fetch the current frame at the new resolution
    };
    box.append(img, video, canvas, msg, proxyTag);
    const w = node.addDOMWidget("preview", "div", box, { serialize: false });
    w.computeSize = (width) => [0, node._ocioReadCollapsed ? 0 : _previewH(node, node._ocioPrev, width)];   // scale with node width (aspect-locked); 0 when the Viewer is collapsed
    w._ocioAlwaysVisible = true;                      // always shown, regardless of source kind
    node._ocioPrev = { box, img, video, canvas, msg, gl: null, lutN: 33, lutReady: false, raf: 0, streamUrl: "" };
    node._ocioPrev.pb = { playing: false, dir: 1, mode: "loop", fps: 24, showTransport: false, lastT: 0 };
    _ensureTransport(node, node._ocioPrev);           // transport bar widget (sits under the canvas, video only)
    node.onRemoved = (orig => function () { const pp = node._ocioPrev; _stopViewport(pp); _stopSeq(pp); if (pp && pp.audio) { try { pp.audio.source.disconnect(); pp.audio.gain.disconnect(); pp.audio.splitter.disconnect(); } catch (e) {} } return orig && orig.apply(this, arguments); })(node.onRemoved);
    return node._ocioPrev;
}
function _vpInitGL(p) {
    if (p.gl) return p.gl;
    const gl = p.canvas.getContext("webgl2", { premultipliedAlpha: false, antialias: false, preserveDrawingBuffer: true });
    if (!gl) return null;
    const vs = _vpCompile(gl, gl.VERTEX_SHADER, _VP_VERT), fs = _vpCompile(gl, gl.FRAGMENT_SHADER, _VP_FRAG);
    if (!vs || !fs) return null;
    const prog = gl.createProgram(); gl.attachShader(prog, vs); gl.attachShader(prog, fs);
    gl.bindAttribLocation(prog, 0, "p"); gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { console.error("[OCIO] link:", gl.getProgramInfoLog(prog)); return null; }
    const quad = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);   // one oversized tri
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    const vidTex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, vidTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const lutTex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_3D, lutTex);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
    gl.useProgram(prog);
    const locs = { uN: gl.getUniformLocation(prog, "uN"), uOn: gl.getUniformLocation(prog, "uOn") };
    gl.uniform1i(gl.getUniformLocation(prog, "uVid"), 0); gl.uniform1i(gl.getUniformLocation(prog, "uLut"), 1);
    p.gl = { gl, prog, locs, vidTex, lutTex };
    return p.gl;
}
async function _refreshVideoLut(node, p) {
    const g = p.gl; if (!g) return;
    const q = new URLSearchParams({ in_cs: W(node, "input_colorspace")?.value || "", out_cs: W(node, "output_colorspace")?.value || "",
        raw: W(node, "raw_data")?.value ? "1" : "0", size: "33" });
    try {
        const r = await fetch("/ocio/lut?" + q.toString()); if (!r.ok) throw new Error("lut " + r.status);
        const n = parseInt(r.headers.get("X-Lut-Size") || "33", 10); const buf = new Uint8Array(await r.arrayBuffer());
        const gl = g.gl; gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_3D, g.lutTex);
        gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGBA8, n, n, n, 0, gl.RGBA, gl.UNSIGNED_BYTE, buf);
        p.lutN = n; p.lutReady = true;
    } catch (e) { console.error("[OCIO] lut fetch:", e); p.lutReady = false; }
}
function _drawViewport(p) {
    const g = p.gl; if (!g) return; const gl = g.gl, v = p.video;
    if (!(v.videoWidth > 0) || v.readyState < 2) return;
    // PROXY (Nuke / Vimeo style): a 4K frame uploaded to a GPU texture every rAF stalls playback. Downscale
    // the frame to <=720p on a 2D canvas first (drawImage is GPU-accelerated), so the per-frame upload is small.
    let src = v, sw = v.videoWidth, sh = v.videoHeight;
    const cap = 1280;
    if (Math.max(sw, sh) > cap) {
        const s = cap / Math.max(sw, sh), pw = Math.max(1, Math.round(sw * s)), ph = Math.max(1, Math.round(sh * s));
        if (!p.proxy) { p.proxy = document.createElement("canvas"); p.proxyCtx = p.proxy.getContext("2d"); }
        if (p.proxy.width !== pw || p.proxy.height !== ph) { p.proxy.width = pw; p.proxy.height = ph; }
        try { p.proxyCtx.drawImage(v, 0, 0, pw, ph); } catch (e) { return; }
        src = p.proxy; sw = pw; sh = ph;
    }
    if (p.canvas.width !== sw || p.canvas.height !== sh) { p.canvas.width = sw; p.canvas.height = sh; }
    gl.viewport(0, 0, p.canvas.width, p.canvas.height);
    gl.useProgram(g.prog);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, g.vidTex);
    try { gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, src); } catch (e) { return; }
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_3D, g.lutTex);
    gl.uniform1f(g.locs.uN, p.lutN || 33); gl.uniform1f(g.locs.uOn, p.lutReady ? 1 : 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
}
function _ensureRaf(node, p) {                          // one rAF loop drives both the video viewport and the seq flipbook
    if (p.raf) return;
    const loop = (now) => {
        if (node._ocioPrev !== p) { p.raf = 0; return; }
        if ((node.mode === 2 || node.mode === 4) && p.seqCache && p.seqCache.size) _seqClearCache(p);   // muted / bypassed -> drop the decoded-frame blob cache
        _tickPlayback(p, now || 0);
        if (!p.pb.seqMode) _drawViewport(p);           // a sequence shows its color-managed thumb in <img> (no WebGL)
        _syncTransport(p);
        _drawAudioMeter(p);                            // stereo L/R level meter (video only; self-guards for seq)
        p.raf = requestAnimationFrame(loop);
    };
    p.raf = requestAnimationFrame(loop);
}
function _startViewport(node, p, src) {
    p.pb.seqMode = false;                              // leaving any image-sequence mode
    // 1-based video numbering on THIS Read's own timeline: mirror its frame_shift / start_frame (1 for a plain video)
    const _rs = Math.round(W(node, "frame_shift")?.value || 0), _st = Math.round(W(node, "start_frame")?.value || (node._ocioSeq && node._ocioSeq.start) || 0);
    p.videoBase = _rs > 0 ? _rs : (_st > 0 ? _st : 1);
    if (p.streamPath !== src) {
        p.streamPath = src; p.video.loop = false; p.pb.playing = false; p.pb.dir = 1; p.pb.revAnchor = null;
        p.video.onloadeddata = () => { if (!p.pb.playing) { try { p.video.pause(); p.video.currentTime = 0; } catch (e) {} } };   // load paused, but never fight an explicit play
        // Reverse plays by seeking a PAUSED <video> backward frame by frame; the browser only paints a seeked frame
        // once the seek settles, and the rAF _drawViewport skips while readyState dips mid-seek - so the viewport
        // looked frozen while the playhead moved. Draw on every completed seek so reverse (and any scrub) updates.
        p.video.onseeked = () => { if (!p.pb.seqMode) _drawViewport(p); };
        p.video.onerror = () => { _ocioBusy(p.box, false); _stopViewport(p); _showReadMsg(p, "No media - unsupported format"); };   // decode failed: readable message, not a blank box (Task F)
        // 2026-07-03: resolve through /ocio/proxy so the Read preview plays ProRes / DNxHR / MXF too (was streaming
        // the raw file -> browser could not decode -> "No media"). Browser codec = direct; else an H.264 proxy.
        _resolveStreamUrl(p.box, src, () => node._ocioPrev !== p || p.streamPath !== src).then((url) => {
            if (url == null) return;
            p.streamUrl = url; p.video.src = url;
        });
    }
    if (!_vpInitGL(p)) {                               // no WebGL2 -> static color-managed thumb fallback
        p.canvas.style.display = "none"; p.video.style.display = "none";
        p.img.src = "/ocio/thumb?" + _thumbQuery(node, src); p.img.style.display = ""; return;
    }
    p.img.style.display = "none"; p.video.style.display = "none"; p.canvas.style.display = "";
    p.pb.fps = parseFloat(W(node, "fps")?.value) || p.pb.fps || 24;
    p.pb.showTransport = true; if (p.transport) { p.transport.bar.style.display = "flex"; p.transport.audioRow.style.display = "flex"; }   // audio meter: video only
    node.setSize([node.size[0], node.computeSize()[1]]);
    _refreshVideoLut(node, p);
    _ensureRaf(node, p);
}
// ---- Sequence flipbook player (image sequences: EXR / TIFF / PNG frames). No <video>; the transport bar drives a
// frame-index clock, and each frame is the server's OCIO-correct /ocio/thumb (in_cs -> out_cs applied server-side),
// so live colorspace changes are exact - the whole point of a color node. Heavy 4K EXR frames cannot decode in real
// time from cold, so the [in,out] range is prefetched into a client blob cache (a Nuke/RV-style flipbook); playback
// runs from that cache. Frame numbers <-> 0-based index via _seqBase (orig_start). Added 2026-07-03.
function _seqCsSig(node) {
    return (W(node, "input_colorspace")?.value || "") + "|" + (W(node, "output_colorspace")?.value || "") +
           "|" + (W(node, "raw_data")?.value ? "1" : "0");
}
function _seqUrl(p, idx) {
    const node = p.node, base = (p.seq.origStart | 0);
    return "/ocio/thumb?" + new URLSearchParams({
        src: p.seq.src, frame: String(base + (idx | 0)),
        in_cs: W(node, "input_colorspace")?.value || "", out_cs: W(node, "output_colorspace")?.value || "",
        raw: W(node, "raw_data")?.value ? "1" : "0",
        full: p.original ? "1" : "0",                    // original = full-res thumb, proxy = 512px
    }).toString();
}
function _seqClearCache(p) {
    if (p.seqCache) { for (const u of p.seqCache.values()) { try { URL.revokeObjectURL(u); } catch (e) {} } p.seqCache.clear(); }
    if (p.seqInflight) p.seqInflight.clear();
}
async function _seqFetch(p, idx, show) {
    if (!p.seqCache) p.seqCache = new Map();
    if (!p.seqInflight) p.seqInflight = new Set();
    const last = _pbLast(p); idx = Math.max(0, Math.min(last, idx | 0));
    if (p.seqCache.has(idx)) { if (show && (p.pb.seqFrame | 0) === idx) { const u = p.seqCache.get(idx); if (p.img.src !== u) p.img.src = u; } return; }
    if (p.seqInflight.has(idx)) return;
    p.seqInflight.add(idx);
    try {
        const r = await fetch(_seqUrl(p, idx));
        if (!r.ok) throw new Error("thumb " + r.status);
        const obj = URL.createObjectURL(await r.blob());
        if (!p.seqCache) { URL.revokeObjectURL(obj); return; }       // viewport torn down mid-fetch
        p.seqCache.set(idx, obj);
        if (show && (p.pb.seqFrame | 0) === idx) { p.img.style.display = ""; p.img.src = obj; }   // still the current frame
    } catch (e) { /* missing / failed frame: keep the previous image, do not blank */ }
    finally { p.seqInflight.delete(idx); }
}
// SHARED (OCIO Read + OCIO Player). The transport's seek/scrub/step all route through _pbSeek -> _seqShow.
// OCIO Read's state has p.seq (an <img> flipbook); OCIO Player's has p.player (a float WebGL frame) and no p.seq.
// Delegate to the float uploader when this is a Player - OCIO Read's p never has .player, so its path is unchanged.
function _seqShow(p) {
    if (p.player) { _playerShow(p); return; }        // OCIO Player: upload the float frame to the GPU
    if (p.seq) _seqFetch(p, p.pb.seqFrame | 0, true);
}
function _seqPrefetch(p) {                                            // warm the [in,out] range into the blob cache
    const inI = _pbIn(p), outI = _pbOut(p), CAP = 300;               // bound the burst + client blob memory (each thumb decodes a full EXR); range beyond CAP fetches on demand during playback
    const hi = Math.min(outI, inI + CAP - 1);
    if (outI - inI + 1 > CAP) console.warn(`[OCIO] sequence prefetch capped at ${CAP} frames (range ${inI}-${outI})`);
    let i = inI;
    const pump = () => { if (!p.pb.seqMode || i > hi) return; const idx = i++; _seqFetch(p, idx, false).then(pump, pump); };
    pump(); pump();                                                  // 2 pumps; server decodes serially anyway
}
function _seqTick(p, now) {
    const pb = p.pb; if (!pb.playing) return;
    const inI = _pbIn(p), outI = _pbOut(p), span = Math.max(1, outI - inI + 1);
    const fps = Math.max(1, parseFloat(W(p.node, "fps")?.value) || pb.fps || 24);
    if (!pb.seqAnchor) pb.seqAnchor = { wall: now, frame: Math.max(inI, Math.min(outI, pb.seqFrame | 0)) };
    const steps = Math.floor(((now - pb.seqAnchor.wall) / 1000) * fps) * (pb.dir < 0 ? -1 : 1);
    const raw = pb.seqAnchor.frame + steps;
    let idx;
    if (pb.mode === "bounce") {
        const period = Math.max(1, 2 * span - 2), ph = (((raw - inI) % period) + period) % period;
        idx = inI + (ph < span ? ph : period - ph);
    } else {
        idx = inI + ((((raw - inI) % span) + span) % span);         // loop within [in,out]
    }
    if (idx !== (pb.seqFrame | 0)) { pb.seqFrame = idx; _seqShow(p); }
}
function _stopSeq(p) {
    if (!p || !p.pb) return;
    p.pb.seqMode = false; p.pb.playing = false; p.pb.seqAnchor = null;
    p.pb.showTransport = false; if (p.transport) p.transport.bar.style.display = "none";
    _seqClearCache(p); p.seq = null;
}
function _startSeqViewport(node, p, src, seq) {
    _stopViewport(p);                                                // ensure the video path is off (pauses <video>, cancels rAF)
    const origStart = (seq.orig_start != null ? seq.orig_start : (seq.start != null ? seq.start : 0)) | 0;
    const count = Math.max(1, seq.count | 0);
    const csSig = _seqCsSig(node);
    if (p.seq && (p.seq.src !== src || p.seqCsSig !== csSig)) _seqClearCache(p);   // source or colorspace changed -> stale cache
    if (!p.seq || p.seq.src !== src) p.pb.seqFrame = 0;              // new clip starts at frame 0
    p.seq = { src, origStart, count }; p.seqCsSig = csSig;
    p.pb.seqMode = true; p.pb.playing = false; p.pb.dir = 1; p.pb.seqAnchor = null;
    p.pb.fps = parseFloat(W(node, "fps")?.value) || p.pb.fps || 24;
    p.pb.fileFrames = count;
    p.pb.seqFrame = Math.max(0, Math.min(count - 1, p.pb.seqFrame | 0));
    p.canvas.style.display = "none"; p.video.style.display = "none"; p.img.style.display = "";
    p.pb.showTransport = true; if (p.transport) { p.transport.bar.style.display = "flex"; p.transport.audioRow.style.display = "none"; }   // sequences have no audio
    node.setSize([node.size[0], node.computeSize()[1]]);
    _seqShow(p);                                                     // current frame now
    _seqPrefetch(p);                                                 // warm the rest of the range
    _ensureRaf(node, p);
}
function _stopViewport(p) {
    if (!p) return;
    if (p.raf) { cancelAnimationFrame(p.raf); p.raf = 0; }
    try { p.video.pause(); } catch (e) {}
    if (p.pb) { p.pb.playing = false; p.pb.showTransport = false; }
    if (p.transport) p.transport.bar.style.display = "none";
    p.canvas.style.display = "none"; p.video.style.display = "none"; p.streamUrl = "";
}
function _thumbQuery(node, src) {
    return new URLSearchParams({ src, in_cs: W(node, "input_colorspace")?.value || "", out_cs: W(node, "output_colorspace")?.value || "",
        raw: W(node, "raw_data")?.value ? "1" : "0", full: node._ocioPrev?.original ? "1" : "0", rand: String(Date.now()) }).toString();
}
// ---- Nuke-style transport bar for the video viewport (client-side, drives the hidden <video>; the WebGL loop
// renders whatever frame it lands on). A numbered timeline ruler (0..fileFrames-1) with a draggable playhead and
// draggable in / out handles, plus: Repeat / Bounce, set-in (I) / set-out (O) to the current frame, go to first /
// last, play reverse / forward (single triangles), step one frame (triangle + bar), stop, and a frame field. The
// in / out handles ARE the node's start_frame / end_frame widgets (single source of truth): dragging a handle or
// clicking I / O edits the field, editing the field moves the handle, and playback loops or bounces inside
// [in, out]. The frame count is the REAL file's (from seq_range), so it always matches the loaded clip. Video
// source only.
// Every glyph uses ONLY fillable shapes (triangles as closed paths, bars as <rect>): the svg has
// fill=currentColor and no stroke, so a zero-width line bar renders NOTHING - that is why the bars were
// invisible and every button looked like a plain triangle. Stroked glyphs (reset / I / O) set their own stroke.
const _SVG = {
    reset:   '<path fill="none" stroke="currentColor" stroke-width="1.4" d="M12.4 6.2A4.2 4.2 0 1 0 13 9.6"/><path d="M13 2.4v3.4h-3.4z"/>',  // reset range to full clip
    setIn:   '<path fill="none" stroke="currentColor" stroke-width="1.7" d="M4 3.5h5M4 12.5h5M6.5 3.5v9"/>',   // I  set in point
    setOut:  '<circle cx="8" cy="8" r="4.2" fill="none" stroke="currentColor" stroke-width="1.7"/>',           // O  set out point
    first:   '<rect x="2.4" y="3" width="1.8" height="10"/><path d="M14 3l-4.7 5 4.7 5zM9.2 3l-4.7 5 4.7 5z"/>',   // |<< go to first (bar + 2 tri)
    last:    '<path d="M2 3l4.7 5-4.7 5zM6.8 3l4.7 5-4.7 5z"/><rect x="11.8" y="3" width="1.8" height="10"/>',     // >>| go to last
    stepB:   '<path d="M10 3l-6.5 5 6.5 5z"/><rect x="11" y="3" width="1.8" height="10"/>',                       // <|  step back one (1 tri + bar)
    stepF:   '<path d="M6 3l6.5 5-6.5 5z"/><rect x="3.2" y="3" width="1.8" height="10"/>',                        // |>  step forward one
    playR:   '<path d="M12 3l-9 5 9 5z"/>',                                                                       // <   play reverse
    play:    '<path d="M4 3l9 5-9 5z"/>',                                                                         // >   play forward
    pause:   '<path d="M4 3h3v10H4zM9 3h3v10H9z"/>',
    stop:    '<rect x="4" y="4" width="8" height="8"/>',                                                          // stop (pause in place)
    soundOff:'<path d="M2.5 6.2H5L8 3.5v9L5 9.8H2.5z"/><path fill="none" stroke="currentColor" stroke-width="1.3" d="M10.5 6.3l3.2 3.4M13.7 6.3l-3.2 3.4"/>',   // speaker + X (muted)
    soundOn: '<path d="M2.5 6.2H5L8 3.5v9L5 9.8H2.5z"/><path fill="none" stroke="currentColor" stroke-width="1.2" d="M10.4 6a3 3 0 0 1 0 4M12.2 4.4a5 5 0 0 1 0 7.2"/>',   // speaker + waves (on)
};
function _tBtn(icon, title) {
    const b = document.createElement("button"); b.title = title; b.dataset.icon = "1";
    b.style.cssText = "width:17px;height:16px;padding:0;margin:0;border:0;border-radius:2px;background:#2b2b30;color:#e0e8f0;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto;";
    b.innerHTML = `<svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor">${icon}</svg>`;
    b.onmouseenter = () => b.style.background = "#39395a"; b.onmouseleave = () => b.style.background = "#2a2a2a";
    return b;
}
function _setIcon(b, icon) { b.innerHTML = `<svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor">${icon}</svg>`; }
// frame count is the REAL file's frame count (from /ocio/seq_range, set on p.pb.fileFrames), NOT
// video.duration * fps - the latter drifts (a clip reported "3009" when the file was 0..409). Frame <-> time
// uses a proportional map through video.duration, so it is correct even if the fps widget is off.
function _pbFrames(p) { const n = p.pb && p.pb.fileFrames; return (n > 0 && isFinite(n)) ? Math.min(1e6, Math.round(n)) : 1; }
function _pbLast(p) { return _pbFrames(p) - 1; }
// A sequence's start_frame / end_frame widgets hold FRAME NUMBERS (e.g. 86..97), but the timeline + playhead run
// on a 0-based INDEX (0..count-1). _seqBase is the offset (orig_start) that maps between them; 0 for a video (its
// widgets are already 0-based indices), so every formula below reduces to the old video behaviour when base = 0.
function _seqBase(p) { return (p.pb && p.pb.seqMode && p.seq) ? (p.seq.origStart | 0) : 0; }
// DISPLAY frame numbering. OCIO Read: same as _seqBase (its widgets already hold frame numbers). OCIO Player: the
// backend start_frame/end_frame are 0-based BATCH indices and must stay so (the trim uses them), so the source frame
// numbers live here as a display-only offset, learned from the upstream OCIO Read (syncPlayerFromUpstream). Timeline
// labels + the frame field use _dispBase; the widget<->index math (_pbIn/_pbSetIn) keeps using _seqBase (base 0 for Player).
function _dispBase(p) {
    if (p && p.videoBase && p.pb && !p.pb.seqMode) return p.videoBase | 0;   // a VIDEO preview (OCIO Read _startViewport OR the streamed Player): the <video> clock is a 0-based index, videoBase maps it to 1-based (or the Read's re-based) frame numbers
    return _seqBase(p) + ((p.player && p.player.base) ? (p.player.base | 0) : 0);
}
function _pbCur(p) {
    if (p.pb && p.pb.seqMode) return Math.max(0, Math.min(_pbLast(p), p.pb.seqFrame | 0));
    const v = p.video, d = v && v.duration, last = _pbLast(p); if (!(d > 0) || last < 1) return 0;
    return Math.max(0, Math.min(last, Math.round((v.currentTime / d) * last)));
}
function _pbSeek(p, f) {
    const last = _pbLast(p); f = Math.max(0, Math.min(last, Math.round(f)));
    if (p.pb && p.pb.seqMode) { p.pb.seqFrame = f; p.pb.seqAnchor = null; _seqShow(p); return; }
    const v = p.video, d = v && v.duration; if (d > 0) v.currentTime = Math.max(0, Math.min(d - 0.001, (f / Math.max(1, last)) * d));
}
// in / out range = the node's start_frame / end_frame widgets (single source of truth, bidirectional). Widgets
// store frame numbers; these convert to/from the 0-based index via _seqBase (base 0 = video, unchanged).
function _pbIn(p) { const base = _dispBase(p), w = W(p.node, "start_frame"); return Math.max(0, Math.min(_pbLast(p), Math.round((w?.value ?? base)) - base)); }
function _pbOut(p) { const base = _dispBase(p), last = _pbLast(p), w = W(p.node, "end_frame"); return Math.max(_pbIn(p), Math.min(last, Math.round((w?.value ?? (base + last))) - base)); }
function _pbSetField(p, name, f) { const w = W(p.node, name); if (!w) return; w.value = f; try { w.callback && w.callback(f); } catch (e) {} p.node.setDirtyCanvas(true, true); }
function _pbSetIn(p, f) { const base = _dispBase(p); _pbSetField(p, "start_frame", base + Math.max(0, Math.min(_pbOut(p), Math.round(f)))); }
function _pbSetOut(p, f) { const base = _dispBase(p); _pbSetField(p, "end_frame", base + Math.max(_pbIn(p), Math.min(_pbLast(p), Math.round(f)))); }
function _pbResetRange(p) { const base = _dispBase(p); _pbSetField(p, "start_frame", base); _pbSetField(p, "end_frame", base + _pbLast(p)); }
function _pbSet(node, p, on, dir) {
    p.pb.playing = on; p.pb.dir = dir || 1; p.pb.revAnchor = null; p.pb.seqAnchor = null;   // re-anchor on every state change
    const inF = _pbIn(p), outF = _pbOut(p), cur = _pbCur(p);
    if (p.pb.seqMode) {                                              // sequence flipbook: _tickPlayback advances seqFrame
        if (on && p.pb.dir > 0 && cur >= outF) _pbSeek(p, inF);      // at the out-point -> restart at in
        else if (on && p.pb.dir < 0 && cur <= inF) _pbSeek(p, outF); // reverse from the in-point -> restart at out
        _syncTransport(p); return;
    }
    if (on && p.pb.dir > 0) { _ensureAudio(p); if (cur >= outF) _pbSeek(p, inF); p.video.loop = false; p.video.playbackRate = 1; p.video.play().catch(() => {}); }   // build audio graph on the play gesture
    else if (on) { if (cur <= inF) _pbSeek(p, outF); p.video.pause(); }   // reverse is driven manually in _tickPlayback
    else { p.video.pause(); }
    _syncTransport(p);
}
function _pbStop(node, p) { _pbSet(node, p, false, 1); }                  // stop = pause in place (leave the playhead put)
function _pbStep(node, p, d) { _pbSet(node, p, false, 1); _pbSeek(p, _pbCur(p) + d); }
// The playback clock. Forward uses native <video> play (smooth) and loops back to the in-point at the out-point.
// Reverse cannot use native play (browsers ignore a negative rate), so it walks currentTime backwards on a
// WALL-CLOCK anchor (time-accurate regardless of seek latency) and issues a new seek only when the previous one
// has finished (v.seeking) - without that gate, a long clip floods the decoder with seeks and stalls.
function _tickPlayback(p, now) {
    const pb = p.pb, v = p.video; if (!pb) return;
    if (pb.seqMode) { _seqTick(p, now); return; }                 // image-sequence flipbook has no <video> clock
    if (!pb.playing || !(v.duration > 0)) return;
    const d = v.duration, last = Math.max(1, _pbLast(p));
    const inT = (_pbIn(p) / last) * d, outT = Math.min(d - 0.001, ((_pbOut(p) + 0.999) / last) * d);
    if (pb.dir > 0) {
        if (v.currentTime >= outT || v.ended) { v.currentTime = inT; if (v.paused) v.play().catch(() => {}); }   // loop within [in,out]
    } else {
        if (!pb.revAnchor) pb.revAnchor = { wall: now, time: Math.min(v.currentTime, outT) };
        let target = pb.revAnchor.time - ((now - pb.revAnchor.wall) / 1000) * (pb.speed || 1);
        if (target <= inT) { pb.revAnchor = { wall: now, time: outT }; target = outT; }                          // reverse-loop to out
        if (!v.seeking) v.currentTime = Math.max(0, Math.min(d - 0.001, target));
    }
}
function _niceStep(n, maxLabels) {
    const raw = Math.max(1, n / Math.max(1, maxLabels)), pow = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const m of [1, 2, 5, 10]) if (pow * m >= raw) return pow * m;
    return pow * 10;
}
// The timeline + meter are raster <canvas> widgets: at a 1x backing store they blur on a HiDPI screen AND when the
// graph is zoomed in (litegraph CSS-scales the DOM widget, so a 1x bitmap gets stretched - that is the pixelation
// on the ruler numbers and handles). Size the backing store to devicePixelRatio x the on-screen scale and draw in
// CSS-pixel coordinates, so they stay crisp like the vector (SVG) buttons. Added 2026-07-03.
function _prepCanvas(cv, cssH) {
    const rect = cv.getBoundingClientRect();
    const cssW = cv.clientWidth || Math.round(rect.width) || 200;
    const zoom = (cssW > 0 && rect.width > 0) ? rect.width / cssW : 1;      // litegraph graph-zoom (CSS transform scale)
    const scale = Math.min(4, Math.max(1, (window.devicePixelRatio || 1) * zoom));   // cap so extreme zoom cannot blow up memory
    const bw = Math.max(1, Math.round(cssW * scale)), bh = Math.max(1, Math.round(cssH * scale));
    if (cv.width !== bw) cv.width = bw;
    if (cv.height !== bh) cv.height = bh;
    const g = cv.getContext("2d");
    if (g) g.setTransform(scale, 0, 0, scale, 0, 0);                        // 1 unit = 1 CSS pixel -> crisp at DPR x zoom
    return { g, W: cssW, H: cssH };
}
function _drawTimeline(p) {
    const t = p.transport; if (!t || !t.tl) return; const cv = t.tl;
    const { g, W: Wd, H } = _prepCanvas(cv, 26); if (!g) return; const PAD = 8;
    const last = _pbLast(p), cur = _pbCur(p), inF = _pbIn(p), outF = _pbOut(p);
    const X = f => PAD + (last > 0 ? f / last : 0) * (Wd - 2 * PAD);
    g.clearRect(0, 0, Wd, H); g.fillStyle = "#141414"; g.fillRect(0, 0, Wd, H);
    g.fillStyle = "#123039"; g.fillRect(X(inF), H - 5, X(outF) - X(inF), 3);                             // active-range band (dim = to-be-cached track)
    g.fillStyle = "rgba(0,0,0,0.5)"; g.fillRect(0, 0, X(inF), H); g.fillRect(X(outF), 0, Wd - X(outF), H);
    // cache / buffer progress: bright teal filling the bottom band left->right as frames warm into the client cache
    // (GPU textures for OCIO Player, decoded blobs for OCIO Read's flipbook) - tells the user frames ARE caching,
    // not stuck. Sequence / player only; native <video> buffers itself, so there is no frame cache to show.
    if (p.pb && p.pb.seqMode) {
        const cache = p.texCache || p.seqCache;
        if (cache && cache.size) {
            const idxs = [...cache.keys()].filter(i => i >= 0 && i <= last).sort((a, b) => a - b);
            if (idxs.length) {
                const fw = last > 0 ? (Wd - 2 * PAD) / last : (Wd - 2 * PAD);
                g.fillStyle = "#25b3ac";                                                                 // bright teal = cached
                const flush = (a, b) => g.fillRect(X(a), H - 5, Math.max(1.5, X(b) - X(a) + fw), 3);
                let s = idxs[0], prev = idxs[0];
                for (let k = 1; k < idxs.length; k++) { if (idxs[k] === prev + 1) { prev = idxs[k]; continue; } flush(s, prev); s = prev = idxs[k]; }
                flush(s, prev);
            }
        }
    } else if (p.video && p.video.buffered && p.video.duration > 0) {   // streamed video has no frame cache -> draw the browser's BUFFERED ranges as the teal bar, so the timeline still shows loading progress
        const dur = p.video.duration, br = p.video.buffered;
        g.fillStyle = "#25b3ac";
        for (let i = 0; i < br.length; i++) {
            const x0 = PAD + (br.start(i) / dur) * (Wd - 2 * PAD), x1 = PAD + (br.end(i) / dur) * (Wd - 2 * PAD);
            g.fillRect(x0, H - 5, Math.max(1.5, x1 - x0), 3);
        }
    }
    const maxLabels = Math.max(2, Math.floor((Wd - 2 * PAD) / 34)), step = Math.max(1, _niceStep(last + 1, maxLabels));
    g.fillStyle = "#7a8a99"; g.strokeStyle = "#3a3a3a"; g.font = "8px monospace"; g.textAlign = "center";
    for (let f = 0; f <= last; f += step) {
        const x = X(f); g.beginPath(); g.moveTo(x, H - 6); g.lineTo(x, H - 9); g.stroke();
        g.fillText(String(f + _dispBase(p)), Math.max(7, Math.min(Wd - 7, x)), H - 11);   // real source frame number (Read: orig_start; Player: mirrored from upstream Read)
    }
    g.strokeStyle = "#333"; g.beginPath(); g.moveTo(PAD, H - 6); g.lineTo(Wd - PAD, H - 6); g.stroke();
    const handle = (x, d) => { g.fillStyle = "#4cc3ff"; g.fillRect(x - 0.5, 2, 1, H - 6); g.beginPath(); g.moveTo(x, 2); g.lineTo(x + d * 5, 2); g.lineTo(x, 7); g.closePath(); g.fill(); };
    handle(X(inF), 1); handle(X(outF), -1);
    const xc = X(cur); g.fillStyle = "#ff8c1a"; g.fillRect(xc - 0.75, 0, 1.5, H - 5);                    // playhead
    g.beginPath(); g.moveTo(xc - 4, 0); g.lineTo(xc + 4, 0); g.lineTo(xc, 5); g.closePath(); g.fill();
}
function _syncTransport(p) {
    const t = p.transport; if (!t) return;
    const cur = _pbCur(p), pb = p.pb;
    if (document.activeElement !== t.frame) t.frame.value = String(cur + _dispBase(p));   // display the real source frame number
    // Play buttons never turn into a pause (owner spec): the icon stays play / reverse; a green inset ring just
    // shows which direction is currently running. Stop is the only pause.
    t.play.style.boxShadow = (pb.playing && pb.dir > 0) ? "inset 0 0 0 2px #4caf50" : "";
    t.playR.style.boxShadow = (pb.playing && pb.dir < 0) ? "inset 0 0 0 2px #4caf50" : "";
    _drawTimeline(p);
}
function _ensureTransport(node, p) {
    if (p.transport) return p.transport;
    p.node = node;
    const bar = document.createElement("div");
    bar.style.cssText = "width:100%;display:none;flex-direction:column;gap:2px;padding:2px 4px 3px;box-sizing:border-box;background:#181818;";
    const tl = document.createElement("canvas"); tl.height = 26;
    tl.style.cssText = "width:100%;height:26px;display:block;cursor:pointer;";
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;justify-content:center;gap:1px;flex-wrap:nowrap;";
    const mkBtn = (icon, title, fn) => { const b = _tBtn(icon, title); b.onclick = fn; return b; };
    // left -> right: reset | set-in(I) | go-first | step-back | play-rev | STOP | FRAME | play-fwd | step-fwd | go-last | set-out(O)
    const reset = mkBtn(_SVG.reset, "Reset range to the full clip", () => { _pbResetRange(p); _drawTimeline(p); });
    const setIn = mkBtn(_SVG.setIn, "Set IN point to current frame", () => { _pbSetIn(p, _pbCur(p)); _drawTimeline(p); });
    const first = mkBtn(_SVG.first, "Go to first frame of range (in)", () => { _pbSet(node, p, false, 1); _pbSeek(p, _pbIn(p)); _drawTimeline(p); });
    const sb = mkBtn(_SVG.stepB, "Step back one frame", () => _pbStep(node, p, -1));
    const playR = mkBtn(_SVG.playR, "Play reverse", () => _pbSet(node, p, true, -1));   // always play reverse (NOT a toggle); pause is the Stop button only
    const stop = mkBtn(_SVG.stop, "Stop (pause here)", () => _pbStop(node, p));
    const frame = document.createElement("input"); frame.type = "number"; frame.value = "0"; frame.title = "Current frame (type a number to jump)";
    frame.style.cssText = "width:44px;height:16px;text-align:center;background:#101010;color:#cfe;border:1px solid #333;border-radius:2px;font:11px monospace;margin:0 3px;";
    frame.addEventListener("change", () => { const f = (parseInt(frame.value, 10) || 0) - _dispBase(p); _pbSet(node, p, false, 1); _pbSeek(p, f); _drawTimeline(p); });   // user types a source frame number -> index
    const play = mkBtn(_SVG.play, "Play forward", () => _pbSet(node, p, true, 1));   // always play forward (NOT a toggle); pause is the Stop button only
    const sf = mkBtn(_SVG.stepF, "Step forward one frame", () => _pbStep(node, p, 1));
    const last = mkBtn(_SVG.last, "Go to last frame of range (out)", () => { _pbSet(node, p, false, 1); _pbSeek(p, _pbOut(p)); _drawTimeline(p); });
    const setOut = mkBtn(_SVG.setOut, "Set OUT point to current frame", () => { _pbSetOut(p, _pbCur(p)); _drawTimeline(p); });
    const sep = () => { const s = document.createElement("span"); s.style.cssText = "width:4px;display:inline-block;"; return s; };
    row.append(reset, sep(), setIn, first, sb, playR, stop, frame, play, sf, last, setOut);
    // audio strip (video only): mute toggle on the left + a stereo L/R level meter filling the rest, sitting
    // between the transport buttons and the metadata panel. The meter reads the video's audio via Web Audio and
    // moves even while muted (so you can see there IS sound before turning it on - Seedance etc. now emit audio).
    const audioRow = document.createElement("div");
    audioRow.style.cssText = "display:none;align-items:center;gap:5px;padding:5px 4px;box-sizing:border-box;";
    const muteBtn = _tBtn(_SVG.soundOff, "Sound on / off (default off)");
    muteBtn.style.opacity = "0.55";
    muteBtn.onclick = () => _toggleMute(p, muteBtn);
    const meter = document.createElement("canvas"); meter.height = 22;
    meter.style.cssText = "flex:1 1 0;min-width:0;height:22px;display:block;";   // basis 0 + min-width:0: layout width is the flex share, NOT the (HiDPI-enlarged) backing store -> no runaway overflow
    audioRow.append(muteBtn, meter);
    // --- OCIO Player ONLY: HORIZONTAL exposure strip, sitting at the TOP of the transport bar - i.e. directly
    // between the viewport image (the player DOM widget above) and the numbered timeline (tl, below). It replaces
    // the old vertical-right slider. The number field is EDITABLE (type e.g. +2.5, Enter/blur applies, clamp
    // -16..+16); the slider mirrors it. VIEW-ONLY: sets p.exposure -> shader uExposure via _playerDraw, never sent
    // to the node / backend. Double-click the field or hit reset -> 0. Owner spec 2026-07-03 (Task C).
    let expRow = null, expSlider = null, expNum = null;
    if (p.isPlayer) {
        const clampExp = (v) => Math.max(-16, Math.min(16, isFinite(v) ? v : 0));
        const fmtExp = (x) => (x >= 0 ? "+" : "") + (Math.round(x * 100) / 100);   // signed, e.g. "+2.5" / "-3"
        const applyExp = (v, fromNum) => {
            const x = clampExp(v); p.exposure = x;
            if (expSlider) expSlider.value = String(x);
            if (expNum && !fromNum) expNum.value = fmtExp(x);   // don't clobber the field while the user is typing in it
            _playerDraw(p);                              // one-shot redraw (no fetch): instant, works in a background tab
        };
        expRow = document.createElement("div");
        expRow.style.cssText = "display:flex;align-items:center;gap:6px;padding:2px 2px 3px;box-sizing:border-box;";
        const lbl = document.createElement("span");
        lbl.textContent = "Exposure"; lbl.style.cssText = "font:10px sans-serif;color:#9cf;white-space:nowrap;flex:0 0 auto;";
        expSlider = document.createElement("input");
        expSlider.type = "range"; expSlider.min = "-16"; expSlider.max = "16"; expSlider.step = "0.1"; expSlider.value = "0";
        expSlider.title = "Exposure (stops) - VIEW ONLY, never baked into the output";
        expSlider.style.cssText = "flex:1 1 0;min-width:40px;height:14px;cursor:ew-resize;";
        expSlider.oninput = () => applyExp(parseFloat(expSlider.value) || 0, false);
        // type="text" (NOT number): a native number input REJECTS a leading "+" (".value" becomes "" for "+2.5"), so
        // the owner's "+2.5" example would read as 0. Text + manual parse accepts +/-, shows the sign, and clamps.
        expNum = document.createElement("input");
        expNum.type = "text"; expNum.inputMode = "decimal"; expNum.value = fmtExp(0);
        expNum.title = "Exposure in stops (-16..+16) - type a value (e.g. +2.5), Enter or blur to apply. Double-click to reset to 0. VIEW ONLY, never baked.";
        expNum.style.cssText = "width:52px;height:16px;text-align:center;background:#101010;color:#cde;border:1px solid #333;border-radius:2px;font:11px monospace;flex:0 0 auto;";
        const commitNum = () => { const raw = parseFloat(String(expNum.value).replace(/[^0-9.+-]/g, "")); applyExp(isFinite(raw) ? raw : 0, false); };
        expNum.addEventListener("change", commitNum);        // blur / Enter (native change)
        expNum.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); commitNum(); expNum.blur(); } });
        expNum.addEventListener("dblclick", () => { applyExp(0, false); });   // reset to 0
        const expReset = _tBtn(_SVG.reset, "Reset exposure to 0");
        expReset.onclick = () => applyExp(0, false);
        expRow.append(lbl, expSlider, expNum, expReset);
    }
    bar.append(...(expRow ? [expRow] : []), tl, row, audioRow);
    const w = node.addDOMWidget("transport", "div", bar, { serialize: false });
    w.computeSize = () => [0, node._ocioReadCollapsed ? 0 : ((p.pb && p.pb.showTransport) ? (54 + (p.isPlayer ? 22 : 0)) : 0)];   // +22 for the exposure strip on the Player; 0 when the Read Viewer is collapsed
    w._ocioAlwaysVisible = true;
    // timeline scrub + in/out drag
    tl.addEventListener("mousedown", (e) => {
        const r = tl.getBoundingClientRect(), PAD = 8, last = _pbLast(p);
        const X = f => r.left + PAD + (last > 0 ? f / last : 0) * (r.width - 2 * PAD);
        const toF = cx => { let fr = (cx - r.left - PAD) / (r.width - 2 * PAD); return Math.max(0, Math.min(last, Math.round(Math.max(0, Math.min(1, fr)) * last))); };
        const grab = Math.abs(e.clientX - X(_pbIn(p))) <= 7 ? "in" : Math.abs(e.clientX - X(_pbOut(p))) <= 7 ? "out" : "scrub";
        _pbSet(node, p, false, 1);
        const move = ev => { const f = toF(ev.clientX); if (grab === "in") _pbSetIn(p, f); else if (grab === "out") _pbSetOut(p, f); else _pbSeek(p, f); _drawTimeline(p); };
        move(e);
        const up = () => { document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
        document.addEventListener("mousemove", move); document.addEventListener("mouseup", up); e.preventDefault();
    });
    p.transport = { bar, tl, frame, play, playR, audioRow, muteBtn, meter, expRow, expSlider, expNum };
    return p.transport;
}
// ---- audio: mute toggle + stereo L/R level meter (video only). Web Audio taps the <video> so the meter shows
// levels even when the speakers are muted; the mute button just gates a GainNode. Created lazily on a user gesture
// (play / mute click) so the AudioContext is allowed to start. One shared context for all nodes. Added 2026-07-03.
let _ocioAudioCtx = null;
function _ensureAudio(p) {
    if (p.audio) { if (p.audio.ctx.state === "suspended") p.audio.ctx.resume(); return p.audio; }
    if (p._audioFailed || !p.video) return null;
    try {
        if (!_ocioAudioCtx) _ocioAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const ctx = _ocioAudioCtx;
        if (ctx.state === "suspended") ctx.resume();
        p.video.muted = false;                                   // feed the graph; audibility is the gain node's job
        const source = ctx.createMediaElementSource(p.video);    // one per element, for the element's lifetime
        const splitter = ctx.createChannelSplitter(2);
        source.connect(splitter);
        const analyserL = ctx.createAnalyser(), analyserR = ctx.createAnalyser();
        analyserL.fftSize = 256; analyserR.fftSize = 256;
        splitter.connect(analyserL, 0); splitter.connect(analyserR, 1);
        const gain = ctx.createGain(); gain.gain.value = 0;      // default: muted OUTPUT (meter still reads signal)
        source.connect(gain); gain.connect(ctx.destination);
        p.audio = { ctx, source, splitter, analyserL, analyserR, gain, muted: true,
                    dataL: new Uint8Array(analyserL.fftSize), dataR: new Uint8Array(analyserR.fftSize),
                    levelL: 0, levelR: 0 };
    } catch (e) { console.warn("[OCIO] audio init failed:", e && e.message); p._audioFailed = true; p.audio = null; }
    return p.audio;
}
function _toggleMute(p, btn) {
    _ensureAudio(p);
    if (!p.audio) { btn.title = "Audio unavailable for this clip"; return; }
    p.audio.muted = !p.audio.muted;
    p.audio.gain.gain.value = p.audio.muted ? 0 : 1;
    _setIcon(btn, p.audio.muted ? _SVG.soundOff : _SVG.soundOn);
    btn.style.opacity = p.audio.muted ? "0.55" : "1";
    btn.style.color = p.audio.muted ? "#e0e8f0" : "#4caf50";
}
// Level zones are FIXED positions on the bar (NOT a whole-bar recolor): the fill just extends into green (0-75%),
// then yellow (75-95%), then red (95-100%) as the level rises. Fast attack, slow decay so it reads like a VU.
const _MTR_GY = 0.75, _MTR_YR = 0.95;
// Map a linear peak (0..1) to a bar position via dBFS, so the meter reads like a real audio meter instead of raw
// amplitude: 0 dBFS (full scale) fills the bar, and with a -48 dB floor the green/yellow (75%) and yellow/red (95%)
// edges land at -12 dB / -2.4 dB. A -1 dBFS peak now shows ~98% (deep red), not ~a fifth of the bar. Added 2026-07-03.
const _MTR_DB_FLOOR = -48;
function _dbPos(lin) {
    if (!(lin > 1e-4)) return 0;
    const db = 20 * Math.log10(Math.min(1, lin));               // <= 0 dBFS
    return Math.max(0, Math.min(1, 1 - db / _MTR_DB_FLOOR));
}
function _drawMeterBars(cv, lvL, lvR, active) {
    const { g, W: Wd, H } = _prepCanvas(cv, 22); if (!g) return;
    g.clearRect(0, 0, Wd, H);
    const x0 = 11, barW = Math.max(2, Wd - x0 - 2), barH = 6, gap = 5;
    g.font = "8px monospace"; g.textBaseline = "middle";
    [["L", lvL, 3], ["R", lvR, 3 + barH + gap]].forEach(([label, raw, y]) => {
        const lv = Math.max(0, Math.min(1, raw)), fx = f => x0 + f * barW;
        g.fillStyle = active ? "#9cf" : "#5a6472"; g.textAlign = "left"; g.fillText(label, 1, y + barH / 2);
        g.fillStyle = "#0d0d0d"; g.fillRect(x0, y, barW, barH);                                                 // track
        if (lv > 0) { g.fillStyle = "#37c96a"; g.fillRect(x0, y, Math.min(lv, _MTR_GY) * barW, barH); }         // green
        if (lv > _MTR_GY) { g.fillStyle = "#e6c02e"; g.fillRect(fx(_MTR_GY), y, (Math.min(lv, _MTR_YR) - _MTR_GY) * barW, barH); }   // yellow
        if (lv > _MTR_YR) { g.fillStyle = "#e0432e"; g.fillRect(fx(_MTR_YR), y, (lv - _MTR_YR) * barW, barH); }  // red
        g.fillStyle = "#000"; g.fillRect(fx(_MTR_GY), y, 1, barH); g.fillRect(fx(_MTR_YR), y, 1, barH);         // zone dividers
    });
}
function _drawAudioMeter(p) {
    const t = p.transport; if (!t || !t.meter || p.pb.seqMode || t.audioRow.style.display === "none") return;
    const a = p.audio;
    if (!a) { _drawMeterBars(t.meter, 0, 0, false); return; }
    const peak = arr => { let m = 0; for (let i = 0; i < arr.length; i++) { const v = Math.abs(arr[i] - 128); if (v > m) m = v; } return m / 128; };
    a.analyserL.getByteTimeDomainData(a.dataL); a.analyserR.getByteTimeDomainData(a.dataR);
    a.levelL = Math.max(peak(a.dataL), a.levelL * 0.86); a.levelR = Math.max(peak(a.dataR), a.levelR * 0.86);
    _drawMeterBars(t.meter, _dbPos(a.levelL), _dbPos(a.levelR), !a.muted);   // dBFS scale, not raw amplitude
}
function updateReadPreview(node) {
    const p = ensureReadPreview(node);
    const src = (W(node, "source")?.value || "").trim();
    if (!src) { _stopSeq(p); _stopViewport(p); _hideReadMsg(p); _blankReadImg(p); _ocioBusy(p.box, false); return; }
    const seq = node._ocioSeq;
    // Format guard (Task F): a non-media / unsupported path (a .txt, code file, unknown container) never reaches
    // the decode routes - it would 404/400 and blank the box. Surface a readable message and stop. A folder path
    // (sequence dir) passes the guard; the server resolves its frames. A resolved sequence (seq.kind) is trusted.
    if (!(seq && (seq.kind === "sequence" || seq.kind === "video")) && !isKnownMediaPath(src)) {
        _stopSeq(p); _stopViewport(p); _blankReadImg(p);
        _showReadMsg(p, "No media - unsupported format");
        _ocioBusy(p.box, false);
        return;
    }
    _hideReadMsg(p);
    _ocioBusy(p.box, true, "Processing…");                   // updating the preview (source / colorspace change, refresh) - cleared on img load / video ready
    clearTimeout(p._busyTO); p._busyTO = setTimeout(() => _ocioBusy(p.box, false), 8000);   // safety: never leave the spinner stuck (the seq flipbook has no single "loaded" event)
    if (/\.(mov|mp4|mkv|avi|webm|mxf|m4v)$/i.test(src)) {
        _stopSeq(p); _startViewport(node, p, src);
    } else if (seq && seq.kind === "sequence") {
        _startSeqViewport(node, p, src, seq);              // EXR / image sequence -> flipbook player
    } else {
        _stopSeq(p); _stopViewport(p);
        p.img.src = "/ocio/thumb?" + _thumbQuery(node, src); p.img.style.display = "";
    }
}

// ---- read-only metadata panel (OCIO Read): a compact DOM widget under the preview, fed by /ocio/meta.
// Small monospace "Label: value" lines - resolution, format, frame range + count, fps, the auto-detected
// input colorspace, and alpha presence. Same update trigger as the preview (source change).
const META_ROWS = [
    ["resolution", "Resolution"], ["format", "Format"], ["range", "Frames"],
    ["fps", "FPS"], ["input_colorspace", "Colorspace"], ["alpha", "Alpha"],
];
function ensureReadMeta(node) {
    if (node._ocioMeta) return node._ocioMeta;
    const box = document.createElement("div");
    box.style.cssText = "width:100%;font:10px/1.4 monospace;color:#9cf;background:#1a1a1a;padding:4px 6px;box-sizing:border-box;overflow:hidden;white-space:nowrap;";
    const w = node.addDOMWidget("meta", "div", box, { serialize: false });
    w.computeSize = () => [0, node._ocioReadCollapsed ? 0 : (16 * META_ROWS.length + 8)];   // 0 when the Read Viewer is collapsed
    w._ocioAlwaysVisible = true;                      // always shown, regardless of source kind
    node._ocioMeta = box;
    return box;
}
function renderMeta(box, data) {
    if (!data || data.error) { box.innerHTML = ""; return; }
    const rangeTxt = data.kind === "still" ? "1" :
        `${data.start}-${data.end} (${data.count})${data.missing ? ", missing " + data.missing : ""}`;
    const alphaTxt = data.alpha === null || data.alpha === undefined ? "-" : (data.alpha ? "yes" : "no");
    const values = { resolution: data.resolution || "-", format: (data.format || "-").toUpperCase(),
        range: rangeTxt, fps: data.fps ? data.fps.toFixed(3) : "-", input_colorspace: data.input_colorspace || "-",
        alpha: alphaTxt };
    box.innerHTML = META_ROWS.map(([k, label]) => `<div>${label}: ${values[k]}</div>`).join("");
}
async function updateReadMeta(node) {
    const box = ensureReadMeta(node);
    const src = (W(node, "source")?.value || "").trim();
    if (!src) { box.innerHTML = ""; return; }
    try {
        const r = await fetch("/ocio/meta?" + new URLSearchParams({ src }).toString());
        const d = await r.json();
        renderMeta(box, d);
    } catch (e) { console.error("OCIO meta", e); box.innerHTML = ""; }
}

async function fillRange(node, source) {
    if (!source) { applyReadVis(node); updateReadPreview(node); _setVideoOutput(node, false); return; }   // empty source: hide the frame controls (still-image default) + the VIDEO output
    try {
        const r = await fetch("/ocio/seq_range", {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source }),
        });
        const d = await r.json();
        node._ocioSeq = d;
        // auto-set the visible Frame Mode to the detected kind (owner E3): still->single, sequence->sequence, video->video
        const fmMap = { still: "single", sequence: "sequence", video: "video" };
        if (d && fmMap[d.kind]) setWSilent(node, "frame_mode", fmMap[d.kind]);
        _setVideoOutput(node, !!(d && d.kind === "video"));    // R1: expose the VIDEO output only for a video source
        const pv = node._ocioPrev;
        if (d && (d.kind === "sequence" || d.kind === "video")) {
            setWSilent(node, "start_frame", d.start | 0);
            setWSilent(node, "end_frame", d.end | 0);
            setWSilent(node, "frame_shift", d.start | 0);      // default: first frame keeps its source number
            if (d.fps) setWSilent(node, "fps", Math.round(d.fps * 1000) / 1000);
            if (d.input_cs) setWSilent(node, "input_colorspace", d.input_cs);   // folder path has no ext -> fix EXR auto-detect (sRGB -> ACEScg) from the resolved first frame
            // the transport ruler + in/out span the REAL file frame count (authoritative), not video.duration
            if (pv && pv.pb) pv.pb.fileFrames = Math.max(1, (d.count | 0) || ((d.end | 0) - (d.start | 0) + 1) || 1);
        } else {
            setWSilent(node, "start_frame", 0);
            setWSilent(node, "end_frame", 0);
            if (pv && pv.pb) pv.pb.fileFrames = 1;
        }
        applyReadVis(node, d.kind);
        resyncAllWrites();                                     // push the detected range/fps to downstream Writes
        updateReadPreview(node);                              // now that _ocioSeq is known, route to the right player (seq flipbook / video / thumb)
    } catch (e) { console.error("OCIO seq_range", e); }
}

// Per-kind visible widget NAMES (Nuke Read model: frame_mode only means anything for a sequence - it is the
// auto/single/sequence grab-mode toggle; a video is always its whole clip, a still is always just itself).
// Buttons and the preview/meta DOM widgets are tagged _ocioAlwaysVisible where they are created, so every
// kind here only needs to list the value widgets that apply to it.
const READ_VIS = {
    still: ["source", "input_colorspace", "output_colorspace", "raw_data"],
    sequence: ["source", "frame_mode", "input_colorspace", "output_colorspace", "raw_data",
               "start_frame", "end_frame", "frame_shift", "missing_frames", "edge_mode", "fps"],
    video: ["source", "input_colorspace", "output_colorspace", "raw_data", "start_frame", "end_frame", "fps"],
};
// show only the widgets that apply to the source kind (see setVisibleWidgets for the hide mechanism and why
// it keeps every widget in node.widgets). A widget is "always visible" if it is marked _ocioAlwaysVisible
// (buttons + the preview/meta DOM widgets, tagged where they are created) OR its name is in this kind's
// value-widget list - not inferred from widget.type, which is not a stable marker for "is this a button"
// across litegraph/Vue-nodes versions.
function applyReadVis(node, kind) {
    const names = new Set(READ_VIS[kind] || READ_VIS.still);
    setVisibleWidgets(node, (w) => w._ocioAlwaysVisible || names.has(w.name));
}

// ---- wire tracing: OCIO Write pulls its frame range + fps from the OCIO Read at the source end -------------
function findUpstreamRead(node, seen) {
    seen = seen || new Set();
    if (!node || seen.has(node.id)) return null;
    seen.add(node.id);
    if (node.type === "OCIORead") return node;
    for (const inp of (node.inputs || [])) {
        if (inp.link == null) continue;
        const link = app.graph.links[inp.link];
        if (!link) continue;
        const found = findUpstreamRead(app.graph.getNodeById(link.origin_id), seen);
        if (found) return found;
    }
    return null;
}
function syncWriteFromUpstream(node) {
    const ar = W(node, "auto_range");
    if (!ar || !ar.value) return;                              // only while auto is ON
    const read = findUpstreamRead(node);
    const seq = read && read._ocioSeq;
    if (!seq || seq.kind === "still") return;
    const rSF = W(read, "start_frame")?.value || 0;
    const rEF = W(read, "end_frame")?.value || 0;
    const rShift = W(read, "frame_shift")?.value || 0;
    const rFps = W(read, "fps")?.value || 0;
    const s0 = rSF > 0 ? rSF : (seq.start | 0);
    const e0 = rEF > 0 ? rEF : (seq.end | 0);
    const count = Math.max(1, e0 - s0 + 1);
    const first = rShift > 0 ? rShift : s0;
    setWSilent(node, "source_start", first);
    setWSilent(node, "first_frame", first);
    setWSilent(node, "last_frame", first + count - 1);
    setWSilent(node, "start_number", first);
    if (rFps) setWSilent(node, "fps", rFps);
    node.setDirtyCanvas(true, true);
}
function resyncAllWrites() {
    for (const nd of (app.graph && app.graph._nodes) || []) {
        if (nd.type === "OCIOWrite") syncWriteFromUpstream(nd);
    }
}
// OCIO Player mirrors the SAME source frame numbering + fps as the upstream OCIO Read, traced back through any chain
// of nodes (findUpstreamRead). The Player's own start_frame/end_frame stay 0-based batch indices (the backend trim
// needs them); this only sets the DISPLAY base (p.player.base) + the fps, so the viewer reads frames [first .. first+N-1]
// at the source fps - matching OCIO Read even through 10-20 nodes. frame_shift on the Read re-bases the numbering.
function syncPlayerFromUpstream(node) {
    const p = node._ocioPlayer; if (!p || !p.player) return;
    const cached = (p.player.cached | 0) || 1;
    const read = findUpstreamRead(node);
    if (!read) {                                           // no OCIO Read upstream -> plain 0-based numbering (indices)
        p.player.base = 0; setWSilent(node, "base", "0"); p._syncRange = null; node.setDirtyCanvas(true, true); return;
    }
    const seq = read._ocioSeq;
    const rSF = W(read, "start_frame")?.value || 0;
    const rShift = W(read, "frame_shift")?.value || 0;
    const rFps = W(read, "fps")?.value || 0;
    const s0 = rSF > 0 ? rSF : ((seq && seq.start != null) ? (seq.start | 0) : 0);
    const first = (rShift > 0 ? rShift : s0) | 0;          // same rule OCIO Write uses (frame_shift re-bases the numbering)
    const lastN = first + cached - 1;
    p.player.base = first;
    setWSilent(node, "base", String(first));               // STRING widget: backend maps SOURCE start/end numbers -> 0-based batch indices (subtracts base)
    // 2026-07-03 (BUG A fix): start_frame/end_frame hold SOURCE numbers (so the fields match the timeline). On a
    // genuine source-range CHANGE (new first/lastN vs the last sync), snap to the full new range [first .. lastN] - a new clip shows
    // whole. On a re-render of the SAME source (exposure / colorspace change, same range) preserve the current
    // values if still a valid sub-range (keeps a user trim), else snap. Root cause of the stale-widget bug: the
    // old guard could not tell a previous AUTO-SET range from a user trim, so switching to a source whose range
    // CONTAINED the old values kept them (start/end widgets stale while the meta panel already showed the new range).
    const rangeChanged = !p._syncRange || p._syncRange.first !== first || p._syncRange.last !== lastN;
    const curSF = Math.round(W(node, "start_frame")?.value || 0), curEF = Math.round(W(node, "end_frame")?.value || 0);
    if (rangeChanged || !(curSF >= first && curEF <= lastN && curSF <= curEF && curEF > 0)) {
        setWSilent(node, "start_frame", first); setWSilent(node, "end_frame", lastN);
    }
    p._syncRange = { first, last: lastN };
    if (rFps > 0) { p.pb.fps = rFps; setWSilent(node, "fps", rFps); }   // fps must match the source, not the Player default
    node.setDirtyCanvas(true, true);
}
// ---- auto colorspace: wired from LTX's HDR decode node -> set Linear Rec.709 -> ACEScg automatically ---------
function findUpstreamType(node, typeName, seen) {
    seen = seen || new Set();
    if (!node || seen.has(node.id)) return null;
    seen.add(node.id);
    if (node.type === typeName) return node;
    for (const inp of (node.inputs || [])) {
        if (inp.link == null) continue;
        const link = app.graph.links[inp.link];
        if (!link) continue;
        const found = findUpstreamType(app.graph.getNodeById(link.origin_id), typeName, seen);
        if (found) return found;
    }
    return null;
}
function applyAutoColorspace(node) {
    if (!(W(node, "auto_colorspace")?.value)) return;                 // only while auto is ON
    if (!findUpstreamType(node, "LTXVHDRDecodePostprocess")) return;  // only for an LTX HDR upstream
    setWSilent(node, "from_colorspace", "Linear Rec.709 (sRGB)");     // LTX hdr_linear = scene-linear Rec.709
    setWSilent(node, "output_colorspace", "ACEScg");                  // grade space
    node.setDirtyCanvas(true, true);
}

// ---- profile widget: HDR source preset -> from/output colorspace + still_format/bit_depth (silent) ---------
const PROFILE_CS = {
    "LTX 2.3 HDR":               { from: "Linear Rec.709 (sRGB)", out: "ACEScg", fmt: "exr", bit: "16f" },
    "LumiPic LogC3 (Flux/Qwen)": { from: "Linear Rec.709 (sRGB)", out: "ACEScg", fmt: "exr", bit: "16f" },
    "LumiPic V10 LogC4":         { from: "Linear Rec.709 (sRGB)", out: "ACEScg", fmt: "exr", bit: "16f" },
};
// generic upstream tracer: walk input links back through N nodes until `test(node)` matches
function findUpstream(node, test, seen) {
    seen = seen || new Set();
    if (!node || seen.has(node.id)) return null;
    seen.add(node.id);
    if (test(node)) return node;
    for (const inp of (node.inputs || [])) {
        if (inp.link == null) continue;
        const link = app.graph.links[inp.link];
        if (!link) continue;
        const found = findUpstream(app.graph.getNodeById(link.origin_id), test, seen);
        if (found) return found;
    }
    return null;
}
function applyProfile(node, profileName) {
    const p = PROFILE_CS[profileName];
    if (!p) return;                                    // "none" / "auto" (unresolved) / "Seedance ..." -> no-op here
    node._ocioProfileSetting = true;                    // guard: the colorspace writes below are OURS, not a manual edit
    setWSilent(node, "from_colorspace", p.from);
    setWSilent(node, "output_colorspace", p.out);
    setWSilent(node, "still_format", p.fmt);
    setWSilent(node, "bit_depth", p.bit);
    node._ocioProfileSetting = false;
    node.setDirtyCanvas(true, true);
}
// best-effort upstream source detection for profile === "auto"
function findUpstreamSource(node) {
    const ltx = findUpstream(node, (n) => (n.type || "").includes("LTXVHDRDecodePostprocess"));
    if (ltx) return "LTX 2.3 HDR";                      // reliable: a dedicated LTX HDR decode node
    const lora = findUpstream(node, (n) => (n.type || "").includes("LoraLoader"));
    if (lora) {
        const fn = (W(lora, "lora_name")?.value || "").toLowerCase();
        if (fn.includes("logc4")) {
            console.log("OCIO Write: auto profile guessed 'LumiPic V10 LogC4' from LoRA filename", fn);
            return "LumiPic V10 LogC4";
        }
        if (fn.includes("logc3") || fn.includes("hdr")) {
            console.log("OCIO Write: auto profile guessed 'LumiPic LogC3 (Flux/Qwen)' from LoRA filename", fn);
            return "LumiPic LogC3 (Flux/Qwen)";
        }
    }
    // Seedance: no known distinct node type confirmed yet in this codebase - left as a placeholder, not faked.
    const seedance = findUpstream(node, (n) => (n.type || "").toLowerCase().includes("seedance"));
    if (seedance) {
        console.log("OCIO Write: auto profile guessed 'Seedance 4K 10-bit' from upstream node type", seedance.type);
        return "Seedance 4K 10-bit";
    }
    return null;                                        // leave as-is; nothing recognizable upstream
}
function resolveAutoProfile(node) {
    if (W(node, "profile")?.value !== "auto") return;
    const found = findUpstreamSource(node);
    if (!found) return;                                 // no match -> leave on "auto", do not fake a guess
    setWSilent(node, "profile", found);
    applyProfile(node, found);
}

// wrap a widget's callback so we also run `after(value)`
function onChange(node, name, after) {
    const w = W(node, name);
    if (!w) return;
    const orig = w.callback;
    w.callback = function (v) {
        const r = orig ? orig.apply(this, arguments) : undefined;
        try { after(v); } catch (e) { console.error("OCIO io:", e); }
        return r;
    };
}

// ---- OCIO Read: upload (single / sequence) + auto input colorspace ---------------------------------------
async function uploadRead(node) {
    const inp = document.createElement("input");
    inp.type = "file";
    inp.multiple = true;
    inp.accept = ".exr,.hdr,.tif,.tiff,.png,.jpg,.jpeg,.bmp,.dpx,.mov,.mp4,.mkv,.avi,.webm,.mxf,.m4v";
    inp.style.display = "none";
    document.body.appendChild(inp);
    inp.onchange = async () => {
        const files = Array.from(inp.files || []);
        if (!files.length) { inp.remove(); return; }
        try {
            if (files.length === 1) {
                const fd = new FormData();
                fd.append("file", files[0]);
                const data = await (await fetch("/ocio/upload", { method: "POST", body: fd })).json();
                if (data && data.path) {
                    setW(node, "source", data.path);
                    setW(node, "input_colorspace", autoInCs(files[0].name));
                }
            } else {
                // image sequence: group every frame into one server sub-folder
                const stem = (files[0].name.split(".")[0] || "sequence").replace(/\d+$/, "") || "sequence";
                const sub = stem + "_seq";
                for (const f of files) {
                    const fd = new FormData();
                    fd.append("subfolder", sub);
                    fd.append("file", f);
                    await fetch("/ocio/upload", { method: "POST", body: fd });
                }
                setW(node, "source", sub + "/");
                setW(node, "input_colorspace", autoInCs(files[0].name));
            }
        } catch (e) {
            console.error("OCIO Read upload failed", e);
        }
        inp.remove();
    };
    inp.click();
}

// ---- disk browser (server-side) - folders for Write output, folders + files for Read source ---------------
let _ocioLastBrowseDir = "";   // remember the folder the browser was last in, so re-opening Open Files starts there (not the root)
let _ocioLastOutputDir = "";   // OCIO Write: the ABSOLUTE last-CHOSEN output folder, so "Output Folder" re-opens there (owner 2026-07-04). Separate from _ocioLastBrowseDir so input browsing doesn't move the output start.
async function listDir(path, wantFiles, sequence) {
    const r = await fetch("/ocio/list_dirs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path || "", files: !!wantFiles, sequence: !!sequence }),
    });
    return await r.json();
}
function relToOutput(absPath, outputRoot) {
    if (!outputRoot) return absPath;
    const a = absPath.replace(/\\/g, "/"), o = outputRoot.replace(/\\/g, "/");
    if (a === o) return "";
    if (a.startsWith(o + "/")) return a.slice(o.length + 1);
    return absPath;
}
// opts: { widget, pickFiles (Read source), forOutput (Write) }
function openBrowser(node, opts) {
    const overlay = document.createElement("div");
    Object.assign(overlay.style, {
        position: "fixed", inset: "0", background: "rgba(0,0,0,0.55)", zIndex: 10000,
        display: "flex", alignItems: "center", justifyContent: "center",
    });
    const box = document.createElement("div");
    Object.assign(box.style, {
        background: "#222", color: "#ddd", width: "580px", maxHeight: "72vh", borderRadius: "8px",
        padding: "14px", font: "13px sans-serif", display: "flex", flexDirection: "column", gap: "8px",
        boxShadow: "0 8px 30px rgba(0,0,0,0.5)",
    });
    const title = document.createElement("div");
    title.textContent = opts.pickFiles ? "Choose a file, or a sequence folder" : "Choose / create output folder";
    title.style.fontWeight = "600";
    // Sequence checkbox (Read source browser, owner E1): collapse numbered frames in a folder into ONE entry per
    // name-prefix - PBR passes (Diffuse.#### / Normal.#### / Depth.####) each show as one named sequence. Default ON.
    let seqMode = true, seqRow = null, seqChk = null;
    if (opts.pickFiles) {
        seqRow = document.createElement("label");
        seqRow.style.cssText = "display:flex;align-items:center;gap:6px;font-size:12px;color:#bcd;cursor:pointer;user-select:none;";
        seqChk = document.createElement("input"); seqChk.type = "checkbox"; seqChk.checked = true;
        const sl = document.createElement("span"); sl.textContent = "Sequence (collapse numbered frames by name)";
        seqRow.append(seqChk, sl);
        seqChk.onchange = () => { seqMode = seqChk.checked; render(state.path); };
    }
    const cur = document.createElement("input");
    cur.type = "text";
    cur.placeholder = "path (editable) - Enter to go there, e.g. D:\\Projects\\shot";
    cur.style.cssText = "font-size:12px;color:#9cf;background:#1a1a1a;border:1px solid #333;padding:6px;border-radius:4px;";
    const list = document.createElement("div");
    list.style.cssText = "overflow:auto;flex:1;border:1px solid #333;border-radius:4px;min-height:200px;";
    const buttons = document.createElement("div");
    buttons.style.cssText = "display:flex;gap:8px;justify-content:flex-end;";
    const mk = (label, primary) => {
        const b = document.createElement("button");
        b.textContent = label;
        b.style.cssText = `padding:6px 12px;border-radius:5px;border:0;cursor:pointer;${primary ? "background:#3a7;color:#fff;" : "background:#444;color:#ddd;"}`;
        return b;
    };
    let newFolder = null, newRow = null;
    if (opts.forOutput) {
        newRow = document.createElement("div");
        newRow.style.cssText = "display:flex;gap:6px;align-items:center;";
        const lbl = document.createElement("span");
        lbl.textContent = "new subfolder:";
        lbl.style.cssText = "font-size:12px;color:#aaa;white-space:nowrap;";
        newFolder = document.createElement("input");
        newFolder.type = "text";
        newFolder.placeholder = "(optional) e.g. test - created on render";
        newFolder.style.cssText = "flex:1;font-size:12px;color:#ddd;background:#1a1a1a;border:1px solid #333;padding:5px;border-radius:4px;";
        newRow.append(lbl, newFolder);
    }
    const upBtn = mk("↑ up");
    const useBtn = mk(opts.pickFiles ? "use this folder (sequence)" : "use this folder", true);
    const cancelBtn = mk("cancel");
    buttons.append(upBtn, useBtn, cancelBtn);
    box.append(title, cur, ...(seqRow ? [seqRow] : []), list, ...(newRow ? [newRow] : []), buttons);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    const close = () => overlay.remove();
    let state = { path: "", parent: "", output_root: "", dirs: [], files: [] };
    const pick = (p) => {
        if (opts.forOutput) _ocioLastOutputDir = p;   // remember the ABSOLUTE chosen output folder, so next open starts here
        setW(node, opts.widget, opts.forOutput ? relToOutput(p, state.output_root) : p);
        if (opts.pickFiles) setW(node, "input_colorspace", autoInCs(p));
        close();
    };
    async function render(path) {
        state = await listDir(path, opts.pickFiles, opts.pickFiles && seqMode);
        cur.value = state.path;
        if (state.path) _ocioLastBrowseDir = state.path;   // remember where we are for next time

        list.innerHTML = "";
        const here = state.path.replace(/\\/g, "/");
        for (const d of state.dirs) {
            const row = document.createElement("div");
            row.textContent = "📁 " + d;
            row.style.cssText = "padding:6px 10px;cursor:pointer;border-bottom:1px solid #2a2a2a;";
            row.onmouseenter = () => (row.style.background = "#333");
            row.onmouseleave = () => (row.style.background = "");
            row.onclick = () => render(here + "/" + d);
            list.appendChild(row);
        }
        const fileRow = (label, src, icon) => {
            const row = document.createElement("div");
            row.textContent = icon + " " + label;
            row.style.cssText = "padding:6px 10px;cursor:pointer;border-bottom:1px solid #2a2a2a;color:#cdd;";
            row.onmouseenter = () => (row.style.background = "#2c3b2c");
            row.onmouseleave = () => (row.style.background = "");
            row.onclick = () => pick(here + "/" + src);          // frame_mode grabs the whole sequence from the first frame
            list.appendChild(row);
        };
        if (opts.pickFiles && seqMode && Array.isArray(state.seqs)) {
            for (const s of state.seqs) fileRow(s.label, s.src, s.single ? "🎞" : "🎬");   // collapsed sequences (clapperboard) + singles (frame)
        } else {
            for (const f of (state.files || [])) fileRow(f, f, "🎞");
        }
        if (!state.dirs.length && !(state.files || []).length) {
            const e = document.createElement("div");
            e.textContent = "(empty)"; e.style.cssText = "padding:10px;color:#777;";
            list.appendChild(e);
        }
    }
    upBtn.onclick = () => state.parent && render(state.parent);
    cancelBtn.onclick = close;
    cur.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); render(cur.value.trim()); } };
    useBtn.onclick = () => {
        let p = (cur.value || state.path).trim().replace(/[\\/]+$/, "");
        if (newFolder) {
            const nf = newFolder.value.trim().replace(/^[\\/]+|[\\/]+$/g, "");
            if (nf) p = p + "/" + nf;
        }
        pick(p);
    };
    overlay.onclick = (e) => { if (e.target === overlay) close(); };
    const _cur = (W(node, opts.widget)?.value || "").trim();
    // OUTPUT folder: re-open at the last CHOSEN folder (absolute; the widget value is stored relative-to-output, so
    // don't dirname it). FILE picker: open in the current file's folder, else where we last browsed. Fall back to root.
    const _start = opts.forOutput
        ? (_ocioLastOutputDir || _ocioLastBrowseDir || "")
        : (_cur ? _cur.replace(/[\\/][^\\/]*$/, "") : _ocioLastBrowseDir);
    render(_start || "");
}
function openFolderDialog(node) {   // Write output folder
    return openBrowser(node, { widget: "output_folder", forOutput: true });
}

// (2026-07-04: node titles carry NO version suffix anymore - owner wanted clean names. __version__ is still
// exposed via the /ocio/version route; the display name is just "OCIO Read" / "OCIO Write" / etc.)

// ============================================================================================================
// OCIO Player: on-node WebGL2 FLOAT viewport. Added 2026-07-03.
//
// Unlike OCIO Read (which plays a <video> or an <img> flipbook of server-color-managed 8-bit thumbs), the
// Player keeps the material in FLOAT the whole way: the backend cached the incoming batch as full-res HALF-float
// RGBA .npy frames (io_nodes._player_cache), the /ocio/floatframe route serves each as raw float16 bytes, and
// this viewport uploads them into an RGBA16F texture. The shader then does, IN ORDER:
//   sample RGBA16F -> multiply RGB by 2^exposure (VIEW-ONLY gain) -> OCIO in_cs->out_cs 3D LUT (/ocio/lut) -> screen
// So exposure and the display transform are done on the GPU on the real HDR values, not a pre-baked 8-bit image.
//
// Exposure is applied in the INPUT (pre-display-LUT) space. For scene-linear input (ACEScg, Linear Rec.709) that
// is the physically-correct "stops of light" exposure. For a display-encoded input (sRGB - Display etc.) it is an
// APPROXIMATE viewer gain (multiplying an already display-encoded signal is not a true stop), noted honestly here.
// It is VIEW-ONLY: never sent to the node, never affects the node's output (the backend bakes NO exposure).
//
// The transport bar, HiDPI canvas prep (_prepCanvas), and metadata panel are the SAME shared helpers OCIO Read
// uses (_ensureTransport / _pbIn / _pbOut / _pbSeek / _syncTransport, and a metadata DOM widget). Playback runs
// on a float flipbook clock (_playerTick, modeled on _seqTick) that drives /ocio/floatframe -> GPU instead of an
// <img>. start_frame / end_frame are the node's own 0-based OUTPUT indices (io_nodes.py), so the in/out handles
// map 1:1 to cached frames with base 0 - no _seqBase offset (that stays a video/sequence-only concept).

// Exposure shader: RGBA16F float texture -> 2^exposure gain (input space) -> optional OCIO 3D LUT -> screen.
const _PLAYER_FRAG = `#version 300 es
precision highp float; precision highp sampler3D;
in vec2 uv; out vec4 o;
uniform sampler2D uImg; uniform sampler3D uLut; uniform float uN; uniform float uOn; uniform float uExposure;
void main(){
  vec4 src = texture(uImg, uv);
  vec3 c = src.rgb * exp2(uExposure);                 // VIEW-ONLY exposure, in the INPUT colorspace (see header note)
  if (uOn > 0.5) {                                     // OCIO display LUT expects [0,1]; clamp for the sample fetch only
    vec3 s = clamp(c, 0.0, 1.0) * ((uN - 1.0) / uN) + 0.5 / uN;
    c = texture(uLut, s).rgb;
  }
  o = vec4(c, 1.0);
}`;
function _playerInitGL(p) {
    if (p.gl) return p.gl;
    const gl = p.canvas.getContext("webgl2", { premultipliedAlpha: false, antialias: false, preserveDrawingBuffer: true });
    if (!gl) { console.warn("[OCIO Player] no WebGL2"); return null; }
    if (!gl.getExtension("EXT_color_buffer_half_float") && !gl.getExtension("EXT_color_buffer_float")) {
        // RGBA16F as a SAMPLED texture is core in WebGL2; this ext gates render-TO-float only (we don't need it).
        // We still upload/sample RGBA16F fine without it - so this is a warning, not a hard fail.
        console.warn("[OCIO Player] no float color-buffer ext (sampling RGBA16F is still core WebGL2)");
    }
    const vs = _vpCompile(gl, gl.VERTEX_SHADER, _VP_VERT), fs = _vpCompile(gl, gl.FRAGMENT_SHADER, _PLAYER_FRAG);
    if (!vs || !fs) return null;
    const prog = gl.createProgram(); gl.attachShader(prog, vs); gl.attachShader(prog, fs);
    gl.bindAttribLocation(prog, 0, "p"); gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { console.error("[OCIO Player] link:", gl.getProgramInfoLog(prog)); return null; }
    const quad = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);   // one oversized tri
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    const imgTex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, imgTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const lutTex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_3D, lutTex);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
    gl.useProgram(prog);
    const locs = { uN: gl.getUniformLocation(prog, "uN"), uOn: gl.getUniformLocation(prog, "uOn"),
                   uExposure: gl.getUniformLocation(prog, "uExposure") };
    gl.uniform1i(gl.getUniformLocation(prog, "uImg"), 0); gl.uniform1i(gl.getUniformLocation(prog, "uLut"), 1);
    p.gl = { gl, prog, locs, imgTex, lutTex };
    return p.gl;
}
async function _playerRefreshLut(node, p) {
    const g = p.gl; if (!g) return;
    const q = new URLSearchParams({ in_cs: W(node, "input_colorspace")?.value || "", out_cs: W(node, "output_colorspace")?.value || "",
        raw: W(node, "raw_data")?.value ? "1" : "0", size: "33" });
    try {
        const r = await fetch("/ocio/lut?" + q.toString()); if (!r.ok) throw new Error("lut " + r.status);
        const n = parseInt(r.headers.get("X-Lut-Size") || "33", 10); const buf = new Uint8Array(await r.arrayBuffer());
        const gl = g.gl; gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_3D, g.lutTex);
        gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGBA8, n, n, n, 0, gl.RGBA, gl.UNSIGNED_BYTE, buf);
        p.lutN = n; p.lutReady = true; _playerDraw(p);
    } catch (e) { console.error("[OCIO Player] lut fetch:", e); p.lutReady = false; }
}
// Draw the currently-uploaded frame texture with the current exposure + LUT. Cheap: no fetch, no re-upload; used
// for a one-shot redraw after exposure / colorspace / LUT changes (works even in a background tab where rAF is throttled).
function _playerDraw(p) {
    const g = p.gl; if (!g || !p.texW) return; const gl = g.gl;
    if (p.canvas.width !== p.texW || p.canvas.height !== p.texH) { p.canvas.width = p.texW; p.canvas.height = p.texH; }
    gl.viewport(0, 0, p.canvas.width, p.canvas.height);
    gl.useProgram(g.prog);
    const _ent = p.texCache && p.texCache.get(p.pb.seqFrame | 0);       // draw the current frame from the per-frame texture cache (falls back to imgTex before it is cached)
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, (_ent && _ent.tex) || g.imgTex);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_3D, g.lutTex);
    gl.uniform1f(g.locs.uN, p.lutN || 33); gl.uniform1f(g.locs.uOn, p.lutReady ? 1 : 0);
    gl.uniform1f(g.locs.uExposure, p.exposure || 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
}
// ---- Player frame cache: each frame is its own RGBA16F GPU texture kept in a bounded LRU, so playback and scrub
// read from VRAM instead of re-fetching ~100 MB per frame over HTTP (that on-demand refetch was the slowness the
// owner saw vs OCIO Read's tiny 8-bit thumbs). Budget-capped: a 4K RGBA16F frame ~= 116 MB, so ~17 fit in ~2 GB;
// shorter clips cache in full. Over budget, the least-recently-used non-current frame is evicted. State on p:
//   p.texCache: Map<idx,{tex,w,h,bytes}>   p.texOrder: LRU list of idx (oldest first)   p.texBytes: total bytes.
const _PLAYER_TEX_BUDGET = 2.0e9;                                        // ~2 GB of frame textures (tunable); knob for how many frames stay warm
function _playerMkTex(gl) {
    const t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
}
function _playerTouch(p, idx) {                                          // mark idx most-recently-used (moves it to the tail)
    const o = p.texOrder; const i = o.indexOf(idx); if (i >= 0) o.splice(i, 1); o.push(idx);
}
function _playerEvict(p) {                                               // drop LRU frames until under budget, never the current frame
    const gl = p.gl && p.gl.gl; if (!gl || !p.texCache) return;
    const cur = p.pb.seqFrame | 0;
    while (p.texBytes > _PLAYER_TEX_BUDGET && p.texOrder.length > 1) {
        let k = -1; for (let i = 0; i < p.texOrder.length; i++) { if (p.texOrder[i] !== cur) { k = i; break; } }
        if (k < 0) break;
        const idx = p.texOrder.splice(k, 1)[0], e = p.texCache.get(idx);
        if (e) { try { gl.deleteTexture(e.tex); } catch (x) {} p.texBytes -= e.bytes; p.texCache.delete(idx); }
    }
}
function _playerClearTex(p) {                                            // free all frame textures (new render / teardown)
    const gl = p.gl && p.gl.gl;
    if (p.texCache) { if (gl) for (const e of p.texCache.values()) { try { gl.deleteTexture(e.tex); } catch (x) {} } p.texCache.clear(); }
    p.texOrder = []; p.texBytes = 0;
    if (p.playerInflight) p.playerInflight.clear();
}
// Fetch one float16 RGBA frame -> its own RGBA16F texture in the LRU cache (or reuse the cached one) -> draw if
// it is still the current frame. Body is raw float16 bytes (X-Width * X-Height * 4 * 2), a Uint16Array of 16-bit words.
async function _playerFetch(p, idx, show) {
    const node = p.node, dir = p.player && p.player.dir; if (!dir || !p.gl) return;
    const last = _pbLast(p); idx = Math.max(0, Math.min(last, idx | 0));
    if (!p.texCache) { p.texCache = new Map(); p.texOrder = []; p.texBytes = 0; }
    if (!p.playerInflight) p.playerInflight = new Set();
    if (p.texCache.has(idx)) {                                           // cache HIT: no fetch, draw straight from VRAM
        _playerTouch(p, idx);
        if (show && (p.pb.seqFrame | 0) === idx) { const e = p.texCache.get(idx); p.texW = e.w; p.texH = e.h; _playerDraw(p); }
        return;
    }
    if (p.playerInflight.has(idx)) return;
    p.playerInflight.add(idx);
    try {
        const r = await fetch("/ocio/floatframe?" + new URLSearchParams({ dir, frame: String(idx) }).toString());
        if (!r.ok) throw new Error("floatframe " + r.status);
        const w = parseInt(r.headers.get("X-Width") || "0", 10), h = parseInt(r.headers.get("X-Height") || "0", 10);
        const buf = await r.arrayBuffer();
        if (!(w > 0 && h > 0) || buf.byteLength < w * h * 4 * 2) throw new Error("bad float frame dims " + w + "x" + h + " len " + buf.byteLength);
        if (node._ocioPlayer !== p) return;                          // viewport torn down mid-fetch
        // AUTO input colorspace (heuristic, honest - not from metadata): on the FIRST frame, if any RGB value > 1.0
        // (HDR), default input_colorspace to ACEScg; else leave the sRGB - Display default. User override wins, so
        // only do this once and only if the user hasn't already touched it.
        if (!p.autoCsChecked) {
            p.autoCsChecked = true;
            const half0 = new Uint16Array(buf, 0, Math.min(w * h * 4, (buf.byteLength / 2) | 0));
            if (_halfAnyOverOne(half0)) {
                const w0 = W(node, "input_colorspace");
                if (w0 && !p.userSetCs && String(w0.value || "").includes("sRGB")) {
                    setW(node, "input_colorspace", CS_ACESCG); _playerRefreshLut(node, p);
                }
            }
        }
        const g = p.gl, gl = g.gl;
        const tex = _playerMkTex(gl);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, w, h, 0, gl.RGBA, gl.HALF_FLOAT, new Uint16Array(buf));
        const bytes = w * h * 4 * 2;
        if (!p.texCache.has(idx)) { p.texCache.set(idx, { tex, w, h, bytes }); p.texOrder.push(idx); p.texBytes += bytes; }
        else { try { gl.deleteTexture(tex); } catch (x) {} }         // lost a race for this idx -> keep the existing entry
        _playerTouch(p, idx); _playerEvict(p);
        p.texW = w; p.texH = h;
        _adoptAspect(node, p, w, h);                                 // learn media aspect on the first frame -> node refits, image scales with width
        if (show && (p.pb.seqFrame | 0) === idx) _playerDraw(p);     // only draw if this is still the frame on screen
    } catch (e) { if (p._playerFirstErr == null) { p._playerFirstErr = String(e); console.error("[OCIO Player] frame:", e); } }
    finally { p.playerInflight.delete(idx); }
}
// Warm [in,out] into the texture cache (bounded by the VRAM budget; frames beyond it fetch on demand during play).
function _playerPrefetch(p) {
    if (!p.player) return;
    const inI = _pbIn(p), outI = _pbOut(p);
    let i = inI;
    const pump = () => {
        if (!p.player || i > outI || p.texBytes > _PLAYER_TEX_BUDGET) return;   // stop at the out-point or when the budget is full
        const idx = i++; _playerFetch(p, idx, false).then(pump, pump);
    };
    pump();                                                          // single pump: frames are large and the backend reads them serially
}
// Any RGB (ignore alpha, every 4th) half-float > 1.0? Decodes IEEE half from the raw 16-bit words.
function _halfToFloat(h) {
    const s = (h & 0x8000) >> 15, e = (h & 0x7c00) >> 10, f = h & 0x03ff;
    if (e === 0) return (s ? -1 : 1) * Math.pow(2, -14) * (f / 1024);
    if (e === 0x1f) return f ? NaN : (s ? -Infinity : Infinity);
    return (s ? -1 : 1) * Math.pow(2, e - 15) * (1 + f / 1024);
}
function _halfAnyOverOne(half) {
    for (let i = 0; i < half.length; i++) { if ((i & 3) === 3) continue; if (_halfToFloat(half[i]) > 1.001) return true; }
    return false;
}
function _playerShow(p) { if (p.player) _playerFetch(p, p.pb.seqFrame | 0, true); }
// Float-flipbook clock (modeled on _seqTick): advance seqFrame within [in,out] by wall-clock * fps, fetch+draw
// each new frame. dir < 0 = reverse; mode "bounce" ping-pongs. Frames come from /ocio/floatframe (GPU float),
// NOT /ocio/thumb. Only draws a NEW frame when the index changes.
function _playerTick(p, now) {
    const pb = p.pb; if (!pb.playing) return;
    const inI = _pbIn(p), outI = _pbOut(p), span = Math.max(1, outI - inI + 1);
    const fps = Math.max(1, parseFloat(W(p.node, "fps")?.value) || pb.fps || 24);
    if (!pb.seqAnchor) pb.seqAnchor = { wall: now, frame: Math.max(inI, Math.min(outI, pb.seqFrame | 0)) };
    const steps = Math.floor(((now - pb.seqAnchor.wall) / 1000) * fps) * (pb.dir < 0 ? -1 : 1);
    const raw = pb.seqAnchor.frame + steps;
    let idx;
    if (pb.mode === "bounce") {
        const period = Math.max(1, 2 * span - 2), ph = (((raw - inI) % period) + period) % period;
        idx = inI + (ph < span ? ph : period - ph);
    } else {
        idx = inI + ((((raw - inI) % span) + span) % span);
    }
    if (idx !== (pb.seqFrame | 0)) { pb.seqFrame = idx; _playerShow(p); }
}
function _playerEnsureRaf(node, p) {
    if (p.raf) return;
    const loop = (now) => {
        if (node._ocioPlayer !== p) { p.raf = 0; return; }
        if ((node.mode === 2 || node.mode === 4) && p.texCache && p.texCache.size) { _playerClearTex(p); _playerDraw(p); }   // muted / bypassed -> free the frame textures from VRAM
        _playerTick(p, now || 0);
        _syncTransport(p);
        p.raf = requestAnimationFrame(loop);
    };
    p.raf = requestAnimationFrame(loop);
}
function _playerStop(p) {
    if (!p) return;
    if (p.raf) { cancelAnimationFrame(p.raf); p.raf = 0; }
    if (p.pb) { p.pb.playing = false; }
    _playerClearTex(p);                                  // free the frame textures from VRAM on teardown
}

// The Player's preview-state object `p` mirrors the shape the shared transport helpers read (p.node, p.pb, p.gl,
// p.canvas, p.transport, p.exposure). pb.seqMode is TRUE so _pbCur / _pbSeek use the frame-index path (there is
// no <video>), but _seqBase stays 0 (p.seq === null) so start_frame/end_frame are read as plain 0-based indices.
// ---- Ф1b: stream a VIDEO source in the OCIO Player (Load Video -> Player.video, or a video OCIO Read traced to a
// file). A hidden <video> decodes natively (browser GPU, hardware) and streams the WHOLE clip - NO materialization,
// NO frame cap. Each frame is drawn through the SAME exposure + OCIO-LUT float shader as the float path (uploaded as
// an 8-bit texture, sampled as float in-shader, then exposure * 2^stops + display LUT). Big frames are downscaled
// before the per-rAF GPU upload (a raw 4K upload every frame stalls playback - the Nuke/Vimeo proxy trick). The
// shared transport (seqMode=false) drives the <video> for play/scrub/reverse, mapping currentTime<->frame.
function _playerVideoDraw(p) {
    const g = p.gl; if (!g || !p.video) return; const gl = g.gl, v = p.video;
    if (!(v.videoWidth > 0) || v.readyState < 2) return;
    let src = v, sw = v.videoWidth, sh = v.videoHeight;
    const cap = 1920;
    if (Math.max(sw, sh) > cap) {
        const s = cap / Math.max(sw, sh), pw = Math.max(1, Math.round(sw * s)), ph = Math.max(1, Math.round(sh * s));
        if (!p.vproxy) { p.vproxy = document.createElement("canvas"); p.vproxyCtx = p.vproxy.getContext("2d"); }
        if (p.vproxy.width !== pw || p.vproxy.height !== ph) { p.vproxy.width = pw; p.vproxy.height = ph; }
        try { p.vproxyCtx.drawImage(v, 0, 0, pw, ph); } catch (e) { return; }
        src = p.vproxy; sw = pw; sh = ph;
    }
    if (p.canvas.width !== sw || p.canvas.height !== sh) { p.canvas.width = sw; p.canvas.height = sh; }
    p.texW = sw; p.texH = sh;
    gl.viewport(0, 0, p.canvas.width, p.canvas.height);
    gl.useProgram(g.prog);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, g.imgTex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    try { gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, src); } catch (e) { return; }
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_3D, g.lutTex);
    gl.uniform1f(g.locs.uN, p.lutN || 33); gl.uniform1f(g.locs.uOn, p.lutReady ? 1 : 0);
    gl.uniform1f(g.locs.uExposure, p.exposure || 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
}
function _playerVideoRaf(node, p) {
    if (p.raf) return;
    const loop = (now) => {
        if (node._ocioPlayer !== p || !p.videoMode) { p.raf = 0; return; }
        _tickPlayback(p, now || 0);                          // shared video clock (seqMode=false -> drives p.video)
        _playerVideoDraw(p);
        _syncTransport(p);
        p.raf = requestAnimationFrame(loop);
    };
    p.raf = requestAnimationFrame(loop);
}
// 2026-07-03 (Ф3): resolve the streamable URL for a video source via /ocio/proxy. A browser codec (h264/vp8/vp9/
// av1) streams directly; a ProRes / DNxHR / MXF is transcoded ONCE to a cached H.264 proxy server-side (the front
// end shows "Building…" and polls until ready). Sets p.video.src when resolved; bails if the source was switched.
// Resolve a browser-playable URL for a video source: a browser codec (h264/vp8/vp9/av1) streams directly; a
// ProRes / DNxHR / MXF is transcoded ONCE to a cached H.264 proxy (/ocio/proxy), showing a spinner while it
// builds. SHARED by the OCIO Player (playerVideoStart) and the OCIO Read on-node preview (_startViewport) - both
// used to stream the raw file and so failed on ProRes/MXF. `aborted()` lets the caller bail if the source was
// switched or the node torn down mid-build. Returns the streamable URL, or null if aborted. Added 2026-07-03.
async function _resolveStreamUrl(box, path, aborted) {
    let url = "/ocio/stream?src=" + encodeURIComponent(path);
    _ocioBusy(box, true, "Processing…");                     // spinner until the stream (or its transcoded proxy) is ready (cleared on the <video> loadedmetadata / onerror)
    try {
        for (let i = 0; i < 900; i++) {                      // ~15 min ceiling (1 s poll); most proxies are a few seconds
            const r = await fetch("/ocio/proxy?src=" + encodeURIComponent(path));
            const d = await r.json().catch(() => ({}));
            if (aborted && aborted()) return null;           // source switched / node gone -> abandon
            if (d && d.ready && d.url) { url = d.url; break; }
            if (d && d.building) {
                _ocioBusy(box, true, "Building H.264 proxy (transcoding ProRes / DNxHR)…");
                await new Promise((res) => setTimeout(res, 1000));
                continue;
            }
            break;                                           // error/unknown -> fall back to the direct URL (onerror explains if truly undecodable)
        }
    } catch (e) { /* network error -> fall back to the direct URL */ }
    return (aborted && aborted()) ? null : url;
}
async function _resolveStreamSrc(node, p, path, meta) {
    const url = await _resolveStreamUrl(p.box, path, () => node._ocioPlayer !== p || p._vidPath !== path);
    if (url == null) return;
    p._vidUrl = url;
    p.empty.style.display = "none"; p.canvas.style.display = "";   // ready -> show the viewport (the busy overlay clears on the <video> loadedmetadata)
    p.video.src = url;
}
function playerVideoStart(node, p, path, meta) {
    _playerStop(p);                                          // leave the float path (cancels its rAF, frees textures)
    p.videoMode = true; p.player = null;
    // streamed video is 1-BASED (frame 1 = first). Mirror the upstream OCIO Read's numbering (frame_shift / start_frame,
    // both 1 for a plain video); a bare Load Video with no OCIO Read upstream defaults to 1. The <video> clock is a
    // 0-based index; _dispBase adds videoBase so the timeline + frame field read 1..N.
    const _r = findUpstreamRead(node);
    const _rShift = _r ? Math.round(W(_r, "frame_shift")?.value || 0) : 0;
    const _rStart = _r ? Math.round(W(_r, "start_frame")?.value || (_r._ocioSeq && _r._ocioSeq.start) || 0) : 0;
    p.videoBase = (_rShift > 0 ? _rShift : (_rStart > 0 ? _rStart : 1));
    // 2026-07-04: sync the Player's OWN fields from the streamed video + upstream Read (the FLOAT path does this via
    // syncPlayerFromUpstream, but the stream path did NOT - so start/end stayed 0, the in/out handles were not at the
    // clip extremes, and fps / input CS never pulled through). start/end span the whole clip; fps from the video meta;
    // input CS mirrors the Read's OUTPUT colorspace (what actually feeds the Player) unless the user picked one.
    const _vf = Math.max(1, meta.frames || 0);
    setWSilent(node, "start_frame", p.videoBase);
    setWSilent(node, "end_frame", p.videoBase + _vf - 1);
    setWSilent(node, "base", String(p.videoBase));
    if (meta.fps > 0) { setWSilent(node, "fps", meta.fps); p.pb.fps = meta.fps; }
    if (_r && !p.userSetCs) { const _ro = W(_r, "output_colorspace")?.value; if (_ro) setWSilent(node, "input_colorspace", _ro); }
    node.setDirtyCanvas(true, true);
    if (p._vidPath !== path) {
        p._vidPath = path; p.video.loop = false; p.pb.playing = false; p.pb.dir = 1; p.pb.revAnchor = null;
        p.video.onseeked = () => { if (p.videoMode) _playerVideoDraw(p); };   // reverse / scrub: paint each settled seek
        p.video.onloadedmetadata = () => {
            _ocioBusy(p.box, false);                         // stream ready -> clear the "Processing…" / "Building proxy…" overlay
            if (p.video.videoWidth) {
                _adoptAspect(node, p, p.video.videoWidth, p.video.videoHeight);
                renderPlayerMeta(node, { resolution: p.video.videoWidth + "x" + p.video.videoHeight, total: meta.frames || 0, cached: meta.frames || 0, fps: meta.fps || 24, input_cs: W(node, "input_colorspace")?.value });
            }
        };
        p.video.onerror = () => { _ocioBusy(p.box, false); p.videoMode = false; p.canvas.style.display = "none"; p.empty.style.display = "flex"; p.empty.firstChild.textContent = "Video: browser cannot decode this codec (a ProRes / DNxHR proxy could not be built)"; };
        _resolveStreamSrc(node, p, path, meta);              // Ф3: stream a browser codec directly, or build+stream an H.264 proxy for ProRes/DNxHR/MXF
    } else {
        _ocioBusy(p.box, false);                             // SAME video already loaded (Refresh of the same file): no reload -> no onloadedmetadata to clear the spinner the Refresh click showed, so clear it here (fixes the "Processing…" ring spinning forever)
    }
    p.pb.seqMode = false;                                    // shared transport now runs the <video> clock
    p.pb.fileFrames = Math.max(1, meta.frames || 0);
    p.pb.fps = meta.fps || 24;
    p.pb.seqFrame = 0; p.pb.seqAnchor = null;
    if (!_playerInitGL(p)) { p.canvas.style.display = "none"; p.empty.style.display = "flex"; p.empty.firstChild.textContent = "WebGL2 unavailable - cannot show the viewport"; return; }
    _playerRefreshLut(node, p);                              // bake the in_cs -> out_cs display LUT
    p.empty.style.display = "none"; p.canvas.style.display = "";
    p.pb.showTransport = true; if (p.transport) { p.transport.bar.style.display = "flex"; if (p.transport.audioRow) p.transport.audioRow.style.display = "none"; }
    if (p.refreshOverlay) { p.refreshOverlay.textContent = "↻ Refresh"; p.refreshOverlay.style.display = ""; }   // persistent Refresh in video mode: re-reads the current upstream file (switch the Load Video file -> click Refresh)
    _setVideoOutput(node, true);                             // streaming a video (any trigger) -> expose the VIDEO output
    renderPlayerMeta(node, { resolution: meta.res || "-", total: meta.frames || 0, cached: meta.frames || 0, fps: meta.fps || 24, input_cs: W(node, "input_colorspace")?.value });   // show meta now; loadedmetadata fills in the resolution
    node.setSize([node.size[0], node.computeSize()[1]]);
    _playerVideoRaf(node, p);
}
// Trace the graph back to the upstream video FILE: a standard Load Video node (its 'file' widget, resolved against
// the input dir by /ocio/stream) or an OCIO Read with a video 'source'. Lets the Player RE-READ the current file on
// Refresh - so changing the Load Video file (a widget change, NOT a connection change) is picked up without recreating
// the node, and without a backend round-trip that ComfyUI might cache. Added 2026-07-03.
// 2026-07-03 (BUG B): OCIO color-processing node types (NOT the sources Read/LoadVideo, NOT the Player). If ANY of
// these sits between a video source and the Player, streaming the raw source file would silently BYPASS its color
// transform - the Player would show the untouched video, ignoring the intermediate node. So when one is crossed, the
// trace returns null and the caller falls through to a normal render of the PROCESSED (materialized, capped) batch.
const OCIO_PROC_TYPES = new Set(["OCIOLogConvert", "OCIOColorSpace", "OCIODisplay", "OCIOCDLTransform",
    "OCIOFileTransform", "OCIOLookTransform", "OCIOGrade", "OCIOGradeMatch", "OCIOApplyGrade"]);
function _playerTraceVideoSrc(node, seen, crossedProc) {
    try {
        seen = seen || new Set();
        if (!node || seen.has(node.id)) return null;
        seen.add(node.id);
        // A video source is streamable ONLY if reached with NO processing node crossed (crossedProc falsy). With a
        // processing node in between, return null -> the caller renders the PROCESSED batch instead of the raw stream.
        if (node.type === "LoadVideo") { if (crossedProc) return null; const w = W(node, "file"); return (w && w.value) ? String(w.value) : null; }
        if (node.type === "OCIORead") { if (crossedProc) return null; const s = W(node, "source")?.value; return (s && /\.(mov|mp4|mkv|avi|webm|mxf|m4v)$/i.test(String(s))) ? String(s) : null; }
        for (const inp of (node.inputs || [])) {
            if (inp.link == null) continue;
            const link = app.graph.links[inp.link]; if (!link) continue;
            const origin = app.graph.getNodeById(link.origin_id);
            const nextCrossed = crossedProc || (origin && OCIO_PROC_TYPES.has(origin.type));   // crossing INTO a color-processing node taints the stream
            const f = _playerTraceVideoSrc(origin, seen, nextCrossed);
            if (f) return f;
        }
    } catch (e) { console.warn("[OCIO] video trace failed:", e); }   // never let a graph-walk error kill the Refresh onclick -> it falls through to a normal render
    return null;
}
async function _playerVideoRefresh(node) {
    const p = node._ocioPlayer || ensurePlayer(node);
    const src = _playerTraceVideoSrc(node);
    if (!src) { app.queuePrompt(0, 1); return; }              // no traceable video source -> fall back to a full render
    let fps = 24, frames = 0;
    try {
        const r = await fetch("/ocio/seq_range", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source: src }) });
        const d = await r.json(); fps = d.fps || 24; frames = d.count || 0;
    } catch (e) {}
    playerVideoStart(node, p, src, { fps, frames });          // stream the CURRENT upstream file (re-read -> switching the Load Video file works)
}
function ensurePlayer(node) {
    if (node._ocioPlayer) return node._ocioPlayer;
    const box = document.createElement("div");
    box.style.cssText = "width:100%;height:100%;position:relative;display:flex;justify-content:center;align-items:center;overflow:hidden;background:#111;";
    const canvas = document.createElement("canvas");
    canvas.style.cssText = "max-width:100%;max-height:100%;object-fit:contain;display:none;";   // 100%: scale with the node (Load-Image-style); exposure lives in the transport strip, not the viewport
    // "No media" placeholder + a Refresh affordance (the float data only exists after the graph runs -> onExecuted)
    const empty = document.createElement("div");
    empty.style.cssText = "display:flex;flex-direction:column;align-items:center;gap:8px;color:#889;font:12px sans-serif;padding:24px;";
    const emptyMsg = document.createElement("div"); emptyMsg.textContent = "No media - Render to view";
    const refreshBtn = document.createElement("button");
    refreshBtn.textContent = "↻ Refresh";
    refreshBtn.style.cssText = "padding:5px 12px;border:0;border-radius:4px;background:#2b2b40;color:#cde;cursor:pointer;font:12px sans-serif;";
    refreshBtn.onmouseenter = () => refreshBtn.style.background = "#39395a";
    refreshBtn.onmouseleave = () => refreshBtn.style.background = "#2b2b40";
    refreshBtn.onclick = () => { _ocioBusy(node._ocioPlayer && node._ocioPlayer.box, true, "Processing…"); if (_playerTraceVideoSrc(node)) _playerVideoRefresh(node); else app.queuePrompt(0, 1); };   // video upstream -> stream it directly; else render (OCIOPlayer is OUTPUT_NODE)
    empty.append(emptyMsg, refreshBtn);
    // Auto-Refresh overlay: floats OVER the viewport when the node's INPUT changes (a node inserted / rewired
    // upstream, e.g. an OCIO Log Converter dropped in between) - the cached frames are then stale, so this prompts a
    // re-render. Click = Queue (renders this OUTPUT_NODE). Hidden until an input change; hidden again on the next render.
    const refreshOverlay = document.createElement("button");
    refreshOverlay.textContent = "↻ Refresh (input changed)";
    refreshOverlay.title = "The input changed - click to re-render this node";
    refreshOverlay.style.cssText = "position:absolute;top:6px;left:50%;transform:translateX(-50%);z-index:5;display:none;padding:4px 10px;border:0;border-radius:4px;background:rgba(40,40,64,0.92);color:#cde;cursor:pointer;font:11px sans-serif;box-shadow:0 1px 4px rgba(0,0,0,0.5);";
    refreshOverlay.onmouseenter = () => refreshOverlay.style.background = "rgba(57,57,90,0.95)";
    refreshOverlay.onmouseleave = () => refreshOverlay.style.background = "rgba(40,40,64,0.92)";
    refreshOverlay.onclick = () => { const pp = node._ocioPlayer; _ocioBusy(pp && pp.box, true, "Processing…"); if (pp && (pp.videoMode || _playerTraceVideoSrc(node))) _playerVideoRefresh(node); else app.queuePrompt(0, 1); };   // video upstream -> re-read the file; else render
    // Exposure control now lives HORIZONTALLY in the transport strip (between the viewport and the timeline), built
    // in _ensureTransport when p.isPlayer is set. No slider inside the viewport anymore. Owner spec 2026-07-03 (Task C).
    const video = document.createElement("video");           // Ф1b: hidden <video> for streaming a video source into the WebGL viewport (exposure + LUT shader)
    video.muted = true; video.loop = false; video.playsInline = true; video.setAttribute("playsinline", ""); video.style.display = "none";
    box.append(empty, canvas, refreshOverlay, video);
    const w = node.addDOMWidget("player", "div", box, { serialize: false });
    w.computeSize = (width) => [0, (node._ocioPlayer && node._ocioPlayer.player) ? _previewH(node, node._ocioPlayer, width) : 90];   // scale with node width (aspect-locked), like Load Image
    w._ocioAlwaysVisible = true;
    const p = { node, box, canvas, empty, isPlayer: true, gl: null, lutN: 33, lutReady: false,
                raf: 0, exposure: 0, texW: 0, texH: 0, player: null, autoCsChecked: false, userSetCs: false };
    // pb: reuse the transport's playback-state shape. seqMode TRUE (frame-index clock, no <video>); seq stays null.
    p.pb = { playing: false, dir: 1, mode: "loop", fps: 24, showTransport: false, seqMode: true, seqFrame: 0,
             seqAnchor: null, fileFrames: 1 };
    node._ocioPlayer = p;
    p.refreshOverlay = refreshOverlay;
    p.video = video;                                     // Ф1b: streamed-video element (drawn via the exposure+LUT shader)
    _ensureTransport(node, p);                           // shared transport bar (exposure strip + playback), drives seqFrame via _pbSeek/_playerShow
    node.onRemoved = (orig => function () { _playerStop(node._ocioPlayer); return orig && orig.apply(this, arguments); })(node.onRemoved);
    // Show the auto-Refresh overlay when an INPUT connection changes (a node plugged in / rewired upstream) - the
    // cached render is now stale. LiteGraph.INPUT === 1. Only once a render exists (before that, the empty-state Refresh covers it).
    node.onConnectionsChange = (orig => function (type, idx, connected, link_info) {
        try {
            const pp = node._ocioPlayer;
            if (type === 1 && pp) {
                pp._lastExecSig = null;                                                            // input re-wired -> force the next render to re-init the viewport (don't skip as "unchanged")
                if (_playerTraceVideoSrc(node)) _playerVideoRefresh(node);                         // a video source (Load Video / Read) is upstream -> stream the current file
                else if ((pp.player || pp.videoMode) && pp.refreshOverlay) pp.refreshOverlay.style.display = "";   // a float source changed, OR a processing node was inserted into a video chain (trace now null) -> prompt a re-render
            }
        } catch (e) {}
        return orig && orig.apply(this, arguments);
    })(node.onConnectionsChange);
    return p;
}
// Fit the node to its content height (viewport + transport + meta), then redraw. Guarded against reentrancy:
// on the Vue-nodes frontend node.setSize fires onResize, which calls this again -> without the guard that
// recursed until "Maximum call stack size exceeded". The guard makes the inner setSize a no-op. The exposure
// strip lives in the transport DOM widget (flex layout), so it stretches with the node width automatically -
// this just refits the height and redraws.
function _playerLayout(node) {
    const p = node._ocioPlayer; if (!p) return;
    if (!p._laying) {
        p._laying = true;
        try { node.setSize([node.size[0], node.computeSize()[1]]); } finally { p._laying = false; }
    }
    if (p.player) _playerDraw(p);
}
// onExecuted payload -> wire up the float viewport + metadata. Fields arrive as 1-element arrays (ComfyUI ui).
function playerOnExecuted(node, message) {
    const p = ensurePlayer(node);
    _ocioBusy(p.box, false);                                 // a render result arrived -> clear the "Processing…" overlay (video path re-shows "Building…" below if it must transcode a proxy)
    const first = (v) => Array.isArray(v) ? v[0] : v;
    // 2026-07-04: SKIP the re-init (which restarts playback / re-caches the batch / re-fetches frames) when this
    // execution result is IDENTICAL to the last. Rendering ANOTHER node in the same graph re-runs this OUTPUT_NODE
    // viewer with the same result, and the viewer must NOT "re-play itself" on every unrelated render (owner). A real
    // change (new source, new size / frame count) has a different signature and still re-inits; onConnectionsChange
    // clears _lastExecSig so a genuine re-wire always re-inits too.
    const _sig = JSON.stringify([first(message && message.video_path), first(message && message.player_dir),
        first(message && message.player_total), first(message && message.player_cached),
        first(message && message.resolution), first(message && message.input_cs)]);
    if (_sig === p._lastExecSig && (p.player || p.videoMode)) return;   // unchanged -> leave the current viewport playing
    p._lastExecSig = _sig;
    // VIDEO source (a Load Video / OCIO Read video traced to a file): stream it client-side, NOT the float batch.
    // Phase 1a: capture the path + metadata; Phase 1b streams it via WebCodecs. For now show a placeholder + meta.
    const vpath = first(message && message.video_path);
    if (vpath) {
        const vres = first(message.video_res) || "", vfps = parseFloat(first(message.video_fps)) || 24, vframes = parseInt(first(message.video_frames), 10) || 0;
        p.videoSrc = { path: vpath, res: vres, fps: vfps, frames: vframes };
        _setVideoOutput(node, true);                                        // R1: video source -> expose the VIDEO output
        renderPlayerMeta(node, { resolution: vres, total: vframes, cached: vframes, fps: vfps, input_cs: first(message.input_cs) });
        playerVideoStart(node, p, vpath, { fps: vfps, frames: vframes });   // stream the whole clip (native decode + exposure/LUT shader)
        return;
    }
    if (p.videoMode) { p.videoMode = false; try { p.video.pause(); } catch (e) {} if (p.raf) { cancelAnimationFrame(p.raf); p.raf = 0; } }   // was streaming a video, now a float batch -> stop the stream
    _setVideoOutput(node, false);                            // R1: float batch (image/sequence) -> hide the VIDEO output
    const dir = first(message && message.player_dir);
    const total = parseInt(first(message && message.player_total) || "0", 10);
    const cached = parseInt(first(message && message.player_cached) || "0", 10);
    const resolution = first(message && message.resolution) || "";
    const fps = parseFloat(first(message && message.fps) || "") || 0;
    const inputCs = first(message && message.input_cs) || "";
    if (!dir || !(cached > 0)) { renderPlayerMeta(node, null); return; }
    p.player = { dir, total, cached, resolution };
    _playerClearTex(p);                                  // fresh render -> the old frame textures are stale, drop them
    if (p.refreshOverlay) p.refreshOverlay.style.display = "none";   // rendered -> no longer stale, hide the input-changed prompt
    p.autoCsChecked = false;                             // re-evaluate HDR auto-cs for this fresh render
    p._playerFirstErr = null;
    p.pb.fileFrames = cached;                            // transport ruler spans the CACHED frames (0..cached-1)
    p.pb.seqFrame = Math.max(0, Math.min(cached - 1, p.pb.seqFrame | 0));
    if (fps) { p.pb.fps = fps; }
    syncPlayerFromUpstream(node);                        // mirror source frame numbering (base) + fps from the upstream OCIO Read, through any chain of nodes
    // show the viewport, hide the placeholder; show the transport bar
    p.empty.style.display = "none"; p.canvas.style.display = "";
    p.pb.showTransport = true; if (p.transport) { p.transport.bar.style.display = "flex"; if (p.transport.audioRow) p.transport.audioRow.style.display = "none"; }
    if (!_playerInitGL(p)) {                             // no WebGL2 -> message, no viewport
        p.canvas.style.display = "none"; p.empty.style.display = "flex";
        p.empty.firstChild.textContent = "WebGL2 unavailable - cannot show float viewport";
        renderPlayerMeta(node, { resolution, total, cached, fps: p.pb.fps, input_cs: inputCs });   // p.pb.fps = source fps after syncPlayerFromUpstream
        return;
    }
    _playerLayout(node);
    _playerRefreshLut(node, p);                          // bake in_cs->out_cs display LUT
    _playerShow(p);                                      // upload + draw the current frame
    _playerPrefetch(p);                                  // warm the rest of [in,out] into the texture cache (teal bar shows progress)
    _playerEnsureRaf(node, p);
    renderPlayerMeta(node, { resolution, total, cached, fps: p.pb.fps, input_cs: inputCs });   // p.pb.fps = source fps after syncPlayerFromUpstream
}

// ---- Player metadata panel: resolution / frames / fps / colorspace, from the onExecuted payload + widgets.
const PLAYER_META_ROWS = [
    ["resolution", "Resolution"], ["frames", "Frames"], ["range", "Range"], ["fps", "FPS"],
    ["input_colorspace", "Input CS"], ["output_colorspace", "Output CS"],
];
function ensurePlayerMeta(node) {
    if (node._ocioPlayerMeta) return node._ocioPlayerMeta;
    const box = document.createElement("div");
    box.style.cssText = "width:100%;font:10px/1.4 monospace;color:#9cf;background:#1a1a1a;padding:4px 6px;box-sizing:border-box;overflow:hidden;white-space:nowrap;";
    const w = node.addDOMWidget("player_meta", "div", box, { serialize: false });
    w.computeSize = () => [0, 16 * PLAYER_META_ROWS.length + 8];
    w._ocioAlwaysVisible = true;
    node._ocioPlayerMeta = box;
    return box;
}
function renderPlayerMeta(node, data) {
    const box = ensurePlayerMeta(node);
    if (!data) { box.innerHTML = ""; return; }
    const framesTxt = data.cached < data.total ? `${data.total} (viewer capped at ${data.cached})` : String(data.total);
    const pp = node._ocioPlayer;
    const base = (pp && pp.videoMode && pp.videoBase) ? (pp.videoBase | 0)        // streamed video: 1-based (or the upstream Read's numbering)
               : ((pp && pp.player && pp.player.base) ? (pp.player.base | 0) : 0);   // float batch: mirrored from the upstream OCIO Read
    const rangeTxt = `${base}-${base + Math.max(1, data.cached || 1) - 1}`;
    const values = {
        resolution: data.resolution || "-",
        frames: framesTxt,
        range: rangeTxt,
        fps: data.fps ? data.fps.toFixed(3) : (parseFloat(W(node, "fps")?.value) || 0).toFixed(3),
        input_colorspace: W(node, "input_colorspace")?.value || data.input_cs || "-",
        output_colorspace: W(node, "output_colorspace")?.value || "-",
    };
    box.innerHTML = PLAYER_META_ROWS.map(([k, label]) => `<div>${label}: ${values[k]}</div>`).join("");
}

// OCIO Write "Render" button (owner spec 2026-07-04): (1) overwrite guard - ask the server which output files this
// Write would create and which already exist; if any exist, confirm before overwriting (Cancel aborts). (2) bump the
// hidden render_nonce so ComfyUI does NOT cache an identical Write - a repeat render to the SAME path actually
// re-writes (the reported bug: 2nd click / after deleting the file wrote nothing). window.confirm = the standard
// Overwrite / Cancel dialog the owner asked for.
async function ocioWriteRender(node) {
    try {
        const params = {
            output_folder: W(node, "output_folder")?.value || "", filename: W(node, "filename")?.value || "",
            container: W(node, "container")?.value, still_format: W(node, "still_format")?.value,
            video_codec: W(node, "video_codec")?.value, output_colorspace: W(node, "output_colorspace")?.value,
            raw_data: W(node, "raw_data")?.value ? 1 : 0, colorspace_in_name: W(node, "colorspace_in_name")?.value ? 1 : 0,
            start_number: parseInt(W(node, "start_number")?.value, 10) || 1,
        };
        if (params.container === "still image") {            // a still grabbed from a sequence / video stamps its frame number -> match that name for the exists-check
            const _r = findUpstreamRead(node), _k = _r && _r._ocioSeq && _r._ocioSeq.kind;
            if (_k === "sequence" || _k === "video") params.still_frame = parseInt(W(node, "first_frame")?.value, 10);
        }
        const r = await fetch("/ocio/write_paths", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(params) });
        const d = await r.json();
        if (d && Array.isArray(d.existing) && d.existing.length) {
            const n = d.existing.length, sample = String(d.existing[0]).split(/[\\/]/).pop();
            const msg = n === 1 ? `File already exists:\n\n${sample}\n\nOverwrite it?`
                                : `${n} files already exist (e.g. ${sample}).\n\nOverwrite them?`;
            if (!window.confirm(msg)) return;                     // Cancel -> abort the render, write nothing
        }
    } catch (e) { /* existence probe failed -> don't block the render */ }
    const w = W(node, "render_nonce"); if (w) w.value = String(Date.now());   // bump -> ComfyUI cache miss -> re-writes even to the same path
    app.queuePrompt(0, 1);
}
app.registerExtension({
    name: "ComfyUI-OCIO.io",
    async setup() {
        // Run / Queue feedback (owner spec 2026-07-03): show the "Processing…" spinner on an OCIO Read/Player
        // viewport while THAT node is executing in the graph - covers the global Run button (the node's own Refresh
        // button shows it immediately on click). Cleared when the node's result arrives (playerOnExecuted / img
        // load / video ready) or when the queue goes idle / errors.
        const api = app.api;
        if (!api || !api.addEventListener) return;
        const ocioNodes = () => ((app.graph && app.graph._nodes) || []).filter(n => n.type === "OCIOPlayer" || n.type === "OCIORead");
        const hideAll = () => { for (const n of ocioNodes()) _ocioBusyNode(n, false); };
        api.addEventListener("executing", (e) => {
            const d = e && e.detail;
            if (d == null) { hideAll(); return; }            // null detail = queue idle -> clear every OCIO spinner
            const id = String(d);
            for (const n of ocioNodes()) if (String(n.id) === id) _ocioBusyNode(n, true, "Processing…");
        });
        api.addEventListener("execution_error", hideAll);
        api.addEventListener("execution_interrupted", hideAll);
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        // Uniform slot labels on EVERY OCIO node: an IMAGE carries a still, a sequence, or a video, so show the
        // short "img/seq/vid" on all IMAGE inputs (named image/images) and IMAGE outputs. Labels ONLY - the
        // underlying slot names (run() param keys / RETURN_NAMES) are untouched, so connections still resolve.
        // Runs for the color/grade nodes too, which have no other front-end onNodeCreated. Owner spec 2026-07-03.
        if (nodeData.category === "OCIO" || String(nodeData.name || "").startsWith("OCIO")) {
            const _ocLabel = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const rr = _ocLabel ? _ocLabel.apply(this, arguments) : undefined;
                const relabel = () => {
                    for (const s of (this.inputs || [])) if (s.type === "IMAGE" && (s.name === "image" || s.name === "images")) s.label = "img/seq/vid";
                    for (const s of (this.inputs || [])) if (s.type === "VIDEO") s.label = "Load Video Node";   // OCIO Player: the VIDEO input takes a Load Video node (streamed, not materialized)
                    for (const s of (this.outputs || [])) if (s.type === "IMAGE") s.label = "img/seq/vid";
                    this.setDirtyCanvas(true, true);
                };
                relabel(); setTimeout(relabel, 0);                 // now + after slots finish populating
                return rr;
            };
        }
        if (nodeData.name === "OCIORead") {
            const onCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const r = onCreated ? onCreated.apply(this, arguments) : undefined;
                // one file picker: browse ANY path on disk straight into `source`. No copy - OCIORead reads it
                // in place, which is what a local workflow wants (no duplicating big EXR sequences / video into
                // the input folder). uploadRead() (copy-into-input) still exists if we ever want to re-expose it.
                const browseBtn = this.addWidget("button", "Open Files", null,
                    () => openBrowser(this, { widget: "source", pickFiles: true }), { serialize: false });
                browseBtn._ocioAlwaysVisible = true;
                // The `source` STRING widget IS the editable path (type a file / sequence / video, or fill it via
                // Open Files). Tooltip clarifies it is not a duplicate of the button. Added 2026-07-03.
                const srcW = W(this, "source");
                if (srcW) srcW.tooltip = "Path to a file / sequence / video - type it here, or use Open Files. This is the source; the button just fills it.";
                // Collapsible Viewer (OCIO Read only): a disclosure toggle that folds the whole preview + transport +
                // metadata block away and back (owner spec 2026-07-03). Default expanded; runtime-only, not serialized.
                const self = this;
                const viewerToggle = this.addWidget("button", "▾ Viewer", null, () => {
                    const c = self._ocioReadCollapsed = !self._ocioReadCollapsed;
                    viewerToggle.name = (c ? "▸" : "▾") + " Viewer";
                    const p = self._ocioPrev;
                    if (p && p.box) p.box.style.display = c ? "none" : "flex";
                    if (self._ocioMeta) self._ocioMeta.style.display = c ? "none" : "";
                    if (p && p.transport) p.transport.bar.style.display = c ? "none" : ((p.pb && p.pb.showTransport) ? "flex" : "none");
                    self.setSize([self.size[0], self.computeSize()[1]]);
                    self.setDirtyCanvas(true, true);
                }, { serialize: false });
                viewerToggle._ocioAlwaysVisible = true;
                ensureReadPreview(this);                                          // instant preview at the bottom
                ensureReadMeta(this);                                             // metadata panel, under the preview
                this._ocioAllWidgets = this.widgets.slice();                      // full ordered list, captured once
                onChange(this, "source", (v) => { setW(this, "input_colorspace", autoInCs(v)); fillRange(this, v); updateReadMeta(this); });   // fillRange calls updateReadPreview once _ocioSeq is known
                for (const w of ["input_colorspace", "output_colorspace", "raw_data"]) {
                    onChange(this, w, () => updateReadPreview(this));  // colorspace change -> re-render the thumb
                }
                for (const w of ["frame_shift", "fps", "start_frame", "end_frame"]) {
                    onChange(this, w, () => resyncAllWrites());   // Read range/shift/fps -> downstream Writes
                }
                const node = this;
                setTimeout(() => {                                                // fresh node: detect the default
                    fillRange(node, W(node, "source")?.value);
                    updateReadPreview(node);
                    updateReadMeta(node);
                }, 0);
                return r;
            };
            const onConfig = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function () {
                const r = onConfig ? onConfig.apply(this, arguments) : undefined;
                const node = this;
                setTimeout(() => {                                                // loaded workflow: re-detect
                    fillRange(node, W(node, "source")?.value);
                    updateReadPreview(node);
                    updateReadMeta(node);
                }, 0);
                return r;
            };
            // colorspace label (input -> output) in the title bar
            const onDraw = nodeType.prototype.onDrawForeground;
            nodeType.prototype.onDrawForeground = function (ctx) {
                onDraw && onDraw.apply(this, arguments);
                if (this.flags && this.flags.collapsed) return;
                const a = W(this, "input_colorspace"), b = W(this, "output_colorspace");
                if (!a || !b) return;
                ctx.save();
                ctx.font = "10px sans-serif"; ctx.fillStyle = "#9cf"; ctx.textAlign = "right";
                ctx.fillText(`${shorten(a.value)} → ${shorten(b.value)}`, this.size[0] - 8, -6);
                const seq = this._ocioSeq;
                if (seq && seq.kind === "sequence") {
                    ctx.textAlign = "left"; ctx.font = "9px sans-serif";
                    ctx.fillStyle = "#7a9";
                    ctx.fillText(`original range [${seq.orig_start}-${seq.orig_end}]  ${seq.count} frames`, 8, this.size[1] - 18);
                    if (seq.missing_count) {
                        ctx.fillStyle = "#e88";
                        ctx.fillText(`missing frames: ${seq.missing}`, 8, this.size[1] - 6);
                    }
                }
                ctx.restore();
            };
        }

        if (nodeData.name === "OCIOWrite") {
            const onCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const r = onCreated ? onCreated.apply(this, arguments) : undefined;
                const node = this;
                const applyFormat = () => {
                    const fmt = W(node, "still_format")?.value, bw = W(node, "bit_depth");
                    if (bw && BITS[fmt]) {
                        bw.options.values = BITS[fmt].slice();
                        if (!BITS[fmt].includes(bw.value)) bw.value = BIT_DEF[fmt];
                    }
                };
                // compression only makes sense for EXR stills/sequences; container-change and still_format-change
                // both need this same rule, so it lives in one place (DRY)
                const applyCompressionVis = () => {
                    const c = W(node, "container")?.value, isVideo = c === "video";
                    showWidget(node, W(node, "compression"), !isVideo && W(node, "still_format")?.value === "exr");
                };
                const applyContainer = () => {
                    const c = W(node, "container")?.value, isVideo = c === "video", isStill = c === "still image";
                    const isSeq = c === "sequence";
                    showWidget(node, W(node, "still_format"), !isVideo);
                    showWidget(node, W(node, "video_codec"), isVideo);
                    showWidget(node, W(node, "bit_depth"), !isVideo);          // video's real depth is in the codec footer instead
                    applyCompressionVis();
                    showWidget(node, W(node, "auto_range"), !isStill);         // still image writes one chosen frame, no range
                    showWidget(node, W(node, "first_frame"), true);            // always shown (relabelled below)
                    showWidget(node, W(node, "last_frame"), !isStill);
                    showWidget(node, W(node, "start_number"), isSeq);
                    const fpsW = W(node, "fps");                                // optional input; only toggle if it renders as a widget
                    if (fpsW) showWidget(node, fpsW, isVideo);
                    showWidget(node, W(node, "source_start"), false);          // internal (set by the wire)
                    showWidget(node, W(node, "auto_colorspace"), false);       // legacy LTX auto-detect, superseded by profile="auto"
                    const ff = W(node, "first_frame");                         // relabel the shared field
                    if (ff) {
                        ff.label = isStill ? "frame to save" : "first_frame";
                        ff.tooltip = isStill
                            ? "which single frame to write, default 1"
                            : "first frame number to write (auto-filled from the source when auto_range is ON)";
                    }
                    applyCodecLabel();
                    applyFormat();
                    setW(node, "output_colorspace", autoOutCs(c, W(node, "still_format")?.value));
                    pokeWidgets(node);                                          // Vue re-render (hides + labels)
                    node.setSize([node.size[0], node.computeSize()[1]]);
                    node.setDirtyCanvas(true, true);
                };
                // the codec's REAL depth + container extension live in the widget label (visible on every
                // frontend; the canvas footer below only draws on legacy non-Vue frontends)
                const applyCodecLabel = () => {
                    const vc = W(node, "video_codec");
                    const info = vc && CODEC_INFO[vc.value];
                    if (vc) vc.label = info ? `video_codec (${info.bits}, ${info.ext})` : "video_codec";
                };
                onChange(this, "container", applyContainer);
                onChange(this, "still_format", () => {
                    applyFormat();
                    applyCompressionVis();
                    setW(node, "output_colorspace", autoOutCs(W(node, "container")?.value, W(node, "still_format")?.value));
                    pokeWidgets(node);
                });
                onChange(this, "video_codec", () => { applyCodecLabel(); pokeWidgets(node); node.setDirtyCanvas(true, true); });
                // auto frame range / fps from the upstream OCIO Read
                onChange(this, "auto_range", (v) => { if (v) syncWriteFromUpstream(node); });
                for (const w of ["last_frame", "start_number"]) {
                    onChange(this, w, () => { const ar = W(node, "auto_range"); if (ar) ar.value = false; });  // manual edit -> auto OFF
                }
                onChange(this, "first_frame", () => {
                    const ar = W(node, "auto_range"); if (ar) ar.value = false;                                // manual edit -> auto OFF
                    // output keeps the SOURCE frame numbers: start_number tracks first_frame, so rendering frame 39
                    // (first=last=39) names it 0039, not 0000. A later manual start_number edit still overrides (re-base).
                    const ff = W(node, "first_frame"); if (ff) setWSilent(node, "start_number", ff.value);
                    node.setDirtyCanvas(true, true);
                });
                // profile: a concrete HDR preset silently drives from/output colorspace + still_format/bit_depth;
                // "auto" resolves via resolveAutoProfile (upstream trace); a manual colorspace edit flips back to "none"
                onChange(this, "profile", (v) => { if (v !== "auto") applyProfile(node, v); });
                for (const w of ["from_colorspace", "output_colorspace"]) {
                    onChange(this, w, () => {
                        if (node._ocioProfileSetting) return;               // our own silent write, not a user edit
                        const pw = W(node, "profile");
                        if (pw && pw.value !== "none") { pw.value = "none"; node.setDirtyCanvas(true, true); }
                    });
                }
                showWidget(this, W(this, "render_nonce"), false);   // internal cache-buster - hidden with a true collapse (no blank row)
                this.addWidget("button", "Output Folder", null, () => openFolderDialog(this), { serialize: false });
                this.addWidget("button", "▶ Render", null, () => ocioWriteRender(this), { serialize: false });
                setTimeout(() => { applyContainer(); syncWriteFromUpstream(node); resolveAutoProfile(node); }, 0);
                return r;
            };
            const onConn = nodeType.prototype.onConnectionsChange;
            nodeType.prototype.onConnectionsChange = function () {
                const r = onConn ? onConn.apply(this, arguments) : undefined;
                const node = this;
                setTimeout(() => { syncWriteFromUpstream(node); resolveAutoProfile(node); }, 0);   // wire (re)connected -> pull range/fps + auto profile
                return r;
            };
            const onConfigW = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function () {
                const r = onConfigW ? onConfigW.apply(this, arguments) : undefined;
                const node = this;
                setTimeout(() => { syncWriteFromUpstream(node); resolveAutoProfile(node); }, 0);   // loaded workflow -> re-detect
                return r;
            };
            const onDraw = nodeType.prototype.onDrawForeground;
            nodeType.prototype.onDrawForeground = function (ctx) {
                onDraw && onDraw.apply(this, arguments);
                if (this.flags && this.flags.collapsed) return;
                const a = W(this, "from_colorspace"), b = W(this, "output_colorspace");
                if (!a || !b) return;
                const fmt = (W(this, "container")?.value === "video") ? W(this, "video_codec")?.value : W(this, "still_format")?.value;
                ctx.save();
                ctx.font = "10px sans-serif"; ctx.fillStyle = "#9cf"; ctx.textAlign = "right";
                ctx.fillText(`${shorten(a.value)} → ${shorten(b.value)}  [${fmt}]`, this.size[0] - 8, -6);
                ctx.font = "9px sans-serif"; ctx.fillStyle = "#7a9"; ctx.textAlign = "left";
                ctx.fillText("→ " + exampleName(this), 8, this.size[1] - 6);
                if (W(this, "container")?.value === "video") {
                    const info = CODEC_INFO[fmt];
                    if (info) {
                        ctx.fillStyle = "#7a9"; ctx.textAlign = "left"; ctx.font = "9px sans-serif";
                        ctx.fillText(`${CODEC_LABEL[fmt] || fmt} - ${info.bits}`, 8, this.size[1] - 18);
                    }
                }
                if (this._ocioWrote != null) {
                    ctx.fillStyle = "#6c6"; ctx.textAlign = "right";
                    ctx.fillText(`✓ wrote ${this._ocioWrote} frame(s)`, this.size[0] - 8, this.size[1] - 6);
                }
                ctx.restore();
            };
            const onExec = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function (message) {
                onExec && onExec.apply(this, arguments);
                const c = message && message.count;
                if (c) {
                    this._ocioWrote = Array.isArray(c) ? c[0] : c; this.setDirtyCanvas(true, true);
                    // Vue frontends do not draw the canvas "wrote N" corner text; a toast carries the count there
                    app.extensionManager?.toast?.add?.({ severity: "success", summary: "OCIO Write",
                        detail: `wrote ${this._ocioWrote} frame(s)`, life: 4000 });
                }
            };
        }

        if (nodeData.name === "OCIOPlayer") {
            const onCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const r = onCreated ? onCreated.apply(this, arguments) : undefined;
                ensurePlayer(this);                                           // float WebGL viewport + exposure slider
                ensurePlayerMeta(this);                                       // metadata panel under it
                renderPlayerMeta(this, null);                                 // empty until a render arrives
                // a manual colorspace edit wins over the HDR auto-guess; live colorspace change -> re-bake the LUT
                for (const w of ["input_colorspace", "output_colorspace", "raw_data"]) {
                    onChange(this, w, () => {
                        const p = this._ocioPlayer; if (!p) return;
                        if (w === "input_colorspace") p.userSetCs = true;     // user picked -> auto-cs must not override
                        _playerRefreshLut(this, p);                          // re-bake display LUT + redraw
                        renderPlayerMeta(this, p.player ? { resolution: p.player.resolution, total: p.player.total,
                            cached: p.player.cached, fps: p.pb.fps } : null);
                    });
                }
                // fps / range edits: keep the transport + meta in sync (transport reads the widgets live anyway)
                for (const w of ["fps", "start_frame", "end_frame"]) {
                    onChange(this, w, () => { const p = this._ocioPlayer; if (p) { _syncTransport(p); } });
                }
                showWidget(this, W(this, "base"), false);            // 'base' = hidden frontend->backend channel (source first-frame number). showWidget now hides WITHOUT a type-swap (options.hidden + zeroed computeSize), so the value keeps serializing (the old type-swap once blanked it -> '' -> crashed prompt validation).
                _setVideoOutput(this, false);                        // R1: hide the VIDEO output until a video is rendered (playerOnExecuted re-adds it)
                this._ocioAllWidgets = this.widgets.slice();
                return r;
            };
            // the node is resizable; on a live resize just REDRAW the viewport (the DOM widget + the flex:1 exposure
            // slider already stretch with the node - so no setSize here, which would fight the user's drag and, on the
            // Vue frontend, re-enter onResize). Content-fit setSize happens once in playerOnExecuted via _playerLayout.
            const onResize = nodeType.prototype.onResize;
            nodeType.prototype.onResize = function (size) {
                const r = onResize ? onResize.apply(this, arguments) : undefined;
                const p = this._ocioPlayer; if (p && p.player) _playerDraw(p);
                return r;
            };
            // onExecuted delivers the ui payload (player_dir / player_total / player_cached / resolution / fps / input_cs)
            const onExec = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function (message) {
                onExec && onExec.apply(this, arguments);
                try { playerOnExecuted(this, message); } catch (e) { console.error("[OCIO Player] onExecuted:", e); }
            };
            // colorspace label (input -> output) in the title bar, same as OCIO Read
            const onDraw = nodeType.prototype.onDrawForeground;
            nodeType.prototype.onDrawForeground = function (ctx) {
                onDraw && onDraw.apply(this, arguments);
                if (this.flags && this.flags.collapsed) return;
                const a = W(this, "input_colorspace"), b = W(this, "output_colorspace");
                if (!a || !b) return;
                ctx.save();
                ctx.font = "10px sans-serif"; ctx.fillStyle = "#9cf"; ctx.textAlign = "right";
                ctx.fillText(`${shorten(a.value)} → ${shorten(b.value)}`, this.size[0] - 8, -6);
                ctx.restore();
            };
        }
    },
});
