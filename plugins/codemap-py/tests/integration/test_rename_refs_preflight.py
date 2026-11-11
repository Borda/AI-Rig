"""Execute rename skill recipes against source files and saved structural answers."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from _host_skips import needs_git_and_bash

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "claude-skills/rename-refs/SKILL.md"
_SYMBOL = _SKILL.parent / "modes/symbol.md"


def _block(path: Path, marker: str) -> str:
    """Extract a fenced recipe by a distinctive line without depending on prose wrapping."""
    current: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "```bash":
            current = []
        elif line.strip() == "```" and current is not None:
            result = "\n".join(current)
            if marker in result:
                return result
            current = None
        elif current is not None:
            current.append(line)
    raise AssertionError(f"Recipe not found: {marker}")


@pytest.fixture
def recipe_env(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """Provide isolated sources and sentinels without writing any Git state."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "subdir").mkdir()
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    env = {
        **os.environ,
        "TMPDIR": str(sentinels),
        "CLAUDE_CODE_SESSION_ID": "recipe",
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "RENAME_TEST_ROOT": str(root),
        "CODEMAP_INDEX_DIR": str(tmp_path / "indexes"),
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), str(_PLUGIN / "bin"), os.environ.get("PATH", ""))),
    }
    return root, sentinels, env


def _save(sentinels: Path, values: dict[str, object], project: str = "proj") -> None:
    """Write the exact sentinel values consumed by a recipe."""
    for key, value in values.items():
        text = value if isinstance(value, str) else json.dumps(value)
        (sentinels / f"codemap-{project}-{key}-recipe").write_text(text + "\n", encoding="utf-8")


