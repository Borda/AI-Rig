"""Tests for the ``codemap_changed_modules`` bin script.

Git behaviour runs against a real temporary repository. The ``codemap-py`` index is replaced by a small payload in the
shape of ``codemap-py query central``, so the tests never need the provider installed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import codemap_changed_modules as ccm
import pytest

#: ``codemap-py query central`` payload covering ``pkg/__init__.py`` and ``pkg/new.py`` only.
_CENTRAL = {
    "central": [
        {"name": "pkg", "rdep_count": 3, "path": "pkg/__init__.py"},
        {"name": "pkg.new", "rdep_count": 1, "path": "pkg/new.py"},
    ]
}


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a fixed identity and no signing; return stripped stdout."""
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    ).stdout.strip()


def _commit(repo: Path, message: str, files: dict[str, str]) -> None:
    """Write ``files`` (relative path to content), stage everything and commit."""
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture(name="repo")
def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three commits: base, a middle commit adding ``pkg/mid.py``, and a work commit of mixed file types.

    The work commit edits ``pkg/__init__.py``, adds ``pkg/new.py`` and ``pkg/a,b.py`` (a comma in the name), and adds a
    Markdown file. The working directory moves into the repository because the script, like the inline blocks it
    replaces, reads the repository it is run in.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "base", {"pkg/__init__.py": "a = 1\n", "README.md": "x\n"})
    _commit(repo, "mid", {"pkg/mid.py": "m = 1\n"})
    _commit(
        repo,
        "work",
        {"pkg/__init__.py": "a = 2\n", "pkg/new.py": "n = 1\n", "pkg/a,b.py": "c = 1\n", "notes.md": "n\n"},
    )
    monkeypatch.chdir(repo)
    return repo


@pytest.mark.integration
class TestGitDiff:
    """Finding the comparison point and the changed Python files in a real repository."""

    def test_defaults_to_origin_main_without_a_remote_head(self, repo: Path) -> None:
        """With no ``origin/HEAD`` the base falls back to ``origin/main``."""
        assert ccm.default_base() == "origin/main"

    def test_detects_the_remotes_default_branch(self, repo: Path) -> None:
        """A remote whose default branch is not ``main`` is followed instead of assuming ``origin/main``."""
        _git(repo, "update-ref", "refs/remotes/origin/trunk", "HEAD")
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        assert ccm.default_base() == "origin/trunk"

    def test_comparison_point_is_head_parent_without_the_base_ref(self, repo: Path) -> None:
        """A missing base ref falls back to ``HEAD~1``, so a lone feature commit is still diffed."""
        assert ccm.comparison_point("origin/main") == _git(repo, "rev-parse", "HEAD~1")

    def test_comparison_point_is_the_merge_base_when_the_ref_exists(self, repo: Path) -> None:
        """An existing base ref wins over ``HEAD~1``: the diff then covers every commit since the branch point."""
        base = _git(repo, "rev-parse", "HEAD~2")
        _git(repo, "update-ref", "refs/remotes/origin/main", base)
        assert ccm.comparison_point("origin/main") == base

    @pytest.mark.parametrize(
        ("base_ref", "expected"),
        [
            pytest.param(None, ["pkg/__init__.py", "pkg/a,b.py", "pkg/new.py"], id="head-parent-only"),
            pytest.param(
                "HEAD~2",
                ["pkg/__init__.py", "pkg/a,b.py", "pkg/mid.py", "pkg/new.py"],
                id="since-branch-point",
            ),
        ],
    )
    def test_lists_only_changed_python_files(self, repo: Path, base_ref: str | None, expected: list[str]) -> None:
        """Markdown is dropped and a comma in a file name survives, because paths are read NUL-separated.

        The comma is what the old ``paste -sd,`` hand-off to ``resolve_centrality --files`` could not carry.
        """
        point = _git(repo, "rev-parse", base_ref or "HEAD~1")
        assert ccm.changed_python_files(point) == expected


