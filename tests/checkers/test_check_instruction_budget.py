"""Contract: the ``check-instruction-byte-budget`` hook keeps always-loaded instruction text within its pinned budgets.

Every byte of an always-loaded rule, global template or skill file is re-read on each turn or invocation, so growth must
be a deliberate, justified budget raise. The hook fails on an oversized file, an oversized eager rule set, and a budget
whose target moved or resolved to nothing.
"""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "check_instruction_budget.py"
_checker = runpy.run_path(str(_SCRIPT))
Budget = _checker["Budget"]
check = _checker["check"]
is_eager = _checker["is_eager"]
MIN_RULE_FILES = _checker["MIN_RULE_FILES"]

_EAGER = "---\ndescription: x\n---\n"
_LAZY = "---\ndescription: x\npaths:\n  - 'docs/**'\n---\n"


def _rules_dir(root: Path, bodies: dict[str, str]) -> None:
    """Write one rule file per ``name -> text`` under ``root/rules``."""
    rules = root / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    for name, text in bodies.items():
        (rules / name).write_text(text, encoding="utf-8", newline="\n")


def _padded_rules(count: int, text: str = _EAGER) -> dict[str, str]:
    """Return ``count`` rule files of identical text."""
    return {f"rule-{index}.md": text for index in range(count)}


class TestFileBudget:
    """A ``file`` budget bounds one file's byte size."""

    @pytest.mark.parametrize(
        ("size", "limit", "expected"),
        [
            pytest.param(10, 10, 0, id="at-limit"),
            pytest.param(5, 10, 0, id="under-limit"),
            pytest.param(11, 10, 1, id="one-byte-over"),
        ],
    )
    def test_limit_is_inclusive(self, tmp_path: Path, size: int, limit: int, expected: int) -> None:
        """A file passes up to and including its limit and fails one byte past it.

        The size is counted in bytes on disk, not characters.
        """
        (tmp_path / "t.md").write_bytes(b"x" * size)

        findings = check(tmp_path, (Budget("t.md", limit),))

        assert len(findings) == expected, findings

    def test_over_budget_message_names_file_sizes_and_fix(self, tmp_path: Path) -> None:
        """The violation names the path, the real size, the limit and what to do about it."""
        (tmp_path / "t.md").write_bytes(b"x" * 20)

        findings = check(tmp_path, (Budget("t.md", 10),))

        assert findings[0].startswith("t.md: 20 bytes, over the 10-byte budget"), findings
        assert "raise the limit" in findings[0]

    def test_multibyte_text_counts_bytes_not_characters(self, tmp_path: Path) -> None:
        """Five two-byte characters weigh ten bytes, so a nine-byte limit rejects them."""
        (tmp_path / "t.md").write_text("é" * 5, encoding="utf-8")

        assert len(check(tmp_path, (Budget("t.md", 9),))) == 1

    def test_missing_file_is_a_violation(self, tmp_path: Path) -> None:
        """A budgeted file that was renamed or deleted fails instead of silently passing."""
        findings = check(tmp_path, (Budget("gone.md", 10),))

        assert findings == ["gone.md: budgeted file not found; update BUDGETS in scripts/check_instruction_budget.py"]


class TestEagerRulesBudget:
    """An ``eager-rules`` budget bounds the summed size of rules that load at session start."""

    def test_eager_total_over_limit_fails_with_largest_files(self, tmp_path: Path) -> None:
        """Eager rules summing past the limit fail and name the biggest contributors."""
        _rules_dir(tmp_path, _padded_rules(MIN_RULE_FILES))
        total = sum(path.stat().st_size for path in (tmp_path / "rules").glob("*.md"))

        findings = check(tmp_path, (Budget("rules", total - 1, kind="eager-rules"),))

        assert len(findings) == 1, findings
        assert f"always-loaded rules total {total} bytes" in findings[0]
        assert "largest: rule-" in findings[0]

    def test_eager_total_at_limit_passes(self, tmp_path: Path) -> None:
        """Eager rules summing exactly to the limit pass."""
        _rules_dir(tmp_path, _padded_rules(MIN_RULE_FILES))
        total = sum(path.stat().st_size for path in (tmp_path / "rules").glob("*.md"))

        assert check(tmp_path, (Budget("rules", total, kind="eager-rules"),)) == []

    def test_rules_behind_a_narrow_paths_glob_do_not_count(self, tmp_path: Path) -> None:
        """A rule that loads only on a matching file touch adds nothing to the eager total."""
        bodies = _padded_rules(MIN_RULE_FILES) | {"big.md": _LAZY + "x" * 5000}
        _rules_dir(tmp_path, bodies)
        eager_total = MIN_RULE_FILES * len(_EAGER)

        assert check(tmp_path, (Budget("rules", eager_total, kind="eager-rules"),)) == []

    def test_too_few_rules_means_wrong_directory(self, tmp_path: Path) -> None:
        """Fewer than the minimum rule files fails, so a mis-resolved path cannot pass vacuously."""
        _rules_dir(tmp_path, _padded_rules(MIN_RULE_FILES - 1))

        findings = check(tmp_path, (Budget("rules", 10**9, kind="eager-rules"),))

        assert len(findings) == 1
        assert f"only {MIN_RULE_FILES - 1} rule files found" in findings[0]

    def test_missing_directory_fails_as_too_few_rules(self, tmp_path: Path) -> None:
        """A rules directory that does not exist reports zero rule files rather than raising."""
        findings = check(tmp_path, (Budget("rules", 10**9, kind="eager-rules"),))

        assert "only 0 rule files found" in findings[0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("no frontmatter", True, id="no-frontmatter"),
        pytest.param("---\ndescription: x\n---\n", True, id="no-paths-key"),
        pytest.param("---\npaths:\n  - '**'\n---\n", True, id="block-list-match-all"),
        pytest.param("---\npaths: ['**/*']\n---\n", True, id="inline-list-match-all"),
        pytest.param("---\npaths:\n  - 'docs/**'\n  - '**'\n---\n", True, id="match-all-among-narrow"),
        pytest.param("---\npaths:\n  - '**/*.md'\n---\n", False, id="narrow-block-list"),
        pytest.param("---\npaths: ['docs/**', 'src/**']\n---\n", False, id="narrow-inline-list"),
    ],
)
def test_is_eager_follows_paths_frontmatter(text: str, expected: bool) -> None:
    """A rule is eager when it has no ``paths`` or any glob that matches every file."""
    assert is_eager(text) is expected


def test_cli_exits_one_and_prints_violation_for_missing_targets(tmp_path: Path) -> None:
    """Run as the hook does against a tree holding none of the budgeted files: exit 1, one line per budget."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(tmp_path)], capture_output=True, text=True, check=False, timeout=30
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "plugins/cc_foundry/rules: only 0 rule files found" in result.stdout
    assert "plugins/codex-rig/assets/AGENTS.md: budgeted file not found" in result.stdout
