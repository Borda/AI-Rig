"""Tests for ``bin/propagate_shared.py``.

The tool keeps byte-identical cross-plugin shared files in sync from a single canonical source. These tests use a
synthetic MANIFEST over ``tmp_path`` to verify drift detection (``check``) and syncing (``apply``) without touching the
real repository manifest.
"""

from __future__ import annotations

import importlib.util
import io
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


@pytest.mark.parametrize(
    ("canonical", "copy"),
    [
        pytest.param(
            "plugins/codemap-py/claude-skills/_shared/codemap-context.md",
            "plugins/cc_foundry/skills/_shared/codemap-py--codemap-context.md",
            id="foundry-context",
        ),
        pytest.param(
            "plugins/codemap-py/claude-skills/_shared/codemap-context.md",
            "plugins/cc_develop/skills/_shared/codemap-py--codemap-context.md",
            id="develop-context",
        ),
        pytest.param(
            "plugins/codemap-py/claude-skills/_shared/codemap-gates.md",
            "plugins/cc_develop/skills/_shared/codemap-py--codemap-gates.md",
            id="develop-gates",
        ),
        pytest.param(
            "plugins/codemap-py/claude-skills/_shared/codemap-gates.md",
            "plugins/cc_oss/skills/_shared/codemap-py--codemap-gates.md",
            id="oss-gates",
        ),
        pytest.param(
            "plugins/codemap-py/claude-skills/_shared/codemap-gates.md",
            "plugins/cc_research/skills/_shared/codemap-py--codemap-gates.md",
            id="research-gates",
        ),
    ],
)
def test_codemap_contract_copy_is_manifested_and_identical(canonical: str, copy: str) -> None:
    """Each consumer ships a registered, byte-identical provider contract."""
    root = _MOD_PATH.parents[3]
    manifest = {str(entry["canonical"]): entry["copies"] for entry in ps.MANIFEST}

    assert copy in manifest.get(canonical, [])
    assert (root / copy).read_bytes() == (root / canonical).read_bytes()


@pytest.mark.parametrize(
    ("wrapper", "resolver", "local_contract"),
    [
        pytest.param(
            "plugins/cc_foundry/skills/_shared/codemap-context.md",
            'resolve_shared_path.py" foundry skills/_shared',
            'cat "$_FOUNDRY_SHARED/codemap-py--codemap-context.md"',
            id="foundry-context",
        ),
        pytest.param(
            "plugins/cc_develop/skills/_shared/codemap-context.md",
            "dev_shared_resolve.py",
            'cat "$_DEV_SHARED/codemap-py--codemap-context.md"',
            id="develop-context",
        ),
        pytest.param(
            "plugins/cc_develop/skills/_shared/codemap-gates.md",
            "dev_shared_resolve.py",
            'cat "$_DEV_SHARED/codemap-py--codemap-gates.md"',
            id="develop-gates",
        ),
        pytest.param(
            "plugins/cc_oss/skills/_shared/codemap-gates.md",
            'resolve_shared_path.py" oss skills/_shared',
            'cat "$_OSS_SHARED/codemap-py--codemap-gates.md"',
            id="oss-gates",
        ),
        pytest.param(
            "plugins/cc_research/skills/_shared/codemap-gates.md",
            "resolve_shared.py",
            'cat "$_RESEARCH_SHARED/codemap-py--codemap-gates.md"',
            id="research-gates",
        ),
    ],
)
def test_codemap_wrapper_loads_its_own_contract(wrapper: str, resolver: str, local_contract: str) -> None:
    """Each wrapper resolves its own shared directory instead of a sibling plugin."""
    text = (_MOD_PATH.parents[3] / wrapper).read_text(encoding="utf-8")

    assert resolver in text
    assert local_contract in text
    assert "codemap-py/*/claude-skills/_shared" not in text


