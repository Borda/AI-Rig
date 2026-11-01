"""Skill-roster + truth-claim parity between ``claude-skills/`` and ``codex-skills/``.

Black-box against on-disk ``SKILL.md`` files and ``shared/capability-contract.md`` — the single source of truth-claims
both runtime rosters must not contradict (per that file's own header). Comparator helpers here are exercised positively
against the real skills and negatively against synthetic fixtures — real plugin files are never mutated to prove the
negative paths. Copy-sync facts (roster equality, the integration mode table, NOT-for cross-references) are not tests:
the repo-only ``check-codemap-skill-parity`` hook (``scripts/check_codemap_skill_parity.py``) owns them.

Prose claims are checked on whitespace-collapsed text (``_flat`` lower-cases, ``_squash`` keeps case), so reflowing a
paragraph never breaks an assertion; code-shaped checks stay on single lines.
"""

from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from itertools import pairwise
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_CLAUDE_SKILLS_DIR = _PLUGIN_ROOT / "claude-skills"
_CODEX_SKILLS_DIR = _PLUGIN_ROOT / "codex-skills"
_CAPABILITY_CONTRACT = _PLUGIN_ROOT / "shared" / "capability-contract.md"
_INTEGRATION_CONTRACT = _PLUGIN_ROOT / "shared" / "integration-contract.md"
_CODEX_MANIFEST = _PLUGIN_ROOT / ".codex-plugin" / "plugin.json"

_CANONICAL_SKILLS = {"scan-codebase", "query-code", "test-impact", "rename-refs", "integration", "debrief-coding"}

_RUNTIMES = [pytest.param(_CLAUDE_SKILLS_DIR, id="claude"), pytest.param(_CODEX_SKILLS_DIR, id="codex")]


# Query routing is a product contract, not merely documentation style.  Both
# surfaces must steer the model to the same supported high-leverage command.
_QUERY_CODE_REQUIRED_SNIPPETS = (
    "choose the smallest complete query set",
    "production module importers / blast radius | `rdeps <module> --exclude-tests`",
    "production centrality / highest in-degree | `central --top n --exclude-tests`",
    "internal-import coupling (not centrality)",
    "direct production callers | `fn-rdeps <module::symbol> --exclude-tests`",
    "transitive callers / function blast | `fn-blast <module::symbol>`",
    "broken sphinx cross-references | `xrefs --broken <module>`",
    "never `--depth`",
    "never invent flags",
    "ordinary repository reads remain allowed",
    "distinct independent ast/oracle view",
)
_QUERY_CODE_FORBIDDEN_SNIPPETS = ("fn-blast <module::symbol> --depth",)
_DIRECT_CALLER_ROUTING_RULES = (
    "for caller requests, use `fn-rdeps <module::symbol> --exclude-tests` for direct, every, all, production, "
    "and blast-radius wording; use `fn-blast <module::symbol>` only when the user explicitly asks for transitive "
    "callers, closure, hops, or all levels.",
    "direct/every/all/production/blast-radius callers → `fn-rdeps <module::symbol> --exclude-tests`; "
    "`fn-blast <module::symbol>` only for explicit transitive, closure, hops, or all-levels requests.",
)
_CALLER_AND_TEST_IMPORT_ROUTING_SNIPPETS = (
    "callers plus test-module importers",
    "`fn-rdeps <module::symbol> --exclude-tests`, then `rdeps <module>`",
)
_SYMBOL_TARGET_GRAMMAR_SNIPPETS = (
    "`symbol <name>` accepts",
    "`authenticate`",
    "`myclass.method`",
    "`module::symbol` belongs to `fn-*` call-graph queries",
    "requested qualified extension method",
    "`symbol myclass.add_feature`",
    "nearby `symbol myclass`",
    "`symbols <module>` listing",
)
_SYMBOL_TO_CALL_GRAPH_CHAINING_SNIPPETS = (
    "qualified_name",
    "<module>::<qualified_name>",
    "mypackage.module::myclass.method",
)
_OVERRIDE_CANDIDATE_SNIPPETS = (
    "find-symbol '<classsuffix>\\.<method>$' --exclude-tests --limit 0",
    "same-name",
    "override candidate",
    "inheritance",
    "verify ancestry",
    "package boundaries",
)
_LOCALIZED_EDIT_ROUTING_SNIPPETS = (
    "exact file",
    "symbol",
    "local",
    "skip codemap",
    "caller",
    "dependency",
    "blast radius",
    "test impact",
    "import",
    "source slice",
    "explicit structural",
    "tool requirement",
    "override",
    "smallest complete query",
)
_LIFECYCLE_BOUNDARY_ROUTING_SNIPPETS = (
    "lifecycle boundary",
    "callback/hook",
    "cancellation/exception",
    "scheduling/cleanup",
    "state transfer",
    "inspect source",
    "named test/oracle",
    "`fn-rdeps` for caller",
    "`fn-deps` for callee",
)
_CLAUDE_QUERY_CURRENT_REPOSITORY_SNIPPETS = (
    "run every query from the caller's current repository",
    "codemap-py query --compact <subcommand> [arguments]",
    "do not `cd` into `$claude_plugin_root`",
)
_CLAUDE_QUERY_COMMAND_LITERAL = "codemap-py query --compact <subcommand> [arguments]"
_CLAUDE_QUERY_BASH_PATTERNS = (
    "codemap-py query:*",
    "*/bin/codemap-py* query:*",
)
_CLAUDE_QUERY_ALLOWED_TOOLS = "allowed-tools: Bash(codemap-py query:*), Bash(*/bin/codemap-py* query:*), Read, Write"
_QUERY_PATH_BASE_SNIPPETS = (
    "complete-query paths are caller-repo-relative",
    "never skill-relative",
    "do not re-query/read/grep",
)
_CUSTOM_ROOT_INDEX_SNIPPETS = (
    "--index <emitted-index-path>",
    "--root <same-root>",
    "`--root <same-root>` to select that root's canonical index",
    "`--root` also sets query path resolution and the accepted custom-index boundary",
)
_QUERY_EXECUTION_CONTRACT_SNIPPETS = (
    "resolve the installed launcher once",
    "independent read-only queries may run concurrently",
    "prepares the canonical project index before answering",
    "dependent queries wait",
    "while that preparation may occur",
    "no arbitrary total-call cap",
    "bounded targeted correction retries",
    "same correction failure recurs",
    "complete, untruncated",
    "settles its own",
    "independent ast/oracle",
)
_GATED_DISPATCH_SKILLS = ("scan-codebase", "query-code", "test-impact", "rename-refs")

