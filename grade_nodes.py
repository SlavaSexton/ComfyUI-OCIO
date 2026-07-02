# ComfyUI-OCIO - arithmetic grade nodes (lift/gamma/gain/offset, shot match, apply-elsewhere).
# @author Slava Sexton, 2026-07-01
#
# Three nodes: OCIO Grade, OCIO Grade Match, OCIO Apply Grade. All IMAGE -> IMAGE, float, HDR-safe
# (never clamp to 0..1 unless clamp_output is explicitly set True). Pure torch arithmetic on the
# ComfyUI IMAGE tensor [B,H,W,C] - no OCIO config needed, these are grade-math nodes, not colorspace
# conversions. Alpha (channel 3, if present) is preserved untouched.
#
# Shared grade_info JSON schema (produced by Grade and Grade Match, consumed by Apply Grade):
#   {"type": "ocio_grade", "lift": [r,g,b], "gamma": [r,g,b], "gain": [r,g,b], "offset": [r,g,b],
#    "contrast": c, "pivot": p, "saturation": s}
#
# Sign-preserving power is used everywhere an exponent hits possibly-negative scene-linear data
# (contrast about pivot, gamma): torch.sign(x) * clamp(|x|, min=eps) ** e. A plain clamp(x, 0) ** e
# would crush negative values (below-black, common in scene-linear HDR/ACEScg) to zero and destroy
# information a downstream grade could still recover - that is a real HDR-safety bug, not a style
# choice, so every power op in this file goes through _signed_pow.

import json

import torch

CATEGORY = "OCIO"

_EPS = 1e-10

# Rec.709 luma weights: ComfyUI's working IMAGE tensor is Rec.709-primaried (sRGB primaries without
# the sRGB OETF applied - i.e. "linear Rec.709" for scene-linear graphs, or gamma-encoded Rec.709 for
# display-referred graphs). Rec.709 luma weights are the correct weights for a Rec.709-primaried
# saturation op in either case, since luma coefficients are a function of primaries, not encoding.
_LUMA_R, _LUMA_G, _LUMA_B = 0.2126, 0.7152, 0.0722


def _signed_pow(x, e):
    """Sign-preserving power: sign(x) * |x|^e. Keeps negative scene-linear values negative-signed
    instead of crushing them to 0 (what clamp(x, 0)**e would do) - required for HDR-safe grading of
    below-black / out-of-gamut pixels that a plain power op would otherwise destroy irrecoverably."""
    return torch.sign(x) * torch.clamp(x.abs(), min=_EPS) ** e


# --------------------------------------------------------------------------- grade_info schema

def _identity_grade_info():
    return {"type": "ocio_grade", "lift": [0.0, 0.0, 0.0], "gamma": [1.0, 1.0, 1.0],
            "gain": [1.0, 1.0, 1.0], "offset": [0.0, 0.0, 0.0], "contrast": 1.0, "pivot": 0.18,
            "saturation": 1.0}


def _grade_info_to_json(d):
    return json.dumps(d)


def _grade_info_from_json(s):
    """Parse a grade_info JSON string. Returns (dict, error) - error is None on success. Never
    raises: a malformed / missing string returns the identity grade_info plus an error message, so
    a broken upstream string degrades to a no-op instead of crashing the graph."""
    if not s or not str(s).strip():
        return _identity_grade_info(), "empty grade_info: passing image through unchanged"
    try:
        d = json.loads(s)
        if not isinstance(d, dict):
            return _identity_grade_info(), f"grade_info is not a JSON object: {type(d).__name__}"
        out = _identity_grade_info()
        for key in ("lift", "gamma", "gain", "offset"):
            v = d.get(key, out[key])
            if isinstance(v, (list, tuple)) and len(v) == 3:
                out[key] = [float(v[0]), float(v[1]), float(v[2])]
        for key in ("contrast", "pivot", "saturation"):
            if key in d:
                out[key] = float(d[key])
        return out, None
    except Exception as e:
        return _identity_grade_info(), f"malformed grade_info JSON ({e}): passing image through unchanged"


