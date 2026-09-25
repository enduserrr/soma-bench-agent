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
CMP_PREFIX = "[[CMP]]\\n"      # bare compressed form prefix (matched literally)
CMP_SUFFIX = "\\n[[/CMP]]"     # bare compressed form suffix (matched literally)


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
            capped, changed, age_tightened = _cap_openai_tool_result_with_age(
                soma, message, tail_distance)
            out.append(capped)
            results_capped += 1 if changed else 0
            age_resqueezes += 1 if age_tightened else 0
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

    Reality of the Hermes request flow: persisted history is RAW (compression
    never mutates it), so every request re-derives from raw messages — an old
    oversized result is birth-capped fresh each request. The age tier is
    therefore applied AT CAP TIME: a result old enough for a tighter band is
    capped directly to that band's budget instead of the 24K birth cap.
    Young results (band 0) behave byte-identically to today.

    Cache economics: a result's form is a pure function of (raw content,
    band). When it crosses a band boundary (tail 19->20, 39->40) its form
    changes once — one cache break per crossing, never continuous churn.
    The explicit re-squeeze path (already-[[CMP]] input) is kept for
    robustness but is not exercised by the standard Hermes flow.
    """
    capped, changed = _cap_openai_tool_result(soma, message)
    budget = _age_budget(tail_distance)
    if budget is None:
        return capped, changed, False  # band 0: today's behaviour exactly
    if not changed:
        # not birth-capped now; only an already-compressed old result could
        # still be too big for its band (non-standard flow) — re-squeeze
        content = capped.get("content") if isinstance(capped, dict) else None
        if not isinstance(content, str) or "[[CMP]]" not in content:
            return capped, changed, False
        retext = _age_resqueeze_text(soma, content, budget)
        if retext is None or len(retext) >= len(content):
            return capped, changed, False
        out = dict(capped)
        out["content"] = retext
        return out, True, True
    # birth cap happened; tighten it to the age budget when the result is old
    content = capped.get("content")
    if not isinstance(content, str) or "[[CMP]]" not in content:
        return capped, changed, False
    retext = _age_resqueeze_text(soma, content, budget)
    if retext is None or len(retext) >= len(content):
        return capped, changed, False  # could not tighten further; keep birth cap
    out = dict(capped)
    out["content"] = retext
    return out, True, True


def _age_resqueeze_text(soma: Any, content: str, budget: int):
    """Squeeze a [[CMP]] tool result (bare or JSON-envelope) to `budget` chars.

    Returns None when nothing should change (within budget / hysteresis /
    no shrink). Envelope metadata is preserved on re-wrap.
    """
    unwrapped = _unwrap_json_envelope(content)
    if unwrapped is None:
        if len(content) <= budget * AGE_HYSTERESIS:
            return None
        # bare [[CMP]] content: strip the marker, squeeze the inner text,
        # re-wrap — the compressed form must keep its [[CMP]] marker (the
        # marker line itself is not "pinned", so squeezing the raw content
        # would drop it).
        inner = content
        had_marker = content.startswith(CMP_PREFIX)
        if had_marker:
            inner = content[len(CMP_PREFIX):]
            if inner.endswith(CMP_SUFFIX):
                inner = inner[: -len(CMP_SUFFIX)]
        new_inner, _changed = soma.extractive_compress(inner, budget, frozenset())
        if not _changed or len(new_inner) >= len(content):
            return None
        return soma.cmp_block(new_inner) if had_marker else new_inner
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
