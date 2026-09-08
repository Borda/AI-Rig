"""Tests for ``bin/verify_perm.py``.

Pure ``status_for`` covered by doctest in source; this file exercises file-bound behaviour using ``tmp_path`` and CLI
surface via ``capsys``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


import verify_perm  # noqa: E402


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

    def test_malformed_json(self, tmp_path: Path) -> None:
        """Malformed JSON → False (no error)."""
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        assert verify_perm.rule_in_settings("Bash(ls:*)", p) is False

    def test_missing_permissions_key(self, tmp_path: Path) -> None:
        """Reject settings that omit the permissions mapping."""
        p = tmp_path / "s.json"
        p.write_text("{}", encoding="utf-8")
        assert verify_perm.rule_in_settings("Bash(ls:*)", p) is False

    def test_allow_not_a_list(self, tmp_path: Path) -> None:
        """Reject a permissions allow value that is not a list."""
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"permissions": {"allow": "not-a-list"}}), encoding="utf-8")
        assert verify_perm.rule_in_settings("Bash(ls:*)", p) is False

    def test_top_level_not_object(self, tmp_path: Path) -> None:
        """Top-level JSON is a list, not an object → False."""
        p = tmp_path / "s.json"
        p.write_text("[]", encoding="utf-8")
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

    def test_present_mode_both_ok(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Present mode + rule in both → "OK / OK", exit 0."""
        rule = "Bash(ls:*)"
        s, g = self._setup(tmp_path, [rule], [rule])
        rc = verify_perm.main([rule, str(s), str(g), "present"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "settings: OK" in out
        assert "guide: OK" in out

    def test_present_mode_settings_missing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Present mode + missing from settings → "MISSING / OK", exit 1."""
        rule = "Bash(ls:*)"
        s, g = self._setup(tmp_path, [], [rule])
        rc = verify_perm.main([rule, str(s), str(g), "present"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "settings: MISSING" in out
        assert "guide: OK" in out

    def test_present_mode_guide_missing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Present mode + missing from guide → "OK / MISSING", exit 1."""
        rule = "Bash(ls:*)"
        s, g = self._setup(tmp_path, [rule], [])
        rc = verify_perm.main([rule, str(s), str(g), "present"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "settings: OK" in out
        assert "guide: MISSING" in out

    def test_absent_mode_both_clean(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Absent mode + rule absent from both → "OK / OK", exit 0."""
        rule = "Bash(rm:*)"
        s, g = self._setup(tmp_path, ["Bash(ls:*)"], ["Bash(ls:*)"])
        rc = verify_perm.main([rule, str(s), str(g), "absent"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "settings: OK" in out
        assert "guide: OK" in out

    def test_absent_mode_still_present_in_settings(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Absent mode + lingering in settings → "STILL_PRESENT / OK", exit 1."""
        rule = "Bash(ls:*)"
        s, g = self._setup(tmp_path, [rule], [])
        rc = verify_perm.main([rule, str(s), str(g), "absent"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "settings: STILL_PRESENT" in out
        assert "guide: OK" in out

    def test_absent_mode_still_present_in_guide(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Absent mode + lingering in guide → "OK / STILL_PRESENT", exit 1."""
        rule = "Bash(ls:*)"
        s, g = self._setup(tmp_path, [], [rule])
        rc = verify_perm.main([rule, str(s), str(g), "absent"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "settings: OK" in out
        assert "guide: STILL_PRESENT" in out

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
