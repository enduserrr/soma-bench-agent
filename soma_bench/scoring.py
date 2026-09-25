"""Dendrite SOMA SWE scoring, ported faithfully from the spec (Sep 2026).

Source: thesoma.ai/docs/miner/scoring (mcpplatform/app/api/routes/scoring.py)
Functions mirrored: compute_weighted_tokens, compute_swe_task_score,
build_swe_miner_scores, build_swe_miner_total_score.

Mapping to our bench: "baseline" = the no-SOMA arm (built-in compressor),
"miner" = the SOMA arm. x = resolved baseline runs, y = resolved SOMA runs.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

# Default weights (Dendrite spec)
WEIGHT_INPUT = 1.0
WEIGHT_CACHED = 0.1
WEIGHT_OUTPUT = 3.0

BONUS_CAP = 3.0
PENALTY_FLOOR = -4.0
PENALTY_CEILING = -2.0
RAW_CLAMP = (-4.0, 3.0)
NORMALIZED_RANGE = (-1.0, 1.0)


def compute_weighted_tokens(input_tokens, output_tokens, cached_input_tokens=None) -> Optional[float]:
    """Dendrite compute_weighted_tokens: None if required values missing or any negative."""
    if input_tokens is None or output_tokens is None:
        return None
    cached = 0 if cached_input_tokens is None else cached_input_tokens
    if cached is None:
        return None
    vals = (input_tokens, output_tokens, cached)
    if any(v is None or v < 0 for v in vals):
        return None
    return (WEIGHT_INPUT * input_tokens
            + WEIGHT_CACHED * cached
            + WEIGHT_OUTPUT * output_tokens)


def compute_swe_task_score(x: int, y: int, n: int,
                           baseline_tokens: Optional[float],
                           miner_tokens: Optional[float]) -> Dict:
    """Per-task score. Returns {'score': float|None, 'pool': 'main'|'hard_boost'|'excluded',
    'detail': {...}} — faithfully implements the Dendrite zones."""
    # Compression ratio
    if baseline_tokens is None or miner_tokens is None or baseline_tokens <= 0 or miner_tokens <= 0:
        r = 0.0
        r_valid = False
    else:
        r = max(-2.0, min(math.log2(baseline_tokens / miner_tokens), 2.0))
        r_valid = True

    if y == 0:
        return {"score": None, "pool": "excluded", "detail": {
            "x": x, "y": y, "n": n, "r": r, "r_valid": r_valid, "zone": "excluded"}}

    t = math.floor(0.8 * x)

    if x <= 1:  # hard tasks
        if x == 1 and y == 1:
            s = r
            zone = "maintain"
        else:
            s = max(-2.0, min(r + (y - x) / (n - x), 3.0))
            zone = "bonus" if y > x else "maintain_bonus_clamped"
        return {"score": s, "pool": "hard_boost", "detail": {
            "x": x, "y": y, "n": n, "r": r, "r_valid": r_valid, "zone": zone}}

    # standard tasks (x >= 2)
    if y < t:
        s = max(-4.0, min(-2.0 - 2.0 * (1.0 - y / t), -2.0))
        zone = "penalty"
    elif t <= y <= x:
        s = r
        zone = "maintain"
    else:  # y > x
        s = max(-2.0, min(r + (y - x) / (n - x), 3.0))
        zone = "bonus"
    return {"score": s, "pool": "main", "detail": {
        "x": x, "y": y, "n": n, "r": r, "r_valid": r_valid, "zone": zone}}


def build_swe_miner_scores(task_rows: List[Dict]) -> Dict:
    """Aggregate per-task rows (each with x, y, n, T_B, T_A) into miner scores.

    Each row needs keys: x, y, n, baseline_weighted_tokens, miner_weighted_tokens.
    """
    main_rows = [row for row in task_rows if row.get("pool") == "main"]
    hard_rows = [row for row in task_rows if row.get("pool") == "hard_boost"]

    # Main score: x^(1/3)-weighted average of main-task scores
    if main_rows:
        num = sum(row["score"] * (row["x"] ** (1 / 3)) for row in main_rows)
        den = sum(row["x"] ** (1 / 3) for row in main_rows)
        main_score = num / den
    else:
        main_score = 0.0

    # Hard boost: sum of positive hard-task contributions / (N_M + N_H)
    if hard_rows:
        h_sum = sum(max(0.0, row["score"]) for row in hard_rows)
        hard_boost = h_sum / (len(main_rows) + len(hard_rows))
    else:
        hard_boost = 0.0

    raw_total = main_score + hard_boost
    return {
        "main_score": main_score,
        "hard_boost": hard_boost,
        "raw_total": raw_total,
        "n_main": len(main_rows),
        "n_hard": len(hard_rows),
    }


def build_swe_miner_total_score(raw_total: float) -> float:
    """Clamp to [-4, 3] then linearly normalize to [-1, 1]."""
    clamped = max(RAW_CLAMP[0], min(raw_total, RAW_CLAMP[1]))
    return 2 * ((clamped + 4) / 7) - 1
