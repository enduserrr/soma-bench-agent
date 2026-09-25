#!/usr/bin/env python3
"""Regenerate soma_bench/patches/token_ladder.patch deterministically.

User proposal (Sep 2026), token-based ladder (contrast to the live 24K-CHAR cap):
  est_tokens > 32K -> compress to <= 24K tokens
  est_tokens > 18K -> compress to <= 18K tokens
  else             -> passthrough untouched

Token estimates use the same estimator as SOMA accounting (tiktoken when
installed; on this host the deterministic chars/4 fallback is active), so the
thresholds are expressed in chars: 18K tok = 72,000 chars, 32K tok = 128,000
chars, 24K tok = 96,000 chars. Run:

    python3 soma_bench/patches/gen_token_ladder_patch.py

The patch is applied by arm_setup.ensure_tok_engine() (fresh live copy +
`patch -p0`). Never hand-edit the derived engine.
"""
from __future__ import annotations

import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LIVE = Path.home() / ".hermes/plugins/context_engine/soma/engine.py"
OUT_PATCH = Path(__file__).resolve().parent / "token_ladder.patch"

CONST_ANCHOR = "LARGE_UPPER_CHARS = 53_300\nLARGE_CAP_CHARS = 24_000\n"
CONST_NEW = CONST_ANCHOR + '''
# ---------------------------------------------------------------------------
# Token-ladder sizing (bench candidate, soma-tok arm — NOT live).
# User proposal (Sep 2026), token-based ladder:
#   est_tokens > 32K  -> compress to <= 24K tokens
#   est_tokens > 18K  -> compress to <= 18K tokens
#   else              -> passthrough untouched
# Estimates use the same estimator as SOMA accounting (tiktoken when
# installed; on this host the deterministic chars/4 fallback is active), so
# thresholds are expressed in chars: 18K tok = 72,000 chars, 32K tok =
# 128,000 chars, 24K tok = 96,000 chars. This is ~3-4x more permissive than
# the live 24K-CHAR cap (~6K tokens). Compress-once-at-birth semantics are
# preserved: the form is a pure function of the raw message, so the emitted
# prefix stays byte-stable turn-to-turn (cache-safe).
# ---------------------------------------------------------------------------
LADDER_T1_TRIGGER_TOKENS = 18_000
LADDER_T2_TRIGGER_TOKENS = 32_000
LADDER_T1_KEEP_TOKENS = 18_000
LADDER_T2_KEEP_TOKENS = 24_000
LADDER_CHARS_PER_TOKEN = 4   # matches the core's CHARS_PER_TOKEN fallback
LADDER_T1_TRIGGER_CHARS = LADDER_T1_TRIGGER_TOKENS * LADDER_CHARS_PER_TOKEN  # 72_000
LADDER_T2_TRIGGER_CHARS = LADDER_T2_TRIGGER_TOKENS * LADDER_CHARS_PER_TOKEN  # 128_000
LADDER_T1_KEEP_CHARS = LADDER_T1_KEEP_TOKENS * LADDER_CHARS_PER_TOKEN        # 72_000
LADDER_T2_KEEP_CHARS = LADDER_T2_KEEP_TOKENS * LADDER_CHARS_PER_TOKEN        # 96_000
LADDER_RECORD_REASON = "token_ladder"


def _ladder_budget(text_chars: int):
    """Keep-budget in chars for a raw tool result, or None = passthrough."""
    if text_chars > LADDER_T2_TRIGGER_CHARS:
        return LADDER_T2_KEEP_CHARS
    if text_chars > LADDER_T1_TRIGGER_CHARS:
        return LADDER_T1_KEEP_CHARS
    return None
'''

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
        ladder_engaged = 0
        out: List[Dict[str, Any]] = []
        for message in request_messages:
            capped, changed, engaged = _cap_openai_tool_result_ladder(soma, message)
            out.append(capped)
            results_capped += 1 if changed else 0
            ladder_engaged += 1 if engaged else 0
            out_changed = out_changed or changed
        if not out_changed:
            return None'''

REASON_OLD = '''            results_capped,
            "near_passthrough",
            self._session_id,'''
REASON_NEW = '''            results_capped,
            LADDER_RECORD_REASON if ladder_engaged else "near_passthrough",
            self._session_id,'''

HELPER_ANCHOR = '''    out = dict(capped)
    out["role"] = "tool"
    return out, True
'''
HELPER_NEW = HELPER_ANCHOR + '''

def _cap_openai_tool_result_ladder(soma: Any, message: Dict[str, Any]) -> tuple:
    """Token-ladder replacement for _cap_openai_tool_result (soma-tok arm).

    Same machinery as the live cap (envelope unwrap, [[CMP]] markers,
    extractive pinning, no-inflation, envelope metadata preservation) but
    sizing follows the token ladder: results under ~18K est-tokens pass
    through untouched; 18-32K tokens compress to ~18K tokens (72K chars);
    over 32K tokens compress to ~24K tokens (96K chars). Returns
    (message, changed, ladder_engaged).
    """
    if not isinstance(message, dict) or soma.normalize_role(message.get("role")) != "tool":
        return message, False, False
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return message, False, False
    if soma.CMP_START in content:
        return message, False, False  # already-compressed fixed point
    unwrapped = _unwrap_json_envelope(content)
    if unwrapped is not None:
        inner_text, envelope_obj, text_key = unwrapped
        if soma.CMP_START in inner_text:
            return message, False, False
        budget = _ladder_budget(len(inner_text))
        if budget is None or len(inner_text) <= budget:
            return message, False, False
        new_inner, changed = soma.extractive_compress(inner_text, budget, frozenset())
        if not changed:
            return message, False, False
        wrapped = soma.cmp_block(new_inner)
        envelope_obj[text_key] = wrapped
        out = dict(message)
        out["content"] = json.dumps(envelope_obj, ensure_ascii=False)
        if len(out["content"]) < len(content):
            return out, True, True
        return message, False, False
    # bare (non-envelope) text content
    budget = _ladder_budget(len(content))
    if budget is None or len(content) <= budget:
        return message, False, False
    new_text, changed = soma.extractive_compress(content, budget, frozenset())
    if not changed:
        return message, False, False
    wrapped = soma.cmp_block(new_text)
    if len(wrapped) >= len(content):
        return message, False, False
    out = dict(message)
    out["content"] = wrapped
    return out, True, True
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
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "soma"
        shutil.copytree(LIVE.parent, stage,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "bench_results", "accounting.jsonl"))
        proc = subprocess.run(["patch", "-p0", "--batch", "-i", str(OUT_PATCH)],
                              cwd=str(stage), capture_output=True, text=True)
        if proc.returncode != 0:
            sys.exit(f"patch does not apply cleanly:\\n{proc.stdout}\\n{proc.stderr}")
        proc = subprocess.run([sys.executable, "-m", "py_compile", str(stage / "engine.py")],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            sys.exit(f"patched engine.py does not compile:\\n{proc.stderr}")
    print("patch applies cleanly and compiles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
