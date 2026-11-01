"""Tests for ``bin/parse_rename_args.py`` — the single ``rename-refs`` ``$ARGUMENTS`` parser."""

from __future__ import annotations

from pathlib import Path

import pytest
from parse_rename_args import RenameArgsError, main, parse_rename_args


class TestParseRenameArgs:
    """Turn the ``rename-refs`` argument blob into typed fields or a user-facing rejection."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param(
                "symbol old_fn new_fn",
                {
                    "subcommand": "symbol",
                    "old_ref": "old_fn",
                    "new_ref": "new_fn",
                    "dry_run": False,
                    "deprecate": False,
                },
                id="minimal-symbol",
            ),
            pytest.param(
                "module pkg.old pkg.new --dry-run",
                {"subcommand": "module", "old_ref": "pkg.old", "new_ref": "pkg.new", "dry_run": True},
                id="module-dry-run",
            ),
            pytest.param("symbol Old New --deprecate", {"deprecate": True, "decorator": ""}, id="bare-deprecate"),
            pytest.param(
                "symbol Old New --deprecate=\"@deprecated_class(target=New, deprecated_in='1.0')\"",
                {"deprecate": True, "decorator": "@deprecated_class(target=New, deprecated_in='1.0')"},
                id="double-quoted-decorator-with-spaces",
            ),
            pytest.param(
                "symbol Old New --deprecate=@deprecated --no-deprecate",
                {"deprecate": False, "decorator": ""},
                id="no-deprecate-wins",
            ),
            pytest.param(
                "symbol a b --since 1.2 --removed-in=2.0",
                {"since": "1.2", "removed_in": "2.0"},
                id="value-flags-space-and-equals",
            ),
            pytest.param(
                "symbol a b --no-deprecate --remove-if-no-callers",
                {"remove_if_zero": True, "deprecate": False},
                id="negated-deprecate-is-no-conflict",
            ),
            pytest.param(
                "symbol a b --deprecate=\"@deprecated(note='--dry-run --remove-if-no-callers')\"",
                {
                    "dry_run": False,
                    "remove_if_zero": False,
                    "decorator": "@deprecated(note='--dry-run --remove-if-no-callers')",
                },
                id="flag-text-inside-a-value-is-not-a-flag",
            ),
        ],
    )
    def test_accepts_valid_arguments(self, arguments: str, expected: dict[str, object]) -> None:
        """Every accepted spelling yields the documented field values.

        Covers bare and valued flags (space and ``=`` forms), the ``--no-deprecate`` override, and quoted values that
        merely contain flag text — the old grep substring test fired on those.
        """
        parsed = parse_rename_args(arguments)

        assert {key: getattr(parsed, key) for key in expected} == expected

    @pytest.mark.parametrize(
        ("arguments", "message"),
        [
            pytest.param("", "Usage: /codemap-py:rename-refs symbol", id="empty"),
            pytest.param("rename a b", "Usage: /codemap-py:rename-refs symbol", id="unknown-subcommand"),
            pytest.param("symbol a", "old or new ref missing", id="missing-new-ref"),
            pytest.param(
                "symbol --dry-run a b", "looks like a flag, not a target — check argument order", id="flag-first"
            ),
            pytest.param("symbol pkg::f g", "module::symbol form", id="module-qualified"),
            pytest.param("module pkg.old ../evil", "is not a dotted Python name", id="path-traversal-target"),
            pytest.param("module pkg.old pkg.class", "is not a dotted Python name", id="keyword-component"),
            pytest.param("symbol a b --since", "--since requires a value", id="since-without-value"),
            pytest.param("symbol a b --since --dry-run", "--since requires a value", id="since-followed-by-flag"),
            pytest.param("symbol a b --bogus", "! Unknown flag(s): --bogus. Supported flags:", id="unknown-flag"),
            pytest.param("module a b --deprecate", "symbol-only", id="symbol-flag-under-module"),
            pytest.param("symbol a b --deprecate=@x(a, b)", "Unexpected argument", id="unquoted-decorator-with-space"),
            pytest.param("symbol a b --deprecate='@x", "Could not parse arguments", id="unbalanced-quote"),
            pytest.param(
                "symbol a b --deprecate --remove-if-no-callers",
                "⚠ conflicting flags: --deprecate creates alias, --remove-if-no-callers deletes target",
                id="deprecate-and-delete-conflict",
            ),
        ],
    )
    def test_rejects_invalid_arguments(self, arguments: str, message: str) -> None:
        """Rejections carry the exact user-facing wording the skill prints unchanged.

        Each malformed blob (missing refs, unsafe targets, flag misuse, quoting errors) raises ``RenameArgsError`` whose
        text is shown to the user verbatim, so the wording is part of the contract.
        """
        with pytest.raises(RenameArgsError) as excinfo:
            parse_rename_args(arguments)

        assert message in str(excinfo.value)


class TestMain:
    """Exit codes and sentinel files of the ``parse_rename_args.py`` entry point."""

    @pytest.fixture(autouse=True)
    def _sentinel_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Point sentinel writes at a private temp dir with a fixed session token."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "sess1")

    def test_writes_every_sentinel(self, tmp_path: Path) -> None:
        """Exit 0 writes the eleven sentinels later steps read back with ``IFS= read -r``.

        A symbol rename with ``--dry-run`` and ``--since`` exercises string, boolean and value sentinels at once.
        """
        rc = main(["symbol Cls.old_m Cls.new_m --dry-run --since 1.0", "--output-prefix", "codemap-proj"])

        def sentinel(name: str) -> str:
            """Read one sentinel written for this session."""
            return (tmp_path / f"codemap-proj-rename-{name}-sess1").read_text(encoding="utf-8")

        assert rc == 0
        assert sentinel("SUBCOMMAND") == "symbol\n"
        assert sentinel("OLD_NAME") == "old_m\n"
        assert sentinel("NEW_NAME") == "new_m\n"
        assert sentinel("DRY_RUN") == "true\n"
        assert sentinel("SINCE_VER") == "1.0\n"
        assert sentinel("DEPRECATE") == "false\n"
        assert len(list(tmp_path.glob("codemap-proj-rename-*-sess1"))) == 11

    def test_rejection_writes_nothing(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A rejected blob exits 1 with the message on stderr and leaves no sentinel behind.

        Stale sentinels from a failed parse would let later skill steps act on an earlier invocation's values.
        """
        rc = main(["symbol a", "--output-prefix", "codemap-proj"])

        assert rc == 1
        assert "old or new ref missing" in capsys.readouterr().err
        assert list(tmp_path.iterdir()) == []

    def test_rejects_unsafe_output_prefix(self) -> None:
        """A prefix with a path separator would escape the temp dir, so it exits 3."""
        assert main(["symbol a b", "--output-prefix", "../x"]) == 3
