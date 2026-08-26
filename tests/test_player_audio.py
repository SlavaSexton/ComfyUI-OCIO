"""Regression: OCIO Player carries a soundtrack, cut to the frames it cached (run: python tests/test_player_audio.py).

The Player's picture path is half-float frames on disk, and a batch of frames has no sound in it - so with an
IMAGE input the L/R meters had nothing to read, whatever the graph upstream had generated. The `audio` input
is where the track comes in.

WHAT THIS LOCKS DOWN, and the second one is the whole point:

1. The track is written beside the frames and named in the node's report, so the front end can find it.
2. It is cut to the frames the viewer CACHED, not to the track's own length. The viewer caps its frames at
   _PLAYER_FRAME_CAP and builds its transport from what it holds; a full-length track against a truncated
   picture drifts by exactly the frames that were dropped, and it drifts silently - the sound simply stops
   agreeing with the picture somewhere in the middle.
3. A malformed AUDIO does not take the picture down with it. Everywhere else in this pack a bad track raises,
   because there the result is a delivered file that is silently silent. A VIEWER trades the other way: the
   frames still play and the problem is named in the report.
4. The file is 16-bit PCM. Chrome decodes 24-bit through decodeAudioData and Firefox has failed on it for
   years, and no level meter can tell the two apart (16-bit carries 96 dB against a meter floor of 48).
"""
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import types
import wave

os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FPS = 24.0
SR = 48000
FRAMES = 10
H, W = 4, 6


def _load(tmp):
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


def _images(n=FRAMES):
    import torch
    return torch.rand((n, H, W, 3), dtype=torch.float32)


def _audio(seconds, ch=2):
    """A ComfyUI AUDIO of a given length: {"waveform": [B,C,T], "sample_rate": int}."""
    import torch
    t = int(round(seconds * SR))
    return {"waveform": torch.zeros((1, ch, t), dtype=torch.float32), "sample_rate": SR}


def _play(io, tmp, audio, frames=FRAMES, uid="audiotest"):
    return io.OCIOPlayer().play("ACEScg", "ACEScg", False, 0, 0, FPS,
                                images=_images(frames), audio=audio, unique_id=uid)


def check_the_track_is_written_and_named(io, tmp):
    ui = _play(io, tmp, _audio(FRAMES / FPS))["ui"]
    assert ui.get("player_audio") == [io._PLAYER_AUDIO], f"the report does not name the track: {ui.get('player_audio')}"
    path = os.path.join(ui["player_dir"][0], io._PLAYER_AUDIO)
    assert os.path.isfile(path), f"no track beside the frames at {path}"
    assert ui.get("audio_sig") and ui["audio_sig"][0], "no audio term in the re-init signature; only-the-sound-changed would not reload"


def check_a_long_track_is_cut_to_the_cached_frames(io, tmp):
    """The drift case: ten frames of picture, a hundred frames' worth of sound."""
    ui = _play(io, tmp, _audio(100 / FPS))["ui"]
    path = os.path.join(ui["player_dir"][0], io._PLAYER_AUDIO)
    with wave.open(path, "rb") as w:
        got, sr = w.getnframes(), w.getframerate()
    want = int(round(FRAMES / FPS * SR))
    assert sr == SR, f"sample rate changed: {sr}"
    assert abs(got - want) <= 1, (
        f"the track is {got} samples for {FRAMES} frames at {FPS} fps; expected {want}. A track longer than "
        f"the cached picture drifts by exactly the frames that were dropped.")


def check_a_short_track_is_padded_not_truncated(io, tmp):
    """Silence at the end keeps sound and picture the same length; the alternative is a picture that ends early."""
    ui = _play(io, tmp, _audio(2 / FPS))["ui"]
    with wave.open(os.path.join(ui["player_dir"][0], io._PLAYER_AUDIO), "rb") as w:
        got = w.getnframes()
    want = int(round(FRAMES / FPS * SR))
    assert abs(got - want) <= 1, f"a short track produced {got} samples, expected {want} (padded to picture length)"


def check_it_is_sixteen_bit(io, tmp):
    ui = _play(io, tmp, _audio(FRAMES / FPS))["ui"]
    with wave.open(os.path.join(ui["player_dir"][0], io._PLAYER_AUDIO), "rb") as w:
        width, ch = w.getsampwidth(), w.getnchannels()
    assert width == 2, f"the viewer's copy is {width * 8}-bit; browsers are only unanimous about 16"
    assert ch == 2, f"stereo in, {ch} channel(s) out - the L/R meters need both"


