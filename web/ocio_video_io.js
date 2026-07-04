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
                if (this.__ocioDualBusy) return r;                 // re-entrancy guard: our own disconnects re-fire this
                this.__ocioDualBusy = true;
                if (type === LG().INPUT && connected) {
                    const imgIn = inSlot(this, cfg.imgIn), vidIn = inSlot(this, cfg.vidIn);
                    if (index === imgIn) {                          // IMAGE connected -> drop the VIDEO input + the (now dead) VIDEO output
                        if (vidIn >= 0) this.disconnectInput(vidIn);
                        if (cfg.vidOut != null) this.disconnectOutput(cfg.vidOut);
                    } else if (index === vidIn) {                   // VIDEO connected -> drop the IMAGE input + the (now dead) IMAGE output
                        if (imgIn >= 0) this.disconnectInput(imgIn);
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
