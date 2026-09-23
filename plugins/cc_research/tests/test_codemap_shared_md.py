"""Regression tests for the codemap prose/bash shared by research skills and the scientist agent.

Pinned failure modes:

* Research's gates were an unversioned inline copy: no contract resolution block, no
  ``cat`` of the shipped contract, no version marker, so the provider could move to v3 with
  research silently frozen at its transcribed v2 text.
* The wrappers named a build command their own loaded contract does not use, inside
  the same sentence that says "apply verbatim".
* The shipped contract now names the gated ``codemap-py index`` launcher itself, so
  the former override clause announced a deviation from text it already agrees with
  and kept the retired alias alive in a wrapper that never invokes it.
* ``PROJ=$(basename "$(git rev-parse ...)") || PROJ=$(basename "$PWD")`` never fired its
  fallback: ``basename ""`` exits 0, so in a non-git project PROJ was empty and codemap was
  silently off.
* ``_IDX`` defaulted to a CWD-relative ``.cache/codemap``.
* The retired ``codemap:`` skill prefix.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RESEARCH = _REPO_ROOT / "plugins" / "cc_research"
_GATES = _RESEARCH / "skills" / "_shared" / "codemap-gates.md"
_CONTEXT = _RESEARCH / "skills" / "_shared" / "codemap-context.md"
_SCIENTIST = _RESEARCH / "agents" / "scientist.md"

_BASH_BLOCK_FILES = [_CONTEXT, _SCIENTIST]


def test_context_distinguishes_static_links_from_measured_coverage() -> None:
    """Research cannot infer runtime coverage from absent static callers or an empty package."""
    text = _CONTEXT.read_text(encoding="utf-8")
    assert "no test coverage" not in text
    for phrase in ("static test callers", "measured line coverage", "exact module", "unknown, not zero"):
        assert phrase in text


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(p, id=p.relative_to(_RESEARCH).as_posix())
        for p in [*_BASH_BLOCK_FILES, _RESEARCH / "skills/run/SKILL.md", _RESEARCH / "skills/verify/SKILL.md"]
    ],
)
def test_context_allows_source_verification_and_gates_answer_reuse(path: Path) -> None:
    """Neither stale answers nor an old legacy flag may prohibit required source reads.

    ``run``/``verify`` no longer duplicate the reuse-gate prose inline — they ``cat`` and reference ``codemap-
    context.md`` at runtime instead, so their effective text includes its contract.
    """
    text = path.read_text(encoding="utf-8")
    if path not in _BASH_BLOCK_FILES and "codemap-context.md" in text:
        text += _CONTEXT.read_text(encoding="utf-8")
    for phrase in ("only when `query_complete` is absent", "`stale`", "source-body", "valid empty"):
        assert phrase in text


def _find_posix_bash() -> str | None:
    """Return a bash that runs POSIX script syntax, if the host provides one."""
    if sys.platform != "win32":
        candidates = ["bash"]
    else:
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles(x86)")]
        candidates = [
            *(str(Path(root) / "Git" / sub / "bash.exe") for root in roots if root for sub in ("bin", "usr/bin")),
            *([shutil.which("bash")] if shutil.which("bash") else []),
        ]
    for candidate in candidates:
        try:
            probe = subprocess.run([candidate, "-c", "printf ok"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "ok":
            return candidate
    return None


_POSIX_BASH = _find_posix_bash()


@pytest.mark.parametrize(
    ("exit_code", "payload", "accepted"),
    [
        pytest.param(0, '{"callers": [], "index": {"query_complete": true}}', True, id="valid-empty"),
        pytest.param(1, '{"callers": []}', False, id="failed-process-with-output"),
        pytest.param(0, '{"error": "not indexed"}', False, id="structured-error"),
        pytest.param(0, "", False, id="missing-output"),
    ],
)
@pytest.mark.skipif(_POSIX_BASH is None, reason="requires a working POSIX bash")
def test_query_wrapper_preserves_failure_vs_empty_answer(
    exit_code: int, payload: str, accepted: bool, tmp_path: Path
) -> None:
    """Run the shipped query wrapper against a CLI boundary, retaining valid empty answers."""
    block = next(b for b in _bash_blocks(_CONTEXT) if "_cq()" in b)
    function = re.search(r"    _cq\(\) \{.*?\n    \}", block, re.DOTALL)
    assert function is not None
    script = (
        'codemap-py() { printf "%s" "$PROBE_PAYLOAD"; return "$PROBE_EXIT"; }\n'
        "_CM_N=0 _CM_H=0\n" + function.group() + "\n_cq rdeps pkg.mod\n"
        'printf "hits=%s" "$_CM_H" >&2\n'
    )
    result = subprocess.run(
        [_POSIX_BASH, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=os.environ | {"PROBE_PAYLOAD": payload, "PROBE_EXIT": str(exit_code)},
        check=True,
    )
    assert result.stdout.strip() == (payload if accepted else "")
    assert f"hits={int(accepted)}" in result.stderr
    if not accepted:
        assert "unavailable" in result.stderr


def _bash_blocks(path: Path) -> list[str]:
    """Return the bodies of every ```bash fenced block in *path*.

    Examples:
        >>> path = getfixture("tmp_path") / "guide.md"
        >>> _ = path.write_text("```bash\\necho ready\\n```\\n")
        >>> _bash_blocks(path)
        ['echo ready\\n']
    """
    return re.findall(r"```bash\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL)


