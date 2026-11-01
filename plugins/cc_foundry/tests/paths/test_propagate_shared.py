"""Tests for ``bin/propagate_shared.py``.

The tool keeps byte-identical cross-plugin shared files in sync from a single canonical source. These tests use a
synthetic MANIFEST over ``tmp_path`` to verify drift detection (``check``) and syncing (``apply``) without touching the
real repository manifest, plus the shipped codemap wrapper loaders that the real MANIFEST deliberately leaves out.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_MOD_PATH = Path(__file__).resolve().parent.parent.parent / "bin" / "propagate_shared.py"
_spec = importlib.util.spec_from_file_location("propagate_shared", _MOD_PATH)
assert _spec
assert _spec.loader
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)

_requires_bash = pytest.mark.skipif(not _bash_runs_posix_script(), reason="Bash cannot execute POSIX scripts")


def _manifest() -> list[dict[str, object]]:
    """Return a one-entry manifest matching the tmp tree built below.

    Examples:
        >>> _manifest()[0]["copies"]
        ['b/hook.js', 'c/hook.js']
    """
    return [{"canonical": "a/hook.js", "copies": ["b/hook.js", "c/hook.js"]}]


def _tree(root: Path, canonical: str, b: str, c: str) -> None:
    """Create a/hook.js, b/hook.js, c/hook.js with the given contents.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     _tree(Path(directory), "A", "B", "C")
        ...     (Path(directory) / "b" / "hook.js").read_text()
        'B'
    """
    for sub, content in (("a", canonical), ("b", b), ("c", c)):
        (root / sub).mkdir(parents=True, exist_ok=True)
        (root / sub / "hook.js").write_text(content, encoding="utf-8")


@pytest.fixture(name="synthetic_manifest")
def _synthetic_manifest(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Point the tool's module-level MANIFEST at the one-entry synthetic manifest for the test."""
    manifest = _manifest()
    monkeypatch.setattr(ps, "MANIFEST", manifest)
    return manifest


class TestCheck:
    """``check`` reports drift between canonical files and their copies without writing."""

    def test_clean_when_all_match(self, tmp_path: Path) -> None:
        """No findings when every copy equals the canonical.

        Scenario: canonical and both copies hold identical bytes, the state a healthy repository stays in.
        """
        _tree(tmp_path, "X\n", "X\n", "X\n")
        assert ps.check(tmp_path, _manifest()) == []

    def test_reports_each_drifted_copy(self, tmp_path: Path) -> None:
        """Each copy differing from canonical is reported once.

        Scenario: one of two copies is stale. Only that copy appears in the findings, so a reviewer sees what to sync.
        """
        _tree(tmp_path, "X\n", "OLD\n", "X\n")
        findings = ps.check(tmp_path, _manifest())
        assert len(findings) == 1
        assert "b/hook.js" in findings[0]

    def test_reports_missing_canonical(self, tmp_path: Path) -> None:
        """A missing canonical file is reported, not silently skipped.

        Scenario: the manifest names a canonical path that does not exist. Skipping it would hide that the copies have
        no source of truth.
        """
        manifest = [{"canonical": "gone/hook.js", "copies": ["b/hook.js"]}]
        findings = ps.check(tmp_path, manifest)
        assert len(findings) == 1
        assert "canonical missing" in findings[0]


