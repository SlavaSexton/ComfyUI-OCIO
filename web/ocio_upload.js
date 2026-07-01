// ComfyUI-OCIO - "upload" buttons, like Load Image: pick a file from your machine, it is sent to the server's
// input folder, and the node's dropdown selects it. No manual copying into folders, no free-text path.
// @author Slava Sexton
import { app } from "../../scripts/app.js";

const UPLOAD = {
    OCIOColorSpace:    { widget: "config_path", accept: ".ocio", label: "upload .ocio config" },
    OCIODisplay:       { widget: "config_path", accept: ".ocio", label: "upload .ocio config" },
    OCIOLookTransform: { widget: "config_path", accept: ".ocio", label: "upload .ocio config" },
    OCIOFileTransform: { widget: "file_path",
                         accept: ".cube,.3dl,.spi1d,.spi3d,.csp,.ccc,.cdl,.clf,.lut",
                         label: "upload LUT file" },
    // OCIORead's upload (with image-sequence support) lives in ocio_io.js.
};

function widget(node, name) {
    return (node.widgets || []).find((w) => w.name === name);
}

app.registerExtension({
    name: "ComfyUI-OCIO.upload",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const cfg = UPLOAD[nodeData.name];
        if (!cfg) return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            const node = this;
            this.addWidget("button", cfg.label, null, () => {
                const inp = document.createElement("input");
                inp.type = "file";
                inp.accept = cfg.accept;
                inp.style.display = "none";
                document.body.appendChild(inp);
                inp.onchange = async () => {
                    if (!inp.files || !inp.files.length) { inp.remove(); return; }
                    const fd = new FormData();
                    fd.append("file", inp.files[0]);
                    try {
                        const resp = await fetch("/ocio/upload", { method: "POST", body: fd });
                        const data = await resp.json();
                        if (data && data.path) {
                            const w = widget(node, cfg.widget);
                            if (w) {
                                if (w.options && Array.isArray(w.options.values) && !w.options.values.includes(data.path)) {
                                    w.options.values.push(data.path);
                                }
                                w.value = data.path;
                                if (w.callback) w.callback(w.value);
                                node.setDirtyCanvas(true, true);
                            }
                        } else {
                            console.error("OCIO upload: unexpected response", data);
                        }
                    } catch (e) {
                        console.error("OCIO upload failed", e);
                    }
                    inp.remove();
                };
                inp.click();
            }, { serialize: false });
            return r;
        };
    },
});
