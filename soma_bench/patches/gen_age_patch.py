#!/usr/bin/env python3
"""Regenerate soma_bench/patches/age_tier.patch deterministically.

Edits a scratch copy of the LIVE engine (read-only reference) and emits the
unified diff vs the pristine live engine.py. Run from anywhere:

    python3 soma_bench/patches/gen_age_patch.py

The resulting patch is applied by arm_setup.ensure_age_engine() the same way
the guard patch is (fresh live copy + `patch -p0`). Never hand-edit the
derived engine — recreate = rerun this script.
"""
from __future__ import annotations

import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LIVE = Path.home() / ".hermes/plugins/context_engine/soma/engine.py"
OUT_PATCH = Path(__file__).resolve().parent / "age_tier.patch"

# ---- edit 1: constants + helpers after LARGE_CAP_CHARS block --------------
CONST_ANCHOR = "LARGE_UPPER_CHARS = 53_300\nLARGE_CAP_CHARS = 24_000\n"
CONST_NEW = CONST_ANCHOR + '''
# ---------------------------------------------------------------------------
# Age-tiered re-squeeze (bench candidate, soma-age arm — NOT live).
# Hypothesis under test (user, Sep 2026): in long conversations the oldest
# context has little marginal value, so compression pressure should rise with
# age (reverse-Pareto shape). Implemented as ONE stable ratchet per band, not
# continuous squeezing, because every rewrite breaks the provider prompt-cache
# at that point in the prefix: band 0 = today's behaviour (byte-identical),
# band 1 re-squeezes to AGE_TIER1_KEEP, band 2 to AGE_TIER2_KEEP. Distance is
# measured from the request TAIL (messages after this one) — pure,
# deterministic, monotone: results only age INTO tighter bands.
#
# Cache-stability: the tier form is a fixed point. A [[CMP]] block at band-N
# size is only re-squeezed when its own size exceeds the NEXT band's budget,
# and the hysteresis margin (compress only when len > budget * 1.10) means a
# boundary result cannot oscillate between two forms.
# ---------------------------------------------------------------------------
AGE_TAIL_MESSAGES_TIER1 = 20   # >= 20 messages after the result -> tier 1
AGE_TAIL_MESSAGES_TIER2 = 40   # >= 40 messages after the result -> tier 2
AGE_TIER1_KEEP = 12_000
AGE_TIER2_KEEP = 6_000
AGE_HYSTERESIS = 1.10          # compress only if len > budget * 1.10
AGE_RECORD_REASON = "age_resqueeze"


def _age_budget(messages_from_tail: int):
    """Budget for an already-[[CMP]] result given its age (tail distance).

    None = young enough for today's behaviour to hold (band 0).
    """
    if messages_from_tail >= AGE_TAIL_MESSAGES_TIER2:
        return AGE_TIER2_KEEP
    if messages_from_tail >= AGE_TAIL_MESSAGES_TIER1:
        return AGE_TIER1_KEEP
    return None
'''

# ---- edit 2: _select_context_impl — enumerate + age path + reason ---------
IMPL_OLD = '''        soma = _load_soma()
        out_changed = False
        results_capped = 0
        out: List[Dict[str, Any]] = []
        for message in request_messages:
            capped, changed = _cap_openai_tool_result(soma, message)
            out.append(capped)
            results_capped += 1 if changed else 0
            out_changed = out_changed or changed
        if not out_changed:
            return None'''
IMPL_NEW = '''        soma = _load_soma()
        out_changed = False
        results_capped = 0
        age_resqueezes = 0
        out: List[Dict[str, Any]] = []
        n = len(request_messages)
        for i, message in enumerate(request_messages):
            tail_distance = n - 1 - i
            capped, changed = _cap_openai_tool_result_with_age(
                soma, message, tail_distance)
            out.append(capped)
            results_capped += 1 if changed else 0
            was_old_cmp = isinstance(message, dict) and (
                "cmp" in str(message.get("role") or "") or
                "[[CMP]]" in str(message.get("content") or ""))
            age_resqueezes += 1 if (changed and was_old_cmp) else 0
            out_changed = out_changed or changed
        if not out_changed:
            return None'''

# ---- edit 3: accounting reason records age re-squeezes -------------------
REASON_OLD = '''            results_capped,
            "near_passthrough",
            self._session_id,'''
