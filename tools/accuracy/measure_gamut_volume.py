"""3D gamut volume comparison: how much color each working space actually covers, in one picture.

The bar-chart parity numbers (measure_ocio_parity.py) prove our node math matches OpenColorIO bit-for-bit,
but a column of zeros doesn't SHOW anything. This renders the RGB gamut of sRGB, ACEScg, ACES2065-1 and ARRI
Wide Gamut 3 as solids in CIE L*a*b* space, so the gamut differences (why "grade in ACEScg, not sRGB" matters)
are visible at a glance.

Method (confirmed, not invented): each colorspace's unit RGB cube boundary (216 points on the 6 faces of a
9x9x9 grid) is converted to CIE XYZ-D65 via the ACTIVE OCIO config's own processor - `cfg.getProcessor(cs,
"CIE XYZ-D65 - Display-referred")`, the studio config's own `cie_xyz_d65_interchange` role - then to Lab
(_common.xyz_to_lab). No colorspace math is hand-rolled here; OCIO does every conversion. The plotted surface
is the convex hull of those boundary points (scipy), a standard gamut-volume visualization.

Run:  E:/ComfyUI/ComfyUI/ComfyUI/.venv/Scripts/python.exe D:/n8n/projects/ComfyUI-OCIO/tools/accuracy/measure_gamut_volume.py
"""
import numpy as np
import _common as C

XYZ_D65 = "CIE XYZ-D65 - Display-referred"

# (label, OCIO colorspace name, matplotlib color) - a representative spread: web/display, ACES scene-linear
# (the pack's default working space), the wide-gamut interchange, and a camera-native gamut. Colors are spread
# ~90 degrees apart on the hue wheel (cyan / orange / green / magenta) so the four overlapping hulls stay
# visually distinct instead of blending into each other where they overlap.
SPACES = [
    ("sRGB (Rec.709)", "sRGB - Display", "#29b6f6"),                        # cyan
    ("ACEScg", "ACEScg", "#ffa726"),                                        # orange
    ("ACES2065-1", "ACES2065-1", "#66bb6a"),                                # green
    ("ARRI Wide Gamut 3", "Linear ARRI Wide Gamut 3", "#ec407a"),           # magenta/pink
]

# CIE Lab display window: real, humanly-visible colors stay roughly in this box (confirmed - sRGB's own full
# range is a*[-86,98] b*[-108,94], well inside it). ACES2065-1 and ARRI Wide Gamut 3 have IMAGINARY primaries
# (outside the visible spectral locus by design, so they can hold any real color plus headroom) - their RGB
# cube corners project to Lab values far outside this box (confirmed: ACES2065-1 L* down to -60, ARRI Wide
# Gamut 3 a* up to +623). Full extents are in the JSON; the chart axes are clipped to this box so the
# meaningful, real-color comparison isn't squashed into a sliver by one imaginary corner.
LAB_WINDOW = {"L": (0, 100), "a": (-130, 130), "b": (-140, 140)}


def cube_boundary_rgb(n=9):
    """216 points on the 6 faces of the unit RGB cube (n x n grid per face) - the cube's boundary is exactly
    where the gamut volume's hull lives; interior points would be redundant for a hull computation."""
    lin = np.linspace(0.0, 1.0, n, dtype=np.float64)
    g0, g1 = np.meshgrid(lin, lin, indexing="ij")
    z0, z1 = np.zeros_like(g0), np.ones_like(g0)
    faces = [
        np.stack([z0, g0, g1], -1), np.stack([z1, g0, g1], -1),   # R=0 / R=1
        np.stack([g0, z0, g1], -1), np.stack([g0, z1, g1], -1),   # G=0 / G=1
        np.stack([g0, g1, z0], -1), np.stack([g0, g1, z1], -1),   # B=0 / B=1
    ]
    return np.concatenate([f.reshape(-1, 3) for f in faces], axis=0)


