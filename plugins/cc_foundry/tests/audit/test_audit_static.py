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
    (plugin / "agents").mkdir()
    (plugin / "agents" / "bad.md").write_text("<role>\na role with no closing tag\n", encoding="utf-8")

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


def test_jsonl_output_written(seeded_jsonl: Path) -> None:
    """Verify command-line option behavior.

    --jsonl writes one parseable JSON object per check.
    """
    lines = seeded_jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(aud.CHECKS)
    for line in lines:
        obj = json.loads(line)
        assert {"check", "status", "findings", "lines"} <= obj.keys()
