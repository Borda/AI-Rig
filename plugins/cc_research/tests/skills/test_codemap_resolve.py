"""Regression tests for cc_research's codemap gate wiring.

``bin/codemap_resolve.py`` is byte-identical to cc_develop's canonical copy, so the resolver's own behaviour is pinned
once on the canonical side. What must be pinned *here* is that this plugin's shipped copy really is that file and that
research's own wrapper supplies research's sentinel name — the only per-plugin difference left.

These are re-asserted against the shipped copy rather than trusted transitively: the byte-identity check and these
behavioural checks fail independently, so a bad propagation cannot pass by matching a stale canonical.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from _research_text import load_module

_REPO_ROOT = Path(__file__).resolve().parents[4]
_RESEARCH_BIN = _REPO_ROOT / "plugins" / "cc_research" / "bin"
_RESEARCH_RESOLVER = _RESEARCH_BIN / "codemap_resolve.py"
_GATES = _REPO_ROOT / "plugins" / "cc_research" / "skills" / "_shared" / "codemap-gates.md"


resolver = load_module(_RESEARCH_RESOLVER, "research_codemap_resolve_under_test")


@pytest.fixture(scope="module")
def flag() -> ModuleType:
    """Research's ``codemap-flag.py`` wrapper, loaded once under a unique module name."""
    return load_module(_RESEARCH_BIN / "codemap-flag.py", "research_codemap_flag_under_test")


def _git_repo(tmp_path: Path, name: str) -> Path:
    """Create an initialized git repository named *name* under *tmp_path*."""
    root = tmp_path / name
    (root / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


class TestShippedResolver:
    """The resolver copy this plugin ships and how it locates the codemap index."""

    def test_index_found_from_repository_subdirectory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The index is located at the repository root even when the session sits in a subdirectory.

        Research skills run from wherever the session sits, while the codemap index lives at the git root.
        """
        root = _git_repo(tmp_path, "proj")
        index = root / ".cache" / "codemap" / "proj.json"
        index.parent.mkdir(parents=True)
        index.write_text("{}")
        monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
        monkeypatch.chdir(root / "sub")

        assert resolver._index_path(resolver._canonical_root()) == index.resolve()

    @pytest.mark.parametrize("name", ["café", "my+proj", "two words"])
    def test_project_name_is_raw_basename(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        """The index file name keeps the project's raw basename, unsanitized.

        Sanitizing the name here sought a filename the scanner never writes, so non-ASCII, ``+`` and spaces must
        survive.
        """
        root = _git_repo(tmp_path, name)
        monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
        monkeypatch.chdir(root)

        assert resolver._index_path(resolver._canonical_root()).name == f"{name}.json"

    def test_retired_plugin_label_is_gone(self) -> None:
        """User-facing text carries the ``codemap-py`` label and no retired query label.

        This copy had drifted to the retired bare ``codemap`` label in user-facing messages.
        """
        assert resolver.TOOL_LABEL == "codemap-py"
        assert not hasattr(resolver, "QUERY_LABEL")


class TestResearchWrapper:
    """Research's own ``codemap-flag.py`` wrapper around the shared resolver."""

    @mock.patch("subprocess.run", autospec=True)
    def test_supplies_research_currency_prefix(self, mock_run: mock.MagicMock, flag: ModuleType) -> None:
        """The wrapper passes research's currency-sentinel prefix to the resolver.

        The sentinel name is research's concern and lives in research's own wrapper. The resolver subprocess is mocked
        to a successful boolean answer; only the argv the wrapper builds is under test.
        """
        mock_run.side_effect = lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="true\n")

        flag._run_resolver("auto", "sess")

        argv = mock_run.call_args.args[0]
        assert flag.CURRENCY_PREFIX == "research-codemap-currency"
        assert argv[argv.index("--currency-prefix") + 1] == "research-codemap-currency"
        assert argv[0] == sys.executable

    def test_prefix_matches_sentinel_gates_read_back(self, flag: ModuleType) -> None:
        """The resolver writes and ``codemap-gates.md`` reads the same sentinel name.

        If the two names diverge the stale-index gate never sees what the resolver recorded and goes blind.
        """
        gates = _GATES.read_text(encoding="utf-8")

        assert f"{flag.CURRENCY_PREFIX}-${{CSID}}" in gates
