"""Do the workflows this pack SHIPS still match the nodes this pack ships?

RESPONSIBLE FOR: catching an example workflow that has rotted against the code (2026-08-13). `OCIO_Nodes.json` was
written when the pack had nine nodes and fewer widgets, and `widgets_values` is POSITIONAL - so when two nodes grew
a widget, every value after it sat one place out. The shipped example opened with `OCIOWrite.profile` holding
"sRGB - Display", `container` holding "exr", `raw_data` holding "ocio_out", and `OCIOLogConvert` still carrying the
old lowercase combo values "lin_to_log" and "cineon". Nine combos held values absent from their own lists, and the
server refused the graph outright:

    Value not in list: container: 'exr' not in ['still image', 'sequence', 'video']
    Failed to convert an input value to a INT value: source_start, invalid literal for int() with base 10: ''

So anyone who installed the pack and opened its own example got a graph that could not run, and nothing in the gate
said a word, because no test read the example at all. This one does.

Deliberately in-process: it reads the node classes from the source rather than a running server. A server holds
Python from startup, so it answers with whatever was loaded then - exactly the stale copy this check must not
trust. Types for the handful of stock ComfyUI nodes an example may use are declared below, and a name outside that
set is reported rather than silently accepted.
"""
import importlib.util
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp()
fp = types.ModuleType("folder_paths")
fp.get_output_directory = fp.get_temp_directory = fp.get_input_directory = lambda: TMP
fp.get_filename_list = lambda *a, **k: []
sys.modules.setdefault("folder_paths", fp)
pkg = types.ModuleType("p"); pkg.__path__ = [ROOT]; sys.modules["p"] = pkg

