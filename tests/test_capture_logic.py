#!/usr/bin/env python3
"""Offline unit checks for capture.py logic that needs no camera, torch
or network: device normalization, free-space math, argument validation,
same-second filename collision suffixes, and the retention purge.

Run:  python3 -I tests/test_capture_logic.py
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import capture  # noqa: E402

PASS = 0
FAIL = 0


def check(label, ok):
    global PASS, FAIL
    print(("  ok  " if ok else "  FAIL") + f"  {label}")
    PASS, FAIL = PASS + (1 if ok else 0), FAIL + (1 if not ok else 0)


def main():
    print("[1] normalize_device (fswebcam-style bare index)")
    check("bare indexes map to /dev/videoN",
          capture.normalize_device("0") == "/dev/video0"
          and capture.normalize_device("1") == "/dev/video1")
    check("real paths pass through untouched",
          capture.normalize_device("/dev/video0") == "/dev/video0"
          and capture.normalize_device("/dev/v4l-subdev0") == "/dev/v4l-subdev0")

    print("[2] free_gib matches shutil.disk_usage")
    tmp = tempfile.mkdtemp(prefix="caplogic-")
    check("same filesystem, same number",
          abs(capture.free_gib(tmp)
              - shutil.disk_usage(tmp).free / capture.GIB) < 1e-6)

    print("[3] parse_args validation")
    def exits(argv):
        try:
            capture.parse_args(argv)
        except SystemExit:
            return True
        return False
    check("invalid values are rejected",
          exits(["--interval", "0", "--no-detect"])
          and exits(["--min-free-gib", "-1", "--no-detect"])
          and exits(["--infer-size", "0", "--no-detect"])
          and exits(["--retain-days", "-1", "--no-detect"]))
    args = capture.parse_args(["--infer-size", "640", "--retain-days", "7",
                               "--no-detect"])
    check("the new flags parse", args.infer_size == 640 and args.retain_days == 7)

    print("[4] unused_capture_path (same-second collisions)")
    names = Path(tmp) / "names"
    names.mkdir()
    first = capture.unused_capture_path(names, "2026-11-22_101010")
    check("first path is plain <stamp>.jpg",
          first.name == "2026-11-22_101010.jpg")
    first.write_bytes(b"x")
    second = capture.unused_capture_path(names, "2026-11-22_101010")
    check("collision gets -2", second.name == "2026-11-22_101010-2.jpg")
    second.write_bytes(b"x")
    third = capture.unused_capture_path(names, "2026-11-22_101010")
    check("then -3", third.name == "2026-11-22_101010-3.jpg")

    print("[5] purge_old_captures")
    ret = Path(tmp) / "ret"
    ret.mkdir()
    old_a = ret / "2026-08-01_120000.jpg"
    old_a.write_bytes(b"a" * 100)
    old_b = ret / "2026-08-01_120001-2.jpg"  # collision suffix must purge too
    old_b.write_bytes(b"b" * 50)
    fresh = ret / "2026-11-22_120000.jpg"
    fresh.write_bytes(b"c" * 10)
    stranger = ret / "notes.txt"  # old, but not ours to delete
    stranger.write_bytes(b"d" * 40)
    sub = ret / "sub"
    sub.mkdir()
    sub_old = sub / "2026-08-01_120002.jpg"  # old, but in a subdirectory
    sub_old.write_bytes(b"e" * 70)
    old = time.time() - 40 * 86400
    for p in (old_a, old_b, stranger, sub_old):
        os.utime(p, (old, old))
    count, freed = capture.purge_old_captures(ret, 30)
    check("only old top-level pattern matches are deleted",
          count == 2 and freed == 150)
    check("fresh capture, strangers and subdirectories survive",
          fresh.exists() and stranger.exists() and sub_old.exists())

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
