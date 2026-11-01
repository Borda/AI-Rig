"""Regression tests for bin/codemap_resolve.py — the consumer codemap gate resolver.

Each test pins one audit finding's failure mode:

* The index directory defaulted to a CWD-relative ``.cache/codemap``, so a skill
  invoked from a repository subdirectory reported a false ``no_index``.
* The project name was ASCII-sanitized, so any repository directory containing a
  space, ``+`` or a non-ASCII character resolved to a filename the scanner never writes.
* The currency probe ran untimed, twice per gate, and discarded every ``stale``
  verdict because it gated on the exit code that *carries* that verdict.
* One canonical resolver, byte-identical across plugins, with a single ``codemap-py``
  label and no plugin-specific sentinel name baked in.

The module is loaded by explicit path rather than imported by name: both cc_develop and
cc_research ship a ``codemap_resolve`` module, and a plain import would let whichever
plugin's ``bin/`` reached ``sys.path`` first answer for both.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from _develop_text import load_module

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEV_BIN = _REPO_ROOT / "plugins" / "cc_develop" / "bin"
_DEV_RESOLVER = _DEV_BIN / "codemap_resolve.py"


resolver = load_module(_DEV_RESOLVER, "dev_codemap_resolve_under_test")


def _git_repo(tmp_path: Path, name: str) -> Path:
    """Create an initialized git repository named *name* under *tmp_path*."""
    root = tmp_path / name
    (root / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _write_index(root: Path, project: str) -> Path:
    """Write a placeholder index for *project* under *root*'s default cache directory.

    Examples:
        >>> root = getfixture("tmp_path")
        >>> path = _write_index(root, "demo")
        >>> path.relative_to(root).as_posix()
        '.cache/codemap/demo.json'
    """
    index = root / ".cache" / "codemap" / f"{project}.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("{}")
    return index


def _write_fake_check_currency(path: Path, status: str, reason: str, exit_code: int) -> Path:
    """Write a stand-in ``check-index-currency`` that prints one JSON verdict and exits with *exit_code*."""
    path.write_text(
        f"import json, sys\nprint(json.dumps({{'status': '{status}', 'reason': '{reason}'}}))\nsys.exit({exit_code})\n"
    )
    return path


def _symlinks_supported() -> bool:
    """Probe whether this host lets the process create symlinks (native Windows often does not)."""
    with tempfile.TemporaryDirectory() as probe:
        try:
            (Path(probe) / "link").symlink_to(Path(probe))
        except OSError:
            return False
    return True


_requires_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="symlink creation unavailable on this host")


@pytest.fixture(name="sentinel_session")
def _sentinel_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``TMPDIR`` at ``tmp_path`` and fix the session token to ``sess``; return the sentinel directory."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "sess")
    return tmp_path


class TestIndexPath:
    """The index file is located from the git toplevel with the raw directory basename."""

    def test_found_from_repository_subdirectory(self, tmp_path, monkeypatch):
        """A skill run from a subdir resolves the root's index, not a CWD-relative miss.

        Real ``git init`` is the contract here: the toplevel is what the provider anchors on, and the pre-fix code
        looked under ``.cache/codemap`` relative to the CWD, which does not exist in a subdirectory.
        """
        root = _git_repo(tmp_path, "proj")
        index = _write_index(root, "proj")
        monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
        monkeypatch.chdir(root / "pkg")

        resolved = resolver._index_path(resolver._canonical_root())

        assert resolved == index.resolve()
        assert resolved.is_file()
        assert not (Path.cwd() / ".cache" / "codemap" / "proj.json").exists()

    def test_override_dir_is_flat(self, tmp_path, monkeypatch):
        """``CODEMAP_INDEX_DIR`` resolves to a flat ``<override>/<project>.json``, matching the provider."""
        root = _git_repo(tmp_path, "proj")
        override = tmp_path / "shared-cache"
        override.mkdir()
        monkeypatch.setenv("CODEMAP_INDEX_DIR", str(override))
        monkeypatch.chdir(root)

        assert resolver._index_path(resolver._canonical_root()) == (override / "proj.json").resolve()

    @mock.patch.object(
        resolver.subprocess, "run", autospec=True, return_value=subprocess.CompletedProcess([], 128, stdout="")
    )
    def test_root_falls_back_to_cwd_outside_repository(self, mock_run, tmp_path, monkeypatch):
        """Outside a repo the root is the CWD, mirroring the provider, never a literal ``default``.

        ``git rev-parse`` is mocked to fail with 128 so the fallback is exercised without depending on where the host
        keeps its temp directory.
        """
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        monkeypatch.chdir(outside)

        assert resolver._canonical_root() == outside.resolve()

    @pytest.mark.parametrize("name", ["café", "my+proj", "two words", "ünïcode+dir name"])
    def test_project_name_is_the_raw_basename(self, tmp_path, monkeypatch, name):
        """The scanner writes the directory basename verbatim; stripping characters yields a false ``no_index``."""
        root = _git_repo(tmp_path, name)
        index = _write_index(root, name)
        monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
        monkeypatch.chdir(root)

        resolved = resolver._index_path(resolver._canonical_root())

        assert resolved.name == f"{name}.json"
        assert resolved == index.resolve()
        assert resolved.is_file()


class TestCurrency:
    """One timed currency call; the verdict is read from stdout, never the exit code."""

    @pytest.mark.parametrize(
        ("status", "reason", "exit_code"),
        [
            # check-index-currency exits 1 *because* the index is stale — that is not a failure.
            pytest.param("stale", "HEAD changed", 1, id="stale-verdict-with-exit-1"),
            # exit 2 carries a no_index verdict; it must reach the caller, not be coerced to current.
            pytest.param("no_index", "gone", 2, id="no-index-verdict-with-exit-2"),
        ],
    )
    def test_verdict_survives_nonzero_exit_code(self, tmp_path, status, reason, exit_code):
        """The verdict is read from stdout, so the probe's non-zero exit code never turns it into a failure.

        A stale index exits 1 and a missing one exits 2; both verdicts must reach the caller with their reason intact.
        The probe is a real child process because the exit-code-versus-stdout split is the contract.
        """
        fake = _write_fake_check_currency(tmp_path / "fake_cic.py", status, reason, exit_code)

        assert resolver._currency(str(fake), tmp_path / "index.json") == (status, reason)

    @mock.patch.object(
        resolver.subprocess,
        "run",
        autospec=True,
        return_value=subprocess.CompletedProcess(["cic"], 1, stdout=json.dumps({"status": "stale", "reason": "r"})),
    )
    def test_probe_makes_one_bounded_call(self, mock_run, tmp_path):
        """A single timed subprocess yields both fields; the old ``--field`` form spawned two, untimed."""
        assert resolver._currency("cic", tmp_path / "index.json") == ("stale", "r")

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["timeout"] == resolver._CURRENCY_TIMEOUT_S
        assert "--field" not in mock_run.call_args.args[0]

    @mock.patch.object(resolver.subprocess, "run", autospec=True, side_effect=subprocess.TimeoutExpired("cic", 1))
    def test_probe_fails_open_when_check_hangs(self, mock_run, tmp_path):
        """A timed-out verdict reads as ``current`` so a hung check never blocks the gate."""
        assert resolver._currency("cic", tmp_path / "index.json") == ("current", "")

    def test_stale_index_is_recorded_and_warned(self, sentinel_session, monkeypatch, capsys):
        """End to end, a stale verdict reaches the sentinel the gates read back and a warning on stderr.

        The probe is a real child process printing a stale verdict and exiting 1, as the shipped checker does.
        """
        fake = _write_fake_check_currency(sentinel_session / "fake_cic.py", "stale", "3 changed", 1)
        monkeypatch.setattr(resolver.shutil, "which", lambda _name: str(fake))

        resolver._record_currency("dev-codemap-currency", sentinel_session / "index.json")

        assert (sentinel_session / "dev-codemap-currency-sess").read_text() == "stale\n"
        assert "index is stale — 3 changed" in capsys.readouterr().err


class TestCanonicalResolver:
    """One canonical file, one label, no baked-in sentinel name."""

    @pytest.mark.parametrize("sentinel_name", ["dev-codemap-currency", "research-codemap-currency"])
    def test_hardcodes_no_plugin_sentinel_name(self, sentinel_name):
        """The per-plugin delta arrives as an argument, never as an edit to the shared file."""
        assert sentinel_name not in _DEV_RESOLVER.read_text(encoding="utf-8")

    def test_tool_label_is_current_plugin_name(self):
        """``TOOL_LABEL`` names ``codemap-py``; it had drifted to the retired name."""
        assert resolver.TOOL_LABEL == "codemap-py"

    def test_has_no_unused_query_label(self):
        """The dead ``QUERY_LABEL`` constant is gone from this copy."""
        assert not hasattr(resolver, "QUERY_LABEL")

    @mock.patch.object(resolver.shutil, "which", autospec=True, return_value=None)
    def test_strict_mode_names_the_current_plugin(self, mock_which, capsys):
        """The retired bare ``codemap`` plugin name does not reappear in the user-facing install error."""
        assert resolver.main(["strict", "--currency-prefix", "dev-codemap-currency"]) == 1
        assert "Install codemap-py plugin" in capsys.readouterr().err

    @mock.patch.object(
        resolver.subprocess, "run", autospec=True, return_value=subprocess.CompletedProcess([], 0, stdout="true\n")
    )
    def test_dev_wrapper_supplies_develop_prefix(self, mock_run, tmp_path):
        """cc_develop's own wrapper owns develop's sentinel name and launches the resolver with this interpreter."""
        gate = load_module(_DEV_BIN / "dev_codemap_gate.py", "dev_codemap_gate_under_test")

        gate._run_codemap_resolve(tmp_path, "auto")

        argv = mock_run.call_args.args[0]
        assert gate.CURRENCY_PREFIX == "dev-codemap-currency"
        assert argv[argv.index("--currency-prefix") + 1] == "dev-codemap-currency"
        assert argv[0] == sys.executable


class TestCurrencySentinel:
    """The currency sentinel is written safely and keyed by validated prefix and session identity."""

    @_requires_symlinks
    def test_replaces_planted_symlink_instead_of_following_it(self, sentinel_session):
        """A symlink pre-created at the predictable sentinel name is displaced; its target stays untouched.

        ``CSID`` defaults to ``shared``, so the sentinel name is guessable in a shared temp dir. A plain ``write_text``
        followed such a link and clobbered the victim's file with the status word.
        """
        victim = sentinel_session / "victim.txt"
        victim.write_text("precious\n")
        sentinel = sentinel_session / "codemap-currency-sess"
        sentinel.symlink_to(victim)

        resolver._write_currency("codemap-currency", "stale")

        assert (victim.read_text(), sentinel.is_symlink(), sentinel.read_text()) == ("precious\n", False, "stale\n")
        assert sorted(p.name for p in sentinel_session.iterdir()) == ["codemap-currency-sess", "victim.txt"]

    def test_write_failure_never_raises(self, tmp_path, monkeypatch):
        """An unwritable sentinel directory is swallowed: currency status is advisory and must not fail the gate."""
        monkeypatch.setenv("TMPDIR", str(tmp_path / "missing"))

        assert resolver._write_currency("codemap-currency", "current") is None

    def test_cli_writes_the_caller_supplied_sentinel(self, sentinel_session, capsys):
        """The prefix flag is what lets one byte-identical file serve two plugins."""
        assert resolver.main(["off", "--currency-prefix", "research-codemap-currency"]) == 0
        assert capsys.readouterr().out == "false\n"
        assert (sentinel_session / "research-codemap-currency-sess").read_text() == "off\n"

    @pytest.mark.parametrize("identity", ["prefix", "session"])
    @pytest.mark.parametrize("value", ["a..b", "../escape", "bad/name", "bad\nname"])
    def test_invalid_identity_exits_before_writing(self, tmp_path, monkeypatch, capsys, identity, value):
        """An invalid prefix or session token exits 2 before any stdout, temporary file, or sentinel replacement."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", value if identity == "session" else "safe.session-1_2")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        prefix = value if identity == "prefix" else "safe.slug-1_2"

        assert resolver.main(["off", "--currency-prefix", prefix]) == 2

        output = capsys.readouterr()
        assert output.out == ""
        assert len(output.err.splitlines()) == 1
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("csid", ["session.1_2-3", ""])
    def test_valid_identity_and_shared_fallback(self, tmp_path, monkeypatch, capsys, csid):
        """Allowed punctuation and the missing-session fallback still write the expected sentinel."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", csid)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        assert resolver.main(["off", "--currency-prefix", "skill.1_2-3"]) == 0

        assert capsys.readouterr().out == "false\n"
        assert (tmp_path / f"skill.1_2-3-{csid or 'shared'}").read_text(encoding="utf-8") == "off\n"

    def test_empty_prefix_is_rejected(self, tmp_path, monkeypatch, capsys):
        """An explicitly empty prefix is invalid even with the shared-session fallback."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.delenv("CSID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        assert resolver.main(["off", "--currency-prefix", ""]) == 2

        output = capsys.readouterr()
        assert output.out == ""
        assert len(output.err.splitlines()) == 1
        assert list(tmp_path.iterdir()) == []
