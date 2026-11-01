"""Execute fresh consumer shells and nested helpers against the real Codemap provider.

Each case leaves the persisted gate choice a consumer's earlier step would have written, runs that consumer's shipped
bash recipe in a fresh shell, and checks that the choice (frozen, current or skipped) reached every provider subprocess,
fallback children included. The provider and its entry point are real; only a logging shim sits in front of them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import NamedTuple

import pytest
from _develop_text import bash_fences

ROOT = Path(__file__).resolve().parents[4]
BASH = shutil.which("bash")
_ENTRY = ROOT / "plugins/codemap-py/scripts/codemap_py_entry.py"

#: Logging shim in front of the real provider entry point; ``FAIL_BATCH=1`` makes ``batch`` fail to force fallbacks.
_PROVIDER_SCRIPT = (
    "import json,os,subprocess,sys\n"
    "with open(os.environ['QUERY_LOG'],'a') as f:\n"
    " f.write(json.dumps({'args':sys.argv[1:],'freeze':os.environ.get('SCAN_NO_AUTOBUILD')})+'\\n')\n"
    "if os.environ.get('FAIL_BATCH') == '1' and 'batch' in sys.argv: raise SystemExit(1)\n"
    f"result=subprocess.run([sys.executable,{str(_ENTRY)!r},*sys.argv[1:]],capture_output=True)\n"
    "with open(os.environ['QUERY_RESULTS_LOG'],'a',encoding='utf-8') as f:\n"
    " f.write(json.dumps({'args':sys.argv[1:],'returncode':result.returncode,'stdout':result.stdout.decode('utf-8',errors='backslashreplace'),'stderr':result.stderr.decode('utf-8',errors='backslashreplace')})+'\\n')\n"
    "sys.stdout.buffer.write(result.stdout)\n"
    "sys.stderr.buffer.write(result.stderr)\n"
    "raise SystemExit(result.returncode)\n"
)


class _Outcome(NamedTuple):
    """What a recipe run left behind: provider invocations, their diagnostics, and whether the index file changed."""

    queries: list[dict]
    diagnostics: str
    index_changed: bool


def _write(path: Path, text: str) -> None:
    """Write *text* to *path* as UTF-8 with LF newlines."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _init_project(tmp_path: Path) -> tuple[Path, Path]:
    """Create an empty Git project holding ``leaf.py`` and ``caller.py``; return the project and the caller file."""
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    (project / "leaf.py").write_text("def _target():\n    return 1\n", encoding="utf-8")
    caller = project / "caller.py"
    caller.write_text("VALUE = 1\n", encoding="utf-8")
    return project, caller


