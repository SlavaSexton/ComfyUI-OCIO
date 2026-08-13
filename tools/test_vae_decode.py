"""Regression: OCIO VAE Decode must mutate IN PLACE (run: python tools/test_vae_decode.py). No models needed.

ComfyUI calls `self.process_output(pixel_samples[x:x+batch_number])` at comfy/sd.py:1215 and THROWS THE RETURN
VALUE AWAY - it relies on the stock lambda `image.add_(1).div_(2).clamp_(0,1)` mutating the tensor. Our node
replaces that lambda, and on 2026-08-12 it replaced it with an OUT-OF-PLACE `image.add(1).div(2)`. The new
tensor was discarded, the raw VAE output survived untouched, and the node emitted values in the VAE's native
[-1, 1] instead of [0, 1]: three quarters of a dark frame came out negative, blacks crushed, highlights burnt.

Nothing caught it. py_compile passed, the package imported, the graph validated, /prompt succeeded, ffprobe
reported a technically perfect ProRes - the files were flawless and the pixels were wrong. It was found by a
human looking at the picture. This test makes that impossible to repeat: the fake VAE below discards the
return value exactly as ComfyUI does, so an out-of-place implementation fails here.

Also locks the two other promises of the node: clamp=True must reproduce the stock result exactly, and the
VAE's process_output / vae_dtype must be restored afterwards even when decode raises.
"""
import importlib.util
import os
import sys
import types

import torch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_vae_nodes():
    pkg = types.ModuleType("ocio_pkg")
    pkg.__path__ = [_ROOT]
    sys.modules["ocio_pkg"] = pkg
    spec = importlib.util.spec_from_file_location("ocio_pkg.vae_nodes", os.path.join(_ROOT, "vae_nodes.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ocio_pkg.vae_nodes"] = mod
    spec.loader.exec_module(mod)
    return mod


class DiscardingVAE:
    """A VAE that calls process_output the way comfy/sd.py:1215 does: on a slice, return value discarded.

    raw is what the decoder produced, in the [-1, 1] convention the stock post-step assumes.
    """

    def __init__(self, raw):
        self.raw = raw
        self.vae_dtype = torch.bfloat16
        self.first_stage_model = types.SimpleNamespace(to=lambda *a, **k: None)
        self.process_output = lambda image: image.add_(1.0).div_(2.0).clamp_(0.0, 1.0)
        self.raise_on_decode = False

    def decode(self, latent):
        if self.raise_on_decode:
            raise RuntimeError("decode blew up on purpose")
        px = self.raw.clone()
        for x in range(px.shape[0]):
            self.process_output(px[x:x + 1])          # RETURN DISCARDED, exactly like core
        return px


class UsingReturnVAE(DiscardingVAE):
    """The other core call style (sd.py:1104/1119/1123/1127) which DOES use the return value. In-place ops must
    satisfy this one too, since they mutate and return the same tensor."""

    def decode(self, latent):
        return self.process_output(self.raw.clone())


def main():
    vn = _load_vae_nodes()
    node = vn.OCIOVAEDecode()

    # A dark frame in the VAE's own convention: black is -1, with a little undershoot and overshoot.
    raw = torch.tensor([[[[-1.0, -1.0, -1.0], [-1.05, -0.9, -1.0]],
                         [[0.0, 0.5, 1.0], [1.10, 1.0, 0.2]]]], dtype=torch.float32)
    expect_noclamp = raw.add(1.0).div(2.0)            # [-0.025 .. 1.05]
    expect_clamped = expect_noclamp.clamp(0.0, 1.0)

    for label, cls in (("return discarded (sd.py:1215)", DiscardingVAE),
                       ("return used (sd.py:1104)", UsingReturnVAE)):
        vae = cls(raw)
        out, _rep = node.decode({"samples": torch.zeros(1)}, vae, precision="float32", clamp=False)
        assert out.shape == expect_noclamp.shape, f"{label}: shape {out.shape}"
        assert torch.allclose(out, expect_noclamp, atol=1e-6), (
            f"{label}: the [-1,1] -> [0,1] mapping did not reach the caller.\n"
            f"  got      {out.flatten().tolist()}\n  expected {expect_noclamp.flatten().tolist()}\n"
            "  This is the out-of-place bug: process_output must use add_/div_, not add/div.")
        assert float(out.min()) > -0.05, f"{label}: min {float(out.min())} - looks like raw [-1,1] output"
        print(f"[PASS] mapping applied, clamp off, {label}")

        out_c, _rep = node.decode({"samples": torch.zeros(1)}, cls(raw), precision="float32", clamp=True)
        assert torch.allclose(out_c, expect_clamped, atol=1e-6), (
            f"{label}: clamp=True must reproduce the stock result exactly, got {out_c.flatten().tolist()}")
        assert float(out_c.min()) >= 0.0 and float(out_c.max()) <= 1.0
        print(f"[PASS] clamp on reproduces the stock 0..1 result, {label}")

    # the unclamped path must actually KEEP what the clamp would have removed
    vae = DiscardingVAE(raw)
    out, _rep = node.decode({"samples": torch.zeros(1)}, vae, precision="float32", clamp=False)
    assert float(out.min()) < 0.0, "unclamped output lost the sub-zero sample"
    assert float(out.max()) > 1.0, "unclamped output lost the above-one sample"
    print(f"[PASS] out-of-range survives: {float(out.min()):+.4f} .. {float(out.max()):+.4f}")

    # process_output and vae_dtype must be restored, including when decode raises
    vae = DiscardingVAE(raw)
    original = vae.process_output
    node.decode({"samples": torch.zeros(1)}, vae, precision="float32", clamp=False)
    assert vae.process_output is original, "process_output was not restored after a normal decode"
    print("[PASS] process_output restored after a normal decode")

    vae = DiscardingVAE(raw)
    vae.raise_on_decode = True
    original = vae.process_output
    saved_dtype = vae.vae_dtype
    try:
        node.decode({"samples": torch.zeros(1)}, vae, precision="float32", clamp=False)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the fake decode was supposed to raise")
    assert vae.process_output is original, "process_output was NOT restored after decode raised"
    assert vae.vae_dtype == saved_dtype, f"vae_dtype was left at {vae.vae_dtype}, not restored to {saved_dtype}"
    print("[PASS] process_output and vae_dtype restored even when decode raises")

    # a video VAE returns [B, T, H, W, C]; the time axis must fold into the batch
    raw5 = torch.zeros((1, 3, 2, 2, 3), dtype=torch.float32) - 1.0
    out5, _rep = node.decode({"samples": torch.zeros(1)}, DiscardingVAE(raw5), precision="float32", clamp=False)
    assert out5.shape == (3, 2, 2, 3), f"5-D video output folded wrongly: {out5.shape}"
    assert torch.allclose(out5, torch.zeros_like(out5), atol=1e-6), "mapping not applied on the 5-D path"
    print("[PASS] 5-D video output folds T into the batch and is mapped")

    print("\nALL CHECKS PASSED - OCIO VAE Decode mutates in place, so the mapping reaches the caller")


if __name__ == "__main__":
    main()