# Executed forms of the ungated `scan-index`/`scan-query` compatibility aliases, which take
# no read/write lease and are removed no earlier than 1.0.0.  Prose *mentions* (a roster
# explaining why it avoids them) are deliberately not matched — only command positions are.
_UNGATED_ALIAS_INVOCATIONS = (
    re.compile(r'"\$(?:SQ|SCAN_BIN)"'),  # launcher held in a shell variable
    re.compile(r'bin/scan-(?:index|query)"?\s'),  # direct path execution
    re.compile(r"locate_scan_query\.py"),  # alias-locator helper
)
_JQ_INVOCATIONS = (re.compile(r"\bjq\s+-r\b"), re.compile(r"\bjq\s+--arg"), re.compile(r"command -v jq"))
# The suppression idiom only: `2>/dev/null || echo "0"`.  A roster quoting the antipattern
# in order to forbid it must not trip this.
_SUPPRESSED_QUERY_DEFAULT = re.compile(r'2>/dev/null\s*\|\|\s*echo\s*"(?:\[\]|0)"')
_CODEMAP_BIN_INVOCATION = re.compile(r'(?m)^\s*"?\$\{?CODEMAP_BIN\}?')
_QNAME_GRAMMAR_CLAIM = "module-qualified form (`module::symbol`) is not accepted by `find-symbol`"
_TRUNCATION_DISCLOSURE_SNIPPETS = (
    "truncation at 20 items is a real cap",
    "`--limit 0`",
    "graph coverage only",
    "index.confidence",
    "index.truncated",
    "index.total_available",
)


def _skill_path(runtime_dir: Path, skill_name: str) -> Path:
    """Return one roster skill's ``SKILL.md`` path."""
    return runtime_dir / skill_name / "SKILL.md"


