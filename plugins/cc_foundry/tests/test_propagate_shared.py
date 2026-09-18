"""Tests for ``bin/propagate_shared.py``.

The tool keeps byte-identical cross-plugin shared files in sync from a single canonical source. These tests use a
synthetic MANIFEST over ``tmp_path`` to verify drift detection (``check``) and syncing (``apply``) without touching the
real repository manifest.
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

_MOD_PATH = Path(__file__).resolve().parent.parent / "bin" / "propagate_shared.py"
_spec = importlib.util.spec_from_file_location("propagate_shared", _MOD_PATH)
assert _spec and _spec.loader
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


def test_check_clean_when_all_match(tmp_path: Path) -> None:
    """No findings when every copy equals the canonical."""
    _tree(tmp_path, "X\n", "X\n", "X\n")
    assert ps.check(tmp_path, _manifest()) == []


def test_check_reports_each_drifted_copy(tmp_path: Path) -> None:
    """Each copy differing from canonical is reported once."""
    _tree(tmp_path, "X\n", "OLD\n", "X\n")
    findings = ps.check(tmp_path, _manifest())
    assert len(findings) == 1
    assert "b/hook.js" in findings[0]


def test_check_reports_missing_canonical(tmp_path: Path) -> None:
    """A missing canonical file is reported, not silently skipped."""
    manifest = [{"canonical": "gone/hook.js", "copies": ["b/hook.js"]}]
    findings = ps.check(tmp_path, manifest)
    assert len(findings) == 1
    assert "canonical missing" in findings[0]


def test_apply_syncs_drifted_copies(tmp_path: Path) -> None:
    """Overwrite drifted copies and return their paths before confirming a clean state."""
    _tree(tmp_path, "NEW\n", "OLD\n", "NEW\n")
    updated = ps.apply(tmp_path, _manifest())
    assert updated == ["b/hook.js"]
    assert (tmp_path / "b" / "hook.js").read_text() == "NEW\n"
    assert ps.check(tmp_path, _manifest()) == []


def test_main_check_exit_codes(tmp_path: Path) -> None:
    """Main --check exits 1 on drift, 0 when clean (via monkeypatched MANIFEST)."""
    _tree(tmp_path, "X\n", "OLD\n", "X\n")
    original = ps.MANIFEST
    ps.MANIFEST = _manifest()
    try:
        assert ps.main(["--root", str(tmp_path)]) == 1
        ps.main(["--apply", "--root", str(tmp_path)])
        assert ps.main(["--root", str(tmp_path)]) == 0
    finally:
        ps.MANIFEST = original


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
    """A propagated copy's namespace cannot collide with a consumer-owned contract."""
    assert Path(copy).name.startswith("foundry--")


_CODEMAP_WRAPPERS = [
    "plugins/cc_foundry/skills/_shared/codemap-context.md",
    "plugins/cc_develop/skills/_shared/codemap-context.md",
    "plugins/cc_develop/skills/_shared/codemap-gates.md",
    "plugins/cc_oss/skills/_shared/codemap-gates.md",
    "plugins/cc_research/skills/_shared/codemap-gates.md",
]

_PROVIDER_RESOLVE = 'resolve_shared_path.py" codemap-py claude-skills/_shared'


def _wrapper_script(wrapper_path: Path) -> str:
    """Return the first fenced bash block of a wrapper — the loader the tests execute."""
    return wrapper_path.read_text(encoding="utf-8").split("```bash\n", 1)[1].split("```", 1)[0]


def _fallback_line(wrapper_path: Path) -> str:
    """Return the exact fallback line a wrapper prints when the provider contract is absent."""
    stem = "codemap gates contract absent" if wrapper_path.stem == "codemap-gates" else "codemap contract absent"
    return f"{stem} — use fallback below\n"


@pytest.mark.parametrize("wrapper", _CODEMAP_WRAPPERS)
def test_codemap_wrapper_resolves_the_active_provider_install(wrapper: str) -> None:
    """Each wrapper reads the provider contract from the active codemap-py install, never a local copy.

    The optional-provider exception (plugins/CLAUDE.md §Self-Contained _shared) allows exactly this
    shape: own-plugin resolver copy, provider name + subdir, registry tier first — and no manifested
    ``codemap-py--`` copy and no newest-version cache glob.
    """
    text = (_MOD_PATH.parents[3] / wrapper).read_text(encoding="utf-8")

    assert _PROVIDER_RESOLVE in text
    assert "command -v codemap-py" in text
    assert "codemap-py--" not in text
    assert "plugins/cache" not in text


