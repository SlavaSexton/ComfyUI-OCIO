"""OCIO parity vs reference (2026-07-03).

Answers Sam Hodge's "vibe-coding color maths without regression testing is bold": prove the pack's
OCIO-backed transforms match a RAW PyOpenColorIO CPU processor built independently of the node wrapper,
so a wiring bug in the node cannot hide behind the node's own code path.

For each fixture (ColorChecker, spectral_sweep, hdr_ramp, real nyc image) we run OUR node path and compare
to an INDEPENDENT reference:
  - io_nodes._convert(tensor, in, out)                         vs _common.ocio_reference(in, out, rgb)
  - OCIOColorSpace().convert(tensor, in, out)                  vs _common.ocio_reference(in, out, rgb)
  - OCIODisplay().run(tensor, in, display, view, invert)       vs a fresh DisplayViewTransform processor
  - OCIOCDLTransform().run(tensor, slope/offset/power/sat,...) vs a fresh CDLTransform processor

The Display/CDL references are built here from a fresh config (_resolve_config_keyed) and a fresh transform
object -> fresh processor, NOT by calling the node, so the node's own path is what's under test.

Reports max abs + RMS error per (transform x fixture) and per transform aggregated across fixtures.
Chart: bar of aggregate max-abs error per transform, log y, with a 1e-4 threshold line.

Run:  E:/ComfyUI/ComfyUI/ComfyUI/.venv/Scripts/python.exe measure_ocio_parity.py
"""
import os
import numpy as np
import torch

import _common as C

N, IO = C.load_pack()
import PyOpenColorIO as OCIO   # noqa: E402  (loaded by load_pack already)


# --------------------------------------------------------------------------- independent references
def ref_colorspace(in_cs, out_cs, rgb):
    """Ground truth for a colorspace pair: the harness's raw-processor reference."""
    return C.ocio_reference(in_cs, out_cs, rgb)


def ref_display(in_cs, display, view, invert, rgb):
    """INDEPENDENT ground truth for OCIODisplay: build a fresh DisplayViewTransform processor from a fresh
    config resolve (not the node's run method), apply to a packed copy. Mirrors the OCIO C++ path the node
    uses, but wired here so a node-side bug cannot hide."""
    cfg, _ = N._resolve_config_keyed("")
    t = OCIO.DisplayViewTransform(src=in_cs, display=display, view=view)
    t.setDirection(OCIO.TRANSFORM_DIR_INVERSE if invert else OCIO.TRANSFORM_DIR_FORWARD)
    cpu = cfg.getProcessor(t).getDefaultCPUProcessor()
    out = np.ascontiguousarray(rgb.astype(np.float32)).copy()
    flat = out.reshape(-1, 3)
    desc = OCIO.PackedImageDesc(flat, flat.shape[0], 1, 3)
    cpu.apply(desc)
    return out


def ref_cdl(slope, offset, power, sat, direction, rgb):
    """INDEPENDENT ground truth for OCIOCDLTransform: fresh CDLTransform processor, built here. The node
    resolves the built-in config (falls back to CreateRaw); CDL is config-independent, so use the same
    config resolve for parity."""
    cfg, _ = N._resolve_config_keyed("")
    if cfg is None:
        cfg = OCIO.Config.CreateRaw()
    t = OCIO.CDLTransform()
    t.setSlope(list(slope))
    t.setOffset(list(offset))
    t.setPower(list(power))
    t.setSat(float(sat))
    t.setDirection(OCIO.TRANSFORM_DIR_FORWARD if direction == "forward" else OCIO.TRANSFORM_DIR_INVERSE)
    cpu = cfg.getProcessor(t).getDefaultCPUProcessor()
    out = np.ascontiguousarray(rgb.astype(np.float32)).copy()
    flat = out.reshape(-1, 3)
    desc = OCIO.PackedImageDesc(flat, flat.shape[0], 1, 3)
    cpu.apply(desc)
    return out


# --------------------------------------------------------------------------- node path wrappers (torch in/out)
def as_tensor(rgb):
    """[...,3] float array -> torch IMAGE tensor [1, M, 1, 3] (batch of one, 1-pixel-tall row of M pixels).
    _apply_processor loops the batch and packs each frame with PackedImageDesc(rgb, w, h, 3), so any 2D
    [M,3] fixture is flattened to a single row here; the node output is squeezed back to [...,3]."""
    flat = np.ascontiguousarray(rgb.reshape(-1, 3).astype(np.float32))
    return torch.from_numpy(flat[None, :, None, :])   # [1, M, 1, 3]


def from_tensor(t, shape):
    return t.detach().cpu().numpy().reshape(shape).astype(np.float64)


