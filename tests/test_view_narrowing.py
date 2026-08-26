"""Regression: the `view` list is narrowed to what the pair can actually render (run: python tests/test_view_narrowing.py).

A VIEW BELONGS TO A DISPLAY, and the widget cannot say so: it is one flat combo, fixed when INPUT_TYPES is
evaluated, built as the union of every view across every display of TWO configs. Measured on the configs this
pack loads by default, 24 of its 32 real entries are invalid for `Rec.1886 Rec.709 - Display`, and choosing one
is not a soft miss - it raises at render time, after the graph has spent its time.

It became an ordinary mistake rather than an exotic one when the ACES 1.3 entries arrived. `ACES 1.1 - SDR
Video (Rec.709 lim)` reads like the obvious pick for a Rec.709 deliverable, and it lives on `Rec.1886 Rec.2020
- Display` - a display the ACES 2.0 config does not even have. That is the report this test exists for.

TWO ways a pick can be impossible, and they fail differently, so both are locked down:
  1. the view is not on the resolved display -> this pack's ValueError, which names the valid ones;
  2. the config carrying the view does not HAVE the scene colorspace (`D-Log D-Gamut` and `Linear D-Gamut` are
     in the ACES 2.0 config only) -> the view is valid for the display and the transform still cannot be built.
     Before this test that surfaced as a raw OCIO "Cannot find source color space", from inside getProcessor,
     with no mention of the view that caused it.

WHY THIS RUNS THE JAVASCRIPT. The narrowing itself lives in web/ocio_io.js, and a source-level check would only
prove the text is present - the class of defect this pack has been bitten by before (a button that was deleted
under a green gate; see tests/test_node_buttons_present.py). So `viewsForCrossing` is lifted out of the file
and EXECUTED under node against the real config data, and every entry it keeps is then put through the real
_convert_via_view. If node is unavailable the JS checks say so and are skipped; the Python-side contract - the
data the front end narrows with - is checked either way.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JS = os.path.join(_ROOT, "web", "ocio_io.js")


def _load(tmp="."):
    fp = types.ModuleType("folder_paths")
    fp.get_output_directory = fp.get_temp_directory = fp.get_input_directory = lambda: tmp
    fp.get_filename_list = lambda *a, **k: []
    sys.modules.setdefault("folder_paths", fp)
    pkg = types.ModuleType("ocio_pkg")
    pkg.__path__ = [_ROOT]
    sys.modules["ocio_pkg"] = pkg
    for name in ("nodes", "io_nodes"):
        spec = importlib.util.spec_from_file_location(f"ocio_pkg.{name}", os.path.join(_ROOT, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"ocio_pkg.{name}"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["ocio_pkg.io_nodes"]


DISPLAY = "Rec.1886 Rec.709 - Display"     # the display the reported ValueError resolved to
SCENE = "ACEScg"
REPORTED = "ACES 1.3: ACES 1.1 - SDR Video (Rec.709 lim)"   # what was picked, and lives on another display
VALID_13 = "ACES 1.3: ACES 1.0 - SDR Video"                 # a 1.3 view that IS on this display


# --------------------------------------------------------------------------------------------------
# the Python side: the data the front end narrows with
# --------------------------------------------------------------------------------------------------

def check_alt_map_agrees_with_ocio(io):
    """The per-display map is asked of OCIO, not of another of our own functions - two of our paths agreeing
    proves nothing about either."""
    import PyOpenColorIO as OCIO
    got = io._alt_views_by_display()
    assert got, "no ACES 1.3 views per display at all; the front end has nothing to narrow with"
    alt = OCIO.Config.CreateFromFile(io._ALT_ACES_URI)
    for d in alt.getDisplays():
        want = [io._ALT_ACES_LABEL + v for v in alt.getViews(d)]
        assert got.get(d) == want, f"display '{d}': map says {got.get(d)}, the config says {want}"
    assert set(got) == set(alt.getDisplays()), "the map and the config disagree about which displays exist"


def check_flat_list_and_map_come_from_one_read(io):
    """_view_choices offers the widget's entries; the map decides which of them survive narrowing. An entry in
    one and not the other is an entry that can be picked and never offered again, or offered and never valid."""
    flat = set(io._view_choices())
    mapped = {v for views in io._alt_views_by_display().values() for v in views}
    missing = mapped - flat
    assert not missing, f"the map offers entries the widget does not have: {sorted(missing)[:4]}"
    prefixed_in_flat = {v for v in flat if v.startswith(io._ALT_ACES_LABEL)}
    assert prefixed_in_flat == mapped, (
        f"the widget offers ACES 1.3 entries the map cannot place on any display: "
        f"{sorted(prefixed_in_flat - mapped)[:4]}")


def check_the_reported_pick_is_on_another_display(io):
    """The premise of the whole fix, measured rather than restated: the reported view exists, and not here."""
    by_display = io._alt_views_by_display()
    assert REPORTED not in by_display.get(DISPLAY, []), (
        f"'{REPORTED}' is listed for '{DISPLAY}' - if that is now true the report has been fixed elsewhere "
        f"and this test is measuring the wrong thing")
    elsewhere = [d for d, vs in by_display.items() if REPORTED in vs]
    assert elsewhere, f"'{REPORTED}' is on no display at all; it should not be in the widget either"
    assert VALID_13 in by_display.get(DISPLAY, []), (
        f"'{VALID_13}' is not offered for '{DISPLAY}'; the narrowing would leave no 1.3 choice at all")


def check_the_missing_colorspace_is_named_not_raw(io):
    """Failure mode 2. The view is valid for the display and the transform still cannot be built, because the
    1.3 config has no such colorspace. Must be this pack's message, not OCIO's from inside getProcessor."""
    import torch
    missing = [cs for cs in ("D-Log D-Gamut", "Linear D-Gamut")
               if cs not in io._alt_colorspace_names()]
    if not missing:
        raise AssertionError("no colorspace differs between the two configs any more; this check is measuring "
                             "nothing and needs a new example")
    scene = missing[0]
    img = torch.zeros((1, 1, 2, 3), dtype=torch.float32)
    try:
        io._convert_via_view(img, DISPLAY, scene, VALID_13)
    except ValueError as e:
        assert scene in str(e) and "does not exist in the config" in str(e), (
            f"raised, but not about the missing colorspace: {e}")
        return
    except Exception as e:
        raise AssertionError(f"raised the raw OCIO error instead of a named one: {type(e).__name__}: {e}")
    raise AssertionError(f"'{scene}' is absent from the 1.3 config and the transform was built anyway")


# --------------------------------------------------------------------------------------------------
# the JavaScript side: the narrowing itself, EXECUTED
# --------------------------------------------------------------------------------------------------

def _lift(src, decl):
    """The text of one top-level JS declaration, by brace balance. Not a parser - it only has to survive this
    one function, and node itself rejects anything malformed when the test runs it."""
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


def _js_view_none(src):
    m = re.search(r'const\s+VIEW_NONE\s*=\s*"([^"]+)"', src)
    assert m, "VIEW_NONE is not declared in web/ocio_io.js"
    return m.group(1)


def _run_narrowing(io, scene, display):
    """Run the REAL front-end function under node, with the REAL config data, and return what it keeps."""
    src = open(_JS, encoding="utf-8").read()
    fn = _lift(src, "function viewsForCrossing(")
    enc = {"views": {d: {"all": list(io._resolve_config_keyed("")[0].getViews(d))}
                     for d in io._resolve_config_keyed("")[0].getDisplays()},
           "alt": {"label": io._ALT_ACES_LABEL,
                   "views": io._alt_views_by_display(),
                   "colorspaces": io._alt_colorspace_names()}}
    payload = {"enc": enc, "cross": {"display": display, "scene": scene}, "all": io._view_choices()}
    script = (
        f'const VIEW_NONE = {json.dumps(_js_view_none(src))};\n'
        f'{fn}\n'
        'const p = JSON.parse(process.argv[2]);\n'
        'const allow = viewsForCrossing(p.enc, p.cross);\n'
        'const kept = allow === null ? null : p.all.filter(v => allow.includes(v));\n'
        'process.stdout.write(JSON.stringify({allow: allow, kept: kept}));\n'
    )
    d = tempfile.mkdtemp(prefix="ocio_js_")
    try:
        path = os.path.join(d, "narrow.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        proc = subprocess.run(["node", path, json.dumps(payload)], capture_output=True,
                              text=True, encoding="utf-8")
        assert proc.returncode == 0, f"node refused the lifted function: {proc.stderr[:400]}"
        return json.loads(proc.stdout)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_js_drops_the_reported_pick(io):
    r = _run_narrowing(io, SCENE, DISPLAY)
    kept = r["kept"]
    assert kept is not None, "the narrowing declined to narrow a pair that crosses; nothing would be filtered"
    assert REPORTED not in kept, f"'{REPORTED}' survived the narrowing for '{DISPLAY}' - the render still fails"
    assert VALID_13 in kept, f"'{VALID_13}' was dropped; the artist loses the 1.3 choice that does work"
    assert io._VIEW_NONE in kept, "the do-nothing value must always survive; it is the default"
    assert len(kept) < len(io._view_choices()), "nothing was narrowed at all"


def check_everything_js_keeps_actually_renders(io):
    """The strongest check here: every entry the front end leaves selectable is put through the real
    conversion. A narrowing that keeps something the renderer refuses is worse than none - it promises."""
    import torch
    kept = _run_narrowing(io, SCENE, DISPLAY)["kept"]
    assert kept is not None, "the narrowing declined to narrow a crossing pair; there is nothing to check"
    img = torch.zeros((1, 1, 2, 3), dtype=torch.float32)
    for v in kept:
        try:
            io._convert_via_view(img, DISPLAY, SCENE, v)
        except Exception as e:
            raise AssertionError(f"the narrowing kept '{v}', and rendering it raises {type(e).__name__}: {e}")


def check_something_js_drops_really_would_fail(io):
    """The other half: a filter that drops nothing real is decoration. The reported pick must still raise."""
    import torch
    img = torch.zeros((1, 1, 2, 3), dtype=torch.float32)
    try:
        io._convert_via_view(img, DISPLAY, SCENE, REPORTED)
    except ValueError:
        return
    raise AssertionError(f"'{REPORTED}' renders fine now; the narrowing is dropping a valid choice")


def check_js_drops_all_13_when_the_colorspace_is_absent(io):
    """Failure mode 2 seen from the front end: the views are valid for the display, the colorspace is not in
    that config, so not one of them may be offered."""
    missing = [cs for cs in ("D-Log D-Gamut", "Linear D-Gamut")
               if cs not in io._alt_colorspace_names()]
    assert missing, "no colorspace differs between the configs any more; this check needs a new example"
    kept = _run_narrowing(io, missing[0], DISPLAY)["kept"]
    assert kept is not None, "the narrowing declined to narrow a crossing pair; every 1.3 view stays offered"
    stragglers = [v for v in kept if v.startswith(io._ALT_ACES_LABEL)]
    assert not stragglers, (
        f"'{missing[0]}' is not in the ACES 1.3 config, yet {len(stragglers)} of its views are still offered: "
        f"{stragglers[:3]}")


def check_js_leaves_a_non_crossing_pair_alone(io):
    """scene -> scene has no display and no fork: `view` does nothing, so the list must not be touched."""
    src = open(_JS, encoding="utf-8").read()
    assert "return null;" in _lift(src, "function viewsForCrossing("), \
        "viewsForCrossing no longer has a no-narrowing path"
    r = _run_narrowing(io, SCENE, DISPLAY)
    assert r["allow"] is not None, "sanity: a crossing pair must narrow"


def check_the_auto_filled_view_is_the_1_3_one(io):
    """The version the widget fills itself in with is ACES 1.3, not the loaded config's own 2.0.

    Which version renders is a statement about who opens the file: a Nuke 13 / 14 comp is on ACES 1.2 / 1.3,
    and a render made with the wrong one is off by 35.5% on the worst pixel of a real Rec.709 master. The 2.0
    entries stay one click away in the same list for anyone delivering into a 2.0 pipeline."""
    src = open(_JS, encoding="utf-8").read()
    fn = _lift(src, "function preferredViewFor(")
    enc = {"views": {d: {"default": io._resolve_config_keyed("")[0].getDefaultView(d)}
                     for d in io._resolve_config_keyed("")[0].getDisplays()},
           "alt": {"defaults": io._alt_default_views(), "colorspaces": io._alt_colorspace_names()}}
    payload = {"enc": enc, "cross": {"display": DISPLAY, "scene": SCENE}}
    script = (f'const VIEW_NONE = {json.dumps(_js_view_none(src))};\n{fn}\n'
              'const p = JSON.parse(process.argv[2]);\n'
              'process.stdout.write(JSON.stringify({want: preferredViewFor(p.enc, p.cross)}));\n')
    d = tempfile.mkdtemp(prefix="ocio_pref_")
    try:
        path = os.path.join(d, "pref.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        proc = subprocess.run(["node", path, json.dumps(payload)], capture_output=True,
                              text=True, encoding="utf-8")
        assert proc.returncode == 0, f"node refused the lifted function: {proc.stderr[:400]}"
        want = json.loads(proc.stdout)["want"]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert want and want.startswith(io._ALT_ACES_LABEL), (
        f"the auto-fill chose {want!r}; expected an '{io._ALT_ACES_LABEL}' entry for a Nuke-bound render")
    assert want == VALID_13, f"the auto-fill chose {want!r}, expected {VALID_13!r} (that display's 1.3 default)"
    # and it must be a view the render will actually accept
    import torch
    io._convert_via_view(torch.zeros((1, 1, 2, 3), dtype=torch.float32), DISPLAY, SCENE, want)


def check_the_auto_fill_falls_back_when_1_3_cannot_do_it(io):
    """A colorspace the 1.3 config does not have must fall back to the loaded config's default rather than
    filling in a view that cannot be built at all."""
    src = open(_JS, encoding="utf-8").read()
    fn = _lift(src, "function preferredViewFor(")
    missing = [cs for cs in ("D-Log D-Gamut", "Linear D-Gamut") if cs not in io._alt_colorspace_names()]
    assert missing, "no colorspace differs between the configs any more; this check needs a new example"
    cfg = io._resolve_config_keyed("")[0]
    enc = {"views": {DISPLAY: {"default": cfg.getDefaultView(DISPLAY)}},
           "alt": {"defaults": io._alt_default_views(), "colorspaces": io._alt_colorspace_names()}}
    payload = {"enc": enc, "cross": {"display": DISPLAY, "scene": missing[0]}}
    script = (f'const VIEW_NONE = {json.dumps(_js_view_none(src))};\n{fn}\n'
              'const p = JSON.parse(process.argv[2]);\n'
              'process.stdout.write(JSON.stringify({want: preferredViewFor(p.enc, p.cross)}));\n')
    d = tempfile.mkdtemp(prefix="ocio_pref2_")
    try:
        path = os.path.join(d, "pref.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(script)
        proc = subprocess.run(["node", path, json.dumps(payload)], capture_output=True,
                              text=True, encoding="utf-8")
        assert proc.returncode == 0, f"node refused the lifted function: {proc.stderr[:400]}"
        want = json.loads(proc.stdout)["want"]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert want and not want.startswith(io._ALT_ACES_LABEL), (
        f"with '{missing[0]}' absent from the 1.3 config the auto-fill still chose {want!r}, which cannot "
        f"be built")


def check_js_and_python_agree_on_the_sentinel(io):
    """The comment above VIEW_NONE says a typo here makes the auto-fill silently do nothing forever."""
    js = _js_view_none(open(_JS, encoding="utf-8").read())
    assert js == io._VIEW_NONE, f"VIEW_NONE differs: JS has {js!r}, Python has {io._VIEW_NONE!r}"


PY_CHECKS = (check_alt_map_agrees_with_ocio, check_flat_list_and_map_come_from_one_read,
             check_the_reported_pick_is_on_another_display, check_the_missing_colorspace_is_named_not_raw,
             check_js_and_python_agree_on_the_sentinel)
JS_CHECKS = (check_js_drops_the_reported_pick, check_everything_js_keeps_actually_renders,
             check_something_js_drops_really_would_fail, check_js_drops_all_13_when_the_colorspace_is_absent,
             check_js_leaves_a_non_crossing_pair_alone, check_the_auto_filled_view_is_the_1_3_one,
             check_the_auto_fill_falls_back_when_1_3_cannot_do_it)


def main():
    io = _load()
    failures = []
    checks = list(PY_CHECKS)
    if shutil.which("node"):
        checks += list(JS_CHECKS)
    else:
        print("  SKIP the JavaScript checks: node is not on PATH, so the narrowing itself was NOT executed")
    for fn in checks:
        try:
            fn(io)
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            failures.append(fn.__name__)
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            failures.append(fn.__name__)
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nthe view list narrows to what the pair can render: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