def _lerp_grade_info(d, strength):
    """Lerp a grade_info dict toward identity by (1 - strength). strength=1.0 -> d unchanged;
    strength=0.0 -> identity (true no-op)."""
    if strength >= 1.0:
        return d
    s = max(0.0, min(1.0, float(strength)))
    ident = _identity_grade_info()
    out = dict(d)
    for key in ("lift", "gamma", "gain", "offset"):
        out[key] = [ident[key][i] + (d[key][i] - ident[key][i]) * s for i in range(3)]
    for key in ("contrast", "saturation"):
        out[key] = ident[key] + (d[key] - ident[key]) * s
    out["pivot"] = d.get("pivot", ident["pivot"])
    return out


# --------------------------------------------------------------------------- shared grade core

def _apply_grade(img_rgb, params):
    """Apply a lift/gamma/gain/offset/contrast/saturation grade to an RGB tensor [..., 3].

    Order (documented, matches the standard ASC-CDL-plus-gamma "LGG" grading pipeline used in Nuke's
    Grade node and most DI tools):
      1. Offset  (add):               x = x + offset
      2. Lift    (shadow lift):       x = x + lift*(1-x)   [pushes black up without moving white;
                                       the standard Nuke/Resolve lift form - lift=1 maps x=0 to x=1,
                                       lift=0 is a no-op, matching "lift raises the black point"]
      3. Gain    (multiply):          x = x * gain
      4. Contrast about pivot:        x = pivot + (x-pivot)*contrast_signed_pow   [sign-preserving;
                                       pivot defaults to 0.18, scene-linear mid-gray (18% reflectance)]
      5. Gamma   (sign-preserving power): x = signed_pow(x, 1/gamma)
      6. Saturation (luminance-preserving, Rec.709 luma 0.2126/0.7152/0.0722): x = luma + (x-luma)*sat

    Identity params (lift=0, gamma=1, gain=1, offset=0, contrast=1, saturation=1) is an exact no-op:
    each step is skipped when its params are identity, so the fast path returns img_rgb unchanged
    (same tensor values, not just "close").
    """
    lift = params["lift"]
    gamma = params["gamma"]
    gain = params["gain"]
    offset = params["offset"]
    contrast = params["contrast"]
    pivot = params["pivot"]
    saturation = params["saturation"]

    lift_is_id = lift == [0.0, 0.0, 0.0]
    gamma_is_id = gamma == [1.0, 1.0, 1.0]
    gain_is_id = gain == [1.0, 1.0, 1.0]
    offset_is_id = offset == [0.0, 0.0, 0.0]
    contrast_is_id = float(contrast) == 1.0
    sat_is_id = float(saturation) == 1.0

    if lift_is_id and gamma_is_id and gain_is_id and offset_is_id and contrast_is_id and sat_is_id:
        return img_rgb  # true no-op fast path, bit-identical

    x = img_rgb

    # 1. Offset (add)
    if not offset_is_id:
        off = torch.tensor(offset, dtype=x.dtype, device=x.device)
        x = x + off

    # 2. Lift (shadow lift): x + lift*(1-x). lift=0 -> no-op; lift=1 -> flat 1.0.
    if not lift_is_id:
        lft = torch.tensor(lift, dtype=x.dtype, device=x.device)
        x = x + lft * (1.0 - x)

    # 3. Gain (multiply)
    if not gain_is_id:
        gn = torch.tensor(gain, dtype=x.dtype, device=x.device)
        x = x * gn

    # 4. Contrast about pivot (sign-preserving power on the pivot-relative distance, not the raw
    # value - the distance can be negative even where x itself is positive, e.g. shadows below pivot).
    # _signed_pow(d, contrast) already returns sign(d)*|d|^contrast, so this is a single clean step:
    # no separate sign multiply needed.
    if not contrast_is_id:
        p = float(pivot)
        d = x - p
        x = p + _signed_pow(d, float(contrast))

    # 5. Gamma (sign-preserving power, exponent = 1/gamma so gamma>1 brightens midtones, matching the
    # conventional monitor-gamma sense used by Nuke/Resolve/CDL "power").
    if not gamma_is_id:
        g = torch.tensor(gamma, dtype=x.dtype, device=x.device)
        inv_g = 1.0 / torch.clamp(g, min=_EPS)
        x = _signed_pow(x, inv_g)

    # 6. Saturation (luminance-preserving, Rec.709 luma weights - see module comment on why Rec.709
    # is correct regardless of linear vs display encoding: it is a function of primaries only).
    if not sat_is_id:
        luma = (x[..., 0:1] * _LUMA_R + x[..., 1:2] * _LUMA_G + x[..., 2:3] * _LUMA_B)
        x = luma + (x - luma) * float(saturation)

    return x


