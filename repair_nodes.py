"""OCIO Clip Repair - put a reconstructed HDR pass ONLY where the plate ran out of data.

RESPONSIBLE FOR: keeping a plate's own tone, texture and temporal stability everywhere it still
carries information, and taking pixels from a reconstruction pass only inside the clipped ends.

WHY THIS EXISTS. An SDR-to-HDR model (LumiPic on FLUX.2, LTX-2.3's HDR IC-LoRA, and the rest of that
family) rewrites the WHOLE frame. Measured on one shot: full-frame application moved channel balance
outside the clipped zones from R/B 2.34 to 1.44 - a visible tone shift on 90% of a picture that was
never damaged - softened shadow texture, and, run per frame over a clip, made the highlights flicker
(coefficient of variation 2.1% in the source frames, 22.7% after). None of that buys anything where
the plate was already fine. Compositing the pass through a mask of the clipped ends keeps the tone
(R/B back to 2.23), keeps the mid level (0.1085 plate against 0.1103 repaired), and keeps the
recovered range (peak 1.0 -> 445).

THE TWO ENDS ARE NOT SYMMETRICAL, and the defaults say so. Highlights genuinely reconstruct: a sun
disc that was a flat white patch comes back with a falloff, visible when the exposure is stopped
down. Shadows, on the same material, came back SMOOTHED - the plate's grainy-but-real texture was
replaced by a clean gradient, which reads as worse even though a local-contrast metric scores it
higher (the metric was measuring the mask edge, not detail). So `repair_shadows` defaults OFF, and
the tooltip says why rather than leaving it to be rediscovered.

THRESHOLDS ARE NOT FIXED AT 1.0. Detail dies before the code reaches white. Scanned on an 8-bit
plate, bands from 0.60 to 0.99 still held 88-139% of the mid-tone structure, while the dark end had
lost most of its own by 0.04 (30% at 0.000-0.004, 25% at 0.004-0.010, 49% at 0.020-0.040). That is
why both thresholds are widgets: the right value is a property of the plate, not a constant.
"""
import numpy as np
import torch

# Rec.709 luminance, applied to RGB.
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def _to_numpy(image):
    """ComfyUI IMAGE is [B,H,W,C] float32 in torch; work in numpy, return the same shape."""
    return image.detach().cpu().numpy().astype(np.float32)


def _luma(arr):
    return arr[..., :3] @ _LUMA


def _dilate(mask, radius):
    """Grow a mask by a square radius. Written with a max-filter rather than cv2 so the node
    carries no dependency the pack does not already have."""
    if radius <= 0:
        return mask
    out = mask.copy()
    for _ in range(radius):
        p = np.pad(out, 1, mode="edge")
        out = np.maximum.reduce([
            p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:], p[1:-1, 1:-1],
        ])
    return out


def _blur(mask, radius):
    """Separable box blur, repeated - a cheap Gaussian, and enough to hide a composite seam."""
    if radius <= 0:
        return mask
    k = max(1, int(radius))
    out = mask.astype(np.float32)
    for _ in range(3):
        pad = np.pad(out, ((0, 0), (k, k)), mode="edge")
        cs = np.cumsum(pad, axis=1)
        out = (cs[:, 2 * k:] - cs[:, :-2 * k]) / (2 * k)
        pad = np.pad(out, ((k, k), (0, 0)), mode="edge")
        cs = np.cumsum(pad, axis=0)
        out = (cs[2 * k:, :] - cs[:-2 * k, :]) / (2 * k)
    return np.clip(out, 0.0, 1.0)


