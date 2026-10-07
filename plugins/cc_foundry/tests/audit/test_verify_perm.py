"""Tests for ``bin/verify_perm.py``.

Pure ``status_for`` covered by doctest in source; this file exercises file-bound behaviour using ``tmp_path`` and CLI
surface via ``capsys``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import verify_perm


def _write_settings(path: Path, allow: list[str] | None) -> None:
    """Write a minimal ``settings.json`` with the given allow list.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     path = Path(directory) / "settings.json"
        ...     _write_settings(path, ["Bash(ls:*)"])
        ...     json.loads(path.read_text())["permissions"]["allow"]
        ['Bash(ls:*)']
    """
    payload: dict = {}
    if allow is not None:
        payload["permissions"] = {"allow": allow}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_guide(path: Path, rules: list[str]) -> None:
    """Write a minimal markdown guide with each rule on its own backticked line.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     path = Path(directory) / "guide.md"
        ...     _write_guide(path, ["Bash(ls:*)"])
        ...     "`Bash(ls:*)`" in path.read_text()
        True
    """
    body = "\n".join(f"| `{r}` | desc | use |" for r in rules) + "\n"
    path.write_text(body, encoding="utf-8")


class TestRuleInSettings:
    """rule_in_settings: JSON parsing + allow-list membership."""

    def test_present(self, tmp_path: Path) -> None:
        """Rule in allow list → True."""
        p = tmp_path / "settings.json"
        _write_settings(p, ["Bash(ls:*)", "Bash(pwd:*)"])
        assert verify_perm.rule_in_settings("Bash(ls:*)", p) is True

    def test_absent(self, tmp_path: Path) -> None:
        """Rule not in allow list → False."""
        p = tmp_path / "settings.json"
        _write_settings(p, ["Bash(ls:*)"])
        assert verify_perm.rule_in_settings("Bash(rm:*)", p) is False

    def test_missing_file(self, tmp_path: Path) -> None:
        """Missing settings.json → False (no error)."""
        assert verify_perm.rule_in_settings("Bash(ls:*)", tmp_path / "nope.json") is False

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("{not json", id="malformed-json"),
            pytest.param("{}", id="missing-permissions-key"),
            pytest.param(json.dumps({"permissions": {"allow": "not-a-list"}}), id="allow-not-a-list"),
            pytest.param("[]", id="top-level-not-an-object"),
        ],
    )
    def test_unusable_settings_content_is_not_a_match(self, tmp_path: Path, content: str) -> None:
        """Settings that cannot hold an allow list report the rule as absent, never an error.

        Covers malformed JSON, a document without the permissions mapping, an allow value that is not a list, and a top-
        level JSON value that is a list rather than an object.
        """
        p = tmp_path / "s.json"
        p.write_text(content, encoding="utf-8")
        assert verify_perm.rule_in_settings("Bash(ls:*)", p) is False


class TestRuleInGuide:
    """rule_in_guide: literal backticked substring search."""

    def test_present_in_table_row(self, tmp_path: Path) -> None:
        """Rule wrapped in backticks → True."""
        p = tmp_path / "guide.md"
        _write_guide(p, ["Bash(ls:*)", "Bash(pwd:*)"])
        assert verify_perm.rule_in_guide("Bash(ls:*)", p) is True

    def test_absent(self, tmp_path: Path) -> None:
        """Rule not in guide → False."""
        p = tmp_path / "guide.md"
        _write_guide(p, ["Bash(ls:*)"])
        assert verify_perm.rule_in_guide("Bash(rm:*)", p) is False

    def test_missing_file(self, tmp_path: Path) -> None:
        """Missing guide → False (no error)."""
        assert verify_perm.rule_in_guide("Bash(ls:*)", tmp_path / "nope.md") is False

    def test_unbackticked_match_is_not_a_match(self, tmp_path: Path) -> None:
        """Plain rule text without backticks → False (matches bash ``grep -qF "\\`rule\\`"``)."""
        p = tmp_path / "guide.md"
        p.write_text("Bash(ls:*) is documented here.\n", encoding="utf-8")
        assert verify_perm.rule_in_guide("Bash(ls:*)", p) is False


class TestStatusFor:
    """status_for: presence + mode → status token (also covered by doctest)."""

    @pytest.mark.parametrize(
        ("present", "mode", "expected"),
        [
            pytest.param(True, "present", "OK", id="true-present"),
            pytest.param(False, "present", "MISSING", id="false-present"),
            pytest.param(True, "absent", "STILL_PRESENT", id="true-absent"),
            pytest.param(False, "absent", "OK", id="false-absent"),
        ],
    )
    def test_matrix(self, present: bool, mode: str, expected: str) -> None:
        """Every (presence, mode) combination produces the documented status."""
        assert verify_perm.status_for(present, mode) == expected  # type: ignore[arg-type]


class TestMain:
    """Main: CLI — stdout format + exit codes across modes."""

    def _setup(self, tmp_path: Path, allow: list[str], guide_rules: list[str]) -> tuple[Path, Path]:
        """Create paired settings and guide fixtures for one CLI assertion."""
        s = tmp_path / "settings.json"
        g = tmp_path / "guide.md"
        _write_settings(s, allow)
        _write_guide(g, guide_rules)
        return s, g

    @pytest.mark.parametrize(
        ("rule", "allow", "guide_rules", "mode", "expected_rc", "settings_status", "guide_status"),
        [
            pytest.param("Bash(ls:*)", ["Bash(ls:*)"], ["Bash(ls:*)"], "present", 0, "OK", "OK", id="present-both-ok"),
            pytest.param(
                "Bash(ls:*)", [], ["Bash(ls:*)"], "present", 1, "MISSING", "OK", id="present-settings-missing"
            ),
            pytest.param("Bash(ls:*)", ["Bash(ls:*)"], [], "present", 1, "OK", "MISSING", id="present-guide-missing"),
            pytest.param("Bash(rm:*)", ["Bash(ls:*)"], ["Bash(ls:*)"], "absent", 0, "OK", "OK", id="absent-both-clean"),
            pytest.param(
                "Bash(ls:*)", ["Bash(ls:*)"], [], "absent", 1, "STILL_PRESENT", "OK", id="absent-lingering-in-settings"
            ),
            pytest.param(
                "Bash(ls:*)", [], ["Bash(ls:*)"], "absent", 1, "OK", "STILL_PRESENT", id="absent-lingering-in-guide"
            ),
        ],
    )
    def test_status_lines_and_exit_code_per_mode(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        rule: str,
        allow: list[str],
        guide_rules: list[str],
        mode: str,
        expected_rc: int,
        settings_status: str,
        guide_status: str,
    ) -> None:
        """Each (mode, settings presence, guide presence) combination prints its status tokens and exit code.

        Present mode expects the rule in both files, absent mode expects it in neither: a rule missing from one file
        reports ``MISSING`` and a lingering rule reports ``STILL_PRESENT``, both with exit 1, while a satisfied mode
        reports ``OK`` for both files with exit 0.
        """
        s, g = self._setup(tmp_path, allow, guide_rules)
        rc = verify_perm.main([rule, str(s), str(g), mode])
        out = capsys.readouterr().out
        assert rc == expected_rc
        assert f"settings: {settings_status}" in out
        assert f"guide: {guide_status}" in out

    def test_invalid_mode_exits_2(self) -> None:
        """Invalid mode token → exit 2 (argparse choices)."""
        with pytest.raises(SystemExit) as exc:
            verify_perm.main(["rule", "s.json", "g.md", "bogus"])
        assert exc.value.code == 2

    def test_missing_args_exits_2(self) -> None:
        """Too few args → exit 2 (argparse usage)."""
        with pytest.raises(SystemExit) as exc:
            verify_perm.main(["rule"])
        assert exc.value.code == 2

    def test_output_format_exact(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Two lines exactly: ``settings: <tok>\\nguide: <tok>\\n``."""
        rule = "Bash(ls:*)"
        s, g = self._setup(tmp_path, [rule], [rule])
        rc = verify_perm.main([rule, str(s), str(g), "present"])
        assert rc == 0
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert len(lines) == 2
        assert lines[0] == "settings: OK"
        assert lines[1] == "guide: OK"
