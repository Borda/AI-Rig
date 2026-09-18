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
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


class TestContextContract:
    """The shipped context contract is the plugin-agnostic single source of truth."""

    def test_has_version_header(self):
        """Contract header carries an explicit version string feeding the injection version check."""
        text = _CONTEXT_CONTRACT.read_text(encoding="utf-8")
        assert "# Codemap context contract — v3" in text

    def test_declares_cross_plugin_consumers(self):
        """Consumer header names the managed-block contract and wrapper consumers."""
        text = _CONTEXT_CONTRACT.read_text(encoding="utf-8")
        assert "<!-- file: codemap-context.md" in text
        assert "codemap-py.integration.v2" in text

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
        """Every generic section the wrappers delegate to must be present in the contract."""
        assert section in _CONTEXT_CONTRACT.read_text(encoding="utf-8")

    def test_target_derivation_is_pluggable(self):
        """Target derivation is explicitly consumer-supplied, not baked into the generic contract."""
        text = _CONTEXT_CONTRACT.read_text(encoding="utf-8")
        assert "consumer-supplied inputs" in text
        assert "contract doesn't derive them" in text

    def test_carries_evidence_line_and_completeness_semantics(self):
        """The evidence line and all four completeness states are defined once in the contract."""
        text = _CONTEXT_CONTRACT.read_text(encoding="utf-8")
        assert "codemap_evidence:" in text
        for state in ("exhaustive", "partial", "stale", "unknown"):
            assert state in text

    def test_routes_localized_edits_only_when_a_structural_fact_remains(self):
        """A known local edit skips retrieval unless a fact or explicit requirement still needs it."""
        text = " ".join(_CONTEXT_CONTRACT.read_text(encoding="utf-8").lower().replace("-", " ").split())

        for phrase in ("exact file", "symbol", "localized", "skip codemap"):
            assert phrase in text
        for fact in ("caller", "dependency", "blast radius", "test impact"):
            assert fact in text
        for override in ("explicit structural", "tool requirement", "override"):
            assert override in text
        assert "smallest complete query" in text

    def test_adaptive_routes_exclude_unscoped_symbol_lookup(self):
        """Quick routes must not turn a module-qualified target into an ambiguous bare symbol query."""
        text = _CONTEXT_CONTRACT.read_text(encoding="utf-8")
        derivation = text.split("## Target derivation", 1)[1].split("## Core query map", 1)[0]
        batch = text.split('case "$_CM_ROUTE" in', 1)[1].split("    esac", 1)[0]
        standard = batch.split("        *)", 1)[1]

        for route in ("skip", "central", "callers", "blast", "dependencies", "test-impact", "coupling", "standard"):
            assert f"`{route}`" in derivation
        assert "`imports`" not in derivation
        assert "`source`" not in derivation
        assert "imports)" not in batch
        assert "source)" not in batch
        assert "symbol --with-imports" in standard

    @pytest.mark.skipif(_POSIX_BASH is None, reason="no working POSIX bash on this host")
    @pytest.mark.parametrize(
        ("query_kind", "expected_queries"),
        (
            pytest.param("skip", [], id="localized-skip"),
            pytest.param(
                "callers",
                ["--timeout 5 fn-rdeps package.module::target --exclude-tests"],
                id="direct-callers",
            ),
            pytest.param("test-impact", ["--timeout 5 test-impact package.module::target"], id="targeted-test-impact"),
            pytest.param("coupling", ["--timeout 5 coupled"], id="targetless-coupling"),
            pytest.param(
                "imports",
                [
                    "--timeout 5 central --top 5",
                    "--timeout 5 fn-rdeps package.module::target --exclude-tests",
                    "--timeout 5 fn-blast package.module::target",
                    "--timeout 5 symbol --with-imports target",
                ],
                id="removed-symbol-route-falls-back-to-standard",
            ),
        ),
    )
    def test_batch_preflight_executes_only_the_selected_route(
        self,
        tmp_path: Path,
        query_kind: str,
        expected_queries: list[str],
    ) -> None:
        """The executable guard must skip retrieval or issue only the mapped compact query."""
        contract = _CONTEXT_CONTRACT.read_text(encoding="utf-8")
        batch = contract.split("## Batch pre-flight pattern", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
        trace = tmp_path / "queries.txt"

        # Shell functions, not executables on PATH: whether an extensionless file carrying a
        # shebang counts as executable is a property of the host — Git-for-Windows decides it
        # from mount flags and content sniffing, and Python's chmod cannot set that bit at all.
        # This test measures which queries the snippet issues, never how a host resolves a
        # command, and `command -v` reports functions, so the snippet's own guard still runs.
        stubs = "\n".join(
            (
                'git() { printf "%s\\n" "$FAKE_REPO"; }',
                "scan-index() { return 0; }",
                # The snippet probes `scan-query --help` once to learn whether this build knows
                # `--format`. That probe is capability detection, not a query, so it stays out of
                # the trace; $SCAN_HELP decides which answer the stub gives.
                'scan-query() { case "$1" in --help) printf "%s\\n" "${SCAN_HELP:-usage: scan-query}"; return 0;; esac; '
                'printf "%s\\n" "$*" >> "$TRACE"; printf \'%s\\n\' \'{"query_complete":true}\'; }',
                "",
            )
        )

        index_dir = tmp_path / ".cache" / "codemap"
        index_dir.mkdir(parents=True)
        (index_dir / f"{tmp_path.name}.json").write_text("{}\n", encoding="utf-8")
        env = os.environ | {
            "CODEMAP_QUERY_KIND": query_kind,
            # Values the *shell* reads, so both are spelled the way it reads them: `basename`
            # does not split on a backslash, so a native `C:\...\repo` would leave the whole
            # string as the project name and the index probe below would never match.
            "FAKE_REPO": tmp_path.as_posix(),
            "TARGET_FN": "target",
            "TARGET_MODULE": "package.module",
            "TARGET_QUALIFIED": "package.module::target",
            "TRACE": trace.as_posix(),
        }

        subprocess.run(
            [_POSIX_BASH, "-c", stubs + batch],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        queries = trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []
        assert queries == expected_queries

    @pytest.mark.skipif(_POSIX_BASH is None, reason="no working POSIX bash on this host")
    @pytest.mark.parametrize(
        ("query_kind", "tsv_commands", "json_commands"),
        (
            pytest.param("central", ["central --top 5"], [], id="central-is-tabular"),
            pytest.param("coupling", ["coupled"], [], id="coupled-is-tabular"),
            pytest.param(
                "callers",
                ["fn-rdeps package.module::target --exclude-tests"],
                [],
                id="fn-rdeps-is-tabular",
            ),
            pytest.param("blast", ["fn-blast package.module::target"], [], id="fn-blast-is-tabular"),
            pytest.param(
                "test-impact",
                [],
                ["test-impact package.module::target"],
                id="test-impact-refuses-tsv",
            ),
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
        ),
    )
    def test_tsv_is_requested_only_for_the_commands_that_render_as_one_table(
        self,
        tmp_path: Path,
        query_kind: str,
        tsv_commands: list[str],
        json_commands: list[str],
    ) -> None:
        """Only four query kinds may carry ``--format tsv``.

        ``rdeps`` and ``test-impact`` exit 1 ``format_not_tabular``, which the wrapper would read as a miss and
        downgrade completeness for. ``symbol`` does render as a table, but a one-row one whose header roughly equals its
        payload and whose ``source`` field would become a single quoted multi-line cell — tabular, and still the wrong
        format.
        """
        contract = _CONTEXT_CONTRACT.read_text(encoding="utf-8")
        batch = contract.split("## Batch pre-flight pattern", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
        trace = tmp_path / "queries.txt"
        stubs = "\n".join(
            (
                'git() { printf "%s\\n" "$FAKE_REPO"; }',
                "scan-index() { return 0; }",
                'scan-query() { case "$1" in --help) printf "%s\\n" "usage: scan-query [--format {json,tsv}]"; return 0;; esac; '
                'printf "%s\\n" "$*" >> "$TRACE"; printf \'%s\\n\' \'{"query_complete":true}\'; }',
                "",
            )
        )

        index_dir = tmp_path / ".cache" / "codemap"
        index_dir.mkdir(parents=True)
        (index_dir / f"{tmp_path.name}.json").write_text("{}\n", encoding="utf-8")
        env = os.environ | {
            "CODEMAP_QUERY_KIND": query_kind,
            "FAKE_REPO": tmp_path.as_posix(),
            "TARGET_FN": "target",
            "TARGET_MODULE": "package.module",
            "TARGET_QUALIFIED": "package.module::target",
            "TRACE": trace.as_posix(),
        }

        subprocess.run(
            [_POSIX_BASH, "-c", stubs + batch],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        queries = trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []
        assert queries == [f"--timeout 5 --format tsv {c}" for c in tsv_commands] + [
            f"--timeout 5 {c}" for c in json_commands
        ]

    @pytest.mark.skipif(_POSIX_BASH is None, reason="no working POSIX bash on this host")
    def test_an_older_scan_query_without_format_keeps_every_query_on_json(self, tmp_path: Path) -> None:
        """``--format`` arrived in 0.37.0; an older build on PATH must not be handed the flag.

        Argparse rejects an unknown option with exit 2 and an empty stdout, which the wrapper would score as a miss on
        the four commands worth the most.
        """
        contract = _CONTEXT_CONTRACT.read_text(encoding="utf-8")
        batch = contract.split("## Batch pre-flight pattern", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
        trace = tmp_path / "queries.txt"
        stubs = "\n".join(
            (
                'git() { printf "%s\\n" "$FAKE_REPO"; }',
                "scan-index() { return 0; }",
                'scan-query() { case "$1" in --help) printf "%s\\n" "usage: scan-query [--timeout N]"; return 0;; esac; '
                'case "$*" in *--format*) printf "%s\\n" "unrecognized arguments: --format" >&2; return 2;; esac; '
                'printf "%s\\n" "$*" >> "$TRACE"; printf \'%s\\n\' \'{"query_complete":true}\'; }',
                "",
            )
        )

        index_dir = tmp_path / ".cache" / "codemap"
        index_dir.mkdir(parents=True)
        (index_dir / f"{tmp_path.name}.json").write_text("{}\n", encoding="utf-8")
        env = os.environ | {
            "CODEMAP_QUERY_KIND": "central",
            "FAKE_REPO": tmp_path.as_posix(),
            "TRACE": trace.as_posix(),
        }

        result = subprocess.run(
            [_POSIX_BASH, "-c", stubs + batch],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        assert trace.read_text(encoding="utf-8").splitlines() == ["--timeout 5 central --top 5"]
        assert "completeness=exhaustive" in result.stdout

    @pytest.mark.skipif(_POSIX_BASH is None, reason="no working POSIX bash on this host")
    @pytest.mark.parametrize(
        ("envelope", "expected"),
        (
            pytest.param('{"index": {"stale": true}}', "completeness=stale", id="stale-on-stderr"),
            pytest.param(
                '{"index": {"query_complete": false}}',
                "completeness=partial",
                id="incomplete-on-stderr",
            ),
            pytest.param('{"index": {"stale": false}}', "completeness=exhaustive", id="clean-on-stderr"),
        ),
    )
    def test_the_tsv_envelope_is_read_from_stderr(self, tmp_path: Path, envelope: str, expected: str) -> None:
        """Staleness and completeness must survive the move to stderr.

        Discarding stderr would report a stale index as ``exhaustive``, and the contract grants consumers permission to
        skip re-querying on exactly that value — so the failure would be silently wrong answers rather than a missing
        optimisation.
        """
        contract = _CONTEXT_CONTRACT.read_text(encoding="utf-8")
        batch = contract.split("## Batch pre-flight pattern", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
        stubs = "\n".join(
            (
                'git() { printf "%s\\n" "$FAKE_REPO"; }',
                "scan-index() { return 0; }",
                'scan-query() { case "$1" in --help) printf "%s\\n" "usage: scan-query [--format {json,tsv}]"; return 0;; esac; '
                'printf "%s\\n" "name\tn" ; printf "%s\\n" "a\t1"; printf "%s\\n" "$ENVELOPE" >&2; }',
                "",
            )
        )

        index_dir = tmp_path / ".cache" / "codemap"
        index_dir.mkdir(parents=True)
        (index_dir / f"{tmp_path.name}.json").write_text("{}\n", encoding="utf-8")
        env = os.environ | {
            "CODEMAP_QUERY_KIND": "central",
            "ENVELOPE": envelope,
            "FAKE_REPO": tmp_path.as_posix(),
        }

        result = subprocess.run(
            [_POSIX_BASH, "-c", stubs + batch],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        assert expected in result.stdout
        assert "hits=1" in result.stdout

    @pytest.mark.skipif(_POSIX_BASH is None, reason="no working POSIX bash on this host")
    def test_an_empty_tsv_table_counts_as_a_hit(self, tmp_path: Path) -> None:
        """A query that matched nothing is an answer, not a retrieval failure.

        Under JSON an empty result still carries its keys, so the wrapper saw a non-empty stdout. Under TSV it writes no
        rows at all, and judging emptiness on stdout would score it a miss and drop completeness to ``unknown``.
        """
        contract = _CONTEXT_CONTRACT.read_text(encoding="utf-8")
        batch = contract.split("## Batch pre-flight pattern", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
        stubs = "\n".join(
            (
                'git() { printf "%s\\n" "$FAKE_REPO"; }',
                "scan-index() { return 0; }",
                'scan-query() { case "$1" in --help) printf "%s\\n" "usage: scan-query [--format {json,tsv}]"; return 0;; esac; '
                'printf "%s\\n" \'{"index": {"stale": false}}\' >&2; }',
                "",
            )
        )

        index_dir = tmp_path / ".cache" / "codemap"
        index_dir.mkdir(parents=True)
        (index_dir / f"{tmp_path.name}.json").write_text("{}\n", encoding="utf-8")
        env = os.environ | {"CODEMAP_QUERY_KIND": "central", "FAKE_REPO": tmp_path.as_posix()}

        result = subprocess.run(
            [_POSIX_BASH, "-c", stubs + batch],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        assert "queries_run=1 hits=1" in result.stdout
        assert "completeness=exhaustive" in result.stdout

    def test_block_reference_target_matches_contract(self):
        """The managed block identifies the shipped integration contract."""
        body = integration._managed_block_body("claude", "develop", "1.0.0")
        rendered = integration._render_managed_block(body)
        assert "Contract: shared/integration-contract.md" in rendered
        assert (_PLUGIN_ROOT / "shared" / "integration-contract.md").is_file()


class TestGatesContract:
    """The shipped gates contract carries the plugin-agnostic Gate A / Gate B machinery."""

    def test_has_version_header_and_consumer_declaration(self):
        """Gates contract carries its version header and a cross-plugin consumer declaration."""
        text = _GATES_CONTRACT.read_text(encoding="utf-8")
        assert "# Codemap gates contract — v3" in text
        assert "<!-- file: codemap-gates.md" in text

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
            # The former bare `scan-index` alias had already been replaced by every skill
            # and consumer wrapper, which otherwise needed an explicit override.
            "run `codemap-py index` in the foreground",
            # v3 Gate B: auto-rebuild is the default, the confirm-first prompt is the opt-in —
            # a revert of the default flip must fail here, not only in a consumer's behaviour.
            "**`LAZY_CODEMAP` unset (default)**: rebuild without asking",
            "No `AskUserQuestion` in either case",
            "**`LAZY_CODEMAP` set** (any non-empty value): ask before rebuilding",
            "print `! codemap index busy — continuing with stale data`",
        ],
    )
    def test_carries_gate_machinery(self, marker: str):
        """Both gates and every option/action survive in the generic gates contract."""
        assert marker in _GATES_CONTRACT.read_text(encoding="utf-8")

    def test_build_action_never_model_invokes_disabled_skill(self):
        """Build/rebuild action must not Skill()-call scan-codebase — it is disable-model-invocation:true."""
        assert 'Skill(skill="codemap:scan-codebase")' not in _GATES_CONTRACT.read_text(encoding="utf-8")


@pytest.mark.skipif(not _DEVELOP_CONTEXT.is_file(), reason="develop plugin sibling tree absent")
class TestDevelopWrapper:
    """The develop context wrapper references the contract and keeps only its per-plugin surface."""

    def test_reads_the_context_contract_from_the_active_install(self):
        """Wrapper resolves the active codemap-py install and reads this plugin's context contract live.

        No manifested copy, no newest-version cache glob, no bare cross-plugin source path.
        """
        text = _DEVELOP_CONTEXT.read_text(encoding="utf-8")
        assert _PROVIDER_RESOLVE in text
        assert 'cat "$_CODEMAP_SHARED/codemap-context.md"' in text
        assert not _DEVELOP_CONTEXT.with_name("codemap-py--codemap-context.md").exists()
        assert "codemap-py--" not in text
        assert "plugins/cache" not in text
        assert "plugins/codemap-py/claude-skills/_shared" not in text

    def test_never_uses_bare_relative_cross_plugin_path(self):
        """Wrapper must not cross-reference the codemap plugin via a bare relative path."""
        text = _DEVELOP_CONTEXT.read_text(encoding="utf-8")
        assert "../codemap" not in text

    def test_keeps_graceful_fallback(self):
        """Wrapper degrades gracefully when the codemap plugin is absent — never a broken load."""
        text = _DEVELOP_CONTEXT.read_text(encoding="utf-8")
        assert "Fallback when codemap plugin absent" in text
        assert "Never break load." in text

    def test_fix_consumer_selects_the_shared_zero_or_task_fit_query_route(self) -> None:
        """The production fix workflow must classify retrieval before loading the shared batch."""
        wrapper = _DEVELOP_CONTEXT.read_text(encoding="utf-8")
        fix = _DEVELOP_FIX.read_text(encoding="utf-8")

        assert "CODEMAP_QUERY_KIND" in wrapper
        assert "CODEMAP_QUERY_KIND=skip" in wrapper
        for marker in ("CODEMAP_QUERY_KIND", "exact file/symbol", "explicit structural/tool request", "`standard`"):
            assert marker in fix

        route_guidance = fix.split("**Codemap route and target derivation**", 1)[1].split("```bash", 1)[0]
        assert "`imports`" not in route_guidance
        assert "`source`" not in route_guidance

    @pytest.mark.skipif(not _DEVELOP_QNAME.is_file(), reason="develop plugin sibling tree absent")
    def test_fix_route_fails_safe_to_standard(self) -> None:
        """An unresolved route must fall back to `standard`, wherever that fallback now lives.

        The fallback used to be an inline `CODEMAP_QUERY_KIND="standard"` assignment in the SKILL.md; it moved into
        `parse_target_qname.py` when the block was extracted. Pin it at its current home so this gate keeps testing the
        contract rather than a shell spelling.
        """
        fix = _DEVELOP_FIX.read_text(encoding="utf-8")
        assert "fails safe to standard" in fix

        qname = _DEVELOP_QNAME.read_text(encoding="utf-8")
        assert '_DEFAULT_QUERY_KIND = "standard"' in qname
        assert "query_kind = _DEFAULT_QUERY_KIND" in qname

    @pytest.mark.parametrize("surface", ["uncovered --top 20", "mock-rdeps", "undocumented", "codemap_scan.py"])
    def test_retains_per_plugin_surface(self, surface: str):
        """Develop-specific dimensions and the batch producer script stay in the wrapper."""
        assert surface in _DEVELOP_CONTEXT.read_text(encoding="utf-8")

    def test_cache_script_documented_as_oss_owned_not_invoked(self):
        """The wrapper names `codemap_cache.py` only as oss-owned; develop never carries its command block.

        The `write`/`read`/`report` fence used to live here and reached into the oss plugin's `bin/`; the ownership
        paragraph replaced it, so the name must survive in prose while the invocation must not come back.
        """
        text = _DEVELOP_CONTEXT.read_text(encoding="utf-8")
        assert "`codemap_cache.py` ship in the oss plugin" in text
        assert "bin/codemap_cache.py" not in text


@pytest.mark.skipif(not _DEVELOP_GATES.is_file(), reason="develop plugin sibling tree absent")
class TestDevelopGatesWrapper:
    """The develop gates wrapper references the gates contract and supplies its skip flag."""

    def test_reads_the_gates_contract_from_the_active_install(self):
        """Wrapper resolves the active codemap-py install and reads this plugin's gates contract live."""
        text = _DEVELOP_GATES.read_text(encoding="utf-8")
        assert _PROVIDER_RESOLVE in text
        assert 'cat "$_CODEMAP_SHARED/codemap-gates.md"' in text
        assert not _DEVELOP_GATES.with_name("codemap-py--codemap-gates.md").exists()
        assert "codemap-py--" not in text
        assert "plugins/cache" not in text
        assert "plugins/codemap-py/claude-skills/_shared" not in text

    def test_supplies_develop_skip_flag_and_fallback(self):
        """Wrapper carries develop's skip flag and a graceful fallback."""
        text = _DEVELOP_GATES.read_text(encoding="utf-8")
        assert "CODEMAP_RAW=auto" in text
        assert "Never break load." in text


@pytest.mark.skipif(not _OSS_GATES.is_file(), reason="oss plugin sibling tree absent")
class TestOssGatesWrapper:
    """The oss gates wrapper references the gates contract and supplies its skip flag."""

    def test_reads_the_gates_contract_from_the_active_install(self):
        """Wrapper resolves the active codemap-py install and reads this plugin's gates contract live."""
        text = _OSS_GATES.read_text(encoding="utf-8")
        assert _PROVIDER_RESOLVE in text
        assert 'cat "$_CODEMAP_SHARED/codemap-gates.md"' in text
        assert not _OSS_GATES.with_name("codemap-py--codemap-gates.md").exists()
        assert "codemap-py--" not in text
        assert "plugins/cache" not in text
        assert "plugins/codemap-py/claude-skills/_shared" not in text

    def test_supplies_oss_skip_flag_and_fallback(self):
        """Wrapper carries oss's skip flag and a graceful fallback."""
        text = _OSS_GATES.read_text(encoding="utf-8")
        assert "CODEMAP_FORCE_OFF=false" in text
        assert "Never break the load." in text


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
        """Plan then apply writes the allowlisted oss gates block with the contract reference."""
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