def _skill_text(runtime_dir: Path, skill_name: str) -> str:
    """Return one roster skill's raw ``SKILL.md`` text."""
    return _skill_path(runtime_dir, skill_name).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Lowercase and collapse whitespace for prose checks across line breaks.

    >>> _flat(" Read\\n  ONLY ")
    'read only'
    """
    return " ".join(text.lower().split())


def _squash(text: str) -> str:
    """Collapse whitespace but keep case, for code-shaped tokens that must match exactly.

    >>> _squash(" Read\\n  ONLY ")
    'Read ONLY'
    """
    return " ".join(text.split())


def _direct_caller_routing_violations(skill_text: str) -> list[str]:
    """Return a violation when ambiguous caller wording can select a transitive query.

    >>> _direct_caller_routing_violations(_DIRECT_CALLER_ROUTING_RULES[1])
    []
    """
    if any(rule in skill_text.lower() for rule in _DIRECT_CALLER_ROUTING_RULES):
        return []
    return ["ambiguous direct-caller wording lacks the fn-rdeps routing rule"]


def _claude_query_current_repository_violations(skill_text: str) -> list[str]:
    """Return violations when the Claude query skill can leave the caller repository."""
    normalized = skill_text.lower()
    violations = [
        f"missing current-repository query contract: {snippet}"
        for snippet in _CLAUDE_QUERY_CURRENT_REPOSITORY_SNIPPETS
        if snippet.lower() not in normalized
    ]
    if re.search(r"(?m)^\s*cd\s+", skill_text):
        violations.append("forbidden current-directory change in query-code command")
    return violations


def _claude_query_frontmatter_violations(skill_text: str) -> list[str]:
    """Return violations when query-code can invoke arbitrary Bash instead of its read-only CLI surface.

    >>> _claude_query_frontmatter_violations("---\\nallowed-tools: Read\\n---")
    ['query-code must allow only the PATH or installed absolute `codemap-py query` launchers, Read, and Write']
    """
    frontmatter = skill_text.split("---", 2)[1]
    if _CLAUDE_QUERY_ALLOWED_TOOLS in frontmatter:
        return []
    return ["query-code must allow only the PATH or installed absolute `codemap-py query` launchers, Read, and Write"]


def _negated_status_reads(shell_text: str) -> list[str]:
    """Return ``if !`` lines whose next non-blank line prints ``$?`` — the status of the negation, not the command."""
    code_lines = [line for line in shell_text.splitlines() if line.strip()]
    return [
        first
        for first, second in pairwise(code_lines)
        if re.search(r"if !\s", first) and re.search(r"^\s*printf.*\$\?", second)
    ]


def _claim_rows(
    claim: str, surfaces: dict[str, Path], required: tuple[str, ...], forbidden: tuple[str, ...] = ()
) -> list:
    """Build one ``(path, required, forbidden)`` case per surface, with id ``<claim>-<surface>``."""
    return [pytest.param(path, required, forbidden, id=f"{claim}-{label}") for label, path in surfaces.items()]


def _skill_surfaces(skill_name: str, *, contract: Path | None = None) -> dict[str, Path]:
    """Map ``claude`` and ``codex`` to the skill's ``SKILL.md``; add the shared *contract* under its stem when given."""
    surfaces = {
        "claude": _skill_path(_CLAUDE_SKILLS_DIR, skill_name),
        "codex": _skill_path(_CODEX_SKILLS_DIR, skill_name),
    }
    return surfaces if contract is None else {**surfaces, contract.stem: contract}


_QUERY = _skill_surfaces("query-code")
_QUERY_AND_CAPABILITY = _skill_surfaces("query-code", contract=_CAPABILITY_CONTRACT)
_SCAN = _skill_surfaces("scan-codebase")
_RENAME = _skill_surfaces("rename-refs")
_INTEGRATION_AND_CONTRACT = _skill_surfaces("integration", contract=_INTEGRATION_CONTRACT)
_DEBRIEF = _skill_surfaces("debrief-coding")
_REPORT_WRITERS = {
    f"{runtime}-{skill}": _skill_path(runtime_dir, skill)
    for runtime, runtime_dir in (("claude", _CLAUDE_SKILLS_DIR), ("codex", _CODEX_SKILLS_DIR))
    for skill in ("test-impact", "rename-refs")
}

