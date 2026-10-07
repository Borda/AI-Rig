"""Tests for ``bin/parse_deprecate_args.py`` — ``--deprecate`` flag extraction from $ARGUMENTS strings."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import parse_deprecate_args
import pytest
from parse_deprecate_args import format_shell_assignments, main
from parse_deprecate_args import parse_deprecate_args as parse


def _file_symlink_is_available() -> bool:
    """Return whether this host can create and resolve a file symlink."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        link = root / "link"
        target.write_text("target\n", encoding="utf-8")
        try:
            link.symlink_to(target)
        except OSError:
            return False
        return link.is_symlink() and link.read_text(encoding="utf-8") == "target\n"


def _private_file_mode_is_preserved() -> bool:
    """Return whether ``mkstemp`` records its private ``0o600`` mode on this host."""
    with tempfile.TemporaryDirectory() as directory:
        descriptor, filename = tempfile.mkstemp(dir=directory)
        os.close(descriptor)
        return os.stat(filename).st_mode & 0o777 == 0o600


_skip_file_symlink_unavailable = pytest.mark.skipif(
    not _file_symlink_is_available(), reason="file symlink creation is unavailable on this host"
)
_skip_private_mode_unavailable = pytest.mark.skipif(
    not _private_file_mode_is_preserved(), reason="private 0o600 file modes are unavailable on this host"
)


# ---------------------------------------------------------------------------
# parse_deprecate_args() — pure function
# ---------------------------------------------------------------------------


class TestBareDeprecateFlag:
    """Bare ``--deprecate`` enables deprecation with empty decorator value."""

    @pytest.mark.parametrize(
        "arguments",
        [
            pytest.param("--deprecate", id="alone"),
            pytest.param("--dry-run --deprecate --since 1.0", id="with-surrounding-args"),
            pytest.param("--since 1.0 --deprecate", id="at-end"),
            pytest.param("--deprecate --since 1.0", id="at-start"),
        ],
    )
    def test_bare_flag_enables_with_empty_decorator(self, arguments: str) -> None:
        """A bare ``--deprecate`` is recognised wherever it sits in the argument string."""
        assert parse(arguments) == (True, "")


class TestDeprecateValueExtraction:
    """The three quoting styles must all be recognised for ``--deprecate=<value>``."""

    @pytest.mark.parametrize(
        ("arguments", "expected_decorator"),
        [
            pytest.param("--deprecate=@deprecated", "@deprecated", id="unquoted-simple"),
            pytest.param(
                "--deprecate='@deprecated(target=bar)'",
                "@deprecated(target=bar)",
                id="single-quoted-with-parens",
            ),
            pytest.param(
                '--deprecate="@deprecated_class(target=Bar)"',
                "@deprecated_class(target=Bar)",
                id="double-quoted-class-form",
            ),
            pytest.param(
                "--deprecate=@deprecated(target=fn,deprecated_in='1.0')",
                "@deprecated(target=fn,deprecated_in='1.0')",
                id="unquoted-with-embedded-single-quote",
            ),
            pytest.param(
                "--dry-run --deprecate=@mydecorator --since 1.0",
                "@mydecorator",
                id="value-form-with-surrounding-args",
            ),
        ],
    )
    def test_value_extraction(self, arguments: str, expected_decorator: str) -> None:
        deprecate, decorator = parse(arguments)
        assert deprecate is True
        assert decorator == expected_decorator


class TestDeprecationNotRequested:
    """Arguments that do not request deprecation yield DEPRECATE=false with an empty decorator.

    Covers the explicit ``--no-deprecate`` flag (which wins over any ``--deprecate`` form), the total absence of the
    flag, and flags that merely share a prefix with ``--deprecate`` — ``--deprecated`` and ``--deprecate-foo`` are
    distinct tokens and must not trigger an accidental match.
    """

    @pytest.mark.parametrize(
        "arguments",
        [
            pytest.param("--no-deprecate", id="no-deprecate-alone"),
            pytest.param("--deprecate --no-deprecate", id="no-deprecate-overrides-bare-deprecate"),
            pytest.param("--deprecate=@mydecorator --no-deprecate", id="no-deprecate-overrides-deprecate-value"),
            pytest.param("--no-deprecate --since 1.0", id="no-deprecate-at-start"),
            pytest.param("", id="absent-empty-string"),
            pytest.param("   ", id="absent-only-whitespace"),
            pytest.param("--dry-run --since 1.0 --removed-in 2.0", id="absent-unrelated-flags"),
            pytest.param("symbol mypkg::old_fn mypkg::new_fn", id="absent-subcommand-only"),
            pytest.param("--deprecated", id="similar-deprecated-suffix"),
            pytest.param("--deprecate-foo", id="similar-deprecate-dash-suffix"),
        ],
    )
    def test_deprecation_stays_disabled(self, arguments: str) -> None:
        assert parse(arguments) == (False, "")


