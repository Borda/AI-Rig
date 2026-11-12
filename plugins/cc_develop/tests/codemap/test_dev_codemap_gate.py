"""Prove Codemap gate decisions survive separate helper and consumer processes.

Every case runs the shipped gate scripts as real child processes, because the contract is what a *later, fresh* process
reads back from the sentinel files an earlier one wrote.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_ROOT = PLUGIN_ROOT.parent
_GATE = PLUGIN_ROOT / "bin/dev_codemap_gate.py"


def _run(command: list[str], cwd: Path | None, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run *command* as a child process and capture its text output without raising on failure."""
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)


@dataclass
class _Consumer:
    """How one consumer's gate is invoked and where it persists its decision."""

    command: list[str]
    skip: list[str]
    enabled: Path
    currency: Path
    preserved: dict[Path, str] = field(default_factory=dict)


def _wire_develop(tmp_path: Path, skill: str) -> _Consumer:
    """Wire a develop skill: the raw flag sentinel starts as ``auto`` and must survive a skip."""
    raw = tmp_path / f"dev-{skill}-codemap-gate-flow"
    raw.write_text("auto\n", encoding="utf-8")
    command = [sys.executable, str(_GATE), skill]
    return _Consumer(
        command=command,
        skip=[*command, "--skip"],
        enabled=tmp_path / f"dev-{skill}-codemap-enabled-gate-flow",
        currency=tmp_path / "dev-codemap-currency-gate-flow",
        preserved={raw: "auto\n"},
    )


def _wire_develop_fix(tmp_path: Path) -> _Consumer:
    """Wire the develop ``fix`` skill."""
    return _wire_develop(tmp_path, "fix")


def _wire_develop_review(tmp_path: Path) -> _Consumer:
    """Wire the develop ``review`` skill, whose legacy enabled alias also starts raw before initial detection."""
    (tmp_path / "dev-review-codemap-enabled-gate-flow").write_text("auto\n", encoding="utf-8")
    return _wire_develop(tmp_path, "review")


def _wire_oss_review(tmp_path: Path) -> _Consumer:
    """Wire the oss ``review`` detection script with its ``--force-off`` skip."""
    command = [sys.executable, str(PLUGINS_ROOT / "cc_oss/bin/detect_codemap.py"), "--prefix", "review"]
    return _Consumer(
        command=command,
        skip=[*command, "--force-off"],
        enabled=tmp_path / "review-codemap-enabled-gate-flow",
        currency=tmp_path / "review-codemap-currency-gate-flow",
    )


def _wire_research_run(tmp_path: Path) -> _Consumer:
    """Wire the research flag script with its ``--no-codemap`` skip."""
    script = str(PLUGINS_ROOT / "cc_research/bin/codemap-flag.py")
    return _Consumer(
        command=[sys.executable, script, "research-run", ""],
        skip=[sys.executable, script, "research-run", "--no-codemap"],
        enabled=tmp_path / "research-run-codemap-enabled-gate-flow",
        currency=tmp_path / "research-codemap-currency-gate-flow",
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "wire",
    [
        pytest.param(_wire_develop_fix, id="develop-fix"),
        pytest.param(_wire_develop_review, id="develop-review"),
        pytest.param(_wire_oss_review, id="oss-review"),
        pytest.param(_wire_research_run, id="research-run"),
    ],
)
def test_gate_build_and_skip_reach_next_process(tmp_path: Path, wire: Callable[[Path], _Consumer]) -> None:
    """A successful build enables the next fresh consumer, while an explicit skip persists disabled.

    Starting with no index, the gate records disabled/``no_index`` and the next consumer makes no query. After a real
    index is built the same gate records enabled/``current`` and the next consumer queries once. A skip flips both back
    to disabled/``off`` without a further query.
    """
    project = tmp_path / "project"
    project.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    index_dir = tmp_path / "index"
    query_log = tmp_path / "queries.jsonl"
    provider = bin_dir / ("codemap-py.cmd" if os.name == "nt" else "codemap-py")
    provider.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "if sys.argv[1] == 'index':\n"
        "    index = Path(os.environ['CODEMAP_INDEX_DIR']) / 'project.json'\n"
        "    index.parent.mkdir(parents=True, exist_ok=True)\n"
        "    index.write_text('{}')\n"
        "else:\n"
        "    with Path(os.environ['GATE_QUERY_LOG']).open('a') as stream:\n"
        "        stream.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
        newline="\n",
    )
    provider.chmod(0o700)
    # The oss gate no longer assumes a missing probe means "current": the real provider ships this probe beside itself.
    probe = bin_dir / "check-index-currency"
    probe.write_text(
        "import json\nprint(json.dumps({'status': 'current', 'reason': ''}))\n",
        encoding="utf-8",
        newline="\n",
    )
    config = tmp_path / "config"
    registry = config / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps({"plugins": {"codemap-py@borda-ai-rig": [{"installPath": str(tmp_path.resolve())}]}}),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "TMPDIR": str(tmp_path),
        "CSID": "gate-flow",
        "CLAUDE_CONFIG_DIR": str(config),
        "CODEMAP_INDEX_DIR": str(index_dir),
        "GATE_QUERY_LOG": str(query_log),
    }
    consumer = wire(tmp_path)
    next_consumer = [
        sys.executable,
        "-c",
        "import subprocess, sys\nfrom pathlib import Path\n"
        "if Path(sys.argv[1]).read_text().strip() == 'true':\n"
        "    subprocess.run([sys.executable, sys.argv[2], 'query', 'central'], check=True)\n",
        str(consumer.enabled),
        str(provider),
    ]

    initial = _run(consumer.command, project, env)
    assert initial.returncode == 0, initial.stderr
    assert consumer.enabled.read_text() == "false\n"
    assert consumer.currency.read_text() == "no_index\n"
    subprocess.run(next_consumer, cwd=project, env=env, check=True)
    assert not query_log.exists()

    subprocess.run([sys.executable, str(provider), "index"], cwd=project, env=env, check=True)
    assert (index_dir / "project.json").is_file()
    resolved = _run(consumer.command, project, env)
    assert resolved.returncode == 0, resolved.stderr
    assert consumer.enabled.read_text() == "true\n"
    assert consumer.currency.read_text() == "current\n"
    subprocess.run(next_consumer, cwd=project, env=env, check=True)
    assert [json.loads(line) for line in query_log.read_text().splitlines()] == [["query", "central"]]

    skipped = _run(consumer.skip, project, env)
    assert skipped.returncode == 0, skipped.stderr
    assert consumer.enabled.read_text() == "false\n"
    assert consumer.currency.read_text() == "off\n"
    subprocess.run(next_consumer, cwd=project, env=env, check=True)
    assert len(query_log.read_text().splitlines()) == 1
    assert {path: path.read_text() for path in consumer.preserved} == consumer.preserved


