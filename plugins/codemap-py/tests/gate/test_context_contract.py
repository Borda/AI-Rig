"""Tests for the plugin-agnostic codemap context/gates contract and its consumer wrappers.

Covers:
  * the shipped context contract carries its version header + every required section;
  * the shipped gates contract carries Gate A / Gate B machinery with all options;
  * develop/oss wrapper files resolve and read their own byte-identical manifested
    contract copies, keep a graceful-degradation fallback, and add only their
    per-plugin surface;
  * stranger-fixture — injecting the block on a fresh project yields a reference line that
    resolves to the shipped contract file.

Sibling-plugin wrapper tests skip when the sibling files are absent (installed-plugin isolation:
a lone codemap install has no develop/oss tree next to it).

Prose assertions compare against whitespace-collapsed text, so reflowing a Markdown paragraph never breaks them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _codemap_skill_text import between, flat_file
from codemap_py import integration

_PLUGIN_ROOT = Path(__file__).parent.parent.parent
_PLUGINS_DIR = _PLUGIN_ROOT.parent
_SHARED = _PLUGIN_ROOT / "claude-skills" / "_shared"
_CONTEXT_CONTRACT = _SHARED / "codemap-context.md"
_GATES_CONTRACT = _SHARED / "codemap-gates.md"

_DEVELOP_CONTEXT = _PLUGINS_DIR / "cc_develop" / "skills" / "_shared" / "codemap-context.md"
_DEVELOP_FIX = _PLUGINS_DIR / "cc_develop" / "skills" / "fix" / "SKILL.md"
_DEVELOP_QNAME = _PLUGINS_DIR / "cc_develop" / "bin" / "parse_target_qname.py"
_DEVELOP_GATES = _PLUGINS_DIR / "cc_develop" / "skills" / "_shared" / "codemap-gates.md"
_OSS_GATES = _PLUGINS_DIR / "cc_oss" / "skills" / "_shared" / "codemap-gates.md"
_PROVIDER_RESOLVE = 'resolve_shared_path.py" codemap-py claude-skills/_shared'


def _find_working_posix_bash() -> str | None:
    """Return a Bash executable that executes POSIX script syntax."""
    if sys.platform != "win32":
        candidates = ["bash"]
    else:
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles(x86)")]
        candidates = [
            *([os.environ["GIT_BASH"]] if os.environ.get("GIT_BASH") else []),
            *(str(Path(root) / "Git" / sub / "bash.exe") for root in roots if root for sub in ("bin", "usr/bin")),
            *(
                [str(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git" / "bin" / "bash.exe")]
                if os.environ.get("LOCALAPPDATA")
                else []
            ),
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


_POSIX_BASH = _find_working_posix_bash()
_skip_no_posix_bash = pytest.mark.skipif(_POSIX_BASH is None, reason="no working POSIX bash on this host")

# Shell fragments shared by the stubbed ``codemap-py query`` bodies. The snippet probes ``query --help`` once to learn
# whether this build knows ``--format``; that probe is capability detection, not a query, so it stays out of the trace.
_RECORD_QUERY = 'printf "%s\\n" "$*" >> "$TRACE"; printf \'%s\\n\' \'{"query_complete":true}\';'
_TARGET_ENV = {"TARGET_FN": "target", "TARGET_MODULE": "package.module", "TARGET_QUALIFIED": "package.module::target"}


def _batch_snippet() -> str:
    """Return the bash block of the contract's batch pre-flight pattern."""
    contract = _CONTEXT_CONTRACT.read_text(encoding="utf-8")
    return contract.split("## Batch pre-flight pattern", 1)[1].split("```bash", 1)[1].split("```", 1)[0]


def _help_branch(usage: str) -> str:
    """Return a stub ``query`` case branch answering ``--help`` with *usage*."""
    return f'case "$1" in --help) printf "%s\\n" "{usage}"; return 0;; esac; '


