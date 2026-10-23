"""Prove the batched codemap supplement blocks stage the same text as the per-module loops they replaced.

oss:review appends two query families per changed module to the staged structural context. The old blocks spawned one
``codemap-py`` process per query; the new ones pipe a single ``codemap-py query batch`` through
``bin/codemap_batch.py``. The staged text is what every dimension agent reads, so the equivalence tests run both shapes
against one stand-in ``codemap-py`` and compare the appended bytes. The batch is a whole-process alarm, so the fallback
tests make it fail and require the single-query calls to stage what the old loops staged.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import codemap_batch  # type: ignore[import-not-found]
import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_CONTEXT_MODE = _PLUGIN / "skills/review/modes/codemap-context.md"
_skip_shell_unavailable = pytest.mark.skipif(shutil.which("bash") is None, reason="The review blocks require Bash.")

#: Stand-in for ``codemap-py``: one answer table serves both the single-query and the batch entry point.
#: ``SHIM_BATCH`` makes the batch call fail like the real alarm (``timeout``: stderr line, exit 2, no stdout) or answer
#: all but its last item (``drop-last``); ``SHIM_SLOW_MODULE`` makes single queries for that module fail the same way;
#: ``SHIM_LOG`` collects one JSON argv line per call.
_SHIM = """
import json
import os
import sys


def answer(cmd, args):
    module = args[-1]
    if module == "missing.mod":
        return {"error": "module not indexed", "module": module, "suggestions": []}
    return {"cmd": cmd, "module": module, "flags": args[:-1], "names": [module, "calf\\u00e9"]}


def alarm(seconds):
    print(f"scan-query: timed out after {seconds}s", file=sys.stderr)
    sys.exit(2)


sys.stdout.reconfigure(newline="\\n")
rest = sys.argv[1:]
if os.environ.get("SHIM_LOG"):
    with open(os.environ["SHIM_LOG"], "a", encoding="utf-8", newline="\\n") as handle:
        handle.write(json.dumps(rest) + "\\n")
assert rest[0] == "query", rest
rest = rest[1:]
if rest[0] == "--timeout":
    rest = rest[2:]
if rest[0] == "batch":
    if os.environ.get("SHIM_BATCH") == "timeout":
        alarm(20)
    items = json.load(sys.stdin)
    out = []
    for position, item in enumerate(items):
        result = answer(item["cmd"], item["args"])
        out.append({"ok": "error" not in result, "index": position, "cmd": item["cmd"], "result": result})
    if os.environ.get("SHIM_BATCH") == "drop-last":
        out = out[:-1]
    print(json.dumps({"batch": out, "count": len(out), "index": {"query_complete": True}}))
else:
    if rest[-1] == os.environ.get("SHIM_SLOW_MODULE"):
        alarm(5)
    print(json.dumps(answer(rest[0], rest[1:])))
"""

#: argv the shipped block gives the batch call for two modules: the whole-process alarm is 10 s per module.
_BATCH_CALL = ["query", "--timeout", "20", "batch", "-"]

#: What ``render --fallback qa`` stages for module ``pkg.a`` when the batch answered nothing, one shim answer per line.
_FALLBACK_QA_PKG_A = "".join(
    (
        "### QA queries: pkg.a\n",
        '{"cmd": "mock-rdeps", "module": "pkg.a", "flags": [], "names": ["pkg.a", "calf\\u00e9"]}\n',
        '{"cmd": "uncovered", "module": "pkg.a", "flags": ["--top", "20"], "names": ["pkg.a", "calf\\u00e9"]}\n',
        "\n",
    )
)

#: The pre-batch loops, kept verbatim as the oracle (head of the audit's O11 change).
_OLD_QA_LOOP = r"""export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r CODEMAP_CONTEXT_STAGE < "${TMPDIR:-/tmp}/oss-review-codemap-context-stage-${CLEAN_ARGS}-${CSID}" 2>/dev/null || CODEMAP_CONTEXT_STAGE=""
[ -n "$CODEMAP_CONTEXT_STAGE" ] && [ -f "$CODEMAP_CONTEXT_STAGE" ] && while IFS= read -r mod; do
    [ -n "$mod" ] || continue
    echo "### QA queries: $mod"
    codemap-py query --timeout 5 mock-rdeps "$mod" 2>/dev/null
    codemap-py query --timeout 5 uncovered --top 20 "$mod" 2>/dev/null
    echo
