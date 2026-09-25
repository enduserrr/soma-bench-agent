"""Arm setup: scratch HERMES_HOMEs, SOMA engine copy, per-arm config.yaml.

Isolation model (dev-safety):
- Each arm gets its own scratch HERMES_HOME under ~/git/SOMA-BENCH/profiles/<arm>/.
  HERMES_HOME is honored by hermes ONLY when its parent dir is named 'profiles'
  (main.py::_apply_profile_override) — satisfied by construction here.
- The SOMA arm loads the engine via install-tree symlink 'somabench' pointing at
  a COPY of the live plugin (~/git/SOMA-BENCH/engines/somabench). The copy's
  accounting.jsonl therefore lands inside the bench tree. The LIVE plugin is
  never modified and never loaded by the bench.
- Both arms' config.yaml match on model/provider/reasoning/toolsets/max_turns.
  The ONLY difference is context.engine: 'somabench' (SOMA copy) vs 'compressor'.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import (BENCH_ENGINE_DIR, BENCH_ENGINE_SYMLINK, LIVE_CONFIG,
               LIVE_SOMA_PLUGIN, PROFILES_DIR, ARM_BASELINE, ARM_SOMA)

ARM_ENGINE = {ARM_SOMA: "somabench", ARM_BASELINE: "compressor"}


def ensure_engine_copy(force: bool = False) -> Path:
    """Copy the live SOMA plugin into the bench tree as engines/somabench.

    Verifies the copy is byte-identical to the live plugin (excluding
    accounting.jsonl, bench_results, __pycache__, .git, .pytest_cache).
    Never writes anything into the live plugin dir.
    """
    if BENCH_ENGINE_DIR.exists() and not force:
        _verify_copy()
        return BENCH_ENGINE_DIR
    if BENCH_ENGINE_DIR.exists():
        shutil.rmtree(BENCH_ENGINE_DIR)
    BENCH_ENGINE_DIR.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        LIVE_SOMA_PLUGIN, BENCH_ENGINE_DIR,
        ignore=shutil.ignore_patterns(
            "__pycache__", ".pytest_cache", "accounting.jsonl",
            "bench_results", ".git", "*.pyc"),
    )
    # The copy must stay a git-less, accounting-clean bench artifact.
    _verify_copy()
    return BENCH_ENGINE_DIR


def _verify_copy() -> None:
    """Assert every tracked source file in the copy is byte-identical to the live plugin."""
    import filecmp
    checked = 0
    for live_file in LIVE_SOMA_PLUGIN.rglob("*"):
        if live_file.is_dir():
            continue
        rel = live_file.relative_to(LIVE_SOMA_PLUGIN)
        if any(p in {"__pycache__", ".pytest_cache", ".git", "bench_results"} for p in rel.parts):
            continue
        if rel.name == "accounting.jsonl":
            continue
        copy_file = BENCH_ENGINE_DIR / rel
        if not copy_file.exists():
            raise RuntimeError(f"engine copy missing file: {rel}")
        if not filecmp.cmp(live_file, copy_file, shallow=False):
            raise RuntimeError(f"engine copy drifted from live plugin: {rel}")
        checked += 1
    if checked < 5:
        raise RuntimeError(f"engine copy verification checked suspiciously few files ({checked})")


def ensure_install_symlink() -> Path:
    """Create/repair the install-tree symlink plugins/context_engine/somabench -> engine copy.

    The install tree only scans its OWN plugins/context_engine/ dir, so the SOMA
    arm needs this link. It is an ADDITIONAL name (the live 'soma' symlink stays
    untouched) and is safe to leave in place between runs.
    """
    if BENCH_ENGINE_SYMLINK.is_symlink():
        target = os.readlink(BENCH_ENGINE_SYMLINK)
        if Path(target).resolve() == BENCH_ENGINE_DIR.resolve():
            return BENCH_ENGINE_SYMLINK
        BENCH_ENGINE_SYMLINK.unlink()  # repoint at the bench copy
    elif BENCH_ENGINE_SYMLINK.exists():
        raise RuntimeError(
            f"{BENCH_ENGINE_SYMLINK} exists and is NOT a symlink — refusing to touch it. "
            "Investigate manually.")
    BENCH_ENGINE_SYMLINK.symlink_to(BENCH_ENGINE_DIR)
    return BENCH_ENGINE_SYMLINK


def remove_install_symlink() -> None:
    """Idempotent teardown of the bench symlink (never touches the live 'soma' link)."""
    if BENCH_ENGINE_SYMLINK.is_symlink():
        BENCH_ENGINE_SYMLINK.unlink()


_ARM_CONFIG_TEMPLATE = """\
model:
  default: {model}
  provider: {provider}
  key_env: {key_env}
  base_url: {base_url}
providers:
  {provider}:
    name: {provider_name}
    base_url: {base_url}
    model: {model}
    discover_models: true
    key_env: {key_env}
    default_model: {model}
agent:
  max_turns: '{max_turns}'
  reasoning_effort: {reasoning}
  verbose: false
terminal:
  backend: local
  cwd: .
  timeout: 600
compression:
  enabled: true
  threshold: 0.5
  protect_last_n: 20
  min_tail_user_messages: 1
context:
  engine: {engine}
auxiliary:
  compression:
    provider: {provider}
    model: {aux_model}
    base_url: {base_url}
