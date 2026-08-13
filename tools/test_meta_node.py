"""Does OCIO Metadata write keys the rest of the pack actually reads, and does a correction really take?

The trap this node could fall into is writing a vocabulary of its own. `io_nodes._IDENTITY_FROM` maps each field
to the spellings every writer here understands, and `_first_meta` takes the FIRST spelling it finds - so setting
only the canonical key while a plate's `dpx:Shot` or `com.apple.proapps.shot` survives would leave the OLD value
winning downstream, and the correction would look applied while changing nothing. That case is asserted below.

Every check goes through the node's real entry point and then through `_identity_meta`, the same function the EXR,
TIFF, PNG, MXF and sidecar paths use to decide what to write. Agreeing with the node's own report is not enough:
the question is what a WRITER would see.
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
mods = {}
for mod in ("nodes", "io_nodes", "meta_nodes"):
    spec = importlib.util.spec_from_file_location(f"p.{mod}", os.path.join(ROOT, f"{mod}.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[f"p.{mod}"] = m
    spec.loader.exec_module(m)
    mods[mod] = m
io, meta = mods["io_nodes"], mods["meta_nodes"]

FAILS = []


def check(ok, what, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}" + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(what)


def run(**kw):
    r = meta.OCIOMetadata().run(**kw)
    text = r["result"][0]
    return json.loads(text), r["result"][1], text


print("the node's vocabulary must BE the pack's vocabulary, not a copy of it")
check(meta.CANONICAL == {f: io._IDENTITY_FROM[f][0] for f in meta.CANONICAL},
      "every canonical key comes from io_nodes._IDENTITY_FROM", str(meta.CANONICAL))
check(meta.CANONICAL["reel"] == "reel_name", "reel is written as reel_name (the MXF-structural field)",
      meta.CANONICAL.get("reel"))
check(meta.CANONICAL["camera"] == "cameraModel", "camera is written as cameraModel", meta.CANONICAL.get("camera"))
check(all(meta.SPELLINGS[f] == tuple(io._IDENTITY_FROM[f]) for f in meta.SPELLINGS),
      "every ALTERNATIVE spelling is taken from the same table too")
# The fallback exists for a standalone import and is a copy, so it is only safe while something compares them.
check(set(meta._FALLBACK_KEYS) == set(meta.CANONICAL) and
      all(meta._FALLBACK_KEYS[f] == meta.CANONICAL[f] for f in meta.CANONICAL),
      "the standalone fallback agrees with the real table")

print("\nauthoring from nothing: no wire in, fields out")
d, rep, _ = run(mode="merge", reel="A001R2B4", scene="12", shot="0106", take="3",
                camera="ARRI ALEXA 35", lens="Signature Prime 47mm")
ident = io._identity_meta(d["attrs"])
check(ident.get("reel") == "A001R2B4" and ident.get("shot") == "0106" and ident.get("take") == "3",
      "a writer reading the result sees every field that was typed", str(ident))
check(ident.get("camera") == "ARRI ALEXA 35" and ident.get("lens") == "Signature Prime 47mm",
      "including camera and lens", str(ident))
check(len(ident) == 6, "and exactly the six that were typed, nothing invented", str(sorted(ident)))

print("\nmerge: what was typed wins, the rest of the plate survives")
plate = json.dumps({"source": "LeftGirl.v01.086.exr", "kind": "exr",
                    "attrs": {"nuke/version": "14.0v5", "timeCode": "(14, 48, 24, 22, 0, 0, 0, 0, 0, 0)",
                              "shot": "wrong_shot", "scene": "9"}})
d, rep, _ = run(mode="merge", shot="0106", metadata=plate)
check(d["attrs"].get("nuke/version") == "14.0v5", "an attribute nobody typed is still there")
check(d["attrs"].get("timeCode", "").startswith("(14, 48"), "the plate's own timeCode is untouched")
check(io._identity_meta(d["attrs"]).get("shot") == "0106", "the corrected field really changed",
      str(io._identity_meta(d["attrs"])))
check(io._identity_meta(d["attrs"]).get("scene") == "9", "a field left blank keeps the plate's value")
check(d.get("source") == "LeftGirl.v01.086.exr", "the source name travels through")

print("\nTHE TRAP: an alternative spelling must not survive a correction")
# _first_meta takes the FIRST spelling present, and dpx:Shot / com.apple.proapps.shot come BEFORE nothing but
# after `shot` in the table - so a stale one of those could out-rank the canonical key the node just set. This is
# the case that makes a correction look applied while changing nothing.
for stale_key in ("dpx:Shot", "com.apple.proapps.shot"):
    p = json.dumps({"attrs": {stale_key: "STALE", "shot": "also_stale"}})
    d, _, _ = run(mode="merge", shot="0106", metadata=p)
    got = io._identity_meta(d["attrs"]).get("shot")
    check(got == "0106", f"{stale_key} does not out-rank the corrected value", f"a writer would see {got!r}")
    check(stale_key not in d["attrs"], f"{stale_key} is removed rather than left to argue")

print("\nreplace: only what was typed")
d, _, _ = run(mode="replace", reel="A001R2B4", metadata=plate)
check(list(d["attrs"]) == ["reel_name"], "everything the plate carried is dropped", str(list(d["attrs"])))
check("nuke/version" not in d["attrs"], "including attributes that are not identity at all")

print("\npassthrough: nothing changes")
d, _, text = run(mode="passthrough", shot="ignored", metadata=plate)
check(d["attrs"] == json.loads(plate)["attrs"], "the attributes come out byte-for-byte as they went in")

print("\nand it must never take a render down")
for bad in ("not json at all", "[1,2,3]", "", "null", '{"attrs": "not a dict"}'):
    try:
        d, rep, _ = run(mode="merge", shot="0106", metadata=bad)
        ok = isinstance(d, dict) and io._identity_meta(d.get("attrs") or {}).get("shot") == "0106"
        check(ok, f"malformed input {bad[:18]!r} is reported and the typed field still lands", rep[:70])
    except Exception as e:
        check(False, f"malformed input {bad[:18]!r} did not raise", f"{type(e).__name__}: {e}")

print("\nthe report has to say what happened")
d, rep, _ = run(mode="merge", shot="0106", camera="ALEXA 35", metadata=plate)
check("shot=0106" in rep and "camera=ALEXA 35" in rep, "it names what was set", rep[:80])
check("identity:" in rep and "attribute(s) out" in rep, "and what a writer will see", rep[:120])

print("\nand the output must be exactly what OCIO Write accepts")
# Write parses its `metadata` input with json.loads and reads .get("attrs") - so the shape is the contract.
d, _, text = run(mode="merge", reel="A001R2B4", metadata=plate)
check(isinstance(json.loads(text), dict) and "attrs" in json.loads(text),
      "a JSON object carrying an 'attrs' mapping, the shape Write reads")
sig = io.OCIOWrite.INPUT_TYPES()
check("metadata" in (sig.get("optional") or {}), "and Write's socket is still called 'metadata'",
      str(list((sig.get("optional") or {}))))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + ", ".join(FAILS[:6]))
    sys.exit(1)
print("OCIO Metadata speaks the pack's own vocabulary, and a correction really reaches the writer")
