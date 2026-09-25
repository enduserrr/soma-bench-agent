"""SOMA-BENCH orchestrator: run tasks across arms, verify, score, report.

CLI (python3 soma_bench/orchestrate.py … or -m soma_bench):
    setup           — engine copy + install symlink + arm homes (idempotent)
    verify          — read-only integrity check of the bench tree
    tasks           — list tasks in the corpus
    run             — run one task (-t NAME), or all; options:
                        --arms soma,baseline   (default both)
                        --repeats N            (default from task, else 1)
                        --max-turns N
                        --tag LABEL            (named run dir; default timestamp)
    score           — score a run dir (Dendrite SWE scoring) -> summary.json
    report          — print a run dir's summary as a compact table

Run layout (runs/<tag>/):
    run.json        — full config + per-task per-arm per-repeat records
    rows.jsonl      — flat per-task-per-arm result rows (append-only during run)
    summary.json    — scoring + comparison tables (written by `score`)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import (ARM_BASELINE, ARM_SOMA, ARM_SOMA_AGE, ARM_SOMA_GUARD, ARM_SOMA_TOK,
               ARMS, BENCH_ROOT, RUNS_DIR, TASKS_DIR, load_json, save_json, sh)
from . import scoring
from . import arm_setup, runner, verify as verify_mod


def cmd_setup(args):
    info = arm_setup.setup_bench(force=args.force)
    print(json.dumps(info, indent=2))


def cmd_verify(args):
    print(json.dumps(arm_setup.verify_bench(), indent=2))


def cmd_tasks(args):
    tasks = sorted(TASKS_DIR.glob("*.json")) if TASKS_DIR.is_dir() else []
    for t in tasks:
        spec = load_json(t)
        print(f"{t.stem}: {spec.get('title', '(untitled)')} — probes: "
              f"{'patch' if spec.get('verify_patch', True) else '-'}"
              f"{'/tests' if spec.get('test_command') else ''}"
              f"{'/needles' if spec.get('needles') else ''}")


def _load_tasks(names: list[str] | None) -> list:
    if not TASKS_DIR.is_dir():
        return []
    paths = sorted(TASKS_DIR.glob("*.json"))
    if names:
        want = set(names)
        paths = [p for p in paths if p.stem in want]
        missing = want - {p.stem for p in paths}
        if missing:
            sys.exit(f"unknown task(s): {', '.join(sorted(missing))}")
    return [(p.stem, load_json(p)) for p in paths]


def _run_one(tag_dir: Path, task_name: str, task: dict, arm: str, repeat: int,
             model_setup: dict, timeout_s: int) -> dict:
    arm_home = arm_setup.arm_home(arm)
    task_dir = tag_dir / task_name / f"{arm}_r{repeat}"
    task_dir.mkdir(parents=True, exist_ok=True)

    record = runner.run_task(arm_home, task, task_dir,
                             timeout_s=timeout_s, model_setup=model_setup)
    record.update({"task": task_name, "arm": arm, "repeat": repeat})

    usage = runner.collect_usage(arm_home, record.get("session_id") or "")
    chars = runner.collect_char_accounting(arm_home, record.get("session_id") or "")
    record["usage"] = usage
    record["chars"] = chars

    verdict = verify_mod.verify_task(task, record, arm_home=arm_home)
    record["resolved"] = verdict["resolved"]
    record["checks"] = verdict["checks"]

    # append-only log line
    with open(tag_dir / "rows.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "task": task_name, "arm": arm, "repeat": repeat,
            "resolved": record["resolved"], "session_id": record.get("session_id"),
            "exit_code": record["exit_code"], "wall_s": record["wall_s"],
            "usage": record["usage"], "chars": record["chars"],
        }, ensure_ascii=False) + "\n")

    # full record (events can be large) — one file per run
    save_json(task_dir / "record.json", record)
    print(f"  {task_name}/{arm}/r{repeat}: resolved={record['resolved']} "
          f"exit={record['exit_code']} wall={record['wall_s']}s "
          f"in={usage.get('input_tokens', 0)} cache={usage.get('cache_read_tokens', 0)} "
          f"out={usage.get('output_tokens', 0)} chars={chars.get('session_total_chars', 0)}",
          flush=True)
    return record


def cmd_run(args):
    arm_setup.setup_bench()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for a in arms:
        if a not in ARMS:
            sys.exit(f"unknown arm {a!r} (choose from {ARMS})")
    tasks = _load_tasks(args.task)
    if not tasks:
        sys.exit("no tasks matched")

    tag = args.tag or time.strftime("%Y%m%d_%H%M%S")
    tag_dir = RUNS_DIR / tag
    tag_dir.mkdir(parents=True, exist_ok=True)
    run_meta = {
        "tag": tag, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "arms": arms, "tasks": [t[0] for t in tasks], "repeats": args.repeats,
        "model": arm_setup.lift_live_config(),
    }
    save_json(tag_dir / "run.json", run_meta)
    print(f"run {tag}: tasks={[t[0] for t in tasks]} arms={arms} repeats={args.repeats}")

    model_setup = arm_setup.lift_live_config()
    if args.max_turns:
        model_setup["max_turns"] = args.max_turns

    t0 = time.time()
    for task_name, task in tasks:
        repeats = args.repeats or task.get("repeats", 1)
        for rep in range(1, repeats + 1):
            for arm in arms:
                try:
                    _run_one(tag_dir, task_name, task, arm, rep, model_setup,
                             timeout_s=args.timeout)
                except Exception as exc:  # keep the bench alive; record the failure
                    print(f"  {task_name}/{arm}/r{rep}: FAILED to run: {exc}", flush=True)
                    save_json(tag_dir / task_name / f"{arm}_r{rep}" / "error.json",
                              {"error": str(exc)})
    run_meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    run_meta["wall_s"] = round(time.time() - t0, 1)
    save_json(tag_dir / "run.json", run_meta)
    print(f"done in {run_meta['wall_s']}s -> {tag_dir}")


def cmd_reverify(args):
    """Re-run verification probes over stored run records — no API calls.

    Use after changing probe semantics (e.g. needle trajectory-wide checks) or
    task specs: verdicts update from the persisted records + arm state.db.
    Rewrites rows.jsonl (from records, not append), then rescoring is required.
    """
    tag_dir = RUNS_DIR / args.tag
    if not tag_dir.is_dir():
        sys.exit(f"no such run: {args.tag}")
    tasks_by_name = dict(_load_tasks(None))
    changed = 0
    new_rows = []
    for task_dir in sorted(p for p in tag_dir.iterdir() if p.is_dir()):
        task_name = task_dir.name
        task = tasks_by_name.get(task_name)
        if not task:
            print(f"  {task_name}: no task spec found — skipped")
            continue
        for rec_path in sorted(task_dir.glob("*_r*/record.json")):
            record = load_json(rec_path)
            arm = record.get("arm")
            if arm not in ARMS:
                continue
            arm_home = arm_setup.arm_home(arm)
            verdict = verify_mod.verify_task(task, record, arm_home=arm_home)
            if verdict["resolved"] != record.get("resolved"):
                print(f"  {task_name}/{arm}/r{record.get('repeat')}: "
                      f"{record.get('resolved')} -> {verdict['resolved']}")
                changed += 1
                record["resolved"] = verdict["resolved"]
            record["checks"] = verdict["checks"]
            save_json(rec_path, record)
            new_rows.append({
                "task": task_name, "arm": arm, "repeat": record.get("repeat"),
                "resolved": record["resolved"], "session_id": record.get("session_id"),
                "exit_code": record.get("exit_code"), "wall_s": record.get("wall_s"),
                "usage": record.get("usage"), "chars": record.get("chars"),
            })
    if not new_rows:
        sys.exit("no records found")
    with open(tag_dir / "rows.jsonl", "w", encoding="utf-8") as fh:
        for row in new_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"reverified {len(new_rows)} records ({changed} verdict changes) -> rows.jsonl rewritten")
    if changed:
        print(f"re-run: python3 -m soma_bench.orchestrate score {args.tag}")


def cmd_score(args):
    tag_dir = RUNS_DIR / args.tag
    if not tag_dir.is_dir():
        sys.exit(f"no such run: {args.tag}")
    rows = [json.loads(l) for l in (tag_dir / "rows.jsonl").read_text().splitlines() if l.strip()] \
        if (tag_dir / "rows.jsonl").exists() else []
    if not rows:
        sys.exit("no rows to score")

    summary = {"tag": args.tag, "generated": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # ---- token & char comparison (billing truth) ----
    per_task = {}
    for row in rows:
        per_task.setdefault(row["task"], {a: [] for a in ARMS})
        arm = row["arm"]
        if arm in per_task[row["task"]]:
            per_task[row["task"]][arm].append(row)

    token_table = []
    for task_name, by_arm in sorted(per_task.items()):
        entry = {"task": task_name}
        for arm in ARMS:
            rs = by_arm.get(arm, [])
            entry[arm] = _arm_agg(rs)
        entry["delta"] = _delta(entry)  # soma vs baseline
        if by_arm.get(ARM_SOMA_GUARD) and by_arm.get(ARM_SOMA):
            entry["delta_guard_vs_soma"] = _delta(
                {ARM_SOMA: entry[ARM_SOMA_GUARD], ARM_BASELINE: entry[ARM_SOMA]},
                soma_key=ARM_SOMA)
        if by_arm.get(ARM_SOMA_AGE) and by_arm.get(ARM_SOMA):
            entry["delta_age_vs_soma"] = _delta(
                {ARM_SOMA: entry[ARM_SOMA_AGE], ARM_BASELINE: entry[ARM_SOMA]},
                soma_key=ARM_SOMA)
        if by_arm.get(ARM_SOMA_TOK) and by_arm.get(ARM_SOMA):
            entry["delta_tok_vs_soma"] = _delta(
                {ARM_SOMA: entry[ARM_SOMA_TOK], ARM_BASELINE: entry[ARM_SOMA]},
                soma_key=ARM_SOMA)
        token_table.append(entry)
    summary["per_task"] = token_table

    # ---- Dendrite scoring ----
    # baseline = no-SOMA arm; miner = the SOMA arm(s). Score each SOMA variant
    # separately against the shared baseline so guard vs current compare cleanly.
    task_rows = []
    task_rows_guard = []
    task_rows_age = []
    task_rows_tok = []
    for task_name, by_arm in sorted(per_task.items()):
        base = by_arm.get(ARM_BASELINE, [])
        n_base = len(base)
        for miner_arm, out_rows in ((ARM_SOMA, task_rows), (ARM_SOMA_GUARD, task_rows_guard),
                                    (ARM_SOMA_AGE, task_rows_age), (ARM_SOMA_TOK, task_rows_tok)):
            miner = by_arm.get(miner_arm, [])
            if not miner and not base:
                continue
            x = sum(1 for r in base if r["resolved"])
            y = sum(1 for r in miner if r["resolved"])
            n = max(n_base, len(miner))
            T_B = _avg_weighted(base)
            T_A = _avg_weighted(miner)
            scored = scoring.compute_swe_task_score(x, y, n, T_B, T_A)
            scored.update({
                "task": task_name, "x": x, "y": y, "n": n,
                "T_B": T_B, "T_A": T_A, "miner_arm": miner_arm,
                "baseline_weighted_tokens": T_B, "miner_weighted_tokens": T_A,
            })
            out_rows.append(scored)
    def _score_block(rows):
        return {
            "weights": {"input": scoring.WEIGHT_INPUT, "cached": scoring.WEIGHT_CACHED,
                        "output": scoring.WEIGHT_OUTPUT},
            "per_task": rows,
            "aggregates": scoring.build_swe_miner_scores(rows),
            "final_normalized_score": scoring.build_swe_miner_total_score(
                scoring.build_swe_miner_scores(rows)["raw_total"]),
        }
    summary["dendrite_scoring"] = _score_block(task_rows)
    if task_rows_guard:
        summary["dendrite_scoring_guard"] = _score_block(task_rows_guard)
    if task_rows_age:
        summary["dendrite_scoring_age"] = _score_block(task_rows_age)
    if task_rows_tok:
        summary["dendrite_scoring_tok"] = _score_block(task_rows_tok)

    # SOMA engine accounting (bench copies only — never the live plugin)
    def _load_acct(engine_dir):
        recs = []
        acct_path = engine_dir / "accounting.jsonl"
        if acct_path.exists():
            for line in acct_path.read_text().splitlines():
                if line.strip():
                    try:
                        recs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return recs

    def _acct_block(recs):
        return {
            "records": len(recs),
            "total_input_est_chars": sum(r.get("input_est_chars", 0) for r in recs),
            "total_output_est_chars": sum(r.get("output_est_chars", 0) for r in recs),
            "total_est_tokens_saved": sum(
                r.get("input_est_tokens", 0) - r.get("output_est_tokens", 0) for r in recs),
            "records_by_reason": {
                reason: sum(1 for r in recs if r.get("reason") == reason)
                for reason in sorted({r.get("reason", "?") for r in recs})
            },
            "by_session": _group_sessions(recs),
        }

    soma_acct = _load_acct(arm_setup.BENCH_ENGINE_DIR)
    summary["soma_engine_accounting"] = _acct_block(soma_acct)
    guard_acct = _load_acct(arm_setup.GUARD_ENGINE_DIR)
    if guard_acct:
        summary["guard_engine_accounting"] = _acct_block(guard_acct)
    age_acct = _load_acct(arm_setup.AGE_ENGINE_DIR)
    if age_acct:
        summary["age_engine_accounting"] = _acct_block(age_acct)
    tok_acct = _load_acct(arm_setup.TOK_ENGINE_DIR)
    if tok_acct:
        summary["tok_engine_accounting"] = _acct_block(tok_acct)

    save_json(tag_dir / "summary.json", summary)
    print(f"scored -> {tag_dir / 'summary.json'}")
    _print_summary(summary)


def _group_sessions(recs: list) -> dict:
    by = {}
    for r in recs:
        sid = r.get("session_id", "-")
        g = by.setdefault(sid, {"records": 0, "chars_in": 0, "chars_out": 0,
                                "est_tokens_in": 0, "est_tokens_out": 0})
        g["records"] += 1
        g["chars_in"] += r.get("input_est_chars", 0)
        g["chars_out"] += r.get("output_est_chars", 0)
        g["est_tokens_in"] += r.get("input_est_tokens", 0)
        g["est_tokens_out"] += r.get("output_est_tokens", 0)
    return by


def _arm_agg(rows: list) -> dict:
    def avg(key):
        vals = [r["usage"].get(key, 0) for r in rows if r.get("usage")]
        return round(sum(vals) / len(vals), 1) if vals else 0

    return {
        "runs": len(rows),
        "resolved": sum(1 for r in rows if r["resolved"]),
        "input_tokens_avg": avg("input_tokens"),
        "cache_read_tokens_avg": avg("cache_read_tokens"),
        "output_tokens_avg": avg("output_tokens"),
        "reasoning_tokens_avg": avg("reasoning_tokens"),
        "api_calls_avg": avg("api_call_count"),
        "weighted_tokens_avg": _avg_weighted(rows),
        "chars_avg": round(sum(r.get("chars", {}).get("session_total_chars", 0) for r in rows)
                           / len(rows), 1) if rows else 0,
        "wall_s_avg": round(sum(r["wall_s"] for r in rows) / len(rows), 1) if rows else 0,
    }


def _avg_weighted(rows: list) -> float | None:
    vals = []
    for r in rows:
        u = r.get("usage")
        if not u:
            continue
        w = scoring.compute_weighted_tokens(
            u.get("input_tokens"), u.get("output_tokens"), u.get("cache_read_tokens"))
        if w is not None:
            vals.append(w)
    return round(sum(vals) / len(vals), 1) if vals else None


def _delta(entry: dict, soma_key: str = ARM_SOMA) -> dict:
    s, b = entry.get(soma_key), entry.get(ARM_BASELINE)
    if not s or not b:
        return {}
    def pct(a, b):
        return round((a - b) / b * 100, 1) if b else None
    return {
        "weighted_tokens_pct": pct(s["weighted_tokens_avg"], b["weighted_tokens_avg"]),
        "chars_pct": pct(s["chars_avg"], b["chars_avg"]),
        "input_tokens_pct": pct(s["input_tokens_avg"], b["input_tokens_avg"]),
        "cache_read_tokens_pct": pct(s["cache_read_tokens_avg"], b["cache_read_tokens_avg"]),
        "resolved_delta": s["resolved"] - b["resolved"],
    }


def cmd_report(args):
    p = RUNS_DIR / args.tag / "summary.json"
    if not p.exists():
        sys.exit(f"no summary for run {args.tag} (run `score` first)")
    _print_summary(load_json(p))


def _print_summary(s):
    print(f"\n=== SOMA-BENCH run {s['tag']} ===")
    print(f"\n-- per-task (avg over repeats) --")
    hdr = f"{'task':24} {'arm':8} {'res':>4} {'in_tok':>9} {'cache':>9} {'out_tok':>8} {'wtok':>9} {'chars':>9} {'wall_s':>7}"
    print(hdr)
    for e in s.get("per_task", []):
        for arm in ARMS:
            a = e.get(arm)
            if not a or not a.get("runs"):
                continue
            print(f"{e['task'][:24]:24} {arm:8} {a['resolved']:>4} {a['input_tokens_avg']:>9} "
                  f"{a['cache_read_tokens_avg']:>9} {a['output_tokens_avg']:>8} "
                  f"{a['weighted_tokens_avg']:>9} {a['chars_avg']:>9} {a['wall_s_avg']:>7}")
        d = e.get("delta")
        if d:
            print(f"{'':24} {'DELTA':8} wt {d['weighted_tokens_pct']}% chars {d['chars_pct']}% "
                  f"resolved {d['resolved_delta']:+d}")
        dg = e.get("delta_guard_vs_soma")
        if dg:
            print(f"{'':24} {'G-S':8} wt {dg['weighted_tokens_pct']}% chars {dg['chars_pct']}% "
                  f"resolved {dg['resolved_delta']:+d}  (guard vs current soma)")
        da = e.get("delta_age_vs_soma")
        if da:
            print(f"{'':24} {'A-S':8} wt {da['weighted_tokens_pct']}% chars {da['chars_pct']}% "
                  f"resolved {da['resolved_delta']:+d}  (age vs current soma)")
        dt = e.get("delta_tok_vs_soma")
        if dt:
            print(f"{'':24} {'T-S':8} wt {dt['weighted_tokens_pct']}% chars {dt['chars_pct']}% "
                  f"resolved {dt['resolved_delta']:+d}  (token-ladder vs current soma)")
    for key, label in (("dendrite_scoring", "miner=SOMA (current)"),
                       ("dendrite_scoring_guard", "miner=SOMA-GUARD (patched)"),
                       ("dendrite_scoring_age", "miner=SOMA-AGE (age-tiered)"),
                       ("dendrite_scoring_tok", "miner=SOMA-TOK (token ladder)")):
        ds = s.get(key)
        if not ds:
            continue
        print(f"\n-- Dendrite SWE scoring (baseline=no-SOMA, {label}) --")
        for t in ds.get("per_task", []):
            det = t.get("detail", {})
            print(f"  {t['task'][:24]:24} x={t['x']} y={t['y']} n={t['n']} r={det.get('r', 0):.3f} "
                  f"score={t['score']} pool={t['pool']} zone={det.get('zone')}")
        agg = ds.get("aggregates", {})
        print(f"  main={agg.get('main_score'):.3f} hard_boost={agg.get('hard_boost'):.3f} "
              f"raw={agg.get('raw_total'):.3f} "
              f"final_normalized={ds.get('final_normalized_score'):.3f}")
    for key, label in (("soma_engine_accounting", "SOMA engine (bench copy)"),
                       ("guard_engine_accounting", "GUARD engine (bench copy)"),
                       ("age_engine_accounting", "AGE engine (bench copy)"),
                       ("tok_engine_accounting", "TOK engine (bench copy)")):
        se = s.get(key)
        if not se:
            continue
        print(f"\n-- {label} --")
        print(f"  records={se['records']} est_tokens_saved={se['total_est_tokens_saved']}"
              f" by_reason={se.get('records_by_reason', {})}")
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="soma_bench")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="engine copy + install symlink + arm homes")
    p.add_argument("--force", action="store_true", help="recreate everything")
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("verify", help="read-only bench integrity check")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("tasks", help="list tasks in the corpus")
    p.set_defaults(fn=cmd_tasks)

    p = sub.add_parser("run", help="run tasks across arms")
    p.add_argument("-t", "--task", action="append", help="task name (repeatable); default all")
    p.add_argument("--arms", default="soma,baseline,soma-age",
                   help="comma-separated arms (soma, baseline, soma-guard, soma-age)")
    p.add_argument("--repeats", type=int, default=0, help="0 = task default (1)")
    p.add_argument("--max-turns", type=int, default=0)
    p.add_argument("--timeout", type=int, default=1800, help="per-run seconds")
    p.add_argument("--tag", default=None, help="run tag; default timestamp")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("score", help="score a run dir")
    p.add_argument("tag", help="run tag under runs/")
    p.set_defaults(fn=cmd_score)

    p = sub.add_parser("reverify", help="re-run probes over stored records (no API calls)")
    p.add_argument("tag", help="run tag under runs/")
    p.set_defaults(fn=cmd_reverify)

    p = sub.add_parser("report", help="print an existing summary")
    p.add_argument("tag", help="run tag under runs/")
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