# Prose claims: lower-cased, whitespace-collapsed text must contain every ``required`` and no ``forbidden`` snippet.
_PROSE_CLAIMS = [
    *_claim_rows(
        "query-routes-to-supported-commands", _QUERY, _QUERY_CODE_REQUIRED_SNIPPETS, _QUERY_CODE_FORBIDDEN_SNIPPETS
    ),
    *_claim_rows(
        "query-execution-bounds-concurrency-and-retries",
        _QUERY,
        _QUERY_EXECUTION_CONTRACT_SNIPPETS,
        ("maximum three codemap calls",),
    ),
    *_claim_rows("lifecycle-boundaries-are-nonlocal", _QUERY_AND_CAPABILITY, _LIFECYCLE_BOUNDARY_ROUTING_SNIPPETS),
    *_claim_rows("result-paths-from-callers-repository", _QUERY_AND_CAPABILITY, _QUERY_PATH_BASE_SNIPPETS),
    *_claim_rows(
        "direct-test-importers-use-rdeps",
        _QUERY_AND_CAPABILITY,
        (
            "`rdeps <module>`",
            "filter/report test",
            "`test-impact <target>`",
            "direct",
            "test",
            "import",
            "transitive",
        ),
    ),
    *_claim_rows("callers-and-test-importers-take-two-queries", _QUERY, _CALLER_AND_TEST_IMPORT_ROUTING_SNIPPETS),
    *_claim_rows("symbol-results-compose-into-call-graph-queries", _QUERY, _SYMBOL_TO_CALL_GRAPH_CHAINING_SNIPPETS),
    *_claim_rows("override-candidates-are-name-matches", _QUERY, _OVERRIDE_CANDIDATE_SNIPPETS),
    *_claim_rows("symbol-target-grammar", _QUERY, _SYMBOL_TARGET_GRAMMAR_SNIPPETS),
    *_claim_rows("result-cap-disclosed-beside-stop-rule", _QUERY, _TRUNCATION_DISCLOSURE_SNIPPETS),
    *_claim_rows(
        "routing-table-admits-it-is-partial", _QUERY, ("routing shortlist, not the parser's full surface", "--help")
    ),
    *_claim_rows("test-impact-subcommand-versus-skill-split", _QUERY, ("test-impact split", "one-off structural fact")),
    *_claim_rows(
        "root-selects-canonical-index-unless-index-overrides",
        _QUERY,
        ("--root <root>", "canonical index", "--index <", "file"),
        ("`--root` is path resolution only",),
    ),
    *_claim_rows("custom-root-followup-selects-emitted-index", _SCAN, _CUSTOM_ROOT_INDEX_SNIPPETS),
    *_claim_rows(
        "contract-keeps-result-cap",
        {"shared-contract": _CAPABILITY_CONTRACT},
        ("truncation at 20 items is a real cap", "`--limit 0`"),
    ),
    *_claim_rows(
        "contract-records-test-impact-routing-split",
        {"shared-contract": _CAPABILITY_CONTRACT},
        ("routing split, not a dead zone",),
    ),
    *_claim_rows(
        "test-impact-failure-is-not-an-empty-result",
        {"claude": _skill_path(_CLAUDE_SKILLS_DIR, "test-impact")},
        ("not a 'no affected tests' result",),
    ),
    *_claim_rows("rename-qname-matches-find-symbol", _RENAME, (_QNAME_GRAMMAR_CLAIM,), ("or full (",)),
    *_claim_rows(
        "rename-lists-non-call-sites",
        _RENAME,
        ("--kind name", "--files-from-json", "ancestor package", "--alias-in", "residual whole-word"),
    ),
    *_claim_rows("rename-reads-completeness-forward-first", _RENAME, ("query_complete",), ("index:{exhaustive,",)),
    *_claim_rows(
        "rename-never-applies-partial-caller-migration",
        _RENAME,
        ("stop before any source edit", "resolve every known caller before any source edit"),
        ("edit first 50 only", "warn/skip a caller absent", "warn and skip this caller"),
    ),
    *_claim_rows(
        "rename-keeps-preflight-without-arbitrary-stops",
        _RENAME,
        ("resolve every known caller before any source edit", "preserve", "uncommitted", "untracked", "non-git"),
        (">50 callers", "caller count > 50", "commit or stash", "require add/commit/stash"),
    ),
    *_claim_rows("report-paths-branch-scoped-never-overwritten", _REPORT_WRITERS, ("branch", "never overwrite")),
    *_claim_rows(
        "integration-demo-promises-current-evidence",
        _INTEGRATION_AND_CONTRACT,
        ("one representative structural smoke query", "token savings", "current-session activation"),
    ),
    *_claim_rows(
        "integration-separates-guidance-from-metadata-evidence",
        _INTEGRATION_AND_CONTRACT,
        ("active consumer", "metadata-only", "one", "artifact", "reuse", "installed", "session"),
    ),
    *_claim_rows(
        "debrief-aggregates-with-helper",
        _DEBRIEF,
        ("bin/debrief_stats.py",),
        ("read **every** returned path",),
    ),
    *_claim_rows(
        "debrief-reads-anonymized-telemetry",
        {"claude": _skill_path(_CLAUDE_SKILLS_DIR, "debrief-coding")},
        ("tools*-anon.jsonl",),
        ("never anonymized",),
    ),
]