def _apply_grade_full(image, params, mix, clamp_output=False):
    """Apply _apply_grade to an IMAGE tensor [B,H,W,C], preserving alpha (channel 3+) untouched,
    blending with the original via mix (same 0..1 blend-with-original convention as nodes.py's
    _blend), and optionally clamping the RGB result to [0,1] (off by default - HDR-safe)."""
    rgb = image[..., :3]
    graded = _apply_grade(rgb, params)
    if clamp_output:
        graded = torch.clamp(graded, 0.0, 1.0)
    graded = _blend(rgb, graded, mix)
    if image.shape[-1] > 3:
        out = torch.cat([graded, image[..., 3:]], dim=-1)
    else:
        out = graded
    return out


def _mix_input():
    return ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01,
                      "tooltip": "Blend with the original: 1.0 = full effect, 0.0 = bypass."})


def _blend(orig, new, mix):
    mix = float(max(0.0, min(1.0, mix)))
    if mix >= 1.0:
        return new
    if mix <= 0.0:
        return orig
    return orig * (1.0 - mix) + new * mix


def _clamp_output_input():
    return ("BOOLEAN", {"default": False, "label_on": "Clamp to 0..1", "label_off": "HDR (no clamp)",
                        "tooltip": "Clamp the graded RGB to 0..1. Default off: values above 1.0 or "
                                   "below 0.0 (scene-linear HDR, out-of-gamut) pass through unclipped."})


# --------------------------------------------------------------------------- CIE Lab helpers (Grade Match)

# sRGB (Rec.709 primaries, D65 white) linear RGB -> CIE XYZ (D65), the standard IEC 61966-2-1 / Rec.709
# matrix (same primaries and white point as ComfyUI's working space). Row-major, applied as M @ rgb.
_RGB_TO_XYZ = ((0.4124564, 0.3575761, 0.1804375),
               (0.2126729, 0.7151522, 0.0721750),
               (0.0193339, 0.1191920, 0.9503041))
# Inverse of the matrix above (standard sRGB D65 XYZ -> linear RGB matrix).
_XYZ_TO_RGB = ((3.2404542, -1.5371385, -0.4985314),
               (-0.9692660, 1.8760108, 0.0415560),
               (0.0556434, -0.2040259, 1.0572252))
# CIE standard illuminant D65 reference white (2 degree observer), used to normalize XYZ before the
# Lab f() nonlinearity.
_D65_XN, _D65_YN, _D65_ZN = 0.95047, 1.00000, 1.08883

_LAB_DELTA = 6.0 / 29.0  # CIE Lab piecewise breakpoint


def _rgb_to_xyz(rgb):
    m = torch.tensor(_RGB_TO_XYZ, dtype=rgb.dtype, device=rgb.device)
    return rgb @ m.T


def _xyz_to_rgb(xyz):
    m = torch.tensor(_XYZ_TO_RGB, dtype=xyz.dtype, device=xyz.device)
    return xyz @ m.T


def _lab_f(t):
    """CIE Lab forward nonlinearity f(t), sign-preserving so a slightly-negative XYZ (out-of-gamut
    scene-linear input, common after gain/offset drift) does not crash or silently clip - it is
    treated as a small negative excursion around the piecewise breakpoint rather than crushed to 0."""
    t_abs = t.abs()
    cube_root = _signed_pow(t, 1.0 / 3.0)
    linear = t / (3.0 * _LAB_DELTA ** 2) + 4.0 / 29.0
    return torch.where(t_abs > _LAB_DELTA ** 3, cube_root, linear)


def _rgb_to_lab(rgb):
    """Linearize is skipped on purpose: this operates on the IMAGE tensor as-is (whatever space it is
    already in - scene-linear or display-encoded), treating it as 'linear RGB' input to the sRGB/Rec.709
    primaries matrix. Grade Match is a relative shot-matcher (mean/std in Lab space moved toward a
    reference), not an absolute colorimetric pipeline, so this is a principled simplification: matching
    two images that are already in the same space (both scene-linear, or both display-encoded, as they
    are for two frames flowing through the same ComfyUI graph) is invariant to that choice, since both
    sides go through the same forward map. Matrices: standard sRGB/Rec.709 D65 (IEC 61966-2-1), same
    primaries ComfyUI's IMAGE tensor uses. Cited above at _RGB_TO_XYZ / _XYZ_TO_RGB."""
    xyz = _rgb_to_xyz(rgb)
    x = _lab_f(xyz[..., 0:1] / _D65_XN)
    y = _lab_f(xyz[..., 1:2] / _D65_YN)
    z = _lab_f(xyz[..., 2:3] / _D65_ZN)
    L = 116.0 * y - 16.0
    a = 500.0 * (x - y)
    b = 200.0 * (y - z)
    return torch.cat([L, a, b], dim=-1)


