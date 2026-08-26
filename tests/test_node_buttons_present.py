"""Regression: every node still has the buttons it is operated with (run: python tests/test_node_buttons_present.py).

On 2026-08-16 the `▶ Render` button vanished from OCIO Write. Not by a decision - by an edit that removed the
Viewer toggle beneath it and took the line above along with it. The node still loaded, still validated, still
wrote files from a queued graph, and the whole Python gate stayed green, because nothing in it touches the
front end. The only thing that broke was the button an artist presses.

That is the shape of this defect: a button is a single `addWidget` call in `onNodeCreated`, so deleting one is
one line and produces no error anywhere. There is nothing to throw, nothing to fail, and no test that would
notice unless it is looking for exactly this.

So this looks for exactly this. It reads `web/ocio_io.js` and asserts each button is still constructed. That
is a source-level check, which is weak coverage in general - it proves the text is there, not that it works -
and here that is the right level, because presence IS the property under test. Whether the callback does its
job is the business of the tests around it and of a real click in the canvas.
"""
import os
import re
import sys

# The labels under test contain ▶ and ▾, and a Windows console is cp1251 here: printing them raw raises
# UnicodeEncodeError and the test fails whether or not the buttons are there. A test that cannot report its
# own result is worse than no test, so the stream is made lossy rather than fatal.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:                                   # not a reconfigurable stream (a pipe, a capture)
        pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JS = os.path.join(_ROOT, "web", "ocio_io.js")

# Label -> what it is for, so a failure says what the artist lost rather than just naming a string.
WANT = {
    "▶ Render":            "OCIO Write: queues the graph. The node's whole point of action.",
    "Output Folder":       "OCIO Write: picks where the render lands.",
    "Detect from Source":  "OCIO Read: reads the plate and fills in what it finds.",
    "Open Files":          "OCIO Read: the disk browser that fills `source`.",
    "▾ Viewer":            "OCIO Read: folds the preview and its transport away.",
    "▾ Metadata":          "OCIO Read: shows what the file actually carries.",
    "▾ Info":              "OCIO Player: the clip's own numbers.",
}

# Deliberately absent, and each for a reason worth keeping. A test that only checks presence would let a
# removed thing quietly come back.
WANT_ABSENT = {
    "__ocio_view":  "OCIO Write's own viewport. Removed: OCIO Player is the viewer, and a player on every "
                    "Write node turns a graph of four writes into a column of four players.",
    "__ocio_flip":  "the written-sequence flipbook, same removal.",
}


def main():
    if not os.path.isfile(_JS):
        print(f"  ERROR: {_JS} is missing")
        return 1
    src = open(_JS, encoding="utf-8").read()

    failures = []
    for label, why in WANT.items():
        # the construction, not a mention in a comment: addWidget("button", "<label>"
        pat = r'addWidget\(\s*"button"\s*,\s*"' + re.escape(label) + r'"'
        if not re.search(pat, src):
            failures.append(f'the "{label}" button is not constructed any more. {why}')
        else:
            print(f'  ok  "{label}"')

    for token, why in WANT_ABSENT.items():
        if token in src:
            failures.append(f'"{token}" is back in the front end. {why}')
        else:
            print(f"  ok  {token} stays removed")

    if failures:
        print()
        for f in failures:
            print("  FAIL " + f)
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nevery node still has the buttons it is operated with: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
