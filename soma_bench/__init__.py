"""Shared constants + helpers for the SOMA A/B bench (SOMA-BENCH).

Layout (all under ~/git/SOMA-BENCH/):
    tasks/          task corpus (JSON problem statements + verification spec)
    profiles/       scratch HERMES_HOMEs, one per ARM (soma / baseline) — never ~/.hermes
    engines/        copied SOMA engine (dir name 'somabench') so accounting.jsonl
                    lands in the bench tree, NOT in the live plugin
    runs/           one run dir per bench execution: run.json, per-task rows,
                    trajectory logs, final summary
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

BENCH_ROOT = Path(os.environ.get("SOMABENCH_ROOT", Path(__file__).resolve().parent.parent)).resolve()
TASKS_DIR = BENCH_ROOT / "tasks"
PROFILES_DIR = BENCH_ROOT / "profiles"
ENGINES_DIR = BENCH_ROOT / "engines"
RUNS_DIR = BENCH_ROOT / "runs"

# The LIVE plugin — read-only reference; NEVER modify, copy on demand.
LIVE_SOMA_PLUGIN = Path.home() / ".hermes" / "plugins" / "context_engine" / "soma"
INSTALL_TREE = Path.home() / ".hermes" / "hermes-agent"
INSTALL_ENGINES = INSTALL_TREE / "plugins" / "context_engine"

ARM_SOMA = "soma"
ARM_BASELINE = "baseline"
ARM_SOMA_GUARD = "soma-guard"
ARM_SOMA_AGE = "soma-age"
ARM_SOMA_TOK = "soma-tok"
ARM_SOMA_16K = "soma-16k"
ARMS = (ARM_SOMA, ARM_BASELINE, ARM_SOMA_GUARD, ARM_SOMA_AGE, ARM_SOMA_TOK, ARM_SOMA_16K)

# Install-tree symlink that points at the bench's ENGINE COPY (never the live plugin).
BENCH_ENGINE_SYMLINK = INSTALL_ENGINES / "somabench"
BENCH_ENGINE_DIR = ENGINES_DIR / "somabench"

# Model setup both arms share (identical except context.engine).
# Toolsets: no skills (the bench's own skill would otherwise load mid-task and
# contaminate arms), no web/browser/delegation/memory/session_search — tasks are
# self-contained; fewer tools = fewer divergence sources between arms.
DEFAULT_TOOLSETS = "file,terminal,code_execution"

LIVE_CONFIG = Path.home() / ".hermes" / "config.yaml"


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


def sh(cmd: list[str], *, cwd=None, env=None, timeout=None, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, env=env, timeout=timeout, check=check,
        capture_output=True, text=True,
    )


def sqlite_ro(uri: str) -> "sqlite3.Connection":
    import sqlite3
    return sqlite3.connect(f"file:{uri}?mode=ro", uri=True)
