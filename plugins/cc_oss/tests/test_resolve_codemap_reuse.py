"""Execute the shipped caller-context block to prevent repeated and empty-cache queries."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import codemap_cache


@pytest.mark.integration
@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize(
    "cached",
    [
        False,
        True,
        "missing-callers",
        "error",
        "wrong-module",
        "incomplete",
        "stale",
        "missing-index",
        "missing-completeness",
        "null-completeness",
        "legacy-complete",
        "forward-wins",
        "forward-incomplete",
    ],
)
@pytest.mark.parametrize(
    "shell",
    [
        pytest.param(
            name,
            id=name,
            marks=pytest.mark.skipif(
                shutil.which(name) is None or shutil.which("jq") is None,
                reason="Shipped shell block requires this shell and jq.",
            ),
        )
        for name in ("bash", "zsh")
    ],
)
def test_one_query_per_module_in_shipped_preloop(
    tmp_path: Path, capsys, cached: bool | str, shell: str, refresh: bool
) -> None:
    """Share empty caller answers and mappings; refresh invalidates mapping reuse."""
    plugin = Path(__file__).resolve().parents[1]
    source = (plugin / "skills/resolve/modes/action-item-dispatch.md").read_text()
    block = next(
        block for block in re.findall(r"```bash\n(.*?)```", source, re.DOTALL) if 'BLAST_RADIUS_CONTEXT=""' in block
    )
    structural = next(block for block in re.findall(r"```bash\n(.*?)```", source, re.DOTALL) if "DEPS_MAP=" in block)
    impl = tmp_path / "impl"
    impl.mkdir()
    (impl / "selected-items.txt").write_text("1 2 3 4 5\n")
    (impl / "action-items.jsonl").write_text(
        "".join(json.dumps({"id": item, "file": "pkg/mod.py"}) + "\n" for item in range(1, 6))
    )
    (tmp_path / "resolve-impl-dir-test-session").write_text(str(impl) + "\n")
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    index = index_dir / f"{tmp_path.name}.json"
    index.write_text(json.dumps({"git_sha": "abc", "scanned_at": "2026-09-01T00:00:00Z"}))
    cache = tmp_path / "cache"
    if cached:
        batch = tmp_path / "batch.json"
        batch.write_text(
            json.dumps(
                {
                    "batch": [
                        {
                            "cmd": "rdeps",
                            "ok": True,
                            "result": {"module": "pkg.mod", "imported_by": [], "index": {"query_complete": True}},
                        }
                    ]
                }
            )
        )
        assert (
            codemap_cache.main(["write", "--batch", str(batch), "--index", str(index), "--cache-dir", str(cache)]) == 0
        )
        capsys.readouterr()
        (tmp_path / "resolve-codemap-cache-dir-test-session").write_text(str(cache) + "\n")
        if isinstance(cached, str):
            artifact_path = cache / "pkg.mod.json"
            artifact = json.loads(artifact_path.read_text())
            answer = artifact["prefix"]["answers"]["rdeps"]
            if cached == "missing-callers":
                answer.pop("imported_by")
            elif cached == "error":
                answer["error"] = "failed"
            elif cached == "wrong-module":
                answer["module"] = "pkg.other"
            elif cached == "missing-index":
                answer.pop("index")
            elif cached == "missing-completeness":
                answer["index"] = {}
            elif cached == "null-completeness":
                answer["index"] = {"query_complete": None, "exhaustive": True}
            elif cached == "legacy-complete":
                answer["index"] = {"exhaustive": True}
            elif cached == "forward-wins":
                answer["index"] = {"query_complete": True, "exhaustive": False}
            elif cached == "forward-incomplete":
                answer["index"] = {"query_complete": False, "exhaustive": True}
            else:
                answer["index"] = {"query_complete": cached != "incomplete", "stale": cached == "stale"}
            artifact["prefix"]["content_hash"] = codemap_cache._content_hash(artifact["prefix"]["answers"])
            artifact_path.write_text(json.dumps(artifact))
    central = json.dumps({"central": [{"name": "pkg.mod", "path": "pkg/mod.py", "rdep_count": 0}]})
    answer = json.dumps({"module": "pkg.mod", "imported_by": []})
    # Only external commands are substituted; the skill's real cache helper and loop execute.
    prelude = f"""
python() {{ {shlex.quote(sys.executable)} "$@"; }}
git() {{ printf '%s\\n' "$PWD"; }}
codemap-py() {{
    printf '%s\\n' "$*" >> calls.txt
    case "$*" in
        *central*) printf '%s\\n' {shlex.quote(central)} ;;
        *rdeps*) printf '%s\\n' {shlex.quote(answer)} ;;
        *deps*) printf '%s\\n' '{{"module":"pkg.mod","direct_imports":[]}}' ;;
    esac
}}
"""
    env = {
        **os.environ,
        "TMPDIR": str(tmp_path),
        "CLAUDE_CODE_SESSION_ID": "test-session",
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "CODEMAP_INDEX_DIR": str(index_dir),
    }
    between = '\nprintf "%s" "$BLAST_RADIUS_CONTEXT"\n'
    if refresh:
        between += (
            "python -c "
            + shlex.quote(f"from pathlib import Path; Path({str(index)!r}).write_text('changed-index')")
            + "\n"
        )
    result = subprocess.run(
        [shutil.which(shell), "-c", prelude + block + between + structural],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.txt").read_text().splitlines()
    reusable = cached is True or cached in ("legacy-complete", "forward-wins")
    assert sum("rdeps" in call for call in calls) == (0 if reusable else 1), calls
    assert sum("central" in call for call in calls) == (2 if refresh else 1), calls
    assert sum("query deps " in call for call in calls) == 1, calls
    for item in range(1, 6):
        assert f"item #{item} (pkg.mod) callers:" in result.stdout
    assert '"imported_by": []' in result.stdout