# Token claims: whitespace-collapsed text, case kept — code-shaped literals the skills must (not) contain verbatim.
_CLAUDE_TEST_IMPACT = {"claude": _skill_path(_CLAUDE_SKILLS_DIR, "test-impact")}
_TOKEN_CLAIMS = [
    *_claim_rows(
        "integration-approval-names-plan-artifact",
        _skill_surfaces("integration"),
        ("SHA-256", "plan artifact"),
    ),
    *_claim_rows("scan-uses-one-unknown-flag-string", _SCAN, ("Unknown flag(s)",), ("Unsupported flag(s)",)),
    *_claim_rows(
        "scan-captures-scanner-status", {"claude": _skill_path(_CLAUDE_SKILLS_DIR, "scan-codebase")}, ("_SCAN_RC=$?",)
    ),
    *_claim_rows(
        "test-impact-reports-query-failure-status",
        _CLAUDE_TEST_IMPACT,
        ("_TI_RC=$?",),
    ),
    *_claim_rows(
        "test-impact-reparses-original-flags",
        _CLAUDE_TEST_IMPACT,
        ('--arguments="$QNAME $ARGUMENTS"',),
        ('--arguments="$QNAME"',),
    ),
    *_claim_rows(
        "test-impact-caveat-reads-summary-keys",
        _CLAUDE_TEST_IMPACT,
        ("`query_complete` reads `false`",),
        ("`query-complete`", "<completeness-reason"),
    ),
    *_claim_rows(
        "test-impact-parses-arguments-through-shlex-helper",
        _CLAUDE_TEST_IMPACT,
        ('parse_test_impact_args.py" --output-prefix "codemap-${_CM_PROJ}" --arguments="$ARGUMENTS"',),
        ("awk '{print $1}'",),
    ),
]


class TestRosters:
    """The Codex manifest points at the Codex skill roster."""

    def test_codex_manifest_advertises_skills_dir(self) -> None:
        """The Codex manifest declares the ``codex-skills/`` roster path (Phase 4, six-skill roster)."""
        manifest = json.loads(_CODEX_MANIFEST.read_text())

        assert manifest["skills"] == "./codex-skills/"


# --------------------------------------------------------------------------------------
# Prose and token claims — one case per (claim, surface).
# --------------------------------------------------------------------------------------


