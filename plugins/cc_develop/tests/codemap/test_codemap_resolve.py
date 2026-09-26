"""Regression tests for bin/codemap_resolve.py — the consumer codemap gate resolver.

Each test pins one audit finding's failure mode:

* The index directory defaulted to a CWD-relative ``.cache/codemap``, so a skill
  invoked from a repository subdirectory reported a false ``no_index``.
* The project name was ASCII-sanitized, so any repository directory containing a
  space, ``+`` or a non-ASCII character resolved to a filename the scanner never writes.
* The currency probe ran untimed, twice per gate, and discarded every ``stale``
  verdict because it gated on the exit code that *carries* that verdict.
*  /  / One canonical resolver, byte-identical across plugins, with a
  single ``codemap-py`` label and no plugin-specific sentinel name baked in.

The module is loaded by explicit path rather than imported by name: both cc_develop and
cc_research ship a ``codemap_resolve`` module, and a plain import would let whichever
plugin's ``bin/`` reached ``sys.path`` first answer for both.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEV_BIN = _REPO_ROOT / "plugins" / "cc_develop" / "bin"
_DEV_RESOLVER = _DEV_BIN / "codemap_resolve.py"
_RESEARCH_RESOLVER = _REPO_ROOT / "plugins" / "cc_research" / "bin" / "codemap_resolve.py"


def _load(path: Path, name: str) -> ModuleType:
    """Load *path* as a uniquely named module so sibling plugins cannot shadow each other."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolver = _load(_DEV_RESOLVER, "dev_codemap_resolve_under_test")


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


# --------------------------------------------------------------------------------------
# Index directory anchored to the git toplevel, not the CWD
# --------------------------------------------------------------------------------------


def test_index_is_found_from_a_repository_subdirectory(tmp_path, monkeypatch):
    """A skill run from a subdir must resolve the root's index, not a CWD-relative miss."""
    root = _git_repo(tmp_path, "proj")
    index = _write_index(root, "proj")
    monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
    monkeypatch.chdir(root / "pkg")

    resolved = resolver._index_path(resolver._canonical_root())

    assert resolved == index.resolve()
    assert resolved.is_file()
    # The pre-fix behaviour: `.cache/codemap` relative to the CWD, which does not exist.
    assert not (Path.cwd() / ".cache" / "codemap" / "proj.json").exists()


def test_index_dir_override_is_flat(tmp_path, monkeypatch):
    """CODEMAP_INDEX_DIR resolves to a flat <override>/<project>.json, matching the provider."""
    root = _git_repo(tmp_path, "proj")
    override = tmp_path / "shared-cache"
    override.mkdir()
    monkeypatch.setenv("CODEMAP_INDEX_DIR", str(override))
    monkeypatch.chdir(root)

    assert resolver._index_path(resolver._canonical_root()) == (override / "proj.json").resolve()


def test_canonical_root_falls_back_to_cwd_outside_a_repository(tmp_path, monkeypatch):
    """Outside a repo the root is the CWD — mirroring the provider, never a literal 'default'."""
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setattr(resolver.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 128, stdout=""))

    assert resolver._canonical_root() == outside.resolve()


# --------------------------------------------------------------------------------------
# Raw basename, no sanitization
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["café", "my+proj", "two words", "ünïcode+dir name"])
def test_project_name_is_the_raw_basename(tmp_path, monkeypatch, name):
    """The scanner writes the directory basename verbatim; stripping chars = false no_index."""
    root = _git_repo(tmp_path, name)
    index = _write_index(root, name)
    monkeypatch.delenv("CODEMAP_INDEX_DIR", raising=False)
    monkeypatch.chdir(root)

    resolved = resolver._index_path(resolver._canonical_root())

    assert resolved.name == f"{name}.json"
    assert resolved == index.resolve()
    assert resolved.is_file()


# --------------------------------------------------------------------------------------
# One timed currency call, verdict read from stdout not the exit code
# --------------------------------------------------------------------------------------


def test_stale_verdict_survives_the_nonzero_exit_code(tmp_path):
    """Check-index-currency exits 1 *because* the index is stale — that is not a failure."""
    fake = tmp_path / "fake_cic.py"
    fake.write_text("import json, sys\nprint(json.dumps({'status': 'stale', 'reason': 'HEAD changed'}))\nsys.exit(1)\n")

    assert resolver._currency(str(fake), tmp_path / "index.json") == ("stale", "HEAD changed")


