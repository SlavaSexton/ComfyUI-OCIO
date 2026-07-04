// ComfyUI-OCIO - a standardized one-press "swap" button.
// @author Slava Sexton
//
// One button on every OCIO node that has a natural pair. Press it and the pair is swapped instantly,
// the same way on every node (no checkbox to lose track of):
//   OCIOColorSpace, OCIOLookTransform -> swap in_colorspace <-> out_colorspace
//   OCIOLogConvert                    -> swap operation lin_to_log <-> log_to_lin
import { app } from "../../scripts/app.js";

const SWAPS = {
    OCIOColorSpace:   { kind: "pair",   a: "in_colorspace", b: "out_colorspace", label: "⇄ swap in/out" },
    OCIOLookTransform:{ kind: "pair",   a: "in_colorspace", b: "out_colorspace", label: "⇄ swap in/out" },
    OCIOLogConvert:   { kind: "toggle", a: "operation", on: "Linear to Log", off: "Log to Linear", label: "⇄ swap direction" },
};

function widget(node, name) {
    return (node.widgets || []).find((w) => w.name === name);
}

app.registerExtension({
    name: "ComfyUI-OCIO.swap",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const cfg = SWAPS[nodeData.name];
        if (!cfg) return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            this.addWidget("button", cfg.label, null, () => {
                if (cfg.kind === "pair") {
                    const a = widget(this, cfg.a);
                    const b = widget(this, cfg.b);
                    if (a && b) {
                        const t = a.value;
                        a.value = b.value;
                        b.value = t;
                        if (a.callback) a.callback(a.value);
                        if (b.callback) b.callback(b.value);
                    }
                } else {
                    const w = widget(this, cfg.a);
                    if (w) {
                        w.value = w.value === cfg.on ? cfg.off : cfg.on;
                        if (w.callback) w.callback(w.value);
                    }
                }
                this.setDirtyCanvas(true, true);
            }, { serialize: false });
            return r;
        };
    },
});
