"""Dependency-free verification of the arithmetic grade nodes (grade_nodes.py): OCIO Grade,
OCIO Grade Match, OCIO Apply Grade. Pure torch, no ComfyUI and no OpenColorIO needed - the grade
math operates directly on the IMAGE tensor. Checks:
  1. _apply_grade identity params is a true no-op (bit-identical).
  2. HDR-safe: values >1 and <0 survive by default (clamp_output=False).
  3. Sign-preserving gamma: negative input stays negative-signed after gamma != 1.
  4. Round-trip: OCIOGrade's grade_info fed into OCIOApplyGrade (strength=1.0, same source)
     reproduces OCIOGrade's own output.
  5. Grade Match: image = reference*k + b is matched back toward reference, per-channel mean gap
     reduced by a large fraction.
Run from the pack root: python tools/test_grade.py"""
import os
import sys

import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import grade_nodes as g

torch.manual_seed(0)
PASS = True


def check(name, cond, detail=""):
    global PASS
    status = "PASS" if cond else "FAIL"
    if not cond:
        PASS = False
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))


# --------------------------------------------------------------------------- 1. identity no-op

print("\n== 1. Identity params: true no-op ==")
img = torch.rand(2, 8, 8, 4, dtype=torch.float32)  # includes alpha
ident = g._identity_grade_info()
out = g._apply_grade(img[..., :3], ident)
check("identity RGB bit-identical", torch.equal(out, img[..., :3]),
     f"max abs diff = {(out - img[..., :3]).abs().max().item()}")

out_full = g._apply_grade_full(img, ident, mix=1.0, clamp_output=False)
check("identity full (incl. alpha) bit-identical", torch.equal(out_full, img),
     f"max abs diff = {(out_full - img).abs().max().item()}")


# --------------------------------------------------------------------------- 2. HDR-safe (no clamp)

print("\n== 2. HDR-safe: values >1 and <0 survive (clamp_output=False) ==")
hdr = torch.tensor([[-0.5, 2.5, 10.0]], dtype=torch.float32).reshape(1, 1, 1, 3)
grade_mild = dict(ident)
grade_mild["gain"] = [1.2, 1.2, 1.2]
grade_mild["offset"] = [0.01, 0.01, 0.01]
out_hdr = g._apply_grade(hdr, grade_mild)
has_below0 = bool((out_hdr < 0).any())
has_above1 = bool((out_hdr > 1).any())
check("negative value survives (not clamped to 0)", has_below0, f"out = {out_hdr.tolist()}")
check("above-1 value survives (not clamped to 1)", has_above1, f"out = {out_hdr.tolist()}")

out_hdr_full = g._apply_grade_full(hdr, grade_mild, mix=1.0, clamp_output=False)
check("full path (clamp_output=False) preserves HDR range", bool((out_hdr_full < 0).any()) and bool((out_hdr_full > 1).any()))

out_hdr_clamped = g._apply_grade_full(hdr, grade_mild, mix=1.0, clamp_output=True)
check("clamp_output=True actually clamps to [0,1]",
     bool((out_hdr_clamped >= 0).all()) and bool((out_hdr_clamped <= 1).all()),
     f"out = {out_hdr_clamped.tolist()}")


# --------------------------------------------------------------------------- 3. sign-preserving gamma

print("\n== 3. Sign-preserving gamma: negative input stays negative-signed ==")
neg = torch.tensor([[-0.3, -0.05, -1.5]], dtype=torch.float32).reshape(1, 1, 1, 3)
grade_gamma = dict(ident)
grade_gamma["gamma"] = [2.2, 2.2, 2.2]
out_gamma = g._apply_grade(neg, grade_gamma)
check("gamma-graded negative input is still negative", bool((out_gamma < 0).all()),
     f"in = {neg.tolist()}  out = {out_gamma.tolist()}")

# also confirm _signed_pow itself, directly, against a naive clamp(x,0)**e that WOULD crush to 0
naive_crushed = torch.clamp(neg, min=0.0) ** (1.0 / 2.2)
check("contrast against the naive clamp(x,0)**e crush", bool((naive_crushed == 0).all()) and bool((out_gamma != 0).all()),
     "naive clamp(x,0)**e crushes all-negative input to exactly 0; _signed_pow does not")


# --------------------------------------------------------------------------- 4. round-trip Grade -> Apply Grade

print("\n== 4. Round-trip: OCIOGrade grade_info -> OCIOApplyGrade reproduces OCIOGrade output ==")
src = torch.rand(1, 16, 16, 3, dtype=torch.float32) * 2.0 - 0.3  # spans negative and >1

