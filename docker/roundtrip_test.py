#!/usr/bin/env python3
"""End-to-end OCIO round-trip color-accuracy test, driven through a real headless ComfyUI.

Flow:
  1. Ensure the Kodak "Marcie" test image is in the mounted volume (download INPUT_IMAGE_URL if absent).
  2. Boot ComfyUI on the CPU and wait until every OCIO node is registered.
  3. For each enabled chain, build an API-format graph that round-trips the image ACEScg -> ... -> ACEScg
     (reading/writing RAW 32-bit float EXR so only the node chain is under test), submit it, wait for
     the OCIOWrite output, and compare output-vs-input with OpenCV histograms + pixel error.
  4. Print a combined report. Exit non-zero if any GATED chain fails.

Chains (see build_workflow.py):
  colorspace : ACEScg->LogC->LINEAR Rec.709/sRGB->back, all invertible -> expected ~1e-6. This is the
               "colour math is perfect" proof (gated).
  srgb       : same path but via the display-range sRGB TEXTURE encoding -> EXPECTED to clip HDR values
               >1 (a property of the display-range encoding, not the math). Reported, not gated.
  display    : routes through the Rec.709/sRGB DISPLAY view (RRT+ODT); tone-maps + clips -> residual
               EXPECTED. Reported, not gated.

Env knobs (all optional):
  INPUT_IMAGE_URL   default: sobotka marcie_whacked.exr (verified direct download)
  DATA_DIR          default: /data            (holds input/ and output/)
  CHAINS            default: "colorspace,srgb,display"
  GATE_CHAINS       default: "colorspace"     (which chains' PASS/FAIL sets the exit code)
  COMFYUI_DIR       default: /opt/ComfyUI
  COMFYUI_URL       default: http://127.0.0.1:8188
  TOL_CORR          default: 0.99999          (min histogram correlation to pass)
  TOL_PIX           default: 1e-4             (max abs pixel error to pass)
"""
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from comfyui_client import ComfyUIClient          # noqa: E402
from build_workflow import build_graph            # noqa: E402
import ocio_names                                 # noqa: E402
import compare_histograms                          # noqa: E402

DEFAULT_URL = ("https://raw.githubusercontent.com/sobotka/"
               "OpenColorIO-Colour-Management-Test/master/marcie_whacked.exr")
INPUT_IMAGE_URL = os.environ.get("INPUT_IMAGE_URL") or DEFAULT_URL  # empty env value -> default
DATA_DIR = os.environ.get("DATA_DIR", "/data")
INPUT_DIR = os.path.join(DATA_DIR, "input")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
INPUT_PATH = os.path.join(INPUT_DIR, "marcie.exr")
COMFYUI_DIR = os.environ.get("COMFYUI_DIR", "/opt/ComfyUI")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
CHAINS = [c.strip() for c in os.environ.get("CHAINS", "colorspace,srgb,display").split(",") if c.strip()]
GATE_CHAINS = {c.strip() for c in os.environ.get("GATE_CHAINS", "colorspace").split(",") if c.strip()}
TOL_CORR = float(os.environ.get("TOL_CORR", "0.99999"))
TOL_PIX = float(os.environ.get("TOL_PIX", "1e-4"))
SERVER_LOG = os.path.join(DATA_DIR, "comfyui-server.log")


def ensure_input():
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if os.path.exists(INPUT_PATH) and os.path.getsize(INPUT_PATH) > 0:
        print(f"[input] using existing {INPUT_PATH} ({os.path.getsize(INPUT_PATH)} bytes)")
        return
    print(f"[input] downloading {INPUT_IMAGE_URL}\n        -> {INPUT_PATH}")
    urllib.request.urlretrieve(INPUT_IMAGE_URL, INPUT_PATH)
    size = os.path.getsize(INPUT_PATH)
    if size == 0:
        raise RuntimeError("downloaded input is empty")
    print(f"[input] downloaded {size} bytes")


def start_comfyui():
    log = open(SERVER_LOG, "wb")
    print(f"[comfyui] starting headless CPU server (log -> {SERVER_LOG})")
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--cpu", "--port", "8188", "--listen", "127.0.0.1"],
        cwd=COMFYUI_DIR, stdout=log, stderr=subprocess.STDOUT)
    return proc, log


def tail(path, n=40):
    try:
        with open(path, "r", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(no server log)"


def main():
    ensure_input()
    proc, log = start_comfyui()
    client = ComfyUIClient(COMFYUI_URL)
    results = {}
    try:
        try:
            client.wait_until_ready(timeout=300)
        except TimeoutError:
            print("[comfyui] server never became ready. Last log lines:\n" + tail(SERVER_LOG))
            return 3

        ok, missing = client.check_nodes()
        if not ok:
            print(f"[comfyui] OCIO nodes missing from /object_info: {missing}\n" + tail(SERVER_LOG))
            return 3
        print("[comfyui] all 9 OCIO nodes registered.")

        cfg_src = os.environ.get("OCIO") or "(built-in ACES config)"
        names = ocio_names.resolve_names()
        print(f"[config] OCIO = {cfg_src}")
        print("[config] resolved names:")
        for k, v in names.items():
            print(f"           {k:14s} = {v}")

        for chain in CHAINS:
            out_name = f"roundtrip_{chain}"
            graph, expected = build_graph(chain, names, INPUT_PATH, OUTPUT_DIR, out_name)
            if os.path.exists(expected):
                os.remove(expected)
            print(f"\n[run] chain '{chain}' -> {expected}")
            try:
                client.run(graph, timeout=600)
            except Exception as e:  # a chain (e.g. inverse display under 1.0.3) may fail; don't abort others
                print(f"[run] chain '{chain}' errored: {e}\n" + tail(SERVER_LOG))
                results[chain] = None
                continue
            if not os.path.exists(expected):
                print(f"[run] chain '{chain}': OCIOWrite produced no file at {expected}\n" + tail(SERVER_LOG))
                results[chain] = None
                continue
            metrics = compare_histograms.compare(INPUT_PATH, expected, tol_corr=TOL_CORR, tol_pix=TOL_PIX)
            results[chain] = metrics
            print(compare_histograms.format_report(f"chain '{chain}'", metrics))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    # --- summary + exit code ---
    print("\n==================== SUMMARY ====================")
    gated_fail = False
    for chain in CHAINS:
        m = results.get(chain)
        gated = chain in GATE_CHAINS
        tag = "GATED" if gated else "info "
        if m is None:
            print(f"  [{tag}] {chain:12s}: NO OUTPUT (error)")
            if gated:
                gated_fail = True
            continue
        verdict = "PASS" if m["pass"] else "FAIL"
        print(f"  [{tag}] {chain:12s}: {verdict}  "
              f"corr(min)={m['hist_correl_min']:.6f}  maxpix={m['max_abs_pixel_error']:.3e}")
        if gated and not m["pass"]:
            gated_fail = True
    if any(c in results and results[c] and not results[c]["pass"] for c in ("srgb", "display")):
        print("\n  Note: the 'srgb' and 'display' chains are EXPECTED to show a residual. marcie_whacked is")
        print("  HDR (values up to ~4.35 linear); the display-range sRGB texture encoding clamps to [0,1]")
        print("  and the ACES display view (RRT+ODT) tone-maps + clips, so HDR highlights cannot round-")
        print("  trip. This is a property of those encodings, not the color math - which the gated")
        print("  'colorspace' chain (linear Rec.709/sRGB) proves invertible to floating-point precision.")
    print("================================================")
    return 1 if gated_fail else 0


if __name__ == "__main__":
    sys.exit(main())
