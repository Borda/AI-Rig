"""Tests for the ``compaction_boundary`` bin script.

Covers the Boundary-step excerpt (byte-equal to the ``sed`` range it replaces), contract lookup, and the optional
foundry gate against a stubbed provider. The real ``compact_gate.py`` is never run here: it reads session transcripts
and appends to ``~/.claude/logs``. The stub resolver and gate are real child processes because the script's contract is
what it prints and returns when it spawns them; the ``-h`` and ``sed`` tests likewise keep their real subprocess.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import compaction_boundary as cb
import pytest

_BIN = Path(__file__).resolve().parents[3] / "bin"
_CONTRACT = Path(__file__).resolve().parents[3] / "skills" / "_shared" / "compaction-contract.md"
#: The ``sed`` range every consumer block used before the script existed.
_SED_RANGE = "/^## Boundary step$/,/^## Acceptance criteria$/{/^## Acceptance criteria$/!p;}"
_skip_without_sed = pytest.mark.skipif(shutil.which("sed") is None, reason="sed is not installed on this host")
_FALLBACK = "compact-gate: continue (foundry gate unavailable)\n"
_GATE_ADVISE = "print('compact-gate: advise (stub)')\n"
_GATE_FAILS = "import sys\nprint('compact-gate: stop (stub)')\nsys.exit(1)\n"
_CONTRACT_TEXT = "## Intro\nignored\n## Boundary step\n1. go\n## Acceptance criteria\nignored\n"
_EXCERPT = "## Boundary step\n1. go\n"


def _stub_provider(tmp_path: Path, *, gate_body: str | None, resolver_exit: int = 0) -> Path:
    """Write a fake resolver that prints a provider bin dir, and a fake gate inside it unless ``gate_body`` is None."""
    provider_bin = tmp_path / "provider-bin"
    provider_bin.mkdir()
    if gate_body is not None:
        # Child stdout is inherited, so the stub must keep LF output on Windows too.
        gate_body = 'import sys\nsys.stdout.reconfigure(newline="\\n")\n' + gate_body
        (provider_bin / "compact_gate.py").write_text(gate_body, encoding="utf-8")
    resolver = tmp_path / "resolve_shared_path.py"
    resolver.write_text(f"import sys\nprint({str(provider_bin)!r})\nsys.exit({resolver_exit})\n", encoding="utf-8")
    return resolver


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    """Caller-resolved shared directory holding a contract with a Boundary step."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / cb._CONTRACT_NAME).write_text(_CONTRACT_TEXT, encoding="utf-8")
    return shared


@pytest.fixture
def own_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Contract shipped beside this script, with the script's plugin root pointed at its plugin."""
    plugin = tmp_path / "plugin"
    own = plugin / "skills" / "_shared"
    own.mkdir(parents=True)
    (own / cb._CONTRACT_NAME).write_text("own\n", encoding="utf-8")
    monkeypatch.setattr(cb, "_PLUGIN_ROOT", plugin)
    return own / cb._CONTRACT_NAME


