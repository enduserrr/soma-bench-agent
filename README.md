# SOMA-BENCH

A/B benchmark for the SOMA context compressor on this Hermes instance. Runs
SWE-bench-style tasks through **two matched Hermes subagents** — one with the
SOMA context engine (a bench-isolated copy), one with the stock built-in
compressor — and scores token/char savings + task performance with the Dendrite
SOMA SWE scoring formulas.

Maintained by the `soma-bench` skill (Hermes: `skill_view(name="soma-bench")`).
That skill is the operating manual; this README is the short orientation.

## Safety model

- The LIVE plugin at `~/.hermes/plugins/context_engine/soma/` is **never
  modified, never loaded** by the bench. A byte-verified copy lives at
  `engines/somabench/`, loaded via an install-tree symlink
  `~/.hermes/hermes-agent/plugins/context_engine/somabench` (an ADDITIONAL name
  — the live `soma` symlink is untouched). SOMA's `accounting.jsonl` therefore
  writes inside this repo, never into the live plugin.
- Arms run under scratch `HERMES_HOME`s at `profiles/<arm>/` (own `state.db`,
  config, empty skills; `.env` symlinked for identical credentials). The live
  `~/.hermes/state.db` is never written.
- Arms differ ONLY in `context.engine` (`somabench` vs `compressor`); everything
  else (model, provider, reasoning, toolsets, max_turns) is lifted from the
  live config so both arms match the instance.

## Usage

```bash
cd ~/git/SOMA-BENCH
python3 -m soma_bench.orchestrate setup    # engine copy + symlink + arm homes
python3 -m soma_bench.orchestrate verify   # integrity check (read-only)
python3 -m soma_bench.orchestrate tasks    # list the corpus
python3 -m soma_bench.orchestrate run --tag myrun           # all tasks, both arms
python3 -m soma_bench.orchestrate run -t soma-bench-004 --tag t4
python3 -m soma_bench.orchestrate score myrun               # Dendrite scoring
python3 -m soma_bench.orchestrate report myrun
```

## Task corpus

| Task | Type | What it stresses |
|---|---|---|
| 001 | SWE-lite bugfix | baseline parity; small tool results (SOMA no-op) |
| 002 | static needle hunt | large static file; agents may grep around it (dodge risk) |
| 003 | static needle hunt | same, build-notes variant |
| 004 | **runtime audit** | >24K terminal output produced at runtime (ungreppable in advance), needle mid-report, must re-run after fix → oversized result re-sent across turns — the true SOMA regime |

Task 004 is the reference design for SOMA-regime tasks. Static-file tasks
(002/003) measure whether the agent reads the file whole vs greps; keep them
as low-pressure/dodge-behavior probes, but don't expect SOMA savings there.

## Metrics captured per arm-run

- Provider-reported tokens (billing truth, read-only sqlite from the arm's
  state.db): input / output / cache_read / cache_write / reasoning, api calls.
- Persisted-history chars per role/tool (`messages.content`).
- Stream-json events: tool calls, durations, final text.
- SOMA engine accounting (bench copy only): per-request chars/tokens in→out,
  results capped, reason.
- Dendrite SWE score per task + aggregate (weights 1.0/0.1/3.0, r =
  clamp(log2(T_B/T_A)), zones, hard boost, normalized [-1,1]).

## Results layout

`runs/<tag>/`: `run.json` (config), `rows.jsonl` (one row per task-arm-repeat),
`<task>/<arm>_r<N>/record.json` (full record incl. events), `work/` (the
agent's actual workspace), `summary.json` (scored comparison).

## Provenance

- Scoring: thesoma.ai/docs/miner/scoring (Sep 2026 spec), ported in
  `soma_bench/scoring.py`.
- Benchmark design modeled on github.com/DendriteHQ/SOMA-benchmark (their
  backends: OpenClaw/Copilot in docker; ours: matched Hermes subagents under
  scratch homes, no docker required).
