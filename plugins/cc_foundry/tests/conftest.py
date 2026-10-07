"""Shared test configuration for foundry plugin tests.

Auto-loads all ``bin/`` Python scripts as importable modules so tests can ``from find_polluter import main`` directly.

JS hook helpers ``run_hook`` and ``state_dir`` are exposed as pytest fixtures so test methods receive them as parameters
— no explicit imports required.

Non-fixture host-capability helpers (``_hook_tmp_base``, ``_bash_runs_posix_script``) live in ``_hook_env.py``, not
here. Nothing may import this module by the bare name ``conftest``: ``ini_options.testpaths`` spans ``benchmarks`` and
``plugins``, every tree has its own ``conftest.py``, and under ``--import-mode=importlib`` the bare name resolves to
whichever one loaded first — ``benchmarks/conftest.py`` in a full run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_BIN_DIR = _TESTS_DIR.parent / "bin"
_HOOKS_DIR = _TESTS_DIR.parent / "hooks"


def _load_bin_modules() -> None:
    """Load Foundry bin scripts into ``sys.modules`` for direct test imports."""
    for script in sorted(_BIN_DIR.glob("*.py")):
        module_name = script.stem.replace("-", "_")
        if module_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(module_name, script)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]


_load_bin_modules()


@pytest.fixture(name="run_hook")
def _run_hook() -> Callable[..., subprocess.CompletedProcess]:
    """Return callable that spawns a foundry hook via ``node``.

    Strips ``OPENAI_API_KEY`` and ``ANTHROPIC_API_KEY`` so ``agent-router.js`` falls through to tier-3 fallback without
    live API calls.
    """

    def _run(
        hook: str,
        payload: dict,
        *,
        cwd: Path | None = None,
        home: Path | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run one hook with the test's isolated environment and JSON payload."""
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(_HOOKS_DIR.parent)}
        if home is not None:
            env["HOME"] = str(home)
            env["USERPROFILE"] = str(home)
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            env.pop(k, None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["node", str(_HOOKS_DIR / hook)],
            input=json.dumps(payload),
            capture_output=True,
            # Explicit UTF-8, never bare text=True: that decodes with the parent's locale codec,
            # and cp1252 has no mapping for 0x8f — the VS-16 byte of the statusline's emoji
            # markers. The pipe reader thread dies mid-decode and stdout silently becomes None.
            encoding="utf-8",
            env=env,
            cwd=str(cwd) if cwd else None,
        )

    return _run


@pytest.fixture(name="state_dir")
def _state_dir() -> Callable[[str], Path]:
    """Return callable that maps a session id to its ``claude-state-<sid>`` path.

    Base comes from :func:`_hook_tmp_base`, so the path tracks the hook's own ``getSentinelDir()`` on every platform
    instead of assuming ``/tmp``.
    """
    from _hook_env import _hook_tmp_base  # local: _TESTS_DIR is on sys.path only after this module loads

    def _state_dir(sid: str) -> Path:
        """Return the hook state directory for one session id."""
        return _hook_tmp_base() / f"claude-state-{sid}"

    return _state_dir