# ---------------------------------------------------------------------------
# format_shell_assignments() — output formatting (pure helper, not called by main)
# ---------------------------------------------------------------------------


class TestFormatShellAssignments:
    @pytest.mark.parametrize(
        ("deprecate", "decorator", "expected"),
        [
            pytest.param(True, "", "DEPRECATE=true\nDEPRECATE_DECORATOR=''", id="true-with-empty-decorator"),
            pytest.param(False, "", "DEPRECATE=false\nDEPRECATE_DECORATOR=''", id="false-with-empty-decorator"),
            pytest.param(
                True, "@deprecated", "DEPRECATE=true\nDEPRECATE_DECORATOR=@deprecated", id="true-with-simple-decorator"
            ),
            # Single quotes must come back shell-quoted so `eval` rebuilds one token.
            pytest.param(
                True,
                "@deprecated(target=bar)",
                "DEPRECATE=true\nDEPRECATE_DECORATOR='@deprecated(target=bar)'",
                id="true-with-decorator-containing-parens-and-quotes",
            ),
        ],
    )
    def test_assignments_are_formatted_for_eval(self, deprecate: bool, decorator: str, expected: str) -> None:
        """The flag and the shell-quoted decorator are rendered as two ``eval``-ready assignment lines."""
        assert format_shell_assignments(deprecate, decorator) == expected

    def test_true_with_decorator_containing_spaces(self) -> None:
        out = format_shell_assignments(True, "@deprecated(target=bar, in='1.0')")
        # shlex.quote wraps in single quotes and escapes embedded single quotes
        # via '"'"' — the exact form may vary by Python version but eval must round-trip.
        assert out.startswith("DEPRECATE=true\nDEPRECATE_DECORATOR=")


# ---------------------------------------------------------------------------
# main() — CLI entry point: writes to pid-qualified temp files, prints paths
# ---------------------------------------------------------------------------


