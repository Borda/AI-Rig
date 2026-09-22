"""Execute the shipped caller-context block to prevent repeated and empty-cache queries."""

from __future__ import annotations

import io
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
@pytest.mark.parametrize("line_endings", ["lf", "crlf"])
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
    tmp_path: Path,
    capsys,
    cached: bool | str,
    shell: str,
    refresh: bool,
    line_endings: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Share caller answers and mappings under a Windows-style default text codec."""
    # Exercise the Windows default codec on every host, without changing the real shell or cache helper.
    monkeypatch.setattr(io, "text_encoding", lambda encoding, stacklevel=2: encoding or "cp1252")
    plugin = Path(__file__).resolve().parents[1]
    source = (plugin / "skills/resolve/modes/action-item-dispatch.md").read_text(encoding="utf-8")
    block = next(
        block for block in re.findall(r"```bash\n(.*?)```", source, re.DOTALL) if 'BLAST_RADIUS_CONTEXT=""' in block
    )
    structural = next(block for block in re.findall(r"```bash\n(.*?)```", source, re.DOTALL) if "DEPS_MAP=" in block)
    impl = tmp_path / "impl"
    impl.mkdir()
    (impl / "selected-items.txt").write_text("1 2 3 4 5\n", encoding="utf-8", newline="\n")
    (impl / "action-items.jsonl").write_text(
        "".join(json.dumps({"id": item, "file": "pkg/mod.py"}) + "\n" for item in range(1, 6)),
        encoding="utf-8",
        newline="\n",
    )
    (tmp_path / "resolve-impl-dir-test-session").write_text(str(impl) + "\n", encoding="utf-8", newline="\n")
    index_dir = tmp_path / "index with spaces"
    index_dir.mkdir()
    index = index_dir / f"{tmp_path.name}.json"
    index.write_text(json.dumps({"git_sha": "abc", "scanned_at": "2026-09-01T00:00:00Z"}), encoding="utf-8")
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
            ),
            encoding="utf-8",
        )
        assert (
            codemap_cache.main(["write", "--batch", str(batch), "--index", str(index), "--cache-dir", str(cache)]) == 0
        )
        capsys.readouterr()
        (tmp_path / "resolve-codemap-cache-dir-test-session").write_text(
            str(cache) + "\n", encoding="utf-8", newline="\n"
        )
        if isinstance(cached, str):
            artifact_path = cache / "pkg.mod.json"
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
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
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    central = json.dumps({"central": [{"name": "pkg.mod", "path": "pkg/mod.py", "rdep_count": 0}]})
    answer = json.dumps({"module": "pkg.mod", "imported_by": []})
    # Model native Windows pipe newlines while still executing the real jq/Python commands.
    native_output = tmp_path / "native_output.py"
    native_output.write_text(
        '"""Relay real command output using Windows newline semantics."""\n'
        "import subprocess, sys\n"
        "line_endings, kind, program, *args = sys.argv[1:]\n"
        "result = subprocess.run([program, *args], stdout=subprocess.PIPE)\n"
        "output = result.stdout.replace(b'\\r\\n', b'\\n')\n"
        "if line_endings == 'crlf' and (kind != 'jq' or not any(arg in ('-b', '--binary') for arg in args)):\n"
        "    output = output.replace(b'\\n', b'\\r\\n')\n"
        "sys.stdout.buffer.write(output)\n"
        "sys.exit(result.returncode)\n",
        encoding="utf-8",
        newline="\n",
    )
    relay = f"{shlex.quote(Path(sys.executable).as_posix())} {shlex.quote(native_output.as_posix())} {line_endings}"
    # Only external commands are substituted; the skill's real cache helper and loop execute.
    prelude = f"""
python() {{ {relay} python {shlex.quote(Path(sys.executable).as_posix())} "$@"; }}
jq() {{ {relay} jq {shlex.quote(Path(shutil.which("jq")).as_posix())} "$@"; }}
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
            + shlex.quote(
                "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('changed-index', encoding='utf-8')"
            )
            + " "
            + shlex.quote(index.as_posix())
            + "\n"
        )
    # A file avoids Windows command-line quoting of the entire multi-line shell program.
    script = tmp_path / "preloop.sh"
    script.write_text(prelude + block + between + structural, encoding="utf-8", newline="\n")
    result = subprocess.run(
        [shutil.which(shell), script.as_posix()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    if refresh:
        assert index.read_text(encoding="utf-8") == "changed-index"
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    reusable = cached is True or cached in ("legacy-complete", "forward-wins")
    expected_calls = ["query --timeout 15 central --top 100000"]
    if not reusable:
        expected_calls.append("query rdeps pkg.mod")
    if refresh:
        expected_calls.append("query central --top 100000")
    expected_calls.append("query deps pkg.mod")
    assert calls == expected_calls
    for item in range(1, 6):
        assert f"#{item} pkg.mod ← callers:" in result.stdout
        assert f"item #{item} (pkg.mod) callers:" in result.stdout
    assert '"imported_by": []' in result.stdout