@pytest.mark.integration
@_requires_bash
@pytest.mark.parametrize(
    "wrapper",
    [
        "plugins/cc_foundry/skills/_shared/codemap-context.md",
        "plugins/cc_develop/skills/_shared/codemap-context.md",
        "plugins/cc_develop/skills/_shared/codemap-gates.md",
        "plugins/cc_oss/skills/_shared/codemap-gates.md",
        "plugins/cc_research/skills/_shared/codemap-gates.md",
    ],
)
@pytest.mark.parametrize(
    ("cli_present", "contract_present"),
    [
        pytest.param(True, True, id="load-local-contract"),
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
    script = wrapper_path.read_text(encoding="utf-8").split("```bash\n", 1)[1].split("```", 1)[0]
    contract = tmp_path / f"codemap-py--{wrapper_path.name}"
    content = "provider contract bytes\n"
    if contract_present:
        contract.write_text(content, encoding="utf-8", newline="\n")

    # Resolve the consumer directory without depending on an installed plugin or
    # host PATH. Keep real cat I/O so missing-file failures remain observable.
    prelude = 'python() { printf "%s\\n" "$CONTRACT_DIRECTORY"; }\ncat() { command -p cat "$@"; }\nPATH=\n'
    if cli_present:
        prelude += "codemap-py() { :; }\n"
    env = {**os.environ, "CONTRACT_DIRECTORY": tmp_path.as_posix()}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    result = subprocess.run(
        ["bash", "-c", prelude + script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    fallback = "codemap gates contract absent" if wrapper_path.stem == "codemap-gates" else "codemap contract absent"
    expected = content if cli_present and contract_present else f"{fallback} — use fallback below\n"
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
@pytest.mark.parametrize(
    ("wrapper", "resolver"),
    [
        pytest.param(
            "plugins/cc_develop/skills/_shared/codemap-context.md", "dev_shared_resolve.py", id="develop-context"
        ),
        pytest.param("plugins/cc_develop/skills/_shared/codemap-gates.md", "dev_shared_resolve.py", id="develop-gates"),
        pytest.param("plugins/cc_research/skills/_shared/codemap-gates.md", "resolve_shared.py", id="research-gates"),
    ],
)
@pytest.mark.parametrize("contract_present", [True, False])
def test_codemap_wrapper_keeps_the_active_version_when_a_newer_cache_exists(
    tmp_path: Path, wrapper: str, resolver: str, contract_present: bool
) -> None:
    """A newer cached plugin cannot replace the active version's contract or its local fallback."""
    root = _MOD_PATH.parents[3]
    wrapper_path = root / wrapper
    plugin_path = wrapper_path.parents[2]
    active_root = tmp_path / "active-plugin"
    active_shared = active_root / "skills" / "_shared"
    active_shared.mkdir(parents=True)
    (active_root / "bin").mkdir()
    (active_root / "bin" / resolver).write_bytes((plugin_path / "bin" / resolver).read_bytes())
    contract_name = f"codemap-py--{wrapper_path.name}"
    if contract_present:
        (active_shared / contract_name).write_bytes(b"active contract\n")
    newer_shared = (
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "borda-ai-rig"
        / plugin_path.name.removeprefix("cc_")
        / "99.0.0"
        / "skills"
        / "_shared"
    )
    newer_shared.mkdir(parents=True)
    (newer_shared / contract_name).write_bytes(b"newer cached contract\n")
    script = wrapper_path.read_text(encoding="utf-8").split("```bash\n", 1)[1].split("```", 1)[0]
    prelude = 'python() { "$TEST_PYTHON" "$@"; }\ncodemap-py() { :; }\n'
    result = subprocess.run(
        ["bash", "-c", prelude + script],
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": tmp_path.as_posix(),
            "USERPROFILE": str(tmp_path),
            "CLAUDE_PLUGIN_ROOT": active_root.as_posix(),
            "TEST_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    fallback = "codemap gates contract absent" if wrapper_path.stem == "codemap-gates" else "codemap contract absent"
    expected = "active contract\n" if contract_present else f"{fallback} — use fallback below\n"
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    assert result.stderr == ""
