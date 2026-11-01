"""Tests for bin/detect_codemap.py — codemap availability detection.

Covers: missing ``--prefix`` exit 2, ``--force-off``, codemap-py query present/absent, index present/absent,
``--strict`` mode errors, option overrides, env vars, and _resolve_proj() helper.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest.mock as mock
from pathlib import Path

import detect_codemap  # type: ignore[import-not-found]
import pytest

_CODEMAP = "/usr/bin/codemap-py"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``TMPDIR`` at ``tmp_path`` and drop ``CODEMAP_INDEX_DIR`` so state files and index lookups stay private."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)


def _enabled(tmp_path: Path) -> str:
    """Return the text the script wrote to its ``test-codemap-enabled`` state file."""
    return (tmp_path / "test-codemap-enabled-shared").read_text()


def _make_index_dir(tmp_path: Path, name: str = "idx", *index_files: str) -> Path:
    """Create ``tmp_path/<name>`` holding an empty-JSON index file for each of ``index_files``."""
    idx_dir = tmp_path / name
    idx_dir.mkdir()
    for index_file in index_files:
        (idx_dir / index_file).write_text("{}")
    return idx_dir


def _init_repo(path: Path) -> Path:
    """Create a git repository at *path* and return the root git itself reports."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()
    return Path(top)


def _repo_with_index(path: Path) -> Path:
    """Create a git repo at *path* whose root-level ``.cache/codemap`` holds ``<repo name>.json``; return the root."""
    root = _init_repo(path)
    index_dir = root / ".cache" / "codemap"
    index_dir.mkdir(parents=True)
    (index_dir / f"{root.name}.json").write_text("{}")
    return root


def _which_with_currency_probe(name: str) -> str:
    """Expose both codemap-py and the currency probe executables."""
    return "/usr/bin/check-index-currency" if name == "check-index-currency" else _CODEMAP


def _which_without_currency_probe(name: str) -> str | None:
    """Expose codemap-py while making the optional currency probe absent."""
    return None if name == "check-index-currency" else _CODEMAP


