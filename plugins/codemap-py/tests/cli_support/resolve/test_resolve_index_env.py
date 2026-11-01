"""Tests for ``bin/resolve_index_env.py`` — PROJ + INDEX temp-file writer.

The script calls ``resolve_proj_index.py`` via subprocess, reads PROJ (line 1)
and INDEX (line 2), and writes each to ``${TMPDIR}/codemap-resolve-{proj,index}-${CSID}``
for the caller to read back with ``cat``.

Tests cover:
* Happy path — resolver returns valid PROJ + INDEX → temp files written, exit 0
* ``--check-exists`` with present INDEX file → exit 0, temp files written
* ``--check-exists`` with missing INDEX file → exit 1, temp files still written
* Resolver failure (empty output) → exit 1, temp files written (empty)
* Unknown flag → exit 2 with stderr message
* Newline contract — written files end with the delimiter ``IFS= read -r`` needs

The resolver subprocess is replaced by a mock everywhere except the planted-resolver security test, which must run the
real ``_run_resolver``. ``CSID`` / ``CLAUDE_CODE_SESSION_ID`` are cleared by the suite-wide ``conftest`` fixture, so the
session suffix of written files is always ``shared``.
"""

from __future__ import annotations

import doctest
import importlib.util
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from _host_skips import skip_file_symlink_unavailable