def _lab_f_inv(t):
    """Inverse of _lab_f: sign-preserving cube (t**3 for |t| above the breakpoint, linear below),
    exact algebraic inverse of the CIE Lab piecewise nonlinearity."""
    t_abs = t.abs()
    cube = torch.sign(t) * t_abs ** 3
    linear = 3.0 * _LAB_DELTA ** 2 * (t - 4.0 / 29.0)
    return torch.where(t_abs > _LAB_DELTA, cube, linear)


def _lab_to_rgb(lab):
    """Exact algebraic inverse of _rgb_to_lab (same matrices, same D65 white, cited there)."""
    L, a, b = lab[..., 0:1], lab[..., 1:2], lab[..., 2:3]
    y = (L + 16.0) / 116.0
    x = y + a / 500.0
    z = y - b / 200.0
    xyz = torch.cat([_lab_f_inv(x) * _D65_XN, _lab_f_inv(y) * _D65_YN, _lab_f_inv(z) * _D65_ZN], dim=-1)
    return _xyz_to_rgb(xyz)


_MATCH_STD_MIN = 1e-4  # guard: below this std, a channel is treated as flat (skip scaling, avoid /0)


def _compute_match_grade(image_rgb, reference_rgb, match_strength):
    """Shot-to-shot color match, Reinhard-style statistical transfer in CIE Lab:

      1. Convert both image and reference RGB to Lab.
      2. Match image's per-channel (L, a, b) mean/std to reference's: normalize image to zero mean /
         unit std, then rescale/shift to reference's mean/std (classic Reinhard/Pitie color transfer,
         done in Lab because Lab's L/a/b axes are closer to perceptually independent than RGB, so
         matching them independently distorts hue/chroma less than matching RGB channels directly).
      3. Convert the matched Lab back to RGB.
      4. Express the RGB-space change (original image RGB -> matched RGB) as a per-channel gain +
         offset: fit matched = gain*original + offset by least squares per channel over all pixels.
         This is what maps the Lab-domain match onto the grade_info schema (gain/offset primarily;
         gamma=1, lift=0 - a full nonlinear Lab match has no exact global gain/offset equivalent, so
         the linear fit is the principled projection of "move image's stats toward reference's" onto
         this pack's grade vocabulary).
      5. Scale the fitted (gain, offset) toward identity (gain=1, offset=0) by match_strength.

    Tiny reference/image std (near-flat channel) is guarded at _MATCH_STD_MIN: that channel is left
    at gain=1/offset=0 rather than dividing by ~0 and blowing up the correction.

    Returns (grade_info_dict, stats_dict) where stats_dict carries the raw Lab means for reporting.
    """
    img_lab = _rgb_to_lab(image_rgb.reshape(-1, 3))
    ref_lab = _rgb_to_lab(reference_rgb.reshape(-1, 3))

    img_mean = img_lab.mean(dim=0)
    img_std = img_lab.std(dim=0)
    ref_mean = ref_lab.mean(dim=0)
    ref_std = ref_lab.std(dim=0)

    safe_img_std = torch.clamp(img_std, min=_MATCH_STD_MIN)
    scale = torch.where(img_std > _MATCH_STD_MIN, ref_std / safe_img_std, torch.ones_like(img_std))

    matched_lab = (img_lab - img_mean) * scale + ref_mean
    matched_rgb = _lab_to_rgb(matched_lab).clamp(min=-4.0, max=4.0)  # guard fit against extreme outliers

    # _apply_grade's pipeline order is (x + offset) * gain = gain*x + gain*offset (offset precedes
    # gain - see _apply_grade step 1 vs step 3). A per-channel least-squares fit of
    # matched = fit_gain*original + fit_offset therefore needs fit_offset re-expressed as
    # offset = fit_offset / fit_gain to land correctly once run back through _apply_grade.
    orig_rgb = image_rgb.reshape(-1, 3)
    gain = [1.0, 1.0, 1.0]
    offset = [0.0, 0.0, 0.0]
    for c in range(3):
        xo = orig_rgb[:, c]
        xm = matched_rgb[:, c]
        var = torch.var(xo, unbiased=False)
        if float(var) > _MATCH_STD_MIN ** 2:
            cov = torch.mean((xo - xo.mean()) * (xm - xm.mean()))
            fit_gain = float(cov / var)
            fit_offset = float(xm.mean() - fit_gain * xo.mean())
            if abs(fit_gain) > _MATCH_STD_MIN:
                gain[c] = fit_gain
                offset[c] = fit_offset / fit_gain

    s = max(0.0, min(1.0, float(match_strength)))
    gain = [1.0 + (g - 1.0) * s for g in gain]
    offset = [o * s for o in offset]

    grade = _identity_grade_info()
    grade["gain"] = gain
    grade["offset"] = offset
    stats = {"image_lab_mean": [float(v) for v in img_mean], "reference_lab_mean": [float(v) for v in ref_mean],
              "image_lab_std": [float(v) for v in img_std], "reference_lab_std": [float(v) for v in ref_std]}
    return grade, stats


