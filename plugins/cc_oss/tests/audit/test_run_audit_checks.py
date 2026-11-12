"""Tests for ``bin/run_audit_checks.py``.

``subprocess.run`` and ``which`` are patched, so no real tool is invoked; the ``audit`` fixture also moves the working
directory into ``tmp_path`` so the filesystem scans (version and code-signal grep) only see fixture files. Covers
argument parsing, the tag-injection guard, the gh preflight, check-banner emission, the grep helpers and the range
resolution.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
import run_audit_checks as rac

_BANNERS = [
    "--- check: gh-auth ---",
    "--- check: repo-state ---",
    "--- check: ci-health ---",
    "--- check: open-issues-prs ---",
    "--- check: docs-alignment ---",
    "--- check: version-consistency ---",
    "--- check: code-signals ---",
    "--- check: end ---",
]


def _dispatch(responses: dict[str, tuple[int, str]]) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a ``subprocess.run`` stub dispatching on ``<binary> <first subcommand>``.

    The first pattern contained in that key wins; an unmatched command succeeds with empty output.
    """

    def _fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Return the canned response for one command."""
        key = f"{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''}".strip()
        for pattern, (rc, out) in responses.items():
            if pattern in key:
                return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return _fake_run


def _happy_dispatch() -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a ``subprocess.run`` stub that makes every check pass."""
    return _dispatch(
        {
            "gh auth": (0, "Logged in to github.com\n"),
            "git status": (0, ""),
            "git log": (0, "abc123 some commit\n"),
            "git rev-parse": (0, "main"),
            "git diff": (0, ""),
            "git describe": (0, "v1.0.0"),
            "git remote": (0, "HEAD branch: main\n"),
            "gh run": (0, "[]"),
            "gh issue": (0, "[]"),
            "gh pr": (0, "[]"),
        }
    )


def _write_numbered_files(root: Path, prefix: str, template: str, count: int) -> None:
    """Write ``count`` files ``<prefix><i>.py`` under ``root``, each holding ``template`` with ``{i}`` filled in."""
    for i in range(count):
        (root / f"{prefix}{i}.py").write_text(template.format(i=i))


def _which_without(missing: str) -> Callable[[str], str | None]:
    """Build a ``which`` stub that finds every tool except ``missing``."""
    return lambda cmd: None if cmd == missing else f"/fake/{cmd}"


