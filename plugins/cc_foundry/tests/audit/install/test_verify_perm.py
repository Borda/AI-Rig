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


@pytest.fixture(name="paired_files")
def _paired_files(tmp_path: Path):
    """Return a factory writing a settings file and a guide file side by side and returning both paths."""

    def _make(allow: list[str], guide_rules: list[str]) -> tuple[Path, Path]:
        """Write the pair for one CLI assertion."""
        settings, guide = tmp_path / "settings.json", tmp_path / "guide.md"
        _write_settings(settings, allow)
        _write_guide(guide, guide_rules)
        return settings, guide

    return _make


class TestRuleInSettings:
    """rule_in_settings: JSON parsing + allow-list membership."""

    @pytest.mark.parametrize(
        ("rule", "present"),
        [
            pytest.param("Bash(ls:*)", True, id="in-allow-list"),
            pytest.param("Bash(rm:*)", False, id="not-in-allow-list"),
        ],
    )
    def test_reports_allow_list_membership(self, tmp_path: Path, rule: str, present: bool) -> None:
        """A rule is reported present exactly when it appears in ``permissions.allow``.

        The allow list holds ``ls`` and ``pwd``; ``ls`` is a member and ``rm`` is not.
        """
        p = tmp_path / "settings.json"
        _write_settings(p, ["Bash(ls:*)", "Bash(pwd:*)"])
        assert verify_perm.rule_in_settings(rule, p) is present

    def test_missing_file_is_absent(self, tmp_path: Path) -> None:
        """Missing settings.json → False (no error).

        A project without a settings file has simply allowed nothing, so the check answers "absent" without raising.
        """
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

    @pytest.mark.parametrize(
        ("rule", "present"),
        [pytest.param("Bash(ls:*)", True, id="in-table-row"), pytest.param("Bash(rm:*)", False, id="not-in-guide")],
    )
    def test_reports_backticked_rule_presence(self, tmp_path: Path, rule: str, present: bool) -> None:
        """A rule is reported present exactly when the guide lists it wrapped in backticks.

        The guide holds ``ls`` and ``pwd`` table rows; ``ls`` is documented and ``rm`` is not.
        """
        p = tmp_path / "guide.md"
        _write_guide(p, ["Bash(ls:*)", "Bash(pwd:*)"])
        assert verify_perm.rule_in_guide(rule, p) is present

    def test_missing_file_is_absent(self, tmp_path: Path) -> None:
        """Missing guide → False (no error).

        A plugin without a permissions guide has documented nothing, so the check answers "absent" without raising.
        """
        assert verify_perm.rule_in_guide("Bash(ls:*)", tmp_path / "nope.md") is False

    def test_unbackticked_match_is_not_a_match(self, tmp_path: Path) -> None:
        """Plain rule text without backticks → False (matches bash ``grep -qF "\\`rule\\`"``).

        Prose that merely mentions the rule is not documentation of it; only the backticked table cell counts.
        """
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
    def test_maps_presence_and_mode_to_status(self, present: bool, mode: str, expected: str) -> None:
        """Every (presence, mode) combination produces the documented status.

        ``present`` mode wants the rule found and ``absent`` mode wants it gone, so each mode inverts the other's
        verdict.
        """
        assert verify_perm.status_for(present, mode) == expected  # type: ignore[arg-type]


class TestMain:
    """Main: CLI — stdout format + exit codes across modes."""

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
        paired_files,
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
        s, g = paired_files(allow, guide_rules)
        rc = verify_perm.main([rule, str(s), str(g), mode])
        out = capsys.readouterr().out
        assert rc == expected_rc
        assert f"settings: {settings_status}" in out
        assert f"guide: {guide_status}" in out

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["rule", "s.json", "g.md", "bogus"], id="invalid-mode"),
            pytest.param(["rule"], id="missing-args"),
        ],
    )
    def test_bad_arguments_exit_2(self, argv: list[str]) -> None:
        """An invalid mode token or too few arguments exits 2 (argparse usage error).

        Exit 2 tells a caller the invocation itself was wrong, which must stay distinct from exit 1 (a rule is missing
        or lingering).
        """
        with pytest.raises(SystemExit) as exc:
            verify_perm.main(argv)
        assert exc.value.code == 2

    def test_output_is_exactly_two_lines(self, paired_files, capsys: pytest.CaptureFixture[str]) -> None:
        """Two lines exactly: ``settings: <tok>\\nguide: <tok>\\n``.

        Callers parse the report by line, so any extra output line would break them.
        """
        rule = "Bash(ls:*)"
        s, g = paired_files([rule], [rule])
        rc = verify_perm.main([rule, str(s), str(g), "present"])
        assert rc == 0
        assert capsys.readouterr().out.splitlines() == ["settings: OK", "guide: OK"]
