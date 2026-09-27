# SOMA BENCH

An agent-level A/B benchmark that measures what a context compressor **actually does to an autonomous agent** — token cost, cache behavior, resident context size, and task success — by running the same task through two (or more) matched Hermes agent "arms" that differ *only* in their context engine. Built to evaluate [DendriteHQ's SOMA compressor](https://github.com/DendriteHQ/SOMA-OpenClaw-compressor) on a real Hermes Agent instance, with [DendriteHQ/SOMA-benchmark](https://github.com/DendriteHQ/SOMA-benchmark)'s SWE scoring spec ported for comparable metrics.

### WHAT'S NEW vs the original SOMA-benchmark

| | Dendrite `SOMA-benchmark` | this `SOMA-BENCH` |
|---|---|---|
| Purpose | subnet-miner evaluation at scale | single-instance, decision-grade A/B of compressor variants |
| Test subjects | miner compressor services over an API | context-engine plugins loaded into matched Hermes subagents |
| Arms | miner vs reference | any engine variant: live SOMA copy, no-SOMA baseline, patched variants (guard / age-tier / token-ladder / flat-16K) |
| Scoring | SWE formulas: weighted tokens (in×1.0 + cache×0.1 + out×3.0), r-based resolution score | **identical formulas, ported and unit-verified** (23/23 checks vs the published spec) |
| Tasks | their SWE-bench-derived set | self-contained pocket tasks (SWE-bench-style: broken repo → fix → tests must pass) with oversized tool results in the regimes compressors actually engage (>24K chars) |
| Engine isolation | n/a (remote service) | byte-verified copy of the live plugin; the live engine is never loaded or written by the bench |
| Evidence discipline | — | per-run contamination checks (grep-dodge, /tmp dumping, cross-run file leakage), excluded-run documentation, invalid-run archiving |

**Why standalone rather than a fork:** no shared code — this bench ports the original's *scoring spec* (formulas, verified against the published docs) but every line of the harness is new. The original benchmarks *miners*; this benchmarks *variants of your own engine config* before you change anything live. Several proposed "improvements" were killed by this bench before ever reaching production (see `results/RESULTS.md`).

### Results at a glance

Full data: [`results/RESULTS.md`](results/RESULTS.md) (per-run CSVs alongside), analysis: `REPORT-*.md`.

- **SOMA vs no-SOMA:** −31% … −71% weighted tokens on oversized-result tasks, resolution parity or better.
- **Flat 16K-char birth cap vs live 24K:** −24.3% weighted tokens / −93% resident chars on medium sessions — the only variant that beat the live config.
- **Rejected with evidence:** marginal-rewrite guard (+90.8%), age-tiered re-compression (+210%), token-unit ladder (structurally inert — terminal results are pre-capped at 50K chars by the agent harness).

### Important links
- [INSTALL.md](INSTALL.md) — install, configure, and run (humans and agents)
- [ARCHITECTURE.md](ARCHITECTURE.md) — arms, isolation model, scoring, task format
- [results/RESULTS.md](results/RESULTS.md) — every run, every verdict
- [DendriteHQ/SOMA-OpenClaw-compressor](https://github.com/DendriteHQ/SOMA-OpenClaw-compressor) — the compressor under test
- [DendriteHQ/SOMA-benchmark](https://github.com/DendriteHQ/SOMA-benchmark) — the scoring spec this bench ports
- [thesoma.ai/docs/miner/scoring](https://thesoma.ai/docs/miner/scoring) — published scoring formulas

### Safety model (read before running)

- The live SOMA plugin (`~/.hermes/plugins/context_engine/soma/`) is **never modified, never loaded**. Arms run from a byte-verified copy under `engines/`, loaded via an additional install-tree symlink (the live `soma` name is untouched).
- Each arm runs in its own scratch `HERMES_HOME` (own `state.db`, own config, empty skills); task agents run in throwaway `/tmp` workdirs, outside this repo — agents cannot reach other runs' files.
- Secrets are never copied: arm configs reference the same env-var *name* the live config uses; `.env` is a read-only symlink.
- `tasks/` (answer keys) and raw run records are git-ignored — they stay local, never published.

### License

MIT.