REASON_NEW = '''            results_capped,
            AGE_RECORD_REASON if age_resqueezes else "near_passthrough",
            self._session_id,'''

# ---- edit 4: age-aware wrapper + resqueeze helpers after the cap function -
HELPER_ANCHOR = '''    out = dict(capped)
    out["role"] = "tool"
    return out, True
'''
HELPER_NEW = HELPER_ANCHOR + '''

def _cap_openai_tool_result_with_age(soma: Any, message: Dict[str, Any],
                                     tail_distance: int) -> tuple:
    """Age-aware wrapper around _cap_openai_tool_result.

    Band 0 (young or first-time cap): delegate untouched — behaviour
    identical to today. Band 1/2 (old): if the message is an
    already-[[CMP]] result whose compressed size still exceeds the tier
    budget, re-squeeze it to that budget. Idempotent per band (fixed
    point) with 10% hysteresis. Never-compressed messages are untouched by
    the age path — first-time caps still happen at birth (any age).
    """
    capped, changed = _cap_openai_tool_result(soma, message)
    if changed or not isinstance(capped, dict):
        return capped, changed  # first-time cap (or non-result): today's path
    content = capped.get("content")
    if not isinstance(content, str) or "[[CMP]]" not in content:
        return capped, changed
    budget = _age_budget(tail_distance)
    if budget is None:
        return capped, changed
    retext = _age_resqueeze_text(soma, content, budget)
    if retext is None or len(retext) >= len(content):
        return capped, changed  # no shrink (or JSON-escape overhead ate it)
    out = dict(capped)
    out["content"] = retext
    return out, True


def _age_resqueeze_text(soma: Any, content: str, budget: int):
    """Re-squeeze an already-[[CMP]] tool result to `budget` chars.

    Returns None when nothing should change (within budget / hysteresis /
    no shrink). Works on bare [[CMP]] content and Hermes JSON-envelope
    content; envelope metadata is preserved on re-wrap.
    """
    unwrapped = _unwrap_json_envelope(content)
    if unwrapped is None:
        if len(content) <= budget * AGE_HYSTERESIS:
            return None
        new_text, _changed = soma.extractive_compress(content, budget, frozenset())
        if not _changed or len(new_text) >= len(content):
            return None
        return new_text
    inner_text, envelope_obj, text_key = unwrapped
    if len(inner_text) <= budget * AGE_HYSTERESIS:
        return None
    new_inner, _changed = soma.extractive_compress(inner_text, budget, frozenset())
    if not _changed or len(new_inner) >= len(inner_text):
        return None
    envelope_obj[text_key] = soma.cmp_block(new_inner)
    return json.dumps(envelope_obj, ensure_ascii=False)
'''


def main() -> int:
    live_text = LIVE.read_text(encoding="utf-8")
    edited = live_text
    for label, old, new in (
            ("constants", CONST_ANCHOR, CONST_NEW),
            ("impl", IMPL_OLD, IMPL_NEW),
            ("reason", REASON_OLD, REASON_NEW),
            ("helpers", HELPER_ANCHOR, HELPER_NEW)):
        if edited.count(old) != 1:
            sys.exit(f"anchor {label!r} matched {edited.count(old)} times (need 1) — live engine drifted")
        edited = edited.replace(old, new)
    diff = difflib.unified_diff(
        live_text.splitlines(keepends=True),
        edited.splitlines(keepends=True),
        fromfile="engine.py", tofile="engine.py",
    )
    OUT_PATCH.write_text("".join(diff), encoding="utf-8")
    print(f"wrote {OUT_PATCH} ({OUT_PATCH.stat().st_size} bytes)")
    # verify it applies cleanly to a fresh copy
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "soma"
        shutil.copytree(LIVE.parent, stage,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "bench_results", "accounting.jsonl"))
        proc = subprocess.run(["patch", "-p0", "--batch", "-i", str(OUT_PATCH)],
                              cwd=str(stage), capture_output=True, text=True)
        if proc.returncode != 0:
            sys.exit(f"patch does not apply cleanly:\n{proc.stdout}\n{proc.stderr}")
        # and the patched file compiles
        proc = subprocess.run([sys.executable, "-m", "py_compile", str(stage / "engine.py")],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            sys.exit(f"patched engine.py does not compile:\n{proc.stderr}")
    print("patch applies cleanly and compiles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