@pytest.fixture
def repo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the test from inside ``tmp_path``, so scans only see files the test writes."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def audit(repo_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Patch ``which`` and ``subprocess.run`` so every check passes; expose both mocks to retune per test.

    ``LAST_TAG`` is cleared so the ambient environment cannot change the release range.
    """
    monkeypatch.delenv("LAST_TAG", raising=False)
    with (
        mock.patch.object(rac, "which", autospec=True, side_effect=_which_without("")) as which,
        mock.patch.object(rac.subprocess, "run", autospec=True, side_effect=_happy_dispatch()) as run,
    ):
        yield SimpleNamespace(which=which, run=run)


class TestMainArgs:
    """Main: argument parsing and the tag-injection guard."""

    def test_unknown_arg_exits_1(self, audit: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
        """Unrecognized flag → exit 1 with 'unknown arg' on stderr."""
        rc = rac.main(["--unknown"])
        assert (rc, "unknown arg" in capsys.readouterr().err) == (1, True)

    def test_accepts_repository_tag_and_range(self, audit: SimpleNamespace) -> None:
        """Repository, tag and range options are accepted together."""
        assert rac.main(["--repo", "owner/repo", "--tag", "v1.0.0", "--range", "v0.9.0..HEAD"]) == 0

    @pytest.mark.parametrize("bad_tag", ["-injected", "--injected", "-x"])
    def test_rejects_tag_starting_with_dash(
        self,
        audit: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        bad_tag: str,
    ) -> None:
        """``LAST_TAG`` starting with '-' → exit 2 with 'invalid tag' on stderr.

        The tag reaches ``git`` as an argument, where a leading dash would be read as an option rather than a ref.
        """
        monkeypatch.setenv("LAST_TAG", bad_tag)
        audit.run.side_effect = _dispatch({})
        rc = rac.main([])
        assert (rc, "invalid tag" in capsys.readouterr().err) == (2, True)


class TestGhPreflight:
    """Main: the audit cannot proceed without an installed, authenticated ``gh``."""

    @pytest.mark.parametrize(
        ("which_side_effect", "responses"),
        [
            pytest.param(_which_without("gh"), {"git describe": (0, "v1.0.0")}, id="gh-not-on-path"),
            pytest.param(_which_without(""), {"gh auth": (1, "not logged in")}, id="gh-not-authenticated"),
        ],
    )
    def test_exits_2_when_gh_is_unusable(
        self,
        audit: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        which_side_effect: Callable[[str], str | None],
        responses: dict[str, tuple[int, str]],
    ) -> None:
        """``gh`` missing from PATH, or present but not logged in → exit 2.

        Every later check reads GitHub, so continuing without a working ``gh`` would report empty results as clean.
        """
        monkeypatch.setenv("LAST_TAG", "v1.0.0")
        audit.which.side_effect = which_side_effect
        audit.run.side_effect = _dispatch(responses)
        assert rac.main(["--range", "v1.0.0..HEAD"]) == 2


class TestOutput:
    """Main: what the audit prints."""

    def test_emits_every_check_banner(self, audit: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
        """All mocked tools pass → every check banner and the end banner are emitted.

        The release skill splits the output on these banners, so a missing one hides a whole check from the report.
        """
        rc = rac.main(["--range", "v1.0.0..HEAD"])
        out = capsys.readouterr().out
        assert (rc, [banner for banner in _BANNERS if banner not in out]) == (0, [])

    def test_includes_requested_tag_in_version_section(
        self, audit: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The requested tag appears in the version-consistency output."""
        rac.main(["--range", "v1.0.0..HEAD", "--tag", "v2.0.0"])
        assert "v2.0.0" in capsys.readouterr().out

    def test_reports_missing_pip_audit_with_greppable_signal(
        self, audit: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing audit dependency is reported on a greppable line without failing the overall command.

        Check 6 turns this signal into the install-or-skip question (see ``templates/audit-checks.md``), so it must be
        emitted exactly when ``pip-audit`` is absent.
        """
        audit.which.side_effect = _which_without("pip-audit")
        rc = rac.main(["--range", "v1.0.0..HEAD"])
        out = capsys.readouterr().out
        assert (rc, rac.PIP_AUDIT_MISSING_SIGNAL in out, "install with: pip install pip-audit" in out) == (
            0,
            True,
            True,
        )

    def test_omits_pip_audit_signal_when_available(
        self, audit: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The missing-tool signal is absent when the audit dependency is available."""
        rc = rac.main(["--range", "v1.0.0..HEAD"])
        assert (rc, rac.PIP_AUDIT_MISSING_SIGNAL in capsys.readouterr().out) == (0, False)


class TestRangeResolution:
    """Main: the release range comes from ``--range``, ``LAST_TAG`` or the initial commit."""

    def test_uses_last_tag_env_when_no_range_given(
        self, audit: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``LAST_TAG`` forms the range left side when ``--range`` is not supplied."""
        monkeypatch.setenv("LAST_TAG", "v0.5.0")
        rc = rac.main([])
        assert (rc, "v0.5.0..HEAD" in capsys.readouterr().out) == (0, True)

    def test_falls_back_to_initial_commit_without_tags(
        self, audit: SimpleNamespace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No tags at all → the initial commit SHA is the range left side.

        A brand-new repository has nothing to diff against, so the audit must still cover its whole history.
        """
        audit.run.side_effect = _dispatch(
            {"git describe": (1, ""), "git rev-list": (0, "deadbeef\n"), "gh auth": (0, "Logged in\n")}
        )
        rc = rac.main([])
        assert (rc, "deadbeef" in capsys.readouterr().out) == (0, True)

    @pytest.mark.parametrize(
        ("remote_response", "expected"),
        [
            pytest.param((0, "  HEAD branch: develop\n"), "develop", id="parses-head-branch"),
            pytest.param(
                (0, "  origin  https://example.com (fetch)\n"), "main", id="no-head-branch-line-falls-back-to-main"
            ),
            pytest.param((1, ""), "main", id="remote-failure-falls-back-to-main"),
        ],
    )
    @mock.patch("run_audit_checks.subprocess.run", autospec=True)
    def test_detect_trunk(self, run: mock.MagicMock, remote_response: tuple[int, str], expected: str) -> None:
        """'HEAD branch: develop' in remote output → 'develop'; no such line, or a failing remote show → 'main'."""
        run.side_effect = _dispatch({"git remote": remote_response})
        assert rac._detect_trunk("/fake/git") == expected


class TestGrepScans:
    """_grep_version_files / _grep_code_signals: bounded scans of the checked-out tree.

    These write real files into ``tmp_path`` and scan them: the file walk is the behaviour under test.
    """

    @pytest.mark.parametrize(
        ("scan", "filename", "content", "needle"),
        [
            pytest.param(rac._grep_version_files, "mod.py", '__version__ = "1.0.0"\n', "__version__", id="version-py"),
            pytest.param(
                rac._grep_version_files,
                "pyproject.toml",
                '[project]\nversion = "0.1.0"\n',
                "version",
                id="version-toml",
            ),
            pytest.param(
                rac._grep_code_signals, "src.py", "# FIXME: clean this up\nx = 1\n", "FIXME", id="signal-fixme"
            ),
            pytest.param(
                rac._grep_code_signals,
                "src.py",
                "# TODO before release: update changelog\n",
                "TODO",
                id="signal-todo-release",
            ),
        ],
    )
    def test_finds_match(self, repo_dir: Path, scan: Any, filename: str, content: str, needle: str) -> None:
        """A Python ``__version__`` or TOML ``version =`` line, and a FIXME or release-TODO comment, are returned."""
        (repo_dir / filename).write_text(content)
        assert any(needle in result for result in scan())

    @pytest.mark.parametrize(
        ("scan", "dirname", "filename", "content", "needle"),
        [
            pytest.param(
                rac._grep_version_files, ".git", "config.py", '__version__ = "0.0.1"\n', ".git", id="version-git-dir"
            ),
            pytest.param(
                rac._grep_code_signals, "tests", "test_x.py", "# FIXME in test\n", "FIXME", id="signals-tests-dir"
            ),
        ],
    )
    def test_excludes_directory(
        self, repo_dir: Path, scan: Any, dirname: str, filename: str, content: str, needle: str
    ) -> None:
        """Files inside ``.git/`` are not scanned for versions, and files inside ``tests/`` not for code signals."""
        (repo_dir / dirname).mkdir()
        (repo_dir / dirname / filename).write_text(content)
        assert not any(needle in result for result in scan())

    @pytest.mark.parametrize(
        ("scan", "limit_name", "prefix", "content"),
        [
            pytest.param(
                rac._grep_version_files, "_MAX_VERSION_LINES", "m", '__version__ = "{i}"\n', id="version-lines"
            ),
            pytest.param(rac._grep_code_signals, "_MAX_SIGNAL_LINES", "s", "# FIXME item {i}\n", id="signal-lines"),
        ],
    )
    def test_caps_result_lines(self, repo_dir: Path, scan: Any, limit_name: str, prefix: str, content: str) -> None:
        """Results are capped at ``_MAX_VERSION_LINES`` / ``_MAX_SIGNAL_LINES``."""
        limit = getattr(rac, limit_name)
        _write_numbered_files(repo_dir, prefix, content, limit + 5)
        assert len(scan()) <= limit

    @pytest.mark.parametrize("scan", [rac._grep_version_files, rac._grep_code_signals])
    def test_returns_empty_list_without_matches(self, repo_dir: Path, scan: Any) -> None:
        """An empty tree gives an empty list, never ``None`` or an exception."""
        assert scan() == []

    @pytest.mark.parametrize(
        ("scan", "prefix", "content"),
        [
            pytest.param(rac._grep_version_files, "m", '__version__ = "{i}"\n', id="version-files"),
            pytest.param(rac._grep_code_signals, "s", "# FIXME item {i}\n", id="code-signals"),
        ],
    )
    def test_signals_file_count_truncation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        repo_dir: Path,
        capsys: pytest.CaptureFixture[str],
        scan: Any,
        prefix: str,
        content: str,
    ) -> None:
        """Hitting the file-count cap prints SCAN_TRUNCATED_SIGNAL and still returns a bounded list.

        The cap is lowered via monkeypatch so the test only needs a handful of fixture files, not thousands, to exercise
        the truncation path.
        """
        monkeypatch.setattr(rac, "_MAX_SCAN_FILES", 2)
        _write_numbered_files(repo_dir, prefix, content, 4)
        results = scan()
        assert len(results) <= 2
        assert rac.SCAN_TRUNCATED_SIGNAL in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("scan", "content"),
        [
            pytest.param(
                rac._grep_version_files, '__version__ = "0.0.0"  # padding beyond the size cap\n', id="version-files"
            ),
            pytest.param(rac._grep_code_signals, "# FIXME padding beyond the size cap\n", id="code-signals"),
        ],
    )
    def test_skips_oversized_file(
        self, monkeypatch: pytest.MonkeyPatch, repo_dir: Path, scan: Any, content: str
    ) -> None:
        """A file above ``_MAX_SCAN_FILE_SIZE`` is never read, even if it would otherwise match."""
        monkeypatch.setattr(rac, "_MAX_SCAN_FILE_SIZE", 10)
        (repo_dir / "huge.py").write_text(content)
        assert scan() == []