class TestApply:
    """``apply`` rewrites drifted copies from the canonical file."""

    def test_syncs_drifted_copies(self, tmp_path: Path) -> None:
        """Overwrite drifted copies and return their paths before confirming a clean state.

        Scenario: one copy is stale. After ``apply`` it holds the canonical bytes and a fresh ``check`` finds nothing.
        """
        _tree(tmp_path, "NEW\n", "OLD\n", "NEW\n")
        updated = ps.apply(tmp_path, _manifest())
        assert updated == ["b/hook.js"]
        assert (tmp_path / "b" / "hook.js").read_text() == "NEW\n"
        assert ps.check(tmp_path, _manifest()) == []

    @pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
    def test_noop_reports_success_on_legacy_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        synthetic_manifest: list[dict[str, object]],
        encoding: str,
    ) -> None:
        """No-op propagation reports success without requiring Unicode stdout.

        Scenario: every copy is already in sync and stdout uses a legacy codec (a Windows console). The success line
        must stay ASCII-safe so the tool does not crash after doing nothing.
        """
        _tree(tmp_path, "X\n", "X\n", "X\n")
        with io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="\n") as output:
            monkeypatch.setattr(sys, "stdout", output)
            assert ps.main(["--apply", "--root", str(tmp_path)]) == 0
            output.flush()
            assert output.buffer.getvalue() == b"OK: all shared copies already in sync\n"


class TestMain:
    """``main`` maps check and apply results to exit codes."""

    def test_check_exits_1_on_drift(self, tmp_path: Path, synthetic_manifest: list[dict[str, object]]) -> None:
        """The default check mode exits 1 when a copy has drifted.

        Scenario: one copy differs from canonical and the manifest is the synthetic one, so the real repository is
        never consulted.
        """
        _tree(tmp_path, "X\n", "OLD\n", "X\n")
        assert ps.main(["--root", str(tmp_path)]) == 1

    def test_check_exits_0_after_apply(self, tmp_path: Path, synthetic_manifest: list[dict[str, object]]) -> None:
        """A check run after ``--apply`` exits 0.

        Scenario: drift is repaired by ``--apply`` first. The follow-up check proves the repair leaves the tree clean.
        """
        _tree(tmp_path, "X\n", "OLD\n", "X\n")
        ps.main(["--apply", "--root", str(tmp_path)])
        assert ps.main(["--root", str(tmp_path)]) == 0


@pytest.mark.parametrize(
    "copy",
    [
        str(copy)
        for entry in ps.MANIFEST
        if str(entry["canonical"]).endswith(("_shared/quality-stack.md", "_shared/cross-validation-protocol.md"))
        for copy in entry["copies"]
    ],
)
def test_real_manifest_prefixes_migrated_copies(copy: str) -> None:
    """A propagated copy's namespace cannot collide with a consumer-owned contract.

    Scenario: the real MANIFEST copies quality-stack and cross-validation-protocol into several plugins. Each copy keeps
    the ``foundry--`` prefix so it never shadows a file the consuming plugin owns under the same basename.
    """
    assert Path(copy).name.startswith("foundry--")


_CODEMAP_WRAPPERS = [
    "plugins/cc_foundry/skills/_shared/codemap-context.md",
    "plugins/cc_develop/skills/_shared/codemap-context.md",
    "plugins/cc_develop/skills/_shared/codemap-gates.md",
    "plugins/cc_oss/skills/_shared/codemap-gates.md",
    "plugins/cc_research/skills/_shared/codemap-gates.md",
]

_PROVIDER_RESOLVE = 'resolve_shared_path.py" codemap-py claude-skills/_shared'

#: Shared parametrization rows: stdout expected from a wrapper run is either the provider contract or the fallback line.
_CONTRACT_BYTES = "provider contract bytes\n"


def _wrapper_script(wrapper_path: Path) -> str:
    """Return the first fenced bash block of a wrapper — the loader the tests execute."""
    return wrapper_path.read_text(encoding="utf-8").split("```bash\n", 1)[1].split("```", 1)[0]


def _fallback_line(wrapper_path: Path) -> str:
    """Return the exact fallback line a wrapper prints when the provider contract is absent."""
    stem = "codemap gates contract absent" if wrapper_path.stem == "codemap-gates" else "codemap contract absent"
    return f"{stem} — use fallback below\n"


def _expected_stdout(outcome: str, contract_text: str, wrapper_path: Path) -> str:
    """Map an ``outcome`` label (``contract`` or ``fallback``) to the stdout a wrapper run must produce."""
    return {"contract": contract_text, "fallback": _fallback_line(wrapper_path)}[outcome]


