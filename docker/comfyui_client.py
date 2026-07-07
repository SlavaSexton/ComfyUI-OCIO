"""Tiny stdlib-only client for driving a headless ComfyUI over its HTTP API.

No third-party deps (urllib/json only) so it runs in any Python the image already has. Used by
roundtrip_test.py and run_tests.sh to: wait for the server, inspect registered nodes / combo
options via /object_info, submit an API-format prompt, and poll /history to completion.
"""
import json
import time
import urllib.request
import urllib.error

# The 9 nodes ComfyUI-OCIO registers (must all appear in /object_info once the pack loads).
OCIO_NODE_CLASSES = [
    "OCIOColorSpace", "OCIOLogConvert", "OCIODisplay", "OCIOCDLTransform",
    "OCIOFileTransform", "OCIOLookTransform", "OCIORead", "OCIOWrite", "OCIOPlayer",
]


class ComfyUIClient:
    def __init__(self, base_url="http://127.0.0.1:8188", client_id="ocio-roundtrip"):
        self.base = base_url.rstrip("/")
        self.client_id = client_id

    # --- low-level HTTP ---
    def _get(self, path, timeout=30):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _post(self, path, payload, timeout=60):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # --- lifecycle ---
    def wait_until_ready(self, timeout=300, interval=2.0):
        """Block until /object_info responds (ComfyUI has finished importing all custom nodes)."""
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                self._get("/object_info", timeout=10)
                return True
            except Exception as e:  # noqa: BLE001 - server not up yet / still importing
                last_err = e
                time.sleep(interval)
        raise TimeoutError(f"ComfyUI not ready after {timeout}s: {last_err}")

    # --- introspection ---
    def object_info(self, node_class=None):
        return self._get("/object_info" + (f"/{node_class}" if node_class else ""))

    def check_nodes(self):
        """Return (ok, missing) for the OCIO node classes; proves the pack imported inside ComfyUI."""
        info = self.object_info()
        missing = [c for c in OCIO_NODE_CLASSES if c not in info]
        return (not missing, missing)

    def combo_options(self, node_class, input_name):
        """The list of allowed values for a combo widget, as populated by the live OCIO config."""
        info = self.object_info(node_class)[node_class]
        spec = info["input"]["required"].get(input_name) or info["input"].get("optional", {}).get(input_name)
        # combo spec is [ [option, ...], {opts} ] or just [ [option, ...] ]
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            return spec[0]
        return []

    # --- execution ---
    def submit(self, prompt_graph):
        """POST an API-format graph. Returns prompt_id. Raises with node_errors on validation failure."""
        try:
            resp = self._post("/prompt", {"prompt": prompt_graph, "client_id": self.client_id})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"/prompt rejected the graph (HTTP {e.code}):\n{body}") from None
        if "prompt_id" not in resp:
            raise RuntimeError(f"/prompt returned no prompt_id: {resp}")
        return resp["prompt_id"]

    def wait_for_history(self, prompt_id, timeout=600, interval=1.0):
        """Poll /history/{id} until the run completes; return its history entry. Raise on node error."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            hist = self._get(f"/history/{prompt_id}")
            entry = hist.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("completed") or status.get("status_str") == "success":
                    return entry
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI run errored: {json.dumps(status, indent=2)}")
            time.sleep(interval)
        raise TimeoutError(f"ComfyUI run {prompt_id} did not finish within {timeout}s")

    def run(self, prompt_graph, timeout=600):
        """Submit and block until done; returns the history entry."""
        return self.wait_for_history(self.submit(prompt_graph), timeout=timeout)
