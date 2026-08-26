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
    // Flag the callback as OUR write. A widget edited in the UI calls its callback directly, so anything
    // wrapped here is code-driven and must not be counted as a manual edit (issue #3: "Open Files" writes
    // source and then input_colorspace, and the second write cancelled the auto-fill the first one started).
    if (w.callback) {
        node._ocioAutoWrite = (node._ocioAutoWrite | 0) + 1;
        try { w.callback(value); } catch (e) {} finally { node._ocioAutoWrite--; }
    }
    node.setDirtyCanvas(true, true);
}
// set a widget value WITHOUT firing its callback (so an auto-sync isn't mistaken for a manual edit)
function setWSilent(node, name, value) {
    const w = W(node, name);
    if (!w) return;
    w.value = value;
    node.setDirtyCanvas(true, true);
}
// Overlay chrome for every persistent Refresh square in this file (OCIO Player's viewport, OCIO Write's
// flipbook). MODULE level on purpose: these were local to the Player's setup function, and the flipbook's
// button - defined hundreds of lines above it - read them at runtime and would have thrown ReferenceError.
// node --check passes either way, because a name is only resolved when the code runs.
const OV_BASE = "rgba(40,40,64,0.85)", OV_STALE = "rgba(150,95,20,0.92)";

function extOf(name) { return (String(name || "").toLowerCase().split(".").pop() || ""); }
function isExr(name) { const e = extOf(name); return e === "exr" || e === "hdr"; }
function shorten(cs) { return String(cs || "").replace(" - Display", "").replace(" - Texture", ""); }

// ---- "Processing…" busy overlay + CSS spinner -----------------------------------------
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
// A movie is a Rec.709 deliverable, not an sRGB one: same primaries, different transfer function. This used to
// return CS_SRGB for a video container, which tagged every ProRes / DNxHR with the computer-display curve
// (trc=iec61966-2-1) instead of Rec.709. MUST stay in step with _auto_output_cs in io_nodes.py - if the two
// disagree, the node shows one colourspace and the backend writes another. Fixed 2026-08-12.
const CS_REC709_DISPLAY = "Rec.1886 Rec.709 - Display";
function autoInCs(filename) { return isExr(filename) ? CS_ACESCG : CS_SRGB; }
// DPX gets ADX10, and that is not a preference. DPX is the film-scan container: it exists to carry printing
// density, and ADX10 is the ACES encoding of exactly that (SMPTE ST 2065-3). Falling through to sRGB - Display,
// which is what it did before, declares a scanned negative to be a finished picture on a monitor - the same
// class of error as reading log as linear, and it mis-stated every plate written this way.
//
// ADX16 exists for 16-bit DPX, but the depth default is chosen separately (BIT_DEF), so this stays on the
// 10-bit encoding, which is what a 10-bit scan and a Cineon-lineage file are. Change the depth by hand and the
// colorspace is yours to match; this only fills a sane starting point, and any manual edit wins over it.
const CS_ADX10 = "ADX10";
function autoOutCs(container, stillFormat) {
    if (container === "video") return CS_REC709_DISPLAY;
    if (stillFormat === "exr") return CS_ACESCG;
    if (stillFormat === "dpx") return CS_ADX10;
    return CS_SRGB;
}

// ---- the `view` widget, filled in automatically -----------------------------------------------------------
//
// NOBODY SHOULD HAVE TO KNOW THAT ACEScg -> Rec.709 NEEDS A VIEW PICKED BY HAND. The widget decides whether a
// scene/display crossing is rendered (the ACES Output Transform) or merely re-encoded (colorimetric), and the
// difference is not subtle: scene-linear 4.0 leaves as 1.78 and is clipped flat by the container, against
// 0.905 rolled off. But the fork only exists on pairs that actually cross, and knowing which pairs those are
// means knowing which of 55 colorspaces is scene-referred and which is display-referred.
//
// So the node works it out. /ocio/encodings serves OCIO's own `encoding` per colorspace and the config's
// default view per display; on a colorspace change this fills the widget with the display's default view when
// the pair crosses, and clears it back to the do-nothing value when it does not.
//
// TWO RULES IT MUST OBEY, both learned here the hard way:
//   1. A HUMAN EDIT ALWAYS WINS. Once the artist touches `view` it is theirs; this never overwrites it again.
//      Same contract as auto_range and the same one issue #3 was about.
//   2. IT MUST NOT FIRE ON LOAD. onNodeCreated runs its timer AFTER configure() restores saved values, so a
//      naive "fill it in" would rewrite a saved graph's view on open - silently re-rendering somebody's
//      finished work. Only a real change of colorspace triggers it.
// MUST match _VIEW_NONE in io_nodes.py exactly. It is the widget's default and the value that means "leave
// this alone", so a typo here would make the auto-fill try to set a choice that does not exist, and the
// includes() guard below would silently do nothing forever.
const VIEW_NONE = "(none) colorimetric, no tone map";

// ---- which colorspaces make sense for which output ------------------------------------------------------
//
// WHY NARROW AT ALL. Writing ACEScg into a 10-bit ProRes is legal here and useless in practice. Measured on a
// nine-stop ramp through the real writer: 43.55% of samples clipped, the brightest surviving scene-linear
// value 1.000 out of 92.2 - six and a half of nine stops gone - and in the shadows the worst step between
// adjacent codes is 33.33% of luminance, against 2.63% for a display space, where about 1% is the threshold
// of visibility. The same ramp through ARRI LogC4 clips nothing and keeps all nine stops.
//
// Which is why the rule is NOT "video means Rec.709": camera log in a ProRes is how masters are delivered,
// and forbidding it would break the most ordinary professional job there is. The line falls between log and
// scene-linear, not between display and everything else.
//
// ffv1 is deliberately exempt. It is 16-bit RGB and bit-exact - decoded output equals input, max abs diff 0 -
// and at 16 bits even scene-linear steps by 0.54%, under the visibility threshold. Lumping it in with ProRes
// would forbid the one video codec where the objection does not apply.
//
// `null` means no narrowing at all.
const ENC_ALLOW = {
    prores:   ["sdr-video", "hdr-video", "edr-video", "log"],   // >=10-bit integer video: display + camera log
    dnxhr8:   ["sdr-video"],                                    // 8-bit: log steps 23-46% in the shadows
    delivery: ["sdr-video", "hdr-video"],                       // h264 / hevc, feeding consumer decoders
    hevc12:   ["sdr-video", "hdr-video", "log"],                // 12-bit 4:4:4: log steps fall to 0.8-2.9%
    dpx:      ["log", "sdr-video"],                             // the Cineon lineage, ADX included
    still16:  ["sdr-video", "log"],
    still8:   ["sdr-video"],
    anything: null,                                             // float and lossless destroy nothing
};
const CODEC_ALLOW = {
    prores_4444: "prores", prores_422hq: "prores", prores_422: "prores", prores_4444xq: "prores",
    prores_4444_mxf: "prores", prores_4444xq_mxf: "prores",
    dnxhr_hqx: "prores", dnxhr_444: "prores", dnxhr_hqx_mxf: "prores", dnxhr_444_mxf: "prores",
    dnxhr_hq: "dnxhr8", dnxhr_hq_mxf: "dnxhr8", dnxhr_hq_mxf_opatom: "dnxhr8",
    h264: "delivery", hevc: "delivery", hevc_444_12: "hevc12", ffv1: "anything",
};
const STILL_ALLOW = { exr: "anything", tiff: "anything", dpx: "dpx", png: "still16", jpeg: "still8" };
let _ocioEnc = null;    // { encodings: {name: encoding}, views: {display: {...}}, alt: {label, views, colorspaces} }
async function ocioEncodings() {
    if (_ocioEnc) return _ocioEnc;
    const empty = { label: "", views: {}, colorspaces: [] };
    try {
        const r = await fetch("/ocio/encodings");
        const j = await r.json();
        _ocioEnc = { encodings: j.encodings || {}, views: j.views || {}, alt: j.alt || empty };
    } catch (e) {
        _ocioEnc = { encodings: {}, views: {}, alt: empty };   // no data -> no auto-fill, the old behaviour
    }
    return _ocioEnc;
}

// (display, scene) when this pair crosses the scene/display boundary, else null. Mirrors _crosses_scene_display
// in io_nodes.py - the backend decides the same question the same way, and the two must not drift.
function crossingOf(enc, fromCs, outCs) {
    if (!fromCs || !outCs || fromCs === outCs) return null;
    const a = enc.encodings[fromCs], b = enc.encodings[outCs];
    if (!a || !b) return null;
    const aDisp = a.endsWith("-video"), bDisp = b.endsWith("-video");
    if (aDisp === bDisp) return null;
    return aDisp ? { display: fromCs, scene: outCs } : { display: outCs, scene: fromCs };
}

// Narrow `output_colorspace` to what the chosen output can actually carry.
//
// THE ONE THING IT MUST NEVER DO IS COERCE. The bit_depth narrowing a few lines up resets an out-of-range
// value to the format default, which is right there: depth is cosmetic and the file is written either way.
// A colorspace is not. Silently swapping a restored one would change the rendered image of a saved workflow
// without anybody asking, which is the exact failure this pack refuses everywhere else. So a value that is
// no longer on the list is KEPT, still selected, and named in the label - the artist decides.
//
// Frontend only, and that is load-bearing. INPUT_TYPES is static and cannot see the current container, so a
// backend list is impossible anyway; more importantly a backend filter would make ComfyUI reject the whole
// prompt with value_not_in_list, killing saved workflows outright rather than warning about them.
// OCIO Write has NO preview of its own, and that is a decision rather than an omission (2026-08-16).
// It briefly grew one: a flipbook of the written frames, a movie player, a transport and a collapsible
// Viewer. All of it is gone. The pack already has a viewer and it is a better one - OCIO Player is a
// float viewport with in / out points, reverse, an exposure strip, audio metering and a GPU frame
// cache - and a second, smaller player on every Write node turned a graph of four writes into a column
// of four video players. The node reports in text and writes files; wire OCIO Player to look.

async function applyCsNarrowing(node) {
    const w = W(node, "output_colorspace");
    if (!w || !w.options) return;
    const enc = await ocioEncodings();
    if (!Object.keys(enc.encodings).length) return;          // no data -> offer everything, as before
    if (!w._ocioAllCs) w._ocioAllCs = (w.options.values || []).slice();   // the full list, kept once
    const container = W(node, "container")?.value;
    const key = container === "video" ? CODEC_ALLOW[W(node, "video_codec")?.value]
                                      : STILL_ALLOW[W(node, "still_format")?.value];
    const allow = key ? ENC_ALLOW[key] : null;
    if (!allow) {                                            // "anything", or an output we have no rule for
        w.options.values = w._ocioAllCs.slice();
        if (w.label) w.label = "output_colorspace";
        return;
    }
    const ok = w._ocioAllCs.filter(cs => allow.includes(enc.encodings[cs] || ""));
    if (!ok.length) { w.options.values = w._ocioAllCs.slice(); return; }  // never leave an empty picker
    if (!ok.includes(w.value)) ok.unshift(w.value);          // keep the current pick reachable, never replace it
    w.options.values = ok;
    // A kept-but-inadvisable value is said out loud rather than fixed behind the artist's back.
    const bad = !allow.includes(enc.encodings[w.value] || "");
    w.label = bad ? "output_colorspace (not advised for this output)" : "output_colorspace";
    node.setDirtyCanvas(true, true);
}

// Which view this pair should fill itself in with. ACES 1.3 WHEN THAT CONFIG HAS ONE FOR THIS DISPLAY, and
// the loaded config's own default otherwise.
//
// The version is a statement about who opens the file, not about which standard is newer. A Nuke 13 or 14
// comp is on ACES 1.2 / 1.3, and in an ACES project Nuke's Read applies the inverse Output Transform on the
// way in while the viewer applies the forward one - so a render has to have been made by the version that
// comp will view it through. Get that wrong and the worst pixel of a real Rec.709 master is off by 35.5%,
// presenting as "close, but the blacks and highlights are wrong" with nothing in the file to say why.
//
// Delivering into a 2.0 pipeline is one click away in the same list. This picks the common case.
// Pure, so tests/test_view_narrowing.py can run it under node against the real config data.
function preferredViewFor(enc, cross) {
    if (!cross) return VIEW_NONE;
    const alt = enc.alt || {};
    const altDefault = (alt.defaults || {})[cross.display];
    const altCs = alt.colorspaces || [];
    // the 1.3 config must also KNOW the scene colorspace, or the transform cannot be built at all
    if (altDefault && altCs.indexOf(cross.scene) !== -1) return altDefault;
    const d = enc.views ? enc.views[cross.display] : null;
    return (d && d.default) ? d.default : null;                   // null = we have nothing for this display
}

async function applyAutoView(node) {
    const vw = W(node, "view");
    if (!vw) return;
    if ((node._ocioEdited || new Set()).has("view")) return;      // rule 1: the artist owns it now
    const enc = await ocioEncodings();
    const cross = crossingOf(enc, W(node, "input_colorspace")?.value, W(node, "output_colorspace")?.value);
    let want = VIEW_NONE;
    if (cross) {
        want = preferredViewFor(enc, cross);
        if (!want) return;                                        // a display we have no views for: leave it
    }
    if (vw.value !== want && (vw.options?.values || []).includes(want)) {
        setWSilent(node, "view", want);
        node.setDirtyCanvas(true, true);
    }
}

