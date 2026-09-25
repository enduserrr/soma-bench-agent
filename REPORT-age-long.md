# SOMA-BENCH: Age-Tiered Compression A/B/B Report (task 006, glm-5.3-flash)

**Run:** `age-long2` (valid) · **Previous invalid attempt:** `age-long-invalid-no-ratchets` (kept for the record)
**Date:** 2026-09-25 · **Model:** glm-5.3-flash via say-gm, reasoning=high, max_turns=125
**Cost guard:** user cap $3.00 · measured spend ≈ $0.35–0.60 (6 agent runs; input $0.134/Mtok, output $0.445/Mtok, cache-read $0.027/Mtok — verified live from the provider's model list)

## The hypothesis under test (user's words)

In long conversations, compression pressure should **increase with age** — a reverse-Pareto shape: the oldest ~20% of context contributes very little to outcomes and should take most of the compression, while the most recent context stays untouched. Rationale: 150K tokens from the head has little marginal value compared to the first 60–80K.

## Design

### What was built
- **Arm `soma-age`** (4th bench arm): live SOMA copy + `age_tier.patch` (regenerable via `soma_bench/patches/gen_age_patch.py`, applies to a fresh live copy — never edits the live plugin).
  - Age = **distance from the request tail** (messages after the result). Pure, deterministic, monotone: results only age INTO tighter bands.
  - **Bands:** tail < 20 → byte-identical to today (24K cap); tail ≥ 20 → 12K budget; tail ≥ 40 → 6K budget.
  - **At-cap-time semantics** (the critical fix): Hermes persists raw history, so every request re-derives from raw messages; an old oversized result is capped **directly to its band budget** instead of being birth-capped to 24K first. A result's form is a pure function of (raw content, band) — it changes exactly once per band crossing → one cache break per crossing, no churn.
  - Hysteresis 1.10 (only squeeze when > budget×1.10) so boundary results cannot oscillate.
  - `[[CMP]]` marker preserved through re-squeeze; envelope metadata (exit_code etc.) preserved.
- **Task 006 `soma-bench-006`** (long-session): 8 sequential ops phases, each printing a ~50K one-shot report (ungreppable in advance, re-print refused), the agent must extract an incident code + two numbers per phase, acknowledge them, and at the end pass a **cross-phase recall audit** (`--close-shift`) that checks all 8 phases' values — including the OLDEST phases whose reports were squeezed hardest (6K) by then. 505K total report chars; ~25–40 agent turns.
- Offline validation: 8/8 checks green (bands fire at exact distances, needles survive 6K squeezes, fixed points, hysteresis, envelope path).

### Arms compared
| arm | engine | meaning |
|---|---|---|
| `soma` | byte-identical live copy | current behaviour |
| `baseline` | built-in compressor only | no-SOMA control |
| `soma-age` | live copy + age tiers | the hypothesis |

2 repeats each, matched on everything except the engine (model, provider, reasoning, toolsets, max_turns, compression threshold 0.85 — raised from 0.50 **equally for all arms** so the built-in compaction cliff stays out of the age signal).

## Honest accounting of the journey

1. **First run invalid.** The original patch re-squeezed *already-compressed* results; but Hermes never persists compressed forms — every request re-derives from RAW history — so old reports were re-birth-capped to 24K and the 12K/6K ratchets never fired (0 `age_resqueeze` records proved it). The headline −37% weighted tokens in that run was **agent-path variance** (the agent took a leaner route), not an age effect. Kept as `runs/age-long-invalid-no-ratchets`, never counted as evidence.
2. Root cause fixed (at-cap-time application), plus two latent bugs found by the offline tests: dropped `[[CMP]]` marker in bare-form re-squeeze, and an accounting label that would have hidden age events again.

## Results (age-long2)

### Per-run raw data (session-attributed)

| arm | run | session | wtok | in_tok | cache_read | out_tok | resolved | reports-in-context |
|---|---|---|---|---|---|---|---|---|
| soma | r1 | 8cc34c | 98,276 | 26,578 | 592,448 | 4,151 | ✓ | **1 — CONTAMINATED** (dumped reports to /tmp/phase*.txt, grepped them) |
| soma | r2 | 786b10 | 284,195 | 192,292 | 882,816 | 1,207 | ✓ | 8 (honest) |
| baseline | r1 | 8f8f22 | 13,912 | 7,211 | 47,808 | 640 | ✗ | 0 (shortcut path, failed probes) |
| baseline | r2 | 8e72aa | 516,756 | 351,983 | 1,607,680 | 1,335 | ✓ | 8 (honest) |
| soma-age | r1 | b0c5bf | 642,838 | 597,656 | 405,376 | 1,548 | ✓ | 8 (honest) |
| soma-age | r2 | fb192d | 542,783 | 482,975 | 549,632 | 1,615 | ✓ | 8 (honest) |

### Honest comparison (reports actually in context, all resolved)

| comparison | weighted tokens | verdict |
|---|---|---|
| soma (r2) vs baseline (r2) | 284K vs 517K (−45%) | current SOMA wins on long sessions too |
| **soma-age vs soma** | 592K avg vs 284K (**+91…+126%**) | **age-tiering loses ~2×** |
| soma-age vs baseline | 592K vs 517K (+15%) | age-tiering is worse than no SOMA at all |

Engine accounting (age arm): 35 rewritten requests — 20 `near_passthrough` (young results, byte-identical behavior) + **15 `age_resqueeze`** (ratchets genuinely fired; invalid-run-1's bug confirmed fixed). Dendrite scoring: soma +1.47 (bonus zone), soma-age **−0.16**.

## Reading the numbers

**The hypothesis's fidelity premise was CONFIRMED; its economics premise was REFUTED.**

1. **No intelligence loss.** Both age-arm runs passed the cross-phase recall audit — all 8 phases' incident codes + threshold/drift values recalled correctly at the end, even though the oldest reports had been squeezed to 6K chars (from 51K raw / 24K birth-cap). The pinned-line extractive squeeze kept every needle. "Really old context has little to contribute" — for *noise-heavy machine reports*, true: 88% of those chars was never needed again.
2. **Cache economics dominate.** The age arm's cache reads collapsed (405K/550K vs soma's 883K in the matched r2 pair). Mechanism: a result's compressed form is a pure function of (raw content, band); when it crosses a band boundary (tail 19→20, 39→40) its form changes, and everything AFTER that point in the prefix is re-billed as fresh input. With ~8 oversized results aging through 2 boundaries each in a ~50-message session, the prefix broke ~15 times. Each break converts hundreds of K of cache-priced tokens (0.1× weight) to fresh-priced (1.0×). The 18K-char-per-result savings never pay for that.
3. **SOMA's current design is precisely cache-optimal.** Its "compress once at birth, never touch again" rule makes the emitted prefix byte-identical turn-to-turn — maximum cache hit rate. That's not an accident; it's the core architectural bet, and this experiment is the strongest evidence yet that it's the right one *on providers with prompt caching* (say-gm/glm-5.3-flash: cache reads at ~20% of input price).

## Cost

6 agent runs ≈ **$0.34 total** (input $0.1335/Mtok, cache-read $0.0267/Mtok, output $0.445/Mtok — provider-verified), well under the $3 cap. Two runs (soma r1, baseline r1) are excluded from evidence due to strategy contamination; 4 honest runs carry the verdict.

## Verdict

**NEGATIVE — do not apply age-tiered compression to the live engine.** The reverse-Pareto shape is implementable, fidelity-safe (needles survive, recall audit passes), and observably engaged — and it still loses by ~2× on weighted tokens because band crossings break the prompt cache, and glm-5.3-flash's cache pricing makes prefix stability worth more than char savings. Same class of result as the marginal-guard experiment: the winning move on this provider is *fewer, earlier, stable rewrites*, not more aggressive ones.

**Provider-dependence caveat (honest scope):** on a provider WITHOUT prompt caching (or with free/unstable cache), the calculus inverts — age-tiering's char savings would be pure profit and the cache-break penalty nonexistent. The verdict here is for say-gm/glm-5.3-flash, the environment this instance actually runs. Revisit only if the provider changes.

Artifacts: `runs/age-long2/` (valid), `runs/age-long-invalid-no-ratchets/` (kept for the record), patch + generator in `soma_bench/patches/` (`age_tier.patch`, `gen_age_patch.py`, `test_age_offline.py` — 8/8 green). Live plugin untouched throughout (verified: clean git, accounting untouched).

