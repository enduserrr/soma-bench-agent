#!/usr/bin/env python3
"""Build sanitized public result sheets from local bench run data.

Reads runs/*/rows.jsonl (session-ids stripped — never emitted), writes:
  results/<tag>.csv        per-run rows (arm, task, resolved, tokens, chars, wall)
  results/RESULTS.md       master summary of every experiment + verdicts
Idempotent: safe to re-run; output overwritten each time.
"""
from __future__ import annotations
import json
import csv
from pathlib import Path

BENCH = Path.home() / "git/SOMA-BENCH"
RUNS = BENCH / "runs"
OUT = BENCH / "results"
OUT.mkdir(exist_ok=True)

VERDICTS = {
    "smoke001": ("Parity smoke (small result)", "PARITY — both arms resolved; SOMA never engaged (results under floor)"),
    "smoke002": ("Static needle hunt", "PARITY — both resolved after trajectory-wide needle fix"),
    "smoke003": ("Build-notes needle hunt", "SOMA engaged (46 events) but +43% weighted tokens (cache invalidation); baseline failed probe"),
    "smoke004": ("Runtime audit (forced oversized)", "SOMA WIN — −31% weighted tokens, −94% resident chars, needle preserved"),
    "guard-ab": ("Marginal-guard A/B/B (003+004)", "INCONCLUSIVE on normal shapes — guard never fired (all rewrites save 30–64%)"),
    "guard-megadump": ("Marginal-guard decisive test (005)", "NEGATIVE — guard +90.8% weighted tokens vs soma; skipped result stays resident, cache break happens anyway"),
    "age-long-invalid-no-ratchets": ("Age-tier attempt 1 (invalid)", "INVALID — ratchets never fired (history is raw; patch applied on wrong layer); kept for the record"),
    "age-long2": ("Age-tier reverse-Pareto (006)", "NEGATIVE — recall audit PASSED (fidelity confirmed) but +210% weighted tokens; band crossings break cache prefix"),
    "tok-ladder": ("Token-unit ladder (004+005)", "NEGATIVE (structural) — terminal pre-capped at 50K chars; ladder trigger unreachable; 0 compression events"),
    "cap16k-invalid-partial-ladder": ("Flat-16K attempt 1 (invalid)", "INVALID — patch left a partial ladder (MAX_KEEP unclamped); archived"),
    "cap16k-flat": ("Flat 16K birth cap (004+006)", "POSITIVE — 006: −24.3% weighted, −93% resident chars, 2/2 resolved; 004: wash. First variant to beat the live 24K config"),
}

EXCLUDE_RUNS = {  # (task, arm, repeat) cells documented as contaminated/invalid
    ("cap16k-flat", "soma-bench-004", "soma-16k", 1): "harness-contaminated (stale pre-fixed file grepped from old run dir; workdirs since isolated in /tmp)",
}


def wtok(u):
    return u.get("input_tokens", 0) + 0.1 * u.get("cache_read_tokens", 0) + 3 * u.get("output_tokens", 0)


def main() -> None:
    index = []
    for rows_path in sorted(RUNS.glob("*/rows.jsonl")):
        tag = rows_path.parent.name
        rows = [json.loads(l) for l in rows_path.read_text().splitlines() if l.strip()]
        csv_path = OUT / f"{tag}.csv"
        with open(csv_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["task", "arm", "repeat", "resolved", "excluded", "weighted_tokens",
                        "input_tokens", "cache_read_tokens", "output_tokens",
                        "resident_chars", "wall_s"])
            for r in rows:
                u = r.get("usage") or {}
                key = (tag, r["task"], r["arm"], r["repeat"])
                w.writerow([r["task"], r["arm"], r["repeat"], r["resolved"],
                            "yes" if key in EXCLUDE_RUNS else "",
                            round(wtok(u)), u.get("input_tokens", 0),
                            u.get("cache_read_tokens", 0), u.get("output_tokens", 0),
                            (r.get("chars") or {}).get("session_total_chars", 0),
                            r.get("wall_s", "")])
        index.append(tag)
    print("wrote:", ", ".join(f"{t}.csv" for t in index))
    _write_md(index)
    print("wrote results/RESULTS.md")


def _write_md(tags: list) -> None:
    lines = [
        "# SOMA-BENCH result sheets",
        "",
        "Every A/B experiment run on this harness (single Hermes instance, matched arms;",
        "scoring per the Dendrite SOMA-benchmark spec: weighted tokens = input×1.0 +",
        "cache_read×0.1 + output×3.0). Per-run data in `<tag>.csv`; methodology and full",
        "analysis in `REPORT-*.md` at the repo root. `excluded=yes` rows are documented",
        "invalid runs (harness contamination or patch bug) — kept visible, never counted.",
        "",
        "| run | experiment | verdict |",
        "|---|---|---|",
    ]
    for tag in tags:
        title, verdict = VERDICTS.get(tag, ("", "—"))
        lines.append(f"| `{tag}` | {title} | {verdict} |")
    lines += [
        "",
        "## Headline numbers (honest runs only)",
        "",
        "**SOMA vs no-SOMA baseline** (tasks 003–005, runs `smoke004`, `guard-*`):",
        "−31% to −71% weighted tokens on oversized-result tasks, resolution parity or better.",
        "",
        "**Flat 16K-char birth cap vs live 24K** (`cap16k-flat`, glm-5.3-flash):",
        "- medium session (8×50K reports, ~30–45 turns): **−24.3% weighted tokens, −93% resident chars, 2/2 resolved**",
        "- short session (~5 turns): wash (−7.4% weighted, +9.4% raw, within ±20% single-run variance)",
        "",
        "**Rejected variants** (each negative for a distinct, evidenced reason):",
        "- marginal-rewrite guard (skip <2% rewrites): +90.8% on the real marginal shape",
        "- age-tiered re-squeeze (reverse-Pareto): +210% (cache-prefix breaks per band crossing)",
        "- token-unit ladder (18K/32K): structurally inert (terminal results pre-capped at 50K chars)",
        "",
    ]
    (OUT / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