// ---- which VIEWS this pair can actually be rendered through -----------------------------------------------
//
// A VIEW BELONGS TO A DISPLAY. The combo cannot express that: it is one flat list, fixed when INPUT_TYPES is
// evaluated, and it is the union across every display of TWO configs. Measured on the configs this pack loads
// by default, 24 of its 32 real entries are invalid for `Rec.1886 Rec.709 - Display` - and picking one of them
// is not a soft miss, it raises at render time, after the graph has already spent its time.
//
// The trap got ordinary rather than exotic when the ACES 1.3 entries arrived: `ACES 1.1 - SDR Video (Rec.709
// lim)` reads like the obvious choice for a Rec.709 deliverable and lives on `Rec.1886 Rec.2020 - Display`, a
// display the ACES 2.0 config does not even have.
//
// TWO reasons an entry can be wrong for a pair, and both are checked here because they fail differently:
//   1. the view is not on the resolved display -> this pack's own ValueError, which names the valid ones;
//   2. the 1.3 config does not HAVE the scene colorspace (`D-Log D-Gamut`, `Linear D-Gamut` are in the ACES
//      2.0 config only) -> a raw OCIO "Cannot find source color space" from inside the processor build. The
//      view is perfectly valid for the display in that case, so rule 1 would let it through.
//
// Pure and dependency-free on purpose: tests/test_view_narrowing.py lifts THIS function out of the file and
// runs it under node against the real config data, so the narrowing is tested by execution rather than by a
// grep for its name. Returns null when there is nothing to narrow by, which means "leave the list alone".
function viewsForCrossing(enc, cross) {
    if (!cross) return null;                                   // no crossing -> `view` does nothing at all
    const d = enc.views ? enc.views[cross.display] : null;
    if (!d || !Array.isArray(d.all)) return null;              // a display we have no views for: leave it
    const out = [VIEW_NONE].concat(d.all);
    const alt = enc.alt || {};
    const altViews = (alt.views || {})[cross.display];
    const altCs = alt.colorspaces || [];
    if (altViews && altCs.indexOf(cross.scene) !== -1) out.push.apply(out, altViews);
    return out;
}

// Narrow `view` to what this from/output pair can be rendered through. Same contract as applyCsNarrowing and
// for the same reason: IT MUST NEVER COERCE. A value that is no longer on the list is KEPT, still selected,
// and said out loud in the label - a saved workflow's rendering transform is not ours to swap.
// `write_audio` is shown for exactly one situation: sound arriving WITHOUT A WIRE.
//
// Wiring already answers the question everywhere else. Nothing connected to `audio` writes no sound - not a
// muxed track, not a sidecar .wav - because there is none to write. Something connected to `audio` is a
// deliberate act with a wire to pull if it was not wanted. Neither needs a toggle, and a toggle that can only
// hold the answer it already has is a row of noise on every write node in the graph.
//
// The exception is a native ComfyUI VIDEO. It carries its own track INSIDE the object, so connecting a movie
// hands the writer sound that no wire represents, and this is the only way to ask for picture only. That is
// why the row appears then, and why it appears for a SEQUENCE too: a sequence cannot hold audio in its files,
// so the track lands beside them as a .wav, and declining it has to stay possible.
//
// An explicit `audio` wire alongside a `video` one wins in the writer, so the toggle steps back out of the
// way - the wire is the control at that point.
function applyAudioVis(node) {
    const w = W(node, "write_audio");
    if (!w) return;
    const wired = (name) => {
        const s = (node.inputs || []).find(i => i.name === name);
        return !!(s && s.link !== null && s.link !== undefined);
    };
    showWidget(node, w, wired("video") && !wired("audio"));
    node.setSize([node.size[0], node.computeSize()[1]]);
    node.setDirtyCanvas(true, true);
}

async function applyViewNarrowing(node) {
    const w = W(node, "view");
    if (!w || !w.options) return;
    const enc = await ocioEncodings();
    if (!Object.keys(enc.views || {}).length) return;          // no data -> offer everything, as before
    if (!w._ocioAllViews) w._ocioAllViews = (w.options.values || []).slice();   // the full list, kept once
    const allow = viewsForCrossing(enc, crossingOf(enc, W(node, "input_colorspace")?.value,
                                                       W(node, "output_colorspace")?.value));
    if (!allow) {
        w.options.values = w._ocioAllViews.slice();
        w.label = "view";
        node.setDirtyCanvas(true, true);
        return;
    }
    const ok = w._ocioAllViews.filter(v => allow.includes(v));
    if (!ok.length) { w.options.values = w._ocioAllViews.slice(); return; }     // never leave an empty picker
    if (!ok.includes(w.value)) ok.unshift(w.value);            // keep the current pick reachable, never replace it
    w.options.values = ok;
    w.label = allow.includes(w.value) ? "view" : "view (not valid for this pair - the render will refuse it)";
    node.setDirtyCanvas(true, true);
}

// bit-depth options + default per still format
// dpx is integer-only and takes 10 or 16: 16-bit is what Netflix's archival-master spec names first for log
// material, 10-bit matches a 10-bit camera original. No float entry, because DPX has no float variant here.
const BITS = { exr: ["16f", "32f"], tiff: ["8", "16", "32f"], png: ["8", "16"], jpeg: ["8"], dpx: ["10", "16"] };
const BIT_DEF = { exr: "16f", tiff: "16", png: "8", jpeg: "8", dpx: "16" };
const STILL_EXT = { exr: "exr", tiff: "tif", png: "png", jpeg: "jpg", dpx: "dpx" };

// video codec -> real bit depth + extension (mirrors io_nodes.py save_video's codec->pix_fmt map). bit_depth
// stays hidden for video (still-format 16f/32f/16/8 don't map to video 8/10/12) - this footer shows the real,
// codec-fixed depth instead.
// EVERY codec in the backend's video_codec combo needs an entry here, and `ext` is the ONLY place the front end
// decides an extension. It used to re-derive it from a name prefix instead, which is how dnxhr_hq_mxf came to
// preview .mov while the backend wrote .mxf: 'dnxhr_hq_mxf'.startsWith('dnxhr') is true. A prefix test is a
// second copy of a rule that already lives in this table, and the two drift the moment a codec is added.
// tests/test_codec_ext_parity.py reads both sides and fails if they ever disagree again.
// THE ProRes 4444 ROWS SAY 12-bit AND THAT IS CORRECT, though the reason is worth writing down because it is
// not the obvious one. ffmpeg's prores_ks advertises only 10-bit pixel formats, and this pack asks it for
// yuv444p10le - so it is tempting to conclude the files are 10-bit. They are not: ProRes 4444 stores in a
// 12-bit grid, and a decoder reads the written file back as yuv444p12le, which is what
// tests/test_codec_ext_parity.py measures on a real encode rather than restating from a help page. That test
// caught this table being "corrected" to 10-bit on 2026-08-15 and was right to.
//
// Related, since it comes up: there is no separate codec to install that would change any of this. Nuke on
// Windows carries avcodec-58.dll, the same ffmpeg, because Apple ended QuickTime for Windows in 2016.
const CODEC_INFO = {
    prores_4444: { bits: "12-bit", ext: ".mov" }, prores_422hq: { bits: "10-bit", ext: ".mov" },
    prores_422: { bits: "10-bit", ext: ".mov" }, dnxhr_hq: { bits: "8-bit", ext: ".mov" },
    h264: { bits: "8-bit", ext: ".mp4" }, hevc: { bits: "8-bit", ext: ".mp4" },
    dnxhr_hq_mxf: { bits: "8-bit", ext: ".mxf" }, dnxhr_hq_mxf_opatom: { bits: "8-bit", ext: ".mxf" },
    dnxhr_hqx: { bits: "10-bit", ext: ".mov" }, dnxhr_444: { bits: "10-bit", ext: ".mov" },
    // MXF above 8 bits. Until these existed the only route into an MXF was dnxhr_hq, which is 8-bit by
    // profile, so the one container the industry hands masters around in was the one place this pack could
    // not put a master. A real camera MXF measured for this read ProRes 4444, yuv444p12le, 12-bit.
    prores_4444_mxf: { bits: "12-bit", ext: ".mxf" }, prores_4444xq_mxf: { bits: "12-bit", ext: ".mxf" },
    dnxhr_hqx_mxf: { bits: "10-bit", ext: ".mxf" }, dnxhr_444_mxf: { bits: "10-bit", ext: ".mxf" },
    // The only genuinely 12-bit encode in this build. ProRes 4444 reads back as 12-bit because that is what
    // the FORMAT is, but every ffmpeg ProRes encoder tops out at 10-bit data; libx265 writes 12.
    hevc_444_12: { bits: "12-bit", ext: ".mp4" },
    prores_4444xq: { bits: "12-bit", ext: ".mov" },
    // The only encode here that returns this pack's own 16-bit input unchanged, measured by md5. Matroska is
    // the container the Library of Congress pairs it with, not MOV.
    ffv1: { bits: "16-bit", ext: ".mkv" },
};
// h264 and hevc say 8-bit because that is what they write for an SDR delivery, and the parity test measures
// exactly that. Choose a BT.2100 output colorspace and the backend moves them to 10-bit, because HLG and PQ
// are not defined below it; the 8-bit DNxHR profiles refuse that combination outright instead.
const CODEC_LABEL = {
    prores_4444: "ProRes 4444", prores_422hq: "ProRes 422 HQ", prores_422: "ProRes 422",
    dnxhr_hq: "DNxHR HQ", h264: "H.264", hevc: "HEVC",
    dnxhr_hq_mxf: "DNxHR HQ - MXF OP1a", dnxhr_hq_mxf_opatom: "DNxHR HQ - MXF OPAtom",
    dnxhr_hqx: "DNxHR HQX", dnxhr_444: "DNxHR 444",
    prores_4444_mxf: "ProRes 4444 - MXF OP1a", prores_4444xq_mxf: "ProRes 4444 XQ - MXF OP1a",
    dnxhr_hqx_mxf: "DNxHR HQX - MXF OP1a", dnxhr_444_mxf: "DNxHR 444 - MXF OP1a",
    hevc_444_12: "HEVC 4:4:4 12-bit (mastering)",
    prores_4444xq: "ProRes 4444 XQ", ffv1: "FFV1 lossless (archival)",
};

// hide / show ONE widget with a TRUE collapse (no blank row). 2026-07-04: switched off the old OCIO_HIDDEN
// type-swap - it left a blank row on Vue-nodes frontends (they drop a row only via options.hidden / v-if, not a
// type change) AND risked blanking the widget value on serialize. Now identical to setVisibleWidgets' per-widget
// logic: widget.hidden + options.hidden (dual-set - canvas reads .hidden, Vue reads options.hidden) + a zeroed
// computeSize, NO type swap (so the value keeps serializing). Used by OCIO Write's per-container visibility.
// THE RESTORE IS KEYED ON THE PROPERTY EXISTING, NOT ON ITS VALUE BEING TRUTHY, and that distinction was a real
// user-visible defect until 2026-08-13. MOST of these widgets have NO computeSize of their own - litegraph lays
// them out from the prototype - so hiding stashed `undefined` and the old restore, `if (w._ocioCompute)`, was
// FALSY and never ran. The zeroed function therefore stayed forever: hidden once, invisible for good, while
// `hidden` and `options.hidden` both read false and every flag said the row was showing.
//
// Measured in the live canvas. Switch container to video and back to sequence and `compression` comes back with
// hidden=false, options.hidden=false and computeSize()[1] === 0. The node's own height went 666 -> 490 across
// that one round trip: 176 pixels of controls the node believed it was drawing. An artist who looked at a
// movie and returned to an EXR sequence could not set compression again without reloading the graph.
//
// `delete w.computeSize` is the correct undo for the no-own-property case: it exposes the prototype's layout
// again, which is what "this widget had no computeSize" meant in the first place. Assigning undefined would
// leave an own property shadowing it.
function _ocioRestoreCompute(w) {
    if (!("_ocioCompute" in w)) return;
    if (w._ocioCompute === undefined) delete w.computeSize;
    else w.computeSize = w._ocioCompute;
    delete w._ocioCompute;
}