def _codemap_py_stubs(query_body: str) -> str:
    """Shell stub of the ``codemap-py`` dispatcher the batch snippet calls.

    ``index`` succeeds silently; ``query`` runs ``query_body`` with the ``query`` word already consumed, so the body
    sees the arguments the engine itself would receive.
    """
    return f'_cm_query() {{ {query_body} }}\ncodemap-py() {{ case "$1" in index) return 0;; query) shift; _cm_query "$@";; esac; }}'


def _make_index(tmp_path: Path) -> None:
    """Create the empty per-project index file the batch snippet probes for."""
    index_dir = tmp_path / ".cache" / "codemap"
    index_dir.mkdir(parents=True)
    (index_dir / f"{tmp_path.name}.json").write_text("{}\n", encoding="utf-8")


def _run_batch(tmp_path: Path, query_body: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Run the batch snippet in bash against a stubbed ``git`` and ``codemap-py`` and return the result.

    Shell functions, not executables on PATH: whether an extensionless file carrying a shebang counts as executable is
    a property of the host — Git-for-Windows decides it from mount flags and content sniffing, and Python's chmod
    cannot set that bit at all. The tests measure which queries the snippet issues, never how a host resolves a
    command, and ``command -v`` reports functions, so the snippet's own guard still runs. Values the shell reads are
    spelled the way it reads them: ``basename`` does not split on a backslash, so a native ``C:\\...\\repo`` would
    leave the whole string as the project name and the index probe would never match.
    """
    stubs = f'git() {{ printf "%s\\n" "$FAKE_REPO"; }}\n{_codemap_py_stubs(query_body)}\n'
    full_env = os.environ | {
        "FAKE_REPO": tmp_path.as_posix(),
        "TRACE": (tmp_path / "queries.txt").as_posix(),
        **env,
    }
    return subprocess.run(
        [_POSIX_BASH, "-c", stubs + _batch_snippet()],
        cwd=tmp_path,
        env=full_env,
        check=True,
        capture_output=True,
        text=True,
    )


def _queries(tmp_path: Path) -> list[str]:
    """Return the query lines the stubbed ``codemap-py`` recorded, or an empty list when none ran."""
    trace = tmp_path / "queries.txt"
    return trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []


class TestContextContract:
    """The shipped context contract is the plugin-agnostic single source of truth."""

    def test_has_version_header(self):
        """Contract header carries an explicit version string feeding the injection version check.

        The injection version check compares this header, so a contract edited without bumping it would go stale in
        every consumer silently.
        """
        assert "# Codemap context contract — v5" in _CONTEXT_CONTRACT.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "section",
        [
            "## Target derivation — pluggable (consumer supplies)",
            "## Core query map",
            "## Batch pre-flight pattern",
            "## Evidence-line contract",
            "## Coverage metadata in output",
            "## Effort-tier guidance",
            "## Extended scan — multi-file / API changes",
            "## Targeted-edit pattern (known symbol, large file)",
        ],
    )
    def test_carries_required_section(self, section: str):
        """Every generic section the wrappers delegate to must be present in the contract.

        Wrappers point at these headings instead of copying them, so a renamed or dropped section leaves a dangling
        delegation.
        """
        assert section in flat_file(_CONTEXT_CONTRACT)

    @pytest.mark.parametrize("term", ["codemap_evidence:", "exhaustive", "partial", "stale", "unknown"])
    def test_defines_evidence_line_and_completeness_state(self, term: str):
        """The evidence line and all four completeness states are defined once in the contract.

        Consumers quote these exact words in their evidence line, so each state must exist in the single source.
        """
        assert term in flat_file(_CONTEXT_CONTRACT)

    @pytest.mark.parametrize(
        "phrase",
        [
            "exact file",
            "symbol",
            "localized",
            "skip codemap",
            "caller",
            "dependency",
            "blast radius",
            "test impact",
            "explicit structural",
            "tool requirement",
            "override",
            "smallest complete query",
        ],
    )
    def test_routes_localized_edits_only_when_a_structural_fact_remains(self, phrase: str):
        """A known local edit skips retrieval unless a fact or explicit requirement still needs it.

        The routing guidance is prose; hyphens and wrapping are normalised so the check pins the vocabulary, not the
        layout.
        """
        text = " ".join(_CONTEXT_CONTRACT.read_text(encoding="utf-8").lower().replace("-", " ").split())

        assert phrase in text

    @pytest.mark.parametrize(
        "route", ["skip", "central", "callers", "blast", "dependencies", "test-impact", "coupling", "standard"]
    )
    def test_documents_every_adaptive_route(self, route: str):
        """Each adaptive retrieval route is named in the consumer-facing target derivation section.

        Consumers choose a route by name, so an undocumented route would be unreachable from the wrappers.
        """
        text = _CONTEXT_CONTRACT.read_text(encoding="utf-8")

        assert f"`{route}`" in between(text, "## Target derivation", "## Core query map")

    def test_adaptive_routes_exclude_unscoped_symbol_lookup(self):
        """Quick routes must not turn a module-qualified target into an ambiguous bare symbol query.

        A bare ``imports``/``source`` route would look a symbol up by name alone; only the ``standard`` fallback may
        issue ``symbol --with-imports``.
        """
        text = _CONTEXT_CONTRACT.read_text(encoding="utf-8")
        derivation = between(text, "## Target derivation", "## Core query map")
        batch = between(text, 'case "$_CM_ROUTE" in', "    esac")
        standard = batch.split("        *)", 1)[1]

        assert "`imports`" not in derivation
        assert "`source`" not in derivation
        assert "imports)" not in batch
        assert "source)" not in batch
        assert "symbol --with-imports" in standard

    @_skip_no_posix_bash
    @pytest.mark.parametrize(
        ("query_kind", "expected_queries", "index_present"),
        [
            pytest.param("skip", [], True, id="localized-skip"),
            pytest.param(
                "callers", ["--timeout 5 fn-rdeps package.module::target --exclude-tests"], True, id="direct-callers"
            ),
            pytest.param(
                "test-impact", ["--timeout 5 test-impact package.module::target"], True, id="targeted-test-impact"
            ),
            pytest.param("coupling", ["--timeout 5 coupled"], True, id="targetless-coupling"),
            pytest.param(
                "imports",
                [
                    "--timeout 5 central --top 5",
                    "--timeout 5 fn-rdeps package.module::target --exclude-tests",
                    "--timeout 5 fn-blast package.module::target",
                    "--timeout 5 symbol --with-imports target",
                ],
                True,
                id="removed-symbol-route-falls-back-to-standard",
            ),
            pytest.param(
                "central", ["--timeout 5 central --top 5"], False, id="missing-index-reaches-auto-preparing-query"
            ),
        ],
    )
    def test_batch_preflight_executes_only_the_selected_route(
        self, tmp_path: Path, query_kind: str, expected_queries: list[str], index_present: bool
    ) -> None:
        """The executable guard must skip retrieval or issue only the mapped compact query.

        The contract's bash block runs for real against stubbed ``git`` and ``codemap-py``; only the stub's trace of
        issued queries is compared, so the snippet's own routing decides the outcome.
        """
        if index_present:
            _make_index(tmp_path)

        _run_batch(
            tmp_path,
            _help_branch("usage: codemap-py query") + _RECORD_QUERY,
            CODEMAP_QUERY_KIND=query_kind,
            **_TARGET_ENV,
        )

        assert _queries(tmp_path) == expected_queries

    @_skip_no_posix_bash
    @pytest.mark.parametrize(
        ("currency", "freeze", "lazy", "enabled", "no_heal"),
        [
            pytest.param("stale", "0", "", {"CODEMAP_ENABLED": "true"}, True, id="persisted-continue-stale-choice"),
            pytest.param("current", "1", "", {"CODEMAP_ENABLED": "true"}, True, id="inherited-autobuild-freeze"),
            pytest.param(
                "current", "0", "1", {"CODEMAP_ENABLED": "true"}, False, id="lazy-preference-alone-allows-query-heal"
            ),
            pytest.param("current", "0", "", {}, False, id="unset-enabled-preserves-auto-query"),
        ],
    )
    def test_stale_or_frozen_queries_reach_cli_with_no_heal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        currency: str,
        freeze: str,
        lazy: str,
        enabled: dict[str, str],
        no_heal: bool,
    ) -> None:
        """A persisted stale choice reaches the CLI as ``--no-heal``; LAZY alone does not freeze queries.

        ``CODEMAP_ENABLED`` is cleared from the inherited environment and set only through ``enabled``; the single
        recorded query must carry ``--no-heal`` exactly when the index is stale or the autobuild freeze is inherited.
        """
        monkeypatch.delenv("CODEMAP_ENABLED", raising=False)

        _run_batch(
            tmp_path,
            _help_branch("usage: codemap-py query") + _RECORD_QUERY,
            CODEMAP_QUERY_KIND="central",
            CODEMAP_CURRENCY=currency,
            SCAN_NO_AUTOBUILD=freeze,
            LAZY_CODEMAP=lazy,
            **enabled,
        )

        queries = _queries(tmp_path)
        assert len(queries) == 1
        assert ("--no-heal" in queries[0]) is no_heal
        assert queries[0].endswith("central --top 5")

    @_skip_no_posix_bash
    def test_persisted_skip_blocks_later_query(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A persisted ``CODEMAP_ENABLED=false`` stops the snippet from issuing any query.

        The user chose to skip codemap for this skill run; a later query in the same run must honour that choice.
        """
        monkeypatch.delenv("CODEMAP_ENABLED", raising=False)

        _run_batch(
            tmp_path,
            _help_branch("usage: codemap-py query") + _RECORD_QUERY,
            CODEMAP_QUERY_KIND="central",
            CODEMAP_CURRENCY="current",
            SCAN_NO_AUTOBUILD="0",
            LAZY_CODEMAP="",
            CODEMAP_ENABLED="false",
        )

        assert _queries(tmp_path) == []

    @_skip_no_posix_bash
    @pytest.mark.parametrize(
        ("query_kind", "tsv_commands", "json_commands"),
        [
            pytest.param("central", ["central --top 5"], [], id="central-is-tabular"),
            pytest.param("coupling", ["coupled"], [], id="coupled-is-tabular"),
            pytest.param("callers", ["fn-rdeps package.module::target --exclude-tests"], [], id="fn-rdeps-is-tabular"),
            pytest.param("blast", ["fn-blast package.module::target"], [], id="fn-blast-is-tabular"),
            pytest.param("test-impact", [], ["test-impact package.module::target"], id="test-impact-refuses-tsv"),
            pytest.param("dependencies", [], ["rdeps package.module"], id="rdeps-refuses-tsv"),
            pytest.param(
                "standard",
                [
                    "central --top 5",
                    "fn-rdeps package.module::target --exclude-tests",
                    "fn-blast package.module::target",
                ],
                ["symbol --with-imports target"],
                id="symbol-stays-json-beside-three-tables",
            ),
        ],
    )
    def test_tsv_is_requested_only_for_the_commands_that_render_as_one_table(
        self, tmp_path: Path, query_kind: str, tsv_commands: list[str], json_commands: list[str]
    ) -> None:
        """Only four query kinds may carry ``--format tsv``.

        ``rdeps`` and ``test-impact`` exit 1 ``format_not_tabular``, which the wrapper would read as a miss and
        downgrade completeness for. ``symbol`` does render as a table, but a one-row one whose header roughly equals its
        payload and whose ``source`` field would become a single quoted multi-line cell — tabular, and still the wrong
        format.
        """
        _make_index(tmp_path)

        _run_batch(
            tmp_path,
            _help_branch("usage: codemap-py query [--format {json,tsv}]") + _RECORD_QUERY,
            CODEMAP_QUERY_KIND=query_kind,
            **_TARGET_ENV,
        )

        assert _queries(tmp_path) == [f"--timeout 5 --format tsv {c}" for c in tsv_commands] + [
            f"--timeout 5 {c}" for c in json_commands
        ]

    @_skip_no_posix_bash
    def test_an_older_scan_query_without_format_keeps_every_query_on_json(self, tmp_path: Path) -> None:
        """``--format`` arrived in 0.37.0; an older build on PATH must not be handed the flag.

        Argparse rejects an unknown option with exit 2 and an empty stdout, which the wrapper would score as a miss on
        the four commands worth the most.
        """
        _make_index(tmp_path)
        reject_format = (
            'case "$*" in *--format*) printf "%s\\n" "unrecognized arguments: --format" >&2; return 2;; esac; '
        )

        result = _run_batch(
            tmp_path,
            _help_branch("usage: codemap-py query [--timeout N]") + reject_format + _RECORD_QUERY,
            CODEMAP_QUERY_KIND="central",
        )

        assert _queries(tmp_path) == ["--timeout 5 central --top 5"]
        assert "completeness=exhaustive" in result.stdout

    @_skip_no_posix_bash
    @pytest.mark.parametrize(
        ("envelope", "expected"),
        [
            pytest.param('{"index": {"stale": true}}', "completeness=stale", id="stale-on-stderr"),
            pytest.param('{"index": {"query_complete": false}}', "completeness=partial", id="incomplete-on-stderr"),
            pytest.param('{"index": {"stale": false}}', "completeness=partial", id="missing-completeness"),
            pytest.param(
                '{"index": {"stale": false, "query_complete": true}}', "completeness=exhaustive", id="clean-on-stderr"
            ),
        ],
    )
    def test_the_tsv_envelope_is_read_from_stderr(self, tmp_path: Path, envelope: str, expected: str) -> None:
        """Staleness and completeness must survive the move to stderr.

        Discarding stderr would report a stale index as ``exhaustive``, and the contract grants consumers permission to
        skip re-querying on exactly that value — so the failure would be silently wrong answers rather than a missing
        optimisation.
        """
        _make_index(tmp_path)
        tsv_body = _help_branch("usage: codemap-py query [--format {json,tsv}]") + (
            'printf "%s\\n" "name\tn" ; printf "%s\\n" "a\t1"; printf "%s\\n" "$ENVELOPE" >&2;'
        )

        result = _run_batch(tmp_path, tsv_body, CODEMAP_QUERY_KIND="central", ENVELOPE=envelope)

        assert expected in result.stdout
        assert "hits=1" in result.stdout

    @_skip_no_posix_bash
    def test_an_empty_tsv_table_counts_as_a_hit(self, tmp_path: Path) -> None:
        """A query that matched nothing is an answer, not a retrieval failure.

        Under JSON an empty result still carries its keys, so the wrapper saw a non-empty stdout. Under TSV it writes no
        rows at all, and judging emptiness on stdout would score it a miss and drop completeness to ``unknown``.
        """
        _make_index(tmp_path)
        empty_table_body = _help_branch("usage: codemap-py query [--format {json,tsv}]") + (
            'printf "%s\\n" \'{"index": {"stale": false, "query_complete": true}}\' >&2;'
        )

        result = _run_batch(tmp_path, empty_table_body, CODEMAP_QUERY_KIND="central")

        assert "queries_run=1 hits=1" in result.stdout
        assert "completeness=exhaustive" in result.stdout

    @_skip_no_posix_bash
    @pytest.mark.parametrize("failure", ["nonzero", "error", "empty"])
    def test_partial_preflight_never_claims_exhaustive(self, tmp_path: Path, failure: str) -> None:
        """One successful query cannot hide another failed or missing direction.

        The ``standard`` route runs two directional queries; the stub makes the ``rdeps`` direction fail in one of three
        ways while the other succeeds, and the summary must read ``partial`` rather than ``exhaustive``.
        """
        _make_index(tmp_path)
        failing_rdeps_body = (
            'case "$*" in --help) return 0;; *rdeps*) '
            "case \"$FAILURE\" in nonzero) printf '%s\\n' '{\"query_complete\":true}'; return 1;; "
            "error) printf '%s\\n' '{\"error\":\"unavailable\"}'; return 0;; empty) return 0;; esac;; esac; "
            "printf '%s\\n' '{\"query_complete\":true}';"
        )

        result = _run_batch(
            tmp_path,
            failing_rdeps_body,
            CODEMAP_QUERY_KIND="standard",
            TARGET_MODULE="pkg.mod",
            TARGET_FN="",
            FAILURE=failure,
        )

        assert "queries_run=2 hits=1 completeness=partial" in result.stdout

    def test_block_reference_target_matches_contract(self):
        """The managed block identifies the shipped integration contract.

        The rendered block names ``shared/integration-contract.md``; that file must also exist in the plugin, or the
        reference points at nothing.
        """
        body = integration._managed_block_body("claude", "develop", "1.0.0")
        rendered = integration._render_managed_block(body)
        assert "Contract: shared/integration-contract.md" in rendered
        assert (_PLUGIN_ROOT / "shared" / "integration-contract.md").is_file()


class TestGatesContract:
    """The shipped gates contract carries the plugin-agnostic Gate A / Gate B machinery."""

    @pytest.mark.parametrize(
        "marker",
        [
            "## Gate A — missing index",
            "## Gate B — stale index",
            "Continue without codemap",
            "Build index now",
            "Abort",
            "Rebuild now",
            "Continue with stale data",
            "Skip codemap",
            "By default, run `codemap-py index` in the foreground",
            "Set `CODEMAP_ENABLED=true` only after successful preparation",
            "On failure, report the provider's actual error",
            "After success, rerun the consumer's normal gate-state helper",
            "Every later query in this skill must pass `--no-heal`",
            "use the consumer wrapper's own skip-state helper",
            "If inherited `SCAN_NO_AUTOBUILD=1`, do not build, refresh, or ask to override the freeze",
            "**`LAZY_CODEMAP` unset (default)**: rebuild without asking",
            "No `AskUserQuestion` in either case",
            "**`LAZY_CODEMAP` set** (any non-empty value): ask before rebuilding",
            "print `! codemap index busy — continuing with stale data`",
            "30 s default, `CODEMAP_GATE_TIMEOUT` overrides",
        ],
    )
    def test_carries_gate_machinery(self, marker: str):
        """Both gates and every option/action survive in the generic gates contract.

        Consumers delegate their gate prompts to these exact phrases, so a reworded option silently changes behaviour in
        every wrapper.
        """
        assert marker in flat_file(_GATES_CONTRACT)

    def test_build_action_never_model_invokes_disabled_skill(self):
        """Build/rebuild action must not Skill()-call scan-codebase — it is disable-model-invocation:true.

        A model-invoked call to a disabled skill fails at run time, so the contract must route the build through the CLI
        instead.
        """
        assert 'Skill(skill="codemap:scan-codebase")' not in _GATES_CONTRACT.read_text(encoding="utf-8")


@pytest.mark.skipif(not _DEVELOP_CONTEXT.is_file(), reason="develop plugin sibling tree absent")
class TestDevelopWrapper:
    """The develop context wrapper references the contract and keeps only its per-plugin surface."""

    def test_never_uses_bare_relative_cross_plugin_path(self):
        """Wrapper must not cross-reference the codemap plugin via a bare relative path.

        A relative path breaks once the plugins install into separate cache directories.
        """
        assert "../codemap" not in _DEVELOP_CONTEXT.read_text(encoding="utf-8")

    def test_wrapper_exports_the_route_variable(self):
        """The develop wrapper defines the query-kind variable and its ``skip`` route for the shared batch.

        The shared batch reads ``CODEMAP_QUERY_KIND``; a wrapper that never sets it would fall through to ``standard``
        on every task.
        """
        wrapper = flat_file(_DEVELOP_CONTEXT)

        assert "CODEMAP_QUERY_KIND" in wrapper
        assert "CODEMAP_QUERY_KIND=skip" in wrapper

    @pytest.mark.parametrize(
        "marker", ["CODEMAP_QUERY_KIND", "exact file/symbol", "explicit structural/tool request", "`standard`"]
    )
    def test_fix_workflow_classifies_retrieval_before_the_batch(self, marker: str):
        """The production fix workflow must classify retrieval before loading the shared batch.

        Each marker is part of the route decision the fix skill hands to the shared batch.
        """
        assert marker in flat_file(_DEVELOP_FIX)

    def test_fix_route_guidance_excludes_removed_routes(self):
        """The fix workflow's route guidance never offers the removed ``imports`` and ``source`` routes.

        Those routes would issue an unscoped symbol lookup; the guidance sits between the route heading and the first
        bash block.
        """
        fix = _DEVELOP_FIX.read_text(encoding="utf-8")

        route_guidance = between(fix, "**Codemap route and target derivation**", "```bash")

        assert "`imports`" not in route_guidance
        assert "`source`" not in route_guidance

    @pytest.mark.skipif(not _DEVELOP_QNAME.is_file(), reason="develop plugin sibling tree absent")
    def test_fix_route_fails_safe_to_standard(self) -> None:
        """An unresolved route must fall back to `standard`, wherever that fallback now lives.

        The fallback used to be an inline `CODEMAP_QUERY_KIND="standard"` assignment in the SKILL.md; it moved into
        `parse_target_qname.py` when the block was extracted. Pin it at its current home so this gate keeps testing the
        contract rather than a shell spelling.
        """
        qname = _DEVELOP_QNAME.read_text(encoding="utf-8")

        assert "fails safe to standard" in flat_file(_DEVELOP_FIX)
        assert '_DEFAULT_QUERY_KIND = "standard"' in qname
        assert "query_kind = _DEFAULT_QUERY_KIND" in qname

    @pytest.mark.parametrize("surface", ["uncovered --top 20", "mock-rdeps", "undocumented", "codemap_scan.py"])
    def test_retains_per_plugin_surface(self, surface: str):
        """Develop-specific dimensions and the batch producer script stay in the wrapper.

        The shared contract is plugin-agnostic, so these develop-only query dimensions must live in the wrapper.
        """
        assert surface in flat_file(_DEVELOP_CONTEXT)

    def test_cache_script_documented_as_oss_owned_not_invoked(self):
        """The wrapper names `codemap_cache.py` only as oss-owned; develop never carries its command block.

        The `write`/`read`/`report` fence used to live here and reached into the oss plugin's `bin/`; the ownership
        paragraph replaced it, so the name must survive in prose while the invocation must not come back.
        """
        text = flat_file(_DEVELOP_CONTEXT)
        assert "`codemap_cache.py` ship in the oss plugin" in text
        assert "bin/codemap_cache.py" not in text


_skip_develop_context_absent = pytest.mark.skipif(
    not _DEVELOP_CONTEXT.is_file(), reason="develop plugin sibling tree absent"
)
_skip_develop_gates_absent = pytest.mark.skipif(
    not _DEVELOP_GATES.is_file(), reason="develop plugin sibling tree absent"
)
_skip_oss_gates_absent = pytest.mark.skipif(not _OSS_GATES.is_file(), reason="oss plugin sibling tree absent")


class TestConsumerWrappers:
    """Each consumer wrapper references its shipped contract live instead of carrying a copy.

    Covers the develop context wrapper and the develop and oss gates wrappers; a case skips when its sibling plugin tree
    is absent.
    """

    @pytest.mark.parametrize(
        ("wrapper", "contract_name"),
        [
            pytest.param(
                _DEVELOP_CONTEXT, "codemap-context.md", marks=_skip_develop_context_absent, id="develop-context"
            ),
            pytest.param(_DEVELOP_GATES, "codemap-gates.md", marks=_skip_develop_gates_absent, id="develop-gates"),
            pytest.param(_OSS_GATES, "codemap-gates.md", marks=_skip_oss_gates_absent, id="oss-gates"),
        ],
    )
    def test_reads_the_contract_from_the_active_install(self, wrapper: Path, contract_name: str):
        """Wrapper resolves the active codemap-py install and reads this plugin's contract live.

        No manifested copy, no newest-version cache glob, no bare cross-plugin source path.
        """
        text = wrapper.read_text(encoding="utf-8")
        assert _PROVIDER_RESOLVE in text
        assert f'cat "$_CODEMAP_SHARED/{contract_name}"' in text
        assert not wrapper.with_name(f"codemap-py--{contract_name}").exists()
        assert "codemap-py--" not in text
        assert "plugins/cache" not in text
        assert "plugins/codemap-py/claude-skills/_shared" not in text


class TestRequiredPhrases:
    """Shipped contract and wrapper files carry the exact phrases their consumers rely on.

    Covers the context contract's consumer header and consumer-supplied target derivation, the gates contract's version
    header and consumer declaration, and each consumer wrapper's own surface plus its graceful fallback; a wrapper case
    skips when its sibling plugin tree is absent.
    """

    @pytest.mark.parametrize(
        ("path", "first_phrase", "second_phrase"),
        [
            # Consumer header names the managed-block contract and wrapper consumers.
            pytest.param(
                _CONTEXT_CONTRACT,
                "<!-- file: codemap-context.md",
                "codemap-py.integration.v2",
                id="context-declares-cross-plugin-consumers",
            ),
            # Target derivation is explicitly consumer-supplied, not baked into the generic contract.
            pytest.param(
                _CONTEXT_CONTRACT,
                "consumer-supplied inputs",
                "contract doesn't derive them",
                id="context-target-derivation-is-pluggable",
            ),
            # Gates contract carries its version header and a cross-plugin consumer declaration.
            pytest.param(
                _GATES_CONTRACT,
                "# Codemap gates contract — v4",
                "<!-- file: codemap-gates.md",
                id="gates-version-header-and-consumer-declaration",
            ),
            # Context wrapper degrades gracefully when the codemap plugin is absent — never a broken load.
            pytest.param(
                _DEVELOP_CONTEXT,
                "Fallback when codemap plugin absent",
                "Never break load.",
                marks=_skip_develop_context_absent,
                id="develop-context-graceful-fallback",
            ),
            # Gates wrappers supply their skip flag (develop ``CODEMAP_RAW=auto``, oss ``CODEMAP_FORCE_OFF=false``).
            pytest.param(
                _DEVELOP_GATES,
                "CODEMAP_RAW=auto",
                "Never break load.",
                marks=_skip_develop_gates_absent,
                id="develop-gates-skip-flag",
            ),
            pytest.param(
                _OSS_GATES,
                "CODEMAP_FORCE_OFF=false",
                "Never break the load.",
                marks=_skip_oss_gates_absent,
                id="oss-gates-skip-flag",
            ),
        ],
    )
    def test_file_carries_both_phrases(self, path: Path, first_phrase: str, second_phrase: str):
        """The shipped file contains both phrases that pin its consumer declaration or fallback surface.

        Consumers and the injection check key off these literals, so removing one breaks them without a visible error.
        """
        text = flat_file(path)
        assert first_phrase in text
        assert second_phrase in text


def _commit_fixture(root: Path) -> None:
    """Commit the fixture baseline so apply's dirty-overlap guard can run honestly."""
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@t.t"),
        ("config", "user.name", "t"),
        ("add", "-A"),
        ("commit", "-q", "-m", "baseline"),
    ):
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


class TestStrangerFixtureManagedBlock:
    """A fresh consumer uses the current plan/apply protocol, never legacy injection."""

    def test_apply_plan_writes_contract_bound_block(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Plan then apply writes the allowlisted oss gates block with the contract reference.

        A fresh committed project with only an oss manifest is planned and applied for real (real ``git`` supplies the
        dirty-overlap guard); the written gates file must reference the shipped integration contract.
        """
        root = tmp_path / "fixture"
        manifest = root / "plugins" / "cc_oss" / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": "oss", "version": "1.0.0"}) + "\n", encoding="utf-8")
        _commit_fixture(root)
        monkeypatch.chdir(root)

        plan = integration.build_plan("claude", ["oss"], None, root / "plugins" / "codemap-py")
        result = integration.apply_plan(
            plan, plan["plan_sha256"], root / "plugins" / "codemap-py", tmp_path / "journal"
        )

        assert result["state"] == "complete"
        target = root / "plugins" / "cc_oss" / "skills" / "_shared" / "codemap-gates.md"
        assert "Contract: shared/integration-contract.md" in target.read_text(encoding="utf-8")
