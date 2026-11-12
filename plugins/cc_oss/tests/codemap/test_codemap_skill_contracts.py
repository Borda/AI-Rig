"""Contract tests for the codemap wiring embedded in cc_oss skill markdown.

The codemap guard, the index-path convention and the per-module query list live in bash blocks inside
``skills/**/*.md``. Nothing executes them in CI, so every defect they carried was silent in production: a cwd-relative
index dir reported ``no_index`` from a subdirectory, a sanitized project name sought a file the scanner never wrote, and
a ``rdeps`` call with no positional argument errored into ``2>/dev/null`` forever. These tests read the markdown and
assert the invariants those bugs violated.
"""

from __future__ import annotations

import re
from pathlib import Path

import codemap_batch  # type: ignore[import-not-found]
import codemap_cache  # type: ignore[import-not-found]
import pytest
from _skill_source import read_skill_source
from _text_io import flat

_OSS_ROOT = Path(__file__).resolve().parents[2]
_SKILL_MD = sorted(_OSS_ROOT.joinpath("skills").rglob("*.md"))

# Subcommands whose parser declares a required positional (codemap_py.query
# `_build_parser`): deps/rdeps `module`, symbol `name`, fn-rdeps/fn-blast/test-impact
# `qname`. Calling one with flags only exits 2 before the index is ever read.
_TARGET_REQUIRED = frozenset({"deps", "rdeps", "symbol", "fn-rdeps", "fn-blast", "test-impact"})

# Args of one `codemap-py query` call: everything up to a pipe, redirect or newline.
_QUERY_CALL_RE = re.compile(r"codemap-py query\b([^\n|>]*)")

# A line deriving the index's project name: assigns a PROJ-ish variable, or takes the
# basename of the repo root. Unrelated slug sanitization (a GitHub `owner/name` pair)
# matches neither.
_PROJ_DERIVATION_RE = re.compile(r"\b_?PROJ\w*=|\bbasename\b")


def _split_call(args: str) -> tuple[str, list[str]]:
    """Split one query call's argument string into ``(subcommand, remaining_tokens)``.

    Bare integers are treated as flag values (``--timeout 5``, ``--top 20``), never as
    the subcommand or a target, so a flags-only call yields no remaining tokens.

    Examples:
        >>> _split_call(' --timeout 5 rdeps "$mod" ')
        ('rdeps', ['"$mod"'])
        >>> _split_call(" rdeps --top 10 ")
        ('rdeps', [])
        >>> _split_call(" --timeout 5 central --top 5 ")
        ('central', [])
    """
    tokens = [t for t in args.split() if not t.startswith(("-", "$")) and not t.lstrip("-").isdigit()]
    if not tokens:
        return "", []
    return tokens[0], tokens[1:]


def _iter_calls(text: str) -> list[tuple[str, list[str]]]:
    """Return ``(subcommand, remaining_tokens)`` for every query call in *text*.

    Examples:
        >>> _iter_calls("codemap-py query rdeps pkg.mod")
        [('rdeps', ['pkg.mod'])]
    """
    return [_split_call(match.group(1)) for match in _QUERY_CALL_RE.finditer(text)]


def _emitted_queries(block: str) -> set[str]:
    """Return the per-module subcommands a review pre-flight block issues, directly or through ``codemap_batch``.

    Calls inside the block's per-module/per-qname loop count directly. The supplement families are issued through
    ``bin/codemap_batch.py``: a family counts as emitted when the block builds it, and its queries are the helper's own
    table.
    """
    direct = {sub for sub, rest in _iter_calls(block) if any(token in ('"$mod"', '"$qn"') for token in rest)}
    families = re.findall(r'codemap_batch\.py" build (\w+) ', block)
    return direct | {cmd for family in families for cmd, *_ in codemap_batch.QUERY_FAMILIES[family]}


@pytest.fixture(name="skill_texts", scope="module")
def _skill_texts() -> dict[Path, str]:
    """Read every oss skill Markdown file once; the contents are never mutated."""
    return {path: read_skill_source(path) for path in _SKILL_MD}


#: Statements the wrapper's gate contract must keep saying, matched against whitespace-flattened text.
_GATES_REQUIRED = [
    pytest.param("Gate A prepares automatically by default", id="gate-a-automatic-default"),
    pytest.param("`LAZY_CODEMAP` is the explicit confirm-first preference", id="lazy-codemap-confirm-first"),
    pytest.param("Inherited `SCAN_NO_AUTOBUILD=1` freezes both gates", id="inherited-no-autobuild-freezes-gates"),
    pytest.param("detect_codemap.py --prefix <current-skill> --force-off", id="force-off-by-prefix"),
    pytest.param("pass `--no-heal` to every later query", id="no-heal-on-later-queries"),
    pytest.param("contract's own build/rebuild action is the gated `codemap-py index` launcher", id="gated-launcher"),
    pytest.param("without consumer overrides", id="without-consumer-overrides"),
]


@pytest.mark.parametrize("path", [pytest.param(p, id=p.relative_to(_OSS_ROOT).as_posix()) for p in _SKILL_MD])
class TestQueryInvocations:
    """Every `codemap-py query` call embedded in oss skill markdown."""

    def test_target_subcommand_gets_a_positional(self, path: Path, skill_texts: dict[Path, str]) -> None:
        """Subcommands that require a target are never called with flags only.

        ``codemap-py query rdeps --top 10`` supplies no module, so argparse exits before any lookup; with the error
        swallowed by ``2>/dev/null || true`` the step produced nothing and reported nothing.
        """
        offenders = [sub for sub, rest in _iter_calls(skill_texts[path]) if sub in _TARGET_REQUIRED and not rest]
        assert offenders == [], f"{path.name}: {offenders} invoked without a positional target"


