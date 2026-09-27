"""Reproduce the REAL marginal shape: 5-line MCP result with one 49.6K mega-line.
Non-envelope path (raw text content) — the path my patch guards."""
import json
import sys
from pathlib import Path

HERMES_AGENT_DIR = Path.home() / ".hermes" / "hermes-agent"
sys.path.insert(0, str(HERMES_AGENT_DIR))
sys.path.insert(0, str(Path.home() / "git/SOMA-BENCH"))
from plugins.context_engine import load_context_engine

BENCH = Path.home() / "git/SOMA-BENCH"
real = Path("/tmp/real_oversized.txt").read_text()

guard = load_context_engine("somaguard")
soma = load_context_engine("somabench")

# non-envelope raw content (as persisted: starts with <untrusted_tool_result>)
msg = {"role": "tool", "tool_call_id": "call_1", "content": real}
messages = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "go"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "x_search", "arguments": "{}"}}]},
    msg,
]

out_soma = soma.select_context([m for m in messages])
out_guard = guard.select_context([m for m in messages])
in_len = len(real)

def result_len(out):
    if out is None:
        return in_len
    for m in out:
        if m.get("role") == "tool":
            return len(str(m.get("content") or ""))
    return in_len

s_len, g_len = result_len(out_soma), result_len(out_guard)
print(f"input: {in_len} chars (5 lines, mega-line 49.6K)")
print(f"pristine soma:  {'COMMIT' if out_soma else 'no-op'} -> {s_len} chars (saved {in_len - s_len})")
print(f"guard:          {'COMMIT' if out_guard else 'no-op'} -> {g_len} chars (saved {in_len - g_len})")

ok = []
ok.append(("guard SKIPS the real marginal shape", out_guard is None))
ok.append(("soma COMMITS the real marginal shape (guard changes behavior)",
           out_soma is not None and 0 < in_len - s_len < in_len * 0.02))

# same content as JSON envelope (the other path): wrap in {"content": ...}
msg2 = {"role": "tool", "tool_call_id": "call_2",
        "content": json.dumps({"content": real, "total_lines": 5})}
messages2 = [messages[0], messages[1],
             {"role": "assistant", "content": "", "tool_calls": [
                 {"id": "call_2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
             msg2]
out_soma2 = soma.select_context([m for m in messages2])
out_guard2 = guard.select_context([m for m in messages2])
print(f"\nenvelope-wrapped variant: soma={'COMMIT' if out_soma2 else 'no-op'} "
      f"guard={'COMMIT' if out_guard2 else 'no-op'}")
ok.append(("guard SKIPS envelope-wrapped marginal shape too", out_guard2 is None))

for name, cond in ok:
    print(f"  {'PASS' if cond else 'FAIL'} {name}")
sys.exit(0 if all(c for _, c in ok) else 1)
