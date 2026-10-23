"""Prove Codemap gate decisions survive separate helper and consumer processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_ROOT = PLUGIN_ROOT.parent


@pytest.mark.integration
@pytest.mark.parametrize("consumer", ["develop-fix", "develop-review", "oss-review", "research-run"])
def test_gate_build_and_skip_reach_next_process(tmp_path: Path, consumer: str) -> None:
    """A successful build enables the next fresh consumer, while explicit skip persists disabled."""
    project = tmp_path / "project"
    project.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    index_dir = tmp_path / "index"
    index = index_dir / "project.json"
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
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "TMPDIR": str(tmp_path),
        "CSID": "gate-flow",
        "CODEMAP_INDEX_DIR": str(index_dir),
        "GATE_QUERY_LOG": str(query_log),
    }
    if consumer.startswith("develop-"):
        skill = consumer.removeprefix("develop-")
        raw = tmp_path / f"dev-{skill}-codemap-gate-flow"
        raw.write_text("auto\n", encoding="utf-8")
        # The legacy review alias also starts raw, but is overwritten by initial detection.
        if skill == "review":
            (tmp_path / "dev-review-codemap-enabled-gate-flow").write_text("auto\n", encoding="utf-8")
        command = [sys.executable, str(PLUGIN_ROOT / "bin/dev_codemap_gate.py"), skill]
        skip = [*command, "--skip"]
        prefix = f"dev-{skill}"
        currency_prefix = "dev"
    elif consumer == "oss-review":
        command = [sys.executable, str(PLUGINS_ROOT / "cc_oss/bin/detect_codemap.py"), "--prefix", "review"]
        skip = [*command, "--force-off"]
        prefix = currency_prefix = "review"
    else:
        command = [sys.executable, str(PLUGINS_ROOT / "cc_research/bin/codemap-flag.py"), "research-run", ""]
        skip = [*command[:-1], "--no-codemap"]
        prefix = "research-run"
        currency_prefix = "research"
    enabled = tmp_path / f"{prefix}-codemap-enabled-gate-flow"
    currency = tmp_path / f"{currency_prefix}-codemap-currency-gate-flow"
    next_consumer = [
        sys.executable,
        "-c",
        "import subprocess, sys\nfrom pathlib import Path\n"
        "if Path(sys.argv[1]).read_text().strip() == 'true':\n"
        "    subprocess.run([sys.executable, sys.argv[2], 'query', 'central'], check=True)\n",
        str(enabled),
        str(provider),
    ]

    initial = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True, check=False)
    assert initial.returncode == 0, initial.stderr
    assert enabled.read_text() == "false\n"
    assert currency.read_text() == "no_index\n"
    subprocess.run(next_consumer, cwd=project, env=env, check=True)
    assert not query_log.exists()

    subprocess.run([sys.executable, str(provider), "index"], cwd=project, env=env, check=True)
    assert index.is_file()
    resolved = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True, check=False)
    assert resolved.returncode == 0, resolved.stderr
    assert enabled.read_text() == "true\n"
    assert currency.read_text() == "current\n"
    subprocess.run(next_consumer, cwd=project, env=env, check=True)
    assert [json.loads(line) for line in query_log.read_text().splitlines()] == [["query", "central"]]

    skipped = subprocess.run(skip, cwd=project, env=env, capture_output=True, text=True, check=False)
    assert skipped.returncode == 0, skipped.stderr
    assert enabled.read_text() == "false\n"
    assert currency.read_text() == "off\n"
    subprocess.run(next_consumer, cwd=project, env=env, check=True)
    assert len(query_log.read_text().splitlines()) == 1
    if consumer.startswith("develop-"):
        assert raw.read_text() == "auto\n"


@pytest.mark.integration
@pytest.mark.parametrize("skill", ["fix", "review", "plan"])
@pytest.mark.parametrize("mode", ["auto", "strict", "off"])
def test_skip_preserves_original_flag(tmp_path: Path, skill: str, mode: str) -> None:
    """Skip records only the resolved decision, retaining the user's original selection."""
    env = {**os.environ, "TMPDIR": str(tmp_path), "CSID": "skip", "SCAN_NO_AUTOBUILD": "1", "PATH": ""}
    if skill == "plan":
        namespace = tmp_path / "dev-plan-42-skip"
        namespace.mkdir()
        (tmp_path / "dev-plan-ns-current-skip").write_text(str(namespace) + "\n", encoding="utf-8")
        raw, enabled = namespace / "codemap-raw", namespace / "codemap-enabled"
    else:
        raw = tmp_path / f"dev-{skill}-codemap-skip"
        enabled = tmp_path / f"dev-{skill}-codemap-enabled-skip"
    raw.write_text(mode + "\n", encoding="utf-8")
    enabled.write_text("true\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "bin/dev_codemap_gate.py"), skill, "--skip"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "false\n"
    assert raw.read_text() == mode + "\n"
    assert enabled.read_text() == "false\n"
    assert (tmp_path / "dev-codemap-currency-skip").read_text() == "off\n"


@pytest.mark.integration
def test_skip_reports_persistence_failure(tmp_path: Path) -> None:
    """A blocked sentinel write cannot report a successfully persisted skip."""
    enabled = tmp_path / "dev-fix-codemap-enabled-unwritable"
    enabled.mkdir()

    result = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "bin/dev_codemap_gate.py"), "fix", "--skip"],
        env={**os.environ, "TMPDIR": str(tmp_path), "CSID": "unwritable"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "cannot write" in result.stderr
    assert enabled.is_dir()


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["strict", "off"])
def test_review_reresolution_retains_strict_and_off_modes(tmp_path: Path, mode: str) -> None:
    """A prior resolved true cannot replace the original strict/off selection on a later tool call."""
    raw = tmp_path / "dev-review-codemap-flags"
    raw.write_text(mode + "\n", encoding="utf-8")
    enabled = tmp_path / "dev-review-codemap-enabled-flags"
    enabled.write_text("true\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "bin/dev_codemap_gate.py"), "review"],
        env={**os.environ, "TMPDIR": str(tmp_path), "CSID": "flags", "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == (1 if mode == "strict" else 0), result.stderr
    assert raw.read_text() == mode + "\n"
    if mode == "strict":
        assert "BLOCKED" in result.stderr
    else:
        assert enabled.read_text() == "false\n"