done < "${TMPDIR:-/tmp}/oss-review-changed-mods-${CLEAN_ARGS}-${CSID}" >> "$CODEMAP_CONTEXT_STAGE"
"""

_OLD_DOCS_LOOP = r"""export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r CODEMAP_CONTEXT_STAGE < "${TMPDIR:-/tmp}/oss-review-codemap-context-stage-${CLEAN_ARGS}-${CSID}" 2>/dev/null || CODEMAP_CONTEXT_STAGE=""
[ -n "$CODEMAP_CONTEXT_STAGE" ] && [ -f "$CODEMAP_CONTEXT_STAGE" ] && while IFS= read -r mod; do
    [ -n "$mod" ] || continue
    echo "### Docs queries: $mod"
    codemap-py query --timeout 5 xrefs --broken "$mod" 2>/dev/null
    codemap-py query --timeout 5 undocumented "$mod" 2>/dev/null
    echo
done < "${TMPDIR:-/tmp}/oss-review-changed-mods-${CLEAN_ARGS}-${CSID}" >> "$CODEMAP_CONTEXT_STAGE"
"""

# Module-level constants, not dict values: docformatter re-wraps a triple-quoted string that follows `:`.
_OLD_LOOPS = {"qa": _OLD_QA_LOOP, "docs": _OLD_DOCS_LOOP}


def _new_block(family: str) -> str:
    """Return the shipped bash block that stages one supplement family through the batch helper."""
    blocks = re.findall(r"```bash\n(.*?)```", _CONTEXT_MODE.read_text(encoding="utf-8"), re.DOTALL)
    return next(block for block in blocks if f'codemap_batch.py" build {family} ' in block)


def _stage(work: Path, modules: str) -> Path:
    """Create the sentinels the supplement blocks reload and return the pre-seeded stage file."""
    work.mkdir()
    stage = work / "stage.md"
    stage.write_text("## Structural Context\n", encoding="utf-8", newline="\n")
    (work / "review-codemap-enabled-t").write_text("true\n", encoding="utf-8", newline="\n")
    (work / "review-codemap-currency-t").write_text("current\n", encoding="utf-8", newline="\n")
    (work / "oss-review-pr-tag-t").write_text("pr7\n", encoding="utf-8", newline="\n")
    (work / "oss-review-codemap-context-stage-pr7-t").write_text(
        f"{stage.as_posix()}\n", encoding="utf-8", newline="\n"
    )
    (work / "oss-review-changed-mods-pr7-t").write_text(modules, encoding="utf-8", newline="\n")
    return stage


def _install_launcher(directory: Path, shim: Path) -> None:
    """Put an executable ``codemap-py`` on a directory so a Python child resolves it through ``PATH``.

    The Bash prelude's ``codemap-py`` function is invisible to the helper's own subprocess calls, so the fallback needs
    a real file. POSIX gets a ``/bin/sh`` wrapper, Windows a ``.cmd`` file; both run the stand-in with the interpreter
    that runs the tests.
    """
    directory.mkdir(exist_ok=True)
    posix = directory / "codemap-py"
    posix.write_text(
        f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "{shim.as_posix()}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    posix.chmod(0o755)
    windows = directory / "codemap-py.cmd"
    windows.write_text(f'@"{sys.executable}" "{shim}" %*\r\n', encoding="utf-8", newline="")


def _environment(work: Path, shim: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the child environment: sentinels in ``work``, the launcher first on ``PATH``, calls logged to ``work``."""
    _install_launcher(work / "bin", shim)
    environment = os.environ.copy()
    environment.update(
        {
            "TMPDIR": str(work),
            "CLAUDE_CODE_SESSION_ID": "t",
            "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
            "PATH": str(work / "bin") + os.pathsep + environment.get("PATH", ""),
            "SHIM_LOG": str(work / "calls.jsonl"),
        }
    )
    environment.update(extra or {})
    return environment