def check_a_bad_track_does_not_stop_the_picture(io, tmp):
    """A viewer that refuses to show frames because the sound was wrong is the worse failure."""
    out = _play(io, tmp, {"waveform": "not a tensor", "sample_rate": 0})
    ui = out["ui"]
    assert ui.get("player_dir"), "the frames were not cached; a bad track took the picture down with it"
    assert ui.get("player_audio") == [""], f"a broken track was still named as playable: {ui.get('player_audio')}"
    note = (ui.get("audio_note") or [""])[0]
    assert "audio" in note.lower(), f"the report says nothing about the track being dropped: {note!r}"


def check_no_audio_leaves_no_file(io, tmp):
    """And the old behaviour is exactly the old behaviour when nothing is wired."""
    ui = _play(io, tmp, None, uid="silent")["ui"]
    assert ui.get("player_audio") == [""], f"a track was reported with nothing wired: {ui.get('player_audio')}"
    assert not os.path.isfile(os.path.join(ui["player_dir"][0], io._PLAYER_AUDIO)), "a stale track was left behind"


def check_a_new_run_without_audio_clears_the_old_track(io, tmp):
    """The cache dir is per node and reused. Sound from a previous run must not survive into a silent one."""
    ui = _play(io, tmp, _audio(FRAMES / FPS), uid="reuse")["ui"]
    assert os.path.isfile(os.path.join(ui["player_dir"][0], io._PLAYER_AUDIO))
    ui2 = _play(io, tmp, None, uid="reuse")["ui"]
    assert not os.path.isfile(os.path.join(ui2["player_dir"][0], io._PLAYER_AUDIO)), (
        "the previous run's track is still there; the viewer would play sound the graph no longer has")


def check_the_range_note_reports_what_is_there(io, tmp):
    """The Info panel's `Range check`, which says whether pulling exposure down can reveal anything.

    A display-referred master holds nothing above white - its container's ceiling IS white and the writer
    clips there - so the exposure slider darkens and reveals nothing, which looks identical to a broken
    viewer. Measured on a real Rec.709 ProRes from this pack's own writer: 136 samples above white across
    121 frames, 0.000125%. The line is what tells those two states apart without a measurement."""
    import torch
    flat = torch.full((2, H, W, 3), 0.5, dtype=torch.float32)
    ui = io.OCIOPlayer().play("ACEScg", "ACEScg", False, 0, 0, FPS, images=flat, unique_id="range_flat")["ui"]
    note = (ui.get("range_note") or [""])[0]
    assert "nothing above 1.0" in note, f"a batch that tops out at 0.5 should say so; got {note!r}"

    hdr = flat.clone()
    hdr[0, 0, 0, :] = 8.0                                  # one HDR pixel, the thing exposure exists to find
    ui = io.OCIOPlayer().play("ACEScg", "ACEScg", False, 0, 0, FPS, images=hdr, unique_id="range_hdr")["ui"]
    note = (ui.get("range_note") or [""])[0]
    assert "above 1.0" in note and "nothing above" not in note, f"an HDR sample was not reported: {note!r}"
    assert "8.000" in note, f"the peak was not reported: {note!r}"


def check_the_error_names_the_player_not_the_writer(io, tmp):
    """_audio_pcm is shared with OCIO Write, whose messages send a Player user to the wrong node."""
    try:
        io._audio_pcm({"waveform": None, "sample_rate": 0}, FPS, 1, 0, who="OCIO Player")
    except ValueError as e:
        assert "OCIO Player" in str(e), f"the message names the wrong node: {e}"
        return
    raise AssertionError("a sample_rate of 0 was accepted")


# --------------------------------------------------------------------------------------------------
# the front end's half: the clock that keeps the track on the picture, EXECUTED
#
# The frame clock is wall-clock anchored and the sound is an AudioBufferSourceNode, which cannot seek and
# cannot be reused - so staying in step means restarting the node whenever the picture has moved somewhere the
# sound is not. Every one of those moments (a loop back to the in-point, a scrub, an fps change) is checked by
# comparing POSITIONS rather than by listening for the event, and the loop is why: the frame clock wraps by
# modulo and never re-anchors, so an event-driven version would keep playing straight past the out-point.
#
# Lifted out of web/ocio_io.js and run under node, for the same reason tests/test_view_narrowing.py does it:
# a check that greps for the function's name cannot tell a working one from a deleted body.
# --------------------------------------------------------------------------------------------------

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


