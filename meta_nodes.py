# ComfyUI-OCIO - OCIO Metadata: read, edit and author the shot's identity on its own wire.
# @author Slava Sexton, 2026-08-13
#
# WHY THIS NODE EXISTS. `OCIO Read` puts the plate's header out on a sixth output, `metadata`, and until now that
# wire had exactly ONE place to go: `OCIO Write`'s `metadata` input. So the metadata could travel, and it could be
# delivered, but it could not be looked at and it could not be corrected. A plate with no reel name stayed
# nameless; a plate whose scene field was wrong stayed wrong.
#
# WHAT IT IS NOT. It is not a general JSON editor and it does not invent a vocabulary. The pack already has one:
# `_IDENTITY_FROM` in io_nodes.py maps seven fields to the spellings every writer here understands, and the FIRST
# spelling in each tuple is the one the writers author. This node writes exactly those keys, so whatever it sets
# is picked up by the EXR header, the TIFF tags, the PNG text chunks, the MXF reel name and the sidecar without a
# single further change anywhere.
#
# TIMECODE IS DELIBERATELY ABSENT from the widgets. `OCIO Write` already owns `start_timecode`, and it advances the
# code per frame from that one value. A second place to set it would be two answers to one question, which is how
# they drift - and this pack has already been bitten by exactly that (the write_paths route resolving $OUTPUT one
# way while the writer resolved it another). The plate's own timeCode still passes through untouched.

import json

# The canonical spelling each field is WRITTEN as. Read from io_nodes' own table rather than repeated here, so a
# rename there cannot leave this node authoring a key nothing reads. If the import cannot be resolved (a
# standalone import outside ComfyUI), the fallback is the same seven first-spellings, and the test asserts the two
# agree - a copy that can drift is only safe while something checks it.
_FALLBACK_KEYS = {
    "reel": "reel_name",
    "scene": "scene",
    "shot": "shot",
    "take": "take",
    "camera": "cameraModel",
    "lens": "lens",
}
try:
    from .io_nodes import _IDENTITY_FROM as _IDENT
    CANONICAL = {f: _IDENT[f][0] for f in _FALLBACK_KEYS if f in _IDENT}
    # EVERY spelling of each field, so a correction can clear the ones it is overriding. Resolved once, here, so
    # `run` never has to ask whether the import worked - a conditional on globals() inside the hot path is the kind
    # of fragility this pack keeps finding in other people's code.
    SPELLINGS = {f: tuple(_IDENT[f]) for f in _FALLBACK_KEYS if f in _IDENT}
except Exception:                                        # pragma: no cover - standalone import
    CANONICAL = dict(_FALLBACK_KEYS)
    SPELLINGS = {f: (k,) for f, k in _FALLBACK_KEYS.items()}

FIELDS = ("reel", "scene", "shot", "take", "camera", "lens")


def _load(text):
    """The incoming metadata as a dict, or an empty one. Never raises: a malformed string must not take the render
    down, it must be reported. The shape is what OCIO Read emits - {"source": ..., "kind": ..., "attrs": {...}}."""
    if not text:
        return {}, ""
    try:
        d = json.loads(text)
    except Exception as e:
        return {}, f"incoming metadata ignored (not JSON: {str(e)[:60]})"
    if not isinstance(d, dict):
        return {}, "incoming metadata ignored (not an object)"
    return d, ""


class OCIOMetadata:
    """Look at the plate's metadata, correct it, or author it from nothing."""

    @classmethod
    def INPUT_TYPES(cls):
        blank = ("STRING", {"default": "", "multiline": False,
                            "tooltip": "Blank leaves the plate's own value alone. Type here to set or correct it."})
        return {
            "required": {
                "mode": (["merge", "replace", "passthrough"], {"default": "merge",
                          "tooltip": "merge: what you type wins, the rest of the plate's header survives. replace: "
                                     "keep ONLY the fields you typed. passthrough: change nothing, just report."}),
                "reel": ("STRING", {"default": "", "multiline": False,
                         "tooltip": "Reel / tape name. This is the one field Avid reads structurally from an MXF "
                                    "(Physical Source Package Name), so it is the one worth getting right."}),
                "scene": blank,
                "shot": blank,
                "take": blank,
                "camera": ("STRING", {"default": "", "multiline": False,
                           "tooltip": "Camera body, e.g. ARRI ALEXA 35. Written as cameraModel, the spelling this "
                                      "pack's writers and readers both use."}),
                "lens": blank,
            },
            "optional": {
                "metadata": ("STRING", {"forceInput": True,
                             "tooltip": "(optional) OCIO Read's 'metadata' output. Leave it unwired to author a "
                                        "fresh set from the fields below."}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("metadata", "report")
    FUNCTION = "run"
    CATEGORY = "OCIO"
    DESCRIPTION = ("Read, correct or author the shot's identity on the metadata wire between OCIO Read and OCIO "
                   "Write. Writes the same keys every writer in this pack already reads.")

    def run(self, mode="merge", reel="", scene="", shot="", take="", camera="", lens="", metadata=""):
        src, note = _load(metadata)
        attrs = dict(src.get("attrs") or {}) if isinstance(src.get("attrs"), dict) else {}
        typed = {f: v.strip() for f, v in (("reel", reel), ("scene", scene), ("shot", shot),
                                          ("take", take), ("camera", camera), ("lens", lens)) if v and v.strip()}

        if mode == "passthrough":
            out_attrs, changed = attrs, {}
        elif mode == "replace":
            # ONLY what was typed. Everything the plate carried is dropped on purpose, which is the point of the
            # mode: a render that must not inherit a source's identity at all.
            out_attrs = {CANONICAL[f]: v for f, v in typed.items()}
            changed = dict(typed)
        else:
            out_attrs = dict(attrs)
            changed = {}
            for f, v in typed.items():
                key = CANONICAL[f]
                # ALL the spellings of this field are removed before the canonical one is set. Writing only the
                # canonical key would leave a plate's `dpx:Shot` or `com.apple.proapps.shot` in place, and
                # `_first_meta` takes the FIRST spelling it finds - so the old value could still win downstream
                # and the correction would appear to have done nothing.
                for spelling in SPELLINGS.get(f, (key,)):
                    out_attrs.pop(spelling, None)
                out_attrs[key] = v
                changed[f] = v

        out = dict(src)
        out["attrs"] = out_attrs
        text = json.dumps(out, ensure_ascii=False, default=str)

        lines = []
        if note:
            lines.append(note)
        lines.append(f"mode: {mode}" + (f", set: {', '.join(f'{k}={v}' for k, v in changed.items())}"
                                        if changed else ", nothing typed"))
        if src.get("source"):
            lines.append(f"source: {src['source']}" + (f" ({src['kind']})" if src.get("kind") else ""))
        ident = {f: out_attrs.get(CANONICAL[f]) for f in FIELDS if out_attrs.get(CANONICAL[f])}
        lines.append("identity: " + (", ".join(f"{k}={v}" for k, v in ident.items()) if ident else "none"))
        rest = [k for k in out_attrs if k not in {CANONICAL[f] for f in FIELDS}]
        lines.append(f"{len(out_attrs)} attribute(s) out, {len(rest)} of them from the plate"
                     + (": " + ", ".join(sorted(rest)[:8]) if rest else ""))
        report = "\n".join(lines)
        return {"ui": {"text": [report]}, "result": (text, report)}


NODE_CLASS_MAPPINGS = {"OCIOMetadata": OCIOMetadata}
NODE_DISPLAY_NAME_MAPPINGS = {"OCIOMetadata": "OCIO Metadata"}