node_grade = g.OCIOGrade()
graded_img, grade_json = node_grade.run(
    src, lift_r=0.02, lift_g=0.01, lift_b=0.0,
    gamma_r=1.1, gamma_g=0.9, gamma_b=1.0,
    gain_r=1.15, gain_g=1.0, gain_b=0.95,
    offset_r=0.01, offset_g=0.0, offset_b=-0.01,
    contrast=1.2, pivot=0.18, saturation=1.1,
    mix=1.0, clamp_output=False)

node_apply = g.OCIOApplyGrade()
applied_img, = node_apply.run(src, grade_info=grade_json, strength=1.0, mix=1.0, clamp_output=False)

diff = (graded_img - applied_img).abs().max().item()
check("round-trip matches within float tolerance", diff < 1e-5, f"max abs diff = {diff}")
check("grade_info round-trips through JSON", isinstance(grade_json, str) and '"type": "ocio_grade"' in grade_json.replace("'", '"') or "ocio_grade" in grade_json)

# strength=0 must be identity regardless of grade_info content
applied_zero, = node_apply.run(src, grade_info=grade_json, strength=0.0, mix=1.0, clamp_output=False)
check("strength=0.0 is a true no-op", torch.equal(applied_zero, src))

# malformed grade_info must pass through, never crash
applied_bad, = node_apply.run(src, grade_info="{not valid json", strength=1.0, mix=1.0, clamp_output=False)
check("malformed grade_info passes image through unchanged", torch.equal(applied_bad, src))

applied_empty, = node_apply.run(src, grade_info="", strength=1.0, mix=1.0, clamp_output=False)
check("empty grade_info passes image through unchanged", torch.equal(applied_empty, src))


# --------------------------------------------------------------------------- 5. Grade Match

print("\n== 5. Grade Match: image = reference*k + b matched back toward reference ==")
ref = torch.rand(1, 32, 32, 3, dtype=torch.float32) * 0.6 + 0.2  # well-behaved reference plate
k = torch.tensor([0.6, 0.6, 0.6])
b = torch.tensor([0.15, -0.05, 0.1])
shifted = ref * k.view(1, 1, 1, 3) + b.view(1, 1, 1, 3)

node_match = g.OCIOGradeMatch()
matched_img, match_json, note = node_match.run(shifted, ref, match_strength=1.0, mix=1.0, clamp_output=False)

ref_mean = ref[..., :3].reshape(-1, 3).mean(dim=0)
shifted_mean = shifted[..., :3].reshape(-1, 3).mean(dim=0)
matched_mean = matched_img[..., :3].reshape(-1, 3).mean(dim=0)

gap_before = (shifted_mean - ref_mean).abs()
gap_after = (matched_mean - ref_mean).abs()
reduction = 1.0 - (gap_after / gap_before.clamp(min=1e-6))
print(f"  ref mean:      {ref_mean.tolist()}")
print(f"  shifted mean:  {shifted_mean.tolist()}")
print(f"  matched mean:  {matched_mean.tolist()}")
print(f"  gap before:    {gap_before.tolist()}")
print(f"  gap after:     {gap_after.tolist()}")
print(f"  reduction:     {reduction.tolist()}")
print(f"  match note:    {note}")
check("mean gap reduced by a large fraction (>70%) per channel", bool((reduction > 0.7).all()),
     f"reduction = {reduction.tolist()}")

# tiny-std guard: a flat (zero-variance) image and reference must not blow up / NaN
flat_img = torch.full((1, 4, 4, 3), 0.5, dtype=torch.float32)
flat_ref = torch.full((1, 4, 4, 3), 0.7, dtype=torch.float32)
flat_out, flat_json, flat_note = node_match.run(flat_img, flat_ref, match_strength=1.0, mix=1.0, clamp_output=False)
check("flat image/reference (zero std) does not NaN", bool(torch.isfinite(flat_out).all()),
     f"out = {flat_out[0,0,0].tolist()}")

# batch broadcast: single-frame reference against a multi-frame image batch
batch_img = torch.rand(3, 8, 8, 3, dtype=torch.float32)
single_ref = torch.rand(1, 8, 8, 3, dtype=torch.float32)
batch_out, _, _ = node_match.run(batch_img, single_ref, match_strength=1.0, mix=1.0, clamp_output=False)
check("reference batch-broadcasts against a multi-frame image batch", batch_out.shape == batch_img.shape,
     f"out shape = {tuple(batch_out.shape)}")


# --------------------------------------------------------------------------- summary

print("\n" + ("ALL CHECKS PASSED" if PASS else "SOME CHECKS FAILED"))
sys.exit(0 if PASS else 1)