class TestBoundaryExcerpt:
    """Selecting the ``## Boundary step`` section out of a contract."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(
                "## A\na\n## Boundary step\n1. go\n## Acceptance criteria\nz\n",
                "## Boundary step\n1. go\n",
                id="stops-before-acceptance-criteria",
            ),
            pytest.param(
                "## Boundary step\n1. go\n## Other\n2. more\n",
                "## Boundary step\n1. go\n## Other\n2. more\n",
                id="runs-to-end-without-closing-heading",
            ),
            pytest.param(
                "## Boundary step\nlast line", "## Boundary step\nlast line", id="keeps-unterminated-last-line"
            ),
            pytest.param("## A\nx\n", "", id="no-section"),
            pytest.param(
                "## Boundary step\na\n## Acceptance criteria\nb\n## Boundary step\nc\n## Acceptance criteria\n",
                "## Boundary step\na\n## Boundary step\nc\n",
                id="repeated-heading-reopens-section",
            ),
            pytest.param(
                "## Boundary step\r\nx\r\n## Acceptance criteria\r\n", "## Boundary step\r\nx\r\n", id="crlf-checkout"
            ),
            pytest.param("### Boundary step\nx\n  ## Boundary step\ny\n", "", id="near-miss-headings-ignored"),
        ],
    )
    def test_selects_section(self, text: str, expected: str) -> None:
        """The section runs from its heading to just before ``## Acceptance criteria``, or to the end.

        These are the cases where a re-implemented range most easily diverges from ``sed``: a missing closing heading,
        an unterminated last line, a repeated opener, and a CRLF checkout.
        """
        assert cb.boundary_excerpt(text) == expected

    @_skip_without_sed
    @pytest.mark.integration
    def test_matches_sed_on_the_shipped_contract(self) -> None:
        """The excerpt of the real contract is byte-equal to what the replaced ``sed`` range printed.

        This is the equivalence proof for the consumer blocks that now call the script instead of ``sed``, so it runs
        the real ``sed`` against the shipped contract.
        """
        expected = subprocess.run(
            ["sed", "-n", _SED_RANGE, str(_CONTRACT)], capture_output=True, text=True, check=True, encoding="utf-8"
        ).stdout
        assert expected.startswith("## Boundary step\n")
        assert cb.boundary_excerpt(_CONTRACT.read_text(encoding="utf-8")) == expected


class TestFindContract:
    """Choosing which contract file to print."""

    def test_prefers_callers_shared_dir(self, shared_dir: Path, own_contract: Path) -> None:
        """A contract in the caller's resolved shared dir wins over this plugin's own copy.

        Both locations hold a contract; the caller's directory is the one the consuming skill resolved, so it is the one
        that must be printed.
        """
        assert cb.find_contract(str(shared_dir)) == shared_dir / cb._CONTRACT_NAME

    @pytest.mark.parametrize(
        "caller_shared",
        [pytest.param("", id="empty-sentinel"), pytest.param("no-such-shared-dir", id="dir-without-contract")],
    )
    def test_falls_back_to_plugins_own_contract(self, caller_shared: str, own_contract: Path) -> None:
        """An empty or stale sentinel still finds the contract shipped beside this script's ``bin/``.

        That fallback is what lets one propagated script serve every plugin without a per-plugin path literal.
        """
        assert cb.find_contract(caller_shared) == own_contract

    def test_returns_none_without_candidate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neither location holding the contract yields None rather than a path that does not exist.

        Callers then print a note and still run the gate, so the lookup must signal absence instead of raising.
        """
        monkeypatch.setattr(cb, "_PLUGIN_ROOT", tmp_path)
        assert cb.find_contract("") is None


class TestMain:
    """The printed boundary: excerpt first, then exactly the gate's verdict or the fallback line."""

    @pytest.mark.parametrize(
        ("resolver_exit", "gate_body", "expected_tail"),
        [
            pytest.param(0, _GATE_ADVISE, "compact-gate: advise (stub)\n", id="gate-decision-passes-through"),
            pytest.param(1, _GATE_ADVISE, _FALLBACK, id="resolver-failure-falls-back"),
            pytest.param(0, None, _FALLBACK, id="provider-without-gate-falls-back"),
            pytest.param(0, _GATE_FAILS, "compact-gate: stop (stub)\n" + _FALLBACK, id="gate-failure-adds-fallback"),
        ],
    )
    def test_prints_excerpt_then_gate_decision(
        self,
        resolver_exit: int,
        gate_body: str | None,
        expected_tail: str,
        shared_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        """The skill sees the Boundary step, then the gate's own line, or ``continue`` when the gate cannot run.

        A gate that exits non-zero keeps its output and is followed by the fallback line, the same as the ``... &&
        python gate || echo fallback`` chain the script replaces.
        """
        monkeypatch.setattr(cb, "_RESOLVER", _stub_provider(tmp_path, gate_body=gate_body, resolver_exit=resolver_exit))
        assert cb.main([str(shared_dir)]) == 0
        assert capfd.readouterr().out == _EXCERPT + expected_tail

    def test_missing_resolver_falls_back(
        self, shared_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """A plugin without the sibling resolver degrades to the fallback line instead of crashing.

        The resolver path does not exist, as in a plugin that ships the script but not its sibling resolver.
        """
        monkeypatch.setattr(cb, "_RESOLVER", tmp_path / "absent.py")
        assert cb.main([str(shared_dir)]) == 0
        assert capfd.readouterr().out == _EXCERPT + _FALLBACK

    def test_missing_contract_still_runs_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """No contract prints a note on stderr and still reaches the gate, so the boundary check is never skipped.

        With no argument and no contract beside the script, only the gate's line may reach stdout.
        """
        monkeypatch.setattr(cb, "_PLUGIN_ROOT", tmp_path)
        monkeypatch.setattr(cb, "_RESOLVER", _stub_provider(tmp_path, gate_body=_GATE_ADVISE))
        assert cb.main([]) == 0
        captured = capfd.readouterr()
        assert captured.out == "compact-gate: advise (stub)\n"
        assert "not found or unreadable" in captured.err


class TestCli:
    """The script run as a real command."""

    def test_help_exits_zero_without_running_gate(self) -> None:
        """``-h`` prints usage and exits 0 before any provider is resolved, honouring the bin/ help contract.

        Run as a real subprocess because the exit code and stdout of the script itself are what the contract names.
        """
        result = subprocess.run(
            [sys.executable, str(_BIN / "compaction_boundary.py"), "-h"], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0
        assert "usage:" in result.stdout