class TestIntroducedDependencies:
    """_introduced_dependencies: which dependencies the release range touched."""

    @pytest.mark.parametrize(
        ("diff", "expected"),
        [
            pytest.param(
                '+++ b/pyproject.toml\n+    "Foo_Bar>=1",\n+    "baz[x]==2",', {"foo-bar", "baz"}, id="pyproject-added"
            ),
            pytest.param('+++ b/pyproject.toml\n-    "gone>=1",', set(), id="pyproject-removed-ignored"),
            pytest.param("+++ b/requirements.txt\n+requests>=2\n+rich", {"requests", "rich"}, id="requirements-plain"),
            pytest.param(
                '+++ b/uv.lock\n name = "pkg"\n-version = "1"\n+version = "2"', {"pkg"}, id="lock-version-bump"
            ),
            pytest.param('+++ b/uv.lock\n+name = "newdep"\n+version = "1"', {"newdep"}, id="lock-new-package"),
            pytest.param(
                '+++ b/uv.lock\n name = "pkg"\n version = "1"\n+    { name = "x" },',
                set(),
                id="lock-dependency-edit-not-bump",
            ),
            pytest.param("", set(), id="empty-diff"),
        ],
    )
    def test_reports_added_or_bumped_dependencies(self, diff: str, expected: set[str]) -> None:
        """Added or bumped dependencies are reported; removals and unrelated lock edits are not.

        The release audit treats a vulnerable package in this set as a red flag and any other as a warning, so a missed
        addition would silently downgrade a regression and a false hit would block a clean release.
        """
        assert rac._introduced_dependencies(diff) == expected


