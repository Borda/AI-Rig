"""Tests for bin/detect_codemap.py — codemap availability detection.

Covers: missing ``--prefix`` exit 2, ``--force-off``, codemap-py query present/absent, index present/absent,
``--strict`` mode errors, option overrides, env vars, and _resolve_proj() helper.
"""

from __future__ import annotations

import subprocess
import unittest.mock as mock
from pathlib import Path

import pytest

import detect_codemap  # type: ignore[import-not-found]


class TestMain:
    """Verify the command-line entry-point behavior."""

    def test_missing_prefix_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No ``--prefix`` → exit 2, usage message on stderr."""
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
        """An unsafe ``--proj`` value is rejected outright — never sanitized.

        Regression guard for the reject-not-sanitize choice: ``_resolve_proj`` must keep passing the raw
        basename through untouched (stripping characters would seek a filename the scanner never wrote), so
        the CLI boundary rejects a path-shaped ``--proj`` before it ever reaches that function.
        """
        rc = detect_codemap.main(["--prefix", "test", "--proj", proj])
        assert rc == 2
        assert "--proj" in capsys.readouterr().err

    def test_proj_bare_name_still_works(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A normal bare project name is unaffected by the path-separator guard."""
        idx_dir = tmp_path / "idx"
        idx_dir.mkdir()
        (idx_dir / "myproj.json").write_text("{}")
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        with mock.patch("detect_codemap.shutil.which", return_value="/usr/bin/codemap-py"):
            rc = detect_codemap.main(["--prefix", "test", "--proj", "myproj", "--idx-dir", str(idx_dir)])
        assert rc == 0
        assert (tmp_path / "test-codemap-enabled-shared").read_text() == "true\n"

    def test_force_off_writes_false_and_exits_0(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify command-line option behavior.

        ``--force-off`` → writes 'false', exits 0 regardless of codemap state.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        with mock.patch("detect_codemap.shutil.which", return_value=None):
            rc = detect_codemap.main(["--prefix", "test", "--force-off"])
        assert rc == 0
        assert (tmp_path / "test-codemap-enabled-shared").read_text() == "false\n"

    def test_scan_query_present_index_present_writes_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codemap-py query on PATH + index file exists → writes 'true', exits 0."""
        idx_dir = tmp_path / "idx"
        idx_dir.mkdir()
        (idx_dir / "myproj.json").write_text("{}")
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        with mock.patch("detect_codemap.shutil.which", return_value="/usr/bin/codemap-py"):
            rc = detect_codemap.main(["--prefix", "test", "--proj", "myproj", "--idx-dir", str(idx_dir)])
        assert rc == 0
        assert (tmp_path / "test-codemap-enabled-shared").read_text() == "true\n"

    def test_scan_query_present_index_absent_writes_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codemap-py query on PATH but no index file → writes 'false', exits 0."""
        idx_dir = tmp_path / "idx"
        idx_dir.mkdir()
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        with mock.patch("detect_codemap.shutil.which", return_value="/usr/bin/codemap-py"):
            rc = detect_codemap.main(["--prefix", "test", "--proj", "myproj", "--idx-dir", str(idx_dir)])
        assert rc == 0
        assert (tmp_path / "test-codemap-enabled-shared").read_text() == "false\n"

    def test_scan_query_absent_writes_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Codemap-py query not on PATH → writes 'false', exits 0."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        with mock.patch("detect_codemap.shutil.which", return_value=None):
            rc = detect_codemap.main(["--prefix", "test", "--proj", "myproj", "--idx-dir", str(tmp_path / "idx")])
        assert rc == 0
        assert (tmp_path / "test-codemap-enabled-shared").read_text() == "false\n"

    def test_strict_no_scan_query_exits_1_with_install_hint(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify command-line option behavior.

        ``--strict`` + codemap-py query absent → exit 1, install hint on stderr.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        with mock.patch("detect_codemap.shutil.which", return_value=None):
            rc = detect_codemap.main(["--prefix", "test", "--proj", "proj", "--idx-dir", str(tmp_path), "--strict"])
        assert rc == 1
        assert "claude plugin install codemap-py@borda-ai-rig" in capsys.readouterr().err

    def test_strict_scan_query_no_index_exits_1_with_build_hint(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify command-line option behavior.

        ``--strict`` + codemap-py query present + no index → exit 1, build-index hint on stderr.
        """
        idx_dir = tmp_path / "idx"
        idx_dir.mkdir()
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        with mock.patch("detect_codemap.shutil.which", return_value="/usr/bin/codemap-py"):
            rc = detect_codemap.main(["--prefix", "test", "--proj", "proj", "--idx-dir", str(idx_dir), "--strict"])
        assert rc == 1
        assert "/codemap-py:scan-codebase" in capsys.readouterr().err

    def test_proj_override_used_as_index_slug(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify command-line option behavior.

        ``--proj`` overrides git-derived slug; index looked up as <proj>.json.
        """
        idx_dir = tmp_path / "idx"
        idx_dir.mkdir()
        (idx_dir / "custom-proj.json").write_text("{}")
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        with mock.patch("detect_codemap.shutil.which", return_value="/usr/bin/codemap-py"):
            rc = detect_codemap.main(["--prefix", "test", "--proj", "custom-proj", "--idx-dir", str(idx_dir)])
        assert rc == 0
        assert (tmp_path / "test-codemap-enabled-shared").read_text() == "true\n"

    def test_idx_dir_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify command-line option behavior.

        ``--idx-dir`` overrides default .cache/codemap lookup path.
        """
        custom_idx = tmp_path / "custom-index"
        custom_idx.mkdir()
        (custom_idx / "proj.json").write_text("{}")
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        with mock.patch("detect_codemap.shutil.which", return_value="/usr/bin/codemap-py"):
            rc = detect_codemap.main(["--prefix", "test", "--proj", "proj", "--idx-dir", str(custom_idx)])
        assert rc == 0
        assert (tmp_path / "test-codemap-enabled-shared").read_text() == "true\n"

    def test_codemap_index_dir_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """CODEMAP_INDEX_DIR env var sets default index dir when ``--idx-dir`` is absent."""
        idx_dir = tmp_path / "env-idx"
        idx_dir.mkdir()
        (idx_dir / "envproj.json").write_text("{}")
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CODEMAP_INDEX_DIR", str(idx_dir))
        with mock.patch("detect_codemap.shutil.which", return_value="/usr/bin/codemap-py"):
            rc = detect_codemap.main(["--prefix", "test", "--proj", "envproj"])
        assert rc == 0
        assert (tmp_path / "test-codemap-enabled-shared").read_text() == "true\n"

    def test_tmpdir_env_var_controls_output_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """TMPDIR env var controls where the output file is written."""
        out_dir = tmp_path / "mytmp"
        out_dir.mkdir()
        monkeypatch.setenv("TMPDIR", str(out_dir))
        with mock.patch("detect_codemap.shutil.which", return_value=None):
            rc = detect_codemap.main(["--prefix", "myprefix", "--proj", "proj", "--idx-dir", str(tmp_path)])
        assert rc == 0
        assert (out_dir / "myprefix-codemap-enabled-shared").exists()


def _init_repo(path: Path) -> Path:
    """Create a git repository at *path* and return the root git itself reports."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Path(top)


class TestProjectRoot:
    """Anchor project discovery at the Git top-level directory."""

    def test_git_success_returns_toplevel(self) -> None:
        """Git rev-parse success → the reported toplevel path, not the CWD."""
        mock_result = mock.Mock(returncode=0, stdout="/home/user/my-project\n")
        with mock.patch("detect_codemap.subprocess.run", return_value=mock_result):
            assert detect_codemap._project_root() == Path("/home/user/my-project")

    def test_git_nonzero_exit_falls_back_to_cwd(self) -> None:
        """Git rev-parse non-zero exit → CWD, matching the provider's own fallback."""
        mock_result = mock.Mock(returncode=128, stdout="")
        with mock.patch("detect_codemap.subprocess.run", return_value=mock_result):
            assert detect_codemap._project_root() == Path.cwd()

    def test_git_timeout_falls_back_to_cwd(self) -> None:
        """subprocess.run raising TimeoutExpired → CWD, never an exception."""
        with mock.patch(
            "detect_codemap.subprocess.run",
            side_effect=subprocess.TimeoutExpired("git", 5),
        ):
            assert detect_codemap._project_root() == Path.cwd()

    def test_git_missing_binary_falls_back_to_cwd(self) -> None:
        """Git absent (OSError) → CWD, never an exception."""
        with mock.patch("detect_codemap.subprocess.run", side_effect=OSError("no git")):
            assert detect_codemap._project_root() == Path.cwd()


class TestResolveProj:
    """Match provider naming through the raw project basename."""

    def test_basename_used_verbatim(self) -> None:
        """Plain basename returned as-is."""
        assert detect_codemap._resolve_proj(None, Path("/home/user/my-project")) == "my-project"

    def test_proj_override_wins(self) -> None:
        """Verify command-line option behavior.

        ``--proj`` bypasses the root-derived name entirely.
        """
        assert detect_codemap._resolve_proj("explicit-proj", Path("/home/user/other")) == "explicit-proj"

    @pytest.mark.parametrize("name", ["my repo with spaces", "café", "a+b", "proj(1)"])
    def test_special_chars_are_not_stripped(self, name: str) -> None:
        """Space/'+'/non-ASCII survive: the scanner writes the RAW basename.

        Regression: the old unicode-``isalnum`` filter turned ``a+b`` into
        ``ab`` and ``my repo`` into ``myrepo``, so the consumer sought a filename the
        scanner never wrote: a permanent, silent false ``no_index``.
        """
        assert detect_codemap._resolve_proj(None, Path("/tmp") / name) == name


class TestIndexAnchoring:
    """End-to-end index location — anchored at the git root, not the CWD."""

    def test_subdir_invocation_finds_root_anchored_index(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invoked from a repo subdirectory, the root's index still counts.

        The old default was the cwd-relative string ``.cache/codemap``, so running any skill from ``<repo>/pkg/sub``
        probed ``<repo>/pkg/sub/.cache/codemap`` and reported ``no_index`` while the index sat at the repo root.
        """
        root = _init_repo(tmp_path / "my-repo")
        index_dir = root / ".cache" / "codemap"
        index_dir.mkdir(parents=True)
        (index_dir / f"{root.name}.json").write_text("{}")
        subdir = root / "pkg" / "sub"
        subdir.mkdir(parents=True)

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
        monkeypatch.chdir(subdir)
        with mock.patch("detect_codemap.shutil.which", return_value="/usr/bin/codemap-py"):
            rc = detect_codemap.main(["--prefix", "test"])

        assert rc == 0
        assert (tmp_path / "test-codemap-enabled-shared").read_text() == "true\n"
        assert not (subdir / ".cache").exists(), "must not have probed a cwd-relative index dir"

    def test_non_ascii_repo_name_index_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A repo directory named ``café`` resolves to ``café.json``."""
        root = _init_repo(tmp_path / "café")
        index_dir = root / ".cache" / "codemap"
        index_dir.mkdir(parents=True)
        (index_dir / f"{root.name}.json").write_text("{}")

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
        monkeypatch.chdir(root)
        with mock.patch("detect_codemap.shutil.which", return_value="/usr/bin/codemap-py"):
            rc = detect_codemap.main(["--prefix", "test"])

        assert rc == 0
        assert (tmp_path / "test-codemap-enabled-shared").read_text() == "true\n"

    def test_directory_named_like_index_does_not_pass_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A *directory* called ``<proj>.json`` is not an index."""
        idx_dir = tmp_path / "idx"
        (idx_dir / "proj.json").mkdir(parents=True)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        with mock.patch("detect_codemap.shutil.which", return_value="/usr/bin/codemap-py"):
            rc = detect_codemap.main(["--prefix", "test", "--proj", "proj", "--idx-dir", str(idx_dir)])
        assert rc == 0
        assert (tmp_path / "test-codemap-enabled-shared").read_text() == "false\n"
        assert (tmp_path / "test-codemap-currency-shared").read_text() == "no_index\n"


class TestCurrency:
    """Currency probe — fail-open, but never silently."""

    def test_probe_failure_announces_the_coercion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unparsable probe output → 'current' written, coercion noted on stderr."""
        idx_dir = tmp_path / "idx"
        idx_dir.mkdir()
        (idx_dir / "proj.json").write_text("{}")
        monkeypatch.setenv("TMPDIR", str(tmp_path))

        def _which(name: str) -> str | None:
            """Expose both codemap and currency probe executables."""
            return "/usr/bin/check-index-currency" if name == "check-index-currency" else "/usr/bin/codemap-py"

        with (
            mock.patch("detect_codemap.shutil.which", side_effect=_which),
            mock.patch("detect_codemap.subprocess.run", return_value=mock.Mock(returncode=0, stdout="not json")),
        ):
            rc = detect_codemap.main(["--prefix", "test", "--proj", "proj", "--idx-dir", str(idx_dir)])

        assert rc == 0
        assert (tmp_path / "test-codemap-currency-shared").read_text() == "current\n"
        assert "staleness was NOT verified" in capsys.readouterr().err

    def test_probe_absent_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No check-index-currency on PATH → 'current', no warning (nothing failed)."""
        idx_dir = tmp_path / "idx"
        idx_dir.mkdir()
        (idx_dir / "proj.json").write_text("{}")
        monkeypatch.setenv("TMPDIR", str(tmp_path))

        def _which(name: str) -> str | None:
            """Expose codemap while making the optional currency probe absent."""
            return None if name == "check-index-currency" else "/usr/bin/codemap-py"

        with mock.patch("detect_codemap.shutil.which", side_effect=_which):
            rc = detect_codemap.main(["--prefix", "test", "--proj", "proj", "--idx-dir", str(idx_dir)])

        assert rc == 0
        assert (tmp_path / "test-codemap-currency-shared").read_text() == "current\n"
        assert "staleness was NOT verified" not in capsys.readouterr().err
