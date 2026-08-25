"""Does every node the pack registers appear everywhere a reader or a gate looks for it?

WHY THIS EXISTS. `OCIO Clip Repair` shipped in 1.3.1 and was in none of these places: not in
the README, not in the wiring map, not in any reference, not in the package description, and
not in the container smoke test's list of names to look for. The last one is the expensive
one: that list is how the smoke test decides the pack loaded, so a node missing from it can
fail to register while the gate stays green.

None of that is caught by testing behaviour, because each of those files is correct on its
own terms. It is caught by comparing them to one source of truth, which is what this does.

The checks:

  1. NODE_CLASS_MAPPINGS and NODE_DISPLAY_NAME_MAPPINGS cover exactly the same keys.
  2. The smoke test's OCIO_NODE_CLASSES equals NODE_CLASS_MAPPINGS, as a set.
  3. Every display name appears in README.md.
  4. Every display name appears in docs/NODES.md, the wiring map.
  5. Every display name appears in one of the docs/NODES_*.md group references.
  6. The count stated in README.md, docs/NODES.md and pyproject.toml matches how many there are.

Check 6 pins the number as a WORD, and the word is written out here rather than imported, so
that changing the count has to be done in both places deliberately instead of drifting.

Run:  python tools/test_node_inventory.py
"""
import importlib.util
import io
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fp = types.ModuleType("folder_paths")
fp.get_output_directory = fp.get_temp_directory = fp.get_input_directory = lambda: ROOT
fp.get_filename_list = lambda *a, **k: []
sys.modules["folder_paths"] = fp

spec = importlib.util.spec_from_file_location("ocio_pack", os.path.join(ROOT, "__init__.py"),
                                              submodule_search_locations=[ROOT])
pack = importlib.util.module_from_spec(spec)
sys.modules["ocio_pack"] = pack
spec.loader.exec_module(pack)

WORDS = {9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen",
         14: "fourteen", 15: "fifteen", 16: "sixteen"}

FAILED = []


def check(label, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  -> " + str(detail)) if detail else ""))
    if not ok:
        FAILED.append(label)


def read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def main():
    classes = set(pack.NODE_CLASS_MAPPINGS)
    names = pack.NODE_DISPLAY_NAME_MAPPINGS
    n = len(classes)

    check("class and display-name maps cover the same keys",
          classes == set(names), "%d classes, %d names" % (n, len(names)))

    smoke = read(os.path.join("docker", "comfyui_client.py"))
    m = re.search(r"OCIO_NODE_CLASSES\s*=\s*\[(.*?)\]", smoke, re.S)
    listed = set(re.findall(r'"(\w+)"', m.group(1))) if m else set()
    check("the smoke test looks for exactly the registered nodes",
          listed == classes,
          "missing from the smoke list: %s" % sorted(classes - listed) if listed != classes else "%d" % n)

    readme, wiring = read("README.md"), read(os.path.join("docs", "NODES.md"))
    groups = "".join(read(os.path.join("docs", f)) for f in os.listdir(os.path.join(ROOT, "docs"))
                     if f.startswith("NODES_") and f.endswith(".md"))

    for label, text in (("README.md", readme), ("docs/NODES.md", wiring),
                        ("a docs/NODES_*.md reference", groups)):
        missing = sorted(d for d in names.values() if d not in text)
        check("every node is named in %s" % label, not missing, missing or "all %d" % n)

    # A number only counts when it is counting nodes. The first version of this check matched
    # any number anywhere in the file and failed on bit depths and version numbers, which is a
    # probe reporting on itself rather than on the document.
    word = WORDS.get(n, str(n))
    vocab = "|".join(list(WORDS.values()) + [str(k) for k in WORDS])
    counter = re.compile(r"\b(%s)\s+(?:OCIO\s+)?nodes\b" % vocab, re.I)
    for rel, text in (("README.md", readme), ("docs/NODES.md", wiring),
                      ("pyproject.toml", read("pyproject.toml"))):
        stated = {f.lower() for f in counter.findall(text)}
        wrong = stated - {word, str(n)}
        check("%s counts the nodes as %s and nothing else" % (rel, word),
              word in stated and not wrong,
              "found %s" % sorted(stated) if (word not in stated or wrong) else word)

    print()
    if FAILED:
        print("FAILED: %d -> %s" % (len(FAILED), "; ".join(FAILED)))
        return 1
    print("PASS: all %d nodes are registered, listed and documented consistently" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