# --------------------------------------------------------------------------- nodes

class OCIOGrade:
    """Lift / gamma / gain / offset + contrast + saturation grade (Nuke: Grade). Per-channel primary
    controls plus a global contrast-about-pivot and a luminance-preserving saturation. HDR-safe: never
    clamps unless clamp_output is turned on. Emits grade_info (JSON) so the exact grade can be
    re-applied elsewhere via OCIO Apply Grade - "grade once, stamp across the sequence"."""

    @classmethod
    def INPUT_TYPES(cls):
        lift_f = lambda: ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.001,
                          "tooltip": "Shadow lift: raises the black point without moving white. 0 = no-op."})
        gamma_f = lambda: ("FLOAT", {"default": 1.0, "min": 0.01, "max": 4.0, "step": 0.001,
                          "tooltip": "Gamma (sign-preserving power, exponent 1/gamma). 1 = no-op; >1 brightens midtones."})
        gain_f = lambda: ("FLOAT", {"default": 1.0, "min": 0.0, "max": 4.0, "step": 0.001,
                          "tooltip": "Gain: multiplies the channel. 1 = no-op."})
        offset_f = lambda: ("FLOAT", {"default": 0.0, "min": -1.0, "max": 1.0, "step": 0.001,
                          "tooltip": "Offset: adds a constant to the channel. 0 = no-op."})
        return {"required": {
            "image": ("IMAGE",),
            "lift_r": lift_f(), "lift_g": lift_f(), "lift_b": lift_f(),
            "gamma_r": gamma_f(), "gamma_g": gamma_f(), "gamma_b": gamma_f(),
            "gain_r": gain_f(), "gain_g": gain_f(), "gain_b": gain_f(),
            "offset_r": offset_f(), "offset_g": offset_f(), "offset_b": offset_f(),
            "contrast": ("FLOAT", {"default": 1.0, "min": 0.01, "max": 4.0, "step": 0.001,
                        "tooltip": "Contrast about pivot (sign-preserving power). 1 = no-op."}),
            "pivot": ("FLOAT", {"default": 0.18, "min": -1.0, "max": 4.0, "step": 0.001,
                        "tooltip": "Contrast pivot point. Default 0.18 = scene-linear mid-gray (18% reflectance)."}),
            "saturation": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 4.0, "step": 0.001,
                        "tooltip": "Saturation, luminance-preserving (Rec.709 luma weights). 1 = no-op, 0 = grayscale."}),
            "mix": _mix_input(),
            "clamp_output": _clamp_output_input(),
        }}

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "grade_info")
    OUTPUT_TOOLTIPS = ("Image with the lift/gamma/gain/offset grade applied.",
                       "JSON grade_info describing this grade - wire into OCIO Apply Grade to stamp the same grade elsewhere.")
    FUNCTION = "run"
    CATEGORY = CATEGORY

    def run(self, image, lift_r, lift_g, lift_b, gamma_r, gamma_g, gamma_b, gain_r, gain_g, gain_b,
            offset_r, offset_g, offset_b, contrast, pivot, saturation, mix=1.0, clamp_output=False):
        grade = {"type": "ocio_grade", "lift": [lift_r, lift_g, lift_b], "gamma": [gamma_r, gamma_g, gamma_b],
                 "gain": [gain_r, gain_g, gain_b], "offset": [offset_r, offset_g, offset_b],
                 "contrast": float(contrast), "pivot": float(pivot), "saturation": float(saturation)}
        out = _apply_grade_full(image, grade, mix, clamp_output)
        return (out, _grade_info_to_json(grade))


