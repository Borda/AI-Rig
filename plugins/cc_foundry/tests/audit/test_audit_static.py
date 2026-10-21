"""Seeded-defect benchmark for ``bin/audit_static.py`` (Phase-5 Layer 1).

Plants a known mechanical defect for each scope-aware deterministic checker in a disposable plugin tree, then asserts
the driver's Layer-1 pass catches every one (100% recall on mechanical classes). The whole-repo checks (routing-links,
orphaned-bin, shared-drift) walk the real repo rather than the scope, so they are not part of the seeded-recall
assertion — only the scope-aware checks are.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parent.parent.parent / "bin" / "audit_static.py"
_spec = importlib.util.spec_from_file_location("audit_static", _MOD_PATH)
assert _spec
assert _spec.loader
aud = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aud)

# Checks whose scope is honoured via --scan-dir / globbed files (testable in isolation).
SCOPE_AWARE = {
    "tag-symmetry",
    "fence-symmetry",
    "readme-drift",
    "mode-dispatch",
    "bash-persistence",
    "plugin-module-docs",
    "agent-plugin-root",
}

_FENCE = "```"


def _seed_defective_plugin(root: Path) -> Path:
    """Create a plugins/ tree with one deliberately-defective plugin; return the plugins dir."""
    plugins = root / "plugins"
    plugin = plugins / "myplugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "myplugin", "version": "1.0.0"}', encoding="utf-8")

    # readme-drift: marker (0.0.1) disagrees with plugin.json (1.0.0)
    (plugin / "README.md").write_text("Current version: `0.0.1`.\n", encoding="utf-8")

    # plugin-module-docs: a shipped module without its maintainer-facing overview.
    (plugin / "bin").mkdir()
    (plugin / "bin" / "missing_docs.py").write_text("print('missing docs')\n", encoding="utf-8")

    # tag-symmetry: unbalanced <role> (open, never closed)
    # agent-plugin-root: an agent prompt never has the defaulted `${CLAUDE_PLUGIN_ROOT:-...}` form substituted
    (plugin / "agents").mkdir()
    (plugin / "agents" / "bad.md").write_text(
        '<role>\na role with no closing tag\ncat "${CLAUDE_PLUGIN_ROOT:-plugins/myplugin}/refs/a.md"\n',
        encoding="utf-8",
    )

    # fence-symmetry: an opening bash fence with no closing fence
    (plugin / "skills" / "fencebad").mkdir(parents=True)
    (plugin / "skills" / "fencebad" / "SKILL.md").write_text(f"## Step\n\n{_FENCE}bash\necho hi\n", encoding="utf-8")

    # bash-persistence: $RUN_ID assigned in block 1, referenced in block 2 (fresh-shell loss)
    # mode-dispatch: `go to "Mode: Ghost"` with no `## Mode: Ghost` header
    (plugin / "skills" / "persist").mkdir(parents=True)
    (plugin / "skills" / "persist" / "SKILL.md").write_text(
        f"## Step 1\n\n{_FENCE}bash\nRUN_ID=$(date)\n{_FENCE}\n\n"
        f'## Step 2\n\n{_FENCE}bash\necho "$RUN_ID"\n{_FENCE}\n\n'
        'If done, go to "Mode: Ghost" below.\n',
        encoding="utf-8",
    )

    # bash-persistence on real block shapes: each later block carries something that once masked the lost variable
    # (the standard CSID preamble, a read-back of a different variable, a single-quoted jq program, an unassigned
    # `<placeholder>` token), so the plain case above does not prove recall for them.
    (plugin / "skills" / "shapes").mkdir(parents=True)
    (plugin / "skills" / "shapes" / "SKILL.md").write_text(
        f"## Step 1\n\n{_FENCE}bash\nPREAMBLE_LOST=$(date)\nREAD_LOST=$(date)\nJQ_LOST=$(date)\nANGLE_LOST=$(date)\n{_FENCE}\n\n"
        f'## Step 2\n\n{_FENCE}bash\nexport CSID="${{CLAUDE_CODE_SESSION_ID:-$PPID}}"\necho "$PREAMBLE_LOST"\n{_FENCE}\n\n'
        f'## Step 3\n\n{_FENCE}bash\nIFS= read -r OTHER < "$HOME/state" 2>/dev/null || OTHER=""\n'
        f'echo "$OTHER $READ_LOST"\n{_FENCE}\n\n'
        f"## Step 4\n\n{_FENCE}bash\njq '.[] | select(.name == $cmd)' file.json\necho \"$JQ_LOST\"\n{_FENCE}\n\n"
        f'## Step 5\n\n{_FENCE}bash\necho "<RUN_ID> $ANGLE_LOST"\n{_FENCE}\n',
        encoding="utf-8",
    )
    return plugins


@pytest.fixture(scope="module")
def seeded_jsonl(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run all native checkers through the CLI once and retain their JSONL output."""
    root = tmp_path_factory.mktemp("seeded-audit")
    plugins = _seed_defective_plugin(root)
    out = root / "static.jsonl"
    assert aud.main(["--scan-dir", str(plugins), "--jsonl", str(out)]) == 1
    return out


