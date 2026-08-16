"""Regression: `write_audio` is shown only where it is a real question (run: python tools/test_write_audio_visibility.py).

The toggle asks "should this render carry sound?", and on most of OCIO Write's settings that question has
already been answered by the wiring. A still or a sequence writes no movie; a movie with nothing connected to
`audio` or `video` has no sound to write. In those states the row is a control that can only hold the answer
it already has, so it is not drawn.

WHY IT IS NOT SIMPLY DELETED, which is the obvious reading of "wire sound and it gets written, wire none and
it does not": a native ComfyUI VIDEO carries its own track INSIDE the object. Connect a movie and the writer
adopts that track (io_nodes.py, the `audio = _vaudio` arm), and there is no wire to disconnect - this toggle
is the only way to ask for picture only. Deleting it would make a picture-only master impossible to request.

The cost, stated because it is a real one: a SEQUENCE's sidecar .wav can no longer be declined. Wire a track
to a sequence write and the .wav is written. That was a deliberate trade for the row disappearing everywhere
it was noise, not an oversight.

This runs the REAL front-end function under node against stub widgets, rather than grepping for its name -
visibility logic that is only read, never executed, is exactly how a widget ends up hidden forever (see the
`_ocioCompute` incident in web/ocio_io.js).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JS = os.path.join(_ROOT, "web", "ocio_io.js")


def _lift(src, decl):
    i = src.index(decl)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"could not lift '{decl}' out of web/ocio_io.js - the braces do not balance")


def _run(cases):
    """Drive the real applyAudioVis over a list of node states; return whether the row ended up visible."""
    src = open(_JS, encoding="utf-8").read()
    parts = [_lift(src, "function W(node, name)"),
             _lift(src, "function _ocioRestoreCompute(w)"),
             _lift(src, "function showWidget(node, w, visible)"),
             _lift(src, "function applyAudioVis(node)")]
    harness = "\n".join(parts) + """
const cases = JSON.parse(process.argv[2]);
const out = [];
for (const c of cases) {
    const node = {
        widgets: [{name: "container", value: c.container, options: {}},
                  {name: "write_audio", value: true, options: {}}],
        inputs: [{name: "images", link: c.images ? 1 : null},
                 {name: "video", link: c.video ? 2 : null},
                 {name: "audio", link: c.audio ? 3 : null}],
        size: [400, 500],
        computeSize: () => [400, 500],
        setSize() {},
        setDirtyCanvas() {},
    };
    applyAudioVis(node);
    const w = node.widgets[1];
    out.push({shown: w.hidden === false && w.options.hidden === false, hidden: !!w.hidden});
}
process.stdout.write(JSON.stringify(out));
"""
    d = tempfile.mkdtemp(prefix="ocio_audiovis_")
    try:
        path = os.path.join(d, "vis.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(harness)
        proc = subprocess.run(["node", path, json.dumps(cases)], capture_output=True,
                              text=True, encoding="utf-8")
        assert proc.returncode == 0, f"node refused the lifted function: {proc.stderr[:400]}"
        return json.loads(proc.stdout)
    finally:
        shutil.rmtree(d, ignore_errors=True)


CASES = [
    # (label, node state, should the row be visible?)
    # nothing wired: no sound exists, so nothing to decline
    ("sequence, nothing wired",       {"container": "sequence",    "images": True},                 False),
    ("video, nothing but images",     {"container": "video",       "images": True},                 False),
    # an explicit wire IS the control - pull it to drop the sound
    ("sequence + audio wired",        {"container": "sequence",    "images": True, "audio": True},  False),
    ("still image + audio wired",     {"container": "still image", "images": True, "audio": True},  False),
    ("video + audio wired",           {"container": "video",       "images": True, "audio": True},  False),
    # a VIDEO brings sound with no wire to pull: the toggle is the only way to say picture only
    ("video + VIDEO wired",           {"container": "video",       "video": True},                  True),
    ("sequence + VIDEO wired (.wav)", {"container": "sequence",    "video": True},                  True),
    # both: the explicit wire wins in the writer, so the toggle steps back out
    ("video + VIDEO + audio wired",   {"container": "video",       "video": True, "audio": True},   False),
]


def main():
    if not shutil.which("node"):
        print("  SKIP: node is not on PATH, so the visibility rule was NOT executed")
        return 0
    results = _run([c[1] for c in CASES])
    failures = []
    for (label, _state, want), got in zip(CASES, results):
        ok = got["shown"] is want
        print(f"  {'ok  ' if ok else 'FAIL'} {label}: "
              f"{'shown' if got['shown'] else 'hidden'} (expected {'shown' if want else 'hidden'})")
        if not ok:
            failures.append(label)
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nthe sound toggle appears only where sound is a question: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
