"""Tests for ``evolution_changelog``: changelog discovery and parsing, the linkage ladder, heuristics and aliases."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from _evolution_common import check_record, commit_id, entry_ids, parse_version
from _evolution_repos import ChangelogSection, RepoBuilder
from evolution_changelog import GhItem, build_entries, discover_changelog, main, parse_changelog
from evolution_releases import build_release_index

# --------------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------------


def _commit_releases(repo: RepoBuilder, releases: list[tuple[str, str, str | None]]) -> dict[str, dict[str, str]]:
    """Credit each release's ``rev-list <tag> ^<previous tag on its line>`` commits to it on its own line.

    Equals the index's per-line ``commit_releases`` for the small fixtures here, whose line chains follow ancestry.
    """
    tag_of = {parse_version(tag).canonical: tag for tag, _, _ in releases}
    credited: dict[str, dict[str, str]] = {}
    for tag, line, previous in releases:
        revs = [tag, f"^{tag_of[previous]}"] if previous else [tag]
        for sha in repo.git("rev-list", *revs).split():
            credited.setdefault(sha, {})[line] = parse_version(tag).canonical
    return credited


def _index(
    repo: RepoBuilder,
    releases: list[tuple[str, str, str | None]],
    unreleased: list[str] | None = None,
    predecessor: dict[str, str | None] | None = None,
) -> dict:
    """Build a release index by hand: ``(tag, line, previous version on that line)`` per release, oldest first.

    ``predecessor`` overrides the ancestry map, which otherwise equals the previous version on the line.
    """
    records = [
        {
            "tag": tag,
            "version": parse_version(tag).canonical,
            "sha": repo.git("rev-parse", f"{tag}^{{commit}}").strip(),
            "date": "",
            "line": line,
            "is_prerelease": False,
            "order": order,
        }
        for order, (tag, line, _) in enumerate(releases)
    ]
    return {
        "schema_version": 1,
        "releases": records,
        "unparsable_tags": [],
        "lines": {},
        "predecessor": predecessor or {parse_version(tag).canonical: previous for tag, _, previous in releases},
        "commit_releases": _commit_releases(repo, releases),
        "head": {"sha": repo.head(), "unreleased": list(unreleased or [])},
        "fingerprint": "",
    }


def _patch_id(repo: RepoBuilder, sha: str) -> str:
    """Return the stable patch-id of a non-merge commit through ``git show`` (independent of the module's diff-tree)."""
    diff = repo.git("show", "--format=", sha)
    proc = subprocess.run(
        ["git", "-C", str(repo.path), "patch-id", "--stable"],
        input=diff,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=repo.env,
    )
    return proc.stdout.split()[0]


def _by_text(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index every fix and column of a ``build_entries`` result by its text."""
    return {entry["text"]: entry for entry in [*result["fixes"], *result["columns"]]}


def _links(result: dict[str, Any]) -> dict[str, tuple[str, list[str]]]:
    """Return ``{text: (link_method, commits)}`` for every fix and column."""
    return {text: (entry["link_method"], entry["commits"]) for text, entry in _by_text(result).items()}


def _missing_fields(result: dict[str, Any]) -> list[str]:
    """Return ``<entry_id>:<field>`` for every contract or extension field missing from a fix or column."""
    extra = ("line", "patch_ids", "files", "symbols", "secondary_releases", "aliases", "text_refs")
    entries = [*result["fixes"], *result["columns"]]
    missing = [f"{entry['entry_id']}:{key}" for entry in entries for key in check_record("entry", entry)]
    return missing + [f"{entry['entry_id']}:{key}" for entry in entries for key in extra if key not in entry]


def _placement(entries: list[dict[str, Any]]) -> list[tuple[str, list[str], list[str]]]:
    """Return ``(release, secondary_releases, commits)`` per entry, in order."""
    return [(entry["release"], entry["secondary_releases"], entry["commits"]) for entry in entries]


def _ids(entries: list[dict[str, Any]]) -> list[str]:
    """Return the entry IDs in order."""
    return [entry["entry_id"] for entry in entries]


def _warning_heads(result: dict[str, Any]) -> list[str]:
    """Return each warning's leading phrase (text before the first ``:`` or ``;``)."""
    return [re.split(r"[:;]", warning, maxsplit=1)[0] for warning in result["warnings"]]


def _fix_text_refs(result: dict[str, Any]) -> dict[str, list[str]]:
    """Return ``{text: text_refs}`` for every fix."""
    return {entry["text"]: entry["text_refs"] for entry in result["fixes"]}


def _summary(result: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Return ``(id, kind, release, source, link_method, confidence, commits)`` for fixes, then columns."""
    keys = ("entry_id", "kind", "release", "source", "link_method", "link_confidence", "commits")
    return [tuple(entry[key] for key in keys) for entry in [*result["fixes"], *result["columns"]]]


def _write(root: Path, files: dict[str, str]) -> None:
    """Create files (with parent directories) under ``root``."""
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------------------------------------

_LINKED_ADDED = ["Retry helper for flaky calls (#5)", "Cache layer for lookups (#7)"]
_LINKED_TEXT = "Empty configuration files no longer crash"
_LINKED_UNLINKED = "Polish documentation wording"


@dataclass
class _Linked:
    """The linkage scenario: one release whose entries reach commits through every ladder rung."""

    repo: RepoBuilder
    squash: str
    merge: str
    fixed: str
    text: str
    sha_entry: str


@pytest.fixture
def linked(evo_repo: RepoBuilder) -> _Linked:
    """Build v1.0.0 then v1.1.0 with a squash PR, a merge PR, a SHA-cited fix, a text-matched fix and no-match fixes."""
    repo = evo_repo
    repo.write("src/core.py", "def load():\n    return 1\n")
    repo.commit("Initial import", day=1)
    repo.tag("v1.0.0", day=1)
    repo.branch("feat-retry")
    repo.checkout("feat-retry")
    repo.write("src/retry.py", "def retry():\n    return 2\n")
    repo.commit("wip retry", day=2)
    repo.checkout("main")
    squash = repo.squash_merge("feat-retry", "Add retry helper (#5)", day=3)
    repo.branch("feat-cache")
    repo.checkout("feat-cache")
    repo.write("src/cache.py", "class Cache:\n    pass\n")
    repo.commit("add cache module", day=4)
    repo.checkout("main")
    merge = repo.merge("feat-cache", "Merge pull request #7 from fork/feat-cache\n\nAdd cache layer", day=5)
    repo.write("src/core.py", "def load():\n    return 0\n")
    fixed = repo.commit("Correct load return value", day=6)
    repo.write("src/config.py", "def read():\n    return {}\n")
    text = repo.commit("Handle empty configuration files gracefully", day=7)
    repo.tag("v1.1.0", day=8)
    sha_entry = f"Wrong load result ({fixed[:12]})"
    fixes = [sha_entry, _LINKED_TEXT, _LINKED_UNLINKED, _LINKED_UNLINKED]
    repo.write_changelog([ChangelogSection("1.1.0", day=8, added=_LINKED_ADDED, fixed=fixes)])
    return _Linked(repo, squash, merge, fixed, text, sha_entry)


def _linked_result(scenario: _Linked) -> dict[str, Any]:
    """Run ``build_entries`` without GitHub on the linkage scenario."""
    index = _index(scenario.repo, [("v1.0.0", "main", None), ("v1.1.0", "main", "1.0.0")])
    return build_entries(scenario.repo.path, index, gh_available=False)


@dataclass
class _Heuristic:
    """The heuristic scenario: v1.0.0 covered by the changelog, v1.1.0 not."""

    repo: RepoBuilder
    initial: str
    feat: str
    fix: str
    index: dict


@pytest.fixture
def heuristic(evo_repo: RepoBuilder) -> _Heuristic:
    """Build a v1.1.0 with conventional ``feat:``/``fix:``/``docs:`` commits and no changelog section for it."""
    repo = evo_repo
    repo.write("src/app.py", "def run():\n    return 1\n")
    initial = repo.commit("Initial skeleton", day=1)
    repo.tag("v1.0.0", day=1)
    repo.write("src/metrics.py", "def emit():\n    return []\n")
    feat = repo.commit("feat: add metrics endpoint", day=2)
    repo.write("src/metrics.py", "def emit():\n    return [] or []\n")
    fix = repo.commit("fix(metrics): guard empty payload", day=3)
    repo.write("README.md", "docs\n")
    repo.commit("docs: describe metrics", day=4)
    repo.tag("v1.1.0", day=5)
    repo.write_changelog([ChangelogSection("1.0.0", day=1, added=["Initial skeleton"])])
    index = _index(repo, [("v1.0.0", "main", None), ("v1.1.0", "main", "1.0.0")])
    return _Heuristic(repo, initial, feat, fix, index)


@dataclass
class _Backport:
    """The backport scenario: a feature on main cherry-picked to a maintenance release tagged later."""

    repo: RepoBuilder
    main_sha: str
    cherry_sha: str
    result: dict


@pytest.fixture
def backport(evo_repo: RepoBuilder) -> _Backport:
    """Ship ``Add extra helper (#5)`` in v1.1.0 on main and its cherry-pick in v1.0.1 on a maintenance branch."""
    repo = evo_repo
    repo.write("src/core.py", "def load():\n    return 1\n")
    repo.commit("Initial import", day=1)
    repo.tag("v1.0.0", day=1)
    repo.branch("maint-1.0")
    repo.write("src/extra.py", "def extra():\n    return 3\n")
    main_sha = repo.commit("Add extra helper (#5)", day=2)
    repo.tag("v1.1.0", day=3)
    repo.checkout("maint-1.0")
    cherry_sha = repo.cherry_pick(main_sha, day=4)
    repo.tag("v1.0.1", day=4)
    repo.checkout("main")
    repo.write_changelog(
        [
            ChangelogSection("1.0.1", day=4, added=["Extra helper (#5)"]),
            ChangelogSection("1.1.0", day=3, added=["Extra helper (#5)"]),
        ]
    )
    releases = [("v1.0.0", "main", None), ("v1.1.0", "main", "1.0.0"), ("v1.0.1", "1.0", "1.0.0")]
    result = build_entries(repo.path, _index(repo, releases), gh_available=False)
    return _Backport(repo, main_sha, cherry_sha, result)


_REFS_FIX_LATER = "Parser crash on empty input since v1.1.0 (#11), follow-up to #10"
_REFS_FIX_SAME = "Exporter rounding error, see #12"
_REFS_FIX_EARLY = "Prepare exporter groundwork #12"


@dataclass
class _RefsScenario:
    """The cross-release reference scenario and the commits its entries may link to."""

    fix_sha: str
    exporter_sha: str
    result: dict


@pytest.fixture
def refs_scenario(evo_repo: RepoBuilder) -> _RefsScenario:
    """Build three releases where fixes cite feature PRs from an earlier, the same and a later release."""
    repo = evo_repo
    repo.write("src/core.py", "def load():\n    return 1\n")
    repo.commit("Initial import", day=1)
    repo.tag("v1.0.0", day=1)
    repo.write("src/parser.py", "def parse():\n    return None\n")
    repo.commit("Add parser (#10)", day=2)
    repo.tag("v1.1.0", day=3)
    repo.write("src/parser.py", "def parse():\n    return []\n")
    fix_sha = repo.commit("Fix parser crash (#11)", day=4)
    repo.write("src/exporter.py", "def export():\n    return 1\n")
    exporter_sha = repo.commit("Add exporter (#12)", day=5)
    repo.tag("v1.2.0", day=6)
    repo.write_changelog(
        [
            ChangelogSection("1.2.0", day=6, added=["Exporter (#12)"], fixed=[_REFS_FIX_LATER, _REFS_FIX_SAME]),
            ChangelogSection("1.1.0", day=3, added=["Parser (#10)"], fixed=[_REFS_FIX_EARLY]),
        ]
    )
    releases = [("v1.0.0", "main", None), ("v1.1.0", "main", "1.0.0"), ("v1.2.0", "main", "1.1.0")]
    result = build_entries(repo.path, _index(repo, releases), gh_available=False)
    return _RefsScenario(fix_sha, exporter_sha, result)


@pytest.fixture
def fragments_result(evo_repo: RepoBuilder) -> tuple[str, dict[str, Any]]:
    """Build a repo without a changelog whose unreleased commit adds a towncrier bugfix fragment for ``#21``."""
    repo = evo_repo
    repo.write("pyproject.toml", '[project]\nname = "demo"\n\n[tool.towncrier]\ndirectory = "changes"\n')
    repo.write("src/loader.py", "def load():\n    return 1\n")
    repo.commit("Initial import", day=1)
    repo.tag("v1.0.0", day=1)
    repo.write("src/loader.py", "def load():\n    return 2\n")
    repo.write("changes/21.bugfix.md", "Loader returned a stale value.\n")
    sha = repo.commit("Refresh loader cache (#21)", day=2)
    index = _index(repo, [("v1.0.0", "main", None)], unreleased=[sha])
    return sha, build_entries(repo.path, index, gh_available=False)


class _FakeGitHub:
    """GitHub lookup double: issue #21 is closed by PR #22 whose merge commit is given."""

    def __init__(self, merge_sha: str) -> None:
        """Remember the merge commit the fake PR #22 reports."""
        self.merge_sha = merge_sha
        self.requested: list[int] = []

    def items(self, numbers: list[int]) -> dict[int, GhItem]:
        """Return issue #21 (closed by #22) and PR #22; record what was asked."""
        self.requested += numbers
        known = {
            21: GhItem("issue", linked=(22,), linked_commits=(self.merge_sha,)),
            22: GhItem("pr", merge_commit=self.merge_sha, linked=(21,)),
        }
        return {number: known[number] for number in numbers if number in known}

    def commit_prs(self, shas: list[str]) -> dict[str, list[int]]:
        """Report no PR associations (unused by changelog-covered releases)."""
        return {}


# --------------------------------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------------------------------

_MD_CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Streaming reader

## [1.2.0] - 2024-03-01

### New

- Plugin hooks that span
  two lines
  - nested detail

### Bug Fixes

- Crash on empty input

### Security

- Escape HTML in reports

### Deprecated

- Old loader

## v1.1.0 (2024-02-01)

### Features

* Faster startup

## 1.0.0

### Changes

1. Renamed config key
"""

_RST_CHANGELOG = """Changelog
=========

1.2.0 (2024-01-01)
------------------

Features
~~~~~~~~

* Add streaming
  reader

Bug Fixes
~~~~~~~~~

* Fix crash

v1.1.0
------

Improved
~~~~~~~~

- Faster startup
"""


class TestParseChangelog:
    """Markdown and reStructuredText changelogs split into per-version Added/Changed/Fixed bullets."""

    def test_markdown_keep_a_changelog_with_synonyms(self) -> None:
        """Keep a Changelog Markdown maps subsection synonyms to buckets and keys versions canonically.

        Unreleased, bracketed, ``v``-prefixed and bare version headings each open a section; New, Bug Fixes, Security,
        Deprecated, Features and Changes map to their buckets; a two-line bullet and its nested bullet join into one.
        """
        assert parse_changelog(_MD_CHANGELOG, "md") == {
            "unreleased": {"added": ["Streaming reader"], "changed": [], "fixed": []},
            "1.2.0": {
                "added": ["Plugin hooks that span two lines nested detail"],
                "changed": ["Old loader"],
                "fixed": ["Crash on empty input", "Escape HTML in reports"],
            },
            "1.1.0": {"added": ["Faster startup"], "changed": [], "fixed": []},
            "1.0.0": {"added": [], "changed": ["Renamed config key"], "fixed": []},
        }

    def test_restructuredtext_underlined_headings(self) -> None:
        """ReStructuredText titles are recognised by underline style, with levels in order of first use.

        The document title (``=``) sits above the version titles (``-``), which sit above the subsections (``~``); an
        indented continuation line joins its bullet.
        """
        assert parse_changelog(_RST_CHANGELOG, "rst") == {
            "1.2.0": {"added": ["Add streaming reader"], "changed": [], "fixed": ["Fix crash"]},
            "1.1.0": {"added": [], "changed": ["Faster startup"], "fixed": []},
        }

    @pytest.mark.parametrize(
        ("heading", "key"),
        [
            pytest.param("## [1.2.0] - 2024-01-01", "1.2.0", id="keep-a-changelog"),
            pytest.param("## 1.2.0", "1.2.0", id="bare"),
            pytest.param("## v1.2.0 (2024-01-01)", "1.2.0", id="v-prefix-date"),
            pytest.param("## [Unreleased]", "unreleased", id="unreleased"),
            pytest.param("## [1.3.0rc1](https://example.invalid/compare/v1.2.0...v1.3.0rc1)", "1.3.0rc1", id="link"),
        ],
    )
    def test_version_heading_forms(self, heading: str, key: str) -> None:
        """Each supported version heading form produces its canonical version key.

        A heading followed by a Fixed subsection with one bullet yields exactly that key; link targets in the heading
        never contribute a version.
        """
        assert list(parse_changelog(f"{heading}\n\n### Fixed\n\n- A bug\n", "md")) == [key]

    @pytest.mark.parametrize(
        "marker",
        [pytest.param("**Fixed**", id="bold"), pytest.param("Bug fixes:", id="colon")],
    )
    def test_pseudo_heading_lines_open_a_subsection(self, marker: str) -> None:
        """A bold or colon-terminated line naming a subsection opens that subsection.

        Changelogs written as release notes often mark sections with ``**Fixed**`` instead of a heading.
        """
        assert parse_changelog(f"## 1.0.0\n\n{marker}\n\n- Crash\n", "md") == {
            "1.0.0": {"added": [], "changed": [], "fixed": ["Crash"]}
        }

    def test_txt_falls_back_to_restructuredtext(self) -> None:
        """A ``.txt`` changelog tries Markdown first and uses the reStructuredText reading when Markdown finds nothing.

        The reStructuredText fixture has no ``#`` headings, so only the fallback finds its two versions.
        """
        assert list(parse_changelog(_RST_CHANGELOG, "txt")) == ["1.2.0", "1.1.0"]

    def test_bullets_outside_a_subsection_are_dropped(self) -> None:
        """Bullets directly under a version heading, with no Added/Changed/Fixed subsection, are not entries.

        Such a release then counts as uncovered and falls back to the heuristic classification.
        """
        assert parse_changelog("## 1.0.0\n\n- Something\n", "md") == {
            "1.0.0": {"added": [], "changed": [], "fixed": []}
        }

    def test_unknown_format_raises(self) -> None:
        """An unsupported format name raises ``ValueError`` naming the accepted formats.

        Callers pass the format returned by ``discover_changelog``; anything else is a programming error.
        """
        with pytest.raises(ValueError, match="md, rst or txt"):
            parse_changelog("", "adoc")


# --------------------------------------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------------------------------------


class TestDiscoverChangelog:
    """Changelog discovery follows a documented directory, stem and extension precedence."""

    @pytest.mark.parametrize(
        ("files", "path", "fmt"),
        [
            pytest.param({"CHANGELOG.md": "", "docs/CHANGES.rst": ""}, "CHANGELOG.md", "md", id="root-before-docs"),
            pytest.param({"docs/source/HISTORY.rst": ""}, "docs/source/HISTORY.rst", "rst", id="docs-source"),
            pytest.param({"CHANGES.md": "", "CHANGELOG.rst": ""}, "CHANGELOG.rst", "rst", id="stem-before-extension"),
            pytest.param({"CHANGELOG-old.md": "", "CHANGELOG.rst": ""}, "CHANGELOG.rst", "rst", id="exact-stem-first"),
            pytest.param({"changelog.txt": ""}, "changelog.txt", "txt", id="lower-case-txt"),
            pytest.param({"NEWS": ""}, "NEWS", "txt", id="no-extension"),
            pytest.param({"docs/NEWS.md": "", "docs/source/CHANGELOG.md": ""}, "docs/NEWS.md", "md", id="dir-first"),
        ],
    )
    def test_precedence(self, tmp_path: Path, files: dict[str, str], path: str, fmt: str) -> None:
        """The highest-precedence candidate wins: directory, then stem, then exact stem, then extension.

        Root beats ``docs/`` beats ``docs/source/``; ``CHANGELOG`` beats ``CHANGES``; an exact stem beats a suffixed
        one; an extensionless file reads as plain text.
        """
        _write(tmp_path, files)
        found = discover_changelog(tmp_path)
        assert (found["path"], found["format"]) == (path, fmt)

    @pytest.mark.parametrize(
        "files",
        [
            pytest.param({}, id="empty"),
            pytest.param({"newsfragments.md": "", "CHANGELOG.bak": "", "src/CHANGELOG.md": ""}, id="non-candidates"),
        ],
    )
    def test_absent_returns_none(self, tmp_path: Path, files: dict[str, str]) -> None:
        """Without a candidate the result is an explicit None path and format, never an invented default.

        A name merely starting with a stem, a foreign extension, or a file outside the searched directories is no
        candidate.
        """
        _write(tmp_path, files)
        assert discover_changelog(tmp_path) == {"path": None, "format": None, "towncrier": None}

    def test_towncrier_config_and_fragments(self, tmp_path: Path) -> None:
        """A ``[tool.towncrier]`` section is reported with its fragment directory, rendered filename and fragments.

        The rendered ``NEWS.rst`` does not exist yet, so no changelog path is reported.
        """
        pyproject = '[project]\nname = "x"\n\n[tool.towncrier]\ndirectory = "changes"\nfilename = "NEWS.rst"\n'
        _write(tmp_path, {"pyproject.toml": pyproject, "changes/12.bugfix.md": "Fix crash\n", "changes/x.txt": ""})
        assert discover_changelog(tmp_path) == {
            "path": None,
            "format": None,
            "towncrier": {
                "config": "pyproject.toml",
                "directory": "changes",
                "filename": "NEWS.rst",
                "fragments": ["changes/12.bugfix.md"],
            },
        }


# --------------------------------------------------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------------------------------------------------


class TestLinkageLadder:
    """Changelog entries reach commits through SHA, PR, text match or stay unlinked."""

    def test_every_rung(self, linked: _Linked) -> None:
        """Each entry is linked by the first rung that finds commits.

        A cited SHA resolves directly; ``(#5)`` finds the squash commit and ``(#7)`` the merge commit (kept as the
        merge, not its branch commit); an entry sharing most words with a commit subject is text-matched; the rest are
        unlinked with no commits.
        """
        assert _links(_linked_result(linked)) == {
            linked.sha_entry: ("sha", [linked.fixed]),
            _LINKED_ADDED[0]: ("pr", [linked.squash]),
            _LINKED_ADDED[1]: ("pr", [linked.merge]),
            _LINKED_TEXT: ("text", [linked.text]),
            _LINKED_UNLINKED: ("unlinked", []),
        }

    def test_records_carry_contract_fields(self, linked: _Linked) -> None:
        """Every fix and column carries the common ``entry`` record fields plus the evolution extensions.

        A renamed or dropped key in the serialiser would otherwise reach the scorer and store unnoticed.
        """
        assert _missing_fields(_linked_result(linked)) == []

    def test_confidence_per_rung(self, linked: _Linked) -> None:
        """Exact rungs carry confidence 1.0, a text match at most 0.4, an unlinked entry 0.

        Three of five words are shared with the commit subject, so the text confidence is 0.4 x 0.6.
        """
        entries = _by_text(_linked_result(linked))
        assert (
            entries[linked.sha_entry]["link_confidence"],
            entries[_LINKED_ADDED[1]]["link_confidence"],
            entries[_LINKED_TEXT]["link_confidence"],
            entries[_LINKED_UNLINKED]["link_confidence"],
        ) == (1.0, 1.0, 0.24, 0.0)

    def test_identical_texts_get_occurrence_suffix(self, linked: _Linked) -> None:
        """Two identical Fixed bullets in one release become two rows, the second suffixed ``~2``.

        Both rows survive (same-release entries never alias each other), with IDs from ``entry_ids``.
        """
        result = _linked_result(linked)
        expected = entry_ids("1.1.0", [linked.sha_entry, _LINKED_TEXT, _LINKED_UNLINKED, _LINKED_UNLINKED])
        assert _ids(result["fixes"]) == expected
        assert expected[3] == f"{expected[2]}~2"

    def test_merge_column_footprint_uses_first_parent_diff(self, linked: _Linked) -> None:
        """A merge-linked feature column takes its files and added symbols from the merge's first-parent diff.

        The cache branch added ``src/cache.py`` with class ``Cache``; fixes carry no footprint.
        """
        entries = _by_text(_linked_result(linked))
        column = entries[_LINKED_ADDED[1]]
        assert (column["files"], column["symbols"]) == (["src/cache.py"], ["src/cache.py:Cache"])
        assert (entries[linked.sha_entry]["files"], entries[linked.sha_entry]["symbols"]) == ([], [])

    def test_gh_unavailable_warns(self, linked: _Linked) -> None:
        """Running without GitHub records a warning instead of failing.

        The changelog exists, so that is the only warning.
        """
        result = _linked_result(linked)
        assert result["changelog"] == {"path": "CHANGELOG.md", "format": "md"}
        assert len(result["warnings"]) == 1
        assert result["warnings"][0].startswith("gh unavailable")

    def test_issue_rung_through_github(self, evo_repo: RepoBuilder) -> None:
        """An issue number reaches the merge commit of its closing PR when GitHub is available.

        The fake says ``#21`` is an issue closed by PR ``#22`` whose merge commit exists locally; no local commit
        message names ``#21``, so the issue rung links the entry and moves 21 from PRs to issues.
        """
        repo = evo_repo
        repo.write("src/core.py", "x = 1\n")
        repo.commit("Initial import", day=1)
        repo.tag("v1.0.0", day=1)
        repo.write("src/sched.py", "def tick():\n    return 1\n")
        sha = repo.commit("Rework scheduler loop", day=2)
        repo.tag("v1.1.0", day=3)
        repo.write_changelog([ChangelogSection("1.1.0", day=3, fixed=["Scheduler race (#21)"])])
        index = _index(repo, [("v1.0.0", "main", None), ("v1.1.0", "main", "1.0.0")])
        result = build_entries(repo.path, index, gh_available=True, github=_FakeGitHub(sha))
        fix = result["fixes"][0]
        assert (fix["link_method"], fix["commits"], fix["refs"]["prs"], fix["refs"]["issues"]) == (
            "issue",
            [sha],
            [],
            [21],
        )
        assert result["warnings"] == []


class TestHeuristicFallback:
    """Releases without a changelog section are classified from conventional-commit prefixes."""

    def test_uncovered_release_uses_commit_prefixes(self, heuristic: _Heuristic) -> None:
        """``feat:`` and ``fix(scope):`` commits become heuristic column and row; ``docs:`` is ignored.

        Their IDs are ``commit/<patch-id>`` of the commit's own diff, with confidence capped at 0.5. The covered v1.0.0
        keeps its changelog entry, text-matched to the identical commit subject (full overlap, confidence 0.4).
        """
        result = build_entries(heuristic.repo.path, heuristic.index, gh_available=False)
        fix_id = commit_id(_patch_id(heuristic.repo, heuristic.fix))
        feat_id = commit_id(_patch_id(heuristic.repo, heuristic.feat))
        assert _summary(result) == [
            (fix_id, "fix", "1.1.0", "heuristic", "heuristic", 0.5, [heuristic.fix]),
            (
                entry_ids("1.0.0", ["Initial skeleton"])[0],
                "feature",
                "1.0.0",
                "changelog",
                "text",
                0.4,
                [heuristic.initial],
            ),
            (feat_id, "feature", "1.1.0", "heuristic", "heuristic", 0.5, [heuristic.feat]),
        ]

    def test_cli_writes_entries(
        self, heuristic: _Heuristic, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The ``entries`` subcommand reads the index file, writes the result JSON and prints a one-line summary.

        ``--no-gh`` keeps the run offline regardless of an installed ``gh``.
        """
        index_file, out_file = tmp_path / "index.json", tmp_path / "out" / "entries.json"
        index_file.write_text(json.dumps(heuristic.index), encoding="utf-8")
        argv = ["entries", "--repo", str(heuristic.repo.path), "--index", str(index_file), "--out", str(out_file)]
        assert main([*argv, "--no-gh"]) == 0
        written = json.loads(out_file.read_text(encoding="utf-8"))
        assert (len(written["fixes"]), len(written["columns"])) == (1, 2)
        assert capsys.readouterr().out.startswith("entries: 1 fixes, 2 columns, 0 aliases")


class TestBackportAliases:
    """Entries of one kind with equal patch-id sets in different releases fold into the earliest release's entry."""

    def test_backported_feature_is_one_column_with_alias(self, backport: _Backport) -> None:
        """The cherry-picked feature keeps one canonical column at v1.1.0, aliased by the later v1.0.1 entry.

        v1.1.0 is earlier by index order than the maintenance tag v1.0.1, so it is canonical; the alias lists the v1.0.1
        ID, records v1.0.1 as a secondary release and adds the cherry-pick to the footprint commits.
        """
        canonical, alias = entry_ids("1.1.0", ["Extra helper (#5)"])[0], entry_ids("1.0.1", ["Extra helper (#5)"])[0]
        columns = backport.result["columns"]
        assert len(columns) == 1
        assert (columns[0]["entry_id"], columns[0]["aliases"], columns[0]["secondary_releases"]) == (
            canonical,
            [alias],
            ["1.0.1"],
        )
        assert columns[0]["commits"] == sorted([backport.main_sha, backport.cherry_sha])
        assert backport.result["aliases"] == {alias: canonical}


class TestTextRefs:
    """Fix text references point only at columns introduced at or before the fix's release."""

    def test_refs_respect_release_order(self, refs_scenario: _RefsScenario) -> None:
        """A shared PR number links a fix to a column from an earlier or the same release, never a later one.

        The v1.2.0 fix citing ``#10`` refers to the v1.1.0 parser column; the v1.2.0 fix citing ``#12`` refers to the
        v1.2.0 exporter column; the v1.1.0 fix citing ``#12`` gets nothing because that column ships later.
        """
        parser = entry_ids("1.1.0", ["Parser (#10)"])[0]
        exporter = entry_ids("1.2.0", ["Exporter (#12)"])[0]
        assert _fix_text_refs(refs_scenario.result) == {
            _REFS_FIX_LATER: [parser],
            _REFS_FIX_SAME: [exporter],
            _REFS_FIX_EARLY: [],
        }

    def test_regression_phrase_records_version(self, refs_scenario: _RefsScenario) -> None:
        """A "since vX" phrase naming an indexed release at or before the fix lands in ``refs.versions``.

        The fix's own PR ``#11`` resolves locally inside v1.2.0, so it links by PR to the fix commit, not to the cited
        feature PR ``#10`` of the earlier release.
        """
        entry = _by_text(refs_scenario.result)[_REFS_FIX_LATER]
        refs = entry["refs"]
        assert (refs["versions"], refs["prs"], entry["link_method"], entry["commits"]) == (
            ["1.1.0"],
            [10, 11],
            "pr",
            [refs_scenario.fix_sha],
        )


class TestReleaseRanges:
    """A release's commit range is what ``commit_releases`` credits to it on its own line, never ``tag
    ^predecessor``."""

    def test_skipping_predecessor_does_not_widen_range(self, evo_repo: RepoBuilder) -> None:
        """A predecessor that skips a release on the line leaves each heuristic fix in the release that shipped it.

        The index says v1.0.2's predecessor is v1.0.0 (ancestry can skip an off-branch v1.0.1), while
        ``commit_releases`` credits ``fix: alpha`` to v1.0.1. A ``tag ^predecessor`` range would also put alpha in
        v1.0.2 and record v1.0.2 as a secondary release.
        """
        repo = evo_repo
        repo.write("src/a.py", "a = 0\n")
        repo.commit("Initial import", day=1)
        repo.tag("v1.0.0", day=1)
        repo.write("src/a.py", "a = 1\n")
        alpha = repo.commit("fix: alpha", day=2)
        repo.tag("v1.0.1", day=2)
        repo.write("src/a.py", "a = 2\n")
        beta = repo.commit("fix: beta", day=3)
        repo.tag("v1.0.2", day=3)
        releases = [("v1.0.0", "main", None), ("v1.0.1", "main", "1.0.0"), ("v1.0.2", "main", "1.0.1")]
        skipping = {"1.0.0": None, "1.0.1": "1.0.0", "1.0.2": "1.0.0"}
        result = build_entries(repo.path, _index(repo, releases, predecessor=skipping), gh_available=False)
        assert _placement(result["fixes"]) == [("1.0.1", [], [alpha]), ("1.0.2", [], [beta])]

    @pytest.mark.integration
    def test_off_branch_release_with_real_index(self, evo_repo: RepoBuilder) -> None:
        """With the real release index, an off-branch fix and its squash-merged copy fold into the off-branch release.

        v1.3.6 is tagged on a release branch that is squash-merged into main before v1.3.7, so v1.3.7's predecessor is
        v1.3.5 while the line runs 1.3.5, 1.3.6, 1.3.7. Both copies share one patch-id, hence one heuristic row in
        v1.3.6 with v1.3.7 as a secondary release; ``fix: later fix`` stays a v1.3.7 row.
        """
        repo = evo_repo
        repo.write("src/a.py", "x = 1\n")
        repo.commit("Initial import", day=1)
        repo.tag("v1.3.5", day=1)
        repo.branch("rel-1.3.6")
        repo.checkout("rel-1.3.6")
        repo.write("src/b.py", "y = 2\n")
        side = repo.commit("fix: side fix", day=2)
        repo.tag("v1.3.6", day=3)
        repo.checkout("main")
        squash = repo.squash_merge("rel-1.3.6", "fix: side fix (#3)", day=4)
        repo.write("src/c.py", "z = 3\n")
        later = repo.commit("fix: later fix", day=5)
        repo.tag("v1.3.7", day=6)
        result = build_entries(repo.path, build_release_index(repo.path), gh_available=False)
        assert _placement(result["fixes"]) == [("1.3.6", ["1.3.7"], sorted([side, squash])), ("1.3.7", [], [later])]


class TestReleaseBounds:
    """An entry never links to a commit first shipped after the entry's own release."""

    def test_pr_shipped_later_is_not_linked(self, refs_scenario: _RefsScenario) -> None:
        """A v1.1.0 entry citing ``#12`` stays unlinked although the v1.2.0 commit ``Add exporter (#12)`` exists.

        Out-of-range PR commits are accepted only from releases at or before the entry's release; otherwise the two
        entries citing ``#12`` would share a patch-id set and fold into one fake backport.
        """
        entries = _by_text(refs_scenario.result)
        assert _links(refs_scenario.result)[_REFS_FIX_EARLY] == ("unlinked", [])
        assert entries[_REFS_FIX_SAME]["commits"] == [refs_scenario.exporter_sha]
        assert refs_scenario.result["aliases"] == {}


class TestTowncrierFragments:
    """Towncrier fragments become unreleased entries when the changelog has no unreleased section."""

    def test_bugfix_fragment_is_unreleased_fix(self, fragments_result: tuple[str, dict[str, Any]]) -> None:
        """A ``21.bugfix.md`` fragment yields an unreleased fix citing ``#21``, linked to the local ``(#21)`` commit.

        No changelog exists, so v1.0.0 is heuristic (its unprefixed commit yields nothing) and a warning says so.
        """
        sha, result = fragments_result
        assert _summary(result) == [
            (
                entry_ids("unreleased", ["Loader returned a stale value. (#21)"])[0],
                "fix",
                "unreleased",
                "changelog",
                "pr",
                1.0,
                [sha],
            )
        ]
        assert _warning_heads(result) == ["gh unavailable", "no changelog file found"]
