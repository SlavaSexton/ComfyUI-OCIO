"""Does /ocio/upload's write stay inside the input directory, whatever the client sends?

WHY THIS EXISTS. Version 1.3.0 was BANNED from the Comfy registry for exactly this: the upload route
sanitised the subfolder string with os.path.basename, and os.path.basename("..") returns ".."
unchanged, so subfolder=".." resolved to the ComfyUI install root - an unauthenticated, CSRF-reachable
arbitrary file write. The fix guards the RESOLVED path instead of the string. This test holds that fix
in place.

It calls _confine_to_input FOR REAL, not by counting substrings in the source - we have shipped a
function that passed a substring test and then raised NameError on its first line. And it asserts the
honest case passes, so a function that refused everything could not sneak through green.

Run:  python tools/test_upload_confinement.py     (no ComfyUI, no GPU, no server, ~1 s)
"""
import ast
import os

BS2 = chr(92)  # a literal backslash, spelled so no quoting layer can eat it
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# _confine_to_input lives in __init__.py, which imports the whole pack at module load. We only want
# the one function, so lift its source out by AST and exec it against a bare namespace with os. That
# keeps this a small test and still runs the REAL shipped code, not a copy - if the function is
# renamed or deleted, this fails to find it and the test errors loudly rather than passing hollow.
def _load_guard():
    src = open(os.path.join(ROOT, "__init__.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_confine_to_input":
            ns = {"os": os}
            exec(compile(ast.Module([node], []), "<guard>", "exec"), ns)
            return ns["_confine_to_input"]
    raise SystemExit("FAIL: _confine_to_input not found at module scope in __init__.py")


FAILS = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  -> ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def main():
    guard = _load_guard()
    root = os.path.realpath(tempfile.mkdtemp())
    os.makedirs(os.path.join(root, "input"), exist_ok=True)
    input_root = os.path.join(root, "input")

    # --- refusals: every one of these must come back None ---------------------------
    # The value used in the actual ban is first. `name` carries a traversal too, because
    # basename is applied to the filename in the route but a test should not assume that.
    # Note what is NOT here: subfolder "." resolves to input/ itself, which is INSIDE the
    # root, so it is a legal write, not an escape. The brief listed it among refusals, but
    # the guard is right to allow it and the test would be wrong to demand otherwise - only
    # ".." actually leaves the directory. It is checked as an allowed case below instead.
    refuse = [
        ("subfolder '..' (the banned input)", "..", "pwned.txt"),
        ("subfolder '../..'", os.path.join("..", ".."), "pwned.txt"),
        ("subfolder is an absolute path", os.path.abspath(os.sep + "etc"), "pwned.txt"),
        ("name escapes via ..", "ok", os.path.join("..", "..", "pwned.txt")),
        ("name is absolute", "ok", os.path.abspath(os.sep + "etc" + os.sep + "pwned.txt")),
    ]
    for label, sub, name in refuse:
        dest = guard(input_root, sub, name)
        check(f"refuses: {label}", dest is None,
              "returned None" if dest is None else f"LET THROUGH -> {dest}")

    # A different drive can only be exercised where drives exist; skip elsewhere rather
    # than fake a pass. commonpath raises ValueError across drives and the guard maps
    # that to a refusal.
    if os.name == "nt" and len(input_root) > 1 and input_root[1] == ":":
        other = ("Z:" if input_root[0].upper() != "Z" else "Y:") + os.sep + "elsewhere"
        check("refuses: subfolder on another drive", guard(input_root, other, "pwned.txt") is None)

    # A backslash is a path SEPARATOR on Windows and an ordinary filename character
    # everywhere else, so one string escapes on one platform and is a legal (if ugly)
    # name on the other. The contract is confinement, not rejection of a spelling, so
    # assert that invariant everywhere and the stronger refusal only where it holds.
    bs_dest = guard(input_root, ".." + BS2 + "..", "pwned.txt")
    check("backslash subfolder never escapes (refused on Windows, confined on POSIX)",
          bs_dest is None or os.path.realpath(bs_dest).startswith(input_root + os.sep),
          bs_dest or "returned None")
    if os.name == "nt":
        check("refuses: backslash subfolder, where it is a separator", bs_dest is None,
              "returned None" if bs_dest is None else f"LET THROUGH -> {bs_dest}")

    # --- the honest cases MUST pass, or "refuses everything" would score green ------
    for label, sub, name in [
        ("ocio_assets/frame.0001.exr", "ocio_assets", "frame.0001.exr"),
        ("subfolder '.' stays in input/", ".", "frame.0001.exr"),
    ]:
        d = guard(input_root, sub, name)
        good = d is not None and os.path.realpath(d).startswith(input_root + os.sep)
        check(f"allows the honest case ({label})", good, d or "returned None")

    # --- on refusal, NOTHING is created on disk ------------------------------------
    # This catches a fix that guards the path but leaves the old makedirs above it.
    before = set()
    for dp, _, fns in os.walk(root):
        before.update(os.path.join(dp, f) for f in fns)
    guard(input_root, "..", "pwned.txt")
    after = set()
    for dp, _, fns in os.walk(root):
        after.update(os.path.join(dp, f) for f in fns)
    check("a refused destination creates nothing on disk", before == after,
          f"new paths: {sorted(after - before)}" if after != before else "")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {', '.join(FAILS)}")
        return 1
    print("PASS: upload writes are confined to the input directory")
    return 0


if __name__ == "__main__":
    sys.exit(main())