class TestMain:
    """Verify the command-line entry-point behavior."""

    def test_missing_index_with_legacy_output_encoding(self, tmp_path: Path) -> None:
        """The real CLI writes disabled state even when the output codec cannot encode its warning or project name.

        A console limited to cp1252 meets a project name written in Japanese; the script must escape it instead of
        crashing, still write both state files, and exit 0. Runs the script as a real subprocess because the output
        codec is a process-level property.
        """
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        executable = bin_dir / ("codemap-py.cmd" if os.name == "nt" else "codemap-py")
        executable.write_text("", encoding="utf-8")
        executable.chmod(0o700)
        env = {
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "TMPDIR": str(tmp_path),
            "CSID": "legacy-encoding",
            "PYTHONIOENCODING": "cp1252:strict",
        }
        script = Path(detect_codemap.__file__)
        result = subprocess.run(
            [sys.executable, str(script), "--prefix", "test", "--proj", "日本", "--idx-dir", str(tmp_path)],
            env=env,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode("cp1252")
        assert b"no index for project" in result.stdout
        assert b"\\u65e5\\u672c" in result.stdout
        assert (tmp_path / "test-codemap-enabled-legacy-encoding").read_text(encoding="utf-8") == "false\n"
        assert (tmp_path / "test-codemap-currency-legacy-encoding").read_text(encoding="utf-8") == "no_index\n"

    def test_missing_prefix_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Running without ``--prefix`` exits 2 and prints a usage message on stderr.

        The prefix names the state files, so without it there is nowhere to write the verdict.
        """
        rc = detect_codemap.main([])
        assert rc == 2
        assert "Usage" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "proj",
        [
            pytest.param("../../etc", id="parent-traversal"),
            pytest.param("a/b", id="forward-slash"),
            pytest.param("a\\b", id="backslash"),
            pytest.param(".", id="dot"),
            pytest.param("..", id="dotdot"),
        ],
    )
    def test_proj_with_path_separator_exits_2(self, proj: str, capsys: pytest.CaptureFixture[str]) -> None:
        """An unsafe ``--proj`` value is rejected outright, never sanitized.

        Regression guard for the reject-not-sanitize choice: ``_resolve_proj`` must keep passing the raw basename
        through untouched (stripping characters would seek a filename the scanner never wrote), so the CLI boundary
        rejects a path-shaped ``--proj`` before it ever reaches that function.
        """
        rc = detect_codemap.main(["--prefix", "test", "--proj", proj])
        assert rc == 2
        assert "--proj" in capsys.readouterr().err

    @mock.patch("detect_codemap.shutil.which", autospec=True)
    @pytest.mark.parametrize(
        ("codemap", "flags", "index_dir", "index_files", "proj", "expected"),
        [
            pytest.param(_CODEMAP, [], "idx", ["myproj.json"], "myproj", "true\n", id="bare-project-name"),
            pytest.param(_CODEMAP, [], "idx", ["custom-proj.json"], "custom-proj", "true\n", id="proj-override-slug"),
            pytest.param(_CODEMAP, [], "custom-index", ["proj.json"], "proj", "true\n", id="idx-dir-override"),
            pytest.param(_CODEMAP, [], "idx", [], "myproj", "false\n", id="index-file-absent"),
            pytest.param(None, [], "idx", ["myproj.json"], "myproj", "false\n", id="codemap-absent"),
            pytest.param(_CODEMAP, ["--force-off"], "idx", ["myproj.json"], "myproj", "false\n", id="force-off"),
        ],
    )
    def test_writes_enabled_flag(
        self,
        which: mock.Mock,
        tmp_path: Path,
        codemap: str | None,
        flags: list[str],
        index_dir: str,
        index_files: list[str],
        proj: str,
        expected: str,
    ) -> None:
        """Codemap is enabled only when codemap-py is on PATH and ``<proj>.json`` exists, unless ``--force-off``.

        ``--proj`` overrides the git-derived slug, so the index is looked up as ``<proj>.json``; ``--idx-dir`` overrides
        the default ``.cache/codemap`` location; ``--force-off`` writes ``false`` regardless of codemap state. Every
        case exits 0 and records the verdict in ``<prefix>-codemap-enabled-<csid>``.
        """
        which.return_value = codemap
        idx_dir = _make_index_dir(tmp_path, index_dir, *index_files)
        rc = detect_codemap.main(["--prefix", "test", "--proj", proj, "--idx-dir", str(idx_dir), *flags])
        assert rc == 0
        assert _enabled(tmp_path) == expected

    @mock.patch("detect_codemap.shutil.which", autospec=True)
    @pytest.mark.parametrize(
        ("codemap", "index_files", "hint"),
        [
            pytest.param(None, [], "claude plugin install codemap-py@borda-ai-rig", id="codemap-absent-install-hint"),
            pytest.param(_CODEMAP, [], "/codemap-py:scan-codebase", id="index-absent-build-hint"),
        ],
    )
    def test_strict_exits_1_with_hint(
        self,
        which: mock.Mock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        codemap: str | None,
        index_files: list[str],
        hint: str,
    ) -> None:
        """``--strict`` turns a missing codemap-py or a missing index into exit 1 with an actionable hint on stderr.

        Without codemap-py the hint is the plugin install command; with codemap-py but no index file it points at the
        scan command that builds one.
        """
        which.return_value = codemap
        idx_dir = _make_index_dir(tmp_path, "idx", *index_files)
        rc = detect_codemap.main(["--prefix", "test", "--proj", "proj", "--idx-dir", str(idx_dir), "--strict"])
        assert rc == 1
        assert hint in capsys.readouterr().err

    @mock.patch("detect_codemap.shutil.which", autospec=True, return_value=_CODEMAP)
    def test_codemap_index_dir_env_var(self, which: mock.Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``CODEMAP_INDEX_DIR`` sets the index directory when ``--idx-dir`` is absent."""
        idx_dir = _make_index_dir(tmp_path, "env-idx", "envproj.json")
        monkeypatch.setenv("CODEMAP_INDEX_DIR", str(idx_dir))
        rc = detect_codemap.main(["--prefix", "test", "--proj", "envproj"])
        assert rc == 0
        assert _enabled(tmp_path) == "true\n"

    @mock.patch("detect_codemap.shutil.which", autospec=True, return_value=None)
    def test_tmpdir_env_var_controls_output_path(
        self, which: mock.Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``TMPDIR`` controls the directory the state file is written to, named by the given prefix."""
        out_dir = tmp_path / "mytmp"
        out_dir.mkdir()
        monkeypatch.setenv("TMPDIR", str(out_dir))
        rc = detect_codemap.main(["--prefix", "myprefix", "--proj", "proj", "--idx-dir", str(tmp_path)])
        assert rc == 0
        assert (out_dir / "myprefix-codemap-enabled-shared").exists()


class TestProjectRoot:
    """Anchor project discovery at the Git top-level directory."""

    @mock.patch(
        "detect_codemap.subprocess.run",
        autospec=True,
        return_value=mock.Mock(returncode=0, stdout="/home/user/my-project\n"),
    )
    def test_git_success_returns_toplevel(self, run: mock.Mock) -> None:
        """A successful ``git rev-parse --show-toplevel`` yields the reported toplevel path, not the CWD."""
        assert detect_codemap._project_root() == Path("/home/user/my-project")

    @mock.patch("detect_codemap.subprocess.run", autospec=True)
    @pytest.mark.parametrize(
        "outcome",
        [
            pytest.param(mock.Mock(returncode=128, stdout=""), id="nonzero-exit"),
            pytest.param(subprocess.TimeoutExpired("git", 5), id="timeout"),
            pytest.param(OSError("no git"), id="missing-binary"),
        ],
    )
    def test_git_failure_falls_back_to_cwd(self, run: mock.Mock, outcome: object) -> None:
        """A non-zero git exit, a timeout, or an absent binary (OSError) falls back to the CWD, never an exception.

        The non-zero exit matches the provider's own fallback to the CWD.
        """
        run.side_effect = [outcome]
        assert detect_codemap._project_root() == Path.cwd()


class TestResolveProj:
    """Match provider naming through the raw project basename."""

    @pytest.mark.parametrize(
        ("proj", "root", "expected"),
        [
            pytest.param(None, Path("/home/user/my-project"), "my-project", id="basename-used-verbatim"),
            pytest.param("explicit-proj", Path("/home/user/other"), "explicit-proj", id="proj-override-wins"),
        ],
    )
    def test_project_name_is_override_or_root_basename(self, proj: str | None, root: Path, expected: str) -> None:
        """A plain basename is returned as-is; ``--proj`` bypasses the root-derived name entirely."""
        assert detect_codemap._resolve_proj(proj, root) == expected

    @pytest.mark.parametrize("name", ["my repo with spaces", "café", "a+b", "proj(1)"])
    def test_special_chars_are_not_stripped(self, name: str) -> None:
        """Space, ``+`` and non-ASCII characters survive because the scanner writes the RAW basename.

        Regression: the old unicode-``isalnum`` filter turned ``a+b`` into ``ab`` and ``my repo`` into ``myrepo``, so
        the consumer sought a filename the scanner never wrote: a permanent, silent false ``no_index``.
        """
        assert detect_codemap._resolve_proj(None, Path("/tmp") / name) == name


class TestIndexAnchoring:
    """End-to-end index location, anchored at the git root and not the CWD."""

    @mock.patch("detect_codemap.shutil.which", autospec=True, return_value=_CODEMAP)
    def test_subdir_invocation_finds_root_anchored_index(
        self, which: mock.Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invoked from a repo subdirectory, the root's index still counts.

        The old default was the cwd-relative string ``.cache/codemap``, so running any skill from ``<repo>/pkg/sub``
        probed ``<repo>/pkg/sub/.cache/codemap`` and reported ``no_index`` while the index sat at the repo root. Uses a
        real ``git init`` because resolving the root through git is the behavior under test.
        """
        root = _repo_with_index(tmp_path / "my-repo")
        subdir = root / "pkg" / "sub"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        rc = detect_codemap.main(["--prefix", "test"])
        assert rc == 0
        assert _enabled(tmp_path) == "true\n"
        assert not (subdir / ".cache").exists(), "must not have probed a cwd-relative index dir"

    @mock.patch("detect_codemap.shutil.which", autospec=True, return_value=_CODEMAP)
    def test_non_ascii_repo_name_index_found(
        self, which: mock.Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repo directory named ``café`` resolves to ``café.json``.

        The project name is the raw directory basename, so a non-ASCII repo name must find the index the scanner wrote
        under that same name.
        """
        root = _repo_with_index(tmp_path / "café")
        monkeypatch.chdir(root)
        rc = detect_codemap.main(["--prefix", "test"])
        assert rc == 0
        assert _enabled(tmp_path) == "true\n"

    @mock.patch("detect_codemap.shutil.which", autospec=True, return_value=_CODEMAP)
    def test_directory_named_like_index_does_not_pass_gate(self, which: mock.Mock, tmp_path: Path) -> None:
        """A *directory* called ``<proj>.json`` is not an index.

        Only a regular file counts, so the gate stays disabled and the currency state reads ``no_index``.
        """
        idx_dir = tmp_path / "idx"
        (idx_dir / "proj.json").mkdir(parents=True)
        rc = detect_codemap.main(["--prefix", "test", "--proj", "proj", "--idx-dir", str(idx_dir)])
        assert rc == 0
        assert _enabled(tmp_path) == "false\n"
        assert (tmp_path / "test-codemap-currency-shared").read_text() == "no_index\n"


class TestCurrency:
    """Currency probe: fail-open, but never silently."""

    @mock.patch("detect_codemap.subprocess.run", autospec=True, return_value=mock.Mock(returncode=0, stdout="not json"))
    @mock.patch("detect_codemap.shutil.which", autospec=True, side_effect=_which_with_currency_probe)
    def test_probe_failure_announces_the_coercion(
        self, which: mock.Mock, run: mock.Mock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unparsable probe output writes ``current`` and notes the coercion on stderr.

        The probe's answer is unusable, so the index is treated as current rather than blocking the run, but the warning
        says staleness was not verified so the fail-open choice is never silent.
        """
        idx_dir = _make_index_dir(tmp_path, "idx", "proj.json")
        rc = detect_codemap.main(["--prefix", "test", "--proj", "proj", "--idx-dir", str(idx_dir)])
        assert rc == 0
        assert (tmp_path / "test-codemap-currency-shared").read_text() == "current\n"
        assert "staleness was NOT verified" in capsys.readouterr().err

    @mock.patch("detect_codemap.shutil.which", autospec=True, side_effect=_which_without_currency_probe)
    def test_probe_absent_is_silent(self, which: mock.Mock, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Without ``check-index-currency`` on PATH the state is ``current`` and no warning is printed.

        An absent optional probe is not a failure, so nothing is announced.
        """
        idx_dir = _make_index_dir(tmp_path, "idx", "proj.json")
        rc = detect_codemap.main(["--prefix", "test", "--proj", "proj", "--idx-dir", str(idx_dir)])
        assert rc == 0
        assert (tmp_path / "test-codemap-currency-shared").read_text() == "current\n"
        assert "staleness was NOT verified" not in capsys.readouterr().err