@pytest.mark.parametrize("wrapper", _CODEMAP_WRAPPERS)
def test_codemap_provider_contract_is_not_manifested(wrapper: str) -> None:
    """No MANIFEST entry copies codemap-py's contracts into a consumer any more."""
    manifest_targets = {copy for entry in ps.MANIFEST for copy in entry["copies"]}
    consumer_shared = Path(wrapper).parent

    assert not any(Path(target).parent == consumer_shared and "codemap-py--" in target for target in manifest_targets)
    assert not (_MOD_PATH.parents[3] / consumer_shared / f"codemap-py--{Path(wrapper).name}").exists()


@pytest.mark.integration
@_requires_bash
@pytest.mark.parametrize("wrapper", _CODEMAP_WRAPPERS)
@pytest.mark.parametrize(
    ("cli_present", "contract_present"),
    [
        pytest.param(True, True, id="load-provider-contract"),
        pytest.param(True, False, id="missing-contract-fallback"),
        pytest.param(False, True, id="missing-cli-fallback"),
        pytest.param(False, False, id="missing-both-fallback"),
    ],
)
def test_codemap_wrapper_load_or_fallback(
    tmp_path: Path, wrapper: str, cli_present: bool, contract_present: bool
) -> None:
    """The shipped Bash loader degrades gracefully when either dependency is absent."""
    wrapper_path = _MOD_PATH.parents[3] / wrapper
    contract = tmp_path / wrapper_path.name
    content = "provider contract bytes\n"
    if contract_present:
        contract.write_text(content, encoding="utf-8", newline="\n")

    # Stub the resolver to the tmp provider dir without depending on an installed plugin or
    # host PATH. Keep real cat I/O so missing-file failures remain observable.
    prelude = 'python() { printf "%s\\n" "$CONTRACT_DIRECTORY"; }\ncat() { command -p cat "$@"; }\nPATH=\n'
    if cli_present:
        prelude += "codemap-py() { :; }\n"
    env = {**os.environ, "CONTRACT_DIRECTORY": tmp_path.as_posix()}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    result = subprocess.run(
        ["bash", "-c", prelude + _wrapper_script(wrapper_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    expected = content if cli_present and contract_present else _fallback_line(wrapper_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    assert result.stderr == ""


@pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
def test_noop_apply_reports_success_on_legacy_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    """No-op propagation must report success without requiring Unicode stdout."""
    _tree(tmp_path, "X\n", "X\n", "X\n")
    monkeypatch.setattr(ps, "MANIFEST", _manifest())
    with io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="\n") as output:
        monkeypatch.setattr(sys, "stdout", output)
        assert ps.main(["--apply", "--root", str(tmp_path)]) == 0
        output.flush()
        assert output.buffer.getvalue() == b"OK: all shared copies already in sync\n"


@pytest.mark.integration
@_requires_bash
@pytest.mark.parametrize("wrapper", _CODEMAP_WRAPPERS)
@pytest.mark.parametrize("contract_present", [True, False])
def test_codemap_wrapper_reads_the_recorded_install_not_the_newest_cache(
    tmp_path: Path, wrapper: str, contract_present: bool
) -> None:
    """The install record picks the provider version; a newer cache dir never shadows it.

    Two codemap-py versions sit in the cache and ``installed_plugins.json`` points at the older one. The wrapper must
    print that recorded version's contract, and its own fallback line when the recorded version ships no contract — even
    though the newer dir does.
    """
    root = _MOD_PATH.parents[3]
    wrapper_path = root / wrapper
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
    if contract_present:
        (recorded / "claude-skills" / "_shared" / wrapper_path.name).write_bytes(b"recorded contract\n")
    (tmp_path / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": {
                    "codemap-py@borda-ai-rig": [
                        {
                            "scope": "user",
                            "installPath": recorded.as_posix(),
                            "version": "0.38.0",
                            "installedAt": "2026-09-18T06:00:47.793Z",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    prelude = 'python() { "$TEST_PYTHON" "$@"; }\ncodemap-py() { :; }\n'
    result = subprocess.run(
        ["bash", "-c", prelude + _wrapper_script(wrapper_path)],
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": tmp_path.as_posix(),
            "USERPROFILE": str(tmp_path),
            "CLAUDE_PLUGIN_ROOT": consumer_root.as_posix(),
            "TEST_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    expected = "recorded contract\n" if contract_present else _fallback_line(wrapper_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    assert result.stderr == ""
