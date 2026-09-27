# SOMA-BENCH result sheets

Every A/B experiment run on this harness (single Hermes instance, matched arms;
scoring per the Dendrite SOMA-benchmark spec: weighted tokens = input×1.0 +
cache_read×0.1 + output×3.0). Per-run data in `<tag>.csv`; methodology and full
analysis in `REPORT-*.md` at the repo root. `excluded=yes` rows are documented
invalid runs (harness contamination or patch bug) — kept visible, never counted.

| run | experiment | verdict |
|---|---|---|
| `age-long-invalid-no-ratchets` | Age-tier attempt 1 (invalid) | INVALID — ratchets never fired (history is raw; patch applied on wrong layer); kept for the record |
| `age-long2` | Age-tier reverse-Pareto (006) | NEGATIVE — recall audit PASSED (fidelity confirmed) but +210% weighted tokens; band crossings break cache prefix |
| `cap16k-flat` | Flat 16K birth cap (004+006) | POSITIVE — 006: −24.3% weighted, −93% resident chars, 2/2 resolved; 004: wash. First variant to beat the live 24K config |
| `cap16k-invalid-partial-ladder` | Flat-16K attempt 1 (invalid) | INVALID — patch left a partial ladder (MAX_KEEP unclamped); archived |
| `guard-ab` | Marginal-guard A/B/B (003+004) | INCONCLUSIVE on normal shapes — guard never fired (all rewrites save 30–64%) |
| `guard-megadump` | Marginal-guard decisive test (005) | NEGATIVE — guard +90.8% weighted tokens vs soma; skipped result stays resident, cache break happens anyway |
| `smoke001` | Parity smoke (small result) | PARITY — both arms resolved; SOMA never engaged (results under floor) |
| `smoke002` | Static needle hunt | PARITY — both resolved after trajectory-wide needle fix |
| `smoke003` | Build-notes needle hunt | SOMA engaged (46 events) but +43% weighted tokens (cache invalidation); baseline failed probe |
| `smoke004` | Runtime audit (forced oversized) | SOMA WIN — −31% weighted tokens, −94% resident chars, needle preserved |
| `tok-ladder` | Token-unit ladder (004+005) | NEGATIVE (structural) — terminal pre-capped at 50K chars; ladder trigger unreachable; 0 compression events |

## Headline numbers (honest runs only)

**SOMA vs no-SOMA baseline** (tasks 003–005, runs `smoke004`, `guard-*`):
−31% to −71% weighted tokens on oversized-result tasks, resolution parity or better.

**Flat 16K-char birth cap vs live 24K** (`cap16k-flat`, glm-5.3-flash):
- medium session (8×50K reports, ~30–45 turns): **−24.3% weighted tokens, −93% resident chars, 2/2 resolved**
- short session (~5 turns): wash (−7.4% weighted, +9.4% raw, within ±20% single-run variance)

**Rejected variants** (each negative for a distinct, evidenced reason):
- marginal-rewrite guard (skip <2% rewrites): +90.8% on the real marginal shape
- age-tiered re-squeeze (reverse-Pareto): +210% (cache-prefix breaks per band crossing)
- token-unit ladder (18K/32K): structurally inert (terminal results pre-capped at 50K chars)
