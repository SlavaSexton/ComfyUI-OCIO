# `OCIO Clip Repair`: putting a reconstruction back into a plate

This node composites an SDR-to-HDR reconstruction into the plate it came from, **only where the plate is
damaged**, and leaves the rest of the frame alone.

That restriction is the whole node. A reconstruction pass rewrites the entire frame, and on real material the
rewrite is not neutral: measured on one shot, applying it full-frame moved the channel balance of the whole
picture, while the masked composite kept it (R/B back to 2.23), kept the mid level (0.1085 in the plate against
0.1103 repaired) and kept the plate's own texture everywhere the plate was fine.

## Inputs

| Widget | Type | Default | What it does |
| --- | --- | --- | --- |
| `plate` | IMAGE | - | The original frames. Everything outside the mask comes from here, unchanged. |
| `reconstruction` | IMAGE | - | The output of an SDR-to-HDR pass over the **same** frames. |
| `repair_highlights` | BOOLEAN | **true** | Rebuild blown highlights. This is the end that reconstructs well. |
| `highlight_level` | FLOAT, 0.50-1.0 | 0.97 | Repair above this code. Detail dies before 1.0, so 0.90-0.99 is the normal band. |
| `repair_shadows` | BOOLEAN | **false** | Off on purpose: on measured material the reconstruction returned smooth shadows, not detailed ones. |
| `shadow_level` | FLOAT | 0.01 | Repair below this code. A plate usually loses most shadow structure by about 0.04. |
| `grow` | INT | 6 | Expand the mask outward, so the repair starts slightly before the damage does. |
| `feather` | INT | 24 | Soften the mask edge. This is what stops the composite reading as a seam. |
| `match_levels` | BOOLEAN | true | Scale the reconstruction to the plate on mid-tones before compositing. |
| `plate_space` | combo | `display codes` | `display codes` or `scene linear`. The node cannot detect this, and getting it wrong is the commonest way to a bad result. |

Outputs: `image:IMAGE`, `repair mask:MASK`, `report:STRING`.

## Choosing `highlight_level`, which is the setting that decides the result

**Do not pick it against 1.0.** Comparing the reconstruction to the plate's ceiling is the intuitive move and it
gives an answer that is far too conservative, because the plate is nowhere near 1.0 in the band you care about.
Measured on one shot as plate median to reconstruction median:

| band | plate | reconstruction | gain |
| --- | --- | --- | --- |
| 0.70-0.80 | 0.52 | 0.79 | 1.5x |
| 0.80-0.85 | 0.64 | 1.26 | 2.0x |
| 0.85-0.90 | 0.73 | 1.81 | 2.5x |
| 0.90-0.95 | 0.83 | 3.14 | 3.8x |
| 0.95-0.97 | 0.91 | 7.65 | 8.4x |
| 0.97-1.01 | 0.96 | 23.7 | 25x |

At code 0.85 the plate holds about 0.73, not 1.0, so a pass sitting at 1.81 there is **2.5x the plate** and
worth taking, even though it looks unremarkable measured against 1.0.

The consequence is concrete. Rim-lit cloud on that shot sits at code **0.83**, so a level of 0.90 or 0.97
leaves it at exactly 1.00x the plate: untouched, and still flat. Levels around **0.80-0.85** are what reach it.
Going lower trades away more of the plate's own texture for a shrinking gain, so the useful stop is where the
gain curve flattens, not where the plate happens to clip.

**A lower level is also steadier.** Moving from 0.97 to 0.90, with `grow` 6 to 10 and `feather` 24 to 40, moved
frame-to-frame drift from 1.96% to 1.14% against 0.27% for the source, while lifting the sky's 99th percentile
from 4.96 to 6.96. At 0.97 the mask edge sits exactly where the plate's values are least stable, right at the
clip.

## Run the band comparison against **your** pass

Not every pass gains in the same place. On the same bands, a single-image pass (LumiPic on FLUX.2) measured
**0.45-0.65x the plate below code 0.90** - it darkens there - while reaching 7.5x above 0.97. A level chosen
for one pass is not a level for another, and a pass that darkens the band you opened will make the composite
worse than the plate. Read the `report` output: it carries the gain the node actually applied.

## Wiring, and the two ways it goes wrong

**Both inputs must be in one space, and `plate_space` must say which.** A scene-linear plate fed in while
`plate_space` says `display codes` is compared against thresholds that mean nothing in that space. Measured on
one shot, a mis-wired chain reported a level match of **11.1x** and produced highlights of 488; the same shot
wired correctly reported **1.7x** and highlights of 75.

**The plate side wants to be clamped.** The node's levels are code values, and a plate whose 90th percentile is
already 1.244 has no ceiling for the mask to find. Clamp the plate to 0..1 before it reaches this node; the
reconstruction side stays unclamped, because that is where the range is.

## What this node is not

It is **restoration**, not conversion. It is the right choice for a plate that was actually shot, where
replacing real pixels with invented ones is the thing to avoid. For a generated frame there were never any real
pixels to protect, and the full-frame HDR pass is the better product: the composite carries range only inside
the mask, so on one shot its 99th percentile outside the mask was 0.771 against the model's 2.001 and it could
not survive a stop-down. Both are wanted; they are two products, not a better and a worse one.
