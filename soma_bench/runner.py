"""Run one task against one arm: hermes one-shot under the arm's HERMES_HOME.

Execution model:
- hermes chat --query-file <file> --format stream-json --max-turns N (one-shot)
- run in a fresh per-task scratch workdir (repo copy or synthetic tree) so tasks
  never touch real repos
- capture stream-json events (session_id, tokens) + exit code
- token/char truth comes from the arm's state.db (read-only) keyed by session_id
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import BENCH_ROOT, sh, sqlite_ro


def _arm_db(home: Path) -> Path:
    return home / "state.db"


def run_task(arm_home: Path, task: dict, task_dir: Path, *,
             timeout_s: int = 1800, model_setup: dict | None = None,
             run_env_extra: dict | None = None) -> dict:
    """Execute one task on one arm. Returns a run record (no scoring)."""
    workdir = task_dir / "work"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    # Materialize the task workspace (repo copy or synthetic tree)
    if task.get("repo_copy"):
        src = BENCH_ROOT / task["repo_copy"]
        shutil.copytree(src, workdir, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    for rel, content in (task.get("files") or {}).items():
        f = workdir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    if task.get("generate_log"):
        _generate_log(task["generate_log"], workdir)

    prompt_path = task_dir / "prompt.txt"
    prompt_path.write_text(task["prompt"], encoding="utf-8")

    # Clean child env: whitelist PASS-style inheritance. The parent (desktop/
    # gateway) session sets many HERMES_* policy vars that must NOT leak into a
    # bench arm (they would flip quiet/yolo/approval behavior per parent surface).
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    env["HERMES_HOME"] = str(arm_home)
    # Credentials: keys are resolved by the arm from its own .env symlink, but
    # the parent's provider keys are passed through explicitly so both arms see
    # identical credentials even if the arm .env were missing.
    for k, v in os.environ.items():
        if k.startswith("HERMES_CUSTOM_") and k.endswith("_API_KEY"):
            env.setdefault(k, v)
    env.update(run_env_extra or {})

    cmd = ["hermes", "chat",
           "--query-file", str(prompt_path),
           "--format", "stream-json",
           "--max-turns", str(model_setup.get("max_turns", 125) if model_setup else 125),
           "--in", str(workdir)]
    ms = model_setup or {}
    if ms.get("toolsets"):
        cmd += ["-t", ms["toolsets"]]
    if ms.get("model"):
        cmd += ["-m", ms["model"]]
    if ms.get("provider"):
        cmd += ["--provider", ms["provider"]]
    if ms.get("reasoning"):
        cmd += ["--reasoning", ms["reasoning"]]

    t0 = time.time()
    proc = subprocess.run(
        cmd, cwd=str(workdir), env=env, timeout=timeout_s, capture_output=True, text=True)
    wall_s = time.time() - t0

    events = _parse_stream_json(proc.stdout)
    session_id = _extract_session_id(events, proc.stderr)
    return {
        "arm": arm_home.name,
        "workdir": str(workdir),
        "exit_code": proc.returncode,
        "wall_s": round(wall_s, 1),
        "session_id": session_id,
        "events": events,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def _generate_log(spec: dict, workdir: Path) -> Path:
    """Deterministic synthetic log with ONE needle block at a fixed line.

    This is the context-pressure generator: the agent must READ this large file
    (huge tool result) to find the needle — exactly the regime SOMA targets
    (>24K chars oversized tool results).
    """
    import hashlib
    lines = spec.get("lines", 2000)
    needle_line = spec.get("needle_line", max(1, lines // 2))
    filler = spec.get("filler") or ["INFO filler line for the synthetic log"]
    needle = spec.get("needle_content", "NEEDLE")
    path = workdir / spec["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = hashlib.sha256(str(spec.get("path", "log")).encode()).digest()
    out = []
    fn = filler + [f"{filler[0]} seq={i:08d}" for i in range(10)]
    for i in range(1, lines + 1):
        if i == needle_line:
            out.extend(needle.splitlines())
            continue
        pick = filler[(seed[i % len(seed)] + i) % len(filler)]
        out.append(f"{pick} #{i:06d}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def _parse_stream_json(stdout: str) -> list:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "type" in obj:
            events.append(obj)
    return events


def _extract_session_id(events: list, stderr: str) -> str | None:
    for ev in events:
        if ev.get("type") == "result" and ev.get("session_id"):
            return ev["session_id"]
        if ev.get("type") == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
            sid = ev.get("session_id")
            if sid:
                return sid
    for line in reversed(stderr.splitlines()):
        if line.startswith("session_id: "):
            return line.split(": ", 1)[1].strip()
    return None


def collect_usage(arm_home: Path, session_id: str) -> dict:
    """Billing-truth usage for one run, read-only from the arm's state.db."""
    db = _arm_db(arm_home)
    if not db.exists() or not session_id:
        return {}
    con = sqlite_ro(db)
    try:
        row = con.execute(
            "select input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
            "reasoning_tokens, api_call_count, model, estimated_cost_usd, actual_cost_usd "
            "from sessions where id = ?", (session_id,)).fetchone()
        if not row:
            return {}
        usage = dict(zip(
            ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
             "reasoning_tokens", "api_call_count", "model", "estimated_cost_usd", "actual_cost_usd"],
            row))
        usage["per_model"] = [
            dict(zip(["model", "task", "api_call_count", "input_tokens", "output_tokens",
                      "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"], r))
            for r in con.execute(
                "select model, task, api_call_count, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, reasoning_tokens "
                "from session_model_usage where session_id = ?", (session_id,))]
        return usage
    finally:
        con.close()


def collect_char_accounting(arm_home: Path, session_id: str) -> dict:
    """Persisted-history char accounting from the arm's state.db.

    NOTE: `api_content` is NULL in one-shot runs, so this measures the PERSISTED
    conversation history (content), not the exact per-request payloads. The
    request-level truth is the provider-reported token counts (collect_usage);
    this char metric tracks how much history the agent accumulated per tool/
    role — divergence between arms here reflects different tool-result retention
    (SOMA compresses per-request but never mutates persisted history).
    """
    db = _arm_db(arm_home)
    if not db.exists() or not session_id:
        return {}
    con = sqlite_ro(db)
    try:
        rows = con.execute(
            "select role, tool_name, length(coalesce(api_content, content)), "
            "count(*) from messages where session_id = ? group by role, tool_name",
            (session_id,)).fetchall()
        by_role = {}
        by_tool = {}
        total = 0
        for role, tool_name, chars, _cnt in rows:
            by_role[role] = by_role.get(role, 0) + chars
            if tool_name:
                by_tool[tool_name] = by_tool.get(tool_name, 0) + chars
            total += chars
        n_msgs = con.execute(
            "select count(*) from messages where session_id = ?", (session_id,)).fetchone()[0]
        return {
            "session_total_chars": total,
            "by_role": by_role,
            "by_tool": by_tool,
            "message_count": n_msgs,
        }
    finally:
        con.close()