OURS = {}
for mod in ("nodes", "io_nodes", "vae_nodes"):
    spec = importlib.util.spec_from_file_location(f"p.{mod}", os.path.join(ROOT, f"{mod}.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[f"p.{mod}"] = m
    spec.loader.exec_module(m)
    OURS.update(getattr(m, "NODE_CLASS_MAPPINGS", {}))

# Stock nodes an example is allowed to use, with the shape needed to check a wire into them. Anything else in an
# example is reported: a shipped graph should not depend on a node whose contract nobody here has checked.
STOCK = {
    "PreviewImage": {"inputs": {"images": "IMAGE"}, "outputs": []},
    "PreviewAny": {"inputs": {"source": "*"}, "outputs": []},
    "LoadImage": {"inputs": {}, "outputs": ["IMAGE", "MASK"]},
    "VAELoader": {"inputs": {}, "outputs": ["VAE"]},
    "SaveImage": {"inputs": {"images": "IMAGE"}, "outputs": []},
}

FAILS = []


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILS.append(what)


def spec_of(cls):
    """(widget names in order, {widget: combo list or None}, {input: type}) for one node class."""
    if cls in OURS:
        it = OURS[cls].INPUT_TYPES()
        widgets, combos, sockets = [], {}, {}
        for sect in ("required", "optional"):
            for name, sp in (it.get(sect) or {}).items():
                t = sp[0]
                o = sp[1] if len(sp) > 1 and isinstance(sp[1], dict) else {}
                if isinstance(t, list):
                    widgets.append(name); combos[name] = list(t)
                elif o.get("forceInput"):
                    sockets[name] = t
                elif t in ("INT", "FLOAT", "STRING", "BOOLEAN"):
                    widgets.append(name); combos[name] = None
                else:
                    sockets[name] = t
        return widgets, combos, sockets
    if cls in STOCK:
        return None, {}, STOCK[cls]["inputs"]
    return "unknown", {}, {}


wf_dir = os.path.join(ROOT, "example_workflows")
files = sorted(f for f in os.listdir(wf_dir) if f.endswith(".json"))
check(bool(files), f"the pack ships at least one example workflow ({len(files)} found)")

for fname in files:
    path = os.path.join(wf_dir, fname)
    print(f"\n{fname}")
    with open(path, encoding="utf-8") as fh:
        wf = json.load(fh)
    nodes = wf.get("nodes") or []
    check(bool(nodes), f"{fname}: parses and holds nodes ({len(nodes)})")

    by_id = {n["id"]: n for n in nodes}
    unknown, bad_combo, bad_count = [], [], []
    for n in nodes:
        cls = n.get("type")
        widgets, combos, sockets = spec_of(cls)
        if widgets == "unknown":
            unknown.append(cls)
            continue
        if widgets is None:                      # a stock node: its widget order is not ours to police
            continue
        vals = n.get("widgets_values") or []
        # A BUTTON serialises as null and still occupies a position, so the stored list can be longer than the
        # widget list; it must never be SHORTER in the region we check, and each value must suit its widget.
        for i, wname in enumerate(widgets):
            if i >= len(vals):
                bad_count.append(f"{cls}.{wname} has no stored value (list holds {len(vals)} for {len(widgets)} widgets)")
                continue
            v = vals[i]
            allowed = combos.get(wname)
            if allowed and v is not None and v not in allowed:
                bad_combo.append(f"{cls}.{wname} = {v!r} is not in its own list {allowed[:4]}...")

    check(not unknown, f"{fname}: every node type is known" + (f" - UNKNOWN: {sorted(set(unknown))}" if unknown else ""))
    check(not bad_combo, f"{fname}: every combo value is one the node offers"
          + ("\n          " + "\n          ".join(bad_combo[:6]) if bad_combo else ""))
    check(not bad_count, f"{fname}: every widget has a stored value"
          + ("\n          " + "\n          ".join(bad_count[:6]) if bad_count else ""))

    # links: both ends must exist, and the types must match
    bad_link = []
    for l in (wf.get("links") or []):
        try:
            lid, src, sslot, dst, dslot, ltype = l[:6]
        except Exception:
            bad_link.append(f"malformed link entry {l!r}")
            continue
        if src not in by_id or dst not in by_id:
            bad_link.append(f"link {lid} points at a node that is not in the file")
            continue
        s, d = by_id[src], by_id[dst]
        outs = s.get("outputs") or []
        ins = d.get("inputs") or []
        if sslot >= len(outs):
            bad_link.append(f"link {lid}: {s['type']} has no output slot {sslot}")
            continue
        if dslot >= len(ins):
            bad_link.append(f"link {lid}: {d['type']} has no input slot {dslot}")
            continue
        st, dt = outs[sslot].get("type"), ins[dslot].get("type")
        if st != dt and "*" not in (st, dt):
            bad_link.append(f"link {lid}: {s['type']}.{outs[sslot].get('name')} is {st} into "
                            f"{d['type']}.{ins[dslot].get('name')} which wants {dt}")
    check(not bad_link, f"{fname}: every wire connects existing slots of matching type"
          + ("\n          " + "\n          ".join(bad_link[:6]) if bad_link else ""))

    # a REQUIRED socket left unwired cannot run, and shape 7 marks an optional one
    unwired = []
    for n in nodes:
        for i in (n.get("inputs") or []):
            if i.get("link") is None and i.get("shape") != 7 and i.get("widget") is None:
                unwired.append(f"{n['type']}.{i.get('name')}")
    check(not unwired, f"{fname}: no required socket is left unwired"
          + (f" - {unwired[:6]}" if unwired else ""))

    # every file the graph names must actually ship beside it
    missing = []
    for n in nodes:
        for v in (n.get("widgets_values") or []):
            if isinstance(v, str) and v.lower().endswith((".png", ".exr", ".cube", ".3dl", ".tif", ".tiff", ".mov")):
                if not os.path.isfile(os.path.join(wf_dir, os.path.basename(v))):
                    missing.append(f"{n['type']}: {v}")
    check(not missing, f"{fname}: every asset it names ships beside it"
          + (f" - MISSING {missing[:4]}" if missing else ""))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("the shipped example workflows match the shipped nodes")
