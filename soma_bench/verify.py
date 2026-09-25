"""Task verification: run the task's probes against the agent's workdir.

Probe types (per task spec):
- patch: agent changed files vs the pristine baseline (diff against `files`).
- test_command: shell command(s) run in the workdir; ok if exit code 0.
- needles: substrings that must surface during the run — final answer OR any
  persisted message of the session (trajectory-wide: the needle must be
  AVAILABLE to the agent; restating it in prose is not required).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import sh


def verify_task(task: dict, record: dict, arm_home: Path | None = None) -> dict:
    """Returns {'resolved': bool, 'checks': {...}} — resolved iff ALL enabled probes pass."""
    workdir = Path(record["workdir"])
    checks = {}

    # 1. patch probe: did the agent modify files vs the pristine baseline?
    if task.get("verify_patch", True):
        changed = _list_changed_files(task, workdir)
        checks["patch"] = {
            "changed_files": changed,
            "ok": bool(changed) if task.get("require_patch", True) else True,
        }

    # 2. test command probe
    if task.get("test_command"):
        ok, out = _run_tests(task, workdir)
        checks["tests"] = {"ok": ok, "output_tail": out[-1500:]}

    # 3. needle probe: needle must surface in the run — final answer OR any
    #    persisted message (the agent found it via a tool result but may not
    #    restate it in prose). Trajectory-wide check measures "the needle was
    #    available to the agent", which is what compression fidelity affects.
    if task.get("needles"):
        final = _final_text(record)
        traj = final + "\n" + _trajectory_text(record, arm_home)
        hits = {n: (n in final) for n in task["needles"]}
        found = {n: (n in traj) for n in task["needles"]}
        checks["needles"] = {
            "ok": all(found.values()),
            "in_final_answer": hits,
            "in_trajectory": found,
        }

    resolved = all(c.get("ok", False) for c in checks.values()) if checks else bool(record.get("exit_code") == 0)
    return {"resolved": resolved, "checks": checks}


def _trajectory_text(record: dict, arm_home: Path | None) -> str:
    """All persisted message content for the run's session (read-only)."""
    sid = record.get("session_id") or ""
    if not arm_home or not sid:
        # fall back to stream events (tool results are capped at 5K each there)
        parts = []
        for ev in record.get("events", []):
            if ev.get("type") == "tool_result":
                parts.append(str(ev.get("output", "")))
            elif ev.get("type") == "tool_use":
                parts.append(json.dumps(ev.get("input") or {}, ensure_ascii=False))
        return "\n".join(parts)
    from .runner import collect_char_accounting  # noqa: F401 (doc pointer)
    import sqlite3
    db = arm_home / "state.db"
    if not db.exists():
        return ""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select coalesce(content,''), coalesce(api_content,'') from messages where session_id = ?",
            (sid,)).fetchall()
        return "\n".join(a or b or "" for a, b in rows)
    finally:
        con.close()


def _final_text(record: dict) -> str:
    parts = [ev.get("text", "") for ev in record.get("events", []) if ev.get("type") == "text"]
    return "".join(parts) or (record.get("stdout_tail") or "")


def _pristine_dir(task: dict, workdir: Path) -> Path | None:
    pc = task.get("pristine_copy")
    if pc:
        return Path(pc) if pc.startswith("/") else workdir.parent / pc
    return None


def _list_changed_files(task: dict, workdir: Path) -> list:
    # Compare against embedded 'files' baseline if provided; else against a
    # pristine copy dir; else report any non-ignorable file newer than workdir creation.
    baseline = task.get("files") or {}
    pristine = _pristine_dir(task, workdir)
    changed = []
    if pristine and pristine.is_dir():
        import filecmp
        for f in workdir.rglob("*"):
            if f.is_dir() or any(p in {".git", "__pycache__"} for p in f.relative_to(workdir).parts):
                continue
            rel = f.relative_to(workdir)
            pf = pristine / rel
            if not pf.exists() or not filecmp.cmp(pf, f, shallow=False):
                changed.append(str(rel))
        # files removed by the agent
        for f in pristine.rglob("*"):
            if f.is_file():
                rel = f.relative_to(pristine)
                if not (workdir / rel).exists():
                    changed.append(f"-{rel}")
    elif baseline:
        for rel, content in baseline.items():
            f = workdir / rel
            if not f.exists() or f.read_text(encoding="utf-8", errors="ignore") != content:
                changed.append(str(rel))
    else:
        # fallback: files created/modified during the run window
        for f in workdir.rglob("*"):
            if f.is_file() and not any(p in {".git", "__pycache__"} for p in f.relative_to(workdir).parts):
                changed.append(str(f.relative_to(workdir)))
    return sorted(set(changed))


def _run_tests(task: dict, workdir: Path) -> tuple[bool, str]:
    cmd = task["test_command"]
    if isinstance(cmd, str):
        cmd = [cmd]
    all_out = []
    ok = True
    for c in cmd:
        proc = sh(["bash", "-lc", c], cwd=str(workdir), timeout=task.get("test_timeout", 300))
        all_out.append(f"$ {c}\n{proc.stdout}\n{proc.stderr}")
        ok = ok and proc.returncode == 0
    return ok, "\n".join(all_out)