def test_no_index_verdict_survives_exit_code_two(tmp_path):
    """Exit 2 carries a no_index verdict; it must reach the caller, not be coerced to current."""
    fake = tmp_path / "fake_cic.py"
    fake.write_text("import json, sys\nprint(json.dumps({'status': 'no_index', 'reason': 'gone'}))\nsys.exit(2)\n")

    assert resolver._currency(str(fake), tmp_path / "index.json") == ("no_index", "gone")


def test_currency_probe_makes_one_bounded_call(monkeypatch, tmp_path):
    """A single timed subprocess yields both fields; ``--field`` spawned two, untimed."""
    calls: list[tuple[list[str], dict]] = []

    def _fake_run(cmd, **kwargs):
        """Return a stale verdict while recording the bounded subprocess call."""
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps({"status": "stale", "reason": "r"}))

    monkeypatch.setattr(resolver.subprocess, "run", _fake_run)

    assert resolver._currency("cic", tmp_path / "index.json") == ("stale", "r")
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert kwargs["timeout"] == resolver._CURRENCY_TIMEOUT_S
    assert "--field" not in argv


def test_currency_probe_fails_open_when_the_check_hangs(monkeypatch, tmp_path):
    """A timed-out or unparsable verdict must not block the gate."""

    def _fake_run(cmd, **kwargs):
        """Raise the timeout used to verify fail-open currency handling."""
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    monkeypatch.setattr(resolver.subprocess, "run", _fake_run)

    assert resolver._currency("cic", tmp_path / "index.json") == ("current", "")


def test_stale_index_is_recorded_and_warned(tmp_path, monkeypatch, capsys):
    """End-to-end: a stale verdict reaches the sentinel the gates read back."""
    fake = tmp_path / "fake_cic.py"
    fake.write_text("import json, sys\nprint(json.dumps({'status': 'stale', 'reason': '3 changed'}))\nsys.exit(1)\n")
    monkeypatch.setattr(resolver.shutil, "which", lambda _name: str(fake))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "sess")

    resolver._record_currency("dev-codemap-currency", tmp_path / "index.json")

    assert (tmp_path / "dev-codemap-currency-sess").read_text() == "stale\n"
    assert "index is stale — 3 changed" in capsys.readouterr().err


# --------------------------------------------------------------------------------------
# One canonical file, one label, and no baked-in sentinel name
# --------------------------------------------------------------------------------------


def test_resolver_copies_are_byte_identical():
    """Cc_develop is canonical; cc_research must equal it byte-for-byte."""
    assert _DEV_RESOLVER.read_bytes() == _RESEARCH_RESOLVER.read_bytes()


def test_shared_resolver_hardcodes_no_plugin_sentinel_name():
    """The per-plugin delta must arrive as an argument, never as an edit to this file."""
    text = _DEV_RESOLVER.read_text(encoding="utf-8")
    assert "dev-codemap-currency" not in text
    assert "research-codemap-currency" not in text


def test_single_tool_label_and_no_unused_query_label():
    """/QUERY_LABEL was dead in this copy; TOOL_LABEL had drifted to the retired name."""
    assert resolver.TOOL_LABEL == "codemap-py"
    assert not hasattr(resolver, "QUERY_LABEL")


def test_strict_mode_names_the_current_plugin(capsys, monkeypatch):
    """The retired bare `codemap` plugin name must not reappear in user-facing errors."""
    monkeypatch.setattr(resolver.shutil, "which", lambda _name: None)

    assert resolver.main(["strict", "--currency-prefix", "dev-codemap-currency"]) == 1
    assert "Install codemap-py plugin" in capsys.readouterr().err


def test_cli_writes_the_caller_supplied_sentinel(tmp_path, monkeypatch, capsys):
    """The prefix flag is what makes one byte-identical file serve two plugins."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "sess")

    assert resolver.main(["off", "--currency-prefix", "research-codemap-currency"]) == 0
    assert capsys.readouterr().out == "false\n"
    assert (tmp_path / "research-codemap-currency-sess").read_text() == "off\n"


def test_dev_wrapper_supplies_the_develop_prefix(monkeypatch, tmp_path):
    """cc_develop's own wrapper owns develop's sentinel name."""
    gate = _load(_DEV_BIN / "dev_codemap_gate.py", "dev_codemap_gate_under_test")
    captured: dict[str, list[str]] = {}

    def _fake_run(cmd, **kwargs):
        """Return a successful resolver response for the wrapper assertion."""
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="true\n")

    monkeypatch.setattr(gate.subprocess, "run", _fake_run)
    gate._run_codemap_resolve(tmp_path, "auto")

    argv = captured["cmd"]
    assert gate.CURRENCY_PREFIX == "dev-codemap-currency"
    assert argv[argv.index("--currency-prefix") + 1] == "dev-codemap-currency"
    assert argv[0] == sys.executable
