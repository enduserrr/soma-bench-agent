# SOMA-BENCH: Token-Ladder Sizing A/B Report (tasks 004+005, glm-5.3-flash)

**Run:** `tok-ladder` · **Date:** 2026-09-25 · **Arms:** `soma` (live config) vs `soma-tok` (token ladder)
**User proposal tested:** (1) if est_tokens > 18K → compress to ≤18K tokens; (2) if > 32K → compress to ≤24K tokens.

## First: the unit correction

The proposal's rule 2 ("compress ≤ 24K") is NOT the current setup. Current SOMA caps at **24K chars ≈ 6K tokens** (chars/4 estimator). The ladder's 24K *tokens* = 96K chars — **4× more permissive**. T1 (18K tok trigger = 72K chars) is 3× above the live cap's trigger (24K chars).

## The structural finding (decisive, from source + live data)

1. **Hermes terminal output caps at 50K chars** (`tool_output_limits.py::DEFAULT_MAX_BYTES = 50_000`, head+tail with full text spilled to disk) BEFORE any context engine runs. Terminal results can therefore **never reach the ladder's T1 trigger (72K chars)**. Only `read_file` (≤100K chars) and MCP tool results can exceed it.
2. **Live-instance oversized-result distribution** (171 results >24K chars, live state.db): p50 = 36.7K, p90 = 70.2K, max = 105.9K chars. **92% sit in the 24–72K band** (live cap compresses them; ladder passes them through untouched), 8% exceed 72K, **0% exceed 128K** (T2 never fires on real traffic).
3. **Bench confirmation:** the tok engine wrote **zero accounting records** across all 4 tok-arm runs — it never saw a result above its trigger. Task 005's 467K megadump arrives at the engine as a 51K truncated terminal result → below T1 → untouched.
4. Live per-request accounting corroborates at the request level (2597 rewritten requests): request inputs are big (p50 264K chars) because *multiple* results ride together, but the ladder keys on **per-result** size, and per-result traffic is 92% below its trigger.

**Conclusion: on this instance, the token ladder as specified ≈ SOMA-off for per-request compression.**

## Bench numbers (with honest caveats)

| task | arm | resolved | wtok (avg) | persisted chars |
|---|---|---|---|---|
| 004 (77K runtime audit) | soma | **0/2** ⚠ | 63,946 | 4,246 |
| 004 | soma-tok | 1/2 | 47,379 | 27,892 |
| 005 (467K megadump) | soma | 2/2 | 42,941 | 54,595 |
| 005 | soma-tok | 2/2 | 36,421 | 28,308 |

Caveats that limit interpretation:
- **soma 0/2 on 004 is variance, not signal** — the same arm resolved 004 in every earlier run (smoke004, guard runs). Single-repeat swings of ±30%+ are documented.
- The tok arm's passthrough leaves the 51K terminal results resident in every later request (chars column), yet its 005 wtok came out lower — agent-path variance again (different strategies, 2 repeats only).
- Since the tok engine never engaged (0 records), **soma-tok is behaviorally identical to the no-SOMA baseline arm** for terminal-output tasks; the earlier guard/age runs' baseline rows already characterize that configuration.

## Verdict

**NEGATIVE as specified — do not adopt.** The ladder's thresholds sit above where this instance's traffic actually lives:
- 92% of oversized results (24–72K chars) would pass through uncompressed — losing the measured SOMA wins (43–71% weighted tokens on honest runs, resident-mass control) on exactly the dominant band.
- T2 (>128K chars) has **zero** real traffic; even T1 catches only 8%.
- Terminal output (the biggest source of oversized results here) is pre-capped at 50K chars by the tool layer, forever below T1.

If the goal is a lighter touch on mid-size results, the honest knob is a **chars-based** band raise for `read_file`/MCP results only (e.g. 24K→40K for that source class) — measurable with this same harness. The token-based ladder as written is dominated by the existing single cap on real traffic.

Cost: 4 agent runs ≈ $0.11. Total session spend to date ≈ $0.45 of the $3 cap.

Artifacts: `runs/tok-ladder/`, patch + generator + 9/9 offline tests in `soma_bench/patches/` (`token_ladder.patch`, `gen_token_ladder_patch.py`, `test_tok_offline.py`). Live plugin untouched (verified clean).
