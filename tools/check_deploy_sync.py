"""Is the copy ComfyUI loads the same as the repository - and if not, exactly how does it differ?

Two checkouts of this pack can drift apart silently: the repository you edit and commit in, and the
copy sitting under ComfyUI's custom_nodes that the running server actually imports. A stale, hand-patched
install has previously masked a real bug fix - the repo's tests were green because they never touched the
code the server was actually running. This script answers the sync question directly by reading both
trees' content, not by trusting a version string or a commit message.

What "ships" is defined as every file `git ls-files` tracks in the repository. Deliberately NOT the
narrower set `.comfyignore` carves out for `comfy node publish` (Registry archives skip docs/assets/,
tools/, docker/, .github/ - see that file's own comment for why). This checker exists for a different
distribution path: a git clone or a hand-copied folder under custom_nodes, which is how the pack actually
lands on a dev/render box, and on that path the tools/ test scripts and docs genuinely are what's on disk
and what could silently drift - the earlier incident could just as easily have been a stale test file. So
the manifest here is the full tracked set, filtered only by what the repo's own .gitignore already excludes
(plus .git/ and __pycache__/, which are never tracked in the first place).

Content is compared after normalising line endings, and only for files this script sniffs as text (no NUL
byte in the first 8000 bytes, the same heuristic git itself uses). Binary files (images, video, LUTs) are
compared byte for byte. This avoids a false "differs" on every file from Windows CRLF checkout churn while
still catching a real single-character change.

Locating the installed copy (never hardcoded - this repository is public and the path is only correct on
one machine):
  1. --path PATH                      explicit override, highest priority
  2. env COMFYUI_OCIO_INSTALL_PATH    a direct path to the installed pack folder
  3. env COMFYUI_BASE_DIR             the ComfyUI root (the folder containing main.py); this script then
                                       looks for <COMFYUI_BASE_DIR>/custom_nodes/<DisplayName from
                                       pyproject.toml>, and - if a server is reachable at --comfy-url - asks
                                       it to confirm that module is actually the one loaded, via
                                       /object_info, before trusting the guess. (The ComfyUI HTTP API does
                                       not expose custom node filesystem paths directly, so this is a
                                       confirm-a-guess step, not a true path lookup. That is a real API gap,
                                       not a shortcut taken here - see the README/CHANGELOG or file an issue
                                       against ComfyUI core if a real endpoint would help.)
  If none resolve, the script exits 2 and prints all three options rather than crashing on a NoneType path.

Usage:
    python tools/check_deploy_sync.py --path "C:\\ComfyUI\\custom_nodes\\ComfyUI-OCIO"
    COMFYUI_OCIO_INSTALL_PATH="C:\\ComfyUI\\custom_nodes\\ComfyUI-OCIO" python tools/check_deploy_sync.py
    python tools/check_deploy_sync.py --repo-path "C:\\dev\\ComfyUI-OCIO" --path "C:\\ComfyUI\\custom_nodes\\ComfyUI-OCIO"

Exit codes: 0 = identical (same tracked-file content and, where both sides are git repos, same HEAD);
1 = they differ (the detail section says exactly how); 2 = could not run the comparison at all (path not
resolved, one side not readable).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_INSTALL_PATH = "COMFYUI_OCIO_INSTALL_PATH"
ENV_COMFY_BASE_DIR = "COMFYUI_BASE_DIR"
DEFAULT_COMFY_URL = "http://127.0.0.1:8188"
GIT_TIMEOUT_S = 15
HTTP_TIMEOUT_S = 5
BINARY_SNIFF_BYTES = 8000
# Node names to try, in order, when asking a live server to confirm which module is loaded.
PROBE_NODE_NAMES = ("OCIORead", "OCIOColorSpace", "OCIOWrite", "OCIOVAEDecode")


def default_repo_root() -> Path:
    """The repository is wherever this script itself lives - one directory up from tools/."""
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------------------------------
# git state
# --------------------------------------------------------------------------------------------------

def run_git(repo_dir: Path, args):
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir)] + list(args),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT_S,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def git_state(repo_dir: Path) -> dict:
    """HEAD short SHA, dirty flag, and uncommitted-file count. Never raises - a non-repo or unreadable
    directory is reported as such, not crashed on."""
    rc, out, err = run_git(repo_dir, ["rev-parse", "--is-inside-work-tree"])
    if rc != 0 or out.strip() != "true":
        return {"is_repo": False, "head": None, "dirty": None, "uncommitted": None, "error": err.strip() or "not a git repository"}

    rc, head, err = run_git(repo_dir, ["rev-parse", "--short", "HEAD"])
    head = head.strip() if rc == 0 else None

    rc, status, err = run_git(repo_dir, ["status", "--porcelain", "--untracked-files=all"])
    lines = [ln for ln in status.splitlines() if ln.strip()]
    return {
        "is_repo": True,
        "head": head,
        "dirty": len(lines) > 0,
        "uncommitted": len(lines),
        "error": None,
    }


def git_tracked_files(repo_dir: Path):
    """The manifest of what ships: every file git tracks. Returns None on failure (not a git repo)."""
    rc, out, err = run_git(repo_dir, ["ls-files"])
    if rc != 0:
        return None, err.strip()
    files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return files, None


# --------------------------------------------------------------------------------------------------
# .gitignore-lite, just enough for this repo's own patterns (directory suffix, and fnmatch globs like
# *.py[cod]). Not a full gitignore implementation - documented as such, and it only needs to keep the
# "extra in install" walk from drowning in junk this repo already excludes from tracking.
# --------------------------------------------------------------------------------------------------

def load_gitignore_patterns(repo_dir: Path):
    gi = repo_dir / ".gitignore"
    if not gi.exists():
        return []
    patterns = []
    for raw in gi.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def gitignore_matches(rel_posix_path: str, patterns) -> bool:
    parts = rel_posix_path.split("/")
    basename = parts[-1]
    for pat in patterns:
        if pat.endswith("/"):
            dirname = pat[:-1]
            if dirname in parts[:-1] or (len(parts) > 1 and parts[0] == dirname):
                return True
            if rel_posix_path.startswith(dirname + "/"):
                return True
            continue
        if "/" in pat:
            if fnmatch.fnmatch(rel_posix_path, pat):
                return True
        else:
            if fnmatch.fnmatch(basename, pat):
                return True
    return False


ALWAYS_SKIP_DIRS = {".git", "__pycache__"}


def walk_install_files(install_dir: Path, gitignore_patterns):
    """Every file physically present under install_dir, relative posix paths, skipping .git/__pycache__
    and anything the repo's own .gitignore would exclude."""
    found = set()
    for root, dirnames, filenames in os.walk(install_dir):
        dirnames[:] = [d for d in dirnames if d not in ALWAYS_SKIP_DIRS]
        root_path = Path(root)
        for fname in filenames:
            full = root_path / fname
            rel = full.relative_to(install_dir).as_posix()
            if gitignore_matches(rel, gitignore_patterns):
                continue
            found.add(rel)
    return found