_SCRIPT = Path(__file__).resolve().parents[3] / "bin" / "resolve_index_env.py"
_spec = importlib.util.spec_from_file_location("codemap_resolve_index_env", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

format_eval_line = _mod.format_eval_line
parse_resolver_output = _mod.parse_resolver_output
main = _mod.main
_own_plugin_root = _mod._own_plugin_root
_validate_plugin_root = _mod._validate_plugin_root
_validate_output_prefix = _mod._validate_output_prefix
_write_sentinel_file = _mod._write_sentinel_file


def _private_file_mode_is_preserved() -> bool:
    """Return whether ``mkstemp`` records its private ``0o600`` mode on this host."""
    with tempfile.TemporaryDirectory() as directory:
        descriptor, filename = tempfile.mkstemp(dir=directory)
        os.close(descriptor)
        return os.stat(filename).st_mode & 0o777 == 0o600


_skip_private_mode_unavailable = pytest.mark.skipif(
    not _private_file_mode_is_preserved(), reason="private 0o600 file modes are unavailable on this host"
)
_posix_modes = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX mode bits and owners")


def _resolve_file_path(tmp_path: Path, key: str, prefix: str = "codemap") -> Path:
    """Build the exact temp-file path ``main()`` writes for *key*.

    Args:
        tmp_path: Directory where temp files are written.
        key: File key — ``"proj"`` or ``"index"``.
        prefix: File name prefix (default ``"codemap"``).

    Returns:
        Path of the temp file for *key*.

    >>> _resolve_file_path(Path("tmp"), "index").name
    'codemap-resolve-index-shared'
    """
    # The suite-wide conftest clears CSID/CLAUDE_CODE_SESSION_ID, so _resolve_csid() degrades to "shared".
    return tmp_path / f"{prefix}-resolve-{key}-shared"


def _read_resolve_file(tmp_path: Path, key: str, prefix: str = "codemap") -> str:
    """Read a resolve temp file by exact path, dropping its trailing newline.

    Sentinel files are newline-terminated so the shell's ``IFS= read -r`` succeeds
    instead of falling through to its ``||`` default; ``read`` consumes that
    delimiter, so these assertions compare against the value the shell would see.
    ``TestMain.test_writes_raw_newline_terminated_values`` locks the raw byte contract.

    Args:
        tmp_path: Directory where temp files are written.
        key: File key — ``"proj"`` or ``"index"``.
        prefix: File name prefix (default ``"codemap"``).

    Returns:
        Contents of the temp file without its trailing newline.
    """
    path = _resolve_file_path(tmp_path, key, prefix)
    assert path.exists(), f"Expected temp file not found: {path}"
    return path.read_text().removesuffix("\n")


def _resolver_output(proj: str, index_path: str) -> subprocess.CompletedProcess[str]:
    """Return the ``CompletedProcess`` a successful resolver run produces: PROJ on line 1, INDEX on line 2."""
    return subprocess.CompletedProcess(["resolve"], 0, stdout=f"{proj}\n{index_path}\n", stderr="")


_EMPTY_OUTPUT = subprocess.CompletedProcess(["resolve"], 0, stdout="", stderr="")
_DEMO_OUTPUT = _resolver_output("demo-proj", "/tmp/demo.json")


@pytest.fixture(name="tmpdir_state", params=["present", "empty", "absent"])
def _tmpdir_state(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure ``TMPDIR`` as set to a directory, set but empty, or absent, and return that state's name."""
    if request.param == "absent":
        monkeypatch.delenv("TMPDIR", raising=False)
    else:
        monkeypatch.setenv("TMPDIR", str(tmp_path) if request.param == "present" else "")
    return request.param


@pytest.fixture(
    name="previous_cache",
    params=[pytest.param(False, id="cold-tempfile-cache"), pytest.param(True, id="warm-tempfile-cache")],
)
def _previous_cache(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str | None:
    """Pin ``tempfile.tempdir`` to *tmp_path* (warm) or ``None`` (cold) and return the pinned value."""
    cached = str(tmp_path) if request.param else None
    monkeypatch.setattr(tempfile, "tempdir", cached)
    return cached


class TestParseResolverOutput:
    """Unit tests for ``parse_resolver_output()``."""

    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [
            pytest.param("myproj\n/tmp/idx.json\n", ("myproj", "/tmp/idx.json"), id="two-line-input"),
            pytest.param("only-proj\n", ("only-proj", ""), id="missing-second-line"),
            pytest.param("", ("", ""), id="empty-input"),
            pytest.param("a\nb\nc\nd\n", ("a", "b"), id="extra-lines-ignored"),
        ],
    )
    def test_splits_into_proj_and_index(self, stdout: str, expected: tuple[str, str]) -> None:
        """PROJ comes from line 1 and INDEX from line 2.

        INDEX is empty when only the PROJ line is present, both are empty when stdout is empty, and lines beyond the
        second are discarded.
        """
        assert parse_resolver_output(stdout) == expected


class TestFormatEvalLine:
    """Unit tests for ``format_eval_line()`` — retained as pure helper."""

    @pytest.mark.parametrize(
        ("proj", "index", "expected"),
        [
            # Values without metacharacters appear bare (shlex.quote shortcut).
            pytest.param("myproj", "/tmp/index.json", "PROJ=myproj INDEX=/tmp/index.json", id="simple-values-unquoted"),
            # Whitespace forces single-quote wrapping.
            pytest.param(
                "proj with space", "/tmp/x.json", "PROJ='proj with space' INDEX=/tmp/x.json", id="space-in-proj-quoted"
            ),
        ],
    )
    def test_quotes_values_only_when_needed(self, proj: str, index: str, expected: str) -> None:
        """Plain values stay bare while a value containing whitespace is single-quoted."""
        assert format_eval_line(proj, index) == expected

    @pytest.mark.parametrize(
        "proj",
        [
            pytest.param("round-trip", id="plain-value"),
            pytest.param("proj'with'quote", id="embedded-single-quotes"),
        ],
    )
    def test_round_trips_through_shlex(self, proj: str) -> None:
        """format_eval_line output round-trips through shlex back to the original values.

        Embedded single quotes survive via ``shlex.quote``, so an ``eval`` of the line reproduces PROJ and INDEX
        exactly.
        """
        line = format_eval_line(proj, "/tmp/idx.json")

        parts = dict(tok.split("=", 1) for tok in shlex.split(line) if "=" in tok)
        assert parts["PROJ"] == proj
        assert parts["INDEX"] == "/tmp/idx.json"


def test_tmpdir_doctest_preserves_environment(tmpdir_state: str, previous_cache: str | None) -> None:
    """Prevent the resolver example from changing later tests' temporary-directory root.

    Runs the ``_resolve_tmpdir`` doctest with ``TMPDIR`` present, empty or absent and with a warm or cold ``tempfile``
    cache; neither the environment variable nor the cached temp root may differ afterwards.
    """
    before = ("TMPDIR" in os.environ, os.environ.get("TMPDIR"))
    example = doctest.DocTestFinder().find(_mod._resolve_tmpdir, globs=vars(_mod))[0]

    result = doctest.DocTestRunner().run(example)

    assert result.failed == 0
    assert ("TMPDIR" in os.environ, os.environ.get("TMPDIR")) == before
    assert tempfile.tempdir == previous_cache


class TestTmpdirSafety:
    """A ``TMPDIR`` this user owns is still refused when other users could replace files in it."""

    @_posix_modes
    @pytest.mark.parametrize(
        ("mode", "unsafe"),
        [
            pytest.param(0o700, False, id="private"),
            pytest.param(0o755, False, id="readable-not-writable"),
            pytest.param(0o777, True, id="world-writable"),
            pytest.param(0o770, True, id="group-writable"),
            pytest.param(0o1777, False, id="sticky-world-writable"),
        ],
    )
    def test_mode_decides(self, tmp_path: Path, mode: int, unsafe: bool) -> None:
        """Group/other write access without the sticky bit lets another user swap a sentinel; sticky dirs are fine."""
        base = tmp_path / "base"
        base.mkdir()
        base.chmod(mode)

        reason = _mod._tmpdir_unsafe_reason(str(base))

        assert (reason is not None) is unsafe

    @_posix_modes
    def test_world_writable_tmpdir_is_rejected_and_not_reread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A rejected TMPDIR leaves the environment, so the fallback never re-reads the value just refused."""
        base = tmp_path / "shared"
        base.mkdir()
        base.chmod(0o777)
        monkeypatch.setenv("TMPDIR", str(base))
        monkeypatch.setattr(tempfile, "tempdir", None)

        resolved = _mod._resolve_tmpdir()

        assert resolved != str(base)
        assert "TMPDIR" not in os.environ
        assert "without the sticky bit" in capsys.readouterr().err


@pytest.fixture(name="private_tmpdir")
def _private_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``TMPDIR`` at the private test directory and return it."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


@pytest.mark.usefixtures("private_tmpdir")
class TestMain:
    """Exit codes, stderr messages and sentinel files written by ``main()`` for each resolver outcome."""

    @pytest.mark.parametrize(
        ("proj", "index"),
        [
            pytest.param("demo-proj", "/tmp/demo.json", id="plain-values"),
            pytest.param("proj with space", "/data/tricky idx.json", id="spaces-written-unquoted"),
        ],
    )
    @mock.patch.object(_mod.subprocess, "run", autospec=True)
    def test_writes_raw_newline_terminated_values(
        self, mock_run: mock.Mock, private_tmpdir: Path, proj: str, index: str
    ) -> None:
        """No flags: PROJ and INDEX are written raw (no shell quoting), each ending with a newline, and exit is 0.

        The trailing newline is the delimiter the shell's ``IFS= read -r`` needs, so readers never hit their ``|| VAR=``
        fallback; values with spaces must stay unquoted because the reader does no shell parsing.
        """
        mock_run.return_value = _resolver_output(proj, index)

        rc = main([])

        assert rc == 0
        assert _resolve_file_path(private_tmpdir, "proj").read_bytes() == f"{proj}\n".encode()
        assert _resolve_file_path(private_tmpdir, "index").read_bytes() == f"{index}\n".encode()

    @mock.patch.object(_mod.subprocess, "run", autospec=True)
    def test_check_exists_with_present_index_exits_0(self, mock_run: mock.Mock, private_tmpdir: Path) -> None:
        """``--check-exists`` writes the temp files and succeeds when the index file exists."""
        index = private_tmpdir / "with-index.json"
        index.write_text("{}", encoding="utf-8")
        mock_run.return_value = _resolver_output("with-index", str(index))

        rc = main(["--check-exists"])

        assert rc == 0
        assert _read_resolve_file(private_tmpdir, "proj") == "with-index"
        assert _read_resolve_file(private_tmpdir, "index") == str(index)

    @mock.patch.object(_mod.subprocess, "run", autospec=True)
    def test_check_exists_with_missing_index_exits_1_but_writes_files(
        self, mock_run: mock.Mock, private_tmpdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--check-exists`` fails when the index is absent, yet PROJ and INDEX stay available for diagnostics."""
        missing = private_tmpdir / "absent.json"  # never created
        mock_run.return_value = _resolver_output("no-idx", str(missing))

        rc = main(["--check-exists"])

        captured = capsys.readouterr()
        assert rc == 1
        assert _read_resolve_file(private_tmpdir, "proj") == "no-idx"
        assert _read_resolve_file(private_tmpdir, "index") == str(missing)
        assert "INDEX file not found" in captured.err
        assert str(missing) in captured.err

    @pytest.mark.parametrize(
        "argv", [pytest.param([], id="no-flags"), pytest.param(["--check-exists"], id="check-exists-does-not-mask")]
    )
    @mock.patch.object(_mod.subprocess, "run", autospec=True, return_value=_EMPTY_OUTPUT)
    def test_empty_resolver_output_exits_1(
        self, mock_run: mock.Mock, private_tmpdir: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        """Empty resolver stdout → exit 1, empty temp files written, error on stderr.

        ``--check-exists`` must not change the outcome when the resolver itself fails first.
        """
        rc = main(argv)

        captured = capsys.readouterr()
        assert rc == 1
        assert _read_resolve_file(private_tmpdir, "proj") == ""
        assert _read_resolve_file(private_tmpdir, "index") == ""
        assert "produced no output" in captured.err

    def test_unknown_flag_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Reject an unknown option with exit 2 and identify it in the error output."""
        rc = main(["--no-such-flag"])

        captured = capsys.readouterr()
        assert rc == 2
        assert "unknown flag" in captured.err
        assert "--no-such-flag" in captured.err

    @mock.patch.object(
        _mod.subprocess, "run", autospec=True, return_value=_resolver_output("old-proj", "/tmp/old.json")
    )
    def test_validation_failure_after_success_empties_sentinels(
        self, mock_run: mock.Mock, private_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A seed run writes real values; a later unsafe-plugin-root failure (exit 3) clears them.

        Regression: an exit-3 run used to skip the temp-file write entirely, so the shell consumer's
        ``[ -n "$PROJ" ]`` liveness check passed on stale, unrelated-project data.
        """
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert main(["--output-prefix", "codemap-stale"]) == 0
        assert _read_resolve_file(private_tmpdir, "proj", prefix="codemap-stale") == "old-proj"
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", os.path.join(str(private_tmpdir), "not-the-real-root"))

        rc = main(["--output-prefix", "codemap-stale"])

        assert rc == 3
        assert _read_resolve_file(private_tmpdir, "proj", prefix="codemap-stale") == ""
        assert _read_resolve_file(private_tmpdir, "index", prefix="codemap-stale") == ""


class TestValidatePluginRoot:
    """Validate plugin-root containment through resolved path identity.

    Regression coverage for the residual-critical finding that the prior regex hardcoded the pre-rename plugin name
    ``codemap`` (rejecting the real installed/source root after the ``codemap-py`` rename) while also accepting
    attacker-chosen directories that merely matched the pattern's shape (no traversal normalization).
    """

    def test_own_plugin_root_is_accepted(self) -> None:
        """The directory this script itself runs from always validates."""
        own = str(_own_plugin_root())

        assert _validate_plugin_root(own) == own

    @pytest.mark.parametrize(
        "candidate",
        [
            # A directory that merely matches the old shape (``.../plugins/codemap``) is rejected.
            pytest.param("/tmp/attacker/plugins/codemap", id="unrelated-absolute-path"),
            # A relative value is rejected before any resolution is attempted.
            pytest.param("plugins/codemap-py", id="relative-path"),
            # Appending ``../`` segments to the real root no longer bypasses containment.
            pytest.param(str(_own_plugin_root()) + "/../../../../tmp/evil", id="traversal-out-of-own-root"),
        ],
    )
    def test_foreign_plugin_root_is_rejected(self, candidate: str) -> None:
        """An unrelated absolute path, a relative path or a traversal out of the real root is not a safe plugin root."""
        with pytest.raises(ValueError, match="not a safe path"):
            _validate_plugin_root(candidate)


class TestValidateOutputPrefix:
    """Accept dotted output basenames while rejecting directory traversal.

    Regression coverage for the residual-critical finding that the prior ``[a-zA-Z0-9_-]+`` pattern rejected any project
    directory name containing a dot (including this repository's own ``Borda.local``), a hard break in the documented
    ``codemap-$(basename ...)`` recipe.
    """

    def test_dotted_basename_prefix_is_accepted(self) -> None:
        """A prefix built from a dotted project basename validates."""
        assert _validate_output_prefix("codemap-Borda.local") == "codemap-Borda.local"

    @pytest.mark.parametrize("bad", [".", "..", "../escape", ""])
    def test_dot_forms_and_traversal_are_rejected(self, bad: str) -> None:
        """Bare ``.``/``..``, a traversal segment, and an empty value all raise."""
        with pytest.raises(ValueError, match="output-prefix"):
            _validate_output_prefix(bad)


class TestSentinelSymlinkSafety:
    """Refuse to follow a pre-existing sentinel symlink.

    Regression coverage for the residual-critical finding that ``Path.write_text`` (the prior implementation) follows an
    existing symlink at the predictable sentinel path, letting a co-located attacker overwrite an arbitrary file the
    invoking user can write.
    """

    @skip_file_symlink_unavailable
    def test_preplanted_symlink_is_not_followed(self, tmp_path: Path) -> None:
        """Writing to a path that is a symlink raises instead of truncating the target."""
        victim = tmp_path / "victim.txt"
        victim.write_text("IMPORTANT ORIGINAL CONTENT\n", encoding="utf-8")
        link = tmp_path / "codemap-resolve-proj-shared"
        link.symlink_to(victim)

        with pytest.raises(OSError, match=r"refusing to write through symlink"):
            _write_sentinel_file(link, "PWNED\n")

        assert victim.read_text(encoding="utf-8") == "IMPORTANT ORIGINAL CONTENT\n"

    @_skip_private_mode_unavailable
    def test_written_file_is_mode_0600(self, tmp_path: Path) -> None:
        """A freshly written sentinel is owner-only readable/writable regardless of umask."""
        target = tmp_path / "codemap-resolve-index-shared"

        _write_sentinel_file(target, "value\n")

        assert (target.stat().st_mode & 0o777) == 0o600


class TestUnsetPluginRootUsesOwnRoot:
    """An unset ``CLAUDE_PLUGIN_ROOT`` never lets the working directory pick the resolver that runs."""

    def test_default_is_the_scripts_own_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fallback is this script's own plugin root, not a cwd-relative ``plugins/codemap-py``."""
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        assert _mod._resolve_plugin_root() == str(_mod._own_plugin_root())

    def test_cwd_planted_resolver_is_not_executed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``plugins/codemap-py/bin/resolve_proj_index.py`` shipped by the current directory stays unexecuted.

        A repository under analysis can carry that path; with the old relative default the resolver ran it. This runs
        the real ``_run_resolver`` subprocess, because only a real launch can show the planted script is not executed.
        """
        planted = tmp_path / "plugins" / "codemap-py" / "bin"
        planted.mkdir(parents=True)
        marker = tmp_path / "PWNED"
        (planted / "resolve_proj_index.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        _mod._run_resolver(_mod._resolve_plugin_root())

        assert not marker.exists()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="file ownership is a POSIX concept")
class TestSentinelOwnership:
    """A sentinel pre-planted by another user is refused instead of reused."""

    def test_foreign_owned_sentinel_is_refused_and_left_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An existing file whose owner differs from the caller is not truncated or written."""
        sentinel = tmp_path / "codemap-resolve-index-x"
        sentinel.write_text("planted\n", encoding="utf-8")
        monkeypatch.setattr(_mod.os, "getuid", lambda: os.stat(sentinel).st_uid + 1)

        with pytest.raises(OSError, match="owned by another user"):
            _write_sentinel_file(sentinel, "value\n")

        assert sentinel.read_text(encoding="utf-8") == "planted\n"

    def test_own_sentinel_is_overwritten_in_place(self, tmp_path: Path) -> None:
        """The legitimate rewrite within one session still replaces the previous content."""
        sentinel = tmp_path / "codemap-resolve-index-x"
        _write_sentinel_file(sentinel, "first value that is long\n")

        _write_sentinel_file(sentinel, "short\n")

        assert sentinel.read_text(encoding="utf-8") == "short\n"


class TestSessionSuffix:
    """The ``CSID`` suffix of the written sentinel names stays a plain filename component."""

    @pytest.mark.parametrize(
        ("variable", "bad_value"),
        [
            pytest.param("CSID", "../escaped", id="csid-traversal"),
            pytest.param("CSID", "a/b", id="csid-separator"),
            pytest.param("CSID", "..", id="csid-dotdot"),
            pytest.param("CSID", "x y", id="csid-space"),
            pytest.param("CLAUDE_CODE_SESSION_ID", "../../x", id="session-id-fallback-traversal"),
        ],
    )
    def test_unsafe_value_falls_back_to_shared(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], variable: str, bad_value: str
    ) -> None:
        """A session id that is not a plain filename component becomes ``shared`` with a stderr warning.

        Left verbatim, ``../escaped`` or a value holding a separator would move the sentinel write out of ``TMPDIR``.
        The ``CLAUDE_CODE_SESSION_ID`` fallback goes through the same check as ``CSID``.
        """
        monkeypatch.setenv(variable, bad_value)

        assert _mod._resolve_csid() == "shared"
        assert "CSID is not a plain filename component" in capsys.readouterr().err

    def test_uuid_shaped_csid_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real session id (UUID or PID) passes through unchanged."""
        monkeypatch.setenv("CSID", "1f29db67-5fb5-4f9f-86d9-76a1b943ec16")

        assert _mod._resolve_csid() == "1f29db67-5fb5-4f9f-86d9-76a1b943ec16"