@pytest.mark.integration
class TestChangedModules:
    """End to end from the diff to module names, with a stubbed index."""

    def test_names_come_from_the_index_in_changed_file_order(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``pkg/__init__.py`` resolves to ``pkg`` (the index name), not ``pkg.__init__``; unindexed files drop out.

        ``pkg/a,b.py`` is changed but absent from the index, so it contributes nothing instead of a guessed name.
        """
        monkeypatch.setattr(ccm.shutil, "which", lambda _name: "codemap-py")
        monkeypatch.setattr(ccm, "_central_payload", lambda _codemap: _CENTRAL)
        assert ccm.changed_modules() == ["pkg", "pkg.new"]

    def test_cap_limits_the_output(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """At most ``cap`` names come back, keeping the first in changed-file order."""
        monkeypatch.setattr(ccm.shutil, "which", lambda _name: "codemap-py")
        monkeypatch.setattr(ccm, "_central_payload", lambda _codemap: _CENTRAL)
        assert ccm.changed_modules(cap=1) == ["pkg"]

    def test_nothing_changed_skips_the_index_query(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When HEAD already equals the base there is nothing to resolve and the index is never queried."""
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        monkeypatch.setattr(ccm.shutil, "which", lambda _name: "codemap-py")

        def _never(_codemap: str) -> object:
            raise AssertionError("index queried although no Python file changed")

        monkeypatch.setattr(ccm, "_central_payload", _never)
        assert ccm.changed_modules() == []

    @pytest.mark.parametrize(
        "payload",
        [pytest.param(None, id="query-failed"), pytest.param(["not", "a", "dict"], id="unexpected-shape")],
    )
    def test_unusable_index_payload_yields_nothing(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, payload: object
    ) -> None:
        """A failed or malformed index query prints nothing, so the agent's per-module loop simply does not run."""
        monkeypatch.setattr(ccm.shutil, "which", lambda _name: "codemap-py")
        monkeypatch.setattr(ccm, "_central_payload", lambda _codemap: payload)
        assert ccm.changed_modules() == []


def test_absent_codemap_cli_yields_nothing_without_touching_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``codemap-py`` on PATH the script is a silent no-op, matching the old ``command -v`` guard."""
    monkeypatch.setattr(ccm.shutil, "which", lambda _name: None)

    def _never(_base: str) -> str:
        raise AssertionError("git consulted although codemap-py is absent")

    monkeypatch.setattr(ccm, "comparison_point", _never)
    assert ccm.changed_modules() == []


class TestCentralPayload:
    """Parsing the ``codemap-py query central`` output."""

    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [
            pytest.param('{"central": []}', {"central": []}, id="valid-json"),
            pytest.param("not json", None, id="invalid-json"),
            pytest.param(None, None, id="command-failed"),
            pytest.param("", None, id="empty-output"),
        ],
    )
    def test_parses_or_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str | None, expected: object
    ) -> None:
        """Only well-formed JSON is returned; every other outcome is None, never an exception."""
        monkeypatch.setattr(ccm, "_run", lambda _command, _timeout: stdout)
        assert ccm._central_payload("codemap-py") == expected


def test_run_returns_none_for_a_command_that_cannot_start() -> None:
    """A missing executable is reported as None instead of raising, so callers stay silent."""
    assert ccm._run(["definitely-not-an-installed-command-xyz"], 5) is None


class TestMain:
    """The command line."""

    def test_prints_one_module_per_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Each resolved module is printed on its own line, ready for an unquoted ``for`` loop."""
        monkeypatch.setattr(ccm, "changed_modules", lambda base, cap: ["pkg", "pkg.new"])
        assert ccm.main([]) == 0
        assert capsys.readouterr().out == "pkg\npkg.new\n"

    def test_forwards_base_and_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--base`` and ``--cap`` reach the resolver unchanged."""
        seen: list[tuple[str | None, int]] = []
        monkeypatch.setattr(ccm, "changed_modules", lambda base, cap: seen.append((base, cap)) or [])
        ccm.main(["--base", "origin/trunk", "--cap", "3"])
        assert seen == [("origin/trunk", 3)]

    @pytest.mark.parametrize("cap", [pytest.param("0", id="zero"), pytest.param("-2", id="negative")])
    def test_rejects_a_cap_below_one(self, cap: str) -> None:
        """A cap that would print nothing is a usage error, not a silent empty result."""
        with pytest.raises(SystemExit) as excinfo:
            ccm.main(["--cap", cap])
        assert excinfo.value.code == 2