# --------------------------------------------------------------------------------------------------
# content comparison
# --------------------------------------------------------------------------------------------------

def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:BINARY_SNIFF_BYTES]


def normalize_text(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def files_identical(a_path: Path, b_path: Path) -> bool:
    a = a_path.read_bytes()
    b = b_path.read_bytes()
    if is_binary(a) or is_binary(b):
        return a == b
    return normalize_text(a) == normalize_text(b)


# --------------------------------------------------------------------------------------------------
# locating the installed copy
# --------------------------------------------------------------------------------------------------

def read_display_name(repo_dir: Path):
    pp = repo_dir / "pyproject.toml"
    if not pp.exists():
        return None
    text = pp.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'^\s*DisplayName\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


def ask_server_for_module(comfy_url: str):
    """Best-effort: ask a live ComfyUI server which python module one of this pack's nodes loaded from.
    Returns the python_module string, or None if the server is unreachable or none of the probe node
    names are registered. The HTTP API does not expose a filesystem path, only the module name - callers
    use this to confirm a guessed path, not to derive one from nothing."""
    for name in PROBE_NODE_NAMES:
        url = comfy_url.rstrip("/") + "/object_info/" + urllib.request.quote(name)
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as resp:
                body = resp.read()
        except (urllib.error.URLError, OSError, TimeoutError):
            return None  # server unreachable at all - no point trying the other names
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        info = data.get(name)
        if info and info.get("python_module"):
            return info["python_module"]
    return None


def resolve_install_path(args, repo_root: Path):
    """Returns (path_or_None, list_of_notes_for_the_report)."""
    notes = []

    if args.path:
        p = Path(args.path).expanduser().resolve()
        notes.append("install path from --path")
        return p, notes

    env_path = os.environ.get(ENV_INSTALL_PATH)
    if env_path:
        p = Path(env_path).expanduser().resolve()
        notes.append(f"install path from env {ENV_INSTALL_PATH}")
        return p, notes

    base_dir = os.environ.get(ENV_COMFY_BASE_DIR)
    display_name = read_display_name(repo_root) or repo_root.name
    if base_dir:
        candidate = Path(base_dir).expanduser().resolve() / "custom_nodes" / display_name
        notes.append(f"install path guessed from env {ENV_COMFY_BASE_DIR} + pyproject DisplayName: {candidate}")
        if not args.no_server_check:
            module = ask_server_for_module(args.comfy_url)
            if module is None:
                notes.append(f"could not reach ComfyUI at {args.comfy_url} to confirm this guess")
            elif display_name.lower() in module.lower() or module.lower().endswith(display_name.lower()):
                notes.append(f"server confirms a node from this pack loaded as module '{module}' - guess looks right")
            else:
                notes.append(f"WARNING: server reports this pack's module as '{module}', which does not "
                              f"match the guessed folder name '{display_name}' - the guess may be wrong")
        if candidate.is_dir():
            return candidate, notes
        notes.append(f"guessed path does not exist on disk: {candidate}")
        return None, notes

    if not args.no_server_check:
        module = ask_server_for_module(args.comfy_url)
        if module:
            notes.append(f"server confirms a node from this pack loaded as module '{module}', but the "
                          f"ComfyUI HTTP API does not expose its filesystem path - set {ENV_COMFY_BASE_DIR} "
                          f"as well so this script can build the path, or pass --path directly")
        else:
            notes.append(f"could not reach ComfyUI at {args.comfy_url}")

    return None, notes


# --------------------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------------------

def format_git_line(label: str, path: Path, state: dict) -> str:
    if not state["is_repo"]:
        return f"{label}: {path}  (not a git repository: {state['error']})"
    dirty = f"DIRTY ({state['uncommitted']} uncommitted)" if state["dirty"] else "clean"
    return f"{label}: {path}  HEAD {state['head']}  {dirty}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compare the repository's tracked files against the copy ComfyUI actually loads.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python tools/check_deploy_sync.py --path C:\\ComfyUI\\custom_nodes\\ComfyUI-OCIO\n"
            "  COMFYUI_OCIO_INSTALL_PATH=C:\\ComfyUI\\custom_nodes\\ComfyUI-OCIO python tools/check_deploy_sync.py\n"
        ),
    )
    ap.add_argument("--path", help="path to the installed copy (highest priority)")
    ap.add_argument("--repo-path", help="override the repository root (default: this script's own repo)")
    ap.add_argument("--comfy-url", default=DEFAULT_COMFY_URL, help=f"ComfyUI server URL for the confirm-a-guess "
                     f"fallback (default: {DEFAULT_COMFY_URL})")
    ap.add_argument("--no-server-check", action="store_true", help="never contact a running ComfyUI server")
    ap.add_argument("--all", action="store_true", help="also list identical files in the detail section")
    args = ap.parse_args()

    repo_root = Path(args.repo_path).expanduser().resolve() if args.repo_path else default_repo_root()

    if not repo_root.is_dir():
        print(f"ERROR: repository path does not exist: {repo_root}")
        return 2

    install_root, resolve_notes = resolve_install_path(args, repo_root)
    for n in resolve_notes:
        print("  " + n)

    if install_root is None:
        print()
        print("ERROR: could not locate the installed copy. Provide one of:")
        print("  --path <dir>")
        print(f"  env {ENV_INSTALL_PATH}=<dir>")
        print(f"  env {ENV_COMFY_BASE_DIR}=<ComfyUI root>  (with a server reachable at --comfy-url to confirm)")
        return 2

    if not install_root.is_dir():
        print(f"ERROR: installed copy path does not exist: {install_root}")
        return 2

    repo_files, repo_ls_err = git_tracked_files(repo_root)
    if repo_files is None:
        print(f"ERROR: repository is not readable as a git repo ({repo_root}): {repo_ls_err}")
        return 2

    repo_git = git_state(repo_root)
    install_git = git_state(install_root)

    gitignore_patterns = load_gitignore_patterns(repo_root)
    install_all_files = walk_install_files(install_root, gitignore_patterns)
    repo_files_set = set(repo_files)

    identical, differs, missing, extra = [], [], [], []

    for rel in sorted(repo_files):
        repo_file = repo_root / rel
        install_file = install_root / rel
        if not install_file.is_file():
            missing.append(rel)
            continue
        if files_identical(repo_file, install_file):
            identical.append(rel)
        else:
            differs.append(rel)

    for rel in sorted(install_all_files - repo_files_set):
        extra.append(rel)

    print()
    print(format_git_line("Repository", repo_root, repo_git))
    print(format_git_line("Installed ", install_root, install_git))
    print()

    head_mismatch = (
        repo_git["is_repo"] and install_git["is_repo"]
        and repo_git["head"] is not None and install_git["head"] is not None
        and repo_git["head"] != install_git["head"]
    )
    if head_mismatch:
        print(f"!! HEAD MISMATCH: repository is at {repo_git['head']}, installed copy is at {install_git['head']}.")
        print("!! The installed copy is not built from the commit the repository is on.")
    if install_git["is_repo"] and install_git["dirty"]:
        print(f"!! Installed copy has {install_git['uncommitted']} uncommitted change(s) of its own "
              f"(hand-edited or hand-copied files on top of a git checkout).")

    total = len(repo_files)
    print()
    print(f"Files: {total} tracked | {len(identical)} identical | {len(differs)} differ | "
          f"{len(missing)} missing in install | {len(extra)} extra in install")

    in_sync = not differs and not missing and not extra and not head_mismatch
    print("RESULT:", "IN SYNC" if in_sync else "OUT OF SYNC - the installed copy does not match the repository")
    print()

    if args.all:
        for rel in identical:
            print(f"  identical           {rel}")
    for rel in differs:
        print(f"  DIFFERS             {rel}")
    for rel in missing:
        print(f"  MISSING IN INSTALL  {rel}")
    for rel in extra:
        print(f"  EXTRA IN INSTALL    {rel}")

    return 0 if in_sync else 1


if __name__ == "__main__":
    sys.exit(main())
