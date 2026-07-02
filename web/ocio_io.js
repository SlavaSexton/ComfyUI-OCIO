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

// hide / show a widget (type swap + zero height; survives older and newer ComfyUI). Used by OCIO Write only.
// OCIO Read uses setVisibleWidgets below instead (widget.hidden + zeroed computeSize, no type swap) - see
// that function for why (serialization, not just layout, is the reason the two nodes differ here).
const OCIO_HIDDEN = "ocio-hidden";
function showWidget(node, w, visible) {
    if (!w) return;
    if (visible) {
        if (w.type === OCIO_HIDDEN) { w.type = w._oType; w.computeSize = w._oCompute; }
        w.hidden = false;
    } else if (w.type !== OCIO_HIDDEN) {
        w._oType = w.type; w._oCompute = w.computeSize;
        w.type = OCIO_HIDDEN; w.computeSize = () => [0, -4]; w.hidden = true;
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
function ensureReadPreview(node) {
    if (node._ocioPrev) return node._ocioPrev;
    const box = document.createElement("div");
    box.style.cssText = "width:100%;height:100%;display:flex;justify-content:center;align-items:center;overflow:hidden;";
    const img = document.createElement("img");
    img.style.cssText = "max-width:100%;max-height:200px;object-fit:contain;display:none;";
    img.onerror = () => { img.style.display = "none"; };
    const video = document.createElement("video");
    video.muted = true; video.loop = true; video.playsInline = true; video.setAttribute("playsinline", "");
    video.style.display = "none";
    const canvas = document.createElement("canvas");
    canvas.style.cssText = "max-width:100%;max-height:200px;object-fit:contain;display:none;";
    box.append(img, video, canvas);
    const w = node.addDOMWidget("preview", "div", box, { serialize: false });
    w.computeSize = () => [0, 210];
    w._ocioAlwaysVisible = true;                      // always shown, regardless of source kind
    node._ocioPrev = { img, video, canvas, gl: null, lutN: 33, lutReady: false, raf: 0, streamUrl: "" };
    node._ocioPrev.pb = { playing: false, dir: 1, mode: "loop", fps: 24, showTransport: false, lastT: 0 };
    _ensureTransport(node, node._ocioPrev);           // transport bar widget (sits under the canvas, video only)
    node.onRemoved = (orig => function () { _stopViewport(node._ocioPrev); return orig && orig.apply(this, arguments); })(node.onRemoved);
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
    if (p.canvas.width !== v.videoWidth || p.canvas.height !== v.videoHeight) { p.canvas.width = v.videoWidth; p.canvas.height = v.videoHeight; }
    gl.viewport(0, 0, p.canvas.width, p.canvas.height);
    gl.useProgram(g.prog);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, g.vidTex);
    try { gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, v); } catch (e) { return; }
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_3D, g.lutTex);
    gl.uniform1f(g.locs.uN, p.lutN || 33); gl.uniform1f(g.locs.uOn, p.lutReady ? 1 : 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
}
function _startViewport(node, p, src) {
    const streamUrl = "/ocio/stream?src=" + encodeURIComponent(src);
    if (p.streamUrl !== streamUrl) {
        p.streamUrl = streamUrl; p.video.src = streamUrl; p.video.play().catch(() => {});
        p.video.onerror = () => { _stopViewport(p); p.img.src = "/ocio/thumb?" + _thumbQuery(node, src); p.img.style.display = ""; };
    }
    if (!_vpInitGL(p)) {                               // no WebGL2 -> static color-managed thumb fallback
        p.canvas.style.display = "none"; p.video.style.display = "none";
        p.img.src = "/ocio/thumb?" + _thumbQuery(node, src); p.img.style.display = ""; return;
    }
    p.img.style.display = "none"; p.video.style.display = "none"; p.canvas.style.display = "";
    p.pb.fps = parseFloat(W(node, "fps")?.value) || p.pb.fps || 24;
    p.pb.showTransport = true; if (p.transport) p.transport.bar.style.display = "flex";
    node.setSize([node.size[0], node.computeSize()[1]]);
    _refreshVideoLut(node, p);
    if (!p.raf) {
        const loop = (now) => { if (node._ocioPrev !== p) { p.raf = 0; return; } _tickPlayback(p, now || 0); _drawViewport(p); _syncTransport(p); p.raf = requestAnimationFrame(loop); };
        p.raf = requestAnimationFrame(loop);
    }
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
        raw: W(node, "raw_data")?.value ? "1" : "0", rand: String(Date.now()) }).toString();
}
// ---- Nuke-style transport bar for the video viewport (client-side, drives the hidden <video>; the WebGL
// loop renders whatever frame it lands on). A numbered timeline ruler with a draggable playhead and in / out
// range handles, Repeat / Bounce, go to first / last, play reverse / forward, step one frame, step by N, a
// frame field, and a range-reset. The in / out handles ARE the node's start_frame / end_frame widgets (single
// source of truth): dragging a handle edits the field, editing the field moves the handle, and playback loops
// or bounces inside [in, out]. Shown only for a video source.
const _SVG = {
    first:   '<path d="M4 3v10M13 3l-7 5 7 5z"/>',                 // |<  to first / in
    last:    '<path d="M12 3v10M3 3l7 5-7 5z"/>',                  // >|  to last / out
    playRev: '<path d="M13 3l-6 5 6 5zM7 3l-5 5 5 5z"/>',          // <<  play reverse
    playFwd: '<path d="M3 3l6 5-6 5zM9 3l5 5-5 5z"/>',             // >>  play forward
    stepB:   '<path d="M11 3l-7 5 7 5z"/>',                        // <   step back one
    stepF:   '<path d="M5 3l7 5-7 5z"/>',                          // >   step forward one
    pause:   '<path d="M4 3h3v10H4zM9 3h3v10H9z"/>',
    loop:    '<path fill="none" stroke="currentColor" stroke-width="1.5" d="M4.5 9a3.5 3.5 0 1 1 1 2.5"/><path d="M3 7.5l1.6 2.6 2.2-1.9z"/>',
    bounce:  '<path d="M3 8l3-3v2h4V5l3 3-3 3v-2H6v2z"/>',
    reset:   '<path fill="none" stroke="currentColor" stroke-width="1.4" d="M12 6.5A4 4 0 1 0 12.6 10"/><path d="M12.6 2.5v3.2h-3.2z"/>',
};
function _tBtn(icon, title) {
    const b = document.createElement("button"); b.title = title; b.dataset.icon = "1";
    b.style.cssText = "width:19px;height:17px;padding:0;margin:0;border:0;border-radius:2px;background:#2a2a2a;color:#dbe6f0;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;";
    b.innerHTML = `<svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor">${icon}</svg>`;
    b.onmouseenter = () => b.style.background = "#39395a"; b.onmouseleave = () => b.style.background = "#2a2a2a";
    return b;
}
function _setIcon(b, icon) { b.innerHTML = `<svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor">${icon}</svg>`; }
function _pbFrames(p) { const d = p.video && p.video.duration; if (!(d > 0) || !isFinite(d)) return 1; return Math.max(1, Math.min(1e6, Math.round(d * (p.pb.fps || 24)) || 1)); }
function _pbLast(p) { return _pbFrames(p) - 1; }
function _pbCur(p) { return Math.round(p.video.currentTime * (p.pb.fps || 24)); }
function _pbSeek(p, f) { f = Math.max(0, Math.min(_pbLast(p), Math.round(f))); p.video.currentTime = (f + 0.001) / (p.pb.fps || 24); }
// in / out range = the node's start_frame / end_frame widgets (single source of truth, bidirectional).
function _pbIn(p) { const w = W(p.node, "start_frame"); return Math.max(0, Math.min(_pbLast(p), Math.round(w?.value ?? 0))); }
function _pbOut(p) { const w = W(p.node, "end_frame"); const last = _pbLast(p); return Math.max(_pbIn(p), Math.min(last, Math.round(w?.value ?? last))); }
function _pbSetField(p, name, f) { const w = W(p.node, name); if (!w) return; w.value = f; try { w.callback && w.callback(f); } catch (e) {} p.node.setDirtyCanvas(true, true); }
function _pbSetIn(p, f) { _pbSetField(p, "start_frame", Math.max(0, Math.min(_pbOut(p), Math.round(f)))); }
function _pbSetOut(p, f) { _pbSetField(p, "end_frame", Math.max(_pbIn(p), Math.min(_pbLast(p), Math.round(f)))); }
function _pbResetRange(p) { _pbSetField(p, "start_frame", 0); _pbSetField(p, "end_frame", _pbLast(p)); }
function _pbSet(node, p, on, dir) {
    p.pb.playing = on; p.pb.dir = dir || 1; p.pb.lastT = 0;
    const inF = _pbIn(p), outF = _pbOut(p), cur = _pbCur(p);
    if (on && p.pb.dir > 0) { if (cur >= outF) _pbSeek(p, inF); p.video.loop = false; p.video.playbackRate = 1; p.video.play().catch(() => {}); }
    else if (on) { if (cur <= inF) _pbSeek(p, outF); p.video.pause(); }   // reverse is driven manually in _tickPlayback
    else { p.video.pause(); }
    _syncTransport(p);
}
function _pbStop(node, p) { _pbSet(node, p, false, 1); _pbSeek(p, _pbIn(p)); }
function _pbStep(node, p, d) { _pbSet(node, p, false, 1); _pbSeek(p, _pbCur(p) + d); }
function _pbJump(node, p, end) { _pbSet(node, p, false, 1); _pbSeek(p, end ? _pbOut(p) : _pbIn(p)); }
function _pbMode(p, mode) { p.pb.mode = mode; _syncTransport(p); }
function _tickPlayback(p, now) {
    const pb = p.pb, v = p.video; if (!pb) return;
    const dt = Math.min(0.1, (now - (pb.lastT || now)) / 1000); pb.lastT = now;
    if (!pb.playing || !(v.duration > 0)) return;
    const fps = pb.fps || 24, inT = _pbIn(p) / fps, outT = Math.min(v.duration - 0.001, (_pbOut(p) + 0.999) / fps);
    if (pb.dir > 0) {
        if (v.currentTime >= outT || v.ended) {
            if (pb.mode === "bounce") { pb.dir = -1; if (!v.paused) v.pause(); v.currentTime = outT; }
            else { v.currentTime = inT; if (v.paused) v.play().catch(() => {}); }                        // loop within [in,out]
        }
    } else {
        let t = v.currentTime - dt * (v.playbackRate || 1);
        if (t <= inT) {
            if (pb.mode === "bounce") { pb.dir = 1; v.currentTime = inT; v.loop = false; v.play().catch(() => {}); }
            else { v.currentTime = outT; }                                                               // reverse-loop to out
        } else { v.currentTime = t; }
    }
}
function _niceStep(n, maxLabels) {
    const raw = Math.max(1, n / Math.max(1, maxLabels)), pow = Math.pow(10, Math.floor(Math.log10(raw)));
    for (const m of [1, 2, 5, 10]) if (pow * m >= raw) return pow * m;
    return pow * 10;
}
function _drawTimeline(p) {
    const t = p.transport; if (!t || !t.tl) return; const cv = t.tl;
    const Wd = cv.clientWidth || 250; if (cv.width !== Wd) cv.width = Wd; const H = cv.height;
    const g = cv.getContext("2d"); if (!g) return; const PAD = 8;
    const last = _pbLast(p), cur = _pbCur(p), inF = _pbIn(p), outF = _pbOut(p);
    const X = f => PAD + (last > 0 ? f / last : 0) * (Wd - 2 * PAD);
    g.clearRect(0, 0, Wd, H); g.fillStyle = "#141414"; g.fillRect(0, 0, Wd, H);
    g.fillStyle = "#123039"; g.fillRect(X(inF), H - 5, X(outF) - X(inF), 3);                             // active-range band
    g.fillStyle = "rgba(0,0,0,0.5)"; g.fillRect(0, 0, X(inF), H); g.fillRect(X(outF), 0, Wd - X(outF), H);
    const maxLabels = Math.max(2, Math.floor((Wd - 2 * PAD) / 34)), step = Math.max(1, _niceStep(last + 1, maxLabels));
    g.fillStyle = "#7a8a99"; g.strokeStyle = "#3a3a3a"; g.font = "8px monospace"; g.textAlign = "center";
    for (let f = 0; f <= last; f += step) {
        const x = X(f); g.beginPath(); g.moveTo(x, H - 6); g.lineTo(x, H - 9); g.stroke();
        g.fillText(String(f), Math.max(7, Math.min(Wd - 7, x)), H - 11);
    }
    g.strokeStyle = "#333"; g.beginPath(); g.moveTo(PAD, H - 6); g.lineTo(Wd - PAD, H - 6); g.stroke();
    const handle = (x, d) => { g.fillStyle = "#4cc3ff"; g.fillRect(x - 0.5, 2, 1, H - 6); g.beginPath(); g.moveTo(x, 2); g.lineTo(x + d * 5, 2); g.lineTo(x, 7); g.closePath(); g.fill(); };
    handle(X(inF), 1); handle(X(outF), -1);
    const xc = X(cur); g.fillStyle = "#ff8c1a"; g.fillRect(xc - 0.75, 0, 1.5, H - 5);                    // playhead
    g.beginPath(); g.moveTo(xc - 4, 0); g.lineTo(xc + 4, 0); g.lineTo(xc, 5); g.closePath(); g.fill();
}
function _syncTransport(p) {
    const t = p.transport; if (!t) return;
    const cur = _pbCur(p), n = _pbFrames(p), pb = p.pb;
    if (document.activeElement !== t.frame) t.frame.value = String(cur);
    t.total.textContent = "/ " + (n - 1);
    _setIcon(t.playFwd, (pb.playing && pb.dir > 0) ? _SVG.pause : _SVG.playFwd);
    _setIcon(t.playRev, (pb.playing && pb.dir < 0) ? _SVG.pause : _SVG.playRev);
    t.mode.title = pb.mode === "loop" ? "Repeat (loop)" : "Bounce (ping-pong)";
    _setIcon(t.mode, pb.mode === "loop" ? _SVG.loop : _SVG.bounce);
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
    const mode = _tBtn(_SVG.loop, "Repeat (loop)");   mode.onclick = () => _pbMode(p, p.pb.mode === "loop" ? "bounce" : "loop");
    const first = _tBtn(_SVG.first, "Go to in point"); first.onclick = () => _pbJump(node, p, false);
    const playRev = _tBtn(_SVG.playRev, "Play reverse"); playRev.onclick = () => _pbSet(node, p, !(p.pb.playing && p.pb.dir < 0), -1);
    const sb = _tBtn(_SVG.stepB, "Back one frame");   sb.onclick = () => _pbStep(node, p, -1);
    const frame = document.createElement("input"); frame.type = "number"; frame.value = "0";
    frame.style.cssText = "width:40px;height:15px;text-align:center;background:#101010;color:#cfe;border:1px solid #333;border-radius:2px;font:10px monospace;margin:0 2px;";
    frame.addEventListener("change", () => { const f = parseInt(frame.value, 10) || 0; _pbSet(node, p, false, 1); _pbSeek(p, f); });
    const total = document.createElement("span"); total.style.cssText = "color:#789;font:9px monospace;margin:0 3px 0 1px;min-width:26px;"; total.textContent = "/ 0";
    const sf = _tBtn(_SVG.stepF, "Forward one frame"); sf.onclick = () => _pbStep(node, p, 1);
    const playFwd = _tBtn(_SVG.playFwd, "Play forward"); playFwd.onclick = () => _pbSet(node, p, !(p.pb.playing && p.pb.dir > 0), 1);
    const last = _tBtn(_SVG.last, "Go to out point");  last.onclick = () => _pbJump(node, p, true);
    const reset = _tBtn(_SVG.reset, "Reset range (in / out to full clip)"); reset.onclick = () => { _pbResetRange(p); _drawTimeline(p); };
    const nstep = document.createElement("input"); nstep.type = "number"; nstep.value = "10"; nstep.min = "1";
    nstep.style.cssText = "width:28px;height:15px;text-align:center;background:#101010;color:#9ab;border:1px solid #333;border-radius:2px;font:9px monospace;margin:0 1px;";
    const nsB = _tBtn(_SVG.playRev, "Back N frames");  nsB.onclick = () => _pbStep(node, p, -(parseInt(nstep.value, 10) || 10));
    const nsF = _tBtn(_SVG.playFwd, "Forward N frames"); nsF.onclick = () => _pbStep(node, p, (parseInt(nstep.value, 10) || 10));
    const sep = () => { const s = document.createElement("span"); s.style.cssText = "width:6px;display:inline-block;"; return s; };
    row.append(mode, sep(), first, playRev, sb, frame, total, sf, playFwd, last, sep(), reset, sep(), nsB, nstep, nsF);
    bar.append(tl, row);
    const w = node.addDOMWidget("transport", "div", bar, { serialize: false });
    w.computeSize = () => [0, p.pb && p.pb.showTransport ? 54 : 0];
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
    p.transport = { bar, tl, frame, total, playFwd, playRev, mode };
    return p.transport;
}
function updateReadPreview(node) {
    const p = ensureReadPreview(node);
    const src = (W(node, "source")?.value || "").trim();
    if (!src) { p.img.style.display = "none"; p.img.removeAttribute("src"); _stopViewport(p); return; }
    if (/\.(mov|mp4|mkv|avi|webm|mxf|m4v)$/i.test(src)) {
        _startViewport(node, p, src);
    } else {
        _stopViewport(p);
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
    w.computeSize = () => [0, 16 * META_ROWS.length + 8];
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
    if (!source) { applyReadVis(node); return; }   // empty source: hide the frame controls (still-image default)
    try {
        const r = await fetch("/ocio/seq_range", {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source }),
        });
        const d = await r.json();
        node._ocioSeq = d;
        if (d && (d.kind === "sequence" || d.kind === "video")) {
            setWSilent(node, "start_frame", d.start | 0);
            setWSilent(node, "end_frame", d.end | 0);
            setWSilent(node, "frame_shift", d.start | 0);      // default: first frame keeps its source number
            if (d.fps) setWSilent(node, "fps", Math.round(d.fps * 1000) / 1000);
        } else {
            setWSilent(node, "start_frame", 0);
            setWSilent(node, "end_frame", 0);
        }
        applyReadVis(node, d.kind);
        resyncAllWrites();                                     // push the detected range/fps to downstream Writes
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
async function listDir(path, wantFiles) {
    const r = await fetch("/ocio/list_dirs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path || "", files: !!wantFiles }),
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
    box.append(title, cur, list, ...(newRow ? [newRow] : []), buttons);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    const close = () => overlay.remove();
    let state = { path: "", parent: "", output_root: "", dirs: [], files: [] };
    const pick = (p) => {
        setW(node, opts.widget, opts.forOutput ? relToOutput(p, state.output_root) : p);
        if (opts.pickFiles) setW(node, "input_colorspace", autoInCs(p));
        close();
    };
    async function render(path) {
        state = await listDir(path, opts.pickFiles);
        cur.value = state.path;
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
        for (const f of (state.files || [])) {
            const row = document.createElement("div");
            row.textContent = "🎞 " + f;
            row.style.cssText = "padding:6px 10px;cursor:pointer;border-bottom:1px solid #2a2a2a;color:#cdd;";
            row.onmouseenter = () => (row.style.background = "#2c3b2c");
            row.onmouseleave = () => (row.style.background = "");
            row.onclick = () => pick(here + "/" + f);            // pick this file (frame_mode grabs the sequence)
            list.appendChild(row);
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
    render("");
}
function openFolderDialog(node) {   // Write output folder
    return openBrowser(node, { widget: "output_folder", forOutput: true });
}

// (The pack version is shown in every node's display name, set server-side in __init__.py: a canvas-corner
// badge is invisible on Vue-nodes frontends, which do not draw onDrawForeground, so the title carries it.)

app.registerExtension({
    name: "ComfyUI-OCIO.io",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name === "OCIORead") {
            const onCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const r = onCreated ? onCreated.apply(this, arguments) : undefined;
                // browse: pick ANY path on disk straight into `source` (no copy - OCIORead reads it in place).
                // upload: COPY a browser-picked file INTO ComfyUI's input folder, then point `source` at the
                // copy (see uploadRead) - the two are not duplicates: browse needs no copy, upload is for a
                // file that is not already inside the input folder.
                const browseBtn = this.addWidget("button", "Browse disk...", null,
                    () => openBrowser(this, { widget: "source", pickFiles: true }), { serialize: false });
                const uploadBtn = this.addWidget("button", "Copy to input folder", null,
                    () => uploadRead(this), { serialize: false });
                browseBtn._ocioAlwaysVisible = true;
                uploadBtn._ocioAlwaysVisible = true;
                ensureReadPreview(this);                                          // instant preview at the bottom
                ensureReadMeta(this);                                             // metadata panel, under the preview
                this._ocioAllWidgets = this.widgets.slice();                      // full ordered list, captured once
                onChange(this, "source", (v) => { setW(this, "input_colorspace", autoInCs(v)); fillRange(this, v); updateReadPreview(this); updateReadMeta(this); });
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
                for (const w of ["first_frame", "last_frame", "start_number"]) {
                    onChange(this, w, () => { const ar = W(node, "auto_range"); if (ar) ar.value = false; });  // manual edit -> auto OFF
                }
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
                this.addWidget("button", "📁 choose output folder", null, () => openFolderDialog(this), { serialize: false });
                this.addWidget("button", "▶ Render", null, () => app.queuePrompt(0, 1), { serialize: false });
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
    },
});
