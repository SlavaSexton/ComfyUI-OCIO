# -*- coding: utf-8 -*-
"""Latent decoding that keeps what the standard path throws away.

RESPONSIBLE FOR: giving a colour pipeline the VAE output as the model produced it (2026-08-12).

ComfyUI's VAEDecode finishes every decode with `image.add_(1).div_(2).clamp_(0, 1)`. The clamp is
the problem here: on a measured round-trip through the LTX-2.5 video VAE, 3.01% of the pixels landed
below zero and were flattened to black before any node could see them. Those values are the foot of
the curve - the part a colourist pulls detail out of - and once the clamp has run they do not exist.

The second issue is precision. ComfyUI runs this VAE in bfloat16; the reference pipeline for LTX-2
switches the VAE to float32 whenever the material is HDR (`vae_dtype_for_hdr` in ltx-pipelines).
bfloat16 carries 8 bits of mantissa, which is coarser than the 10-bit codes a broadcast master needs,
so the loss happens before the file format is even chosen.

This node does the same decode with both decisions made for finishing work rather than for preview:
values pass through unclamped, and the VAE can be run in float32.

WHAT THE OUTPUT MEANS. A latent decodes to whatever the generation encoded. For an ordinary SDR
generation that is display-referred RGB. For LTX-2's HDR path it is ACEScct log codes, which look
flat and grey until the curve is undone - feed those to OCIO LogConvert (Log to Linear, curve
`acescct`) to recover scene-linear values, then to OCIO Write. This node deliberately does not guess
which case it is looking at: guessing the encoding is how footage gets silently mangled.
"""

import torch


def _to_comfy_image(x):
    """VAE output -> ComfyUI IMAGE layout [B, H, W, C].

    A video VAE returns [B, T, H, W, C]; ComfyUI carries frames as a batch, so the time axis is
    folded into the batch. A still VAE already returns the right shape and is passed through.
    """
    if x.ndim == 5:
        return x.reshape(-1, x.shape[-3], x.shape[-2], x.shape[-1])
    return x


class OCIOVAEDecode:
    """Decode a latent without the 0..1 clamp, optionally in float32."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "samples": ("LATENT", {"tooltip": "The latent to decode, same input the stock VAE Decode takes."}),
                "vae": ("VAE", {"tooltip": "The VAE that belongs to the model. A VAE is trained together with its "
                                          "transformer and cannot be swapped for another one."}),
                "precision": (["float32", "model default"], {"default": "float32",
                              "tooltip": "float32 runs the VAE itself at full precision, which is what the reference "
                                         "LTX-2 pipeline does for HDR material. 'model default' keeps ComfyUI's "
                                         "choice (bfloat16 for LTX), which is faster and lighter but carries 8 bits "
                                         "of mantissa."}),
                "clamp": ("BOOLEAN", {"default": False, "label_on": "clamp to 0..1", "label_off": "keep everything",
                          "tooltip": "OFF (default) passes values through as the model produced them, including "
                                     "anything below 0 or above 1. ON reproduces the stock VAE Decode exactly - use "
                                     "it only to compare against the standard path."}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image/sequence/video",)
    OUTPUT_TOOLTIPS = ("The decoded frames, unclamped unless you asked otherwise. What the values MEAN depends on "
                       "how the generation encoded them: display RGB for SDR, ACEScct log codes on LTX-2's HDR path.",)
    FUNCTION = "decode"
    CATEGORY = "OCIO"
    DESCRIPTION = ("Decode a latent the way a finishing pipeline needs it: no 0..1 clamp, and the VAE optionally in "
                   "float32. The stock VAE Decode clamps every decode, which on a measured LTX-2.5 round-trip "
                   "flattened 3% of the pixels in the shadows to black before any colour node could reach them.")

    def decode(self, samples, vae, precision="float32", clamp=False):
        latent = samples["samples"]
        # Nested latents (audio-video models) carry the video track first, same as the stock node.
        if getattr(latent, "is_nested", False):
            latent = latent.unbind()[0]

        saved_output = vae.process_output
        saved_dtype = getattr(vae, "vae_dtype", None)
        raised_precision = False
        try:
            # The stock post-step is add(1)/div(2) followed by clamp(0, 1). Keep the mapping, drop the clamp:
            # the [-1, 1] convention is how the VAE was trained, the clamp is a display decision.
            if clamp:
                vae.process_output = lambda image: image.add_(1.0).div_(2.0).clamp_(0.0, 1.0)
            else:
                vae.process_output = lambda image: image.add(1.0).div(2.0)

            if precision == "float32" and saved_dtype is not None and saved_dtype != torch.float32:
                vae.vae_dtype = torch.float32
                vae.first_stage_model.to(torch.float32)
                raised_precision = True

            images = vae.decode(latent)
        finally:
            vae.process_output = saved_output
            if raised_precision:
                vae.vae_dtype = saved_dtype
                vae.first_stage_model.to(saved_dtype)

        return (_to_comfy_image(images),)


NODE_CLASS_MAPPINGS = {"OCIOVAEDecode": OCIOVAEDecode}
NODE_DISPLAY_NAME_MAPPINGS = {"OCIOVAEDecode": "OCIO VAE Decode"}