def _install_provider(tmp_path: Path) -> Path:
    """Install the logging ``codemap-py`` and ``python`` shims on a private bin dir; return the shim script path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    provider_script = bin_dir / "provider.py"
    _write(provider_script, _PROVIDER_SCRIPT)
    provider = bin_dir / "codemap-py"
    _write(provider, f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "{provider_script.as_posix()}" "$@"\n')
    provider.chmod(0o700)
    if os.name == "nt":
        (bin_dir / "codemap-py.cmd").write_text(
            f'@"{sys.executable}" "{provider_script}" %*\r\n', encoding="utf-8", newline=""
        )
    python = bin_dir / "python"
    _write(python, f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n')
    python.chmod(0o700)
    return provider_script


def _base_env(tmp_path: Path) -> dict[str, str]:
    """Return the environment of a fresh consumer shell: shims first on ``PATH`` and every state dir under
    *tmp_path*."""
    env = {
        **os.environ,
        "PATH": str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "TMPDIR": str(tmp_path),
        "CSID": "choice",
        "CLAUDE_CODE_SESSION_ID": "choice",
        "QUERY_LOG": str(tmp_path / "queries.jsonl"),
        "QUERY_RESULTS_LOG": str(tmp_path / "query-results.jsonl"),
        "TARGET_MODULE": "leaf",
        "TARGET_FN": "",
        "CODEMAP_INDEX_DIR": str(tmp_path / "index"),
        "CODEMAP_LOG_DIR": str(tmp_path / "logs"),
        "CODEMAP_COORDINATION_DIR": str(tmp_path / "coordination"),
    }
    for key in ("SCAN_NO_AUTOBUILD", "_CODEMAP_NO_HEAL", "CODEMAP_BIN"):
        env.pop(key, None)
    return env


def _stale_index(tmp_path: Path, project: Path, caller: Path, provider_script: Path, env: dict[str, str]) -> Path:
    """Build a real index, reset the logs, then edit ``caller.py`` so the index is stale; return the index path."""
    built = subprocess.run(
        [sys.executable, str(provider_script), "index"], cwd=project, env=env, capture_output=True, text=True
    )
    assert built.returncode == 0, built.stderr
    (tmp_path / "query-results.jsonl").write_text("", encoding="utf-8")
    caller.write_text("import leaf\n", encoding="utf-8")
    (tmp_path / "queries.jsonl").write_text("", encoding="utf-8")
    return tmp_path / "index/project.json"


def _wire_oss(tmp_path: Path, env: dict[str, str], enabled: str, mode: str) -> tuple[str, str, str]:
    """Wire an oss review block; its batch always fails so the renderer's per-query fallback children run."""
    text = (ROOT / "plugins/cc_oss/skills/review/modes/codemap-context.md").read_text(encoding="utf-8")
    block = text.split(f"{mode} block (", 1)[1].split("```bash\n", 1)[1].split("\n```", 1)[0]
    stage = tmp_path / "stage.md"
    _write(stage, "context\n")
    _write(tmp_path / "oss-review-pr-tag-choice", "target\n")
    _write(tmp_path / "oss-review-codemap-context-stage-target-choice", str(stage) + "\n")
    _write(tmp_path / "oss-review-changed-mods-target-choice", "leaf\n")
    env["FAIL_BATCH"] = "1"
    return "cc_oss", "review", block


def _develop_block(tmp_path: Path, skill: str, enabled: str, needle: str) -> str:
    """Return the develop ``SKILL.md`` bash fence containing *needle* and persist its enabled/target choices."""
    text = (ROOT / f"plugins/cc_develop/skills/{skill}/SKILL.md").read_text(encoding="utf-8")
    block = next(block for block in bash_fences(text) if needle in block)
    _write(tmp_path / f"dev-{skill}-codemap-enabled-choice", enabled + "\n")
    _write(tmp_path / f"dev-{skill}-target-module-choice", "leaf\n")
    return block


def _wire_develop_plain(tmp_path: Path, env: dict[str, str], enabled: str, skill: str, needle: str):
    """Wire a develop skill whose recipe needs no further preparation (``feature`` and ``debug``)."""
    return "cc_develop", "dev", _develop_block(tmp_path, skill, enabled, needle)


def _wire_develop_plan(tmp_path: Path, env: dict[str, str], enabled: str):
    """Wire ``plan``, whose choices also live in a numbered namespace directory."""
    block = _develop_block(tmp_path, "plan", enabled, "sizing-rdeps")
    namespace = tmp_path / "plan"
    namespace.mkdir()
    _write(tmp_path / "dev-plan-ns-current-choice", str(namespace) + "\n")
    _write(namespace / "codemap-enabled", enabled + "\n")
    _write(namespace / "target-module", "leaf\n")
    return "cc_develop", "dev", block


def _wire_develop_refactor(tmp_path: Path, env: dict[str, str], enabled: str):
    """Wire ``refactor``, pointing its ``find <target>`` at the project root."""
    block = _develop_block(tmp_path, "refactor", enabled, "REFACTOR_FILES=")
    return "cc_develop", "dev", block.replace("find <target>", "find .")


