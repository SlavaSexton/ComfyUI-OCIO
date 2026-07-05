#!/usr/bin/env bash
# Unit + smoke test entrypoint for the `test` compose service.
#   1. Run every standalone tools/test_*.py (exit non-zero on any failure).
#   2. Boot ComfyUI headless and assert all 9 OCIO nodes register (proves the pack imports in ComfyUI).
#   3. Optional: tools/accuracy regression suite when RUN_ACCURACY=1.
set -uo pipefail

PACK_DIR="${OCIO_PACK_DIR:-/opt/ComfyUI/custom_nodes/ComfyUI-OCIO}"
COMFYUI_DIR="${COMFYUI_DIR:-/opt/ComfyUI}"
export OPENCV_IO_ENABLE_OPENEXR=1

# The pack's own tools/test_*.py target the built-in ACES 2.x config (they reference names like
# "sRGB - Display"). The image sets $OCIO to the bundled ACES 1.0.3 config for the round-trip demo;
# unset it here so these tests run against the 2.x config they were written for. (The round-trip job,
# roundtrip_test.py, keeps $OCIO = 1.0.3.)
unset OCIO

fail=0

echo "==================== standalone node tests ===================="
cd "$PACK_DIR" || exit 2
for t in tools/test_*.py; do
    echo "----- $t -----"
    if python "$t"; then
        echo "  ok: $t"
    else
        echo "  FAILED: $t"
        fail=1
    fi
done

echo
echo "==================== ComfyUI node-registration smoke ===================="
python - <<'PY'
import subprocess, sys, os
sys.path.insert(0, os.path.join(os.environ.get("OCIO_PACK_DIR", "/opt/ComfyUI/custom_nodes/ComfyUI-OCIO"), "docker"))
from comfyui_client import ComfyUIClient
comfy = os.environ.get("COMFYUI_DIR", "/opt/ComfyUI")
log = open("/tmp/comfyui-smoke.log", "wb")
proc = subprocess.Popen([sys.executable, "main.py", "--cpu", "--port", "8188", "--listen", "127.0.0.1"],
                        cwd=comfy, stdout=log, stderr=subprocess.STDOUT)
rc = 0
try:
    c = ComfyUIClient("http://127.0.0.1:8188")
    c.wait_until_ready(timeout=300)
    ok, missing = c.check_nodes()
    if ok:
        print("  ok: all 9 OCIO nodes registered in ComfyUI")
    else:
        print(f"  FAILED: missing nodes {missing}")
        rc = 1
except Exception as e:
    print(f"  FAILED: {e}")
    rc = 1
finally:
    proc.terminate()
    try: proc.wait(timeout=15)
    except Exception: proc.kill()
    log.close()
sys.exit(rc)
PY
[ $? -ne 0 ] && fail=1

if [ "${RUN_ACCURACY:-0}" = "1" ]; then
    echo
    echo "==================== accuracy regression suite ===================="
    cd "$PACK_DIR" || exit 2
    python tools/accuracy/gen_fixtures.py || fail=1
    for m in tools/accuracy/measure_*.py; do
        echo "----- $m -----"
        python "$m" || fail=1
    done
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "ALL TESTS PASSED"
else
    echo "SOME TESTS FAILED"
fi
exit "$fail"
