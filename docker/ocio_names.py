"""Resolve the colorspace / display / view names the round-trip needs, from the LIVE OCIO config.

Loading the config with PyOpenColorIO directly (honoring $OCIO exactly like the pack's
_resolve_config does) is more robust than scraping node combos: it gives real per-display views, so
the display chain always uses an OCIO-valid (display, view) PAIR. Because the ComfyUI node combos are
built from this same config, the resolved names are guaranteed to be valid combo members too.

Works across config schemes:
  ACES 1.0.3 : "ACES - ACEScg", "Input - ARRI - V3 LogC (EI800) - Wide Gamut",
               "Input - Generic - sRGB - Texture", display "ACES" + view "sRGB".
  ACES 2.x   : "ACEScg", "ARRI LogC3 (EI800)", "sRGB - Texture",
               display "sRGB - Display" + view "ACES 1.0 - SDR Video".
"""
import os

# Tokens that mark a view/display we do NOT want for an SDR Rec.709/sRGB round trip.
_BAD_VIEW = ("raw", "log", "d60 sim", "st2084", "hdr", "nits", "nit)", "dcdm", "p3", "un-tone", "2020")


def load_config():
    """Mirror the pack: explicit $OCIO env -> built-in ACES config."""
    import PyOpenColorIO as OCIO  # lazy: keeps the pure resolvers importable without OCIO installed
    if os.environ.get("OCIO"):
        return OCIO.Config.CreateFromEnv()
    for builtin in ("studio-config-latest", "cg-config-latest", "ocio://default"):
        try:
            return OCIO.Config.CreateFromBuiltinConfig(builtin)
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("no OCIO config available (set $OCIO or install a recent opencolorio)")


def _pick(names, groups, exclude=()):
    """First name matching any group (a group is a substring or a tuple of ANDed substrings)."""
    for group in groups:
        ands = (group,) if isinstance(group, str) else tuple(group)
        for n in names:
            low = n.lower()
            if all(a.lower() in low for a in ands) and not any(x.lower() in low for x in exclude):
                return n
    return None


def _pick_display_view(config):
    """A valid (display, view) pair that produces an SDR Rec.709/sRGB image. Prefers sRGB, then Rec.709."""
    displays = list(config.getDisplays())
    for pref in ("srgb", "rec.709", "rec709"):
        for d in displays:
            for v in config.getViews(d):
                hay = f"{d} {v}".lower()
                if pref in hay and not any(b in hay for b in _BAD_VIEW):
                    return d, v
    # fallback: any non-HDR/raw/log pair, else the first pair that exists
    for d in displays:
        for v in config.getViews(d):
            if not any(b in f"{d} {v}".lower() for b in _BAD_VIEW):
                return d, v
    for d in displays:
        vs = list(config.getViews(d))
        if vs:
            return d, vs[0]
    return None, None


def resolve_names(config=None):
    """Return the colorspace/display/view names the chains use. Raises if a required one is absent.

    Two distinct sRGB/Rec.709 roles, because they behave very differently on HDR input:
      srgb_linear  - LINEAR Rec.709/sRGB primaries (gamut only, NO transfer curve): a pure primaries
                     matrix, so it round-trips HDR values >1 WITHOUT clipping, invertible to float
                     precision (~1e-7, not bit-exact). Used by the gated "perfect" chain.
      srgb_texture - the display-range sRGB *encoding* (sRGB transfer, clamps to [0,1]): clips HDR.
                     Used by an informational chain to show that display-range encoding is lossy.
    """
    config = config or load_config()
    cs = [c.getName() for c in config.getColorSpaces()]
    display, view = _pick_display_view(config)
    names = {
        "acescg": _pick(cs, ["acescg"]),
        "logc": _pick(cs, [("arri", "logc", "ei800"), ("logc3", "ei800"), ("arri", "logc"), "logc3", "logc"]),
        # linear Rec.709/sRGB (non-clamping). 1.0.3: "Utility - Linear - Rec.709"; 2.x: "Linear Rec.709 (sRGB)".
        "srgb_linear": _pick(cs, [("linear", "rec.709"), ("linear", "srgb"), ("lin", "rec709"), ("lin", "srgb")],
                             exclude=("display", "camera", "output")),
        # display-range sRGB texture encoding. 1.0.3: "Input - Generic - sRGB - Texture"; 2.x: "sRGB - Texture".
        "srgb_texture": _pick(cs, [("srgb", "texture"), ("generic", "srgb")],
                              exclude=("output", "display", "out_", "lin_", "crv_", "linear")),
        "display": display,
        "view": view,
    }
    missing = [k for k, v in names.items() if not v]
    if missing:
        raise RuntimeError(f"could not resolve OCIO names {missing}\n  colorspaces: {cs}\n"
                           f"  displays: {list(config.getDisplays())}")
    return names


if __name__ == "__main__":
    for k, v in resolve_names().items():
        print(f"{k:14s} = {v}")
