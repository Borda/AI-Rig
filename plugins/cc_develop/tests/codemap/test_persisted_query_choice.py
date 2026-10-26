"""Execute fresh consumer shells and nested helpers against the real Codemap provider."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
BASH = shutil.which("bash")


@pytest.mark.integration
@pytest.mark.skipif(BASH is None, reason="documented consumer recipes require bash")
@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="the codemap-py entry point rejects CPython below 3.11, the provider's interpreter",
)
@pytest.mark.parametrize(
    "route",
    [
        "oss-qa",
        "oss-docs",
        "develop-review",
        "develop-feature",
        "develop-plan",
        "develop-debug",
        "develop-refactor",
        "research",
    ],
)
@pytest.mark.parametrize("choice", ["stale", "current", "inherited-freeze", "skip"])
def test_fresh_query_shell_retains_gate_choice(tmp_path: Path, route: str, choice: str) -> None:
    """The actual recipe must carry persisted state into every provider subprocess, including fallback."""
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    (project / "leaf.py").write_text("def _target():\n    return 1\n", encoding="utf-8")
    caller = project / "caller.py"
    caller.write_text("VALUE = 1\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "queries.jsonl"
    results_log = tmp_path / "query-results.jsonl"
    provider_script = bin_dir / "provider.py"
    provider_script.write_text(
        "import json,os,subprocess,sys\n"
        "with open(os.environ['QUERY_LOG'],'a') as f:\n"
        " f.write(json.dumps({'args':sys.argv[1:],'freeze':os.environ.get('SCAN_NO_AUTOBUILD')})+'\\n')\n"
        "if os.environ.get('FAIL_BATCH') == '1' and 'batch' in sys.argv: raise SystemExit(1)\n"
        f"result=subprocess.run([sys.executable,{str(ROOT / 'plugins/codemap-py/scripts/codemap_py_entry.py')!r},*sys.argv[1:]],capture_output=True)\n"
        "with open(os.environ['QUERY_RESULTS_LOG'],'a',encoding='utf-8') as f:\n"
        " f.write(json.dumps({'args':sys.argv[1:],'returncode':result.returncode,'stdout':result.stdout.decode('utf-8',errors='backslashreplace'),'stderr':result.stderr.decode('utf-8',errors='backslashreplace')})+'\\n')\n"
        "sys.stdout.buffer.write(result.stdout)\n"
        "sys.stderr.buffer.write(result.stderr)\n"
        "raise SystemExit(result.returncode)\n",
        encoding="utf-8",
        newline="\n",
    )
    provider = bin_dir / "codemap-py"
    provider.write_text(
        f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "{provider_script.as_posix()}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    provider.chmod(0o700)
    if os.name == "nt":
        (bin_dir / "codemap-py.cmd").write_text(
            f'@"{sys.executable}" "{provider_script}" %*\r\n', encoding="utf-8", newline=""
        )
    python = bin_dir / "python"
    python.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding="utf-8", newline="\n")
    python.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "TMPDIR": str(tmp_path),
        "CSID": "choice",
        "CLAUDE_CODE_SESSION_ID": "choice",
        "QUERY_LOG": str(log),
        "QUERY_RESULTS_LOG": str(results_log),
        "TARGET_MODULE": "leaf",
        "TARGET_FN": "",
        "CODEMAP_INDEX_DIR": str(tmp_path / "index"),
        "CODEMAP_LOG_DIR": str(tmp_path / "logs"),
        "CODEMAP_COORDINATION_DIR": str(tmp_path / "coordination"),
    }
    for key in ("SCAN_NO_AUTOBUILD", "_CODEMAP_NO_HEAL", "CODEMAP_BIN"):
        env.pop(key, None)
    built = subprocess.run(
        [sys.executable, str(provider_script), "index"], cwd=project, env=env, capture_output=True, text=True
    )
    assert built.returncode == 0, built.stderr
    results_log.write_text("", encoding="utf-8")
    index = tmp_path / "index/project.json"
    original = index.read_bytes()
    caller.write_text("import leaf\n", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    enabled = "false" if choice == "skip" else "true"
    currency = "stale" if choice == "stale" else "current"
    if choice == "inherited-freeze":
        env["SCAN_NO_AUTOBUILD"] = "1"
    if route.startswith("oss-"):
        plugin, prefix = "cc_oss", "review"
        text = (ROOT / "plugins/cc_oss/skills/review/modes/codemap-context.md").read_text(encoding="utf-8")
        block = text.split(f"{route.removeprefix('oss-')} block (", 1)[1].split("```bash\n", 1)[1].split("\n```", 1)[0]
        stage = tmp_path / "stage.md"
        stage.write_text("context\n", encoding="utf-8", newline="\n")
        (tmp_path / "oss-review-pr-tag-choice").write_text("target\n", encoding="utf-8", newline="\n")
        (tmp_path / "oss-review-codemap-context-stage-target-choice").write_text(
            str(stage) + "\n", encoding="utf-8", newline="\n"
        )
        (tmp_path / "oss-review-changed-mods-target-choice").write_text("leaf\n", encoding="utf-8", newline="\n")
        env["FAIL_BATCH"] = "1"  # Exercise the real renderer's per-query fallback children.
    elif route.startswith("develop-"):
        plugin, prefix = "cc_develop", "dev"
        skill = route.removeprefix("develop-")
        text = (ROOT / f"plugins/cc_develop/skills/{skill}/SKILL.md").read_text(encoding="utf-8")
        needle = {
            "review": "codemap_available=false",
            "feature": "codemap-py query --timeout 5 rdeps",
            "plan": "sizing-rdeps",
            "debug": "codemap-py query central",
            "refactor": "REFACTOR_FILES=",
        }[skill]
        block = next(m.group(1) for m in re.finditer(r"```bash\n(.*?)\n```", text, re.S) if needle in m.group(1))
        (tmp_path / f"dev-{skill}-codemap-enabled-choice").write_text(enabled + "\n", encoding="utf-8", newline="\n")
        (tmp_path / f"dev-{skill}-target-module-choice").write_text("leaf\n", encoding="utf-8", newline="\n")
        if skill == "plan":
            namespace = tmp_path / "plan"
            namespace.mkdir()
            (tmp_path / "dev-plan-ns-current-choice").write_text(str(namespace) + "\n", encoding="utf-8", newline="\n")
            (namespace / "codemap-enabled").write_text(enabled + "\n", encoding="utf-8", newline="\n")
            (namespace / "target-module").write_text("leaf\n", encoding="utf-8", newline="\n")
        if skill == "refactor":
            block = block.replace("find <target>", "find .")
        if skill == "review":
            # The same freshly hydrated shell owns nested impact and scan helper subprocesses.
            block += '\nif [ "$CODEMAP_ENABLED" = true ]; then\n'
            payload = json.dumps({"changed_modules": [{"path": "leaf.py", "changed_symbols": ["leaf::_target"]}]})
            for owner in ("cc_oss", "cc_develop"):
                block += (
                    f"printf '%s' '{payload}' | python '{ROOT / 'plugins' / owner / 'bin/review_impact_tier.py'}'\n"
                )
            block += f"python '{ROOT / 'plugins/cc_develop/bin/codemap_scan.py'}' --source=find --target=.\n"
            block += f"python '{ROOT / 'plugins/cc_oss/bin/resolve_test_plan.py'}' targeted --files leaf.py\n"
            block += f"python '{ROOT / 'plugins/cc_develop/bin/dev_test_targets.py'}' --pytest-cmd pytest --files leaf.py\nfi\n"
    else:
        plugin, prefix = "cc_research", "research"
        text = (ROOT / "plugins/cc_research/skills/_shared/codemap-context.md").read_text(encoding="utf-8")
        block = text.split("```bash\n", 1)[1].split("\n```", 1)[0]
        env["CODEMAP_SENTINEL_SLUG"] = "research-run"
        (tmp_path / "research-run-codemap-enabled-choice").write_text(enabled + "\n", encoding="utf-8", newline="\n")
    (tmp_path / f"{prefix}-codemap-enabled-choice").write_text(enabled + "\n", encoding="utf-8", newline="\n")
    (tmp_path / f"{prefix}-codemap-currency-choice").write_text(currency + "\n", encoding="utf-8", newline="\n")
    env["CLAUDE_PLUGIN_ROOT"] = str(ROOT / "plugins" / plugin)
    result = subprocess.run([BASH, "-c", block], cwd=project, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    queries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    diagnostics = results_log.read_text(encoding="utf-8")
    completed = [json.loads(line) for line in diagnostics.splitlines()]
    expected_completed = [item for item in queries if not (env.get("FAIL_BATCH") == "1" and "batch" in item["args"])]
    assert Counter(tuple(item["args"]) for item in completed) == Counter(
        tuple(item["args"]) for item in expected_completed
    ), diagnostics
    # The deliberately uncommitted fixture has no HEAD for diff-impact; the caller must fall back.
    assert all(item["returncode"] == (1 if "diff-impact" in item["args"] else 0) for item in completed), diagnostics
    assert all(
        json.loads(item["stdout"])["error"] == "git diff failed" for item in completed if "diff-impact" in item["args"]
    ), diagnostics
    if choice == "skip":
        assert queries == []
        assert index.read_bytes() == original
    elif choice in {"stale", "inherited-freeze"}:
        assert queries
        assert all(item["freeze"] == "1" for item in queries), queries
        assert index.read_bytes() == original
    else:
        assert queries
        assert all(item["freeze"] is None for item in queries), queries
        assert index.read_bytes() != original, diagnostics
    if route.startswith("oss-") and choice != "skip":
        assert len(queries) == 3  # Primary batch plus both real fallback queries.

    if route == "develop-review" and choice != "skip":
        assert sum("fn-blast" in item["args"] for item in queries) == 2, diagnostics
        assert any("rdeps" in item["args"] for item in queries), diagnostics
        assert sum("test-impact" in item["args"] for item in queries) == 2, diagnostics
