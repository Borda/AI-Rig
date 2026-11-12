"""Exercise malformed registry values and adversarial scanner inputs without hanging pytest."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import check_cli_flag_drift as drift
import check_routing_links as routing
import pytest

_BIN = Path(__file__).resolve().parents[2] / "bin"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="root-list"),
        pytest.param({"plugins": []}, id="plugins-list"),
        pytest.param({"plugins": {"foundry@borda-ai-rig": "wrong"}}, id="entry-string"),
        pytest.param({"plugins": {"foundry@borda-ai-rig": [None]}}, id="record-null"),
        pytest.param({"plugins": {"foundry@borda-ai-rig": [{"installPath": 7}]}}, id="path-number"),
    ],
)
def test_routing_registry_refuses_malformed_containers(tmp_path: Path, payload: object) -> None:
    """Malformed external registry shapes cannot abort the entire routing audit."""
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(payload), encoding="utf-8")
    assert routing.get_active_install_paths(registry) == {}


def test_routing_registry_refuses_invalid_utf8(tmp_path: Path) -> None:
    """An undecodable registry is unavailable rather than an uncaught decode error."""
    registry = tmp_path / "registry.json"
    registry.write_bytes(b"\xff")
    assert routing.get_active_install_paths(registry) == {}


def test_routing_registry_uses_last_usable_record(tmp_path: Path) -> None:
    """A malformed tail entry cannot shadow a preceding usable active-install path."""
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps({"plugins": {"foundry@borda-ai-rig": [{"installPath": "cache/foundry"}, None]}}),
        encoding="utf-8",
    )
    assert routing.get_active_install_paths(registry) == {"foundry": Path("cache/foundry")}


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(
            "import check_mode_dispatch as m; title='A' + ' '*200000 + 'Z'; "
            "assert m.extract_mode_headers('## Mode: ' + title) == {title}",
            id="mode-title-with-trailing-word",
        ),
        pytest.param(
            "import _codex_composition as m; from types import SimpleNamespace as N; "
            "window=N(attachments={}); state=N(context=N(windows=[window]), "
            "shared=N(_payload_bytes=lambda x:x.encode())); "
            "m._observe_message({'role':'developer','content':'<hook>'*30000},state); "
            "assert window.attachments == {'other':180000}",
            id="unfinished-injection-wrappers",
        ),
        pytest.param(
            "import scan_efficiency_signals as m; from pathlib import Path; "
            "assert m._count_matching({Path('x'): 'find plugins/cache -path '*5000}, "
            "m.EXTRACTION['mode-dispatch pattern']) == 0",
            id="repeated-extraction-prefixes",
        ),
        pytest.param(
            "import consolidate_inventory as m; assert m._scalar('1'*100000 + 'x') is not None",
            id="numeric-looking-plain-scalar",
        ),
        pytest.param(
            "import consolidate_inventory as m; assert m._sibling_markers('<!-- policy-sibling:'*20000) == []",
            id="unfinished-sibling-markers",
        ),
        pytest.param(
            "import consolidate_inventory as m; assert m._CITATION.findall('a'*200000) == []",
            id="unbroken-noncitation-word",
        ),
        pytest.param(
            "import consolidate_inventory as m; text=']('*40000; assert m._drop_link_targets(text) == text",
            id="unclosed-link-targets",
        ),
        pytest.param(
            "import consolidate_inventory as m; assert m._plain_title('_'*200000 + 'A') == 'A'",
            id="unclosed-emphasis-marks",
        ),
    ],
)
def test_scanners_finish_bounded_hostile_input(script: str) -> None:
    """Inputs with repeated candidate starts complete within a generous linear-time allowance."""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(_BIN)},
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("operation", ["iter", "usage"])
def test_cli_flag_scan_skips_recursive_ast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    """The report-only scanner treats AST recursion exhaustion as an unreadable script."""
    script = tmp_path / "plugin" / "bin" / "tool.py"
    script.parent.mkdir(parents=True)
    script.write_text("argparse.ArgumentParser()", encoding="utf-8")

    def exhausted(*args: object, **kwargs: object) -> None:
        """Represent the AST parser's platform-dependent recursion ceiling."""
        raise RecursionError("AST input too deeply nested")

    monkeypatch.setattr(drift, "ast", SimpleNamespace(parse=exhausted, get_docstring=drift.ast.get_docstring))
    result = drift.iter_argparse_script_sources(tmp_path) if operation == "iter" else drift.usage_block_lines("x")
    assert result == []


def test_rtk_scan_finishes_repeated_unclosed_arrays(tmp_path: Path) -> None:
    """Missing array terminators cannot repeatedly rescan the hook's remaining source."""
    hook = tmp_path / "hook.js"
    hook.write_text("RTK_PREFIXES = [" * 20000, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import check_rtk_alignment as m, sys; from pathlib import Path; assert m.hook_prefixes(Path(sys.argv[1])) == []",
            str(hook),
        ],
        env={**os.environ, "PYTHONPATH": str(_BIN)},
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0, result.stderr