def _flat_state(tmp_path: Path, skill: str) -> tuple[Path, Path]:
    """Return the raw-flag and resolved-enabled sentinels of a skill that keeps them as sibling files."""
    return tmp_path / f"dev-{skill}-codemap-skip", tmp_path / f"dev-{skill}-codemap-enabled-skip"


def _plan_state(tmp_path: Path, skill: str) -> tuple[Path, Path]:
    """Return the raw-flag and resolved-enabled sentinels of ``plan``, which keeps them in a numbered namespace."""
    namespace = tmp_path / "dev-plan-42-skip"
    namespace.mkdir()
    (tmp_path / "dev-plan-ns-current-skip").write_text(str(namespace) + "\n", encoding="utf-8")
    return namespace / "codemap-raw", namespace / "codemap-enabled"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("skill", "state"),
    [
        pytest.param("fix", _flat_state, id="fix"),
        pytest.param("review", _flat_state, id="review"),
        pytest.param("plan", _plan_state, id="plan"),
    ],
)
@pytest.mark.parametrize("mode", ["auto", "strict", "off"])
def test_skip_preserves_original_flag(
    tmp_path: Path, skill: str, state: Callable[[Path, str], tuple[Path, Path]], mode: str
) -> None:
    """Skip records only the resolved decision, retaining the user's original selection.

    The raw sentinel holds ``auto``/``strict``/``off`` as the user chose it; a later re-resolution needs that original
    value, so a skip may flip only the resolved enabled flag and the currency status.
    """
    raw, enabled = state(tmp_path, skill)
    raw.write_text(mode + "\n", encoding="utf-8")
    enabled.write_text("true\n", encoding="utf-8")
    env = {**os.environ, "TMPDIR": str(tmp_path), "CSID": "skip", "SCAN_NO_AUTOBUILD": "1", "PATH": ""}

    result = _run([sys.executable, str(_GATE), skill, "--skip"], None, env)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "false\n"
    assert raw.read_text() == mode + "\n"
    assert enabled.read_text() == "false\n"
    assert (tmp_path / "dev-codemap-currency-skip").read_text() == "off\n"


@pytest.mark.integration
def test_skip_reports_persistence_failure(tmp_path: Path) -> None:
    """A blocked sentinel write cannot report a successfully persisted skip.

    A directory occupies the enabled sentinel's path, so the write fails; the gate must exit 1 with a message instead of
    printing a success verdict for a decision that was never stored.
    """
    enabled = tmp_path / "dev-fix-codemap-enabled-unwritable"
    enabled.mkdir()

    result = _run(
        [sys.executable, str(_GATE), "fix", "--skip"],
        None,
        {**os.environ, "TMPDIR": str(tmp_path), "CSID": "unwritable"},
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "cannot write" in result.stderr
    assert enabled.is_dir()


@pytest.fixture(name="review_flags")
def _review_flags(tmp_path: Path) -> Callable[[str], tuple[Path, Path, subprocess.CompletedProcess[str]]]:
    """Return a runner that seeds a prior resolved ``true`` plus a raw *mode*, re-resolves ``review``, and reports."""

    def run(mode: str) -> tuple[Path, Path, subprocess.CompletedProcess[str]]:
        """Seed raw=*mode*, enabled=``true``, run the gate for ``review`` and return both sentinels and the result."""
        raw = tmp_path / "dev-review-codemap-flags"
        raw.write_text(mode + "\n", encoding="utf-8")
        enabled = tmp_path / "dev-review-codemap-enabled-flags"
        enabled.write_text("true\n", encoding="utf-8")
        env = {**os.environ, "TMPDIR": str(tmp_path), "CSID": "flags", "PATH": ""}
        return raw, enabled, _run([sys.executable, str(_GATE), "review"], None, env)

    return run


@pytest.mark.integration
class TestReviewReresolution:
    """A later tool call re-resolves ``review`` from the original selection, not from a prior resolved ``true``."""

    def test_strict_blocks_without_provider(self, review_flags) -> None:
        """``strict`` with no provider on ``PATH`` exits 1 with a BLOCKED message and keeps the raw selection."""
        raw, _enabled, result = review_flags("strict")

        assert result.returncode == 1, result.stderr
        assert raw.read_text() == "strict\n"
        assert "BLOCKED" in result.stderr

    def test_off_disables_and_keeps_raw_selection(self, review_flags) -> None:
        """``off`` exits 0, flips the stale resolved ``true`` to ``false`` and keeps the raw selection."""
        raw, enabled, result = review_flags("off")

        assert result.returncode == 0, result.stderr
        assert raw.read_text() == "off\n"
        assert enabled.read_text() == "false\n"
