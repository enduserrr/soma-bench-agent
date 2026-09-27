# Installing and running SOMA-BENCH

Written for both humans and autonomous agents. Everything below assumes a
Linux host with a working [Hermes Agent](https://hermes-agent.nousresearch.com/docs)
instance whose context engine under test lives at
`~/.hermes/plugins/context_engine/<name>/`.

## Requirements

- Python 3.10+ (stdlib only for the harness; `patch` and `sqlite3` binaries present)
- A `hermes` CLI on PATH, logged in and able to run one-shot chats
  (`hermes chat --query-file … --format stream-json`)
- The context-engine plugin you want to A/B (e.g. the SOMA plugin) installed
  under `~/.hermes/plugins/context_engine/`
- A provider API key your Hermes instance already uses (the bench never
  handles secrets itself)

## Install

```bash
git clone <this-repo> ~/git/SOMA-BENCH
cd ~/git/SOMA-BENCH
python3 -m soma_bench.orchestrate setup      # builds engine copies, symlinks, arm homes
python3 -m soma_bench.orchestrate verify     # read-only integrity check; must print ok:true
```

`setup` is idempotent; re-run any time. It will:

1. copy the live plugin byte-for-byte into `engines/<name>` (and apply variant
   patches for the patched arms, failing loudly if a patch no longer applies),
2. create install-tree symlinks under the Hermes plugins dir using NEW names
   (`somabench`, `somaguard`, …) — never touching existing entries,
3. generate scratch `HERMES_HOME`s under `profiles/<arm>/` with configs lifted
   from your live config (matched model/provider/reasoning/toolsets; only
   `context.engine` differs).

### Task corpus

`tasks/` is **git-ignored** (answer keys). If it's absent, the task specs must
be regenerated locally or authored fresh — see ARCHITECTURE.md for the format.
The harness runs whatever `tasks/*.json` exist; you can also point it at your
own tasks.

## Configure

Nothing to configure for a stock A/B: arms mirror the live config
automatically. Optional knobs, all CLI flags on `orchestrate run`:

| flag | meaning |
|---|---|
| `-t <task>` | task name (repeatable; default all) |
| `--arms` | comma list: `soma,baseline,soma-guard,soma-age,soma-tok,soma-16k` |
| `--repeats N` | repeats per task×arm (2–4 recommended; single-run variance ±20–30%) |
| `--tag T` | run label; results land in `runs/T/` |
| `--timeout S` | per-run seconds (default 1800) |
| `--max-turns N` | cap agent turns |

**Cost note:** every run spends real API tokens on every arm. Estimate =
(per-arm session tokens) × arms × repeats × task count. On glm-5.3-flash the
runs behind `results/` cost ≈ $0.1–0.4 per experiment.

## Run

```bash
python3 -m soma_bench.orchestrate run -t soma-bench-004 --arms soma,baseline --repeats 2 --tag my-first-run
python3 -m soma_bench.orchestrate score my-first-run
python3 -m soma_bench.orchestrate report my-first-run
```

Long runs (long-session tasks, many arms): launch in the background and let it
finish; per-run records are appended as they complete.

```bash
nohup python3 -m soma_bench.orchestrate run --tag bigrun > runs/bigrun.log 2>&1 &
```

### For agents

- Always `verify` before and after a run (`ok: true` required).
- Workdirs are created in `/tmp` — never inside the repo.
- After scoring, check for contamination before believing results: did the
  oversized tool results actually enter the request history (query the arm
  `state.db` `messages` table, read-only)? Did any agent write files outside
  its workdir? Exclude + document suspect runs rather than deleting them.
- `orchestrate reverify <tag>` re-runs probes over stored records with zero
  API cost.
- Publish artifacts with `python3 tools/build_result_sheets.py` (sanitized
  CSVs + RESULTS.md under `results/`).

## Verify & troubleshoot

| symptom | fix |
|---|---|
| `verify` fails on engine copy | live plugin changed; `setup --force` rebuilds (patched arms fail loudly if a patch drifted — regenerate the patch, don't hand-edit engines) |
| arm runs die instantly | check `profiles/<arm>/config.yaml` exists and `.env` symlink resolves; provider key must be in the environment the runner spawns |
| every run unresolved | run one task manually with `hermes chat` under the arm HOME to see the raw error |
| suspiciously cheap runs | verify oversized results entered context (see "For agents") — agents sometimes dodge reads |

## Safety guarantees (enforced, not just promised)

- The live plugin dir is only ever READ (copy source). Byte-verification of
  copies happens at setup and verify.
- Install-tree symlinks use fresh names; the live engine's own symlink is
  never modified.
- Arm `state.db`s are scratch; the live `~/.hermes/state.db` is never written.
- Secrets: arm configs carry env-var NAMES only; `.env` is a read-only symlink.