memory:
  memory_enabled: false
  user_profile_enabled: false
sessions:
  auto_archive: false
telemetry:
  shared_metrics:
    enabled: false
plugins:
  enabled: []
  disabled:
    - context_engine/soma
display:
  personality: concise
  streaming: false
  tool_progress: none
skills:
  creation_nudge_interval: 0
timezone: {timezone}
"""

# Fallbacks if the live config cannot be parsed.
_FALLBACK_LIFT = {
    "provider": "say-gm", "provider_name": "GM",
    "base_url": "https://api.saygm.com/v1",
    "key_env": "HERMES_CUSTOM_SAY_GM_API_KEY",
    "model": "glm-5.3-flash", "aux_model": "glm-5.3-flash",
    "reasoning": "high", "max_turns": 125, "timezone": "Europe/Helsinki",
}


def lift_live_config() -> dict:
    """Non-secret model/provider settings lifted from the LIVE config.yaml so the
    arms track the instance's actual model setup. Secrets are never lifted —
    the arm .env symlinks the live .env (symmetric across arms)."""
    from . import DEFAULT_TOOLSETS
    lift = dict(_FALLBACK_LIFT)
    try:
        import yaml
        cfg = yaml.safe_load(LIVE_CONFIG.read_text(encoding="utf-8")) or {}
        model_cfg = cfg.get("model") or {}
        provider = model_cfg.get("provider") or lift["provider"]
        prov_block = (cfg.get("providers") or {}).get(provider) or {}
        lift.update({
            "provider": provider,
            "provider_name": prov_block.get("name") or provider,
            "base_url": model_cfg.get("base_url") or prov_block.get("base_url") or lift["base_url"],
            "key_env": model_cfg.get("key_env") or prov_block.get("key_env") or lift["key_env"],
            "model": model_cfg.get("default") or prov_block.get("default_model") or lift["model"],
            "reasoning": (cfg.get("agent") or {}).get("reasoning_effort") or lift["reasoning"],
            "max_turns": (cfg.get("agent") or {}).get("max_turns") or lift["max_turns"],
            "timezone": cfg.get("timezone") or lift["timezone"],
        })
        aux = ((cfg.get("auxiliary") or {}).get("compression") or {})
        lift["aux_model"] = aux.get("model") or lift["model"]
    except Exception:
        pass  # fallback lift keeps the bench runnable
    if isinstance(lift["max_turns"], str):
        lift["max_turns"] = int(lift["max_turns"])
    lift.setdefault("toolsets", DEFAULT_TOOLSETS)
    return lift


def arm_home(arm: str) -> Path:
    return PROFILES_DIR / arm


def setup_arm(arm: str, *, force: bool = False, setup: dict | None = None) -> Path:
    """Create/refresh the scratch HERMES_HOME for one arm. Idempotent."""
    home = arm_home(arm)
    if home.exists() and not force:
        _verify_arm(home, arm)
        return home
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    cfg = lift_live_config()
    cfg.update(setup or {})
    (home / "config.yaml").write_text(_ARM_CONFIG_TEMPLATE.format(
        engine=ARM_ENGINE[arm],
        **{k: cfg[k] for k in ("provider", "provider_name", "base_url", "key_env",
                                "model", "aux_model", "reasoning", "max_turns", "timezone")},
    ), encoding="utf-8")
    # .env: symlink the live secrets file (READ-ONLY use; keys never copied).
    os.symlink(LIVE_CONFIG.parent / ".env", home / ".env")
    # skills dir: empty — bench agents load no user skills (keeps arms identical
    # and avoids the bench's own skill leaking into a task run)
    (home / "skills").mkdir()
    _verify_arm(home, arm)
    return home


def _verify_arm(home: Path, arm: str) -> None:
    cfg_path = home / "config.yaml"
    if not cfg_path.exists():
        raise RuntimeError(f"arm home {home} missing config.yaml")
    import yaml
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if cfg.get("context", {}).get("engine") != ARM_ENGINE[arm]:
        raise RuntimeError(f"arm {arm}: context.engine != {ARM_ENGINE[arm]!r} in {cfg_path}")
    if not (home / ".env").is_symlink():
        raise RuntimeError(f"arm home {home}: .env is not a symlink to the live secrets file")


def setup_bench(force: bool = False) -> dict:
    """Full bench setup: engine copy + install symlink + both arm homes."""
    ensure_engine_copy(force=force)
    ensure_install_symlink()
    setup_arm(ARM_SOMA, force=force)
    setup_arm(ARM_BASELINE, force=force)
    return {
        "engine_copy": str(BENCH_ENGINE_DIR),
        "install_symlink": str(BENCH_ENGINE_SYMLINK),
        "arms": {a: str(arm_home(a)) for a in (ARM_SOMA, ARM_BASELINE)},
        "model_lift": lift_live_config(),
    }


def verify_bench() -> dict:
    """Read-only verification pass; raises on any invariant violation."""
    _verify_copy()
    link = BENCH_ENGINE_SYMLINK
    if not link.is_symlink() or Path(os.readlink(link)).resolve() != BENCH_ENGINE_DIR.resolve():
        raise RuntimeError("install-tree 'somabench' symlink missing/mispointed")
    for arm in (ARM_SOMA, ARM_BASELINE):
        _verify_arm(arm_home(arm), arm)
    return {"ok": True}