@pytest.fixture(scope="module")
def seeded_results(seeded_jsonl: Path) -> dict[str, dict]:
    """Read the shared native-checker results for the seeded-defect assertions."""
    results = [json.loads(line) for line in seeded_jsonl.read_text(encoding="utf-8").splitlines()]
    return {result["check"]: result for result in results}


@pytest.mark.parametrize(
    ("check", "expected_file", "expected_text"),
    [
        pytest.param("tag-symmetry", "agents/bad.md", "unbalanced <role>", id="tag-symmetry"),
        pytest.param("fence-symmetry", "skills/fencebad/SKILL.md", "unclosed fence", id="fence-symmetry"),
        pytest.param("readme-drift", "README.md", "0.0.1", id="readme-drift"),
        pytest.param("mode-dispatch", "skills/persist/SKILL.md", "Mode: Ghost", id="mode-dispatch"),
        pytest.param("bash-persistence", "skills/persist/SKILL.md", "$RUN_ID assigned", id="bash-persistence"),
        pytest.param("plugin-module-docs", "bin/missing_docs.py", "missing module docstring", id="plugin-module-docs"),
        pytest.param(
            "agent-plugin-root", "agents/bad.md", "${CLAUDE_PLUGIN_ROOT:-plugins/myplugin}", id="agent-plugin-root"
        ),
        pytest.param(
            "bash-persistence", "skills/shapes/SKILL.md", "$PREAMBLE_LOST assigned", id="bash-persistence-csid-preamble"
        ),
        pytest.param(
            "bash-persistence", "skills/shapes/SKILL.md", "$READ_LOST assigned", id="bash-persistence-other-var-read"
        ),
        pytest.param(
            "bash-persistence", "skills/shapes/SKILL.md", "$JQ_LOST assigned", id="bash-persistence-jq-program"
        ),
        pytest.param(
            "bash-persistence",
            "skills/shapes/SKILL.md",
            "$ANGLE_LOST assigned",
            id="bash-persistence-angle-placeholder",
        ),
    ],
)
def test_layer1_catches_seeded_defect(
    seeded_results: dict[str, dict], check: str, expected_file: str, expected_text: str
) -> None:
    """Each scope-aware checker flags its own planted defect."""
    result = seeded_results[check]
    lines = "\n".join(result["lines"])
    normalized_lines = lines.replace("\\", "/")
    assert result["status"] == "fail", f"{check} missed its seeded defect: {result}"
    assert result["findings"] >= 1
    assert expected_file in normalized_lines
    assert expected_text in lines