def _run(recipe: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Supply only Git's root lookup while keeping query and reference helpers real."""
    recipe = recipe.replace("git rev-parse --show-toplevel", "printf '%s\\n' \"$RENAME_TEST_ROOT\"")
    return subprocess.run(["bash", "-c", recipe], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


@needs_git_and_bash
@pytest.mark.integration
@pytest.mark.parametrize("symbol_type", ["function", "class", "method"])
def test_wrapper_recipe_prints_generated_alias(recipe_env, symbol_type: str) -> None:
    """The recipe exposes the real generator's decorator, version values and import line."""
    root, sentinels, env = recipe_env
    _save(
        sentinels,
        {
            "rename-DEPRECATE": "true",
            "sym-type": symbol_type,
            "rename-OLD_NAME": "Old",
            "rename-NEW_NAME": "New",
            "rename-SINCE_VER": "1.2",
            "rename-REMOVED_IN_VER": "2.0",
        },
    )

    result = _run(_block(_SYMBOL, "DEPRECATION_CODE=$("), env, root)

    assert result.returncode == 0, result.stderr
    assert "from deprecate import deprecated" in result.stdout
    assert "target=New" in result.stdout
    assert 'deprecated_in="1.2"' in result.stdout
    assert 'remove_in="2.0"' in result.stdout
    assert "def Old(" in result.stdout or "class Old: ..." in result.stdout


@needs_git_and_bash
@pytest.mark.integration
@pytest.mark.parametrize("bounded", [False, True])
def test_overload_recipe_finds_definition_lines(recipe_env, bounded: bool) -> None:
    """Overload discovery includes stub definitions and applies class bounds to definition line numbers."""
    root, sentinels, env = recipe_env
    (root / "provider.py").write_text(
        "class Other:\n    @overload\n    def old(self): ...\nclass Selected:\n"
        "    @overload\n    def old(self, x: int): ...\n    @typing.overload\n"
        "    def old(self, x: str): ...\n    def old(self, x): return x\n",
        encoding="utf-8",
    )
    (root / "provider.pyi").write_text("@overload\ndef old(x: int): ...\n", encoding="utf-8")
    _save(sentinels, {"rename-OLD_NAME": "old", "sym-path": "provider.py", "cls-range": "4 9" if bounded else ""})

    result = _run(_block(_SYMBOL, "${SYM_PATH%.py}.pyi"), env, root)

    assert "6-    def old(self, x: int): ..." in result.stdout
    assert "8-    def old(self, x: str): ..." in result.stdout
    assert "2-def old(x: int): ..." in result.stdout
    assert ("3-    def old(self): ..." in result.stdout) is not bounded


@needs_git_and_bash
@pytest.mark.integration
@pytest.mark.parametrize("kind", ["discovery", "delete-guard", "residual"])
def test_reference_recipes_search_from_repository_root(recipe_env, kind: str) -> None:
    """A run from a subdirectory still sees root-relative providers and non-call imports elsewhere."""
    root, sentinels, env = recipe_env
    (root / "provider.py").write_text("def old(): pass\n", encoding="utf-8")
    (root / "consumer.py").write_text("from provider import old\ncallback = old\n", encoding="utf-8")
    _save(
        sentinels,
        {
            "rename-SUBCOMMAND": "symbol",
            "rename-OLD_NAME": "old",
            "sym-module": "provider",
            "sym-path": "provider.py",
            "sym-type": "function",
            "rdeps-json": {"called_by": []},
            "find-symbol-json": {"matches": [{"path": "provider.py"}]},
            "ref-batch-json": {
                "batch": [
                    {
                        "ok": True,
                        "cmd": "list",
                        "result": {
                            "modules": [
                                {"name": "provider", "path": "provider.py"},
                                {"name": "consumer", "path": "consumer.py"},
                            ]
                        },
                    },
                    {"ok": True, "cmd": "rdeps", "result": {"module": "provider", "imported_by": ["consumer"]}},
                ]
            },
        },
    )
    markers = {"discovery": "REF_SITES=", "delete-guard": "grep -rnw", "residual": "printf 'RESIDUAL_COUNT="}
    recipe = _block(_SKILL, markers[kind])
    # Discovery's saved query answers are the input boundary; the reference helper stays real.
    recipe = "\n".join(line for line in recipe.splitlines() if not line.startswith("codemap-py query"))

    result = _run(recipe, env, root / "subdir")

    assert result.returncode == 0, result.stderr
    assert "consumer.py:1:from provider import old" in result.stdout
    assert "consumer.py:2:callback = old" in result.stdout


@needs_git_and_bash
@pytest.mark.integration
def test_preflight_saves_all_caller_ranges_before_apply(recipe_env) -> None:
    """A real symbols query resolves two callers and saves their source ranges before mutation."""
    root, sentinels, env = recipe_env
    (root / "consumer.py").write_text("def first():\n    old()\n\ndef second():\n    old()\n", encoding="utf-8")
    _save(
        sentinels,
        {
            "rename-SUBCOMMAND": "symbol",
            "rdeps-json": {
                "called_by": [
                    {"caller": "consumer::first", "module": "consumer", "path": "consumer.py"},
                    {"caller": "consumer::second", "module": "consumer", "path": "consumer.py"},
                ]
            },
        },
    )

    result = _run(_block(_SKILL, "caller-ranges-"), env, root / "subdir")

    assert result.returncode == 0, result.stderr
    rows = json.loads((sentinels / "codemap-proj-caller-ranges-recipe").read_text(encoding="utf-8"))
    assert [(row["caller"], row["path"], row["start_line"], row["end_line"]) for row in rows] == [
        ("consumer::first", "consumer.py", 1, 2),
        ("consumer::second", "consumer.py", 4, 5),
    ]
    assert rows[1]["source"] == "def second():\n    old()"


@needs_git_and_bash
@pytest.mark.integration
@pytest.mark.parametrize(
    "source",
    [
        "def other(): pass\n",
        "class C:\n    @property\n    def old(self): ...\n    @old.setter\n    def old(self, value): ...\n",
    ],
)
def test_preflight_rejects_missing_or_ambiguous_callers(recipe_env, source: str) -> None:
    """An unresolved caller cannot leave an old preflight sentinel eligible for apply."""
    root, sentinels, env = recipe_env
    (root / "consumer.py").write_text(source, encoding="utf-8")
    _save(
        sentinels,
        {
            "rename-SUBCOMMAND": "symbol",
            "caller-ranges": "stale",
            "rdeps-json": {"called_by": [{"caller": "consumer::C.old", "module": "consumer", "path": "consumer.py"}]},
        },
    )

    result = _run(_block(_SKILL, "caller-ranges-"), env, root)

    assert result.returncode != 0
    assert not (sentinels / "codemap-proj-caller-ranges-recipe").exists()
    assert "consumer::C.old" in result.stderr


@needs_git_and_bash
@pytest.mark.integration
def test_module_residual_recipe_includes_string_imports_and_config(recipe_env) -> None:
    """The module residual search reports dotted string targets beyond static import edges."""
    root, sentinels, env = recipe_env
    (root / "consumer.py").write_text(
        'importlib.import_module("pkg.old")\nmock.patch("pkg.old.fn")\n', encoding="utf-8"
    )
    (root / "pyproject.toml").write_text('target = "pkg.old:main"\n', encoding="utf-8")
    (root / "guide.rst").write_text(":mod:`pkg.old`\n", encoding="utf-8")
    _save(sentinels, {"rename-OLD_REF": "pkg.old", "rename-SUBCOMMAND": "module"})

    result = _run(_block(_SKILL, "MODULE_RESIDUAL_COUNT="), env, root / "subdir")

    assert result.returncode == 0, result.stderr
    assert "MODULE_RESIDUAL_COUNT=4" in result.stdout
    assert 'consumer.py:1:importlib.import_module("pkg.old")' in result.stdout
    assert 'consumer.py:2:mock.patch("pkg.old.fn")' in result.stdout
    assert 'pyproject.toml:1:target = "pkg.old:main"' in result.stdout
    assert "guide.rst:1::mod:`pkg.old`" in result.stdout


@needs_git_and_bash
@pytest.mark.integration
def test_module_residual_recipe_accepts_no_hits(recipe_env) -> None:
    """Grep's no-match exit is a completed search rather than unavailable verification."""
    root, sentinels, env = recipe_env
    (root / "consumer.py").write_text('importlib.import_module("pkg.new")\n', encoding="utf-8")
    _save(sentinels, {"rename-OLD_REF": "pkg.old", "rename-SUBCOMMAND": "module"})

    result = _run(_block(_SKILL, "MODULE_RESIDUAL_COUNT="), env, root)

    assert result.returncode == 0, result.stderr
    assert "MODULE_RESIDUAL_COUNT=0" in result.stdout


@needs_git_and_bash
@pytest.mark.integration
def test_project_validation_leaves_missing_index_to_query_preparation(recipe_env) -> None:
    """Project validation accepts Python sources before the canonical index exists."""
    root, _, env = recipe_env
    (root / "provider.py").write_text("def old(): pass\n", encoding="utf-8")

    result = _run(_block(_SKILL, "resolve_index_env.py"), env, root)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "scan-codebase first" not in result.stdout
    assert not list(Path(env["CODEMAP_INDEX_DIR"]).glob("*.json"))


@needs_git_and_bash
@pytest.mark.integration
def test_fifth_candidate_is_reachable_through_saved_index(recipe_env) -> None:
    """Candidate selection exposes free-text indices and narrows the saved fifth match without re-querying."""
    root, sentinels, env = recipe_env
    candidates = [{"qualified_name": "old", "module": f"module_{i}", "path": f"module_{i}.py"} for i in range(6)]
    _save(sentinels, {"find-symbol-json": {"matches": candidates}})
    prompt = next(line for line in _SKILL.read_text(encoding="utf-8").splitlines() if line.startswith("- Multiple →"))
    recipe = _block(_SKILL, "CHOSEN_IDX=").replace("<0-based index of the match selected via AskUserQuestion>", "4")

    result = _run(recipe, env, root)

    assert "free-text answer" in prompt
    assert "any in-range index" in prompt
    assert "Abort — keep code unchanged" in prompt
    assert "Select another index" not in prompt
    assert result.returncode == 0, result.stderr
    answer = json.loads((sentinels / "codemap-proj-find-symbol-json-recipe").read_text(encoding="utf-8"))
    assert answer["matches"] == [candidates[4]]


@needs_git_and_bash
@pytest.mark.integration
def test_symbol_residual_preserves_non_git_project_support(recipe_env) -> None:
    """A symbol rename can still verify Python sources when no Git root exists."""
    root, sentinels, env = recipe_env
    (root / "consumer.py").write_text("callback = old\n", encoding="utf-8")
    project = subprocess.run(
        ["bash", "-c", "git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo cm"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    _save(sentinels, {"rename-SUBCOMMAND": "symbol", "rename-OLD_NAME": "old", "sym-type": "function"}, project=project)

    result = subprocess.run(
        ["bash", "-c", _block(_SKILL, "printf 'RESIDUAL_COUNT=")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "consumer.py:1:callback = old" in result.stdout


def _preflight_python() -> str:
    """Extract the shipped preflight Python for controlled launcher and query-error boundaries."""
    recipe = _block(_SKILL, "PREFLIGHT_CALLERS=").splitlines()
    start = next(i for i, line in enumerate(recipe) if "<<'PY'" in line)
    return "\n".join(recipe[start + 1 : recipe.index("PY", start)])


def _controlled_preflight(
    recipe_env, monkeypatch, query, launcher="C:/tools/codemap-py.cmd", source="def use():\n    old()\n"
) -> Path:
    """Execute the real preflight body against a saved caller and controlled external query boundary."""
    root, sentinels, _ = recipe_env
    (root / "consumer.py").write_text(source, encoding="utf-8")
    _save(
        sentinels,
        {
            "rename-OLD_NAME": "old",
            "rename-OLD_REF": "old",
            "rdeps-json": {"called_by": [{"caller": "consumer::use", "module": "consumer", "path": "consumer.py"}]},
        },
    )
    monkeypatch.setattr(sys, "argv", ["-", str(sentinels / "codemap-proj"), "recipe", str(root)])
    with (
        mock.patch("shutil.which", return_value=launcher, autospec=True),
        mock.patch("subprocess.run", side_effect=query, autospec=True),
    ):
        recipe = sentinels / "preflight.py"
        recipe.write_text(_preflight_python(), encoding="utf-8")
        runpy.run_path(str(recipe))
    return sentinels / "codemap-proj-caller-ranges-recipe"


def _symbols_answer(*, complete=True, stale=False) -> subprocess.CompletedProcess[str]:
    """Return the external symbols query's documented JSON shape."""
    return subprocess.CompletedProcess(
        [],
        0,
        json.dumps(
            {
                "path": "consumer.py",
                "symbols": [{"qualified_name": "use", "start_line": 1, "end_line": 2}],
                "index": {"query_complete": complete, "stale": stale},
            }
        ),
        "",
    )


def test_preflight_passes_resolved_launcher_to_subprocess(recipe_env, monkeypatch) -> None:
    """A PATHEXT-resolved command path reaches subprocess unchanged."""
    query = mock.Mock(return_value=_symbols_answer())

    output = _controlled_preflight(recipe_env, monkeypatch, query)

    assert output.exists()
    assert query.call_args.args[0][0] == "C:/tools/codemap-py.cmd"


def test_preflight_reports_missing_launcher(recipe_env, monkeypatch) -> None:
    """An absent executable produces a routed stop without attempting a query."""
    query = mock.Mock(return_value=_symbols_answer())

    with pytest.raises(SystemExit, match=r"! caller preflight unavailable:.*launcher"):
        _controlled_preflight(recipe_env, monkeypatch, query, launcher=None)

    query.assert_not_called()


@pytest.mark.parametrize(
    ("complete", "stale"),
    [
        pytest.param(False, True, id="stale"),
        pytest.param(False, False, id="degraded"),
    ],
)
def test_preflight_honors_recorded_coverage_override(recipe_env, monkeypatch, complete, stale) -> None:
    """The recorded proceed decision accepts incomplete metadata for a source-verified unique caller."""
    _, sentinels, _ = recipe_env
    _save(sentinels, {"rename-PROCEED_STALE": "true"})

    output = _controlled_preflight(
        recipe_env, monkeypatch, mock.Mock(return_value=_symbols_answer(complete=complete, stale=stale))
    )

    assert json.loads(output.read_text(encoding="utf-8"))[0]["source"] == "def use():\n    old()"


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(subprocess.CalledProcessError(3, "query", stderr='{"error":"not indexed"}'), id="query-error"),
        pytest.param(subprocess.TimeoutExpired("query", 25, stderr=b"timed out"), id="timeout"),
        pytest.param(subprocess.CompletedProcess([], 0, "not json", ""), id="malformed-json"),
        pytest.param(subprocess.CompletedProcess([], 0, '{"error":"not indexed"}', ""), id="error-payload"),
        pytest.param(subprocess.CompletedProcess([], 0, '{"index":{}}', ""), id="missing-keys"),
    ],
)
def test_preflight_query_failures_produce_routed_stop(recipe_env, monkeypatch, failure) -> None:
    """Query errors expose a bounded diagnostic and never leave a traceback or reusable caller ranges."""
    query = mock.Mock(side_effect=failure) if isinstance(failure, Exception) else mock.Mock(return_value=failure)

    with pytest.raises(SystemExit, match="! caller preflight unavailable: consumer::use"):
        _controlled_preflight(recipe_env, monkeypatch, query)

    _, sentinels, _ = recipe_env
    assert not (sentinels / "codemap-proj-caller-ranges-recipe").exists()


@needs_git_and_bash
@pytest.mark.integration
def test_module_residual_excludes_new_names_caches_and_history(recipe_env) -> None:
    """Longer module names and historical text do not block a completed migration."""
    root, sentinels, env = recipe_env
    (root / "consumer.py").write_text("import pkg.old_v2\nimport pkg.old_extra\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("Moved pkg.old\n", encoding="utf-8")
    (root / ".reports").mkdir()
    (root / ".reports" / "note.md").write_text("pkg.old\n", encoding="utf-8")
    (root / ".tox").mkdir()
    (root / ".tox" / "cached.py").write_text("import pkg.old\n", encoding="utf-8")
    _save(sentinels, {"rename-SUBCOMMAND": "module", "rename-OLD_REF": "pkg.old", "rename-NEW_REF": "pkg.old_v2"})

    result = _run(_block(_SKILL, "MODULE_RESIDUAL_COUNT="), env, root)

    assert result.returncode == 0, result.stderr
    assert "MODULE_RESIDUAL_COUNT=0" in result.stdout
    assert "MODULE_HISTORY_ADVISORY=" in result.stdout
    assert "CHANGELOG.md:1:Moved pkg.old" in result.stdout
    assert "cached.py" not in result.stdout
    assert "note.md" not in result.stdout


@needs_git_and_bash
@pytest.mark.integration
@pytest.mark.parametrize("filename", ["history.py", "changes.py", "changelog_helpers.py"])
def test_module_residual_does_not_classify_python_consumers_as_history(recipe_env, filename) -> None:
    """History-like Python module names remain current consumers whose old imports block completion."""
    root, sentinels, env = recipe_env
    (root / filename).write_text("import pkg.old\n", encoding="utf-8")
    _save(sentinels, {"rename-SUBCOMMAND": "module", "rename-OLD_REF": "pkg.old", "rename-NEW_REF": "pkg.new"})

    result = _run(_block(_SKILL, "MODULE_RESIDUAL_COUNT="), env, root)

    assert result.returncode == 0, result.stderr
    assert "MODULE_RESIDUAL_COUNT=1" in result.stdout
    assert "MODULE_HISTORY_ADVISORY=0" in result.stdout


@needs_git_and_bash
@pytest.mark.integration
@pytest.mark.parametrize(
    "marker", ["REF_SITES=", "printf 'RESIDUAL_COUNT=", "--kind all-exports", "--kind sphinx-symbol"]
)
def test_reference_recipes_preserve_invoking_directory(recipe_env, marker: str) -> None:
    """Reference scans find root files without changing the invoking shell's working directory."""
    root, sentinels, env = recipe_env
    (root / "consumer.py").write_text("old()\n", encoding="utf-8")
    _save(
        sentinels,
        {
            "rename-SUBCOMMAND": "symbol",
            "rename-OLD_NAME": "old",
            "sym-module": "consumer",
            "sym-path": "consumer.py",
            "sym-type": "function",
            "rdeps-json": {"called_by": []},
            "find-symbol-json": {"matches": [{"path": "consumer.py"}]},
            "ref-batch-json": {},
        },
    )
    path = _SYMBOL if marker.startswith("--kind") else _SKILL
    recipe = "\n".join(line for line in _block(path, marker).splitlines() if not line.startswith("codemap-py query"))

    result = _run(recipe + '\nprintf "FINAL_CWD=%s\\n" "$PWD"', env, root / "subdir")

    assert result.returncode == 0, result.stderr
    assert f"FINAL_CWD={root / 'subdir'}" in result.stdout


def _comparison_python() -> str:
    """Extract the new-target comparison recipe's Python body."""
    recipe = _block(_SKILL, "MATCHED_IDENTITIES=").splitlines()
    start = next(i for i, line in enumerate(recipe) if "<<'PY'" in line)
    return "\n".join(recipe[start + 1 : recipe.index("PY", start)])


def _compare_symbol(recipe_env, monkeypatch, *, before, after, exhaustive="false") -> None:
    """Run the shipped identity comparison with controlled external query JSON."""
    root, sentinels, _ = recipe_env
    _save(
        sentinels,
        {
            "rename-SUBCOMMAND": "symbol",
            "rename-OLD_REF": "old",
            "rename-NEW_REF": "new",
            "rename-REMOVE_IF_ZERO": "false",
            "sym-module": "provider",
            "sym-qname": "old",
            "rename-EXHAUSTIVE": exhaustive,
            "caller-ranges": before,
            "rdeps-json": {"called_by": before, "count": len(before)},
        },
    )
    monkeypatch.setattr(sys, "argv", ["-", str(sentinels / "codemap-proj"), "recipe", str(root)])
    with (
        mock.patch("shutil.which", return_value="codemap-py.cmd", autospec=True),
        mock.patch(
            "subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(after), ""), autospec=True
        ),
    ):
        recipe = sentinels / "compare.py"
        recipe.write_text(_comparison_python(), encoding="utf-8")
        runpy.run_path(str(recipe))


def test_identity_comparison_rejects_same_count_wrong_caller(recipe_env, monkeypatch, capsys) -> None:
    """Equal reverse-call counts cannot conceal a missing caller replaced by another identity."""
    before = [{"caller": "consumer::use", "path": "consumer.py"}]
    after = {"called_by": [{"caller": "consumer::other", "path": "consumer.py"}], "count": 1, "index": {}}

    with pytest.raises(SystemExit, match=r"!.*identity"):
        _compare_symbol(recipe_env, monkeypatch, before=before, after=after)

    output = capsys.readouterr().out
    assert 'MISSING_IDENTITIES=[["consumer::use", "consumer.py"]]' in output
    assert 'EXTRA_IDENTITIES=[["consumer::other", "consumer.py"]]' in output


def test_identity_comparison_accepts_matching_nonexhaustive_coverage(recipe_env, monkeypatch, capsys) -> None:
    """An initially non-exhaustive rename still verifies matching known consumers."""
    before = [{"caller": "consumer::use", "path": "consumer.py"}]
    after = {"called_by": before, "count": 1, "index": {"query_complete": False, "stale": False}}

    _compare_symbol(recipe_env, monkeypatch, before=before, after=after)

    assert "⚠ Index non-exhaustive" in capsys.readouterr().out


def test_identity_comparison_rejects_coverage_regression(recipe_env, monkeypatch) -> None:
    """An initially exhaustive rename cannot pass after losing coverage."""
    before = [{"caller": "consumer::use", "path": "consumer.py"}]
    after = {"called_by": before, "count": 1, "index": {"query_complete": False, "stale": False}}

    with pytest.raises(SystemExit, match=r"!.*coverage"):
        _compare_symbol(recipe_env, monkeypatch, before=before, after=after, exhaustive="true")


def test_identity_comparison_transforms_only_provider_prefix(recipe_env, monkeypatch, capsys) -> None:
    """Recursive provider callers and nested members transform while neighboring names retain identity."""
    before = [
        {"caller": "provider::old.inner", "path": "provider.py"},
        {"caller": "provider::older", "path": "provider.py"},
        {"caller": "other::old", "path": "other.py"},
    ]
    after = {
        "called_by": [{**before[0], "caller": "provider::new.inner"}, *before[1:]],
        "count": 3,
        "index": {"query_complete": True, "stale": False},
    }

    _compare_symbol(recipe_env, monkeypatch, before=before, after=after, exhaustive="true")

    assert "MISSING_IDENTITIES=[]" in capsys.readouterr().out
    _, sentinels, _ = recipe_env
    assert json.loads((sentinels / "codemap-proj-new-rdeps-json-recipe").read_text()) == after


@pytest.mark.parametrize(
    "source",
    [
        "def use():\n    other()\n",
        'def use():\n    note = "old()"\n',
    ],
)
def test_preflight_override_requires_expected_call(recipe_env, monkeypatch, source) -> None:
    """Stale permission never licenses a caller range whose saved source lacks the old call."""
    _, sentinels, _ = recipe_env
    _save(sentinels, {"rename-PROCEED_STALE": "true"})

    with pytest.raises(SystemExit, match="! caller preflight source mismatch: consumer::use"):
        _controlled_preflight(
            recipe_env, monkeypatch, mock.Mock(return_value=_symbols_answer(complete=False, stale=True)), source=source
        )

    assert not (sentinels / "codemap-proj-caller-ranges-recipe").exists()


def test_preflight_without_decision_rejects_missing_coverage(recipe_env, monkeypatch) -> None:
    """Unknown caller coverage retains its existing stop unless the user's decision was recorded."""
    with pytest.raises(SystemExit, match="! caller preflight unavailable: consumer::use"):
        _controlled_preflight(
            recipe_env, monkeypatch, mock.Mock(return_value=_symbols_answer(complete=None, stale=None))
        )


def test_preflight_override_rejects_replaced_caller_definition(recipe_env, monkeypatch) -> None:
    """A different function in the stale range cannot impersonate an indexed caller by calling the old name."""
    _, sentinels, _ = recipe_env
    _save(sentinels, {"rename-PROCEED_STALE": "true"})

    with pytest.raises(SystemExit, match="! caller preflight source mismatch: consumer::use"):
        _controlled_preflight(
            recipe_env,
            monkeypatch,
            mock.Mock(return_value=_symbols_answer(complete=False, stale=True)),
            source="def unrelated():\n    old()\n",
        )

    assert not (sentinels / "codemap-proj-caller-ranges-recipe").exists()


def test_preflight_override_accepts_call_in_partially_parseable_module(recipe_env, monkeypatch) -> None:
    """Unrelated syntax damage does not veto a uniquely indexed caller with the expected saved call."""
    _, sentinels, _ = recipe_env
    _save(sentinels, {"rename-PROCEED_STALE": "true"})

    output = _controlled_preflight(
        recipe_env,
        monkeypatch,
        mock.Mock(return_value=_symbols_answer(complete=False, stale=False)),
        source="def use():\n    old()\n\nsyntax ???\n",
    )

    assert output.exists()


@needs_git_and_bash
@pytest.mark.integration
@pytest.mark.parametrize("mode", ["symbol", "module"])
def test_first_query_prints_freshness_and_completeness(recipe_env, mode) -> None:
    """Both target modes expose the actual first answer's freshness without a model-derived value."""
    root, sentinels, env = recipe_env
    (root / "provider.py").write_text("def old(): return 1\n", encoding="utf-8")
    _save(sentinels, {"rename-SUBCOMMAND": mode, "rename-OLD_REF": "old" if mode == "symbol" else "provider"})
    marker = "_FS_SENTINEL=" if mode == "symbol" else "rdeps-summary --kind module"

    result = _run(_block(_SKILL, marker), env, root)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "STALE=false" in result.stdout
    assert "QUERY_COMPLETE=true" in result.stdout


@needs_git_and_bash
@pytest.mark.integration
def test_recorded_stale_decision_is_consumable_by_preflight(recipe_env) -> None:
    """The fixed decision block persists the explicit true value in the sentinel the preflight reads."""
    root, sentinels, env = recipe_env
    recipe = _block(_SKILL, 'PROCEED_STALE="<').replace("<true for Proceed anyway; false otherwise>", "true")

    result = _run(recipe, env, root)

    assert result.returncode == 0, result.stderr
    assert (sentinels / "codemap-proj-rename-PROCEED_STALE-recipe").read_text() == "true\n"


@needs_git_and_bash
@pytest.mark.integration
def test_import_smoke_passes_without_covering_test(recipe_env) -> None:
    """Every provider/consumer imports once and a missing runtime test remains an advisory."""
    root, sentinels, env = recipe_env
    (root / "provider.py").write_text("def new(): return 1\n", encoding="utf-8")
    (root / "consumer.py").write_text("from provider import new\ndef use(): return new()\n", encoding="utf-8")
    _save(
        sentinels,
        {
            "rename-SUBCOMMAND": "symbol",
            "rename-NEW_REF": "new",
            "sym-module": "provider",
            "sym-qname": "old",
            "rdeps-json": {"called_by": [{"module": "consumer"}, {"module": "consumer"}]},
            "ref-batch-json": {"batch": []},
        },
    )

    result = _run(_block(_SKILL, "IMPORT_SMOKE="), env, root)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("IMPORT_SMOKE=consumer EXIT=0") == 1
    assert result.stdout.count("IMPORT_SMOKE=provider EXIT=0") == 1
    assert "ADVISORY pytest command: no covering test" in result.stdout


@needs_git_and_bash
@pytest.mark.integration
def test_import_smoke_reports_failing_consumer_and_continues(recipe_env) -> None:
    """One broken consumer fails the check after the other inventoried modules are still checked."""
    root, sentinels, env = recipe_env
    (root / "provider.py").write_text("def new(): return 1\n", encoding="utf-8")
    (root / "broken.py").write_text("from provider import old\n", encoding="utf-8")
    _save(
        sentinels,
        {
            "rename-SUBCOMMAND": "module",
            "rename-OLD_REF": "old_provider",
            "rename-NEW_REF": "provider",
            "rdeps-json": {"imported_by": ["broken"], "dynamic_imported_by": []},
        },
    )

    result = _run(_block(_SKILL, "IMPORT_SMOKE="), env, root)

    assert result.returncode == 1
    assert "IMPORT_SMOKE=broken EXIT=1" in result.stdout
    assert "IMPORT_SMOKE=provider EXIT=0" in result.stdout
    assert "! import smoke failed: broken" in result.stderr


@needs_git_and_bash
@pytest.mark.integration
def test_edited_file_summary_includes_partial_edits_and_move_paths(recipe_env) -> None:
    """The actual journal plus import-edited consumers survive a failure and include both move endpoints."""
    root, sentinels, env = recipe_env
    _save(
        sentinels,
        {
            "rename-SUBCOMMAND": "module",
            "processed-import-files": "consumer.py",
            "edited-files": "pkg/old.py\npkg/new.py",
            "sym-path": "unrelated_prior_symbol.py",
        },
    )
    record = _block(_SKILL, 'EDITED_PATH="<').replace("<exact path just edited or moved>", "config.toml")

    recorded = _run(record, env, root)
    result = _run(_block(_SKILL, 'print("Files edited:")'), env, root)

    assert recorded.returncode == result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "Files edited:",
        "  config.toml",
        "  consumer.py",
        "  pkg/new.py",
        "  pkg/old.py",
    ]


def _compare_module(recipe_env, monkeypatch, before, after) -> None:
    """Run the real module comparison against controlled external reverse-import answers."""
    root, sentinels, _ = recipe_env
    _save(
        sentinels,
        {
            "rename-SUBCOMMAND": "module",
            "rename-OLD_REF": "pkg.old",
            "rename-NEW_REF": "pkg.new",
            "rename-EXHAUSTIVE": "false",
            "rdeps-json": before,
        },
    )
    monkeypatch.setattr(sys, "argv", ["-", str(sentinels / "codemap-proj"), "recipe", str(root)])
    with (
        mock.patch("shutil.which", return_value="codemap-py.cmd", autospec=True),
        mock.patch(
            "subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(after), ""), autospec=True
        ) as query,
    ):
        recipe = sentinels / "compare.py"
        recipe.write_text(_comparison_python(), encoding="utf-8")
        runpy.run_path(str(recipe))
    assert query.call_args.args[0][-2:] == ["rdeps", "pkg.new"]


def test_module_comparison_retains_static_dynamic_and_config_locations(recipe_env, monkeypatch, capsys) -> None:
    """Target/context changes do not obscure the same importer and configuration source locations."""
    before = {
        "imported_by": ["consumer"],
        "dynamic_imported_by": [{"importer": "dynamic", "path": "dynamic.py", "line": 3, "literal": "pkg.old"}],
        "config_refs": [{"file": "pyproject.toml", "line": 2, "context": 'target = "pkg.old"'}],
    }
    after = {
        **before,
        "config_refs": [{**before["config_refs"][0], "context": 'target = "pkg.new"'}],
        "dynamic_imported_by": [{**before["dynamic_imported_by"][0], "literal": "pkg.new"}],
        "index": {},
    }

    _compare_module(recipe_env, monkeypatch, before, after)

    output = capsys.readouterr().out
    assert '"static", "consumer"' in output
    assert '"dynamic", "dynamic", "dynamic.py", 3' in output
    assert '"config", "pyproject.toml", 2' in output
    assert "MISSING_IDENTITIES=[]" in output


@pytest.mark.parametrize("field", ["imported_by", "dynamic_imported_by", "config_refs"])
def test_module_comparison_rejects_missing_consumer_location(recipe_env, monkeypatch, field) -> None:
    """Losing any inventoried consumer kind fails even when metadata remains non-exhaustive."""
    before = {
        "imported_by": ["consumer"],
        "dynamic_imported_by": [{"importer": "dynamic", "path": "dynamic.py", "line": 3}],
        "config_refs": [{"file": "pyproject.toml", "line": 2}],
    }
    after = {**before, field: [], "index": {}}

    with pytest.raises(SystemExit, match="! consumer identity mismatch"):
        _compare_module(recipe_env, monkeypatch, before, after)


@needs_git_and_bash
@pytest.mark.integration
def test_import_smoke_prints_covering_pytest_command_without_running_it(recipe_env) -> None:
    """A real test-impact answer supplies the advisory selector while its test body stays unexecuted."""
    root, sentinels, env = recipe_env
    (root / "provider.py").write_text("def new(): return 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_provider.py").write_text(
        "from provider import new\nfrom pathlib import Path\ndef test_new():\n"
        '    Path("test_was_run").write_text("yes")\n    assert new() == 1\n',
        encoding="utf-8",
    )
    _save(
        sentinels,
        {
            "rename-SUBCOMMAND": "symbol",
            "rename-NEW_REF": "new",
            "sym-module": "provider",
            "sym-qname": "old",
            "rdeps-json": {"called_by": []},
            "ref-batch-json": {"batch": []},
        },
    )

    result = _run(_block(_SKILL, "IMPORT_SMOKE="), env, root)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ADVISORY pytest command:" in result.stdout
    assert "test_provider.py" in result.stdout
    assert not (root / "test_was_run").exists()


@needs_git_and_bash
@pytest.mark.integration
def test_symbol_edited_summary_includes_definition_and_consumers(recipe_env) -> None:
    """A failed symbol apply still lists its provider, import-edited consumer and other successful edits."""
    root, sentinels, env = recipe_env
    _save(
        sentinels,
        {
            "rename-SUBCOMMAND": "symbol",
            "sym-path": "provider.py",
            "processed-import-files": "consumer.py",
            "edited-files": "guide.rst\nprovider.py",
        },
    )

    result = _run(_block(_SKILL, 'print("Files edited:")'), env, root)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["Files edited:", "  consumer.py", "  guide.rst", "  provider.py"]