class TestSkillClaims:
    """Skills and shared contracts state the same routing, safety and reporting rules."""

    @pytest.mark.parametrize(("path", "required", "forbidden"), _PROSE_CLAIMS)
    def test_prose_states_claim(self, path: Path, required: tuple[str, ...], forbidden: tuple[str, ...]) -> None:
        """The document states every required phrase and none of the forbidden ones, however the prose is wrapped.

        Each case is one product-contract claim on one surface (a runtime ``SKILL.md`` or a shared contract), compared
        on lower-cased whitespace-collapsed text; a failure lists exactly which phrases are missing or present.
        """
        text = _flat(path.read_text(encoding="utf-8"))

        assert [snippet for snippet in required if snippet not in text] == []
        assert [snippet for snippet in forbidden if snippet in text] == []

    @pytest.mark.parametrize(("path", "required", "forbidden"), _TOKEN_CLAIMS)
    def test_text_contains_exact_tokens(
        self, path: Path, required: tuple[str, ...], forbidden: tuple[str, ...]
    ) -> None:
        """The skill contains every required code-shaped token and none of the forbidden ones, case-sensitively.

        Shell fragments, flag spellings and user-visible strings must match exactly; only whitespace is collapsed, so a
        reflow never matters but a changed spelling does.
        """
        text = _squash(path.read_text(encoding="utf-8"))

        assert [token for token in required if token not in text] == []
        assert [token for token in forbidden if token in text] == []

    @pytest.mark.parametrize("contract_path", [pytest.param(p, id=label) for label, p in _QUERY_AND_CAPABILITY.items()])
    def test_query_code_skips_fully_localized_edits_but_keeps_structural_routing(self, contract_path: Path) -> None:
        """Avoid retrieval with no unresolved fact while preserving explicit and structural demand.

        Hyphens are read as spaces so ``skip-codemap`` and ``skip codemap`` count as the same phrase.
        """
        text = _flat(contract_path.read_text(encoding="utf-8").replace("-", " "))

        assert [snippet for snippet in _LOCALIZED_EDIT_ROUTING_SNIPPETS if snippet not in text] == []

    @pytest.mark.parametrize("runtime_dir", _RUNTIMES)
    def test_symbol_grammar_warns_against_nearby_inventory(self, runtime_dir: Path) -> None:
        """Feature scaffolding queries the named method, not a nearby class or module inventory."""
        text = _flat(_skill_text(runtime_dir, "query-code"))

        assert "not nearby" in text or "not a nearby" in text

    @pytest.mark.parametrize("runtime_dir", _RUNTIMES)
    def test_rename_refs_batches_residual_reference_listing(self, runtime_dir: Path) -> None:
        """``fn-rdeps`` holds call edges only; rename-refs lists non-call sites through a batch query."""
        text = _flat(_skill_text(runtime_dir, "rename-refs"))

        assert "query batch" in text or "query --timeout 20 batch" in text

    @pytest.mark.parametrize("runtime_dir", _RUNTIMES)
    @pytest.mark.parametrize("skill_name", _GATED_DISPATCH_SKILLS)
    def test_gated_dispatch_drives_codemap_py_cli(self, runtime_dir: Path, skill_name: str) -> None:
        """Keep index/query work on the leased ``codemap-py`` CLI in both rosters, never the ungated aliases."""
        text = _skill_text(runtime_dir, skill_name)

        assert [p.pattern for p in _UNGATED_ALIAS_INVOCATIONS if p.search(text)] == []
        assert "codemap-py query" in text or "codemap-py index" in text

    @pytest.mark.parametrize("runtime_dir", _RUNTIMES)
    def test_rename_refs_needs_no_jq(self, runtime_dir: Path) -> None:
        """Keep rename-reference workflows independent of an optional JSON utility."""
        text = _skill_text(runtime_dir, "rename-refs")

        assert [p.pattern for p in _JQ_INVOCATIONS if p.search(text)] == []

    @pytest.mark.parametrize("skill_name", sorted(_CANONICAL_SKILLS))
    def test_every_codex_skill_carries_a_runtime_note(self, skill_name: str) -> None:
        """Codex has no ``bin/`` PATH entry and no plugin-root variable — every skill must say how to resolve one."""
        text = _skill_text(_CODEX_SKILLS_DIR, skill_name)

        assert "## Runtime note" in text
        assert "PLUGIN_ROOT" in text

    @pytest.mark.parametrize("helper", ["anonymize.py", "join_avoidance.py", "debrief_stats.py"])
    def test_codex_debrief_uses_portable_helper_launcher(self, helper: str) -> None:
        """Keep documented telemetry helpers runnable without a bare Python executable."""
        text = _skill_text(_CODEX_SKILLS_DIR, "debrief-coding")

        assert f'"PLUGIN_ROOT/bin/python" "PLUGIN_ROOT/bin/{helper}"' in text
        assert f"python PLUGIN_ROOT/bin/{helper}" not in text
        assert "bin/python.cmd" in text

    def test_no_roster_command_invokes_an_undefined_launcher_variable(self) -> None:
        """Prevent roster commands from invoking an undefined launcher variable while allowing contract prose."""
        offenders = [
            f"{runtime_dir.name}/{skill_name}"
            for runtime_dir in (_CLAUDE_SKILLS_DIR, _CODEX_SKILLS_DIR)
            for skill_name in sorted(_CANONICAL_SKILLS)
            if _CODEMAP_BIN_INVOCATION.search(_skill_text(runtime_dir, skill_name))
        ]

        assert offenders == []

    def test_test_impact_query_failure_is_not_suppressed(self) -> None:
        """A failed query must exit non-zero, never render as ``total=0`` — a false "no affected tests"."""
        text = _skill_text(_CLAUDE_SKILLS_DIR, "test-impact")

        assert _SUPPRESSED_QUERY_DEFAULT.search(text) is None
        assert re.search(r"query test-impact[^\n]*2>/dev/null", text) is None

    def test_scan_codebase_does_not_print_negated_status(self) -> None:
        """Capture the command status before branching, so a negated condition never reports the test's own ``$?``."""
        text = _skill_text(_CLAUDE_SKILLS_DIR, "scan-codebase")

        assert _negated_status_reads(text) == []

    @pytest.mark.parametrize("runtime_dir", _RUNTIMES)
    def test_query_code_routes_ambiguous_caller_requests_to_direct_callers(self, runtime_dir: Path) -> None:
        """Prevent `fn-blast` for direct caller requests phrased as a blast radius or every caller."""
        text = _skill_text(runtime_dir, "query-code").lower()

        assert _direct_caller_routing_violations(text) == []

    def test_claude_query_code_runs_from_the_callers_repository(self) -> None:
        """Keep installed-plugin execution from redirecting structural queries into the plugin checkout."""
        text = _skill_text(_CLAUDE_SKILLS_DIR, "query-code")

        assert _claude_query_current_repository_violations(text) == []

    def test_claude_query_code_limits_bash_to_the_query_cli(self) -> None:
        """Prevent query-code from rebuilding indexes or using unrelated shell recovery commands."""
        text = _skill_text(_CLAUDE_SKILLS_DIR, "query-code")

        assert _claude_query_frontmatter_violations(text) == []

    def test_claude_query_permission_pattern_matches_the_literal_path_launcher(self) -> None:
        """Ensure the restricted Bash pattern admits the non-expanding primary command, and only query subcommands."""
        command_pattern = f"{_CLAUDE_QUERY_BASH_PATTERNS[0].removesuffix(':*')}*"

        assert fnmatchcase(_CLAUDE_QUERY_COMMAND_LITERAL, command_pattern)
        assert not fnmatchcase(_CLAUDE_QUERY_COMMAND_LITERAL.replace(" query ", " index "), command_pattern)


