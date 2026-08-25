LTX-2.5 native HDR, the ACEScct route
=====================================

LTX-2.5 has a native HDR path that takes EXR in and writes EXR out. It is not reachable from
ComfyUI: the flag lives in Lightricks' own CLI, `--hdr {SRGB_LINEAR,ACESCG,ACESCCT}`, and
ComfyUI's core has no ACEScct path at all. Searched on 2026-08-25, the official Comfy template
library lists 599 templates in 8 categories and none of them mentions ACEScct; every LTX
template in it has no colour management of any kind. The same search finds KSampler in 251 and
a nonsense token in none, so it is looking rather than failing quietly.

What this pack supplies is the two ends of that path. The middle is their CLI.

    OCIO Read (sRGB or ACEScg)
      -> OCIO ColorSpace   to ACEScg
      -> OCIO LogConvert   Linear to Log, curve ACEScct
      -> OCIO Write        EXR 32f
            [ their CLI:  ltx_pipelines.distilled --hdr ACESCCT ]
      -> OCIO Read         their EXR
      -> OCIO LogConvert   Log to Linear, curve ACEScct
      -> OCIO Write        EXR or ProRes

FILES

plate_acescct.exr
    The plate from frames/GIRL_SDR.0001.exr put through the ACEScct transfer by OCIO Write,
    written 32-bit float and tagged: chromaticities AP1 with ACES white, and
    com.ocio.colorspace ACEScct. Read it with a Log2Lin on ACEScct and it returns to the
    linear plate, worst relative error 3.7e-05.

    Verified against the published constants before it was written:
        linear 0.18 -> code 0.413588   (published mid grey)
        linear 0.0  -> code 0.0729055  (published ACEScct black)

cli_output.0001.exr, cli_output.0009.exr
    First and last frame of what their CLI wrote, `--hdr ACESCCT`, fed the file above. Their
    header declares colorSpace ACEScct on the same AP1 chromaticities. These are CODES.

    Measured against the plate they came from:
        plate  code p50 0.3295   linear p50 0.0648
        output code p50 0.3298   linear p50 0.0651   linear max 71.38

    The output sits on the plate's level. That is the check that the input was interpreted
    correctly: a run fed a LINEAR plate under the same flag, which their docs define as
    "already ACEScct codes, pass through", came out about five stops darker with a median that
    decodes slightly negative.

review.mov, review.json
    ProRes 4444, Rec.709, written by this pack's own OCIO Write from the nine frames with the
    ACES 2.0 SDR output transform applied on the way out. 3 fps, so nine frames run three
    seconds and a player will show them. For looking, not for grading. The sidecar beside it is
    what OCIO Write records about the write.

    Their CLI also writes an HDR master beside the EXRs, HEVC Main 10 in BT.2020 with the HLG
    transfer. It is not included here: at 24 fps nine frames last 0.375 seconds, which most
    players will not show at all, and the review above carries the same picture in a container
    that opens everywhere.

WHY THE CEILING MATTERS HERE

The LTX-2.3 HDR IC-LoRA path this pack also documents is fixed to ARRI LogC3, whose ceiling is
55.1 linear at code 1.0. ACEScct reaches 222.9 at the same code, four times further out.

This particular run did not go anywhere near either. The two frames here top out at code 0.906
and 0.875, with no sample at the ceiling at all, which decodes to a peak of 71.38 linear. So the
headroom was there and the shot did not need it. A brighter shot would be the test of whether
the extra range is reached, and this one is not that test.

RUNNING IT ON WINDOWS

The CLI half is not part of this pack and is not supported here, but one failure is worth
recording because it costs a day to find. On Windows, safetensors' default mmap backend takes
a commit charge the size of the whole checkpoint at open time, before a tensor is read.
Measured on a 39.13 GiB file: available commit fell 147.23 to 106.42 GiB, a charge of 40.81
GiB, while free physical memory moved by half a gigabyte. When the commit limit runs out the
run dies as a RuntimeError about an invalid python storage, as a bare segfault, or as
OSError 1455. Passing backend="pread" to safe_open charges 0.00 GiB and the run completes.

THE GRAPH
    ../OCIO_Example_LTX25_native_ACEScct.json builds the shape above inside ComfyUI: sRGB in,
    converted to ACEScg, compressed to ACEScct codes before the model and decompressed after
    it, written as a linear EXR. One of its notes lists the weights it loads and how they
    differ from the ones the CLI needs.

    It is the SHAPE, not the vendor's pipeline. Theirs does the compression internally, keeps
    the VAE in float32, and writes EXR frames tagged ACEScct plus a BT.2020 HLG master. The two
    frames here are copies of that output; the master is not included.

THE TWO ROUTES SIDE BY SIDE

    ../comparisons/LogC3_vs_ACEScct_towers.png
    ../comparisons/LogC3_vs_ACEScct_cave.png

    Each sheet is one shot, walked down four stops. The top row is the LTX-2.3 HDR IC-LoRA pass
    on LogC3, the bottom is LTX-2.5's native path through ACEScct, and the levels are matched
    first, because the two routes do not return the same exposure and comparing them unmatched
    compares exposures rather than reconstructions. The lift applied is printed on each sheet.

    They do not flatter ACEScct, and that is the point of including them. On both bright shots
    the LogC3 route carried more range: peak 53.43 linear against 16.60 on the towers, and 50.41
    against 8.10 on the cave. Neither route put a meaningful number of samples at its own ceiling
    (0.0000% and 0.0001% for LogC3, none at all for ACEScct), so on this material the limit is
    the model rather than the curve.

    Both paths ship. Which one suits a shot is a decision to make on numbers like these.