class OCIOClipRepair:
    """Composite an HDR reconstruction into a plate's clipped ends only."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "plate": ("IMAGE", {"tooltip": "The original frames. Everything outside the mask comes "
                                           "from here unchanged, which is what preserves tone, "
                                           "texture and frame-to-frame stability."}),
            "reconstruction": ("IMAGE", {"tooltip": "Output of an SDR-to-HDR pass over the same "
                                                    "frames (LumiPic, LTX HDR IC-LoRA, ...). Used "
                                                    "only inside the mask."}),
            "repair_highlights": ("BOOLEAN", {"default": True,
                                              "tooltip": "Rebuild blown highlights. This is the end "
                                                         "that reconstructs well: a clipped sun "
                                                         "comes back with a falloff."}),
            "highlight_level": ("FLOAT", {"default": 0.97, "min": 0.50, "max": 1.0, "step": 0.005,
                                          "tooltip": "Repair above this code value. Detail usually "
                                                     "dies before 1.0, so values in the 0.90-0.99 "
                                                     "band are normal. Lower catches more, at the "
                                                     "cost of overwriting pixels that were fine."}),
            "repair_shadows": ("BOOLEAN", {"default": False,
                                           "tooltip": "OFF by default on purpose. Measured on real "
                                                      "material the reconstruction returned SMOOTH "
                                                      "shadows, replacing the plate's real (if "
                                                      "noisy) texture. Turn on only if your pass "
                                                      "demonstrably does better in the blacks."}),
            "shadow_level": ("FLOAT", {"default": 0.010, "min": 0.0, "max": 0.20, "step": 0.001,
                                       "tooltip": "Repair below this code value. A plate typically "
                                                  "loses most shadow structure by about 0.04."}),
            "grow": ("INT", {"default": 6, "min": 0, "max": 64,
                             "tooltip": "Expand the mask outward, so the repair starts slightly "
                                        "before the damage does."}),
            "feather": ("INT", {"default": 24, "min": 0, "max": 256,
                                "tooltip": "Soften the mask edge. This is what stops the composite "
                                           "reading as a seam."}),
            "match_levels": ("BOOLEAN", {"default": True,
                                         "tooltip": "Scale the reconstruction to the plate on "
                                                    "mid-tones before compositing, so the patch "
                                                    "sits at the plate's exposure instead of its "
                                                    "own."}),
            "threshold_space": (["display codes", "scene linear"],
                                {"default": "display codes",
                                 "tooltip": "Which numbers the two levels refer to. Clipping "
                                            "happens in DISPLAY codes, so the levels are quoted "
                                            "there by default and stay meaningful (0.97, 0.01) "
                                            "even when the plate arriving here is scene-linear - "
                                            "the node converts internally for the mask only, and "
                                            "composites in whatever space you gave it. Set to "
                                            "scene linear to read the levels as linear values "
                                            "instead."}),
        }}

    RETURN_TYPES = ("IMAGE", "MASK", "STRING")
    RETURN_NAMES = ("image", "repair mask", "report")
    FUNCTION = "repair"
    CATEGORY = "OCIO"
    DESCRIPTION = ("Composite an HDR reconstruction into a plate's clipped highlights and/or crushed "
                   "shadows only, leaving the rest of the frame untouched. Thresholds are widgets "
                   "because detail dies before the code reaches 1.0, and where it dies is a property "
                   "of the plate. Shadow repair is off by default: on measured material the "
                   "reconstruction smoothed the blacks rather than restoring their texture.")

    def repair(self, plate, reconstruction, repair_highlights, highlight_level,
               repair_shadows, shadow_level, grow, feather, match_levels,
               threshold_space="display codes"):
        p = _to_numpy(plate)
        r = _to_numpy(reconstruction)
        if r.shape[0] == 1 and p.shape[0] > 1:
            r = np.repeat(r, p.shape[0], axis=0)
        if r.shape[1:3] != p.shape[1:3]:
            raise ValueError(
                f"OCIO Clip Repair: plate is {p.shape[2]}x{p.shape[1]} but the reconstruction is "
                f"{r.shape[2]}x{r.shape[1]}. Resize one of them first - this node will not guess "
                f"which resampling you want on HDR data.")

        out = np.empty_like(p)
        masks = np.zeros(p.shape[:3], dtype=np.float32)
        notes = []
        hi_pct = lo_pct = mask_pct = 0.0
        gain_used = 1.0

        # Clipping happens in display codes, so the levels are quoted there. When the plate arrives
        # scene-linear (the usual case downstream of OCIO Read), the mask is built on a display
        # encoding of it; the COMPOSITE still runs in the space that was handed in, untouched.
        as_display = threshold_space == "display codes"
        linear_plate = as_display and float(p.max()) > 1.001

        for i in range(p.shape[0]):
            pl, rc = p[i], r[i]
            code = pl[..., :3].max(axis=2)
            if linear_plate:
                code = np.power(np.clip(code, 0.0, None), 1.0 / 2.4)
            m = np.zeros(code.shape, dtype=np.float32)
            if repair_highlights:
                hit = (code >= highlight_level).astype(np.float32)
                hi_pct += float(hit.mean()) * 100.0 / p.shape[0]
                m = np.maximum(m, hit)
            if repair_shadows:
                hit = (code <= shadow_level).astype(np.float32)
                lo_pct += float(hit.mean()) * 100.0 / p.shape[0]
                m = np.maximum(m, hit)

            m = _blur(_dilate(m, grow), feather)
            masks[i] = m
            mask_pct += float(m.mean()) * 100.0 / p.shape[0]

            src = rc
            if match_levels:
                lp, lr = _luma(pl), _luma(rc)
                band = (lp > max(shadow_level, 1e-4) * 4.0) & (lp < 0.5)
                if band.sum() > 64:
                    gain = float(np.median(lp[band]) / max(np.median(lr[band]), 1e-9))
                    gain = float(np.clip(gain, 0.05, 20.0))
                    src = rc * gain
                    gain_used = gain
                else:
                    notes.append("too few mid-tone pixels to match levels; used the pass as-is")

            m3 = m[..., None]
            out[i, ..., :3] = pl[..., :3] * (1.0 - m3) + src[..., :3] * m3
            if p.shape[3] > 3:
                out[i, ..., 3:] = pl[..., 3:]

        report = (f"OCIO Clip Repair: mask covers {mask_pct:.2f}% of frame "
                  f"(clipped: {hi_pct:.2f}% high, {lo_pct:.2f}% low), "
                  f"grow {grow} px, feather {feather} px")
        if match_levels:
            report += f", reconstruction scaled x{gain_used:.3f} on mid-tones"
        if not repair_highlights and not repair_shadows:
            report += "\n  note: both ends are off, so the plate passed through unchanged"
        if repair_shadows:
            report += ("\n  note: shadow repair is on. Check the blacks against the plate at +3 EV - "
                       "a reconstruction that smooths them is worse than the plate's own texture.")
        for t in notes:
            report += "\n  note: " + t

        return (torch.from_numpy(out).to(plate.device),
                torch.from_numpy(masks).to(plate.device),
                report)


NODE_CLASS_MAPPINGS = {"OCIOClipRepair": OCIOClipRepair}
NODE_DISPLAY_NAME_MAPPINGS = {"OCIOClipRepair": "OCIO Clip Repair"}