def node_convert(in_cs, out_cs, rgb):
    out = IO._convert(as_tensor(rgb), in_cs, out_cs)
    return from_tensor(out, rgb.shape)


def node_colorspace(in_cs, out_cs, rgb):
    out = N.OCIOColorSpace().convert(as_tensor(rgb), in_cs, out_cs)[0]
    return from_tensor(out, rgb.shape)


def node_display(in_cs, display, view, invert, rgb):
    out = N.OCIODisplay().run(as_tensor(rgb), in_cs, display, view, invert)[0]
    return from_tensor(out, rgb.shape)


def node_cdl(slope, offset, power, sat, direction, rgb):
    out = N.OCIOCDLTransform().run(as_tensor(rgb),
                                   slope[0], slope[1], slope[2],
                                   offset[0], offset[1], offset[2],
                                   power[0], power[1], power[2],
                                   sat, direction)[0]
    return from_tensor(out, rgb.shape)


# --------------------------------------------------------------------------- load fixtures
FIX = C.FIXT_DIR
cc = np.load(os.path.join(FIX, "colorchecker_srgb.npy")).astype(np.float32)          # (24,3) sRGB 0..1
spectral = np.load(os.path.join(FIX, "spectral_sweep.npy")).astype(np.float32)       # (128,512,3)
hdr = np.load(os.path.join(FIX, "hdr_ramp.npy")).astype(np.float32)                   # (256,3) -0.25..16

# real nyc image (sRGB display 0..1)
import cv2  # noqa: E402
nyc_path = r"D:\n8n\projects\ComfyUI-OCIO\example_workflows\nyc_skyline.png"
nyc_bgr = cv2.imread(nyc_path, cv2.IMREAD_UNCHANGED)
if nyc_bgr is None:
    raise RuntimeError(f"could not read {nyc_path}")
nyc = nyc_bgr[..., :3][..., ::-1].astype(np.float32) / 255.0                          # BGR->RGB, 0..1

FIXTURES = {
    "colorchecker": cc,
    "spectral_sweep": spectral,
    "hdr_ramp": hdr,
    "nyc_skyline": nyc,
}


# --------------------------------------------------------------------------- transforms under test
# (label, node_fn, ref_fn)  -- each fn takes (rgb) and returns [...,3]
def mk_colorspace(in_cs, out_cs):
    return (lambda rgb: node_colorspace(in_cs, out_cs, rgb),
            lambda rgb: ref_colorspace(in_cs, out_cs, rgb))


def mk_convert(in_cs, out_cs):
    return (lambda rgb: node_convert(in_cs, out_cs, rgb),
            lambda rgb: ref_colorspace(in_cs, out_cs, rgb))


def mk_display(in_cs, disp, view, inv):
    return (lambda rgb: node_display(in_cs, disp, view, inv, rgb),
            lambda rgb: ref_display(in_cs, disp, view, inv, rgb))


def mk_cdl(slope, offset, power, sat, direction):
    return (lambda rgb: node_cdl(slope, offset, power, sat, direction, rgb),
            lambda rgb: ref_cdl(slope, offset, power, sat, direction, rgb))


DISP, VIEW = "sRGB - Display", "ACES 2.0 - SDR 100 nits (Rec.709)"

TRANSFORMS = {
    # colorspace conversions via the OCIOColorSpace node
    "ColorSpace: sRGB->ACEScg":       mk_colorspace("sRGB - Display", "ACEScg"),
    "ColorSpace: ACES2065-1->ACEScg": mk_colorspace("ACES2065-1", "ACEScg"),
    "ColorSpace: ACEScg->ACES2065-1": mk_colorspace("ACEScg", "ACES2065-1"),
    # the io_nodes._convert path (OCIORead/OCIOWrite/thumb use this)
    "_convert: sRGB->ACEScg":         mk_convert("sRGB - Display", "ACEScg"),
    "_convert: LinRec709->ACEScg":    mk_convert("Linear Rec.709 (sRGB)", "ACEScg"),
    # display + view (forward tone-map, and its inverse)
    "Display: ACEScg->sRGB(fwd)":     mk_display("ACEScg", DISP, VIEW, False),
    "Display: sRGB->ACEScg(inv)":     mk_display("ACEScg", DISP, VIEW, True),
    # CDL grade (forward + inverse)
    "CDL: grade(fwd)":                mk_cdl((1.1, 1.0, 0.9), (0.02, 0.0, -0.02), (0.95, 1.0, 1.05), 1.2, "forward"),
    "CDL: grade(inv)":                mk_cdl((1.1, 1.0, 0.9), (0.02, 0.0, -0.02), (0.95, 1.0, 1.05), 1.2, "inverse"),
}


