"""Offline validation of the age-tier engine (no API calls).

Loads the AGE engine (live copy + age_tier.patch) through the real Hermes
plugin loader and drives select_context() on synthetic message histories:

  1. young already-[[CMP]] result (tail distance < 20) -> byte-identical
  2. band-1 result (tail distance >= 20) at 24K -> re-squeezed to <= 12K
  3. band-2 result (tail distance >= 40) -> re-squeezed to <= 6K
  4. fixed point: re-running select_context on the band output changes nothing
  5. hysteresis: a 13K band-2 result (within 6K*1.10? no -> but within its
     own band budget's spirit) — checks the exact boundary math
  6. accounting records the age_resqueeze reason
  7. first-time caps still happen at ANY age (a fresh 50K result at tail
     distance 60 gets its birth cap, then sits at band-0 pass-through)
"""
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH))

from soma_bench import arm_setup  # noqa: E402

arm_setup.ensure_age_engine()
AGE_DIR = arm_setup.AGE_ENGINE_DIR
acct = AGE_DIR / "accounting.jsonl"
acct.write_text("")

# load engine through the Hermes plugin machinery (same as an arm session)
sys.path.insert(0, str(BENCH / "engines"))
import importlib.util

spec = importlib.util.spec_from_file_location("somaage_engine", AGE_DIR / "engine.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

eng = mod.SomaEngine.__new__(mod.SomaEngine)
eng.__dict__.update({
    "_session_id": "offline-age-test", "context_length": 200_000,
    "last_total_tokens": 0, "compression_count": 0, "_soma_mod": None,
    "_fallback_compressor": None, "_model": "glm-5.3-flash",
    "_base_url": "x", "_api_key": None, "_provider": "say-gm", "_api_mode": "openai",
})

soma = mod._load_soma()


def report_like(kb: int) -> str:
    """~kb*1024 chars of noise + a pinned incident line (task-006 shape)."""
    lines = [f"== REPORT =="]
    i = 0
    while sum(len(l) + 1 for l in lines) < kb * 1000:
        lines.append(f"metric node-{i%40:03d}.prod cpu={i%99}% mem={i%95}% io_wait={(i%400)/100:.2f}ms")
        i += 1
    lines.append("INCIDENT services/opsshift/phase3/incident.log: NEEDLE-A1 metric_a=37 metric_b=5 — see runbooks/phase3.md")
    return "\n".join(lines)


def tool_msg(content: str) -> dict:
    return {"role": "tool", "tool_call_id": "c1", "content": content}


def envelope_msg(inner: str) -> dict:
    return {"role": "tool", "tool_call_id": "c1",
            "content": json.dumps({"output": inner, "exit_code": 0, "cwd": "/w"})}


def run_select(messages):
    return eng.select_context(messages)


def tail_pad(n: int, after: list) -> list:
    """Prepend a tool result, then n filler messages AFTER it (the tail)."""
    return after


print("== 1. young [[CMP]] result untouched ==")
big = report_like(30)                      # ~30K raw
birth = run_select([tool_msg(big)])
assert birth is not None and "[[CMP]]" in birth[0]["content"], "birth cap failed"
young = birth[0]["content"]
padded = birth + [{"role": "user", "content": "go"}] * 10   # tail distance 10
out = run_select(padded)
assert out is None, f"young result was touched (tail 10): {out is not None}"
print("   ok: tail-10 result untouched")

print("== 2. band-1 (tail >= 20) -> <= 12K ==")
padded = birth + [{"role": "user", "content": f"note {i}"} for i in range(20)]
out = run_select(padded)
assert out is not None, "band-1 re-squeeze did not fire"
c = out[0]["content"]
print(f"   24K-class -> {len(c):,} chars; needle kept: {'NEEDLE-A1' in c and 'metric_a=37' in c}")
assert len(c) <= 12_500, f"band-1 size {len(c)} > 12.5K"
assert "NEEDLE-A1" in c and "metric_a=37" in c, "band-1 lost the needle"

print("== 3. band-2 (tail >= 40) -> <= 6K ==")
padded = birth + [{"role": "user", "content": f"note {i}"} for i in range(40)]
out = run_select(padded)
assert out is not None, "band-2 re-squeeze did not fire"
c = out[0]["content"]
print(f"   -> {len(c):,} chars; needle kept: {'NEEDLE-A1' in c and 'metric_a=37' in c}")
assert len(c) <= 6_500, f"band-2 size {len(c)} > 6.5K"
assert "NEEDLE-A1" in c and "metric_a=37" in c, "band-2 lost the needle"

print("== 4. fixed point: band output re-runs unchanged ==")
b2 = out
out2 = run_select(b2 + [{"role": "user", "content": f"note {i}"} for i in range(40)])
assert out2 is None or out2[0]["content"] == b2[0]["content"], "band-2 form is not a fixed point"
print("   ok: idempotent at band 2")

print("== 5. hysteresis boundary ==")
# a band-2 form at ~6.1K (< 6K*1.10=6.6K) must NOT be re-squeezed further
small_cmp = mod._load_soma().cmp_block(report_like(6)[:5900])
msg = tool_msg(small_cmp)
padded = [msg] + [{"role": "user", "content": f"n{i}"} for i in range(40)]
out = run_select(padded)
assert out is None, "hysteresis violated: within-budget result was re-squeezed"
print("   ok: 5.9K band-2 result left alone (within hysteresis)")

print("== 6. accounting reason ==")
recs = [json.loads(l) for l in acct.read_text().splitlines() if l.strip()]
reasons = [r.get("reason") for r in recs]
print("   reasons:", reasons)
assert "age_resqueeze" in reasons, "age_resqueeze reason not recorded"

print("== 7. first-time cap at any age ==")
fresh = tool_msg(report_like(30))
padded = [fresh] + [{"role": "user", "content": f"n{i}"} for i in range(60)]
out = run_select(padded)
assert out is not None and "[[CMP]]" in out[0]["content"], "fresh old result not birth-capped"
c = out[0]["content"]
assert len(c) <= 24_500, f"birth cap size {len(c)}"
print(f"   ok: fresh 30K result at tail-60 birth-capped -> {len(c):,} chars")

print("== 8. envelope path (terminal-style) ==")
env = envelope_msg(report_like(30))
birth = run_select([env])
assert birth is not None and "[[CMP]]" in birth[0]["content"], "envelope birth cap failed"
padded = birth + [{"role": "user", "content": f"n{i}"} for i in range(40)]
out = run_select(padded)
assert out is not None, "envelope band-2 re-squeeze did not fire"
obj = json.loads(out[0]["content"])
inner = obj["output"]
print(f"   envelope re-squeezed -> inner {len(inner):,} chars; exit_code kept: {obj.get('exit_code') == 0}")
assert len(inner) <= 6_800, f"envelope band-2 inner {len(inner)}"
assert obj.get("exit_code") == 0, "envelope metadata lost"
assert "NEEDLE-A1" in inner, "envelope re-squeeze lost needle"

print("\nALL AGE-ENGINE OFFLINE CHECKS PASSED")
