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

import pytest

import codemap_cache  # type: ignore[import-not-found]

_OSS_ROOT = Path(__file__).resolve().parents[1]
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
    tokens = [t for t in args.split() if not t.startswith("-") and not t.lstrip("-").isdigit()]
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


@pytest.mark.parametrize("path", [pytest.param(p, id=p.name) for p in _SKILL_MD])
class TestQueryInvocations:
    """Every `codemap-py query` call embedded in oss skill markdown."""

    def test_target_requiring_subcommand_gets_a_positional(self, path: Path) -> None:
        """Require a query target before any optional flags.

        ``codemap-py query rdeps --top 10`` supplies no module, so argparse exits before any lookup; with the error
        swallowed by ``2>/dev/null || true`` the step produced nothing and reported nothing.
        """
        offenders = [
            sub for sub, rest in _iter_calls(path.read_text(encoding="utf-8")) if sub in _TARGET_REQUIRED and not rest
        ]
        assert offenders == [], f"{path.name}: {offenders} invoked without a positional target"


class TestIndexPathConvention:
    """The index path every guard derives must match the provider's own resolver."""

    def test_no_cwd_relative_index_dir(self) -> None:
        """Index dir defaults are anchored at the git root, never at the CWD."""
        offenders = [p.name for p in _SKILL_MD if "CODEMAP_INDEX_DIR:-.cache" in p.read_text(encoding="utf-8")]
        assert offenders == [], f"cwd-relative codemap index dir in: {offenders}"

    def test_no_project_name_sanitization(self) -> None:
        """The project name is the raw basename — no `tr -cd` filtering.

        Scoped to lines that actually derive the *project name*: a bare ``tr -cd`` search also hits the comments
        explaining why the filter was removed, and ``analyse/SKILL.md``'s ``_REPO_SLUG``, which sanitizes a GitHub
        ``owner/name`` pair into a filename fragment and has nothing to do with the index file the scanner writes.
        """
        offenders = [
            f"{p.name}:{n}"
            for p in _SKILL_MD
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if "tr -cd" in line and not line.lstrip().startswith("#") and _PROJ_DERIVATION_RE.search(line)
        ]
        assert offenders == [], f"project-name sanitization in: {offenders}"

    def test_no_dead_basename_fallback(self) -> None:
        """Non-git fallback uses a `[ -n ]` test, not `||` after `basename`.

        ``basename ""`` exits 0, so ``PROJ=$(basename "$(git ...)") || PROJ=...`` never fired and a non-git project
        silently got an empty project name.
        """
        dead = re.compile(r'basename "\$\(git rev-parse[^\n]*\)"[^\n]*\)\s*\|\|')
        offenders = [p.name for p in _SKILL_MD if dead.search(p.read_text(encoding="utf-8"))]
        assert offenders == [], f"dead basename fallback in: {offenders}"

    def test_no_sed_module_derivation(self) -> None:
        """Module names come from the index, never from a path-to-dotted sed.

        codemap names ``pkg/__init__.py`` after the package (``pkg``); the sed transform produced ``pkg.__init__``,
        which matches no index key and no cache key, so package-init changes got no structural context at all.
        """
        offenders = [p.name for p in _SKILL_MD if "s|^src/||" in p.read_text(encoding="utf-8")]
        assert offenders == [], f"sed module derivation in: {offenders}"


class TestPerModuleQueries:
    """codemap_cache.PER_MODULE_QUERIES against the review pre-flight that feeds it."""

    def test_matches_review_preflight_block(self) -> None:
        """The cache's query list equals the per-module queries oss:review issues.

        The old comment claimed a reorder "is caught" by mirroring cc_develop's list.
        Nothing compared the two, they had already diverged (7 vs 5), and cc_develop is
        a different plugin this one may not import. The checkable relationship is the
        local one: what review emits per module is what the cache must group.
        """
        block = _OSS_ROOT.joinpath("skills/review/modes/codemap-context.md").read_text(encoding="utf-8")
        emitted = {
            sub
            for sub, rest in _iter_calls(block)
            if any(token in ('"$mod"', '"$qn"') for token in rest)  # per-module/per-qname loop calls
        }
        assert emitted == set(codemap_cache.PER_MODULE_QUERIES)


class TestSkillPrefixes:
    """Plugin-prefixed skill references in oss prose."""

    def test_no_retired_codemap_prefix(self) -> None:
        """The plugin is `codemap-py`; the bare `codemap:` prefix is retired."""
        retired = re.compile(r"codemap:[a-z]")
        offenders = [p.name for p in _SKILL_MD if retired.search(p.read_text(encoding="utf-8"))]
        assert offenders == [], f"retired `codemap:` skill prefix in: {offenders}"

    @pytest.mark.parametrize("skill", ["review", "resolve"])
    def test_build_path_is_the_gated_launcher(self, skill: str) -> None:
        """Gate wrappers name the gated binary and forbid model-invoking the skill.

        The former contract named ``scan-index`` while consumers used the gated launcher, so the wrapper once had to
        bind the reader to ``codemap-py index``. The contract now names the launcher itself; claiming a binding would
        describe a disagreement that no longer exists. The launcher assertion stays and the retired alias is asserted
        absent.
        """
        gates = _OSS_ROOT.joinpath("skills/_shared/codemap-gates.md").read_text(encoding="utf-8")
        assert "codemap-py index" in gates
        assert "scan-index" not in gates, "the retired alias has no reason to appear in a wrapper"
        assert "with one binding" not in gates, "the contract no longer disagrees"
        text = _OSS_ROOT.joinpath(f"skills/{skill}/SKILL.md").read_text(encoding="utf-8")
        assert "disable-model-invocation" in text, f"{skill}: build path must state the skill is not invocable"
