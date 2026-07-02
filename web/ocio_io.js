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

// hide / show a widget (type swap + zero height; survives older and newer ComfyUI)
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

async function fillRange(node, source) {
    if (!source) return;
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

// show only the frame controls that apply to the source kind (fps hidden for a still, etc.)
function applyReadVis(node, kind) {
    const still = !kind || kind === "still";
    const seq = kind === "sequence";
    for (const w of ["start_frame", "end_frame", "frame_shift", "fps"]) showWidget(node, W(node, w), !still);
    showWidget(node, W(node, "missing_frames"), seq);   // gaps only exist in a frame sequence
    showWidget(node, W(node, "edge_mode"), seq);
    node.setSize([node.size[0], node.computeSize()[1]]);
    node.setDirtyCanvas(true, true);
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
        if (nd.type === "OCIOWrite") { syncWriteFromUpstream(nd); applyAutoColorspace(nd); }
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

// ---- version badge: every OCIO-category node gets a small "vX.Y.Z" in its bottom-right corner ------------
let OCIO_VER = "";
fetch("/ocio/version").then((r) => r.json()).then((d) => { OCIO_VER = "v" + d.version; }).catch(() => {});

app.registerExtension({
    name: "ComfyUI-OCIO.io",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.category === "OCIO") {
            const prevDraw = nodeType.prototype.onDrawForeground;
            nodeType.prototype.onDrawForeground = function (ctx) {
                prevDraw && prevDraw.apply(this, arguments);
                if ((this.flags && this.flags.collapsed) || !OCIO_VER) return;
                ctx.save();
                ctx.font = "9px sans-serif"; ctx.fillStyle = "rgba(255,255,255,0.35)"; ctx.textAlign = "right";
                ctx.fillText(OCIO_VER, this.size[0] - 6, this.size[1] - 6);
                ctx.restore();
            };
        }

        if (nodeData.name === "OCIORead") {
            const onCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const r = onCreated ? onCreated.apply(this, arguments) : undefined;
                this.addWidget("button", "📁 browse source (disk)", null,
                    () => openBrowser(this, { widget: "source", pickFiles: true }), { serialize: false });
                this.addWidget("button", "⬆ upload into input", null, () => uploadRead(this), { serialize: false });
                onChange(this, "source", (v) => { setW(this, "input_colorspace", autoInCs(v)); fillRange(this, v); });
                for (const w of ["frame_shift", "fps", "start_frame", "end_frame"]) {
                    onChange(this, w, () => resyncAllWrites());   // Read range/shift/fps -> downstream Writes
                }
                const node = this;
                setTimeout(() => fillRange(node, W(node, "source")?.value), 0);  // fresh node: detect the default
                return r;
            };
            const onConfig = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function () {
                const r = onConfig ? onConfig.apply(this, arguments) : undefined;
                const node = this;
                setTimeout(() => fillRange(node, W(node, "source")?.value), 0);  // loaded workflow: re-detect
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
                const applyContainer = () => {
                    const c = W(node, "container")?.value, isVideo = c === "video", isStill = c === "still image";
                    showWidget(node, W(node, "still_format"), !isVideo);
                    showWidget(node, W(node, "video_codec"), isVideo);
                    showWidget(node, W(node, "bit_depth"), !isVideo);
                    showWidget(node, W(node, "last_frame"), !isStill);        // still image writes one chosen frame
                    showWidget(node, W(node, "start_number"), c === "sequence");
                    showWidget(node, W(node, "source_start"), false);         // internal (set by the wire)
                    const ff = W(node, "first_frame");                         // relabel the shared field
                    if (ff) ff.label = isStill ? "which frame" : "first_frame";
                    applyFormat();
                    setW(node, "output_colorspace", autoOutCs(c, W(node, "still_format")?.value));
                    node.setSize([node.size[0], node.computeSize()[1]]);
                    node.setDirtyCanvas(true, true);
                };
                onChange(this, "container", applyContainer);
                onChange(this, "still_format", () => {
                    applyFormat();
                    setW(node, "output_colorspace", autoOutCs(W(node, "container")?.value, W(node, "still_format")?.value));
                });
                onChange(this, "video_codec", () => node.setDirtyCanvas(true, true));  // redraw the codec/bit-depth footer
                // auto frame range / fps from the upstream OCIO Read
                onChange(this, "auto_range", (v) => { if (v) syncWriteFromUpstream(node); });
                onChange(this, "auto_colorspace", (v) => { if (v) applyAutoColorspace(node); });
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
                this.addWidget("button", "📁 browse output folder", null, () => openFolderDialog(this), { serialize: false });
                this.addWidget("button", "▶ Render", null, () => app.queuePrompt(0, 1), { serialize: false });
                setTimeout(() => { applyContainer(); syncWriteFromUpstream(node); applyAutoColorspace(node); resolveAutoProfile(node); }, 0);
                return r;
            };
            const onConn = nodeType.prototype.onConnectionsChange;
            nodeType.prototype.onConnectionsChange = function () {
                const r = onConn ? onConn.apply(this, arguments) : undefined;
                const node = this;
                setTimeout(() => { syncWriteFromUpstream(node); applyAutoColorspace(node); resolveAutoProfile(node); }, 0);   // wire (re)connected -> pull range/fps + auto colorspace/profile
                return r;
            };
            const onConfigW = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function () {
                const r = onConfigW ? onConfigW.apply(this, arguments) : undefined;
                const node = this;
                setTimeout(() => { syncWriteFromUpstream(node); applyAutoColorspace(node); resolveAutoProfile(node); }, 0);   // loaded workflow -> re-detect
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
                if (c) { this._ocioWrote = Array.isArray(c) ? c[0] : c; this.setDirtyCanvas(true, true); }
            };
        }
    },
});
