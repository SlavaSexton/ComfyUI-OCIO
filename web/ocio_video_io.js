// ComfyUI-OCIO - native ComfyUI VIDEO plumbing for the color nodes.
// @author Slava Sexton, 2026-07-04
//
// Each color node carries a mutually-exclusive IMAGE ("Image Sequence Video") vs VIDEO ("ComfyUI Video") input
// pair, and the output MIRRORS the active input. Connecting one input auto-disconnects the other AND the output
// that would then carry no data - so a node drops cleanly into ComfyUI's native video graph
// (Load Video -> OCIO color node -> Video Combine) or stays in an IMAGE-sequence graph, one or the other.
import { app } from "../../scripts/app.js";

// nodeType -> { image input name, video input name, IMAGE output index, VIDEO output index }.
// null out-index = that side is a relabel-only pair (Read = VIDEO output only; Player = VIDEO input only).
const DUAL = {
    OCIOColorSpace:    { imgIn: "image", vidIn: "video", imgOut: 0, vidOut: 1 },
    OCIOLogConvert:    { imgIn: "image", vidIn: "video", imgOut: 0, vidOut: 1 },
    OCIODisplay:       { imgIn: "image", vidIn: "video", imgOut: 0, vidOut: 1 },
    OCIOCDLTransform:  { imgIn: "image", vidIn: "video", imgOut: 0, vidOut: 1 },
    OCIOFileTransform: { imgIn: "image", vidIn: "video", imgOut: 0, vidOut: 1 },
    OCIOLookTransform: { imgIn: "image", vidIn: "video", imgOut: 0, vidOut: 1 },
    // Write (OUTPUT_NODE, path output) and Player (input-only viewer): two mutually-exclusive INPUTS, no output pair.
    OCIOWrite:         { imgIn: "images", vidIn: "video", imgOut: null, vidOut: null },
    OCIOPlayer:        { imgIn: "images", vidIn: "video", imgOut: null, vidOut: null },
};

const LG = () => window.LiteGraph;
const inSlot = (node, name) => (node.inputs || []).findIndex((s) => s && s.name === name);
// Slot LABELS ("Image Sequence Video" / "ComfyUI Video") are owned by ocio_io.js's uniform OCIO relabeler; this file
// only handles the mutually-exclusive wiring so the two do not fight over the label.

app.registerExtension({
    name: "ComfyUI-OCIO.videoio",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const cfg = DUAL[nodeData.name];
        if (!cfg) return;

        const _conn = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function (type, index, connected, link_info, ioSlot) {
            const r = _conn ? _conn.apply(this, arguments) : undefined;
            try {
                // CRITICAL (owner 2026-07-04): do NOT mutate links while a graph is LOADING. On a page reload ComfyUI
                // restores every link, firing onConnectionsChange for each; disconnecting mid-restore corrupted the
                // whole graph (all links vanished). Two guards: skip while app.configuringGraph is set, AND only ever
                // disconnect on a GENUINE conflict (the OTHER input already connected) - a saved graph never has both
                // (mutual exclusion prevents it), so a normal load triggers nothing here.
                if (app.configuringGraph || this.__ocioDualBusy) return r;
                if (type === LG().INPUT && connected) {
                    const imgIn = inSlot(this, cfg.imgIn), vidIn = inSlot(this, cfg.vidIn);
                    const isC = (s) => s >= 0 && this.inputs[s] && this.inputs[s].link != null;
                    this.__ocioDualBusy = true;
                    if (index === imgIn && isC(vidIn)) {            // IMAGE just connected while VIDEO is in -> drop VIDEO in + its dead output
                        this.disconnectInput(vidIn);
                        if (cfg.vidOut != null) this.disconnectOutput(cfg.vidOut);
                    } else if (index === vidIn && isC(imgIn)) {     // VIDEO just connected while IMAGE is in -> drop IMAGE in + its dead output
                        this.disconnectInput(imgIn);
                        if (cfg.imgOut != null) this.disconnectOutput(cfg.imgOut);
                    }
                }
            } finally {
                this.__ocioDualBusy = false;
            }
            return r;
        };
    },
});
