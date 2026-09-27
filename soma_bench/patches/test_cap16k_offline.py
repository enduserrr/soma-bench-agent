"""Offline validation of the cap16k engine (no API calls).

Flat cap: under 16K untouched; over 16K -> <= ~16.5K (wrapper slack).
The critical assertions: a 30K result must land at ~16K (NOT 60% = 18K)
and a 60K result at ~16K (NOT the old 24K MAX_KEEP) — those two catch the
partial-ladder bug where only the floor was lowered.
"""
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH))

from soma_bench import arm_setup  # noqa: E402

arm_setup.ensure_cap16k_engine()
CAP_DIR = arm_setup.CAP16K_ENGINE_DIR
acct = CAP_DIR / "accounting.jsonl"
acct.write_text("")  # clean slate; engine accounting re-scored from runs/

import importlib.util

spec = importlib.util.spec_from_file_location("cap16k_engine", CAP_DIR / "engine.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

eng = mod.SomaEngine.__new__(mod.SomaEngine)
eng.__dict__.update({
    "_session_id": "offline-cap16k-test", "context_length": 200_000,
    "last_total_tokens": 0, "compression_count": 0, "_soma_mod": None,
    "_fallback_compressor": None, "_model": "glm-5.3-flash",
    "_base_url": "x", "_api_key": None, "_provider": "say-gm", "_api_mode": "openai",
})
soma = mod._load_soma()
assert soma.MAX_KEEP_CHARS == 16_000, f"MAX_KEEP_CHARS not clamped: {soma.MAX_KEEP_CHARS}"
assert soma.MIN_PASSTHROUGH_CHARS == 16_000, f"floor not applied: {soma.MIN_PASSTHROUGH_CHARS}"
print(f"   sizing: floor={soma.MIN_PASSTHROUGH_CHARS:,} max_keep={soma.MAX_KEEP_CHARS:,} (flat)")


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


print("== 1. small result (10K) passes through ==")
out = run([bare(noise(10, NEEDLE))])
assert out is None, "small result was touched"
print("   ok")

print("== 2. just under floor (15K) passes through ==")
out = run([bare(noise(15, NEEDLE))])
assert out is None, "15K result was touched (floor is 16K)"
print("   ok")

print("== 3. just over floor (18K) -> ~16K ==")
out = run([bare(noise(18, NEEDLE))])
assert out is not None, "18K result not compressed"
c = out[0]["content"]
print(f"   18K -> {len(c):,} | needle: {NEEDLE.split(': ')[1][:30] in c}")
assert len(c) <= 16_800, f"cap exceeded: {len(c)}"
assert "NEEDLE-A1" in c and "metric_a=37" in c, "lost needle"

print("== 4. mid result (30K) -> ~16K NOT 18K (60% would be the ladder) ==")
out = run([bare(noise(30, NEEDLE))])
assert out is not None, "30K result not compressed"
c = out[0]["content"]
print(f"   30K -> {len(c):,}")
assert len(c) <= 16_800, f"FLAT CAP VIOLATED: {len(c)} > 16.8K (partial-ladder bug)"
assert "NEEDLE-A1" in c and "metric_a=37" in c, "lost needle"

print("== 5. big result (60K) -> ~16K NOT 24K (MAX_KEEP clamp check) ==")
out = run([bare(noise(60, NEEDLE))])
assert out is not None, "60K result not compressed"
c = out[0]["content"]
print(f"   60K -> {len(c):,}")
assert len(c) <= 16_800, f"FLAT CAP VIOLATED: {len(c)} > 16.8K (MAX_KEEP not clamped)"
assert "NEEDLE-A1" in c and "metric_a=37" in c, "lost needle"

print("== 6. envelope path (terminal-style) 60K -> inner ~16K ==")
out = run([envelope(noise(60, NEEDLE))])
assert out is not None, "envelope not compressed"
obj = json.loads(out[0]["content"])
inner = obj["output"]
print(f"   envelope 60K -> inner {len(inner):,} | exit_code kept: {obj.get('exit_code') == 0}")
assert len(inner) <= 16_800, f"envelope cap exceeded: {len(inner)}"
assert obj.get("exit_code") == 0, "envelope metadata lost"
assert "NEEDLE-A1" in inner, "envelope lost needle"

print("== 7. already-compressed result is a fixed point ==")
if out is not None:
    again = run(out)
    assert again is None, "compressed form not idempotent"
    print("   ok")

print("== 8. non-tool messages untouched ==")
msgs = [{"role": "user", "content": noise(60, NEEDLE)},
        {"role": "assistant", "content": "hi"}]
out = run(msgs)
assert out is None, "non-tool message was touched"
print("   ok")

print("\nALL CAP16K OFFLINE CHECKS PASSED")
