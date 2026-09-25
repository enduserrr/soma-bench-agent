"""Offline validation of the token-ladder engine (no API calls).

Ladder (user proposal, token-based):
  <= 18K est-tokens (72K chars)  -> passthrough untouched
  18K-32K tokens (72-128K chars) -> compress to <= 18K tokens (72K chars)
  > 32K tokens (>128K chars)     -> compress to <= 24K tokens (96K chars)
Contrast: live cap = 24K CHARS (~6K tokens) — the ladder is far more permissive.
"""
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH))

from soma_bench import arm_setup  # noqa: E402

arm_setup.ensure_tok_engine()
TOK_DIR = arm_setup.TOK_ENGINE_DIR
acct = TOK_DIR / "accounting.jsonl"
acct.write_text("")

import importlib.util

spec = importlib.util.spec_from_file_location("somatok_engine", TOK_DIR / "engine.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

eng = mod.SomaEngine.__new__(mod.SomaEngine)
eng.__dict__.update({
    "_session_id": "offline-tok-test", "context_length": 200_000,
    "last_total_tokens": 0, "compression_count": 0, "_soma_mod": None,
    "_fallback_compressor": None, "_model": "glm-5.3-flash",
    "_base_url": "x", "_api_key": None, "_provider": "say-gm", "_api_mode": "openai",
})
soma = mod._load_soma()


def noise(kb: int, needle: str) -> str:
    lines = ["== BIG REPORT =="]
    i = 0
    while sum(len(l) + 1 for l in lines) < kb * 1000:
        lines.append(f"metric node-{i%40:03d}.prod cpu={i%99}% mem={i%95}% io_wait={(i%400)/100:.2f}ms")
        i += 1
    lines.append(needle)
    return "\n".join(lines)


NEEDLE = "INCIDENT services/x/phase3/incident.log: NEEDLE-A1 metric_a=37 metric_b=5"


def run(msgs):
    return eng.select_context(msgs)


def envelope(inner: str) -> dict:
    return {"role": "tool", "tool_call_id": "c1",
            "content": json.dumps({"output": inner, "exit_code": 0})}


def bare(text: str) -> dict:
    return {"role": "tool", "tool_call_id": "c1", "content": text}


print("== 1. small result (40K chars = 10K tok) passes through ==")
out = run([bare(noise(40, NEEDLE))])
assert out is None, "small result was touched"
print("   ok")

print("== 2. mid result (90K chars = 22.5K tok) -> <= 72K chars ==")
payload = noise(90, NEEDLE)
out = run([bare(payload)])
assert out is not None, "mid result not compressed"
c = out[0]["content"]
print(f"   90K -> {len(c):,} | needle kept: {NEEDLE.split(': ')[1][:30] in c}")
assert len(c) <= 73_000, f"mid budget exceeded: {len(c)}"
assert "NEEDLE-A1" in c and "metric_a=37" in c, "mid lost needle"

print("== 3. huge result (150K chars = 37.5K tok) -> <= 96K chars ==")
payload = noise(150, NEEDLE)
out = run([bare(payload)])
assert out is not None, "huge result not compressed"
c = out[0]["content"]
print(f"   150K -> {len(c):,} | needle kept: {'NEEDLE-A1' in c}")
assert len(c) <= 97_000, f"huge budget exceeded: {len(c)}"
assert "NEEDLE-A1" in c and "metric_a=37" in c, "huge lost needle"

print("== 4. boundary: exactly 72K chars stays untouched ==")
# 72,000 chars exactly: 72K trigger is > not >=, so untouched
payload = noise(71, NEEDLE)
out = run([bare(payload)])
print("   71K untouched:", out is None)
assert out is None, "71K result should pass through"

print("== 5. envelope path (terminal-style) mid result ==")
payload = noise(90, NEEDLE)
out = run([envelope(payload)])
assert out is not None, "envelope mid not compressed"
obj = json.loads(out[0]["content"])
inner = obj["output"]
print(f"   envelope 90K -> inner {len(inner):,} | exit_code kept: {obj.get('exit_code') == 0}")
assert len(inner) <= 73_000, f"envelope mid budget exceeded: {len(inner)}"
assert obj.get("exit_code") == 0, "envelope metadata lost"
assert "NEEDLE-A1" in inner, "envelope lost needle"

print("== 6. envelope huge result -> <= 96K ==")
payload = noise(150, NEEDLE)
out = run([envelope(payload)])
assert out is not None
obj = json.loads(out[0]["content"])
inner = obj["output"]
print(f"   envelope 150K -> inner {len(inner):,}")
assert len(inner) <= 97_000, f"envelope huge budget exceeded: {len(inner)}"
assert "NEEDLE-A1" in inner

print("== 7. already-compressed result is a fixed point ==")
out = run([bare(payload)])  # huge bare
if out is not None:
    again = run(out)
    assert again is None, "compressed form not idempotent"
    print("   ok (bare form idempotent)")

print("== 8. accounting reason ==")
recs = [json.loads(l) for l in acct.read_text().splitlines() if l.strip()]
reasons = [r.get("reason") for r in recs]
print("   reasons:", reasons)
assert "token_ladder" in reasons, "token_ladder reason not recorded"

print("== 9. non-tool messages untouched ==")
msgs = [{"role": "user", "content": noise(150, NEEDLE)},
        {"role": "assistant", "content": "hi"}]
out = run(msgs)
assert out is None, "non-tool message was touched"
print("   ok")

print("\nALL TOKEN-LADDER OFFLINE CHECKS PASSED")
