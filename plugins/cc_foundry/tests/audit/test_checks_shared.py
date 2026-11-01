"""Execute shared audit snippets against count errors and reciprocal policy links.

The audit's shared check templates embed Bash snippets that the audit runs verbatim. These tests extract a snippet from
``skills/audit/templates/checks-shared.md`` and run it under real Bash, because the contract is how the snippet behaves
in a shell (numeric-test diagnostics, exit status, output lines). They therefore stay real subprocess integration tests
and are skipped on hosts without a POSIX Bash and text utilities.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_CHECKS = Path(__file__).resolve().parents[2] / "skills/audit/templates/checks-shared.md"
_BASH_REQUIRED = pytest.mark.skipif(
    not _bash_runs_posix_script() or any(shutil.which(tool) is None for tool in ("grep", "sed", "tr", "awk")),
    reason="Shared audit snippets require Bash executing POSIX scripts and text utilities",
)
_HEADER = "=== Check 45: Policy-sibling marker symmetry ==="
_ASYMMETRIC = (
    "⚠ 45-ASYMMETRIC: plugins/demo/rules/a.md — declares sibling {sibling}, "
    "but that file has no policy-sibling marker back"
)
_CLEAN = "✓: Check 45 — no unsynced policy-sibling markers found"


@pytest.fixture(scope="module")
def checks_lines() -> list[str]:
    """Lines of the shared checks template, read once for the module."""
    return _CHECKS.read_text(encoding="utf-8").splitlines()


def _first_bash_block(lines: list[str], heading_prefix: str) -> str:
    """Return the first fenced ``bash`` block in the section whose heading starts with ``heading_prefix``."""
    start = next(i for i, line in enumerate(lines) if line.startswith(heading_prefix))
    block: list[str] = []
    inside = False
    for line in lines[start + 1 :]:
        if not inside and line.startswith("## "):
            break
        if line.startswith("```bash"):
            inside = True
        elif inside and line.startswith("```"):
            break
        elif inside:
            block.append(line)
    return "\n".join(block)


@pytest.mark.integration
@_BASH_REQUIRED
class TestExampleSummary:
    """The example-count summary line keeps its numeric-test diagnostic and exit status."""

    @pytest.mark.parametrize(
        ("count", "status", "output"),
        [
            pytest.param("", 2, b"", id="unreadable-count"),
            pytest.param("0", 1, b"", id="no-examples"),
            pytest.param("2", 0, b"sample.md: 2 example blocks, 10 total lines\n", id="valid-count"),
        ],
    )
    def test_preserves_count_status(self, checks_lines: list[str], count: str, status: int, output: bytes) -> None:
        """An empty grep count keeps the numeric-test diagnostic and status; valid counts keep their output.

        The snippet receives the grep count in ``$count``. An empty count must fail the shell's numeric test with status
        2 and a stderr diagnostic rather than pass silently; a zero count exits 1; a valid count prints the summary line
        and exits 0.
        """
        line = next(line for line in checks_lines if "example blocks," in line)
        result = subprocess.run(
            ["bash", "-c", line],
            env={**os.environ, "count": count, "lines": "10", "f": "sample.md"},
            capture_output=True,
            check=False,
        )
        assert result.returncode == status, result.stderr
        assert result.stdout == output
        assert bool(result.stderr) == (count == "")


@pytest.mark.integration
@_BASH_REQUIRED
class TestPolicySiblingSymmetry:
    """Check 45 reports a policy-sibling link whose target does not link back."""

    @pytest.mark.parametrize(
        ("prefix", "b_markers", "expected_line"),
        [
            pytest.param(
                "plugins/demo/rules/",
                "c.md",
                _ASYMMETRIC.format(sibling="plugins/demo/rules/b.md"),
                id="repository-path-reports-missing-back-link",
            ),
            pytest.param(
                "rules/",
                "c.md",
                _ASYMMETRIC.format(sibling="plugins/demo/rules/b.md"),
                id="plugin-path-reports-missing-back-link",
            ),
            pytest.param(
                "./",
                "c.md",
                _ASYMMETRIC.format(sibling="plugins/demo/rules/./b.md"),
                id="directory-path-reports-missing-back-link",
            ),
            pytest.param("plugins/demo/rules/", "c.md, plugins/demo/rules/a.md", _CLEAN, id="repository-path-repaired"),
            pytest.param("rules/", "c.md, rules/a.md", _CLEAN, id="plugin-path-repaired"),
            pytest.param("./", "c.md, ./a.md", _CLEAN, id="directory-path-repaired"),
        ],
    )
    def test_requires_the_declaring_file(
        self, tmp_path: Path, checks_lines: list[str], prefix: str, b_markers: str, expected_line: str
    ) -> None:
        """A→B→C→B reports missing B→A until B links back, whichever path style the markers use.

        File ``a`` declares ``b`` as sibling, ``b`` declares ``c`` and ``c`` declares ``b``, so ``b`` never names ``a``.
        The marker paths come in repository-relative, plugin-relative and ``./`` forms; each must resolve to the same
        file. Once ``b`` also lists ``a`` the graph is reciprocal and the check passes.
        """
        rules = tmp_path / "plugins/demo/rules"
        rules.mkdir(parents=True)
        (rules / "a.md").write_text(f"<!-- policy-sibling: {prefix}b.md -->\n", encoding="utf-8")
        (rules / "b.md").write_text(f"<!-- policy-sibling: {prefix}{b_markers} -->\n", encoding="utf-8")
        (rules / "c.md").write_text(f"<!-- policy-sibling: {prefix}b.md -->\n", encoding="utf-8")
        block = _first_bash_block(checks_lines, "## Check 45 —")

        result = subprocess.run(["bash", "-c", block], cwd=tmp_path, capture_output=True, text=True, check=False)

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert result.stdout.splitlines() == [_HEADER, expected_line]