def _run_wrapper(prelude: str, wrapper_path: Path, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a wrapper's shipped bash loader under real ``bash`` after a shell-function ``prelude``."""
    return subprocess.run(
        ["bash", "-c", prelude + _wrapper_script(wrapper_path)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _install_recorded_codemap(tmp_path: Path, wrapper_path: Path, recorded_contract: bytes | None) -> Path:
    """Lay out a consumer plugin plus a cache with a recorded and a newer codemap-py version; return the consumer root.

    The newer version always ships a contract; the recorded one ships ``recorded_contract`` unless it is ``None``.
    ``installed_plugins.json`` points at the recorded (older) version.
    """
    plugin_path = wrapper_path.parents[2]
    consumer_root = tmp_path / "consumer-plugin"
    (consumer_root / "bin").mkdir(parents=True)
    for helper in ("resolve_shared_path.py", "get_plugin_install_path.py"):
        (consumer_root / "bin" / helper).write_bytes((plugin_path / "bin" / helper).read_bytes())
    (consumer_root / ".claude-plugin").mkdir()
    (consumer_root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": plugin_path.name.removeprefix("cc_")}), encoding="utf-8"
    )
    cache = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig"
    recorded = cache / "codemap-py" / "0.38.0"
    newer = cache / "codemap-py" / "99.0.0"
    for version_dir in (recorded, newer):
        (version_dir / "claude-skills" / "_shared").mkdir(parents=True)
    (newer / "claude-skills" / "_shared" / wrapper_path.name).write_bytes(b"newer cached contract\n")
    if recorded_contract is not None:
        (recorded / "claude-skills" / "_shared" / wrapper_path.name).write_bytes(recorded_contract)
    record = {
        "scope": "user",
        "installPath": recorded.as_posix(),
        "version": "0.38.0",
        "installedAt": "2026-09-18T06:00:47.793Z",
    }
    (tmp_path / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 1, "plugins": {"codemap-py@borda-ai-rig": [record]}}), encoding="utf-8", newline="\n"
    )
    return consumer_root


class TestCodemapWrappers:
    """Consumer ``codemap-*.md`` wrappers read the provider's contract from its active install, never a local copy."""

    @pytest.mark.parametrize("wrapper", _CODEMAP_WRAPPERS)
    def test_resolves_the_active_provider_install(self, wrapper: str) -> None:
        """Each wrapper reads the provider contract from the active codemap-py install, never a local copy.

        The optional-provider exception (plugins/CLAUDE.md §Self-Contained _shared) allows exactly this shape: own-
        plugin resolver copy, provider name + subdir, registry tier first — and no manifested ``codemap-py--`` copy and
        no newest-version cache glob.
        """
        text = (_MOD_PATH.parents[3] / wrapper).read_text(encoding="utf-8")

        assert _PROVIDER_RESOLVE in text
        assert "command -v codemap-py" in text
        assert "codemap-py--" not in text
        assert "plugins/cache" not in text

    @pytest.mark.parametrize("wrapper", _CODEMAP_WRAPPERS)
    def test_provider_contract_is_not_manifested(self, wrapper: str) -> None:
        """No MANIFEST entry copies codemap-py's contracts into a consumer any more.

        Scenario: a manifested ``codemap-py--*`` copy would drift silently from whatever provider version the user
        runs, so neither the manifest nor the consumer tree may carry one.
        """
        manifest_targets = {copy for entry in ps.MANIFEST for copy in entry["copies"]}
        consumer_shared = Path(wrapper).parent

        assert not any(
            Path(target).parent == consumer_shared and "codemap-py--" in target for target in manifest_targets
        )
        assert not (_MOD_PATH.parents[3] / consumer_shared / f"codemap-py--{Path(wrapper).name}").exists()

    @pytest.mark.integration
    @_requires_bash
    @pytest.mark.parametrize("wrapper", _CODEMAP_WRAPPERS)
    @pytest.mark.parametrize(
        ("codemap_cli", "contract_dir", "outcome"),
        [
            pytest.param("codemap-py() { :; }\n", "present", "contract", id="load-provider-contract"),
            pytest.param("codemap-py() { :; }\n", "absent", "fallback", id="missing-contract-fallback"),
            pytest.param("", "present", "fallback", id="missing-cli-fallback"),
            pytest.param("", "absent", "fallback", id="missing-both-fallback"),
        ],
    )
    def test_load_or_fallback(
        self, tmp_path: Path, wrapper: str, codemap_cli: str, contract_dir: str, outcome: str
    ) -> None:
        """The shipped Bash loader degrades gracefully when either dependency is absent.

        Scenario: the resolver is stubbed to a tmp provider directory (``present`` holds the contract, ``absent`` is
        empty) and ``codemap-py`` is a defined shell function or missing. Real ``bash`` and real ``cat`` run the
        shipped loader; only the contract and the CLI together print the contract, everything else prints the
        fallback line.
        """
        wrapper_path = _MOD_PATH.parents[3] / wrapper
        (tmp_path / "present").mkdir()
        (tmp_path / "absent").mkdir()
        (tmp_path / "present" / wrapper_path.name).write_text(_CONTRACT_BYTES, encoding="utf-8", newline="\n")
        # Stub the resolver to the tmp provider dir without depending on an installed plugin or host PATH. Keep real
        # cat I/O so missing-file failures remain observable.
        prelude = f'python() {{ printf "%s\\n" "$CONTRACT_DIRECTORY"; }}\ncat() {{ command -p cat "$@"; }}\nPATH=\n{codemap_cli}'
        env = {**os.environ, "CONTRACT_DIRECTORY": (tmp_path / contract_dir).as_posix()}
        env.pop("CLAUDE_PLUGIN_ROOT", None)

        result = _run_wrapper(prelude, wrapper_path, tmp_path, env)

        assert result.returncode == 0, result.stderr
        assert result.stdout == _expected_stdout(outcome, _CONTRACT_BYTES, wrapper_path)
        assert result.stderr == ""

    @pytest.mark.integration
    @_requires_bash
    @pytest.mark.parametrize("wrapper", _CODEMAP_WRAPPERS)
    @pytest.mark.parametrize(
        ("recorded_contract", "outcome"),
        [
            pytest.param(b"recorded contract\n", "contract", id="recorded-contract-present"),
            pytest.param(None, "fallback", id="recorded-contract-absent"),
        ],
    )
    def test_reads_the_recorded_install_not_the_newest_cache(
        self, tmp_path: Path, wrapper: str, recorded_contract: bytes | None, outcome: str
    ) -> None:
        """The install record picks the provider version; a newer cache dir never shadows it.

        Two codemap-py versions sit in the cache and ``installed_plugins.json`` points at the older one. The wrapper
        must print that recorded version's contract, and its own fallback line when the recorded version ships no
        contract — even though the newer dir does.
        """
        wrapper_path = _MOD_PATH.parents[3] / wrapper
        consumer_root = _install_recorded_codemap(tmp_path, wrapper_path, recorded_contract)
        env = {
            **os.environ,
            "HOME": tmp_path.as_posix(),
            "USERPROFILE": str(tmp_path),
            "CLAUDE_PLUGIN_ROOT": consumer_root.as_posix(),
            "TEST_PYTHON": sys.executable,
        }

        result = _run_wrapper('python() { "$TEST_PYTHON" "$@"; }\ncodemap-py() { :; }\n', wrapper_path, tmp_path, env)

        assert result.returncode == 0, result.stderr
        assert result.stdout == _expected_stdout(outcome, "recorded contract\n", wrapper_path)
        assert result.stderr == ""