class TestIndexPathConvention:
    """The index path every guard derives must match the provider's own resolver."""

    @pytest.mark.parametrize(
        ("needle", "description"),
        [
            pytest.param("CODEMAP_INDEX_DIR:-.cache", "cwd-relative codemap index dir", id="no-cwd-relative-index-dir"),
            pytest.param("s|^src/||", "sed module derivation", id="no-sed-module-derivation"),
        ],
    )
    def test_banned_text_absent_from_skill_markdown(
        self, skill_texts: dict[Path, str], needle: str, description: str
    ) -> None:
        """Index dir defaults are anchored at the git root, and module names come from the index, never from sed.

        A ``CODEMAP_INDEX_DIR:-.cache`` default is CWD-relative; it must be anchored at the git root. codemap names
        ``pkg/__init__.py`` after the package (``pkg``); a path-to-dotted sed transform produced ``pkg.__init__``, which
        matches no index key and no cache key, so package-init changes got no structural context at all.
        """
        offenders = [p.name for p, text in skill_texts.items() if needle in text]
        assert offenders == [], f"{description} in: {offenders}"

    def test_no_project_name_sanitization(self, skill_texts: dict[Path, str]) -> None:
        """The project name is the raw basename, with no `tr -cd` filtering.

        Scoped to lines that actually derive the *project name*: a bare ``tr -cd`` search also hits the comments
        explaining why the filter was removed, and ``analyse/SKILL.md``'s ``_REPO_SLUG``, which sanitizes a GitHub
        ``owner/name`` pair into a filename fragment and has nothing to do with the index file the scanner writes.
        """
        offenders = [
            f"{p.name}:{n}"
            for p, text in skill_texts.items()
            for n, line in enumerate(text.splitlines(), 1)
            if "tr -cd" in line and not line.lstrip().startswith("#") and _PROJ_DERIVATION_RE.search(line)
        ]
        assert offenders == [], f"project-name sanitization in: {offenders}"

    def test_no_dead_basename_fallback(self, skill_texts: dict[Path, str]) -> None:
        """Non-git fallback uses a `[ -n ]` test, not `||` after `basename`.

        ``basename ""`` exits 0, so ``PROJ=$(basename "$(git ...)") || PROJ=...`` never fired and a non-git project
        silently got an empty project name.
        """
        dead = re.compile(r'basename "\$\(git rev-parse[^\n]*\)"[^\n]*\)\s*\|\|')
        offenders = [p.name for p, text in skill_texts.items() if dead.search(text)]
        assert offenders == [], f"dead basename fallback in: {offenders}"


class TestPerModuleQueries:
    """codemap_cache.PER_MODULE_QUERIES against the review pre-flight that feeds it."""

    def test_matches_review_preflight_block(self) -> None:
        """The cache's query list equals the per-module queries oss:review issues.

        The old comment claimed a reorder "is caught" by mirroring cc_develop's list. Nothing compared the two, they
        had already diverged (7 vs 5), and cc_develop is a different plugin this one may not import. The checkable
        relationship is the local one: what review emits per module is what the cache must group.
        """
        block = _OSS_ROOT.joinpath("skills/review/modes/codemap-context.md").read_text(encoding="utf-8")
        assert _emitted_queries(block) == set(codemap_cache.PER_MODULE_QUERIES)


class TestSkillPrefixes:
    """Plugin-prefixed skill references in oss prose."""

    def test_no_retired_codemap_prefix(self, skill_texts: dict[Path, str]) -> None:
        """The plugin is `codemap-py`; the bare `codemap:` prefix is retired."""
        retired = re.compile(r"codemap:[a-z]")
        offenders = [p.name for p, text in skill_texts.items() if retired.search(text)]
        assert offenders == [], f"retired `codemap:` skill prefix in: {offenders}"

    @pytest.mark.parametrize("statement", _GATES_REQUIRED)
    def test_gate_contract_states(self, statement: str) -> None:
        """The wrapper's shared gate contract keeps the provider's automatic-default and gated-launcher rules.

        Review and resolve both defer to ``_shared/codemap-gates.md``; losing one of these sentences silently changes
        when an index is built. Text is compared with whitespace flattened so reflowing the paragraph cannot break it.
        """
        gates = _OSS_ROOT.joinpath("skills/_shared/codemap-gates.md").read_text(encoding="utf-8")
        assert statement in flat(gates)

    def test_gate_contract_has_no_retired_alias(self) -> None:
        """The retired ``scan-index`` alias has no reason to appear in the wrapper's gate contract."""
        gates = _OSS_ROOT.joinpath("skills/_shared/codemap-gates.md").read_text(encoding="utf-8")
        assert "scan-index" not in gates

    @pytest.mark.parametrize("skill", ["review", "resolve"])
    def test_skill_states_it_is_not_model_invocable(self, skill: str) -> None:
        """The build path of review and resolve states that the skill is not model-invocable.

        The gated launcher is only reachable from the user-typed skill; each SKILL.md must keep naming ``disable-model-
        invocation`` so that boundary stays visible next to the build instructions.
        """
        text = read_skill_source(_OSS_ROOT.joinpath(f"skills/{skill}/SKILL.md"))
        assert "disable-model-invocation" in text, f"{skill}: build path must state the skill is not invocable"