def test_clean_scope_passes_scope_aware_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A defect-free plugin passes every scope-aware check (no false positives).

    Only the scope-aware checks are asserted, so only they run: the whole-repo checks ignore the clean scope and walk
    the real repository, which dominated this test's runtime (about 28 s per Windows CI leg) without informing it.
    ``seeded_jsonl`` still runs the full registry through the CLI, so every checker keeps a real subprocess run.
    """
    monkeypatch.setattr(aud, "CHECKS", [check for check in aud.CHECKS if check["id"] in SCOPE_AWARE])
    plugins = tmp_path / "plugins"
    plugin = plugins / "clean"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "clean", "version": "1.0.0"}', encoding="utf-8")
    (plugin / "README.md").write_text("Current version: `1.0.0`.\n", encoding="utf-8")
    (plugin / "skills" / "ok").mkdir(parents=True)
    (plugin / "skills" / "ok" / "SKILL.md").write_text(
        f"## Step\n\n{_FENCE}bash\necho hi\n{_FENCE}\n", encoding="utf-8"
    )

    results = {r["check"]: r for r in aud.run_checks(plugins)}
    for check in SCOPE_AWARE:
        assert results[check]["status"] in {"pass", "skipped"}, f"{check} false-positived: {results[check]}"


@pytest.mark.parametrize(
    ("check", "expected_text"),
    [
        pytest.param("readme-drift", "0.0.1", id="readme-drift"),
        pytest.param("mode-dispatch", "Mode: Ghost", id="mode-dispatch"),
        pytest.param("tag-symmetry", "unbalanced <role>", id="tag-symmetry"),
    ],
)
def test_single_plugin_scope_reports_real_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check: str, expected_text: str
) -> None:
    """A single-plugin scope still catches the planted defect instead of a findingless usage-error FAIL.

    Both checkers read ``--scan-dir`` as a plugins root, so a per-plugin re-audit (``--scan-dir plugins/<one>``) used to
    hand them a directory of skill folders: they exited 2 with no finding lines, a false FAIL on every clean plugin.
    """
    monkeypatch.setattr(aud, "CHECKS", [entry for entry in aud.CHECKS if entry["id"] == check])
    plugin = _seed_defective_plugin(tmp_path) / "myplugin"

    result = aud.run_checks(plugin)[0]

    assert result["status"] == "fail"
    assert result["findings"] >= 1
    assert expected_text in "\n".join(result["lines"])


def test_checker_usage_error_is_reported_as_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A checker that exits non-zero without printing findings is an ``error`` row naming its stderr, not a FAIL.

    A usage error (bad scope, missing argument) used to read as ``fail`` with 0 findings, so a summary of "0 finding(s)"
    hid a check that never ran.
    """
    script = tmp_path / "usage_error.py"
    script.write_text("import sys\nsys.stderr.write('usage: bad scope\\n')\nsys.exit(2)\n", encoding="utf-8")
    monkeypatch.setattr(aud, "BIN", tmp_path)
    monkeypatch.setattr(aud, "CHECKS", [{"id": "usage", "kind": "whole", "script": "usage_error.py"}])

    result = aud.run_checks(tmp_path)[0]

    assert result["status"] == "error"
    assert result["lines"] == ["usage: bad scope"]


def test_files_kind_argv_separates_option_shaped_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Globbed file names follow a ``--`` separator so a leading ``-`` is never parsed as a checker option.

    With scope ``.`` the glob yields a bare ``-x.md`` for a top-level file of that name; passed unseparated, the
    checker's argparse would treat it as an option and scan the wrong thing or fail on an unknown flag.
    """
    (tmp_path / "-x.md").write_text("text\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    check = {"kind": "files", "script": "check_fence_symmetry.py", "globs": ["**/*.md"]}

    argv = aud._argv(check, Path("."))

    assert argv is not None
    assert argv[2:] == ["--", "-x.md"]


def test_jsonl_output_written(seeded_jsonl: Path) -> None:
    """Verify command-line option behavior.

    --jsonl writes one parseable JSON object per check.
    """
    lines = seeded_jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(aud.CHECKS)
    for line in lines:
        obj = json.loads(line)
        assert {"check", "status", "findings", "lines"} <= obj.keys()
