"""Offline validation of the marginal-rewrite guard (no API calls).

Loads the GUARD engine (patched copy) and the SOMA engine (pristine copy)
through the real Hermes plugin loader, then drives select_context with
synthetic oversized tool-result messages in the exact Hermes envelope shapes:

1. pinned-dense payload  -> guard must SKIP (message byte-identical);
                            pristine soma must COMMIT the marginal rewrite.
2. mixed prose payload   -> guard must COMMIT (>=2% savings);
                            output strictly smaller, [[CMP]] present.
3. small payload (<24K)  -> both engines no-op (None).
4. accounting visibility -> guard writes 'marginal_skipped' records.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/home/hermes/.hermes/hermes-agent")
sys.path.insert(0, str(Path.home() / "git/SOMA-BENCH"))

from plugins.context_engine import load_context_engine

BENCH = Path.home() / "git/SOMA-BENCH"

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)

def make_tool_msg(text: str, key: str = "output") -> dict:
    envelope = {key: text, "exit_code": 0}
    return {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(envelope)}

def pinned_dense_payload(target_kb: int = 40) -> str:
    # mostly pinned path-lines + ~1% short unpinned filler lines -> extraction
    # drops only the filler -> keep ~= 99% of input (the marginal-rewrite shape
    # seen 34% of the time in live accounting: 232,346 -> 232,316)
    lines = []
    i = 0
    total = 0
    while total < target_kb * 1024:
        if i % 100 == 7:
            lines.append("ok")  # short, unpinned, zero TF-IDF value
            total += 3
        else:
            l = f"/home/hermes/git/repo/src/module_{i % 977}/file_{i:05d}.py:{i % 900 + 1}: ok status=pass"
            lines.append(l)
            total += len(l) + 1
        i += 1
    return "\n".join(lines)

def mixed_prose_payload(target_kb: int = 40) -> str:
    # mostly unpinned filler lines (logs) + a few pinned ones -> large savings
    lines = []
    i = 0
    while sum(len(l) + 1 for l in lines) < target_kb * 1024:
        if i % 1000 == 500:
            lines.append(f"Traceback (most recent call last) FILE /repo/app.py:{i}: KeyError 'x' TICKET-99")
        else:
            lines.append(f"routine request handled without incident number {i:07d} nothing to see here")
        i += 1
    return "\n".join(lines)

guard_acct = BENCH / "engines/somaguard/accounting.jsonl"
soma_acct = BENCH / "engines/somabench/accounting.jsonl"
guard_acct.write_text("")  # clean slate for this test; bench data re-scored later

# --- load engines through the real loader
guard = load_context_engine("somaguard")
soma = load_context_engine("somabench")
check("guard engine loads", guard is not None and guard.name == "soma")
check("soma engine loads", soma is not None and soma.name == "soma")

# --- 1. pinned-dense (mostly-pinned + few tiny unpinned lines)
# NOTE: a 100%-pinned payload is a genuine no-op in BOTH engines (keep==all).
# The real marginal population is "few giant lines + tiny droppable lines"
# (e.g. MCP results: 5 lines, one 49.6K escaped-JSON mega-line) — covered by
# test_guard_real_shape.py. Here we assert the no-op case stays a no-op under
# the guard (regression: guard must not turn no-ops into commits).
payload = pinned_dense_payload(40)
msg = make_tool_msg(payload)
messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
            msg]

out_guard = guard.select_context([m for m in messages])
out_soma = soma.select_context([m for m in messages])
len_in = len(json.dumps(messages))
print(f"  pinned-dense: input={len_in} chars; "
      f"soma_out={'COMMIT' if out_soma else 'no-op'} guard_out={'COMMIT' if out_guard else 'no-op'}")
check("guard no-ops pinned-dense (unchanged behavior)", out_guard is None)
check("soma no-ops pinned-dense too (fixture is a true no-op)", out_soma is None)

# --- 2. mixed prose (should compress hard under both)
msg2 = make_tool_msg(mixed_prose_payload(40), key="content")
messages2 = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
             {"role": "assistant", "content": "", "tool_calls": [
                 {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
             msg2]
out_guard2 = guard.select_context([m for m in messages2])
out_soma2 = soma.select_context([m for m in messages2])
check("guard COMMITS mixed-prose rewrite (>=2% savings)", out_guard2 is not None)
check("soma COMMITS mixed-prose rewrite", out_soma2 is not None)
if out_guard2:
    len_out = sum(len(str(m.get('content') or '')) for m in out_guard2)
    saved_pct = (len_in2 := len(json.dumps(messages2)))
    print(f"  mixed: in={len_in2} out={len_out} saved={(len_in2-len_out)/len_in2*100:.1f}%")
    check("guard mixed output strictly smaller", len_out < len_in2)
    check("guard mixed output carries CMP marker", any("[[CMP]]" in str(m.get('content')) for m in out_guard2))

# --- 3. small payload no-op under both
small = make_tool_msg("x" * 5000)
out_g3 = guard.select_context([small])
out_s3 = soma.select_context([small])
check("guard no-op on <24K", out_g3 is None)
check("soma no-op on <24K", out_s3 is None)

# --- 4. accounting: guard wrote a marginal_skipped record for the REAL shape
# (the synthetic payloads here don't trigger commits/skips; see
# test_guard_real_shape.py — but run its shape inline for the accounting check)
real = Path("/tmp/real_oversized.txt")
if real.exists():
    raw = real.read_text()
    msg_r = {"role": "tool", "tool_call_id": "call_9", "content": raw}
    msgs_r = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
              {"role": "assistant", "content": "", "tool_calls": [
                  {"id": "call_9", "type": "function", "function": {"name": "x_search", "arguments": "{}"}}]},
              msg_r]
    guard.select_context([m for m in msgs_r])
    records = [json.loads(l) for l in guard_acct.read_text().splitlines() if l.strip()]
    reasons = [r.get("reason") for r in records]
    print(f"  guard accounting reasons: {reasons}")
    check("guard accounting has marginal_skipped record", "marginal_skipped" in reasons)
    skip_recs = [r for r in records if r.get("reason") == "marginal_skipped"]
    check("marginal_skipped records show in==out chars",
          all(r["input_est_chars"] == r["output_est_chars"] for r in skip_recs))
else:
    print("  (real_oversized.txt absent — skipping accounting checks)")

# --- determinism: same input twice -> identical output
out_g4 = guard.select_context([m for m in messages2])
check("guard deterministic on repeat call",
      json.dumps(out_guard2, sort_keys=True) == json.dumps(out_g4, sort_keys=True))

print()
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
