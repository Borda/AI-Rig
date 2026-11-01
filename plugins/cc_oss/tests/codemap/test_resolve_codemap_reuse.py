"""Execute the shipped caller-context block to prevent repeated and empty-cache queries.

The resolve dispatch markdown ships a bash pre-loop that asks codemap for each module's callers once and reuses a cached
answer when it is complete. These tests run that real block (and the structural-map block after it) in real ``bash`` and
``zsh`` with real ``jq`` and the real cache helper; only the ``codemap-py`` binary, ``git`` and the interpreter launcher
are shell functions, so the recorded ``codemap-py`` calls show exactly which queries the block decided to make.
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import codemap_cache
import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_SESSION = "test-session"
_INDEX_TEXT = json.dumps({"git_sha": "abc", "scanned_at": "2026-09-01T00:00:00Z"})
_BASE_CALLS = ["query --timeout 15 central --top 100000"]
_RDEPS_CALL = ["query rdeps pkg.mod"]
_REFRESH_CALL = ["query central --top 100000"]
_DEPS_CALL = ["query deps pkg.mod"]

#: Relays a real command's output with Windows newline semantics so the block is exercised under CRLF pipes.
_RELAY_SOURCE = (
    '"""Relay real command output using Windows newline semantics."""\n'
    "import subprocess, sys\n"
    "line_endings, kind, program, *args = sys.argv[1:]\n"
    "result = subprocess.run([program, *args], stdout=subprocess.PIPE)\n"
    "output = result.stdout.replace(b'\\r\\n', b'\\n')\n"
    "if line_endings == 'crlf' and (kind != 'jq' or not any(arg in ('-b', '--binary') for arg in args)):\n"
    "    output = output.replace(b'\\n', b'\\r\\n')\n"
    "sys.stdout.buffer.write(output)\n"
    "sys.exit(result.returncode)\n"
)


def _skip_unless_shell(name: str) -> pytest.MarkDecorator:
    """Skip a shell case when that shell or ``jq`` is not installed."""
    return pytest.mark.skipif(
        shutil.which(name) is None or shutil.which("jq") is None,
        reason="Shipped shell block requires this shell and jq.",
    )


def _write_lf(path: Path, text: str) -> None:
    """Write UTF-8 text with LF endings so the shell reads the same bytes on every host."""
    path.write_text(text, encoding="utf-8", newline="\n")


@functools.cache
def _shipped_blocks() -> tuple[str, str]:
    """Return the shipped pre-loop block and the structural-map block from the resolve dispatch modes."""
    source = "\n".join(
        (_PLUGIN / f"skills/resolve/modes/dispatch-{part}.md").read_text(encoding="utf-8")
        for part in ("prelude", "phase1", "phase2", "phase3")
    )
    blocks = re.findall(r"```bash\n(.*?)```", source, re.DOTALL)
    preloop = next(block for block in blocks if 'BLAST_RADIUS_CONTEXT=""' in block)
    structural = next(block for block in blocks if "DEPS_MAP=" in block)
    return preloop, structural


def _prepare_session(tmp_path: Path) -> Path:
    """Create the resolve session sentinels and a codemap index; return the index file."""
    impl = tmp_path / "impl"
    impl.mkdir()
    _write_lf(impl / "selected-items.txt", "1 2 3 4 5\n")
    _write_lf(
        impl / "action-items.jsonl",
        "".join(json.dumps({"id": item, "file": "pkg/mod.py"}) + "\n" for item in range(1, 6)),
    )
    _write_lf(tmp_path / f"resolve-impl-dir-{_SESSION}", f"{impl}\n")
    _write_lf(tmp_path / f"resolve-codemap-enabled-{_SESSION}", "true\n")
    _write_lf(tmp_path / f"resolve-codemap-currency-{_SESSION}", "current\n")
    index_dir = tmp_path / "index with spaces"
    index_dir.mkdir()
    index = index_dir / f"{tmp_path.name}.json"
    index.write_text(_INDEX_TEXT, encoding="utf-8")
    return index


def _no_cache(tmp_path: Path, index: Path) -> None:
    """Leave the cache directory unset, as on a first run."""


def _seed_cache(tmp_path: Path, index: Path, mutate: Callable[[dict], None]) -> None:
    """Write a complete rdeps cache artifact, apply ``mutate`` to its stored answer and re-seal its content hash."""
    batch = tmp_path / "batch.json"
    complete = {"module": "pkg.mod", "imported_by": [], "index": {"query_complete": True}}
    batch.write_text(json.dumps({"batch": [{"cmd": "rdeps", "ok": True, "result": complete}]}), encoding="utf-8")
    cache = tmp_path / "cache"
    with contextlib.redirect_stdout(io.StringIO()):
        written = codemap_cache.main(["write", "--batch", str(batch), "--index", str(index), "--cache-dir", str(cache)])
    assert written == 0
    _write_lf(tmp_path / f"resolve-codemap-cache-dir-{_SESSION}", f"{cache}\n")
    artifact_path = cache / "pkg.mod.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    mutate(artifact["prefix"]["answers"]["rdeps"])
    artifact["prefix"]["content_hash"] = codemap_cache._content_hash(artifact["prefix"]["answers"])
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")


def _keep(answer: dict) -> None:
    """Leave the cached answer complete."""


def _drop(key: str) -> Callable[[dict], None]:
    """Build a mutation that removes ``key`` from the cached answer."""
    return lambda answer: answer.pop(key)


def _set(**fields: object) -> Callable[[dict], None]:
    """Build a mutation that overwrites top-level fields of the cached answer."""
    return lambda answer: answer.update(fields)


#: (cache seeder, whether the block may reuse the cached answer instead of querying rdeps again).
_CACHE_CASES = [
    pytest.param(_no_cache, False, id="no-cache"),
    pytest.param(functools.partial(_seed_cache, mutate=_keep), True, id="complete"),
    pytest.param(functools.partial(_seed_cache, mutate=_drop("imported_by")), False, id="missing-callers"),
    pytest.param(functools.partial(_seed_cache, mutate=_set(error="failed")), False, id="error"),
    pytest.param(functools.partial(_seed_cache, mutate=_set(module="pkg.other")), False, id="wrong-module"),
    pytest.param(
        functools.partial(_seed_cache, mutate=_set(index={"query_complete": False, "stale": False})),
        False,
        id="incomplete",
    ),
    pytest.param(
        functools.partial(_seed_cache, mutate=_set(index={"query_complete": True, "stale": True})), False, id="stale"
    ),
    pytest.param(functools.partial(_seed_cache, mutate=_drop("index")), False, id="missing-index"),
    pytest.param(functools.partial(_seed_cache, mutate=_set(index={})), False, id="missing-completeness"),
    pytest.param(
        functools.partial(_seed_cache, mutate=_set(index={"query_complete": None, "exhaustive": True})),
        False,
        id="null-completeness",
    ),
    pytest.param(functools.partial(_seed_cache, mutate=_set(index={"exhaustive": True})), True, id="legacy-complete"),
    pytest.param(
        functools.partial(_seed_cache, mutate=_set(index={"query_complete": True, "exhaustive": False})),
        True,
        id="forward-wins",
    ),
    pytest.param(
        functools.partial(_seed_cache, mutate=_set(index={"query_complete": False, "exhaustive": True})),
        False,
        id="forward-incomplete",
    ),
]


def _write_script(tmp_path: Path, index: Path, line_endings: str, refresh: bool) -> Path:
    """Compose the shell program: stubs, the shipped pre-loop, an optional index refresh, the structural block."""
    preloop, structural = _shipped_blocks()
    central = json.dumps({"central": [{"name": "pkg.mod", "path": "pkg/mod.py", "rdep_count": 0}]})
    answer = json.dumps({"module": "pkg.mod", "imported_by": []})
    native_output = tmp_path / "native_output.py"
    _write_lf(native_output, _RELAY_SOURCE)
    # The relay is harness-only and imports nothing beyond the standard library, so it runs on the base interpreter in
    # isolated, site-free mode: no venv launcher process on Windows and no site start-up per relayed command. The
    # relayed `python` itself stays `sys.executable`, so the shipped commands still run in the project environment.
    relay_python = Path(getattr(sys, "_base_executable", None) or sys.executable).as_posix()
    relay = f"{shlex.quote(relay_python)} -I -S {shlex.quote(native_output.as_posix())} {line_endings}"
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
    rewrite_index = (
        "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('changed-index', encoding='utf-8')"
    )
    refresh_step = f"python -c {shlex.quote(rewrite_index)} {shlex.quote(index.as_posix())}\n"
    between = '\nprintf "%s" "$BLAST_RADIUS_CONTEXT"\n' + refresh * refresh_step
    # A file avoids Windows command-line quoting of the entire multi-line shell program.
    script = tmp_path / "preloop.sh"
    _write_lf(script, prelude + preloop + between + structural)
    return script


@pytest.mark.integration
@pytest.mark.parametrize("line_endings", ["lf", "crlf"])
@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize(("seed_cache", "reusable"), _CACHE_CASES)
@pytest.mark.parametrize(
    "shell", [pytest.param(name, id=name, marks=_skip_unless_shell(name)) for name in ("bash", "zsh")]
)
def test_one_query_per_module_in_shipped_preloop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shell: str,
    seed_cache: Callable[[Path, Path], None],
    reusable: bool,
    refresh: bool,
    line_endings: str,
) -> None:
    """The shipped pre-loop asks codemap once per module and reuses only a complete, fresh, matching cache answer.

    Scenario: five selected items in one module run under a Windows-style default text codec and LF or CRLF pipes. A
    cache entry that is complete (or marked complete by the legacy or forward flag) saves the rdeps query; one that is
    missing, wrong, stale or incomplete must not. An index refreshed mid-run adds one central query and nothing else.
    """
    # Exercise the Windows default codec on every host, without changing the real shell or cache helper.
    monkeypatch.setattr(io, "text_encoding", lambda encoding, stacklevel=2: encoding or "cp1252")
    index = _prepare_session(tmp_path)
    seed_cache(tmp_path, index)
    script = _write_script(tmp_path, index, line_endings, refresh)
    env = {
        **os.environ,
        "TMPDIR": str(tmp_path),
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "CODEMAP_INDEX_DIR": str(index.parent),
    }
    result = subprocess.run(
        [shutil.which(shell), script.as_posix()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    expected_calls = [*_BASE_CALLS, *_RDEPS_CALL * (not reusable), *_REFRESH_CALL * refresh, *_DEPS_CALL]
    callers_lines = [f"#{item} pkg.mod ← callers:" in result.stdout for item in range(1, 6)]
    per_item_lines = [f"item #{item} (pkg.mod) callers:" in result.stdout for item in range(1, 6)]
    assert (result.returncode, result.stderr) == (0, "")
    assert index.read_text(encoding="utf-8") == ("changed-index" if refresh else _INDEX_TEXT)
    assert calls == expected_calls
    assert all(callers_lines)
    assert all(per_item_lines)
    assert '"imported_by": []' in result.stdout