class OCIOGradeMatch:
    """Shot-to-shot color match: moves image's CIE Lab mean/std toward reference's (Reinhard-style
    statistical transfer), expressed back onto the grade_info schema as a per-channel gain + offset
    (gamma=1, lift=0). match_strength scales the correction toward identity. See _compute_match_grade
    for the full method. Emits grade_info so the SAME correction can be stamped across a sequence via
    OCIO Apply Grade, instead of re-deriving stats per frame."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "reference": ("IMAGE",),
            "match_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01,
                        "tooltip": "How much of the computed correction to apply: 1.0 = full match, 0.0 = no correction."}),
            "mix": _mix_input(),
            "clamp_output": _clamp_output_input(),
        }}

    RETURN_TYPES = ("IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("image", "grade_info", "matched")
    OUTPUT_TOOLTIPS = ("Image color-matched toward the reference.",
                       "JSON grade_info for the computed correction - wire into OCIO Apply Grade to stamp the same match elsewhere.",
                       "Human-readable note on the match (Lab mean/std moved, per channel).")
    FUNCTION = "run"
    CATEGORY = CATEGORY

    def run(self, image, reference, match_strength=1.0, mix=1.0, clamp_output=False):
        ref = reference
        if ref.shape[0] != image.shape[0] and ref.shape[0] == 1:
            ref = ref.expand(image.shape[0], -1, -1, -1)
        grade, stats = _compute_match_grade(image[..., :3], ref[..., :3], match_strength)
        out = _apply_grade_full(image, grade, mix, clamp_output)
        note = (f"Lab mean image->ref: L {stats['image_lab_mean'][0]:.3f}->{stats['reference_lab_mean'][0]:.3f}, "
                f"a {stats['image_lab_mean'][1]:.3f}->{stats['reference_lab_mean'][1]:.3f}, "
                f"b {stats['image_lab_mean'][2]:.3f}->{stats['reference_lab_mean'][2]:.3f}; "
                f"gain={[round(g, 4) for g in grade['gain']]} offset={[round(o, 4) for o in grade['offset']]} "
                f"at strength={match_strength:.2f}")
        return (out, _grade_info_to_json(grade), note)


class OCIOApplyGrade:
    """Apply a previously-computed grade_info (from OCIO Grade or OCIO Grade Match) to a new image
    ("grade once, stamp across the sequence"). strength lerps the correction toward identity. Tolerates
    a malformed / missing grade_info string: passes the image through unchanged and prints a warning,
    never raises."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "grade_info": ("STRING", {"default": "", "multiline": True,
                        "tooltip": "grade_info JSON from OCIO Grade or OCIO Grade Match. Wire it in, or paste it."}),
            "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01,
                        "tooltip": "Lerps the grade toward identity: 1.0 = full grade, 0.0 = no-op."}),
            "mix": _mix_input(),
            "clamp_output": _clamp_output_input(),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    OUTPUT_TOOLTIPS = ("Image with the parsed grade_info applied at the given strength.",)
    FUNCTION = "run"
    CATEGORY = CATEGORY

    def run(self, image, grade_info, strength=1.0, mix=1.0, clamp_output=False):
        grade, err = _grade_info_from_json(grade_info)
        if err:
            print(f"[OCIO Apply Grade] {err}")
            return (image,)
        grade = _lerp_grade_info(grade, strength)
        out = _apply_grade_full(image, grade, mix, clamp_output)
        return (out,)


NODE_CLASS_MAPPINGS = {
    "OCIOGrade": OCIOGrade,
    "OCIOGradeMatch": OCIOGradeMatch,
    "OCIOApplyGrade": OCIOApplyGrade,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "OCIOGrade": "OCIO Grade",
    "OCIOGradeMatch": "OCIO Grade Match",
    "OCIOApplyGrade": "OCIO Apply Grade",
}