def _run_sync(steps):
    """Drive the REAL _playerAudioSync through a scripted sequence of clock states and report what it did to
    the source node at each one. The Web Audio objects are the thinnest stubs that record calls."""
    import json
    import shutil
    import subprocess
    import tempfile
    src = open(_JS, encoding="utf-8").read()
    fn = _lift(src, "function _playerAudioSync(")
    stop = _lift(src, "function _stopBufSource(")
    slip = re.search(r"const\s+_AUDIO_SLIP\s*=\s*([0-9.]+)", src)
    assert slip, "_AUDIO_SLIP is not declared; the sync has no drift tolerance"
    harness = """
const _AUDIO_SLIP = %s;
%s
%s
// --- stubs: just enough Web Audio to record what the sync did ---
let now = 0, started = [];
const ctx = { get currentTime() { return now; }, state: "running", resume() {},
              createBufferSource() { const s = { buffer: null, started: null,
                  connect() {}, disconnect() {}, stop() {},
                  start(when, off) { s.started = off; started.push(+off.toFixed(4)); } }; return s; } };
const p = { pb: null, node: null, bufAudio: null };
function W(node, name) { return { value: node[name] }; }
function _pbCur(p) { return p.pb.frame; }
function _pbIn(p) { return p.pb.inF; }
const steps = JSON.parse(process.argv[2]);
const out = [];
p.bufAudio = { ctx, buffer: { duration: steps.duration }, splitter: {}, gain: {},
               src: null, startedAt: 0, startOffset: 0 };
for (const s of steps.steps) {
    now = s.now;
    p.pb = { playing: s.playing, dir: s.dir, fps: s.fps, frame: s.frame, inF: s.inF, seqMode: true };
    p.node = { fps: s.fps };
    _playerAudioSync(p);
    out.push({ live: !!p.bufAudio.src, offset: p.bufAudio.src ? +p.bufAudio.startOffset.toFixed(4) : null });
}
process.stdout.write(JSON.stringify({ frames: out, starts: started }));
""" % (slip.group(1), stop, fn)
    d = tempfile.mkdtemp(prefix="ocio_sync_")
    try:
        path = os.path.join(d, "sync.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(harness)
        proc = subprocess.run(["node", path, json.dumps(steps)], capture_output=True,
                              text=True, encoding="utf-8")
        assert proc.returncode == 0, f"node refused the lifted sync: {proc.stderr[:400]}"
        return json.loads(proc.stdout)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_sync_starts_at_the_playhead_not_at_zero(io, tmp):
    """Hitting play on frame 24 of a 24 fps clip must start the sound one second in, not from the top."""
    r = _run_sync({"duration": 4.0, "steps": [
        {"now": 0, "playing": True, "dir": 1, "fps": 24, "frame": 24, "inF": 0}]})
    assert r["starts"] == [1.0], f"the track started at {r['starts']}, expected [1.0] (frame 24 at 24 fps)"


def check_sync_leaves_a_running_source_alone(io, tmp):
    """The sound is only re-anchored when it has actually drifted. Restarting it every frame would stutter."""
    r = _run_sync({"duration": 4.0, "steps": [
        {"now": 0.0, "playing": True, "dir": 1, "fps": 24, "frame": 0, "inF": 0},
        {"now": 0.5, "playing": True, "dir": 1, "fps": 24, "frame": 12, "inF": 0},
        {"now": 1.0, "playing": True, "dir": 1, "fps": 24, "frame": 24, "inF": 0}]})
    assert r["starts"] == [0.0], f"the source was restarted mid-play: {r['starts']}"


def check_sync_re_anchors_on_a_loop(io, tmp):
    """The case an event-driven version misses: the frame clock wraps to the in-point by modulo, without ever
    re-anchoring itself, so only a position comparison notices that the sound is now two seconds ahead."""
    r = _run_sync({"duration": 4.0, "steps": [
        {"now": 0.0, "playing": True, "dir": 1, "fps": 24, "frame": 0, "inF": 0},
        {"now": 2.0, "playing": True, "dir": 1, "fps": 24, "frame": 48, "inF": 0},
        {"now": 2.05, "playing": True, "dir": 1, "fps": 24, "frame": 0, "inF": 0}]})
    assert len(r["starts"]) == 2 and r["starts"][1] == 0.0, (
        f"the loop did not take the sound back to the in-point: {r['starts']}")


def check_sync_re_anchors_on_a_scrub(io, tmp):
    r = _run_sync({"duration": 4.0, "steps": [
        {"now": 0.0, "playing": True, "dir": 1, "fps": 24, "frame": 0, "inF": 0},
        {"now": 0.1, "playing": True, "dir": 1, "fps": 24, "frame": 72, "inF": 0}]})
    assert r["starts"] == [0.0, 3.0], f"a scrub to frame 72 did not move the sound: {r['starts']}"


def check_sync_is_silent_in_reverse_and_when_stopped(io, tmp):
    """Reverse has no signal to show - a buffer source has no negative rate - so it must stop, not free-run."""
    r = _run_sync({"duration": 4.0, "steps": [
        {"now": 0.0, "playing": True, "dir": 1, "fps": 24, "frame": 0, "inF": 0},
        {"now": 0.2, "playing": True, "dir": -1, "fps": 24, "frame": 4, "inF": 0},
        {"now": 0.4, "playing": False, "dir": 1, "fps": 24, "frame": 4, "inF": 0}]})
    assert r["frames"][1]["live"] is False, "reverse left the track playing forwards under a backwards picture"
    assert r["frames"][2]["live"] is False, "stopping the picture left the sound running"


def check_sync_does_not_restart_past_the_end_of_the_track(io, tmp):
    """A picture longer than its track must fall silent, not spin restarting a source every frame."""
    r = _run_sync({"duration": 1.0, "steps": [
        {"now": 0.0, "playing": True, "dir": 1, "fps": 24, "frame": 48, "inF": 0},
        {"now": 0.1, "playing": True, "dir": 1, "fps": 24, "frame": 49, "inF": 0},
        {"now": 0.2, "playing": True, "dir": 1, "fps": 24, "frame": 50, "inF": 0}]})
    assert r["starts"] == [], f"the sync started a source past the end of the track: {r['starts']}"


def check_sync_counts_from_the_in_point(io, tmp):
    """in / out points trim the picture, and the track was cut to the same frames - so the sound for the
    in-point is the START of the file, not the in-point's own timecode."""
    r = _run_sync({"duration": 4.0, "steps": [
        {"now": 0.0, "playing": True, "dir": 1, "fps": 24, "frame": 36, "inF": 36}]})
    assert r["starts"] == [0.0], f"playing from the in-point started the sound at {r['starts']}, expected [0.0]"


JS_CHECKS = (check_sync_starts_at_the_playhead_not_at_zero, check_sync_leaves_a_running_source_alone,
             check_sync_re_anchors_on_a_loop, check_sync_re_anchors_on_a_scrub,
             check_sync_is_silent_in_reverse_and_when_stopped,
             check_sync_does_not_restart_past_the_end_of_the_track, check_sync_counts_from_the_in_point)

CHECKS = (check_the_track_is_written_and_named, check_a_long_track_is_cut_to_the_cached_frames,
          check_a_short_track_is_padded_not_truncated, check_it_is_sixteen_bit,
          check_a_bad_track_does_not_stop_the_picture, check_no_audio_leaves_no_file,
          check_a_new_run_without_audio_clears_the_old_track, check_the_error_names_the_player_not_the_writer,
          check_the_range_note_reports_what_is_there)


def main():
    tmp = tempfile.mkdtemp(prefix="ocio_player_audio_")
    io = _load(tmp)
    failures = []
    checks = list(CHECKS)
    if shutil.which("node"):
        checks += list(JS_CHECKS)
    else:
        print("  SKIP the clock checks: node is not on PATH, so the sync itself was NOT executed")
    try:
        for fn in checks:
            try:
                fn(io, tmp)
                print(f"  ok  {fn.__name__}")
            except AssertionError as e:
                failures.append(fn.__name__)
                print(f"  FAIL {fn.__name__}: {e}")
            except Exception as e:
                failures.append(fn.__name__)
                print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nthe Player's soundtrack is cut to the frames it holds: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
