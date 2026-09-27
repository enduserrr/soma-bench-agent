#!/usr/bin/env python3
"""Regenerate soma_bench/patches/cap16k.patch deterministically.

Single change: PASSTHROUGH_CHARS + MID_CEILING_CHARS 24_000 -> 16_000.
The ladder collapses to one rule (under 16K untouched, over 16K capped at
16K) exactly as the live 24K setup does — just a tighter floor. Everything
else (compress-once-at-birth, extractive pinning, envelope handling, cache
stability) is byte-identical to the live engine.
"""
from __future__ import annotations
import difflib, shutil, subprocess, sys, tempfile
from pathlib import Path

LIVE = Path.home() / ".hermes/plugins/context_engine/soma/engine.py"
OUT = Path(__file__).resolve().parent / "cap16k.patch"

OLD = """PASSTHROUGH_CHARS = 24_000
MID_CEILING_CHARS = 24_000"""
NEW = """PASSTHROUGH_CHARS = 16_000
MID_CEILING_CHARS = 16_000"""

# Flat-cap override: the sizing formula is
#   min(MAX_KEEP_CHARS, max(MIN_PASSTHROUGH_CHARS, 60% * len))
# so lowering the floor alone leaves MAX_KEEP_CHARS=24K -> a partial ladder
# (27-40K results keep 60%, >40K keep 24K), NOT the approved flat 16K cap.
# Clamp MAX_KEEP_CHARS to the floor in _load_soma (mirrors the existing
# MIN_PASSTHROUGH_CHARS host-override pattern; vendored core stays pristine).
OVR_OLD = """        if PASSTHROUGH_CHARS != mod.MIN_PASSTHROUGH_CHARS:
            mod.MIN_PASSTHROUGH_CHARS = PASSTHROUGH_CHARS"""
OVR_NEW = """        if PASSTHROUGH_CHARS != mod.MIN_PASSTHROUGH_CHARS:
            mod.MIN_PASSTHROUGH_CHARS = PASSTHROUGH_CHARS
        mod.MAX_KEEP_CHARS = PASSTHROUGH_CHARS  # bench cap16k: flat cap, keep == floor"""

def main() -> int:
    live = LIVE.read_text(encoding="utf-8")
    assert live.count(OLD) == 1, f"anchor matched {live.count(OLD)} times"
    assert live.count(OVR_OLD) == 1, f"override anchor matched {live.count(OVR_OLD)} times"
    edited = live.replace(OLD, NEW).replace(OVR_OLD, OVR_NEW)
    assert "MAX_KEEP_CHARS = PASSTHROUGH_CHARS" in edited
    diff = difflib.unified_diff(live.splitlines(keepends=True),
                                edited.splitlines(keepends=True),
                                fromfile="engine.py", tofile="engine.py")
    OUT.write_text("".join(diff), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "soma"
        shutil.copytree(LIVE.parent, stage,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "bench_results", "accounting.jsonl"))
        r = subprocess.run(["patch", "-p0", "--batch", "-i", str(OUT)],
                           cwd=str(stage), capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"patch failed:\n{r.stdout}\n{r.stderr}")
        r = subprocess.run([sys.executable, "-m", "py_compile", str(stage / "engine.py")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"compile failed:\n{r.stderr}")
    print("patch applies cleanly and compiles")
    return 0

if __name__ == "__main__":
    sys.exit(main())