def _wire_develop_review(tmp_path: Path, env: dict[str, str], enabled: str):
    """Wire ``review`` and extend its block with the nested impact and scan helpers a hydrated shell owns."""
    block = _develop_block(tmp_path, "review", enabled, "codemap_available=false")
    block += '\nif [ "$CODEMAP_ENABLED" = true ]; then\n'
    payload = json.dumps({"changed_modules": [{"path": "leaf.py", "changed_symbols": ["leaf::_target"]}]})
    for owner in ("cc_oss", "cc_develop"):
        block += f"printf '%s' '{payload}' | python '{ROOT / 'plugins' / owner / 'bin/review_impact_tier.py'}'\n"
    block += f"python '{ROOT / 'plugins/cc_develop/bin/codemap_scan.py'}' --source=find --target=.\n"
    block += f"python '{ROOT / 'plugins/cc_oss/bin/resolve_test_plan.py'}' targeted --files leaf.py\n"
    block += f"python '{ROOT / 'plugins/cc_develop/bin/dev_test_targets.py'}' --pytest-cmd pytest --files leaf.py\nfi\n"
    return "cc_develop", "dev", block


def _wire_research(tmp_path: Path, env: dict[str, str], enabled: str):
    """Wire the research shared block, which keys its sentinels by ``CODEMAP_SENTINEL_SLUG``."""
    text = (ROOT / "plugins/cc_research/skills/_shared/codemap-context.md").read_text(encoding="utf-8")
    block = text.split("```bash\n", 1)[1].split("\n```", 1)[0]
    env["CODEMAP_SENTINEL_SLUG"] = "research-run"
    _write(tmp_path / "research-run-codemap-enabled-choice", enabled + "\n")
    return "cc_research", "research", block


def _check_oss_fallback(outcome: _Outcome) -> None:
    """Expect the failed primary batch plus both real fallback queries from an oss block."""
    assert len(outcome.queries) == 3


def _check_review_helpers(outcome: _Outcome) -> None:
    """Expect the develop review block's nested impact and scan helpers to query the provider."""
    queries = outcome.queries
    assert sum("fn-blast" in item["args"] for item in queries) == 2, outcome.diagnostics
    assert any("rdeps" in item["args"] for item in queries), outcome.diagnostics
    assert sum("test-impact" in item["args"] for item in queries) == 2, outcome.diagnostics


def _check_nothing_extra(outcome: _Outcome) -> None:
    """Routes without route-specific expectations rely on the choice checks alone."""


def _verify_skip(outcome: _Outcome, route_check: Callable[[_Outcome], None]) -> None:
    """Expect a skipped gate to make no provider call and leave the index untouched."""
    assert outcome.queries == []
    assert not outcome.index_changed


def _verify_frozen(outcome: _Outcome, route_check: Callable[[_Outcome], None]) -> None:
    """Expect a frozen gate to query with ``SCAN_NO_AUTOBUILD=1`` and never rebuild the index."""
    assert outcome.queries
    assert all(item["freeze"] == "1" for item in outcome.queries), outcome.queries
    assert not outcome.index_changed
    route_check(outcome)


def _verify_current(outcome: _Outcome, route_check: Callable[[_Outcome], None]) -> None:
    """Expect a current gate to query without the freeze flag and let the provider rebuild the index."""
    assert outcome.queries
    assert all(item["freeze"] is None for item in outcome.queries), outcome.queries
    assert outcome.index_changed, outcome.diagnostics
    route_check(outcome)