_REAL_RUN = subprocess.run
_VULN_JSON = '{"dependencies": [{"name": "requests", "version": "2.19.0", "vulns": [{"id": "PYSEC-2018-28"}]}]}'
_SHIPPED_PYPROJECT = '[project]\nname = "demo"\nversion = "1"\ndependencies = ["requests==2.19.0"]\n'
_EXTRAS_PYPROJECT = _SHIPPED_PYPROJECT + '[project.optional-dependencies]\nextra1 = ["pyyaml==5.3"]\ndev = ["pytest"]\n'
_DYNAMIC_PYPROJECT = '[project]\nname = "demo"\nversion = "1"\ndynamic = ["dependencies"]\n'


def _cve_dispatch(
    calls: list[list[str]],
    *,
    uv_rc: int = 0,
    audit_rc: int = 1,
    audit_out: str = _VULN_JSON,
    timeout_on: str = "",
    requirements: list[str] | None = None,
    lock_text: str = "[[packages]]\n",
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a ``subprocess.run`` stub for the CVE scan that records every command in ``calls``.

    ``pip-audit`` exits 1 by default, as the real tool does whenever it finds a vulnerability. A successful ``uv`` run
    writes the ``-o`` file it is asked for, and the contents of a ``-r`` requirements file are appended to
    ``requirements`` at call time (the scan deletes its temp directory afterwards). The tool named by ``timeout_on``
    raises ``TimeoutExpired``. The summary parser runs for real so the printed scan line is the production one.
    """

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Answer uv, pip-audit and git with canned output; run everything else for real."""
        calls.append(list(cmd))
        name = Path(cmd[0]).name
        if name == timeout_on:
            raise subprocess.TimeoutExpired(cmd, 1)
        if name == "uv" and uv_rc == 0:
            Path(cmd[cmd.index("-o") + 1]).write_text(lock_text)
        if name == "pip-audit" and requirements is not None and "-r" in cmd:
            requirements.append(Path(cmd[cmd.index("-r") + 1]).read_text())
        canned = {
            "uv": (uv_rc, ""),
            "pip-audit": (audit_rc, audit_out),
            "git": (0, ""),
        }.get(name)
        return (
            subprocess.CompletedProcess(cmd, *canned, stderr=f"{name} failed") if canned else _REAL_RUN(cmd, **kwargs)
        )

    return _fake_run


class TestDependencyCveScan:
    """_check_code_signals: the CVE scan covers the dependency set the release ships, never the host environment."""

    def test_scans_locked_export_and_keeps_findings_on_exit_one(
        self, audit: SimpleNamespace, repo_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A vulnerable locked dependency is reported although pip-audit exits 1 for it.

        pip-audit signals "vulnerabilities found" with exit 1 and still prints complete JSON, so treating a non-zero
        exit as a parse failure discards exactly the findings a release audit exists to surface.
        """
        (repo_dir / "pyproject.toml").write_text(_SHIPPED_PYPROJECT)
        (repo_dir / "uv.lock").write_text("")
        calls: list[list[str]] = []
        audit.run.side_effect = _cve_dispatch(calls)
        rac._check_code_signals("git", "v1.0.0..HEAD")
        out = capsys.readouterr().out
        audit_cmd = next(c for c in calls if Path(c[0]).name == "pip-audit")
        assert ("--locked" in audit_cmd, "-r" in audit_cmd, "1 vulns" in out, "JSON parsing failed" in out) == (
            True,
            False,
            True,
            False,
        )

    def test_reports_clean_for_a_lock_without_runtime_packages(
        self, audit: SimpleNamespace, repo_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A project with no runtime dependencies reads clean, and pip-audit never sees the package-less lock.

        pip-audit errors on a lockfile with no packages ("missing packages in lockfile"), which would turn a project
        that ships nothing into a failed scan on every release.
        """
        (repo_dir / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1"\n')
        (repo_dir / "uv.lock").write_text("")
        calls: list[list[str]] = []
        audit.run.side_effect = _cve_dispatch(calls, lock_text="packages = []\n")
        rac._check_code_signals("git", "v1.0.0..HEAD")
        ran_pip_audit = any(Path(c[0]).name == "pip-audit" for c in calls)
        assert (ran_pip_audit, "0 deps, 0 vulns" in capsys.readouterr().out) == (False, True)

    def test_exports_shipped_extras_without_dev_groups(self, audit: SimpleNamespace, repo_dir: Path) -> None:
        """The locked export is current, marker-independent and limited to shipped groups.

        A shipped extra missing from the export would hide its vulnerabilities; a dev or default group in it would turn
        test and docs tooling into release findings; a stale lock would audit a different set than the release ships; a
        requirements-format export would drop packages whose markers do not match the auditing machine.
        """
        (repo_dir / "pyproject.toml").write_text(_EXTRAS_PYPROJECT)
        (repo_dir / "uv.lock").write_text("")
        calls: list[list[str]] = []
        audit.run.side_effect = _cve_dispatch(calls)
        rac._check_code_signals("git", "v1.0.0..HEAD")
        export_cmd = next(c for c in calls if Path(c[0]).name == "uv")
        flags = {"--locked", "--no-dev", "--no-default-groups", "--extra=extra1", "pylock.toml"}
        assert (flags <= set(export_cmd), "--frozen" in export_cmd, "--extra=dev" in export_cmd) == (True, False, False)

    def test_audits_declared_requirements_without_lock(
        self, audit: SimpleNamespace, repo_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without ``uv.lock`` the core and non-dev extra requirements are audited, not the installed packages.

        A bare ``pip-audit`` call inspects whichever environment the tool runs in, and its own project mode ignores
        extras, so the declared requirements are handed over as a file.
        """
        (repo_dir / "pyproject.toml").write_text(_EXTRAS_PYPROJECT)
        calls: list[list[str]] = []
        requirements: list[str] = []
        audit.run.side_effect = _cve_dispatch(calls, requirements=requirements)
        rac._check_code_signals("git", "v1.0.0..HEAD")
        out = capsys.readouterr().out
        assert (requirements, "scan-target: pyproject.toml" in out) == (["requests==2.19.0\npyyaml==5.3\n"], True)

    @pytest.mark.parametrize(
        ("pyproject", "uv_rc", "reason"),
        [
            pytest.param("", 0, "no readable [project] table", id="no-project-table"),
            pytest.param(_SHIPPED_PYPROJECT, 1, "uv export of uv.lock failed (uv failed)", id="uv-export-fails"),
        ],
    )
    def test_skips_scan_when_shipped_set_is_unavailable(
        self,
        audit: SimpleNamespace,
        repo_dir: Path,
        capsys: pytest.CaptureFixture[str],
        pyproject: str,
        uv_rc: int,
        reason: str,
    ) -> None:
        """No readable shipped set means an announced skip with its cause, never a scan of the host environment.

        Scanning whatever happens to be installed would report vulnerabilities in packages the release never ships and
        miss the ones it does.
        """
        (repo_dir / "pyproject.toml").write_text(pyproject)
        (repo_dir / "uv.lock").write_text("")
        calls: list[list[str]] = []
        audit.run.side_effect = _cve_dispatch(calls, uv_rc=uv_rc)
        rac._check_code_signals("git", "v1.0.0..HEAD")
        ran_pip_audit = any(Path(c[0]).name == "pip-audit" for c in calls)
        assert (ran_pip_audit, f"scan-skipped: {reason}" in capsys.readouterr().out.replace("; ", ";")) == (False, True)

    def test_skips_unlocked_scan_for_dynamic_dependencies(
        self, audit: SimpleNamespace, repo_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Dynamic dependencies cannot be read statically, so nothing is scanned rather than an empty set reading
        clean."""
        (repo_dir / "pyproject.toml").write_text(_DYNAMIC_PYPROJECT)
        calls: list[list[str]] = []
        audit.run.side_effect = _cve_dispatch(calls)
        rac._check_code_signals("git", "v1.0.0..HEAD")
        ran_pip_audit = any(Path(c[0]).name == "pip-audit" for c in calls)
        assert (ran_pip_audit, "scan-skipped: pyproject.toml dependencies are dynamic" in capsys.readouterr().out) == (
            False,
            True,
        )

    def test_skips_scan_when_lock_exists_but_uv_is_missing(
        self, audit: SimpleNamespace, repo_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A lock that cannot be read is not replaced by an unlocked scan of different versions.

        Resolving the declared requirements would audit the newest allowed versions, so a vulnerable pinned version
        would read clean.
        """
        (repo_dir / "pyproject.toml").write_text(_SHIPPED_PYPROJECT)
        (repo_dir / "uv.lock").write_text("")
        audit.which.side_effect = lambda cmd: None if cmd == "uv" else f"/fake/{cmd}"
        calls: list[list[str]] = []
        audit.run.side_effect = _cve_dispatch(calls)
        rac._check_code_signals("git", "v1.0.0..HEAD")
        ran_pip_audit = any(Path(c[0]).name == "pip-audit" for c in calls)
        assert (ran_pip_audit, "scan-skipped: uv.lock present but uv not installed" in capsys.readouterr().out) == (
            False,
            True,
        )

    def test_skips_scan_when_pip_audit_times_out(
        self, audit: SimpleNamespace, repo_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A timed-out scan step is announced as a skip instead of aborting the whole audit with a traceback.

        The CVE scan is the last data-gathering check, so an uncaught timeout would also drop the closing banner the
        release agent waits for.
        """
        (repo_dir / "pyproject.toml").write_text(_SHIPPED_PYPROJECT)
        (repo_dir / "uv.lock").write_text("")
        audit.run.side_effect = _cve_dispatch([], timeout_on="pip-audit")
        rac._check_code_signals("git", "v1.0.0..HEAD")
        assert "scan-skipped: pip-audit timed out after 120s" in capsys.readouterr().out

    def test_reports_failed_scan_when_pip_audit_crashes(
        self, audit: SimpleNamespace, repo_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An exit code above 1 with no JSON is a failed scan carrying pip-audit's own reason, not a clean one."""
        (repo_dir / "pyproject.toml").write_text(_SHIPPED_PYPROJECT)
        (repo_dir / "uv.lock").write_text("")
        audit.run.side_effect = _cve_dispatch([], audit_rc=2, audit_out="")
        rac._check_code_signals("git", "v1.0.0..HEAD")
        assert "JSON parsing failed (exit 2: pip-audit failed)" in capsys.readouterr().out
