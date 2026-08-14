"""Regression: no shipped source file carries a control character that its consumer will choke on.

WHY THIS EXISTS. On 2026-08-13 a comment in web/ocio_io.js was edited by a script whose replacement text
contained a Windows path. The editing language interpreted the escape, and a real carriage return landed in
the middle of a line comment. The line split, the remainder became garbage, and the BROWSER refused to parse
the module - so every front-end feature in the pack disappeared at once: OCIO Read's preview, OCIO Player's
viewport, OCIO Write's buttons. The nodes still executed; nothing could be seen or clicked.

The check that would have caught it did not exist, and the checks that did exist all passed:

  * `node --check` accepted the file. A lone CR terminates a line comment as far as Node is concerned, so
    the damage was invisible to it. The consumer that mattered was the browser, and nobody asked it.
  * The Python gate was green, because none of this is Python.
  * Reading the diff showed a plausible-looking comment: the CR is invisible in a diff.

So this test looks for the SYMPTOM in every shipped file rather than trusting any one parser. It is cheap,
it runs in well under a second, and it fails loudly with the file and line.

WHAT COUNTS AS DAMAGE. Any carriage return that is not part of a CRLF pair, and any of the control
characters below. Tab, newline and CRLF are normal and are not flagged.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Control characters with no legitimate place in this pack's sources. NUL and SUB in particular are what a
# truncated or mis-encoded write leaves behind; ESC is what a terminal capture drags in.
FORBIDDEN = {
    0x00: "NUL", 0x07: "BEL", 0x08: "BACKSPACE", 0x0b: "VERTICAL TAB",
    0x0c: "FORM FEED", 0x1a: "SUB", 0x1b: "ESC", 0x7f: "DEL",
}

# Everything the pack actually ships as source. Binary assets are skipped by extension rather than by
# sniffing, so a new .py or .js can never quietly fall outside the scan.
TEXT_EXT = {".py", ".js", ".json", ".md", ".toml", ".txt", ".yml", ".yaml", ".cfg", ".sh"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "scratchpad", ".claude"}


def shipped_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in TEXT_EXT:
                yield os.path.join(dirpath, name)


def line_of(raw, index):
    return raw[:index].count(b"\n") + 1


def main():
    problems = []
    scanned = 0
    for path in shipped_files():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        raw = open(path, "rb").read()
        scanned += 1

        # A CR that is not followed by LF. This is the exact shape of the 2026-08-13 defect.
        i = 0
        while True:
            i = raw.find(b"\r", i)
            if i < 0:
                break
            if raw[i + 1:i + 2] != b"\n":
                problems.append(f"{rel}:{line_of(raw, i)}: a carriage return that is not part of CRLF - "
                                "this is what splits a line comment and breaks the file for its parser")
            i += 1

        for code, name in FORBIDDEN.items():
            j = raw.find(bytes([code]))
            if j >= 0:
                problems.append(f"{rel}:{line_of(raw, j)}: control character {name} (0x{code:02x})")

    if problems:
        print("SOURCE INTEGRITY FAILED:")
        for p in problems:
            print("  " + p)
        raise SystemExit(1)
    print(f"[PASS] {scanned} shipped source files carry no stray control characters")


if __name__ == "__main__":
    main()