def _assert_children_completed(tmp_path: Path, queries: list[dict], fail_batch: bool) -> str:
    """Assert every non-failed provider call finished with its expected exit code; return the raw diagnostics.

    The fixture project is uncommitted, so ``diff-impact`` has no HEAD and must exit 1 with ``git diff failed``, the
    signal the caller uses to fall back; every other call must exit 0.
    """
    diagnostics = (tmp_path / "query-results.jsonl").read_text(encoding="utf-8")
    completed = [json.loads(line) for line in diagnostics.splitlines()]
    expected = [item for item in queries if not (fail_batch and "batch" in item["args"])]
    assert Counter(tuple(item["args"]) for item in completed) == Counter(tuple(item["args"]) for item in expected), (
        diagnostics
    )
    assert all(item["returncode"] == (1 if "diff-impact" in item["args"] else 0) for item in completed), diagnostics
    assert all(
        json.loads(item["stdout"])["error"] == "git diff failed" for item in completed if "diff-impact" in item["args"]
    ), diagnostics
    return diagnostics


@pytest.mark.integration
@pytest.mark.skipif(BASH is None, reason="documented consumer recipes require bash")
@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="the codemap-py entry point rejects CPython below 3.11, the provider's interpreter",
)
@pytest.mark.parametrize(
    ("wire", "route_check"),
    [
        pytest.param(partial(_wire_oss, mode="qa"), _check_oss_fallback, id="oss-qa"),
        pytest.param(partial(_wire_oss, mode="docs"), _check_oss_fallback, id="oss-docs"),
        pytest.param(_wire_develop_review, _check_review_helpers, id="develop-review"),
        pytest.param(
            partial(_wire_develop_plain, skill="feature", needle="codemap-py query --timeout 5 rdeps"),
            _check_nothing_extra,
            id="develop-feature",
        ),
        pytest.param(_wire_develop_plan, _check_nothing_extra, id="develop-plan"),
        pytest.param(
            partial(_wire_develop_plain, skill="debug", needle="codemap-py query central"),
            _check_nothing_extra,
            id="develop-debug",
        ),
        pytest.param(_wire_develop_refactor, _check_nothing_extra, id="develop-refactor"),
        pytest.param(_wire_research, _check_nothing_extra, id="research"),
    ],
)
@pytest.mark.parametrize(
    ("enabled", "currency", "extra_env", "verify"),
    [
        pytest.param("true", "stale", {}, _verify_frozen, id="stale"),
        pytest.param("true", "current", {}, _verify_current, id="current"),
        pytest.param("true", "current", {"SCAN_NO_AUTOBUILD": "1"}, _verify_frozen, id="inherited-freeze"),
        pytest.param("false", "current", {}, _verify_skip, id="skip"),
    ],
)
def test_fresh_query_shell_retains_gate_choice(tmp_path, wire, route_check, enabled, currency, extra_env, verify):
    """The actual recipe carries the persisted gate choice into every provider subprocess, fallback included.

    A project with a built index goes stale; the consumer's own bash block then runs in a fresh shell with the choice
    sentinels set. Frozen choices must pass ``SCAN_NO_AUTOBUILD=1`` to each query and keep the index bytes, a current
    choice lets the provider rebuild, and a skip makes no query at all.
    """
    project, caller = _init_project(tmp_path)
    provider_script = _install_provider(tmp_path)
    env = _base_env(tmp_path)
    index = _stale_index(tmp_path, project, caller, provider_script, env)
    original = index.read_bytes()
    plugin, prefix, block = wire(tmp_path, env, enabled)
    _write(tmp_path / f"{prefix}-codemap-enabled-choice", enabled + "\n")
    _write(tmp_path / f"{prefix}-codemap-currency-choice", currency + "\n")
    env.update(extra_env)
    env["CLAUDE_PLUGIN_ROOT"] = str(ROOT / "plugins" / plugin)

    result = subprocess.run([BASH, "-c", block], cwd=project, env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    queries = [json.loads(line) for line in (tmp_path / "queries.jsonl").read_text(encoding="utf-8").splitlines()]
    diagnostics = _assert_children_completed(tmp_path, queries, env.get("FAIL_BATCH") == "1")
    verify(_Outcome(queries, diagnostics, index.read_bytes() != original), route_check)
