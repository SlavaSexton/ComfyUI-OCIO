"""Build ComfyUI API-format graphs that round-trip an image through the OCIO nodes and back.

All chains start and end in ACEScg and read/WRITE raw float (no implicit colorspace conversion on
IO), so the measurement isolates exactly the node chain in between:

  chain "colorspace" (GATED - the "colour math is perfect" proof -> expected ~1e-6):
      ACEScg --CS--> LogC --CS--> LINEAR Rec.709/sRGB --CS--> LogC --CS--> ACEScg
      Uses the linear Rec.709/sRGB primaries (gamut only, no display transfer), which invert exactly
      even for HDR values >1 - so this genuinely round-trips ACEScg->LogC->Rec.709/sRGB->back.

  chain "srgb" (informational -> expected to CLIP HDR):
      ACEScg --CS--> LogC --CS--> sRGB TEXTURE --CS--> LogC --CS--> ACEScg
      Same path but through the display-range sRGB *encoding* (sRGB transfer, clamps to [0,1]); HDR
      values >1 cannot survive, so a residual is expected - a property of the encoding, not the math.

  chain "display" (informational -> expected to TONE-MAP + CLIP):
      ACEScg --CS--> LogC --Display(fwd)--> Rec.709/sRGB view --Display(inv)--> LogC --CS--> ACEScg
      Through the ACES display view (RRT+ODT): tone-maps and clips, not losslessly invertible.

Real colorspace / display / view names differ across OCIO config versions; they are resolved at
runtime from the live OCIO config by ocio_names.resolve_names() and passed in here as `names`.
"""


def _read_node(source_path, cs_any):
    """OCIORead in raw mode: file pixels passed through untouched (colorspaces ignored, but a valid
    combo value is still required by ComfyUI's validation)."""
    return {"class_type": "OCIORead", "inputs": {
        "source": source_path,
        "frame_mode": "single",
        "input_colorspace": cs_any,
        "output_colorspace": cs_any,
        "raw_data": True,
        "start_frame": 0, "end_frame": 0, "frame_shift": 0,
        "missing_frames": "black", "edge_mode": "hold", "fps": 0.0,
    }}


def _colorspace_node(in_cs, out_cs, image_ref):
    return {"class_type": "OCIOColorSpace", "inputs": {
        "in_colorspace": in_cs, "out_colorspace": out_cs, "mix": 1.0, "image": image_ref}}


def _display_node(in_cs, display, view, invert, image_ref):
    return {"class_type": "OCIODisplay", "inputs": {
        "in_colorspace": in_cs, "display": display, "view": view,
        "invert_direction": bool(invert), "mix": 1.0, "image": image_ref}}


def _write_node(images_ref, alpha_ref, out_folder, out_name, cs_any):
    """OCIOWrite: raw 32-bit float EXR, deterministic filename (colorspace_in_name off) so the output
    is <out_folder>/<out_name>.exr. raw_data on -> pixels written as-is (no conversion)."""
    return {"class_type": "OCIOWrite", "inputs": {
        "profile": "none",
        "from_colorspace": cs_any, "output_colorspace": cs_any,
        "container": "still image", "still_format": "exr", "video_codec": "prores_4444",
        "bit_depth": "32f", "compression": "zip",
        "auto_range": False, "first_frame": 1, "last_frame": 0, "start_number": 1, "source_start": 1,
        "raw_data": True, "colorspace_in_name": False, "auto_colorspace": True,
        "output_folder": out_folder, "filename": out_name,
        "images": images_ref, "alpha": alpha_ref, "fps": 24.0,
        "render_nonce": out_name,
    }}


def build_graph(chain, names, source_path, out_folder, out_name):
    """Return (graph_dict, expected_output_path) for chain in {"colorspace", "srgb", "display"}."""
    acescg, logc = names["acescg"], names["logc"]
    read = "read"
    graph = {read: _read_node(source_path, acescg)}

    if chain in ("colorspace", "srgb"):
        # "colorspace" uses the non-clamping linear Rec.709/sRGB (perfect); "srgb" uses the
        # display-range sRGB texture encoding (clips HDR). Same 4-hop shape otherwise.
        srgb = names["srgb_linear"] if chain == "colorspace" else names["srgb_texture"]
        graph["n2"] = _colorspace_node(acescg, logc, [read, 0])   # ACEScg -> LogC
        graph["n3"] = _colorspace_node(logc, srgb, ["n2", 0])     # LogC   -> Rec.709/sRGB
        graph["n4"] = _colorspace_node(srgb, logc, ["n3", 0])     # Rec.709/sRGB -> LogC (inverse)
        graph["n5"] = _colorspace_node(logc, acescg, ["n4", 0])   # LogC   -> ACEScg (inverse)
    elif chain == "display":
        display, view = names["display"], names["view"]
        graph["n2"] = _colorspace_node(acescg, logc, [read, 0])                 # ACEScg -> LogC
        graph["n3"] = _display_node(logc, display, view, False, ["n2", 0])      # LogC   -> display (fwd)
        graph["n4"] = _display_node(logc, display, view, True, ["n3", 0])       # display-> LogC   (inv)
        graph["n5"] = _colorspace_node(logc, acescg, ["n4", 0])                 # LogC   -> ACEScg
    else:
        raise ValueError(f"unknown chain {chain!r}")

    graph["write"] = _write_node(["n5", 0], [read, 1], out_folder, out_name, acescg)
    expected = f"{out_folder.rstrip('/')}/{out_name}.exr"
    return graph, expected