def _read_outputs(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    """Read the two pid-qualified temp files whose paths main() printed to stdout.

    Both files are newline-terminated so the shell's ``IFS= read -r`` succeeds
    instead of falling through to its ``||`` default; ``read`` consumes that
    delimiter, so the trailing newline is dropped here to compare against the
    value the calling shell would actually see.

    Args:
        capsys: pytest stdout/stderr capture fixture.

    Returns:
        Tuple ``(flag_text, decorator_text)`` — the contents of the flag and
        decorator files respectively, each without its trailing newline.
    """
    flag_line, dec_line = capsys.readouterr().out.splitlines()
    return Path(flag_line).read_text().removesuffix("\n"), Path(dec_line).read_text().removesuffix("\n")


class TestMain:
    """Write raw values to exclusive temporary files and print their paths.

    The ``--arguments=`` form (equals sign, no space) is required so that values starting with ``--`` survive argparse's
    flag-detection. Filenames carry a random ``tempfile.mkstemp`` suffix, so tests read the printed paths rather than
    fixed names.
    """

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param(["--arguments=--deprecate"], ("true", ""), id="bare-deprecate"),
            pytest.param(["--arguments=--deprecate=@mydecorator"], ("true", "@mydecorator"), id="deprecate-value-raw"),
            pytest.param(["--arguments=--no-deprecate"], ("false", ""), id="no-deprecate"),
            pytest.param(["--arguments=--dry-run --since 1.0"], ("false", ""), id="absent-flag"),
            pytest.param(["--arguments="], ("false", ""), id="empty-arguments"),
            pytest.param([], ("false", ""), id="no-argv-defaults-empty"),
            pytest.param(
                ["--arguments", "symbol mypkg::old mypkg::new"], ("false", ""), id="space-separated-arguments-value"
            ),
        ],
    )
    def test_main_writes_flag_and_decorator_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        argv: list[str],
        expected: tuple[str, str],
    ) -> None:
        """``main`` exits 0 and writes the flag and raw decorator values to the temp files it prints.

        A bare ``--deprecate`` gives flag=true with an empty decorator; ``--deprecate=<value>`` gives the raw value (no
        shell quoting); ``--no-deprecate``, an absent flag, empty arguments and omitted ``--arguments`` all give
        flag=false. The space-separated form works for payloads that do not start with ``--``.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        rc = main(argv)
        assert rc == 0
        assert _read_outputs(capsys) == expected

    def test_main_decorator_written_raw_not_shell_quoted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Decorator with parens/spaces is written raw — no shlex.quote wrapping."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        rc = main(["--arguments=--deprecate='@deprecated(target=bar, in=\"1.0\")'"])
        assert rc == 0
        flag, decorator = _read_outputs(capsys)
        assert flag == "true"
        assert "@deprecated(target=bar" in decorator

    def test_main_uses_sys_argv_when_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Read sys.argv when argv=None."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setattr(
            sys,
            "argv",
            ["parse_deprecate_args.py", "--arguments=--deprecate=@fromargv"],
        )
        rc = main()
        assert rc == 0
        assert _read_outputs(capsys) == ("true", "@fromargv")


# ---------------------------------------------------------------------------
# Doctest hookup — keeps doctest examples covered by `pytest`
# ---------------------------------------------------------------------------


def test_doctests_pass() -> None:
    import doctest

    results = doctest.testmod(parse_deprecate_args, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest(s) failed"


# ---------------------------------------------------------------------------
# Residual-critical regressions — symlink-safe writes, TMPDIR fallback no-op
# ---------------------------------------------------------------------------


class TestSentinelSymlinkSafety:
    """Refuse to follow a pre-planted file or symlink.

    Regression coverage for the residual-critical finding that the prior ``Path.write_text()`` calls had no
    ``O_EXCL``/``O_NOFOLLOW`` — a predictable ``-<pid>``-suffixed name in a shared temp dir let a co-located attacker
    pre-plant a symlink and have its write follow through to an arbitrary target.
    """

    @_skip_file_symlink_unavailable
    def test_preplanted_symlink_is_not_followed(self, tmp_path: Path) -> None:
        """A symlink at the exact guessed old-style pid name is never written through."""
        victim = tmp_path / "victim.txt"
        victim.write_text("IMPORTANT ORIGINAL CONTENT\n", encoding="utf-8")
        preplanted = tmp_path / f"codemap-deprecate-flag-{os.getpid()}"
        preplanted.symlink_to(victim)

        flag_path, _ = parse_deprecate_args._write_temp_vars(True, "@deprecated")

        assert flag_path != preplanted
        assert victim.read_text(encoding="utf-8") == "IMPORTANT ORIGINAL CONTENT\n"

    @_skip_private_mode_unavailable
    def test_written_files_are_mode_0600(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both sentinel files are created owner-only readable/writable."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        flag_path, decorator_path = parse_deprecate_args._write_temp_vars(True, "@deprecated")
        assert (os.stat(flag_path).st_mode & 0o777) == 0o600
        assert (os.stat(decorator_path).st_mode & 0o777) == 0o600


class TestSafeTmpdirFallback:
    """Prevent temporary-directory fallback from returning a rejected path.

    Regression coverage for the residual-critical finding that the prior fallback called ``tempfile.gettempdir()``
    directly, which re-reads ``TMPDIR``/``TEMP``/``TMP`` from the environment — silently undoing the
    ownership/absoluteness check above it.
    """

    def test_nonexistent_tmpdir_override_is_not_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rejected (nonexistent) ``TMPDIR`` value is never the function's return value."""
        rejected = "/nonexistent-hostile-dir-for-test-xyz"
        monkeypatch.setenv("TMPDIR", rejected)
        result = parse_deprecate_args._safe_tmpdir()
        assert result != rejected

    def test_environment_is_restored_after_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The caller's TMPDIR/TEMP/TMP environment is unchanged after the fallback runs."""
        import os

        monkeypatch.setenv("TMPDIR", "/nonexistent-hostile-dir-for-test-xyz")
        parse_deprecate_args._safe_tmpdir()
        assert os.environ.get("TMPDIR") == "/nonexistent-hostile-dir-for-test-xyz"