def gamut_hull_volume(cfg, cs_name):
    """RGB cube boundary -> OCIO -> XYZ-D65 -> Lab, then the convex hull (points + volume in Lab units^3)."""
    from scipy.spatial import ConvexHull
    rgb = cube_boundary_rgb()
    x = np.ascontiguousarray(rgb.astype(np.float32).reshape(1, -1, 3))
    import PyOpenColorIO as OCIO
    cfg.getProcessor(cs_name, XYZ_D65).getDefaultCPUProcessor().apply(OCIO.PackedImageDesc(x, x.shape[1], 1, 3))
    xyz = x.reshape(-1, 3).astype(np.float64)
    lab = C.xyz_to_lab(xyz)
    hull = ConvexHull(lab)
    return lab, hull


def main():
    N, _ = C.load_pack()
    cfg, key = N._resolve_config_keyed("")
    import PyOpenColorIO as OCIO
    results = {"config": cfg.getName(), "ocio_version": OCIO.GetVersion(), "interchange_role": XYZ_D65, "spaces": {}}

    fig = C.new_fig(figsize=(11, 9), subplot_kw={"projection": "3d"})
    fig_obj, ax = fig
    ax.set_facecolor("#14141a")
    for pane in (ax.xaxis.pane, ax.yaxis.pane, ax.zaxis.pane):
        pane.set_facecolor("#1a1a22"); pane.set_edgecolor("#333"); pane.set_alpha(1.0)
    ax.grid(color="#333", linewidth=0.4)

    computed = []
    for label, cs_name, color in SPACES:
        lab, hull = gamut_hull_volume(cfg, cs_name)
        results["spaces"][label] = {"colorspace": cs_name, "hull_volume_Lab_units3": float(hull.volume),
                                     "L_range": [float(lab[:, 0].min()), float(lab[:, 0].max())],
                                     "a_range": [float(lab[:, 1].min()), float(lab[:, 1].max())],
                                     "b_range": [float(lab[:, 2].min()), float(lab[:, 2].max())]}
        computed.append((label, cs_name, color, lab, hull))

    # draw LARGEST hull first, SMALLEST last (on top), so a small gamut like sRGB isn't buried under bigger ones.
    # No per-triangle edge outline (edgecolor="none") - with ~400 hull triangles per space, drawing every edge
    # turned the chart into a tangle of wire that hid the shapes; flat translucent faces read as solids instead.
    computed.sort(key=lambda t: t[4].volume, reverse=True)
    for label, cs_name, color, lab, hull in computed:
        alpha = 0.35 if hull.volume < 1_500_000 else 0.16
        for simplex in hull.simplices:
            tri = lab[simplex]
            ax.plot_trisurf(tri[:, 1], tri[:, 2], tri[:, 0], color=color, alpha=alpha, edgecolor="none", shade=False)
        ax.plot([], [], color=color, label=f"{label}  (vol={hull.volume:,.0f} Lab³)")

    ax.set_xlabel("a*  (green -> red)")
    ax.set_ylabel("b*  (blue -> yellow)")
    ax.set_zlabel("L*  (black -> white)")
    ax.set_xlim(*LAB_WINDOW["a"]); ax.set_ylim(*LAB_WINDOW["b"]); ax.set_zlim(*LAB_WINDOW["L"])
    ax.set_title("RGB gamut volume in CIE L*a*b* (via OCIO's own XYZ-D65 interchange)\n"
                  "bigger hull = wider gamut - this is why we grade in ACEScg / ACES2065-1, not sRGB\n"
                  "(axes clipped to the real-color range - ACES2065-1 / ARRI WG3 have imaginary primaries that "
                  "extend far beyond it)", pad=14, fontsize=11)
    ax.legend(loc="upper left", fontsize=8, facecolor="#191922", labelcolor="#dde")
    ax.view_init(elev=18, azim=-55)

    order = sorted(results["spaces"].items(), key=lambda kv: kv[1]["hull_volume_Lab_units3"])
    print("Gamut volume in CIE Lab (smallest -> largest), all via OCIO's own XYZ-D65 role, no hand-rolled matrices:")
    for label, s in order:
        print(f"  {label:20s} {s['hull_volume_Lab_units3']:>14,.0f} Lab^3")

    path = C.savefig(fig_obj, "gamut_volume_3d.png")
    print("\nplot:", path)
    print("json:", C.dump("gamut_volume.json", results))


if __name__ == "__main__":
    main()