function showWidget(node, w, visible) {
    if (!w) return;
    if (!w.options) w.options = {};
    if (visible) {
        w.hidden = false; w.options.hidden = false;
        _ocioRestoreCompute(w);
    } else {
        w.hidden = true; w.options.hidden = true;
        if (!("_ocioCompute" in w)) w._ocioCompute = w.computeSize;
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
            _ocioRestoreCompute(w);                         // property-keyed, see the comment on showWidget
        } else {
            w.hidden = true;
            w.options.hidden = true;                        // dual-set so Vue drops the row (v-if), no blank gap
            if (!("_ocioCompute" in w)) w._ocioCompute = w.computeSize;
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

// The "_colorspace" the Write node injects before the frame number. MUST stay identical to io_nodes.py
// _cs_tag - tests/test_cs_tag_unique.py asserts the two agree on every colorspace the config offers, because
// a front-end that previews a different filename than the backend writes is worse than no preview at all.
//
// This used to be a table of short tags, and it collapsed 31 of 55 colorspaces onto a shared token (thirteen
// gamuts to "linear", eight transfers to "rec709", six to "p3"). Since the delivered PATH is built from this,
// two writes differing only in colorspace overwrote each other silently. Spelling the name out in full ends
// that. No truncation: a fixed-width cut would re-introduce collisions between names sharing a prefix.
function csCore(name) {
    return (name || "").toLowerCase()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/_+/g, "_")
        .replace(/^_+|_+$/g, "");
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
        return name + t + (CODEC_INFO[v]?.ext || ".mp4");
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
// format" up front, instead of firing a 404/400 that blanks the box silently.
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
// Self-determining output: OCIO Read/Player always declare BOTH an IMAGE and a VIDEO output (backend
// RETURN_TYPES), but we SHOW the VIDEO slot only when the loaded content is a video, and hide it otherwise - so a
// still/sequence looks IMAGE-only and a video exposes the VIDEO output. VIDEO is the LAST output, so add/remove it
// never shifts the IMAGE/MASK/FLOAT/STRING indices (backend maps by index). A wired VIDEO slot is kept (don't yank a
// saved / user connection). The true single self-determining slot would be a V3 MatchType port.
// 2026-08-12: THIS NO LONGER ADDS OR REMOVES THE SLOT, and the reason is a correctness one that overrides the
// tidiness the removal bought. A link is serialised as an output INDEX and the backend maps that index through
// RETURN_TYPES, so the front end's outputs array has to agree with the backend's, position for position. The old
// remove/re-add was safe only while VIDEO was the last output; 'source metadata' now sits at index 5 behind it, so
// removing VIDEO would slide source metadata down into index 4 on the client while the server still answers index 4
// with a VIDEO object - a wire that looks connected and delivers the wrong type, silently. Moving source metadata
// ABOVE VIDEO is not the alternative: that reindexes the VIDEO links of every already-saved workflow.
// So the slot stays and its LABEL carries the "video sources only" hint instead. Less tidy, and it cannot corrupt
// a graph. (Adding the slot when it is missing entirely is kept: a workflow saved before the 6th output existed
// comes back with a short array.)
function _setVideoOutput(node, show) {
    if (node && node.type === "OCIOPlayer") return;   // 2026-07-04: the Player is INPUT-ONLY (no outputs), so never add/remove a VIDEO slot on it
    if (!node || !node.outputs) return;
    let idx = -1;
    for (let i = 0; i < node.outputs.length; i++) if (node.outputs[i].type === "VIDEO") { idx = i; break; }
    if (idx < 0) {
        // Append ONLY when it lands at index 4, which is where the backend puts VIDEO. addOutput always appends,
        // so on any other array length it would place VIDEO at the wrong index and hand the wire the wrong type.
        if (show && node.outputs.length === 4) { node.addOutput("ComfyUI Video", "VIDEO"); node.setDirtyCanvas(true, true); }
        return;
    }
    const o = node.outputs[idx];
    const want = show ? "ComfyUI Video" : "ComfyUI Video (video sources only)";
    if (o.label !== want) { o.label = want; node.setDirtyCanvas(true, true); }
}
function ensureReadPreview(node) {
    if (node._ocioPrev) return node._ocioPrev;
    const box = document.createElement("div");
    box.style.cssText = "width:100%;height:100%;position:relative;display:flex;justify-content:center;align-items:center;overflow:hidden;";
    const img = document.createElement("img");
    img.style.cssText = "max-width:100%;max-height:100%;object-fit:contain;display:none;";   // default INTRINSIC sizing so an empty / pending / broken src shows NOTHING (not a broken-image icon); onload switches to 100%/100% so a small proxy still upscales to fill the node
    // a still/frame that fails to decode (server 404/400, or a non-media path that got through) shows the readable
    // "No media" message instead of a blank box (format guard).
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
    // read the source at full resolution (ORIGINAL, as-is), click again to go back.
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
        p.video.onerror = () => { _ocioBusy(p.box, false); _stopViewport(p); _showReadMsg(p, "No media - unsupported format"); };   // decode failed: readable message, not a blank box
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
    // Play buttons never turn into a pause: the icon stays play / reverse; a green inset ring just
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
    // to the node / backend. Double-click the field or hit reset -> 0.
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
        // a "+2.5" entry would read as 0. Text + manual parse accepts +/-, shows the sign, and clamps.
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
// ---- the OTHER source of sound: a decoded AudioBuffer, for frames that never were a movie -------------------
//
// The float path shows half-float frames off disk. There is no <video> to tap, so createMediaElementSource has
// nothing to attach to and the meters read a signal that does not exist - which is why they were blank on the
// Player no matter what the graph upstream had generated. The `audio` input feeds a WAV beside those frames
// (/ocio/playeraudio) and it is decoded once into an AudioBuffer here.
//
// Downstream of the source node everything is IDENTICAL to the <video> graph on purpose - same splitter, same
// two analysers, same gain, same field names - so _drawAudioMeter and the mute button work on either without
// knowing which one they were handed.
function _ensureBufAudio(p, url) {
    if (p.bufAudio && p.bufAudio.url === url) {
        if (p.bufAudio.ctx.state === "suspended") p.bufAudio.ctx.resume();
        return p.bufAudio;
    }
    _stopBufAudio(p);
    if (!url) return null;
    try {
        if (!_ocioAudioCtx) _ocioAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const ctx = _ocioAudioCtx;
        const splitter = ctx.createChannelSplitter(2);
        const analyserL = ctx.createAnalyser(), analyserR = ctx.createAnalyser();
        analyserL.fftSize = 256; analyserR.fftSize = 256;
        splitter.connect(analyserL, 0); splitter.connect(analyserR, 1);
        const gain = ctx.createGain(); gain.gain.value = 0;      // muted OUTPUT by default; the meter still reads
        gain.connect(ctx.destination);
        const a = { ctx, url, buffer: null, splitter, analyserL, analyserR, gain, muted: true,
                    dataL: new Uint8Array(analyserL.fftSize), dataR: new Uint8Array(analyserR.fftSize),
                    levelL: 0, levelR: 0, src: null, startedAt: 0, startOffset: 0 };
        p.bufAudio = a;
        fetch(url).then(r => (r.ok ? r.arrayBuffer() : Promise.reject(new Error("HTTP " + r.status))))
                  .then(buf => ctx.decodeAudioData(buf))
                  .then(decoded => { if (p.bufAudio === a) a.buffer = decoded; })
                  .catch(e => { console.warn("[OCIO] player audio unavailable:", e && e.message); });
        return a;
    } catch (e) {
        console.warn("[OCIO] audio init failed:", e && e.message);
        p.bufAudio = null;
        return null;
    }
}
function _stopBufSource(a) {
    if (a && a.src) { try { a.src.stop(); } catch (e) {} try { a.src.disconnect(); } catch (e) {} a.src = null; }
}
function _stopBufAudio(p) {
    if (!p || !p.bufAudio) return;
    _stopBufSource(p.bufAudio);
    try { p.bufAudio.gain.disconnect(); } catch (e) {}
    p.bufAudio = null;
}
// How far the sound may drift from the picture before it is re-anchored. An AudioBufferSourceNode is
// fire-and-forget: it cannot seek and cannot be reused, so keeping it in step means restarting it whenever the
// picture clock has moved somewhere the sound is not - a loop back to the in-point, a scrub, an fps change.
// Comparing POSITIONS rather than watching for those events is what makes it cover all of them, including the
// loop, where the frame clock wraps by modulo and never re-anchors itself.
//
// 80 ms is about two frames at 24 fps. Tighter and an ordinary decode hiccup restarts the node constantly;
// looser and a lip-sync error becomes visible.
const _AUDIO_SLIP = 0.08;
function _playerAudioSync(p) {
    const a = p.bufAudio; if (!a || !a.buffer) return;
    const pb = p.pb;
    // REVERSE PLAYS SILENT, and that is a limitation rather than a decision: an AudioBufferSourceNode has no
    // negative playbackRate, so playing backwards would mean decoding a reversed copy of the whole track. The
    // meters fall to zero with it, which is honest - there is no signal.
    if (!pb || !pb.playing || pb.dir < 0) { _stopBufSource(a); return; }
    if (a.ctx.state === "suspended") a.ctx.resume();
    const fps = Math.max(0.001, parseFloat(W(p.node, "fps")?.value) || pb.fps || 24);
    const want = Math.max(0, (_pbCur(p) - _pbIn(p)) / fps);
    if (a.src) {
        const at = a.startOffset + (a.ctx.currentTime - a.startedAt);
        if (at < a.buffer.duration && Math.abs(at - want) < _AUDIO_SLIP) return;   // in step, leave it alone
        _stopBufSource(a);
    }
    if (want >= a.buffer.duration) return;                    // past the end of the track: silence, not a restart loop
    try {
        const s = a.ctx.createBufferSource();
        s.buffer = a.buffer;
        s.connect(a.splitter); s.connect(a.gain);
        s.start(0, want);
        a.src = s; a.startedAt = a.ctx.currentTime; a.startOffset = want;
    } catch (e) { console.warn("[OCIO] player audio start failed:", e && e.message); }
}
function _toggleMute(p, btn) {
    const a = p.bufAudio || _ensureAudio(p);
    if (!a) { btn.title = "Audio unavailable for this clip"; return; }
    a.muted = !a.muted;
    a.gain.gain.value = a.muted ? 0 : 1;
    if (a.ctx && a.ctx.state === "suspended") a.ctx.resume();
    _setIcon(btn, a.muted ? _SVG.soundOff : _SVG.soundOn);
    btn.style.opacity = a.muted ? "0.55" : "1";
    btn.style.color = a.muted ? "#e0e8f0" : "#4caf50";
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
// Reads whichever source this preview has: the <video> tap, or the decoded buffer on the Player's float path.
// The seqMode test that used to be here was doing two jobs - "no audio on an image sequence" is already said by
// the audioRow being hidden, and saying it twice is what kept the meters dark on the Player once it HAD sound.
function _drawAudioMeter(p) {
    const t = p.transport; if (!t || !t.meter || t.audioRow.style.display === "none") return;
    const a = p.audio || p.bufAudio;
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
    // Format guard: a non-media / unsupported path (a .txt, code file, unknown container) never reaches
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
// EVERYTHING THE PLATE ACTUALLY SAYS, in the order an artist reads it: what the file IS, then what the shot
// IS. The second half comes from the file's own header (reel / scene / shot / take / camera / lens /
// timecode, resolved server-side by _plate_identity) and is the reason this panel got its own disclosure
// button: it is the answer to "which picture is this", and OCIO Write no longer has a field to type any of it
// into. Rows with nothing to say are NOT DRAWN - a header that carries no lens should cost no line.
const META_ROWS = [
    ["resolution", "Resolution"], ["format", "Format"], ["codec", "Codec"], ["pix_fmt", "Pixel format"],
    ["range", "Frames"], ["missing", "Missing"], ["fps", "FPS"], ["input_colorspace", "Colorspace"],
    ["color_primaries", "Primaries"], ["color_transfer", "Transfer"], ["alpha", "Alpha"],
    ["reel", "Reel"], ["scene", "Scene"], ["shot", "Shot"], ["take", "Take"],
    ["camera", "Camera"], ["lens", "Lens"], ["timecode", "Timecode"],
];
function ensureReadMeta(node) {
    if (node._ocioMeta) return node._ocioMeta;
    const box = document.createElement("div");
    box.style.cssText = "width:100%;font:10px/1.4 monospace;color:#9cf;background:#1a1a1a;padding:4px 6px;box-sizing:border-box;overflow:hidden;white-space:nowrap;";
    const w = node.addDOMWidget("meta", "div", box, { serialize: false });
    // Height follows the rows ACTUALLY rendered, so a plate with no camera tag reclaims those lines instead of
    // leaving a gap. Folded by its OWN button (not the Viewer's) -> no height at all. undefined means nothing
    // has rendered yet: one line, so a fresh node does not jump on its first fill.
    w.computeSize = () => {
        if (node._ocioReadMetaCollapsed) return [0, 0];
        const n = node._ocioReadMetaRows === undefined ? 1 : node._ocioReadMetaRows;
        return [0, n ? 16 * n + 8 : 0];
    };
    w._ocioAlwaysVisible = true;                      // always shown, regardless of source kind
    node._ocioMeta = box;
    return box;
}
// Guarded against re-entry: on the Vue-nodes frontend setSize fires onResize, which comes back through here.
function _readMetaRelayout(node, rows) {
    if (node._ocioReadMetaRows === rows) return;
    node._ocioReadMetaRows = rows;
    if (node._ocioReadMetaLaying) return;
    node._ocioReadMetaLaying = true;
    try { node.setSize([node.size[0], node.computeSize()[1]]); } finally { node._ocioReadMetaLaying = false; }
}
function renderMeta(box, data, node) {
    if (!data || data.error) { box.innerHTML = ""; if (node) _readMetaRelayout(node, 0); return; }
    const rangeTxt = data.kind === "still" ? "" :
        `${data.start}-${data.end} (${data.count})`;
    const alphaTxt = data.alpha === null || data.alpha === undefined ? "" : (data.alpha ? "yes" : "no");
    const ident = data.identity || {};
    const values = {
        resolution: data.resolution || "", format: (data.format || "").toUpperCase(),
        codec: data.codec || "", pix_fmt: data.pix_fmt || "",
        range: rangeTxt, missing: data.missing || "",
        fps: data.fps ? data.fps.toFixed(3) : "", input_colorspace: data.input_colorspace || "",
        color_primaries: data.color_primaries || "", color_transfer: data.color_transfer || "",
        alpha: alphaTxt,
        reel: ident.reel || "", scene: ident.scene || "", shot: ident.shot || "", take: ident.take || "",
        camera: ident.camera || "", lens: ident.lens || "", timecode: ident.timecode || "",
    };
    const shown = META_ROWS.filter(([k]) => _metaHasValue(values[k]));
    // BUILT AS NODES, NOT AS HTML. Half of these values are strings lifted verbatim out of somebody else's
    // file header - a reel or lens field can hold anything at all - and interpolating them into innerHTML
    // would execute whatever markup a plate happened to carry. textContent renders them as the text they are.
    box.textContent = "";
    for (const [k, label] of shown) {
        const row = document.createElement("div");
        row.textContent = `${label}: ${values[k]}`;
        box.appendChild(row);
    }
    if (node) _readMetaRelayout(node, shown.length);
}
async function updateReadMeta(node) {
    const box = ensureReadMeta(node);
    const src = (W(node, "source")?.value || "").trim();
    // The node is passed through so the panel can shrink to the rows it actually drew; clearing it is a
    // relayout too, or an empty box keeps the height of whatever was in it last.
    if (!src) { box.textContent = ""; _readMetaRelayout(node, 0); return; }
    try {
        const r = await fetch("/ocio/meta?" + new URLSearchParams({ src }).toString());
        const d = await r.json();
        renderMeta(box, d, node);
    } catch (e) {
        console.error("OCIO meta", e);
        box.textContent = "";
        _readMetaRelayout(node, 0);
    }
}

// RESPONSIBLE FOR: keeping user-set widget values alive across a workflow load (2026-08-09, issue #3).
// Detected defaults are applied ONLY when the artist changes the source or presses "Detect from source".
// opts.applyValues === false runs the detect WITHOUT writing any editable widget - it still refreshes what
// is NOT stored in the workflow and therefore has to be re-derived every session: _ocioSeq, the per-kind
// widget visibility and the preview. (The VIDEO output IS serialized, so a passive detect leaves it alone -
// re-deriving it would drop the saved slot whenever the source is offline and the scan reports "still".)
async function fillRange(node, source, opts) {
    const applyValues = !(opts && opts.applyValues === false);
    // Empty source: hide the frame controls (still-image default) and the VIDEO output. The slot is dropped
    // here even on a passive detect - with no source there is no video, and _setVideoOutput never removes a
    // CONNECTED slot, so nothing a workflow restored can be lost. Gating this on applyValues left the slot
    // showing on every freshly added node (2026-08-10).
    if (!source) { applyReadVis(node); updateReadPreview(node); _setVideoOutput(node, false); return; }
    // Everything the artist edits from here on is remembered by name, so a slow scan (big sequence, network
    // share) that lands later overwrites only the fields nobody touched, instead of losing the edit - or,
    // worse, dropping the whole answer and leaving the node holding values from the PREVIOUS clip.
    if (applyValues) node._ocioEdited = new Set();
    try {
        const r = await fetch("/ocio/seq_range", {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source }),
        });
        const d = await r.json();
        // the source moved on while this scan was running: the answer describes a file nobody is looking at
        if ((W(node, "source")?.value || "") !== String(source)) return;
        const edited = node._ocioEdited || new Set();
        const put = (name, val) => { if (applyValues && !edited.has(name)) setWSilent(node, name, val); };
        node._ocioSeq = d;
        // auto-set the visible Frame Mode to the detected kind: still->single, sequence->sequence, video->video
        const fmMap = { still: "single", sequence: "sequence", video: "video" };
        if (d && fmMap[d.kind]) put("frame_mode", fmMap[d.kind]);
        if (applyValues) _setVideoOutput(node, !!(d && d.kind === "video"));   // expose the VIDEO output only for a video source
        const pv = node._ocioPrev;
        if (d && (d.kind === "sequence" || d.kind === "video")) {
            put("start_frame", d.start | 0);
            put("end_frame", d.end | 0);
            put("frame_shift", d.start | 0);                   // default: first frame keeps its source number
            if (d.fps) put("fps", Math.round(d.fps * 1000) / 1000);
            if (d.input_cs) put("input_colorspace", d.input_cs);   // folder path has no ext -> fix EXR auto-detect (sRGB -> ACEScg) from the resolved first frame
            // the transport ruler + in/out span the REAL file frame count (authoritative), not video.duration
            if (pv && pv.pb) pv.pb.fileFrames = Math.max(1, (d.count | 0) || ((d.end | 0) - (d.start | 0) + 1) || 1);
        } else {
            put("start_frame", 0);
            put("end_frame", 0);
            if (pv && pv.pb) pv.pb.fileFrames = 1;
        }
        applyReadVis(node, d && d.kind);                       // d can be null on a malformed answer
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
    // start_frame/end_frame hold SOURCE numbers (so the fields match the timeline). On a
    // genuine source-range CHANGE (new first/lastN vs the last sync), snap to the full new range [first .. lastN] - a new clip shows
    // whole. On a re-render of the SAME source (exposure / colorspace change, same range) preserve the current
    // values if still a valid sub-range (keeps a user trim), else snap. Root cause of the stale-widget bug: the
    // old guard could not tell a previous AUTO-SET range from a user trim, so switching to a source whose range
    // CONTAINED the old values kept them (start/end widgets stale while the meta panel already showed the new range).
    // _syncRange is runtime-only, so after a page reload there is no history at all - and treating "no history"
    // as "the range changed" snapped away a trim the workflow had saved, on the first render. With no history,
    // let the validity test below decide: saved values that are a valid sub-range of the clip are the artist's
    // trim and are kept; anything stale or out of range still snaps to the full clip (2026-08-10).
    // The clip is identified by the upstream SOURCE, not only by its range: two different clips of the same
    // length produce the same first/lastN, and judging by range alone carried one clip's trim onto the next
    // (2026-08-10). No history at all means this is the first sync of the session, where the values came from
    // the workflow and validity is the only thing to judge them by.
    const srcId = (W(read, "source")?.value || "");
    const clipChanged = !!p._syncRange &&
        (p._syncRange.src !== srcId || p._syncRange.first !== first || p._syncRange.last !== lastN);
    const curSF = Math.round(W(node, "start_frame")?.value || 0), curEF = Math.round(W(node, "end_frame")?.value || 0);
    if (clipChanged || !(curSF >= first && curEF <= lastN && curSF <= curEF && curEF > 0)) {
        setWSilent(node, "start_frame", first); setWSilent(node, "end_frame", lastN);
    }
    p._syncRange = { src: srcId, first, last: lastN };
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
// applyAutoColorspace stood here and is removed: it had ZERO callers, and the job it described is done by the
// `profile` combo's "auto" entry through resolveAutoProfile below, which traces the same upstream node and sets
// BOTH colorspaces via applyProfile. One mechanism, reachable, and mirrored by a test. The `auto_colorspace`
// WIDGET stays: widgets_values is positional, so removing it would shift every later value in every saved graph.
// Its tooltip names what actually does the work.

// ---- profile widget: HDR source preset -> from/output colorspace + still_format/bit_depth (silent) ---------
// These must stay byte-identical to the backend mapping in io_nodes.py (OCIOWrite.write), and the from/out
// strings must be values the input_colorspace combo actually offers - ComfyUI rejects an unknown combo value
// with HTTP 400 and no fallback. tests/test_ltx_hdr_profiles.py asserts the mirror - it reads this table and
// compares it against the backend mapping parsed out of io_nodes.py by AST. (This line used to name
// tests/test_write_output.py, which contains no profile assertion at all and never has.)
//
// "LTX 2.3 HDR" IS A 2.3-ONLY PRESET, and the reason is the transfer the frames arrive in. 2.3's HDR is an
// IC-LoRA on the ARRI LogC3 (EI 800) curve, and Lightricks' own ComfyUI node for it,
// LTXVHDRDecodePostprocess, already undoes the curve, so what reaches Write is LINEAR.
// THERE IS NO 2.5 ROW, and its absence is the decision. 2.5's HDR is ACEScct, reached through the --hdr flag
// in their reference CLI; ComfyUI's core has no ACEScct path at all, so a graph here does not produce those
// codes on its own. Using the 2.3 preset on genuine 2.5 ACEScct material treats log as linear and leaves the
// frame flat and grey. Undo that curve explicitly instead: OCIO LogConvert, "Log to Linear", curve "ACEScct".
const PROFILE_CS = {
    "LTX 2.3 HDR":               { from: "Linear Rec.709 (sRGB)", out: "ACEScg", fmt: "exr", bit: "16f" },
    "LumiPic LogC3 (Flux/Qwen)": { from: "Linear Rec.709 (sRGB)", out: "ACEScg", fmt: "exr", bit: "16f" },
    "LumiPic V10 LogC4":         { from: "Linear Rec.709 (sRGB)", out: "ACEScg", fmt: "exr", bit: "16f" },
    // No fmt/bit: this is the one display-referred preset, so it must NOT force EXR 16f the way the HDR ones
    // do. tests/test_ltx_hdr_profiles.py reads the backend mapping by AST and compares it against this table.
    "SDR Rec.709 delivery":      { from: "sRGB - Display",       out: "Rec.1886 Rec.709 - Display" },
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
    setWSilent(node, "input_colorspace", p.from);
    setWSilent(node, "output_colorspace", p.out);
    // GUARDED, and this is not defensive style. setWSilent is a bare `w.value = value`, so a row without fmt/bit
    // used to write JavaScript `undefined` straight into two COMBO widgets. Both serialisations of that are a
    // hard reject, measured against the live backend: null gives 400 [value_not_in_list] "None not in
    // ['exr','tiff','png','jpeg']", and an absent key gives 400 [required_input_missing]. So picking such a
    // profile would leave a graph that cannot be queued at all. Every HDR row carries fmt/bit; "SDR Rec.709
    // delivery" deliberately does not, because a display-referred delivery has no business forcing EXR 16f.
    if (p.fmt) setWSilent(node, "still_format", p.fmt);
    if (p.bit) setWSilent(node, "bit_depth", p.bit);
    node._ocioProfileSetting = false;
    node.setDirtyCanvas(true, true);
}
// best-effort upstream source detection for profile === "auto"
function findUpstreamSource(node) {
    const ltx = findUpstream(node, (n) => (n.type || "").includes("LTXVHDRDecodePostprocess"));
    if (ltx) return "LTX 2.3 HDR";                      // reliable: a dedicated LTX HDR decode node
    // Nothing detects LTX 2.5 here, and there is no longer a preset for it to select. The preset was removed
    // because its premise did not hold on this runtime: 2.5's HDR is ACEScct via the --hdr flag in
    // Lightricks' reference CLI, their ComfyUI pack has an HDR workflow for 2.3 and none for 2.5 (checked
    // 2026-08-12), and ComfyUI's core has no ACEScct path at all (greps across comfy/ and comfy_extras/
    // return zero, rechecked 2026-08-15). Do not add a detector back: a 2.5 checkpoint name says nothing
    // about the transfer, since a 2.5 graph is usually SDR. 2.5 HDR material is undone explicitly with
    // OCIO LogConvert ("Log to Linear", curve "ACEScct").
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
let _ocioLastOutputDir = "";   // OCIO Write: the ABSOLUTE last-CHOSEN output folder, so "Output Folder" re-opens there. Separate from _ocioLastBrowseDir so input browsing doesn't move the output start.
async function listDir(path, wantFiles, sequence) {
    const r = await fetch("/ocio/list_dirs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path || "", files: !!wantFiles, sequence: !!sequence }),
    });
    return await r.json();
}
// A picked output folder -> what gets STORED in the widget. Anything under the ComfyUI output root becomes
// relative, because this value is saved into widgets_values and core SaveVideo / SaveImage embed the whole
// workflow JSON inside the files they write - an absolute server path there ships with the delivery.
// Comparison is case-insensitive and slash-agnostic, because on Windows the same folder can be written
// with either slash and in either case: the old byte-exact startsWith treated those as different places
// and left a hand-typed path absolute for no reason. A path genuinely OUTSIDE the output root is returned
// untouched - targeting a NAS is deliberate, not a mistake.
function relToOutput(absPath, outputRoot) {
    if (!outputRoot) return absPath;
    const norm = (s) => s.replace(/\\/g, "/").replace(/\/+$/, "");
    const a = norm(absPath), o = norm(outputRoot);
    const al = a.toLowerCase(), ol = o.toLowerCase();
    if (al === ol) return "";
    if (al.startsWith(ol + "/")) return a.slice(o.length + 1);
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
    // Sequence checkbox (Read source browser): collapse numbered frames in a folder into ONE entry per
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

// (2026-07-04: node titles carry NO version suffix anymore - titles stay clean. __version__ is still
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

// --------------------------------------------------------------------------- what is this viewport presenting?
//
// Users ask whether the Player shows them HDR or 8 bits, and until now nothing answered. It presents an 8-bit
// SDR composite, and that is worth SAYING rather than leaving people to guess from how the picture looks.
//
// The probe runs on a THROWAWAY 1x1 canvas, never on the live viewport. Asking the real context for a float
// drawing buffer would reallocate and clear it, and a probe that disturbs the thing it measures is not a probe.
//
// Three situations, and they are genuinely different for the person looking at the screen:
//   * the browser has no float drawing buffer at all             -> nothing to be done here
//   * it has one, but the display does not report HDR            -> a float buffer would change nothing on screen,
//                                                                   because the composite still has to land in SDR
//   * it has one AND the display reports HDR                     -> range is being left on the table
//
// `(dynamic-range: high)` reports a CAPABILITY, not an active state, and the window can be dragged to another
// monitor, so its value is tracked through a change listener rather than read once. Everything else is fixed for
// the life of the page.
let _presCaps = null;
let _presHdrMq = null;

function presentationCaps() {
    if (_presCaps) return _presCaps;
    const caps = { webgl2: false, floatExt: false, storageFn: false, floatBuffer: false, colorSpace: null };
    try {
        const c = document.createElement("canvas");
        c.width = c.height = 1;
        const gl = c.getContext("webgl2", { alpha: true, premultipliedAlpha: false, antialias: false });
        if (gl) {
            caps.webgl2 = true;
            caps.floatExt = !!gl.getExtension("EXT_color_buffer_float");
            caps.storageFn = typeof gl.drawingBufferStorage === "function";
            // The colour space must be set BEFORE the storage call, because assigning it reallocates the buffer.
            // Presence of the property does not mean a given enum value is accepted, so it is read back.
            if ("drawingBufferColorSpace" in gl) {
                try {
                    gl.drawingBufferColorSpace = "srgb-linear";
                    caps.colorSpace = gl.drawingBufferColorSpace;
                } catch (e) { caps.colorSpace = null; }
            }
            if (caps.floatExt && caps.storageFn) {
                while (gl.getError() !== gl.NO_ERROR) { /* drain, or a stale error misreports the next call */ }
                gl.drawingBufferStorage(gl.RGBA16F, 1, 1);
                caps.floatBuffer = gl.getError() === gl.NO_ERROR && gl.drawingBufferFormat === gl.RGBA16F;
            }
            const lose = gl.getExtension("WEBGL_lose_context");
            if (lose) { try { lose.loseContext(); } catch (e) { /* tidiness only */ } }
        }
    } catch (e) { /* a probe that throws must not take the node down */ }
    _presCaps = caps;
    return caps;
}

function presentationHdrDisplay() {
    if (_presHdrMq === false) return false;               // matchMedia is unavailable here; asked once, not per draw
    try {
        if (!_presHdrMq) {
            _presHdrMq = window.matchMedia("(dynamic-range: high)");
            // graph.setDirtyCanvas, not node.setDirtyCanvas: this fires outside the render loop and no single node
            // owns the answer. It is the frontend's own pattern for an async event that needs a repaint.
            const redraw = () => { try { app.graph && app.graph.setDirtyCanvas(true, true); } catch (e) {} };
            if (_presHdrMq.addEventListener) _presHdrMq.addEventListener("change", redraw);
            else if (_presHdrMq.addListener) _presHdrMq.addListener(redraw);      // older Safari shape
        }
        return !!_presHdrMq.matches;
    } catch (e) {
        _presHdrMq = false;                               // cache the failure, so a broken environment costs one try
        return false;
    }
}

function presentationLine() {
    try {
        const c = presentationCaps();
        const hdr = presentationHdrDisplay();
        // NOTHING is said when there is no WebGL2. The Player already shows "WebGL2 unavailable" in its own body
        // and displays no image at all in that state, so a second line in the corner has nothing to add and must
        // not describe a picture that is not there. OCIO Read is the node that falls back to a still thumbnail;
        // this function belongs to the Player.
        if (!c.webgl2) return null;
        // SHORT VALUE, LONG REASON. The panel row is one monospace line inside the node's width, so a sentence got
        // cut off mid-word ("float buffer a...") and the part that mattered never arrived. `text` is now what fits
        // and answers the question - 8 bits or not - and `detail` carries the why, which the panel puts on the
        // hover where there is room for it.
        if (!c.floatBuffer) {
            return { text: "8-bit SDR", hdrAvailable: false,
                     detail: "The composite reaching the screen is 8-bit SDR, and this browser offers no float "
                           + "drawing buffer at all, so there is no route to more than 8 bits per channel here. "
                           + "Values above 1.0 are still carried in the data and still written to EXR - it is the "
                           + "on-screen presentation that is 8-bit, not the pixels." };
        }
        // TWO SEPARATE FACTS, and conflating them made the message wrong once: the float buffer and the colour
        // space it can be DECLARED as are independent. Measured in Chromium: the buffer allocates as RGBA16F and
        // reports 16 bits, while `drawingBufferColorSpace` accepts only "srgb" and "display-p3" and THROWS a
        // TypeError for "srgb-linear" and "display-p3-linear". Without a linear declaration the compositor reads
        // the numbers through the sRGB transfer function, which is not what extended linear light means, so a
        // float buffer here is storage without a correct interpretation. Say both, briefly.
        const linear = c.colorSpace === "srgb-linear";
        if (!hdr) {
            return { text: "8-bit SDR", hdrAvailable: false,
                     detail: "The composite reaching the screen is 8-bit SDR. A float drawing buffer IS available "
                           + "in this browser, but this display does not report HDR capability, so there is no "
                           + "headroom to show anything above white and the final conversion has to land in the "
                           + "SDR range. Values above 1.0 are still carried in the data and still written to EXR - "
                           + "it is the on-screen presentation that is 8-bit, not the pixels." };
        }
        return {
            text: linear ? "float buffer, HDR display" : "float buffer, HDR display, no linear space",
            hdrAvailable: true,
            detail: linear
                ? "This display reports HDR capability and a float drawing buffer is available, declared in a "
                  + "linear colour space - the combination that can actually carry values above white to the "
                  + "compositor."
                : "This display reports HDR capability and a float drawing buffer is available, but it could not be "
                  + "declared in a linear colour space (Chromium accepts only srgb and display-p3 and throws for "
                  + "srgb-linear), so the compositor reads the numbers through the sRGB transfer function rather "
                  + "than as extended linear light.",
        };
    } catch (e) { return null; }
}

// Exposure shader: RGBA16F float texture -> 2^exposure gain (input space) -> optional OCIO 3D LUT -> screen.
const _PLAYER_FRAG = `#version 300 es
precision highp float; precision highp sampler3D;
in vec2 uv; out vec4 o;
uniform sampler2D uImg; uniform sampler3D uLut; uniform float uN; uniform float uOn; uniform float uExposure; uniform float uShaper;
float lin2cct(float x){                                 // scene-linear -> ACEScct code (exact inverse of io_nodes._acescct_to_lin)
  if (x <= 0.0078125) return 10.5402377416545 * x + 0.0729055341958355;
  return (log2(max(x, 1e-10)) + 9.72) / 17.52;
}
void main(){
  vec4 src = texture(uImg, uv);
  vec3 c = src.rgb * exp2(uExposure);                 // VIEW-ONLY exposure, in the INPUT colorspace (see header note)
  if (uOn > 0.5) {                                     // OCIO display LUT. Scene-linear input (uShaper) -> log-shape the coord so the [0,1] LUT spans HDR (no crushed highlights / flat toe); else sample linearly.
    vec3 coords = (uShaper > 0.5) ? vec3(lin2cct(c.r), lin2cct(c.g), lin2cct(c.b)) : c;
    vec3 s = clamp(coords, 0.0, 1.0) * ((uN - 1.0) / uN) + 0.5 / uN;
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
                   uExposure: gl.getUniformLocation(prog, "uExposure"), uShaper: gl.getUniformLocation(prog, "uShaper") };
    gl.uniform1i(gl.getUniformLocation(prog, "uImg"), 0); gl.uniform1i(gl.getUniformLocation(prog, "uLut"), 1);
    p.gl = { gl, prog, locs, imgTex, lutTex };
    return p.gl;
}
async function _playerRefreshLut(node, p) {
    const g = p.gl; if (!g) return;
    // 2026-07-04: pick the LUT's INPUT colorspace by viewport kind.
    //  - STREAMING a raw video (p.videoMode): the <video> pixels are in the FILE's SOURCE colorspace - streaming
    //    bypasses the upstream Read's conversion, so the Player's own input_colorspace does NOT describe this data.
    //    Color-manage source -> player output (= what a direct Read(video)->Player must show; it
    //    was applying player_input(ACES...) to raw sRGB video -> lifted/flat). Source = the upstream Read's input_cs.
    //  - FLOAT viewport: the cached frames ARE in the Player's input_colorspace (the Read already converted them),
    //    and may be scene-linear -> ask for the shaper (float=1).
    let inCs = W(node, "input_colorspace")?.value || "";
    let floatFlag = "1";
    if (p.videoMode) {
        const r = (typeof findUpstreamRead === "function") ? findUpstreamRead(node) : null;
        if (r) inCs = W(r, "input_colorspace")?.value || inCs;   // the raw stream is in the source's colorspace
        floatFlag = "0";                                         // display-referred 8-bit video -> no scene-linear log shaper
    }
    const q = new URLSearchParams({ in_cs: inCs, out_cs: W(node, "output_colorspace")?.value || "",
        raw: W(node, "raw_data")?.value ? "1" : "0", size: "33", float: floatFlag });
    try {
        const r = await fetch("/ocio/lut?" + q.toString()); if (!r.ok) throw new Error("lut " + r.status);
        const n = parseInt(r.headers.get("X-Lut-Size") || "33", 10); const buf = new Uint8Array(await r.arrayBuffer());
        p.shaper = (r.headers.get("X-Shaper") === "1") ? 1 : 0;   // backend decided (scene-linear input) -> shader applies lin->ACEScct before the LUT
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
    gl.uniform1f(g.locs.uShaper, p.shaper || 0);          // scene-linear input -> lin->ACEScct shaper before the LUT (fixes the flat Player)
    gl.drawArrays(gl.TRIANGLES, 0, 3);
}
// ---- Player frame cache: each frame is its own RGBA16F GPU texture kept in a bounded LRU, so playback and scrub
// read from VRAM instead of re-fetching ~100 MB per frame over HTTP (that on-demand refetch was the slowness the
// observed vs OCIO Read's tiny 8-bit thumbs). Budget-capped: a 4K RGBA16F frame ~= 116 MB, so ~17 fit in ~2 GB;
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
        _playerAudioSync(p);                                 // keep the track in step with the frame clock
        _drawAudioMeter(p);
        _syncTransport(p);
        p.raf = requestAnimationFrame(loop);
    };
    p.raf = requestAnimationFrame(loop);
}
function _playerStop(p) {
    if (!p) return;
    if (p.raf) { cancelAnimationFrame(p.raf); p.raf = 0; }
    if (p.pb) { p.pb.playing = false; }
    // The rAF is what keeps the track in step, so it has to be torn down HERE and not left to the next tick -
    // there is no next tick. A buffer source outlives the node that started it otherwise, and the sound of a
    // deleted Player keeps playing.
    _stopBufAudio(p);
    _playerClearTex(p);                                  // free the frame textures from VRAM on teardown
}

// The Player's preview-state object `p` mirrors the shape the shared transport helpers read (p.node, p.pb, p.gl,
// p.canvas, p.transport, p.exposure). pb.seqMode is TRUE so _pbCur / _pbSeek use the frame-index path (there is
// no <video>), but _seqBase stays 0 (p.seq === null) so start_frame/end_frame are read as plain 0-based indices.
// ---- Stream a VIDEO source in the OCIO Player (Load Video -> Player.video, or a video OCIO Read traced to a
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
    gl.uniform1f(g.locs.uShaper, p.shaper || 0);          // scene-linear input -> lin->ACEScct shaper before the LUT (fixes the flat Player)
    gl.drawArrays(gl.TRIANGLES, 0, 3);
}
function _playerVideoRaf(node, p) {
    if (p.raf) return;
    const loop = (now) => {
        if (node._ocioPlayer !== p || !p.videoMode) { p.raf = 0; return; }
        _tickPlayback(p, now || 0);                          // shared video clock (seqMode=false -> drives p.video)
        _playerVideoDraw(p);
        _drawAudioMeter(p);                                  // the streamed clip has its own track, via _ensureAudio
        _syncTransport(p);
        p.raf = requestAnimationFrame(loop);
    };
    p.raf = requestAnimationFrame(loop);
}
// Resolve the streamable URL for a video source via /ocio/proxy. A browser codec (h264/vp8/vp9/
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
    // 2026-08-10: span the whole clip only when the current values are NOT a usable trim. This runs on every
    // stream start, including the one right after a workflow load, and it used to overwrite an in/out the artist
    // had saved. Same rule syncPlayerFromUpstream applies on the float path.
    const _vf = Math.max(1, meta.frames || 0);
    const _lo = p.videoBase, _hi = p.videoBase + _vf - 1;
    const _curS = Math.round(W(node, "start_frame")?.value || 0), _curE = Math.round(W(node, "end_frame")?.value || 0);
    // Keep a trim only when the clip did not change under it. Validity alone is not enough: a trim of 10..50
    // fits any other clip of 50 frames or more, so judging by range let one clip's in/out follow the artist
    // onto the next one. The stream path has no range history, so clip identity is the path itself, and
    // p._vidPath still holds the PREVIOUS one here. Unset means this is the first stream of the session: then
    // the values came from the workflow and are judged on validity alone, which is the point of the fix that
    // stopped a saved in/out being wiped on load (2026-08-10).
    const _otherClip = p._vidPath != null && p._vidPath !== path;
    if (_otherClip || !(_curS >= _lo && _curE <= _hi && _curS <= _curE && _curE > 0)) {
        setWSilent(node, "start_frame", _lo);
        setWSilent(node, "end_frame", _hi);
    }
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
        _resolveStreamSrc(node, p, path, meta);              // stream a browser codec directly, or build+stream an H.264 proxy for ProRes/DNxHR/MXF
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
    // AUDIO ROW ON, because in this mode there is real audio to meter: the Player is driving a <video>
    // element over the source file (or its proxy), exactly as OCIO Read's viewer does, and that element
    // carries the track. It was hidden here while the identical Read path showed it, so the same clip metered
    // and played sound in one node and was silent in the other - a difference with no reason behind it. The
    // mute button starts muted, so nothing makes noise until it is asked to. (The float-frame path further
    // down keeps the row hidden, and that one is right: half-float textures are pictures, with no track at
    // all.)
    p.pb.showTransport = true; if (p.transport) { p.transport.bar.style.display = "flex"; if (p.transport.audioRow) p.transport.audioRow.style.display = "flex"; }
    if (p.refreshOverlay) { p.refreshOverlay._stale = false; p.refreshOverlay.style.background = "rgba(40,40,64,0.85)"; p.refreshOverlay.title = "Refresh this viewport"; p.refreshOverlay.style.display = ""; }   // persistent top-left Refresh square in video mode: re-reads the current upstream file (switch the Load Video file -> click)
    _setVideoOutput(node, true);                             // streaming a video (any trigger) -> expose the VIDEO output
    renderPlayerMeta(node, { resolution: meta.res || "-", total: meta.frames || 0, cached: meta.frames || 0, fps: meta.fps || 24, input_cs: W(node, "input_colorspace")?.value });   // show meta now; loadedmetadata fills in the resolution
    node.setSize([node.size[0], node.computeSize()[1]]);
    _playerVideoRaf(node, p);
}
// Trace the graph back to the upstream video FILE: a standard Load Video node (its 'file' widget, resolved against
// the input dir by /ocio/stream) or an OCIO Read with a video 'source'. Lets the Player RE-READ the current file on
// Refresh - so changing the Load Video file (a widget change, NOT a connection change) is picked up without recreating
// the node, and without a backend round-trip that ComfyUI might cache. Added 2026-07-03.
// OCIO color-processing node types (NOT the sources Read/LoadVideo, NOT the Player). If ANY of
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
    // Persistent Refresh: a small SQUARE in the TOP-LEFT, always visible over any content (image / sequence / video)
    // so the viewport can be re-pulled anywhere - not just in video mode. Normally slate;
    // turns amber (._stale) when a node was inserted / rewired upstream so the cached frames are stale, until the next
    // render clears it. onClick: video upstream -> re-read the file; else Queue (OCIOPlayer is an OUTPUT_NODE viewer).
    const refreshOverlay = document.createElement("button");
    refreshOverlay.textContent = "↻";
    refreshOverlay.title = "Refresh this viewport";
    refreshOverlay.style.cssText = "position:absolute;top:6px;left:6px;z-index:5;display:none;width:26px;height:26px;padding:0;border:0;border-radius:4px;background:" + OV_BASE + ";color:#cde;cursor:pointer;font:16px/1 sans-serif;box-shadow:0 1px 4px rgba(0,0,0,0.5);";
    refreshOverlay.onmouseenter = () => refreshOverlay.style.background = refreshOverlay._stale ? "rgba(175,115,30,0.95)" : "rgba(57,57,90,0.95)";
    refreshOverlay.onmouseleave = () => refreshOverlay.style.background = refreshOverlay._stale ? OV_STALE : OV_BASE;
    refreshOverlay.onclick = () => { const pp = node._ocioPlayer; refreshOverlay._stale = false; refreshOverlay.style.background = OV_BASE; refreshOverlay.title = "Refresh this viewport"; _ocioBusy(pp && pp.box, true, "Processing…"); if (pp && (pp.videoMode || _playerTraceVideoSrc(node))) _playerVideoRefresh(node); else app.queuePrompt(0, 1); };
    // Exposure control now lives HORIZONTALLY in the transport strip (between the viewport and the timeline), built
    // in _ensureTransport when p.isPlayer is set. No slider inside the viewport anymore.
    const video = document.createElement("video");           // hidden <video> for streaming a video source into the WebGL viewport (exposure + LUT shader)
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
    p.video = video;                                     // streamed-video element (drawn via the exposure+LUT shader)
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
                else if ((pp.player || pp.videoMode) && pp.refreshOverlay) { pp.refreshOverlay._stale = true; pp.refreshOverlay.style.background = "rgba(150,95,20,0.92)"; pp.refreshOverlay.title = "Input changed - click to re-render"; pp.refreshOverlay.style.display = ""; }   // float source changed / processing node inserted into a video chain -> flag the Refresh square amber (stale)
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
    // viewer with the same result, and the viewer must NOT "re-play itself" on every unrelated render. A real
    // change (new source, new size / frame count) has a different signature and still re-inits; onConnectionsChange
    // clears _lastExecSig so a genuine re-wire always re-inits too.
    const _sig = JSON.stringify([first(message && message.video_path), first(message && message.player_dir),
        first(message && message.player_total), first(message && message.player_cached),
        first(message && message.resolution), first(message && message.input_cs),
        first(message && message.content_sig),
        // audio_sig (rate_channels_samples) for the same reason content_sig exists: swap ONLY the track upstream
        // and the dir, the count and the pixels are all identical, so without a term of its own the viewer would
        // keep playing the sound it already had.
        first(message && message.audio_sig)]);   // content_sig: first-frame mean/std -> a LogConvert swap (same dir/size, different pixels) re-inits instead of going stale
    if (_sig === p._lastExecSig && (p.player || p.videoMode)) return;   // unchanged -> leave the current viewport playing
    p._lastExecSig = _sig;
    // VIDEO source (a Load Video / OCIO Read video traced to a file): stream it client-side, NOT the float batch.
    // Phase 1a: capture the path + metadata; Phase 1b streams it via WebCodecs. For now show a placeholder + meta.
    const vpath = first(message && message.video_path);
    if (vpath) {
        const vres = first(message.video_res) || "", vfps = parseFloat(first(message.video_fps)) || 24, vframes = parseInt(first(message.video_frames), 10) || 0;
        p.videoSrc = { path: vpath, res: vres, fps: vfps, frames: vframes };
        _setVideoOutput(node, true);                                        // video source -> expose the VIDEO output
        renderPlayerMeta(node, { resolution: vres, total: vframes, cached: vframes, fps: vfps, input_cs: first(message.input_cs) });
        // `res: vres` IS LOAD-BEARING, not decoration. playerVideoStart re-renders the metadata panel immediately
        // with `meta.res || "-"`, so leaving it out overwrote the resolution this branch had just displayed. For a
        // clip the browser CAN decode that is invisible, because loadedmetadata then fills the real videoWidth /
        // videoHeight - but a ProRes or DNxHR clip it cannot decode (a case handled a few lines below) never fires
        // that event, so the panel kept reading "-" for a resolution the server had already told us.
        playerVideoStart(node, p, vpath, { fps: vfps, frames: vframes, res: vres });   // stream the whole clip (native decode + exposure/LUT shader)
        return;
    }
    if (p.videoMode) { p.videoMode = false; try { p.video.pause(); } catch (e) {} if (p.raf) { cancelAnimationFrame(p.raf); p.raf = 0; } }   // was streaming a video, now a float batch -> stop the stream
    _setVideoOutput(node, false);                            // float batch (image/sequence) -> hide the VIDEO output
    const dir = first(message && message.player_dir);
    const total = parseInt(first(message && message.player_total) || "0", 10);
    const cached = parseInt(first(message && message.player_cached) || "0", 10);
    const resolution = first(message && message.resolution) || "";
    const fps = parseFloat(first(message && message.fps) || "") || 0;
    const inputCs = first(message && message.input_cs) || "";
    const rangeNote = first(message && message.range_note) || "";
    if (!dir || !(cached > 0)) { renderPlayerMeta(node, null); return; }
    p.player = { dir, total, cached, resolution };
    _playerClearTex(p);                                  // fresh render -> the old frame textures are stale, drop them
    if (p.refreshOverlay) { p.refreshOverlay._stale = false; p.refreshOverlay.style.background = "rgba(40,40,64,0.85)"; p.refreshOverlay.title = "Refresh this viewport"; p.refreshOverlay.style.display = ""; }   // rendered (image/sequence) -> persistent top-left Refresh square, no longer stale
    p.autoCsChecked = false;                             // re-evaluate HDR auto-cs for this fresh render
    p._playerFirstErr = null;
    p.pb.fileFrames = cached;                            // transport ruler spans the CACHED frames (0..cached-1)
    p.pb.seqFrame = Math.max(0, Math.min(cached - 1, p.pb.seqFrame | 0));
    if (fps) { p.pb.fps = fps; }
    syncPlayerFromUpstream(node);                        // mirror source frame numbering (base) + fps from the upstream OCIO Read, through any chain of nodes
    // show the viewport, hide the placeholder; show the transport bar
    p.empty.style.display = "none"; p.canvas.style.display = "";
    // THE METER ROW NOW DEPENDS ON WHETHER THERE IS A TRACK, not on which path drew the picture. It was hidden
    // unconditionally here, which was right while a batch of frames could not have sound and wrong the moment
    // the `audio` input existed.
    const audioName = first(message && message.player_audio) || "";
    const audioUrl = audioName ? "/ocio/playeraudio?dir=" + encodeURIComponent(dir) : "";
    if (audioUrl) _ensureBufAudio(p, audioUrl); else _stopBufAudio(p);
    p.pb.showTransport = true;
    if (p.transport) {
        p.transport.bar.style.display = "flex";
        if (p.transport.audioRow) p.transport.audioRow.style.display = audioUrl ? "flex" : "none";
    }
    if (!_playerInitGL(p)) {                             // no WebGL2 -> message, no viewport
        p.canvas.style.display = "none"; p.empty.style.display = "flex";
        p.empty.firstChild.textContent = "WebGL2 unavailable - cannot show float viewport";
        renderPlayerMeta(node, { resolution, total, cached, fps: p.pb.fps, input_cs: inputCs, range_note: rangeNote });   // p.pb.fps = source fps after syncPlayerFromUpstream
        return;
    }
    _playerLayout(node);
    _playerRefreshLut(node, p);                          // bake in_cs->out_cs display LUT
    _playerShow(p);                                      // upload + draw the current frame
    _playerPrefetch(p);                                  // warm the rest of [in,out] into the texture cache (teal bar shows progress)
    _playerEnsureRaf(node, p);
    renderPlayerMeta(node, { resolution, total, cached, fps: p.pb.fps, input_cs: inputCs, range_note: rangeNote });   // p.pb.fps = source fps after syncPlayerFromUpstream
}

// ---- Player metadata panel: resolution / frames / fps / colorspace, from the onExecuted payload + widgets.
const PLAYER_META_ROWS = [
    ["resolution", "Resolution"], ["frames", "Frames"], ["range", "Range"], ["fps", "FPS"],
    ["input_colorspace", "Input CS"], ["output_colorspace", "Output CS"],
    // "Display" ANSWERS "what am I actually looking at" IN THE DOM, and that is the whole point of it being here
    // rather than in the node's corner text. presentationLine() was originally drawn from onDrawForeground, which
    // the Vue node renderer never calls at all - proven with counters on the real render path: drawNode ran five
    // times for the node while onDrawForeground ran zero. This panel is an addDOMWidget, so it renders on both
    // frontends, and it already exists to describe what is on screen.
    ["presentation", "Display"],
    // WHETHER PULLING EXPOSURE DOWN CAN REVEAL ANYTHING, answered before it is tried. A display-referred
    // master's ceiling IS white - the writer clips there - so its highlights were gone before the file
    // existed, and a viewer that just darkens looks broken while behaving correctly. Measured on a real
    // Rec.709 ProRes from this pack's own writer: 136 samples above white across 121 frames, 0.000125%.
    ["range_note", "Range check"],
];
// A ROW WITH NOTHING TO SAY IS NOT DRAWN. The panel used to print every label always and fill the unknown ones
// with a dash, so a clip the browser cannot decode showed "Resolution: -" and a still with no timecode showed
// three dashes in a row - technical clutter that tells the artist nothing and costs a line of the node each.
// Empty, a literal dash, and a zero all count as nothing: an fps of 0.000 or a frame count of 0 is a missing
// value wearing a number, not a measurement.
// A DISCLOSURE BUTTON'S CHEVRON MUST FOLLOW ITS STATE: pointing down when the block is open, right when it is
// closed. Writing `name` alone does NOT do that on the Vue-nodes frontend - measured by reading the button's own
// DOM text before and after: assigning `name` left it unchanged, assigning `label` flipped it in the same frame.
// The legacy canvas renderer draws `label` when present and falls back to `name`, so both are set and the two
// frontends cannot disagree.
function _setWidgetLabel(w, text) {
    if (!w) return;
    w.name = text;
    w.label = text;
}
const _metaHasValue = (v) => {
    const s = String(v == null ? "" : v).trim();
    return s !== "" && s !== "-" && s !== "0" && s !== "0.000";
};
// The panel's height follows the rows ACTUALLY rendered, so hiding a row reclaims its line instead of leaving a
// gap. Guarded against re-entry, because on the Vue-nodes frontend setSize fires onResize, which comes back
// through the same path - the trap already documented for the player box a few functions below.
function _metaRelayout(node, rows) {
    if (node._ocioMetaRows === rows) return;
    node._ocioMetaRows = rows;
    if (node._ocioMetaLaying) return;
    node._ocioMetaLaying = true;
    try { node.setSize([node.size[0], node.computeSize()[1]]); } finally { node._ocioMetaLaying = false; }
}
function ensurePlayerMeta(node) {
    if (node._ocioPlayerMeta) return node._ocioPlayerMeta;
    const box = document.createElement("div");
    box.style.cssText = "width:100%;font:10px/1.4 monospace;color:#9cf;background:#1a1a1a;padding:4px 6px;box-sizing:border-box;overflow:hidden;white-space:nowrap;";
    const w = node.addDOMWidget("player_meta", "div", box, { serialize: false });
    w.computeSize = () => {
        // Folded by the "Info" button -> no height at all, so the node gives the space back rather than leaving a
        // gap. Otherwise: undefined rows means nothing has rendered yet, and the full height is kept so a fresh
        // node does not visibly grow on its first render; zero rows (no clip loaded) collapses it away too.
        if (node._ocioMetaCollapsed) return [0, 0];
        const n = node._ocioMetaRows === undefined ? PLAYER_META_ROWS.length : node._ocioMetaRows;
        return [0, n > 0 ? 16 * n + 8 : 0];
    };
    w._ocioAlwaysVisible = true;
    node._ocioPlayerMeta = box;
    return box;
}
function renderPlayerMeta(node, data) {
    const box = ensurePlayerMeta(node);
    if (!data) { box.innerHTML = ""; box.title = ""; _metaRelayout(node, 0); return; }
    const framesTxt = data.cached < data.total ? `${data.total} (viewer capped at ${data.cached})` : String(data.total);
    const pp = node._ocioPlayer;
    const base = (pp && pp.videoMode && pp.videoBase) ? (pp.videoBase | 0)        // streamed video: 1-based (or the upstream Read's numbering)
               : ((pp && pp.player && pp.player.base) ? (pp.player.base | 0) : 0);   // float batch: mirrored from the upstream OCIO Read
    const rangeTxt = `${base}-${base + Math.max(1, data.cached || 1) - 1}`;
    // No dashes here any more: a missing value stays missing and _metaHasValue drops its whole row below.
    const values = {
        resolution: data.resolution,
        frames: framesTxt,
        range: rangeTxt,
        fps: data.fps ? data.fps.toFixed(3) : (parseFloat(W(node, "fps")?.value) || 0).toFixed(3),
        input_colorspace: W(node, "input_colorspace")?.value || data.input_cs,
        output_colorspace: W(node, "output_colorspace")?.value,
        presentation: (presentationLine() || {}).text,
        range_note: data.range_note,
    };
    // ESCAPED, because these values are not all ours. `input_colorspace` and `output_colorspace` are read from
    // widgets, and a widget value arrives from the workflow JSON - which on a shared graph is a stranger's file, not
    // a combo the front end filled in. Interpolating that straight into innerHTML made a hand-written workflow able
    // to inject markup into the node panel. Pre-existing rather than introduced here, and one function away from
    // being closed, so it is closed.
    const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const shown = PLAYER_META_ROWS.filter(([k]) => _metaHasValue(values[k]));
    box.innerHTML = shown.map(([k, label]) => `<div>${esc(label)}: ${esc(values[k])}</div>`).join("");
    _metaRelayout(node, shown.length);
    // The hover carries the reason, which is far too long for one monospace row inside a node. A no-WebGL2 viewport
    // returns null from presentationLine and gets no tooltip, because in that state the viewport is hidden behind
    // its own "WebGL2 unavailable" message and there is no picture to describe.
    const pres = presentationLine();
    box.title = pres ? (pres.detail || "") : "";
}

// OCIO Write "Render" button: (1) overwrite guard - ask the server which output files this
// Write would create and which already exist; if any exist, confirm before overwriting (Cancel aborts). (2) bump the
// hidden render_nonce so ComfyUI does NOT cache an identical Write - a repeat render to the SAME path actually
// re-writes (the reported bug: 2nd click / after deleting the file wrote nothing). window.confirm = the standard
// standard Overwrite / Cancel dialog.
async function ocioWriteRender(node) {
    try {
        const params = {
            output_folder: W(node, "output_folder")?.value || "", filename: W(node, "filename")?.value || "",
            container: W(node, "container")?.value, still_format: W(node, "still_format")?.value,
            video_codec: W(node, "video_codec")?.value, output_colorspace: W(node, "output_colorspace")?.value,
            raw_data: W(node, "raw_data")?.value ? 1 : 0, colorspace_in_name: W(node, "colorspace_in_name")?.value ? 1 : 0,
            start_number: parseInt(W(node, "start_number")?.value, 10) || 1,
        };
        // A still image gets its source frame number stamped into the name, and the BACKEND'S RULE IS THE BATCH
        // SIZE, not the presence of an OCIO Read: io_nodes.py passes `still_frame=(first_frame if n > 1 else
        // None)`. This asked findUpstreamRead instead, so a multi-frame batch arriving from anything else - a
        // generation wired straight into Write, which is the ordinary LTX case - predicted `shot.png` while the
        // write produced `shot.0005.png`. The dialog then probed a file that never exists, so it NEVER warned and
        // a repeat render silently overwrote the real one. Reproduced live: predicted `mismatch_test.png`, wrote
        // `mismatch_test.0005.png`.
        //
        // The front end cannot know the batch size - only the graph run does - so when an upstream Read does not
        // settle it, BOTH candidate names are probed and either one existing counts as a conflict. Over-warning
        // costs one dialog; under-warning costs the artist's previous render.
        const probes = [params];
        if (params.container === "still image") {
            const _r = findUpstreamRead(node), _k = _r && _r._ocioSeq && _r._ocioSeq.kind;
            const _sf = parseInt(W(node, "first_frame")?.value, 10);
            if (_k === "sequence" || _k === "video") {
                params.still_frame = _sf;                    // settled: it IS a grab from a sequence
            } else if (Number.isFinite(_sf)) {
                probes.push(Object.assign({}, params, { still_frame: _sf }));   // unsettled: probe both
            }
        }
        const seen = new Set();
        for (const body of probes) {
            const rp = await fetch("/ocio/write_paths", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
            const dp = await rp.json();
            for (const p of ((dp && Array.isArray(dp.existing)) ? dp.existing : [])) seen.add(String(p));
        }
        const d = { existing: Array.from(seen) };
        if (d.existing.length) {
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
        // Run / Queue feedback: show the "Processing…" spinner on an OCIO Read/Player
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
        // Runs for the color/grade nodes too, which have no other front-end onNodeCreated.
        if (nodeData.category === "OCIO" || String(nodeData.name || "").startsWith("OCIO")) {
            const _ocLabel = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const rr = _ocLabel ? _ocLabel.apply(this, arguments) : undefined;
                const relabel = () => {
                    // The IMAGE socket is our sequence path, the VIDEO socket is ComfyUI's native
                    // video pipeline. Name them so on EVERY OCIO node - input and output, both sides. Labels ONLY;
                    // slot names are untouched so connections/saved graphs resolve. Covers OCIO Read's VIDEO output
                    // and OCIO Player's VIDEO input renames too (they are just VIDEO sockets on OCIO nodes).
                    for (const s of (this.inputs || [])) if (s.type === "IMAGE" && (s.name === "image" || s.name === "images")) s.label = "OCIO Img/Seq/Vid";
                    for (const s of (this.inputs || [])) if (s.type === "VIDEO") s.label = "ComfyUI Video";
                    for (const s of (this.outputs || [])) if (s.type === "IMAGE") s.label = "OCIO Img/Seq/Vid";
                    for (const s of (this.outputs || [])) if (s.type === "VIDEO") s.label = "ComfyUI Video";
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
                // Belongs to the source PARAMETERS above it, so it sits directly under them: it re-reads range /
                // fps / colorspace from the file, which is exactly what those widgets hold. Detected defaults are
                // applied on a source CHANGE only, so this is the deliberate way to pull them back after an edit
                // (2026-08-09, issue #3).
                const detectBtn = this.addWidget("button", "Detect from Source", null, () => {
                    fillRange(this, W(this, "source")?.value);
                    updateReadMeta(this);
                }, { serialize: false });
                detectBtn._ocioAlwaysVisible = true;
                // spacer: separates the source section above from the file / viewer controls below
                const gapEl = document.createElement("div");
                gapEl.style.cssText = "height:100%;pointer-events:none;";
                const gapW = this.addDOMWidget("gap", "div", gapEl, { serialize: false });
                gapW.computeSize = () => [0, 10];
                gapW._ocioAlwaysVisible = true;
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
                // metadata block away and back. Default expanded; runtime-only, not serialized.
                const self = this;
                const viewerToggle = this.addWidget("button", "▾ Viewer", null, () => {
                    const c = self._ocioReadCollapsed = !self._ocioReadCollapsed;
                    _setWidgetLabel(viewerToggle, (c ? "▸" : "▾") + " Viewer");   // same chevron fix as the Player's Info toggle
                    const p = self._ocioPrev;
                    if (p && p.box) p.box.style.display = c ? "none" : "flex";
                    if (p && p.transport) p.transport.bar.style.display = c ? "none" : ((p.pb && p.pb.showTransport) ? "flex" : "none");
                    self.setSize([self.size[0], self.computeSize()[1]]);
                    self.setDirtyCanvas(true, true);
                }, { serialize: false });
                viewerToggle._ocioAlwaysVisible = true;
                ensureReadPreview(this);                                          // instant preview at the bottom
                // Metadata: its OWN disclosure, below the viewer, built to match it - same chevron, down when
                // open and right when closed. It used to fold away with the Viewer, which tied "I want to see
                // the picture" to "I want to read the header"; they are separate questions, and the header is
                // the one an artist checks before delivering. What it lists is whatever the file actually
                // carries, timecode included - which is where the timecode lives now that OCIO Write has no
                // field for one. Runtime-only, not serialized, exactly like the Viewer toggle.
                const metaToggle = this.addWidget("button", "▾ Metadata", null, () => {
                    const c = self._ocioReadMetaCollapsed = !self._ocioReadMetaCollapsed;
                    _setWidgetLabel(metaToggle, (c ? "▸" : "▾") + " Metadata");
                    if (self._ocioMeta) self._ocioMeta.style.display = c ? "none" : "";
                    self.setSize([self.size[0], self.computeSize()[1]]);
                    self.setDirtyCanvas(true, true);
                }, { serialize: false });
                metaToggle._ocioAlwaysVisible = true;
                ensureReadMeta(this);                                             // the panel itself, under its button
                this._ocioAllWidgets = this.widgets.slice();                      // full ordered list, captured once
                onChange(this, "source", (v) => { setW(this, "input_colorspace", autoInCs(v)); fillRange(this, v); updateReadMeta(this); });   // fillRange calls updateReadPreview once _ocioSeq is known
                for (const w of ["input_colorspace", "output_colorspace", "raw_data"]) {
                    onChange(this, w, () => updateReadPreview(this));  // colorspace change -> re-render the thumb
                }
                for (const w of ["frame_shift", "fps", "start_frame", "end_frame"]) {
                    onChange(this, w, () => resyncAllWrites());   // Read range/shift/fps -> downstream Writes
                }
                // Remember which auto-filled fields the artist edited, so a detect still in flight overwrites
                // only the others (issue #3). The auto-fill itself writes through setWSilent, which fires no
                // callback; a code-driven setW marks itself via _ocioAutoWrite. What reaches here is a real edit.
                for (const w of ["frame_mode", "input_colorspace", "start_frame", "end_frame", "frame_shift", "fps"]) {
                    onChange(this, w, () => {
                        if (this._ocioAutoWrite) return;
                        (this._ocioEdited || (this._ocioEdited = new Set())).add(w);
                    });
                }
                const node = this;
                // A node born from a LOADED WORKFLOW (or a paste, or a recreate) also runs onNodeCreated, and by
                // the time this timer fires configure() has already restored `source` - so writing detected
                // defaults here overwrote the saved values. A genuinely new node has an empty source and
                // fillRange returns early anyway, so nothing is lost by never applying values here (issue #3).
                setTimeout(() => {                                                // detect kind / visibility only
                    fillRange(node, W(node, "source")?.value, { applyValues: false });
                    updateReadPreview(node);
                    updateReadMeta(node);
                }, 0);
                return r;
            };
            const onConfig = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function () {
                const r = onConfig ? onConfig.apply(this, arguments) : undefined;
                const node = this;
                setTimeout(() => {                                                // loaded workflow: re-detect the KIND only
                    fillRange(node, W(node, "source")?.value, { applyValues: false });   // saved values win (issue #3)
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
                    // A `start_timecode` field used to be shown here for EXR and video only. It is gone: the start
                    // now arrives with the plate through the `metadata` wire, so there is no field to reveal or
                    // hide. Where the code can actually land is unchanged - an EXR header attribute and a movie's
                    // timecode track - and the writer still simply omits it for PNG / JPEG / TIFF.
                    const ff = W(node, "first_frame");                         // relabel the shared field
                    if (ff) {
                        ff.label = isStill ? "frame to save" : "first_frame";
                        ff.tooltip = isStill
                            ? "which single frame to write, default 1"
                            : "first frame number to write (auto-filled from the source when auto_range is ON)";
                    }
                    applyCodecLabel();
                    applyFormat();
                    applyAudioVis(node);                                        // sound is a movie's question only
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
                // RESPONSIBLE FOR: keeping a user-set output_colorspace (and the profile it would reset) alive
                // across a workflow load (2026-08-10). applyContainer only rebuilds the VIEW - visibility, labels,
                // sizing - because it also runs from the onNodeCreated timer, which fires after configure() has
                // restored the saved values. The default colorspace is written here instead: on a real container
                // change, which is a deliberate act. Same split as OCIO Read, issue #3.
                onChange(this, "container", () => {
                    applyContainer();
                    applyCsNarrowing(node);
                    setW(node, "output_colorspace", autoOutCs(W(node, "container")?.value, W(node, "still_format")?.value));
                });
                onChange(this, "still_format", () => {
                    applyFormat();
                    applyCsNarrowing(node);
                    applyCompressionVis();
                    setW(node, "output_colorspace", autoOutCs(W(node, "container")?.value, W(node, "still_format")?.value));
                    pokeWidgets(node);
                });
                onChange(this, "video_codec", () => { applyCsNarrowing(node); applyCodecLabel(); pokeWidgets(node); node.setDirtyCanvas(true, true); });
                // THE `view` WIDGET FILLS ITSELF IN. Whether a scene/display crossing should be rendered or
                // merely re-encoded is a question only these two widgets can answer, so it is answered here,
                // on a real change of either. applyAutoView refuses to act once the artist has touched `view`
                // by hand, and is never called from onNodeCreated - which fires after a saved graph's values
                // are restored, and would otherwise rewrite somebody's finished setup on open.
                // Narrow FIRST, then auto-fill: the display's own default view is always on the narrowed list,
                // so the order is safe either way, but doing it in this one keeps the widget from flashing a
                // list the pair cannot use.
                for (const w of ["input_colorspace", "output_colorspace"]) {
                    onChange(this, w, async () => {
                        await applyViewNarrowing(node);
                        applyAutoView(node);
                        applyCsNarrowing(node);
                    });
                }
                // and a hand-set view is the artist's from then on, same contract as auto_range
                onChange(this, "view", () => {
                    (node._ocioEdited || (node._ocioEdited = new Set())).add("view");
                });
                // auto frame range / fps from the upstream OCIO Read
                onChange(this, "auto_range", (v) => { if (v) syncWriteFromUpstream(node); });
                // fps belongs here too: auto_range pulls it from the Read like the others, and its tooltip has
                // always promised that editing it by hand turns auto OFF - it did not, so a hand-set fps (a
                // conform to 25 against a 23.976 source) was silently pulled back on the next sync (2026-08-10).
                for (const w of ["last_frame", "start_number", "fps"]) {
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
                for (const w of ["input_colorspace", "output_colorspace"]) {
                    onChange(this, w, () => {
                        if (node._ocioProfileSetting) return;               // our own silent write, not a user edit
                        const pw = W(node, "profile");
                        if (pw && pw.value !== "none") { pw.value = "none"; node.setDirtyCanvas(true, true); }
                    });
                }
                showWidget(this, W(this, "render_nonce"), false);   // internal cache-buster - hidden with a true collapse (no blank row)
                this.addWidget("button", "Output Folder", null, () => openFolderDialog(this), { serialize: false });
                // ▶ Render is the LAST widget on this node, deliberately, and it is the one an artist reaches
                // for most: anything added below it pushes the button down the node. There is no Viewer toggle
                // beneath it because there is nothing to view - OCIO Write has no preview of its own (see the
                // note at the top of this file).
                this.addWidget("button", "▶ Render", null, () => ocioWriteRender(this), { serialize: false });
                // applyViewNarrowing runs here too, and unlike applyAutoView that is safe on a loaded graph:
                // it only ever changes which choices the list OFFERS, never the value. So an artist opening a
                // saved workflow sees a `view` list the pair can actually be rendered through, and their own
                // pick - valid or not - is still exactly what they left.
                setTimeout(() => {
                    applyContainer(); syncWriteFromUpstream(node); resolveAutoProfile(node);
                    applyViewNarrowing(node);
                }, 0);
                return r;
            };
            const onConn = nodeType.prototype.onConnectionsChange;
            nodeType.prototype.onConnectionsChange = function () {
                const r = onConn ? onConn.apply(this, arguments) : undefined;
                const node = this;
                // applyAudioVis belongs here as well as in applyContainer: connecting or pulling the `audio`
                // or `video` wire changes whether there is any sound to decline, and no container change
                // accompanies it.
                setTimeout(() => { syncWriteFromUpstream(node); resolveAutoProfile(node); applyAudioVis(node); }, 0);   // wire (re)connected -> pull range/fps + auto profile
                return r;
            };
            const onConfigW = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function () {
                const r = onConfigW ? onConfigW.apply(this, arguments) : undefined;
                const node = this;
                // A GRAPH SAVED BEFORE write_audio EXISTED LOADS IT AS null, AND null STRIPS THE SOUND.
                // widgets_values is positional over ALL widgets, and this pack's two BUTTONS ("Output Folder",
                // "▶ Render") are widgets too, serialised as null. write_audio was appended after what was then
                // the last field and therefore landed exactly where the first button's null used to sit, so an
                // old 23-value
                // graph loads write_audio = null - falsy, and the write then drops the audio it used to keep.
                // Reproduced in the canvas: every other value survived (filename, fps 25, timecode 02:00:00:00)
                // and only this one came back null. "A missing optional input falls through to the Python
                // default" is true and does not help here, because the value is not missing - it is present
                // and null. Repaired on load, and again in write() for a prompt posted straight to the API.
                const wa = W(node, "write_audio");
                if (wa && (wa.value === null || wa.value === undefined)) wa.value = true;
                setTimeout(() => { syncWriteFromUpstream(node); resolveAutoProfile(node); }, 0);   // loaded workflow -> re-detect
                return r;
            };
            const onDraw = nodeType.prototype.onDrawForeground;
            nodeType.prototype.onDrawForeground = function (ctx) {
                onDraw && onDraw.apply(this, arguments);
                if (this.flags && this.flags.collapsed) return;
                const a = W(this, "input_colorspace"), b = W(this, "output_colorspace");
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
                // Audio verdict from the run (2026-08-12): whether a wired track was muxed, shipped as a sidecar,
                // or ignored. Silence about sound is how a silent master ships unnoticed.
                if (this._ocioAudio) {
                    ctx.fillStyle = "#dc8"; ctx.textAlign = "right"; ctx.font = "9px sans-serif";
                    ctx.fillText(this._ocioAudio, this.size[0] - 8, this.size[1] - 18);
                }
                // Metadata verdict: which colour attributes were authored, the start timecode, and - the part that
                // must not be silent - anything DROPPED because a colour transform would have made it false.
                if (this._ocioMeta) {
                    ctx.fillStyle = "#9ab"; ctx.textAlign = "right"; ctx.font = "9px sans-serif";
                    ctx.fillText(this._ocioMeta.slice(0, 120), this.size[0] - 8, this.size[1] - 30);
                }
                ctx.restore();
            };
            const onExec = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function (message) {
                onExec && onExec.apply(this, arguments);
                const c = message && message.count;
                const au = message && message.audio;
                const mt = message && message.meta;
                this._ocioAudio = au ? (Array.isArray(au) ? au[0] : au) : null;
                this._ocioMeta = mt ? (Array.isArray(mt) ? mt[0] : mt) : null;
                // Nothing here draws a picture. What OCIO Write reports after a run is text: the frame count,
                // the colour transform, the metadata verdict and the audio verdict, as corner text on the
                // canvas and as toasts on Vue frontends where that corner text is never drawn.
                if (c) {
                    this._ocioWrote = Array.isArray(c) ? c[0] : c; this.setDirtyCanvas(true, true);
                    // Vue frontends do not draw the canvas "wrote N" corner text; a toast carries the count there
                    app.extensionManager?.toast?.add?.({ severity: "success", summary: "OCIO Write",
                        detail: `wrote ${this._ocioWrote} frame(s)` + (this._ocioAudio ? `, ${this._ocioAudio}` : ""),
                        life: 4000 });
                    // A dropped pixel-state claim is a delivery fact, not a detail: the canvas corner text does not
                    // draw on Vue frontends at all, so it gets its own toast there.
                    // "clipped" joins "dropped" here (2026-08-13): an integer container that ate the below-black
                    // and the highlights is a delivery fact of exactly the same weight, and on a Vue frontend
                    // the corner text is never drawn, so without this toast the artist is told nothing at all.
                    if (this._ocioMeta && /dropped|clipped/.test(this._ocioMeta)) {
                        app.extensionManager?.toast?.add?.({ severity: "warn", summary: "OCIO Write metadata",
                            detail: this._ocioMeta, life: 8000 });
                    }
                } else if (this._ocioAudio || this._ocioMeta) {
                    this.setDirtyCanvas(true, true);
                }
            };
        }

        // A COMBO VALUE THAT NO LONGER EXISTS MAKES A SAVED GRAPH UNRUNNABLE, AND IT LOOKS FINE UNTIL RUN.
        // `precision` on both VAE nodes offered "model default" until 2026-08-13 and now offers only float32 and
        // float16. Measured in the canvas WITH A CONTROL, so the cause is attributed rather than assumed: the same
        // graph carrying "float32" queues with HTTP 200 and no errors, while the one carrying "model default" is
        // refused with HTTP 400 and the server names exactly one reason, `precision: 'model default' not in
        // ['float32', 'float16']`. The widget meanwhile DISPLAYS "model default" - a value it does not offer - so
        // the artist sees something that reads as set and gets a rejection with no hint of what to change.
        //
        // Repaired here rather than silently: the value is snapped to the node's own default so the graph RUNS,
        // and a warning toast names what changed and what it costs, because the old value meant the model's own
        // dtype and the new default is float32, which is measured at about 5x the decode time. Snapping without
        // saying so would hand someone a 5x slower render and no reason for it.
        if (nodeData.name === "OCIOVAEDecode" || nodeData.name === "OCIOVAEEncode") {
            const onConfigV = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function () {
                const r = onConfigV ? onConfigV.apply(this, arguments) : undefined;
                const w = W(this, "precision");
                const offered = w && w.options && Array.isArray(w.options.values) ? w.options.values : null;
                if (w && offered && offered.length && !offered.includes(w.value)) {
                    const was = w.value;
                    w.value = offered.includes("float32") ? "float32" : offered[0];
                    this.setDirtyCanvas(true, true);
                    app.extensionManager?.toast?.add?.({
                        severity: "warn", summary: nodeData.name.replace("OCIO", "OCIO "),
                        detail: `This graph asked for precision "${was}", which this node no longer offers. `
                              + `Set to "${w.value}" so the graph can run. "${was}" meant the model's own dtype; `
                              + `float32 is about 5x the decode time. Pick float16 to fall back to the model's `
                              + `dtype where it does not offer float16.`,
                        life: 12000 });
                }
                return r;
            };
        }

        if (nodeData.name === "OCIOPlayer") {
            const onCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const r = onCreated ? onCreated.apply(this, arguments) : undefined;
                ensurePlayer(this);                                           // float WebGL viewport + exposure slider
                // Fold the info panel away. It answers "what am I looking at" once, and after that it is seven
                // lines of the node spent on an answer you already have. Built as the SAME disclosure button
                // OCIO Read uses for its Viewer (a button widget with serialize:false, label flipping between the
                // two chevrons, runtime-only) rather than a second style of control - Read folds its own metadata
                // away together with its viewer, so the Player was the one node with no way to do it.
                // Created BEFORE the panel, so it sits ABOVE the rows it controls: the header stays put while the
                // block under it opens and closes, instead of the control moving up the node as the rows vanish.
                const selfP = this;
                const infoToggle = this.addWidget("button", "▾ Info", null, () => {
                    const c = selfP._ocioMetaCollapsed = !selfP._ocioMetaCollapsed;
                    _setWidgetLabel(infoToggle, (c ? "▸" : "▾") + " Info");
                    if (selfP._ocioPlayerMeta) selfP._ocioPlayerMeta.style.display = c ? "none" : "";
                    selfP.setSize([selfP.size[0], selfP.computeSize()[1]]);
                    selfP.setDirtyCanvas(true, true);
                }, { serialize: false });
                infoToggle._ocioAlwaysVisible = true;
                ensurePlayerMeta(this);                                       // metadata panel, under the toggle
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
                _setVideoOutput(this, false);                        // hide the VIDEO output until a video is rendered (playerOnExecuted re-adds it)
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
            // colorspace label (input -> output) in the title bar, same as OCIO Read, plus one line saying what
            // the viewport is actually presenting. See presentationCaps: users ask whether they are looking at
            // HDR or at 8 bits, and the honest answer is worth more than a guess either way.
            const onDraw = nodeType.prototype.onDrawForeground;
            nodeType.prototype.onDrawForeground = function (ctx) {
                onDraw && onDraw.apply(this, arguments);
                if (this.flags && this.flags.collapsed) return;
                const a = W(this, "input_colorspace"), b = W(this, "output_colorspace");
                if (!a || !b) return;
                ctx.save();
                ctx.font = "10px sans-serif"; ctx.fillStyle = "#9cf"; ctx.textAlign = "right";
                ctx.fillText(`${shorten(a.value)} → ${shorten(b.value)}`, this.size[0] - 8, -6);
                // The presentation line lives in the metadata panel (PLAYER_META_ROWS "Display"), NOT here. Two
                // reasons, both measured: the Vue node renderer never calls onDrawForeground at all, so a corner
                // string reaches nobody on that frontend; and this corner sits at size[1]-6, which is inside the
                // metadata panel's own rows, so on the legacy canvas the same sentence was drawn on top of the
                // panel that now carries it. The title-bar colorspace label above stays, because it sits at y=-6,
                // outside the node body, and it is the one thing readable when the node is small.
                ctx.restore();
            };
        }
    },
});