# --------------------------------------------------------------------------- measure
results = {}      # transform -> {fixture -> {max_abs, rms}, "_agg": {...}}
worst_overall = {"transform": None, "fixture": None, "max_abs": -1.0}

for label, (node_fn, ref_fn) in TRANSFORMS.items():
    per_fix = {}
    agg_max, agg_sqsum, agg_n = 0.0, 0.0, 0
    for fname, rgb in FIXTURES.items():
        got = node_fn(rgb)
        exp = ref_fn(rgb)
        ma = C.max_abs(got, exp)
        rm = C.rms(got, exp)
        per_fix[fname] = {"max_abs": ma, "rms": rm,
                          "in_range": [float(np.min(rgb)), float(np.max(rgb))],
                          "out_range": [float(np.min(exp)), float(np.max(exp))]}
        agg_max = max(agg_max, ma)
        d = (got.astype(np.float64) - exp.astype(np.float64)).ravel()
        agg_sqsum += float(np.sum(d * d)); agg_n += d.size
        if ma > worst_overall["max_abs"]:
            worst_overall = {"transform": label, "fixture": fname, "max_abs": ma}
    per_fix["_agg"] = {"max_abs": agg_max, "rms": float(np.sqrt(agg_sqsum / agg_n))}
    results[label] = per_fix
    print(f"{label:34s}  max_abs={agg_max:.3e}  rms={per_fix['_agg']['rms']:.3e}")

THRESHOLD = 1e-4
labels = list(results.keys())
agg_max_vals = [results[k]["_agg"]["max_abs"] for k in labels]
n_over = sum(1 for v in agg_max_vals if v > THRESHOLD)
overall_max = max(agg_max_vals)

print()
print(f"transforms tested: {len(labels)}   fixtures each: {len(FIXTURES)}")
print(f"overall worst max_abs: {overall_max:.3e} at "
      f"{worst_overall['transform']} / {worst_overall['fixture']}")
print(f"transforms over {THRESHOLD:.0e} threshold: {n_over}")


# --------------------------------------------------------------------------- chart
fig, ax = C.new_fig(figsize=(11, 5.2))
# floor tiny/zero values so they render on a log axis (annotate them as "= 0 / <1e-12")
floor = 1e-12
plot_vals = [max(v, floor) for v in agg_max_vals]
colors = ["#e0556a" if v > THRESHOLD else "#4fd1c5" for v in agg_max_vals]
xs = np.arange(len(labels))
ax.bar(xs, plot_vals, color=colors, width=0.62, zorder=3)
ax.set_yscale("log")
ax.axhline(THRESHOLD, color="#f6c453", ls="--", lw=1.4, zorder=4,
           label=f"1e-4 parity threshold")
ax.set_xticks(xs)
ax.set_xticklabels(labels, rotation=38, ha="right", fontsize=7.5)
ax.set_ylabel("max abs error  (node vs raw PyOpenColorIO)")
ax.set_title("OCIO parity: pack node output vs independent PyOpenColorIO reference\n"
             f"config {N._resolve_config_keyed('')[0].getName()}  |  {len(FIXTURES)} fixtures/transform",
             fontsize=9.5)
ax.set_ylim(floor, max(THRESHOLD * 5, overall_max * 5))
ax.grid(axis="y", which="both", alpha=0.25, zorder=0)
# annotate exact value above each bar
for x, v in zip(xs, agg_max_vals):
    txt = "0" if v == 0.0 else f"{v:.1e}"
    ax.text(x, max(v, floor) * 1.4, txt, ha="center", va="bottom", fontsize=6.6, color="#cde")
ax.legend(loc="upper right", fontsize=8, framealpha=0.2)
plot_path = C.savefig(fig, "ocio_parity.png")
print("chart:", plot_path)


# --------------------------------------------------------------------------- dump json
payload = {
    "generated": "2026-07-03",
    "python": os.sys.executable,
    "ocio_version": OCIO.__version__,
    "config_name": N._resolve_config_keyed("")[0].getName(),
    "config_key": list(N._resolve_config_keyed("")[1]),
    "colorspace_count": len(list(N._resolve_config_keyed("")[0].getColorSpaces())),
    "threshold": THRESHOLD,
    "fixtures": {k: {"shape": list(v.shape), "range": [float(v.min()), float(v.max())]}
                 for k, v in FIXTURES.items()},
    "transforms": results,
    "summary": {
        "transforms_tested": len(labels),
        "fixtures_per_transform": len(FIXTURES),
        "overall_max_abs": overall_max,
        "worst": worst_overall,
        "transforms_over_threshold": n_over,
        "verdict": "PASS" if n_over == 0 else "CONCERN",
    },
    "plot_path": plot_path,
}
json_path = C.dump("ocio_parity.json", payload)
print("json:", json_path)