class TestComparatorsRejectRegressions:
    """Each claim comparator fails on the exact historical regression it exists to catch."""

    def test_direct_caller_rule_rejects_transitive_substitute(self) -> None:
        """Ensure a plausible `fn-blast` substitute cannot satisfy the direct-caller routing contract."""
        wrong_rule = _DIRECT_CALLER_ROUTING_RULES[1].replace(
            "`fn-rdeps <module::symbol> --exclude-tests`", "`fn-blast <module::symbol>`"
        )

        assert _direct_caller_routing_violations(wrong_rule) == [
            "ambiguous direct-caller wording lacks the fn-rdeps routing rule"
        ]

    def test_current_repository_contract_rejects_plugin_root_directory_change(self) -> None:
        """Prove the launcher stays safe only while the caller repository remains the working directory."""
        stale = (
            'cd "$CLAUDE_PLUGIN_ROOT"\n"${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/codemap-py" query --compact rdeps '
            "package.module"
        )

        violations = _claude_query_current_repository_violations(stale)

        assert any("forbidden current-directory change" in violation for violation in violations)

    def test_frontmatter_rejects_unrestricted_bash(self) -> None:
        """Prove the frontmatter contract rejects the historical unrestricted Bash allowance."""
        stale = "---\nallowed-tools: Bash, Read, Write\n---\n"

        assert _claude_query_frontmatter_violations(stale) == [
            "query-code must allow only the PATH or installed absolute `codemap-py query` launchers, Read, and Write"
        ]

    def test_gated_dispatch_rejects_ungated_alias_call(self) -> None:
        """The dispatcher comparator rejects a launcher-variable call into the ungated alias."""
        stale = 'SQ=$(python3 bin/locate_scan_query.py)\n"$SQ" --timeout 20 find-symbol foo\n'

        assert [p.pattern for p in _UNGATED_ALIAS_INVOCATIONS if p.search(stale)] != []

    def test_suppressed_default_rejects_benign_zero_fallback(self) -> None:
        """The suppression comparator rejects the exact idiom that turned a broken index into an empty result."""
        stale = 'TOTAL=$(echo "$RESULT" | python3 -c "..." 2>/dev/null || echo "0")'

        assert _SUPPRESSED_QUERY_DEFAULT.search(stale) is not None

    def test_negated_status_comparator_flags_status_of_the_test(self) -> None:
        """The negated-status comparator flags a printf of ``$?`` right after an ``if !`` line."""
        stale = 'if ! "$SCAN" --run; then\n  printf "rc=%s\\n" "$?"\nfi\n'

        assert _negated_status_reads(stale) == ['if ! "$SCAN" --run; then']
