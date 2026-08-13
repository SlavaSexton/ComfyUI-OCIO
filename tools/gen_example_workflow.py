"""Regenerate `example_workflows/OCIO_Nodes.json`, the example that SHIPS.

The one in the repository was written when the pack had nine nodes and fewer widgets, and `widgets_values` is
positional: two nodes had since grown a widget, so every value after it sat one place out. `OCIOWrite` opened with
`profile: "sRGB - Display"`, `container: "exr"`, `raw_data: "ocio_out"`, and `OCIOLogConvert` still carried the old
lowercase combo values. Nine combos held values absent from their own lists and the server refused the graph.

Design rules for a SHIPPED example, learned from that:

1. No widget value is typed here. Every node starts from its own declared defaults, read from the node classes
   themselves, and only the fields the example means to demonstrate are overridden - each override checked against
   the live combo before it is written. A widget that gains a neighbour cannot shift anything.
2. It must run for someone who just cloned the pack. So: only assets that ship with it (`nyc_skyline.png`,
   `warm_demo.cube`), and NO model file - the two VAE nodes need a VAE checkpoint that most people will not have,
   and an example that cannot run is the defect being fixed here. They are documented in docs/NODES_VAE.md and
   demonstrated by a separate graph for anyone with an LTX VAE.
3. Sockets that are optional stay unwired rather than being wired to something invented.
"""
import json
import os
import pathlib
import urllib.request