def _calls(work: Path) -> list[list[str]]:
    """Return the argv of every stand-in ``codemap-py`` call made under ``work``, in order."""
    log = work / "calls.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def _run(block: str, work: Path, shim: Path, extra: dict[str, str] | None = None) -> None:
    """Execute a supplement block under Bash with ``codemap-py`` and ``python`` bound to the test interpreter."""
    prelude = (
        f'codemap-py() {{ "{Path(sys.executable).as_posix()}" "{shim.as_posix()}" "$@"; }}\n'
        f'python() {{ "{Path(sys.executable).as_posix()}" "$@"; }}\n'
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", prelude + block],
        cwd=work,
        env=_environment(work, shim, extra),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


@_skip_shell_unavailable
@pytest.mark.integration
@pytest.mark.parametrize("family", ["qa", "docs"])
@pytest.mark.parametrize(
    "modules",
    [
        pytest.param("pkg.alpha\npkg.beta\n", id="two-modules"),
        pytest.param("missing.mod\npkg.alpha\n", id="error-payload-for-unindexed-module"),
        pytest.param(".github.scripts.x\nplugins.codemap-py.bin.foo\n", id="index-style-names"),
        pytest.param("", id="no-modules"),
    ],
)
def test_batched_block_stages_the_same_bytes_as_the_old_loop(tmp_path: Path, family: str, modules: str) -> None:
    """The batched supplement block appends byte-identical text to the stage file as the per-module loop did.

    One stand-in ``codemap-py`` answers both the single-query and the batch entry point from one table, including a non-
    ASCII value and the error payload a failing single query prints on stdout. Both shapes run under Bash against their
    own copy of the sentinels; only the appended text is compared. The stand-in keeps one key order for both entry
    points, so this proves headers, blank lines, positions, encoding and error payloads; the real tool's differing key
    order for two queries is recorded in ``codemap_batch.py`` and is equal after parsing.
    """
    shim = tmp_path / "shim.py"
    shim.write_text(_SHIM, encoding="utf-8", newline="\n")
    old_stage = _stage(tmp_path / "old", modules)
    new_stage = _stage(tmp_path / "new", modules)

    _run(_OLD_LOOPS[family], old_stage.parent, shim)
    _run(_new_block(family), new_stage.parent, shim)

    assert new_stage.read_bytes() == old_stage.read_bytes()
    assert new_stage.read_bytes().startswith(b"## Structural Context\n")


@_skip_shell_unavailable
@pytest.mark.integration
@pytest.mark.parametrize("family", ["qa", "docs"])
def test_complete_batch_issues_no_single_query(tmp_path: Path, family: str) -> None:
    """A batch that answers every query is the only ``codemap-py`` call the block makes.

    The fallback exists for the failure path. Healthy runs must keep the one-process cost that motivated batching, so
    the call log of the shipped block holds just the batch call.
    """
    shim = tmp_path / "shim.py"
    shim.write_text(_SHIM, encoding="utf-8", newline="\n")
    stage = _stage(tmp_path / "new", "pkg.alpha\npkg.beta\n")

    _run(_new_block(family), stage.parent, shim)

    assert _calls(stage.parent) == [_BATCH_CALL]


@_skip_shell_unavailable
@pytest.mark.integration
@pytest.mark.parametrize("family", ["qa", "docs"])
@pytest.mark.parametrize("enabled", ["false", None])
def test_disabled_or_unresolved_gate_does_not_run_supplement(tmp_path: Path, family: str, enabled: str | None) -> None:
    """Healthy fixture defaults cannot hide a production regression that queries despite disabled or absent state."""
    shim = tmp_path / "shim.py"
    shim.write_text(_SHIM, encoding="utf-8", newline="\n")
    stage = _stage(tmp_path / "new", "pkg.alpha\npkg.beta\n")
    before = stage.read_bytes()
    sentinel = stage.parent / "review-codemap-enabled-t"
    if enabled is None:
        sentinel.unlink()
    else:
        sentinel.write_text(enabled + "\n", encoding="utf-8", newline="\n")
    (stage.parent / "review-codemap-currency-t").write_text("off\n", encoding="utf-8", newline="\n")

    _run(_new_block(family), stage.parent, shim)

    assert _calls(stage.parent) == []
    assert stage.read_bytes() == before


@_skip_shell_unavailable
@pytest.mark.integration
@pytest.mark.parametrize("family", ["qa", "docs"])
def test_batch_alarm_falls_back_to_single_queries_staging_the_old_loop_bytes(tmp_path: Path, family: str) -> None:
    """When the whole-process alarm kills the batch, each query reruns alone and stages what the old loop staged.

    The real tool exits 2 from its SIGALRM handler before it prints, so the batch leaves stdout empty and the pipe hides
    the status. Before the fallback this blanked every module section; now the single-query calls must carry the exact
    old argv (``query --timeout 5 <cmd> <flags> <module>``), in the old order, after the one batch call.
    """
    shim = tmp_path / "shim.py"
    shim.write_text(_SHIM, encoding="utf-8", newline="\n")
    old_stage = _stage(tmp_path / "old", "pkg.alpha\npkg.beta\n")
    new_stage = _stage(tmp_path / "new", "pkg.alpha\npkg.beta\n")

    _run(_OLD_LOOPS[family], old_stage.parent, shim)
    _run(_new_block(family), new_stage.parent, shim, {"SHIM_BATCH": "timeout"})

    assert new_stage.read_bytes() == old_stage.read_bytes()
    assert _calls(new_stage.parent) == [_BATCH_CALL, *_calls(old_stage.parent)]


@_skip_shell_unavailable
@pytest.mark.integration
@pytest.mark.parametrize("family", ["qa", "docs"])
def test_one_failing_single_query_loses_only_its_own_section(tmp_path: Path, family: str) -> None:
    """With the batch dead and one module's queries timing out, every other module still stages its lines.

    The slow module keeps its header and blank line but no query lines, exactly as the old loop left it, while the
    modules on both sides answer. This is the per-query isolation the whole-process alarm had removed.
    """
    modules = "pkg.alpha\nslow.mod\npkg.beta\n"
    environment = {"SHIM_BATCH": "timeout", "SHIM_SLOW_MODULE": "slow.mod"}
    shim = tmp_path / "shim.py"
    shim.write_text(_SHIM, encoding="utf-8", newline="\n")
    old_stage = _stage(tmp_path / "old", modules)
    new_stage = _stage(tmp_path / "new", modules)

    _run(_OLD_LOOPS[family], old_stage.parent, shim, environment)
    _run(_new_block(family), new_stage.parent, shim, environment)

    staged = new_stage.read_text(encoding="utf-8")
    assert new_stage.read_bytes() == old_stage.read_bytes()
    assert f"### {codemap_batch.HEADINGS[family]}: slow.mod\n\n" in staged
    assert [staged.count(f'"module": "{module}"') for module in ("pkg.alpha", "slow.mod", "pkg.beta")] == [2, 0, 2]


@_skip_shell_unavailable
@pytest.mark.integration
@pytest.mark.parametrize("family", ["qa", "docs"])
def test_short_batch_reruns_only_the_unanswered_query(tmp_path: Path, family: str) -> None:
    """A batch missing its last item reruns exactly that query and keeps the answered ones from the batch.

    Positions the batch answered are never repeated as single calls, so a partial result costs one extra process rather
    than a full rerun.
    """
    shim = tmp_path / "shim.py"
    shim.write_text(_SHIM, encoding="utf-8", newline="\n")
    old_stage = _stage(tmp_path / "old", "pkg.alpha\npkg.beta\n")
    new_stage = _stage(tmp_path / "new", "pkg.alpha\npkg.beta\n")

    _run(_OLD_LOOPS[family], old_stage.parent, shim)
    _run(_new_block(family), new_stage.parent, shim, {"SHIM_BATCH": "drop-last"})

    assert new_stage.read_bytes() == old_stage.read_bytes()
    assert _calls(new_stage.parent) == [_BATCH_CALL, _calls(old_stage.parent)[-1]]


def test_cli_fallback_reruns_every_query_when_the_batch_printed_nothing(tmp_path: Path) -> None:
    """``render --fallback`` over an empty batch stages one single-query answer per request item.

    Runs the script directly with the stand-in on ``PATH``, so it covers the fallback on hosts without Bash: the batch
    stage is simulated by empty stdin, as the real alarm leaves it.
    """
    shim = tmp_path / "shim.py"
    shim.write_text(_SHIM, encoding="utf-8", newline="\n")
    modules = tmp_path / "mods"
    modules.write_text("pkg.a\n", encoding="utf-8", newline="\n")

    rendered = subprocess.run(
        [sys.executable, str(_PLUGIN / "bin/codemap_batch.py"), "render", "--fallback", "qa", str(modules)],
        input=b"",
        capture_output=True,
        env=_environment(tmp_path, shim),
        check=False,
        timeout=60,
    )

    assert rendered.returncode == 0, rendered.stderr
    assert rendered.stdout.decode("utf-8") == _FALLBACK_QA_PKG_A
    assert _calls(tmp_path) == [
        ["query", "--timeout", "5", "mock-rdeps", "pkg.a"],
        ["query", "--timeout", "5", "uncovered", "--top", "20", "pkg.a"],
    ]


def test_fallback_without_codemap_py_keeps_headers_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no ``codemap-py`` on ``PATH`` the fallback adds nothing and the headers-only text is unchanged.

    The old loops printed headers and blank lines when every call failed to start; the fallback must not raise or spawn
    anything in that case.
    """
    monkeypatch.setenv("PATH", str(tmp_path))
    assert codemap_batch.render("qa", ["m"], "", fallback=True) == "### QA queries: m\n\n"


def test_build_request_orders_items_module_major_with_flags_before_module() -> None:
    """Each module's queries stay adjacent, with flags ahead of the module exactly as the old argv had them."""
    request = codemap_batch.build_request("docs", ["a.b", "c.d"])
    assert request == [
        {"cmd": "xrefs", "args": ["--broken", "a.b"]},
        {"cmd": "undocumented", "args": ["a.b"]},
        {"cmd": "xrefs", "args": ["--broken", "c.d"]},
        {"cmd": "undocumented", "args": ["c.d"]},
    ]


def test_load_modules_skips_unsafe_names_and_keeps_order(tmp_path: Path) -> None:
    """A name that could parse as a flag or escape a path is dropped before it reaches a batch item."""
    source = tmp_path / "mods"
    source.write_text("pkg.a\n--top\n../etc\nspace name\n\npkg.b\n", encoding="utf-8", newline="\n")
    assert codemap_batch.load_modules(source) == ["pkg.a", "pkg.b"]


@pytest.mark.parametrize(
    "batch_text",
    [
        pytest.param("", id="empty"),
        pytest.param("Traceback (most recent call last)", id="not-json"),
        pytest.param('{"error": "no batch"}', id="object-without-batch"),
        pytest.param('{"batch": "oops"}', id="batch-not-a-list"),
    ],
)
def test_render_on_unusable_batch_output_keeps_headers_only(batch_text: str) -> None:
    """When every codemap call printed nothing the old loop left headers and blank lines; the render matches."""
    assert codemap_batch.render("qa", ["m1", "m2"], batch_text) == "### QA queries: m1\n\n### QA queries: m2\n\n"


def test_render_short_batch_fills_by_position_and_leaves_the_tail_out() -> None:
    """A batch shorter than the request still renders the modules it covers, in position order."""
    batch_text = '{"batch": [{"result": {"n": 1}}, {"result": {"n": 2}}, {"result": {"n": 3}}]}'
    assert codemap_batch.render("qa", ["m1", "m2"], batch_text) == (
        '### QA queries: m1\n{"n": 1}\n{"n": 2}\n\n### QA queries: m2\n{"n": 3}\n\n'
    )


def test_cli_round_trip_writes_lf_bytes(tmp_path: Path) -> None:
    """The script emits LF-only bytes on every OS, because the staged file is byte-compared downstream."""
    source = tmp_path / "mods"
    source.write_text("pkg.a\n", encoding="utf-8", newline="\n")
    script = _PLUGIN / "bin/codemap_batch.py"
    built = subprocess.run(
        [sys.executable, str(script), "build", "qa", str(source)], capture_output=True, check=True, timeout=30
    )
    rendered = subprocess.run(
        [sys.executable, str(script), "render", "qa", str(source)],
        input=b'{"batch": [{"result": {"ok": true}}]}',
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert (
        built.stdout
        == b'[{"cmd": "mock-rdeps", "args": ["pkg.a"]}, {"cmd": "uncovered", "args": ["--top", "20", "pkg.a"]}]'
    )
    assert rendered.stdout == b'### QA queries: pkg.a\n{"ok": true}\n\n'