# --------------------------------------------------------------------------------------
# Versioned wrapper over the shipped contract
# --------------------------------------------------------------------------------------


def test_gates_resolve_and_read_the_shipped_contract():
    """An inline transcription cannot replace the contract read from the active codemap-py install.

    Optional-provider exception: the gates have no value without codemap-py, so the wrapper reads the
    installed provider's file through its own resolver copy (registry tier first) instead of a frozen
    local copy — and never globs the cache itself.
    """
    text = _GATES.read_text(encoding="utf-8")

    assert (
        '"${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve_shared_path.py" codemap-py claude-skills/_shared'
        in text
    ), "provider contract is not resolved from the active install"
    assert 'cat "$_CODEMAP_SHARED/codemap-gates.md"' in text, "contract is never loaded"
    assert "codemap-py--" not in text, "manifested copy reintroduced"
    assert "plugins/cache" not in text, "newest-version cache glob reintroduced"
    assert "Contract (version as loaded)" in text, "no contract marker"
    assert "Fallback when codemap-py plugin absent" in text, "no graceful degradation"


def test_gates_do_not_transcribe_the_contract_body():
    """Re-listing the gate options inline is exactly the drift the wrapper removes."""
    text = _GATES.read_text(encoding="utf-8")

    assert "No codemap index for this project" not in text
    assert "Continue with stale data" not in text


def test_build_command_is_applied_from_the_contract_without_an_override():
    """Ensure the contract names the gated dispatcher without claiming an override.

    An earlier contract required the override to be stated because it genuinely named a different command. With the
    contract aligned, "with one override" describes a disagreement that no longer exists, and the retired alias must not
    survive in the wrapper.
    """
    text = _GATES.read_text(encoding="utf-8")

    assert "codemap-py index" in text
    assert "apply verbatim, with one override" not in text, "the contract no longer disagrees"
    assert "scan-index" not in text, "the retired alias has no reason to appear in a wrapper"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(_GATES, id="gates"),
        pytest.param(_CONTEXT, id="context"),
        pytest.param(_SCIENTIST, id="scientist"),
    ],
)
def test_no_retired_codemap_skill_prefix(path: Path) -> None:
    """The plugin is `codemap-py:`; the bare `codemap:<skill>` prefix is retired.

    The skill name must follow the colon immediately, so a prose colon is not flagged.
    """
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"(?<![\w-])codemap:[a-z]", text), f"retired `codemap:<skill>` prefix in {path.name}"


# --------------------------------------------------------------------------------------
# Root-anchored index directory
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [pytest.param(p, id=p.name) for p in _BASH_BLOCK_FILES])
def test_index_dir_is_root_anchored(path: Path):
    """A CWD-relative default reports no_index whenever the session sits in a subdir."""
    blocks = [b for b in _bash_blocks(path) if "_IDX=" in b]
    assert blocks, f"no _IDX block found in {path.name}"
    for block in blocks:
        assert "${CODEMAP_INDEX_DIR:-.cache/codemap}" not in block, "CWD-relative default"
        assert "${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}" in block


# --------------------------------------------------------------------------------------
# The project-name fallback actually fires
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [pytest.param(p, id=p.name) for p in _BASH_BLOCK_FILES])
@pytest.mark.skipif(_POSIX_BASH is None, reason="requires a working POSIX bash")
def test_project_name_fallback_fires_outside_a_git_repository(path: Path, tmp_path: Path):
    """Exit 0, so the old `||` fallback was unreachable and PROJ went empty."""
    block = next(b for b in _bash_blocks(path) if "_ROOT=" in b)
    snippet = "\n".join(
        line for line in block.splitlines() if line.strip().startswith(("_ROOT=", '[ -n "$_ROOT"', "PROJ="))
    )
    assert snippet.strip(), f"no root/PROJ derivation found in {path.name}"

    workdir = tmp_path / "loose project"
    workdir.mkdir()
    result = subprocess.run(
        [_POSIX_BASH, "-c", f'{snippet}\nprintf "%s" "$PROJ"'],
        cwd=workdir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        # A stray parent repo would mask the non-git path this test exists to exercise. The
        # ceiling is what does that; the environment is otherwise inherited, because replacing
        # it with a POSIX PATH literal also dropped SystemRoot, COMSPEC and the rest of the
        # set a Windows child needs before it can start at all.
        env=os.environ | {"GIT_CEILING_DIRECTORIES": str(tmp_path)},
        check=True,
    )

    assert result.stdout == "loose project", f"{path.name}: PROJ was {result.stdout!r}"