HOST = os.environ.get("COMFY_HOST", "http://127.0.0.1:8188")
# Derived from this file's own location, never from where the shell happens to be: the same reason no
# absolute path is written anywhere in this repository.
PACK = pathlib.Path(os.environ.get("PACK_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
OUT = PACK / "example_workflows" / "OCIO_Nodes.json"

_info, _OURS = {}, {}


def _load_pack():
    import importlib.util
    import sys
    import tempfile
    import types
    sys.path.insert(0, str(PACK))
    tmp = tempfile.mkdtemp()
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = fp.get_temp_directory = fp.get_input_directory = lambda: tmp
    fp.get_filename_list = lambda *a, **k: []
    sys.modules.setdefault("folder_paths", fp)
    pkg = types.ModuleType("p"); pkg.__path__ = [str(PACK)]; sys.modules["p"] = pkg
    for mod in ("nodes", "io_nodes", "vae_nodes"):
        spec = importlib.util.spec_from_file_location(f"p.{mod}", str(PACK / f"{mod}.py"))
        m = importlib.util.module_from_spec(spec)
        sys.modules[f"p.{mod}"] = m
        spec.loader.exec_module(m)
        for name, cls in getattr(m, "NODE_CLASS_MAPPINGS", {}).items():
            _OURS[name] = cls


# The one stock node this example uses, declared here rather than fetched. Generating the SHIPPED example must
# not require a running ComfyUI: the server holds Python from startup, so after a rename it answers with the old
# names, and it may simply be down. PreviewImage's shape has been one IMAGE input and no outputs for years; it is
# cross-checked against /object_info by tools/test_example_workflows.py whenever a server is reachable.
_BUILTIN = {
    "PreviewImage": {"input": {"required": {"images": ("IMAGE",)}}, "output": [], "output_name": []},
}


def info(cls):
    if cls in _info:
        return _info[cls]
    if cls in _BUILTIN:
        _info[cls] = _BUILTIN[cls]
        return _info[cls]
    if cls in _OURS:
        c = _OURS[cls]
        _info[cls] = {"input": c.INPUT_TYPES(), "output": list(getattr(c, "RETURN_TYPES", ())),
                      "output_name": list(getattr(c, "RETURN_NAMES", getattr(c, "RETURN_TYPES", ())))}
        return _info[cls]
    with urllib.request.urlopen(f"{HOST}/object_info/{cls}", timeout=30) as r:
        d = json.load(r)
    if cls not in d:
        raise SystemExit(f"the server does not know the node {cls}")
    _info[cls] = d[cls]
    return _info[cls]


class G:
    def __init__(self):
        self.nodes, self.links, self.groups = [], [], []
        self._nid = self._lid = 0

    def add(self, cls, pos, title=None, **over):
        n = info(cls)
        req = n["input"].get("required", {}) or {}
        opt = n["input"].get("optional", {}) or {}
        self._nid += 1
        widgets, inputs = [], []
        for name, spec in list(req.items()) + list(opt.items()):
            t = spec[0]
            o = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if isinstance(t, list):
                val = over.get(name, o.get("default", t[0] if t else ""))
                if t and val not in t:
                    raise SystemExit(f"{cls}.{name}: {val!r} is not in {t[:6]}")
                widgets.append((name, val))
            elif o.get("forceInput"):
                inputs.append({"name": name, "type": t, "link": None,
                               **({"shape": 7} if name in opt else {})})
            elif t in ("INT", "FLOAT", "STRING", "BOOLEAN"):
                d = o.get("default", 0 if t in ("INT", "FLOAT") else ("" if t == "STRING" else False))
                widgets.append((name, over.get(name, d)))
            else:
                inputs.append({"name": name, "type": t, "link": None,
                               **({"shape": 7} if name in opt else {})})
        unknown = set(over) - {w[0] for w in widgets}
        if unknown:
            raise SystemExit(f"{cls}: {sorted(unknown)} are not widgets on this node")
        node = {"id": self._nid, "type": cls, "pos": list(pos), "size": [340, 130], "flags": {},
                "order": self._nid - 1, "mode": 0, "inputs": inputs,
                "outputs": [{"name": nm, "type": ty, "links": [], "slot_index": i}
                            for i, (nm, ty) in enumerate(zip(n.get("output_name") or [], n.get("output") or []))],
                "properties": {"Node name for S&R": cls}, "widgets_values": [w[1] for w in widgets]}
        if title:
            node["title"] = title
        self.nodes.append(node)
        return self._nid

    def link(self, src, slot, dst, inp):
        s = next(n for n in self.nodes if n["id"] == src)
        d = next(n for n in self.nodes if n["id"] == dst)
        di = next((i for i, x in enumerate(d["inputs"]) if x["name"] == inp), None)
        if di is None:
            raise SystemExit(f"{d['type']} has no input {inp!r}; it has {[x['name'] for x in d['inputs']]}")
        st, dt = s["outputs"][slot]["type"], d["inputs"][di]["type"]
        if st != dt and "*" not in (st, dt) and "," not in str(dt):
            raise SystemExit(f"type mismatch: {s['type']}.{slot} is {st}, {d['type']}.{inp} wants {dt}")
        self._lid += 1
        s["outputs"][slot]["links"].append(self._lid)
        d["inputs"][di]["link"] = self._lid
        self.links.append([self._lid, src, slot, dst, di, st])

    def group(self, title, ids, colour):
        ns = [n for n in self.nodes if n["id"] in ids]
        x0 = min(n["pos"][0] for n in ns) - 20
        y0 = min(n["pos"][1] for n in ns) - 60
        x1 = max(n["pos"][0] + 350 for n in ns) + 20
        y1 = max(n["pos"][1] + 430 for n in ns) + 20
        self.groups.append({"title": title, "bounding": [x0, y0, x1 - x0, y1 - y0], "color": colour,
                            "font_size": 24, "flags": {}})

    def out(self):
        return {"id": "ocio-nodes-example", "revision": 0, "last_node_id": self._nid, "last_link_id": self._lid,
                "nodes": self.nodes, "links": self.links, "groups": self.groups, "config": {}, "extra": {},
                "version": 0.4}


_load_pack()
g = G()
IN, WORK, VIEW = "sRGB - Display", "ACEScg", "sRGB - Display"

# The two assets that SHIP with the pack. Copy them into ComfyUI's input folder once and the graph runs as-is.
rd = g.add("OCIORead", (40, 140), title="OCIO Read - one file, a sequence, or a video",
           source="nyc_skyline.png", frame_mode="single", input_colorspace=IN, output_colorspace=WORK)
csn = g.add("OCIOColorSpace", (440, 140), title="OCIO ColorSpace - 55 spaces, any to any",
            in_colorspace=WORK, out_colorspace=WORK)
lg = g.add("OCIOLogConvert", (440, 420), title="OCIO LogConvert - ten camera curves",
           operation="Linear to Log", curve="ACEScct")
lg2 = g.add("OCIOLogConvert", (440, 700), title="OCIO LogConvert - and back to linear",
            operation="Log to Linear", curve="ACEScct")
cdl = g.add("OCIOCDLTransform", (840, 140), title="OCIO CDLTransform - ASC CDL",
            slope_r=1.05, slope_g=1.0, slope_b=0.95, saturation=1.1)
ft = g.add("OCIOFileTransform", (840, 480), title="OCIO FileTransform - a LUT file",
           file_path="warm_demo.cube")
lk = g.add("OCIOLookTransform", (840, 780), title="OCIO LookTransform - a look from the config",
           in_colorspace=WORK, out_colorspace=WORK)
dsp = g.add("OCIODisplay", (1240, 140), title="OCIO Display - the view transform ENDS the chain",
            in_colorspace=WORK, display=VIEW, view="ACES 2.0 - SDR 100 nits (Rec.709)")
pv = g.add("PreviewImage", (1640, 140), title="what the display transform produced")
ply = g.add("OCIOPlayer", (1240, 480), title="OCIO Player - float viewport, exposure in stops",
            input_colorspace=WORK, output_colorspace=VIEW)
wr = g.add("OCIOWrite", (1640, 480), title="OCIO Write - EXR 32f carries the whole range",
           profile="none", from_colorspace=WORK, output_colorspace=WORK, container="sequence",
           still_format="exr", bit_depth="32f", filename="ocio_example")

# LogConvert appears twice on purpose: encoding to a log space and decoding back is the round trip the pack is
# built on, and it is the shape the LUT step needs - a 3D LUT clamps to its own domain, so scene-linear data has
# to be in a log space before it reaches one.
g.link(rd, 0, csn, "image")
g.link(csn, 0, lg, "image")
g.link(lg, 0, ft, "image")
g.link(ft, 0, lg2, "image")
g.link(lg2, 0, cdl, "image")
g.link(cdl, 0, lk, "image")
g.link(lk, 0, dsp, "image")
g.link(dsp, 0, pv, "images")
g.link(cdl, 0, ply, "images")      # the viewport sees the GRADE, scene-linear, not the display-referred end
g.link(cdl, 0, wr, "images")       # and so does the writer: a master is scene-linear, not display-referred
g.link(rd, 5, wr, "metadata")      # the plate's header travels on its own wire; IMAGE carries none of it

g.group("Read", [rd], "#88A")
g.group("The six colour operators", [csn, lg, lg2, cdl, ft, lk, dsp], "#3f789e")
g.group("Look at it, and deliver it", [pv, ply, wr], "#8A8")

data = g.out()
OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
print(f"wrote {OUT.relative_to(PACK)}")
print(f"  nodes {len(data['nodes'])}  links {len(data['links'])}  groups {len(data['groups'])}")
ours = sorted({n["type"] for n in data["nodes"] if n["type"].startswith("OCIO")})
print(f"  pack nodes: {len(ours)} -> {ours}")
print("  no model file, no VAE: it runs on a fresh clone with the two assets that ship beside it")
