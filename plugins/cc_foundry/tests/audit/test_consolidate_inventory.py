"""Tests for ``consolidate_inventory.py`` — the deterministic half of ``/foundry:audit --consolidate``."""

from __future__ import annotations

import hashlib
import io
import itertools
import json
from pathlib import Path

import consolidate_inventory as ci
import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]


def _write(root: Path, rel: str, text: str) -> None:
    """Write ``text`` to ``root/rel``, creating parent directories."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a miniature plugin tree covering every load class, shared copy, sibling marker and citation."""
    root = tmp_path / "repo"
    monkeypatch.setattr(ci, "MANIFEST", [{"canonical": "plugins/cc_a/rules/shared.md", "copies": []}])
    _write(root, "plugins/cc_a/CLAUDE.src.md", "# Top\n\nAlways read.\n\n# Second\n\nMore.\n")
    _write(root, "plugins/cc_a/rules/_full/CLAUDE-full.md", "## Long\n\nfull claude body\n")
    _write(root, "plugins/cc_a/rules/always.md", "## One\n\nrule one\n\n## Two\n\nrule two\n")
    _write(root, "plugins/cc_a/rules/cited.md", "## Cited Rule\n\nanchor target\n\n## Other Rule\n\nnot cited\n")
    _write(root, "plugins/cc_a/rules/scoped.md", "---\ndescription: d\npaths:\n  - '**/*.py'\n---\n\n## S\n\npy rule\n")
    _write(
        root,
        "plugins/cc_a/rules/everywhere.md",
        "---\ndescription: d\npaths:\n  - '**'\n---\n\n## E\n\nall files\n",
    )
    _write(root, "plugins/cc_a/rules/shared.md", "## Shared\n\nbyte identical\n")
    _write(root, "plugins/cc_a/rules/paired.md", "## Stub\n\nsee full\n")
    _write(root, "plugins/cc_a/rules/_full/paired.md", "## Full\n\nlong body\n")
    _write(root, "plugins/cc_a/rules/quality-gates.md", "## Gate Full\n\nfull variant\n")
    _write(root, "plugins/cc_a/rules/quality-gates-delta.md", "## Gate Delta\n\ndelta variant\n")
    _write(
        root,
        "plugins/cc_a/rules/linked.md",
        "## Resolved\n\ntext <!-- policy-sibling: plugins/cc_a/rules/always.md -->\n\n"
        "## Dangling\n\ntext <!-- policy-sibling: plugins/cc_a/rules/missing.md -->\n\n## Free\n\nno marker\n",
    )
    _write(root, "plugins/cc_a/agents/helper.md", "---\nname: helper\n---\n\n## Role\n\nhelps\n")
    _write(root, "plugins/cc_a/agents/worker.md", "## Duty\n\nworks\n")
    _write(
        root,
        "plugins/cc_a/skills/demo/SKILL.md",
        "---\nname: demo\ndescription: routes\n---\n\n## Steps\n\nrun modes/extra.md and cited.md §Cited Rule\n",
    )
    _write(root, "plugins/cc_a/skills/demo/modes/extra.md", "## Extra\n\nmode body\n")
    _write(root, "plugins/cc_a/skills/other/SKILL.md", "## Steps\n\nanother skill\n")
    _write(root, "plugins/cc_b/rules/peer.md", "## Peer\n\nsibling plugin rule\n")
    return root


def _by_file(inventory: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    """Group inventory items by file."""
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in inventory["items"]:  # type: ignore[attr-defined]
        grouped.setdefault(item["file"], []).append(item)
    return grouped


def _id(inventory: dict[str, object], file: str, section: str) -> str:
    """Return the id of the section ``section`` in ``file``."""
    for item in inventory["items"]:  # type: ignore[attr-defined]
        if item["file"] == file and item["section"] == section:
            return item["id"]
    raise AssertionError(f"{file} {section} missing")


def _vet(inventory: dict[str, object], ids: list[str], floor: int = 0) -> dict[str, object]:
    """Validate one cluster over ``ids`` and return its record."""
    return ci.validate(inventory, [{"id": "c", "items": ids}], floor)["clusters"][0]  # type: ignore[index]


class TestParsePaths:
    """Covers the frontmatter ``paths:`` reader."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("---\npaths:\n  - '**/*.py'\n  - docs/**\n---\nbody", ["**/*.py", "docs/**"], id="block-list"),
            pytest.param("---\npaths: ['a/**', 'b/**']\n---\nbody", ["a/**", "b/**"], id="inline-list"),
            pytest.param("---\ndescription: x\n---\nbody", [], id="no-paths"),
            pytest.param("no frontmatter at all", [], id="no-frontmatter"),
            pytest.param("---\npaths:\n  - a\ndescription: after\n---\nbody", ["a"], id="paths-then-key"),
            pytest.param('---\npaths:\n  ["src/**"]\n---\nbody', ["src/**"], id="flow-list-on-its-own-line"),
            pytest.param(
                '---\npaths: [\n  "a/**",\n  "b/**"\n]\n---\nbody', ["a/**", "b/**"], id="multi-line-flow-list"
            ),
            pytest.param("---\npaths: docs/**\n---\nbody", ["docs/**"], id="bare-scalar"),
            pytest.param("---\npaths:\n---\nbody", [ci.UNPARSED_PATHS], id="empty-value-fails-closed"),
            pytest.param("---\npaths: [a/**]  # why\n---\nbody", ["a/**"], id="trailing-comment"),
        ],
    )
    def test_extracts_globs(self, text: str, expected: list[str]) -> None:
        """Block lists, inline lists and absent keys are all read without error."""
        assert ci.parse_paths(text) == expected


class TestSplitSections:
    """Covers heading splitting."""

    def test_heading_inside_code_fence_is_not_a_section(self) -> None:
        """A ``#`` line inside a fenced block stays in the surrounding section."""
        text = "## A\n\n```bash\n# comment\n```\n\n## B\n\nend\n"
        titles = [s[0] for s in ci.split_sections(text)]
        assert titles == ["A", "B"]

    def test_frontmatter_is_its_own_section(self) -> None:
        """Frontmatter is a section of its own, so only it is blocked and the intro after it stays mergeable."""
        text = "---\ndescription: d\n---\n\nintro text\n\n## A\n\nbody\n"
        sections = ci.split_sections(text)
        assert [(s[0], s[1], s[2]) for s in sections] == [("(frontmatter)", 1, 3), ("(preamble)", 4, 6), ("A", 7, 9)]

    def test_unclosed_frontmatter_swallows_the_rest(self) -> None:
        """An opening ``---`` that never closes keeps the whole file in the blocked frontmatter section."""
        sections = ci.split_sections("---\ndescription: d\n\n## A\n\nbody\n")
        assert [s[0] for s in sections] == ["(frontmatter)"]

    def test_line_ranges_are_one_based_and_contiguous(self) -> None:
        """Each section's range starts where the previous one ends."""
        sections = ci.split_sections("## A\n\nx\n\n## B\n\ny\n")
        assert sections[0][1] == 1
        assert sections[1][1] == sections[0][2] + 1

    def test_four_backtick_fence_holds_three_backtick_example(self) -> None:
        """A shorter fence inside a longer one does not close it, so example headings stay inside the section.

        A skill that shows a markdown template inside a four-backtick block would otherwise split at the template's own
        headings and hand fake sections to the clustering step.
        """
        text = "## A\n\n````markdown\n```bash\nx\n```\n## Example heading\n````\n\n## B\n\nend\n"
        assert [s[0] for s in ci.split_sections(text)] == ["A", "B"]

    def test_tilde_fence_is_not_closed_by_backticks(self) -> None:
        """A tilde fence closes only on tildes, so a backtick line inside it does not end the block."""
        text = "## A\n\n~~~\n```\n## Fake\n~~~\n\n## B\n\nend\n"
        assert [s[0] for s in ci.split_sections(text)] == ["A", "B"]


class TestInventory:
    """Covers load classification and dependency resolution."""

    def test_load_classes(self, corpus: Path) -> None:
        """Every instruction kind lands in its documented hierarchy with its trigger."""
        grouped = _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))
        hierarchy = {f: items[0]["load"]["hierarchy"] for f, items in grouped.items()}
        assert hierarchy["plugins/cc_a/CLAUDE.src.md"] == "always"
        assert hierarchy["plugins/cc_a/rules/always.md"] == "always"
        assert hierarchy["plugins/cc_a/rules/scoped.md"] == "scoped"
        assert hierarchy["plugins/cc_a/rules/_full/paired.md"] == "lazy"
        assert hierarchy["plugins/cc_a/agents/helper.md"] == "lazy"
        assert hierarchy["plugins/cc_a/skills/demo/SKILL.md"] == "lazy"
        assert hierarchy["plugins/cc_a/skills/demo/modes/extra.md"] == "lazy"

    def test_match_everything_glob_counts_as_always_loaded(self, corpus: Path) -> None:
        """A rule scoped only to ``**`` loads like an unscoped rule, so it clusters with always-loaded ones."""
        inventory = ci.build_inventory(corpus, ci.ALL_KINDS)
        ids = [
            _id(inventory, "plugins/cc_a/rules/everywhere.md", "E"),
            _id(inventory, "plugins/cc_a/rules/always.md", "One"),
        ]
        assert _vet(inventory, ids)["mixed_scopes"] == []

    def test_scoped_rule_keeps_exact_globs(self, corpus: Path) -> None:
        """A path-scoped rule records its exact globs so scope can be compared."""
        grouped = _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))
        assert grouped["plugins/cc_a/rules/scoped.md"][-1]["load"]["paths"] == ["**/*.py"]

    def test_stub_and_full_are_paired(self, corpus: Path) -> None:
        """The stub names its ``_full`` side and the full side names its stub."""
        grouped = _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))
        assert grouped["plugins/cc_a/rules/paired.md"][0]["load"]["pair"] == "rules/_full/paired.md"
        assert grouped["plugins/cc_a/rules/_full/paired.md"][0]["load"]["pair"] == "rules/paired.md"

    def test_unpaired_rule_has_no_side(self, corpus: Path) -> None:
        """A rule without a ``_full`` body carries no pair, so a missing counterpart is never invented."""
        load = _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))["plugins/cc_a/rules/always.md"][0]["load"]
        assert (load["side"], load["pair"]) == ("", "")

    def test_claude_md_pairs_with_its_differently_named_full_body(self, corpus: Path) -> None:
        """``CLAUDE.src.md`` pairs with ``_full/CLAUDE-full.md`` although the names differ."""
        grouped = _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))
        assert grouped["plugins/cc_a/CLAUDE.src.md"][0]["load"]["pair"] == "rules/_full/CLAUDE-full.md"
        assert grouped["plugins/cc_a/rules/_full/CLAUDE-full.md"][0]["load"]["pair"] == "CLAUDE.src.md"

    def test_manifest_member_is_blocked(self, corpus: Path) -> None:
        """A file in the shared MANIFEST is blocked in every section."""
        grouped = _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))
        assert all("shared-file" in item["blocked"][0] for item in grouped["plugins/cc_a/rules/shared.md"])

    def test_resolved_sibling_blocks_only_its_section(self, corpus: Path) -> None:
        """A marker blocks the section holding it; sections without a marker stay free."""
        items = {
            i["section"]: i for i in _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))["plugins/cc_a/rules/linked.md"]
        }
        assert any(b.startswith("policy-sibling:") for b in items["Resolved"]["blocked"])
        assert not any("unresolved" in b for b in items["Resolved"]["blocked"])
        assert items["Free"]["blocked"] == []

    def test_dangling_sibling_is_flagged_unresolved(self, corpus: Path) -> None:
        """A marker whose target is missing records the missing path."""
        items = {
            i["section"]: i for i in _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))["plugins/cc_a/rules/linked.md"]
        }
        assert any("unresolved" in b and "missing.md" in b for b in items["Dangling"]["blocked"])

    @pytest.mark.parametrize(
        ("rel", "kind", "hierarchy"),
        [
            pytest.param("skills/rules/SKILL.md", "skill", "lazy", id="skill-directory-named-rules"),
            pytest.param("skills/agents/modes/m.md", "skill-fragment", "lazy", id="skill-directory-named-agents"),
            pytest.param("agents/rules.md", "agent", "lazy", id="agent-file-named-rules"),
            pytest.param("skills/s/rules/x.md", "skill-fragment", "lazy", id="rules-folder-inside-a-skill"),
            pytest.param("rules/skills.md", "rule", "always", id="rule-named-skills"),
            pytest.param("skills/s/CLAUDE.src.md", "skill-fragment", "lazy", id="claude-src-name-inside-a-skill"),
        ],
    )
    def test_kind_comes_from_the_top_level_directory(self, rel: str, kind: str, hierarchy: str) -> None:
        """A skill or agent is never taken for a rule because a deeper directory or file is called ``rules``.

        Testing every path component let ``skills/rules/SKILL.md`` classify as an always-loaded rule, which made a skill
        mergeable with real rules and broke the decision that skills and agents are only sharpened or kept.
        """
        loaded_kind, load = ci.classify(rel, "## H\n\nbody\n")
        assert (loaded_kind, load.hierarchy) == (kind, hierarchy)

    def test_scoped_groups_distinguish_a_comma_inside_a_glob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``paths`` of ``['a,b']`` (one glob) and ``['a', 'b']`` (two) do not share a group; glob order does not
        matter."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        head = "---\ndescription: d\npaths:\n"
        _write(tmp_path, "plugins/cc_a/rules/one.md", head + "  - 'a,b'\n---\n\n## One\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/rules/two.md", head + "  - 'a'\n  - 'b'\n---\n\n## Two\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/rules/three.md", head + "  - 'b'\n  - 'a'\n---\n\n## Three\n\nbody\n")
        groups = {
            i["section"]: i["group"]
            for i in ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
            if i["section"] in ("One", "Two", "Three")
        }  # type: ignore[attr-defined]
        assert groups["One"] != groups["Two"]
        assert groups["Two"] == groups["Three"]

    def test_inline_paths_list_keeps_a_comma_inside_quotes(self) -> None:
        """An inline ``paths: ['a,b', 'c']`` is two globs, not three."""
        assert ci.parse_paths("---\npaths: ['a,b', 'c']\n---\n") == ["a,b", "c"]

    def test_frontmatter_section_is_blocked(self, corpus: Path) -> None:
        """A section holding YAML frontmatter is blocked: it carries routing or scope metadata, not mergeable prose."""
        grouped = _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))
        front = grouped["plugins/cc_a/skills/demo/SKILL.md"][0]
        assert front["section"] == "(frontmatter)"
        assert any(b.startswith("frontmatter") for b in front["blocked"])

    def test_text_after_frontmatter_is_not_blocked_for_it(self, tmp_path: Path) -> None:
        """Only the frontmatter lines are blocked: an intro between them and the first heading stays a free section."""
        _write(
            tmp_path, "plugins/cc_a/skills/demo/SKILL.md", "---\nname: demo\n---\n\nintro prose\n\n## Steps\n\nrun\n"
        )
        grouped = _by_file(ci.build_inventory(tmp_path, ci.ALL_KINDS))["plugins/cc_a/skills/demo/SKILL.md"]
        assert {i["section"]: i["blocked"] == [] for i in grouped} == {
            "(frontmatter)": False,
            "(preamble)": True,
            "Steps": True,
        }

    def test_skill_documents_are_examined_by_directory_and_docs_are_listed_as_excluded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every Markdown a skill can load is inventoried wherever it sits; the skill's own docs are reported skipped.

        A glob over ``modes/`` and ``templates/`` missed a root-level ``protocol.md`` and a ``guidelines/`` file that a
        skill loads, so they never reached clustering or the not-examined accounting.
        """
        monkeypatch.setattr(ci, "MANIFEST", [])
        for rel in ("SKILL.md", "protocol.md", "guidelines/writing.md", "modes/m.md", "ARCH.md", "README.md"):
            _write(tmp_path, f"plugins/cc_a/skills/s/{rel}", "## H\n\nbody\n")
        inventory = ci.build_inventory(tmp_path, ci.ALL_KINDS)
        files = set(_by_file(inventory))
        assert {
            f"plugins/cc_a/skills/s/{n}" for n in ("SKILL.md", "protocol.md", "guidelines/writing.md", "modes/m.md")
        } <= files
        assert not files & {"plugins/cc_a/skills/s/ARCH.md", "plugins/cc_a/skills/s/README.md"}
        assert inventory["excluded"] == ["plugins/cc_a/skills/s/ARCH.md", "plugins/cc_a/skills/s/README.md"]

    def test_citation_digest_changes_when_any_plugin_markdown_changes(self, corpus: Path) -> None:
        """A README outside the inventory can add a citation, so the digest of citation inputs must move with it."""
        before = ci.build_inventory(corpus, ci.ALL_KINDS)
        _write(corpus, "plugins/cc_a/README.md", "see cited.md §Cited Rule\n")
        after = ci.build_inventory(corpus, ci.ALL_KINDS)
        assert after["digests"] == before["digests"]
        assert after["citation_digest"] != before["citation_digest"]

    def test_flow_list_scopes_on_their_own_line_are_distinct_groups(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two rules scoped by differently written flow lists land in different scoped groups, never in ``always``."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/src.md", '---\npaths:\n  ["src/**"]\n---\n\n## Src\n\nbody\n')
        _write(tmp_path, "plugins/cc_a/rules/docs.md", '---\npaths:\n  ["docs/**"]\n---\n\n## Docs\n\nbody\n')
        groups = {
            i["section"]: i["group"]
            for i in ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
            if i["section"] in ("Src", "Docs")
        }  # type: ignore[attr-defined]
        assert groups["Src"] != groups["Docs"]
        assert all(g.startswith("scoped:") for g in groups.values())

    def test_citation_to_a_level_five_heading_protects_its_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A citation of ``§CI`` where ``CI`` is a level-5 heading is attributed to the section that contains it."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/target.md", "## Parent\n\ntext\n\n##### CI\n\nchild\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Step\n\nsee target.md §CI\n")
        items = {i["section"]: i for i in ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]}  # type: ignore[attr-defined]
        assert items["Parent"]["inbound"] == ["plugins/cc_a/skills/s/SKILL.md"]

    def test_cited_section_records_its_citing_file(self, corpus: Path) -> None:
        """A section named as ``<file> §<Heading>`` elsewhere lists the citing file; an uncited sibling lists none."""
        items = {
            i["section"]: i for i in _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))["plugins/cc_a/rules/cited.md"]
        }
        assert items["Cited Rule"]["inbound"] == ["plugins/cc_a/skills/demo/SKILL.md"]
        assert items["Other Rule"]["inbound"] == []

    def test_install_alternatives_stand_in_separate_groups(self, corpus: Path) -> None:
        """A rule and its ``-delta`` variant are delivered one or the other, so they never share a merge group."""
        grouped = _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))
        full = grouped["plugins/cc_a/rules/quality-gates.md"][0]["group"]
        delta = grouped["plugins/cc_a/rules/quality-gates-delta.md"][0]["group"]
        assert full != delta
        assert full != grouped["plugins/cc_a/rules/always.md"][0]["group"]

    def test_claude_md_has_its_own_group(self, corpus: Path) -> None:
        """``CLAUDE.src.md`` is copied and user-edited while rules are symlinked, so it never merges into a rule."""
        grouped = _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))
        assert grouped["plugins/cc_a/CLAUDE.src.md"][0]["group"] != grouped["plugins/cc_a/rules/always.md"][0]["group"]

    def test_lazy_group_spans_every_file_of_one_skill(self, corpus: Path) -> None:
        """A skill's entrypoint and its mode file share one lazy group; another skill and the agents do not."""
        grouped = _by_file(ci.build_inventory(corpus, ci.ALL_KINDS))
        demo = grouped["plugins/cc_a/skills/demo/SKILL.md"][0]["group"]
        assert grouped["plugins/cc_a/skills/demo/modes/extra.md"][0]["group"] == demo
        assert grouped["plugins/cc_a/skills/other/SKILL.md"][0]["group"] != demo
        assert (
            grouped["plugins/cc_a/agents/helper.md"][0]["group"] == grouped["plugins/cc_a/agents/worker.md"][0]["group"]
        )

    def test_kinds_filter_limits_corpus(self, corpus: Path) -> None:
        """Requesting only agents inventories no rule or skill files."""
        files = set(_by_file(ci.build_inventory(corpus, ("agents",))))
        assert files == {"plugins/cc_a/agents/helper.md", "plugins/cc_a/agents/worker.md"}

    def test_inventory_records_a_digest_per_examined_file(self, corpus: Path) -> None:
        """Each file the inventory read has a sha256, so a later edit of a source or mode file is detectable.

        A run that re-reads files edited after its first phase otherwise cannot be told apart from one that examined
        exactly what is on disk; the recorded digests make the replay comparison a diff, not a guess.
        """
        digests = ci.build_inventory(corpus, ci.ALL_KINDS)["digests"]
        rel = "plugins/cc_a/rules/always.md"
        assert digests[rel] == hashlib.sha256((corpus / rel).read_bytes()).hexdigest()
        (corpus / rel).write_text("## One\n\nedited\n", encoding="utf-8")
        assert ci.build_inventory(corpus, ci.ALL_KINDS)["digests"][rel] != digests[rel]

    def test_run_never_modifies_sources(self, corpus: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
        """An inventory run leaves every source byte unchanged and writes only the requested output."""

        def digest() -> dict[str, str]:
            return {
                p.relative_to(corpus).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(corpus.rglob("*"))
                if p.is_file()
            }

        before = digest()
        out = tmp_path_factory.mktemp("out") / "inv.json"
        assert ci.main(["inventory", "--root", str(corpus), "--out", str(out)]) == 0
        assert digest() == before
        assert json.loads(out.read_text(encoding="utf-8"))["schema_version"] == ci.SCHEMA_VERSION


class TestValidate:
    """Covers cluster vetting."""

    @pytest.fixture
    def inventory(self, corpus: Path) -> dict[str, object]:
        """Return the mini-corpus inventory."""
        return ci.build_inventory(corpus, ci.ALL_KINDS)

    def test_cross_file_always_cluster_is_ok_and_may_merge(self, inventory: dict[str, object]) -> None:
        """Two unblocked always-loaded sections of different files form an acceptable, mergeable cluster."""
        ids = [
            _id(inventory, "plugins/cc_a/rules/always.md", "One"),
            _id(inventory, "plugins/cc_a/rules/cited.md", "Other Rule"),
        ]
        result = _vet(inventory, ids)
        assert result["ok"] is True
        assert result["merge_allowed"] is True
        assert result["always_loaded_tokens_before"] == result["tokens_before"] > 0

    def test_mergeable_cluster_that_cannot_save_enough_is_not_judged(self, corpus: Path) -> None:
        """A merge keeps its largest member, so a cluster whose rest is under half the floor is listed, not judged.

        The cluster holds enough tokens for the size floor, but one member is nearly all of it; merging the small one
        into it cannot repay a judge and its review.
        """
        _write(corpus, "plugins/cc_a/rules/big.md", "## Big\n\n" + "b" * 1500 + "\n")
        _write(corpus, "plugins/cc_a/rules/tiny.md", "## Tiny\n\nsmall\n")
        inventory = ci.build_inventory(corpus, ci.ALL_KINDS)
        ids = [_id(inventory, "plugins/cc_a/rules/big.md", "Big"), _id(inventory, "plugins/cc_a/rules/tiny.md", "Tiny")]
        small = ci.validate(inventory, [{"id": "c", "items": ids}], 400)["clusters"][0]  # type: ignore[index]
        assert (small["ok"], small["judge"], small["skip_reason"]) == (True, False, "below saving floor")
        assert small["max_saving"] < 200
        _write(corpus, "plugins/cc_a/rules/twin.md", "## Twin\n\n" + "t" * 1500 + "\n")
        twins = ci.build_inventory(corpus, ci.ALL_KINDS)
        pair = [_id(twins, "plugins/cc_a/rules/big.md", "Big"), _id(twins, "plugins/cc_a/rules/twin.md", "Twin")]
        assert ci.validate(twins, [{"id": "c", "items": pair}], 400)["clusters"][0]["judge"] is True  # type: ignore[index]

    def test_same_file_cluster_is_rejected(self, inventory: dict[str, object]) -> None:
        """Repetition inside one file belongs to Check 29, so a single-file cluster never reaches a judge."""
        ids = [_id(inventory, "plugins/cc_a/rules/always.md", s) for s in ("One", "Two")]
        result = _vet(inventory, ids)
        assert result["ok"] is False
        assert any("one file" in r for r in result["reasons"])

    def test_mixed_scope_is_rejected(self, inventory: dict[str, object]) -> None:
        """Merging an always-loaded section with a path-scoped one is flagged."""
        ids = [
            _id(inventory, "plugins/cc_a/rules/always.md", "One"),
            _id(inventory, "plugins/cc_a/rules/scoped.md", "S"),
        ]
        result = _vet(inventory, ids)
        assert result["ok"] is False
        assert len(result["mixed_scopes"]) == 2

    def test_cross_plugin_cluster_is_rejected(self, inventory: dict[str, object]) -> None:
        """Always-loaded sections of two plugins never merge: plugins must stay independently installable."""
        ids = [
            _id(inventory, "plugins/cc_a/rules/always.md", "One"),
            _id(inventory, "plugins/cc_b/rules/peer.md", "Peer"),
        ]
        result = _vet(inventory, ids)
        assert result["ok"] is False
        assert len(result["mixed_scopes"]) == 2

    def test_install_alternatives_are_rejected_together(self, inventory: dict[str, object]) -> None:
        """A merge across a rule and its delta variant would delete text from a copy no user loads alongside the
        other."""
        ids = [
            _id(inventory, "plugins/cc_a/rules/quality-gates.md", "Gate Full"),
            _id(inventory, "plugins/cc_a/rules/quality-gates-delta.md", "Gate Delta"),
        ]
        assert _vet(inventory, ids)["ok"] is False

    def test_claude_md_and_rule_are_rejected_together(self, inventory: dict[str, object]) -> None:
        """A rule obligation moved into the copied ``CLAUDE.md`` would vanish for users who edited their copy."""
        ids = [
            _id(inventory, "plugins/cc_a/CLAUDE.src.md", "Top"),
            _id(inventory, "plugins/cc_a/rules/always.md", "One"),
        ]
        assert _vet(inventory, ids)["ok"] is False

    def test_cross_skill_lazy_cluster_is_rejected(self, inventory: dict[str, object]) -> None:
        """Sections of two different skills load on different triggers, so merging them changes when they load."""
        ids = [
            _id(inventory, "plugins/cc_a/skills/demo/SKILL.md", "Steps"),
            _id(inventory, "plugins/cc_a/skills/other/SKILL.md", "Steps"),
        ]
        assert _vet(inventory, ids)["ok"] is False

    def test_skill_and_its_mode_file_cluster_but_never_merge(self, inventory: dict[str, object]) -> None:
        """A skill's files form one lazy group for overlap detection, but mode text must not move into the entrypoint.

        Merging a mode-only clause into the entrypoint would charge every other mode for it, so the cluster is valid
        only for sharpen or keep.
        """
        ids = [
            _id(inventory, "plugins/cc_a/skills/demo/SKILL.md", "Steps"),
            _id(inventory, "plugins/cc_a/skills/demo/modes/extra.md", "Extra"),
        ]
        result = _vet(inventory, ids)
        assert result["ok"] is True
        assert result["merge_allowed"] is False

    def test_agents_of_one_plugin_cluster_but_never_merge(self, inventory: dict[str, object]) -> None:
        """Two agent files of one plugin may be compared; neither may absorb the other."""
        ids = [
            _id(inventory, "plugins/cc_a/agents/helper.md", "Role"),
            _id(inventory, "plugins/cc_a/agents/worker.md", "Duty"),
        ]
        result = _vet(inventory, ids)
        assert (result["ok"], result["merge_allowed"]) == (True, False)

    def test_groups_rank_by_unblocked_tokens(self, inventory: dict[str, object]) -> None:
        """Groups are ordered by tokens no dependency blocks, so effort goes where a merge is possible."""
        counts = [g["unblocked_tokens"] for g in inventory["groups"]]  # type: ignore[attr-defined]
        assert counts == sorted(counts, reverse=True)
        assert sum(g["items"] for g in inventory["groups"]) == len(inventory["items"])  # type: ignore[attr-defined]

    def test_blocked_member_is_rejected(self, inventory: dict[str, object]) -> None:
        """A cluster holding a shared-file section is flagged with the reason."""
        ids = [
            _id(inventory, "plugins/cc_a/rules/always.md", "One"),
            _id(inventory, "plugins/cc_a/rules/shared.md", "Shared"),
        ]
        result = _vet(inventory, ids)
        assert result["ok"] is False
        assert list(result["blocked_members"]) == [ids[1]]

    def test_frontmatter_section_is_rejected(self, inventory: dict[str, object]) -> None:
        """A skill's frontmatter holds its routing description; clustering it with prose would put routing at risk."""
        ids = [
            _id(inventory, "plugins/cc_a/skills/demo/SKILL.md", "(frontmatter)"),
            _id(inventory, "plugins/cc_a/skills/demo/modes/extra.md", "Extra"),
        ]
        assert _vet(inventory, ids)["ok"] is False

    def test_stub_with_its_full_side_is_flagged_as_flattening(self, inventory: dict[str, object]) -> None:
        """Merging a stub into its own ``_full`` body is reported as pair flattening."""
        ids = [
            _id(inventory, "plugins/cc_a/rules/paired.md", "Stub"),
            _id(inventory, "plugins/cc_a/rules/_full/paired.md", "Full"),
        ]
        result = _vet(inventory, ids)
        assert result["pair_flattened"] == ["plugins/cc_a/rules/paired.md"]
        assert result["ok"] is False

    def test_unknown_member_is_rejected(self, inventory: dict[str, object]) -> None:
        """An id absent from the inventory is reported, not silently dropped."""
        result = _vet(inventory, ["nope#0", "nope#1"])
        assert result["unknown_members"] == ["nope#0", "nope#1"]
        assert result["ok"] is False

    @pytest.mark.parametrize(
        "items",
        [
            pytest.param([], id="empty"),
            pytest.param(["plugins/cc_a/rules/always.md#0"], id="single-member"),
            pytest.param(["plugins/cc_a/rules/always.md#0", "plugins/cc_a/rules/always.md#0"], id="repeated-member"),
        ],
    )
    def test_degenerate_member_lists_are_rejected(self, inventory: dict[str, object], items: list[str]) -> None:
        """An empty, one-member or repeated-member cluster cannot be a merge and must not inflate the token total."""
        assert _vet(inventory, items)["ok"] is False

    def test_repeated_member_counts_once(self, inventory: dict[str, object]) -> None:
        """A member listed twice contributes its tokens once."""
        one = _id(inventory, "plugins/cc_a/rules/always.md", "One")
        single = _vet(inventory, [one, _id(inventory, "plugins/cc_a/rules/cited.md", "Other Rule")])
        doubled = _vet(inventory, [one, one, _id(inventory, "plugins/cc_a/rules/cited.md", "Other Rule")])
        assert doubled["tokens_before"] == single["tokens_before"]

    def test_duplicate_cluster_id_and_overlap_are_rejected(self, inventory: dict[str, object]) -> None:
        """A second cluster reusing an id is rejected; of two clusters sharing a member only one is judged."""
        one = _id(inventory, "plugins/cc_a/rules/always.md", "One")
        other = _id(inventory, "plugins/cc_a/rules/cited.md", "Other Rule")
        cited = _id(inventory, "plugins/cc_a/rules/cited.md", "Cited Rule")
        two = _id(inventory, "plugins/cc_a/rules/always.md", "Two")
        report = ci.validate(
            inventory,
            [
                {"id": "a", "items": [one, other]},
                {"id": "A", "items": [two, other]},
                {"id": "b", "items": [one, cited]},
            ],
            0,
        )["clusters"]
        assert [c["ok"] for c in report][1] is False  # type: ignore[index]
        assert any("duplicate cluster id" in r for r in report[1]["reasons"])  # type: ignore[index]
        assert sum(1 for c in (report[0], report[2]) if c["ok"]) == 1  # type: ignore[index]
        assert any("already claimed" in r for c in (report[0], report[2]) for r in c["reasons"])  # type: ignore[index]

    def test_cluster_past_the_cap_does_not_block_an_overlapping_one(self, inventory: dict[str, object]) -> None:
        """A cluster that is not judged claims nothing, so it cannot push out a better cluster sharing its member."""
        one = _id(inventory, "plugins/cc_a/rules/always.md", "One")
        other = _id(inventory, "plugins/cc_a/rules/cited.md", "Other Rule")
        report = ci.validate(
            inventory, [{"id": "small", "items": [one, other]}, {"id": "big", "items": [one, other]}], 10_000
        )["clusters"]
        assert all(c["ok"] for c in report)  # type: ignore[index]
        assert not any(c["judge"] for c in report)  # type: ignore[index]

    def test_non_string_cluster_id_is_rejected(self, inventory: dict[str, object]) -> None:
        """A numeric or null id is not coerced into a file name."""
        ids = [
            _id(inventory, "plugins/cc_a/rules/always.md", "One"),
            _id(inventory, "plugins/cc_a/rules/cited.md", "Other Rule"),
        ]
        report = ci.validate(inventory, [{"id": 7, "items": ids}, {"id": None, "items": ids}], 0)["clusters"]
        assert not any(c["ok"] for c in report)  # type: ignore[index]

    @pytest.mark.parametrize(
        "bad_id",
        [pytest.param("../escape", id="traversal"), pytest.param("a b", id="space"), pytest.param("", id="empty")],
    )
    def test_unsafe_cluster_id_is_rejected(self, inventory: dict[str, object], bad_id: str) -> None:
        """A cluster id becomes a proposal file name, so one that could leave the proposals directory is refused."""
        ids = [
            _id(inventory, "plugins/cc_a/rules/always.md", "One"),
            _id(inventory, "plugins/cc_a/rules/cited.md", "Other Rule"),
        ]
        result = ci.validate(inventory, [{"id": bad_id, "items": ids}], 0)["clusters"][0]  # type: ignore[index]
        assert result["ok"] is False

    def test_cluster_below_the_token_floor_is_not_judged(self, inventory: dict[str, object]) -> None:
        """A cluster too small for a merge to repay a judge is valid but marked not to be judged."""
        ids = [
            _id(inventory, "plugins/cc_a/rules/always.md", "One"),
            _id(inventory, "plugins/cc_a/rules/cited.md", "Other Rule"),
        ]
        result = _vet(inventory, ids, floor=10_000)
        assert (result["ok"], result["judge"]) == (True, False)

    def test_judge_cap_keeps_mergeable_clusters_first(self, inventory: dict[str, object]) -> None:
        """Past the cap, lazy clusters lose their judge before a mergeable one does, and the skip is explained."""
        mergeable = [
            _id(inventory, "plugins/cc_a/rules/always.md", "One"),
            _id(inventory, "plugins/cc_a/rules/cited.md", "Other Rule"),
        ]
        lazy = [
            _id(inventory, "plugins/cc_a/agents/helper.md", "Role"),
            _id(inventory, "plugins/cc_a/agents/worker.md", "Duty"),
        ]
        report = ci.validate(inventory, [{"id": "lazy", "items": lazy}, {"id": "merge", "items": mergeable}], 0, 1)[
            "clusters"
        ]
        by_id = {c["id"]: c for c in report}  # type: ignore[index]
        assert (by_id["merge"]["judge"], by_id["lazy"]["judge"]) == (True, False)
        assert by_id["lazy"]["skip_reason"] == "judge cap"

    def test_cited_member_reports_its_inbound_references(self, inventory: dict[str, object]) -> None:
        """A cluster member that other files cite by heading carries those citing files in the verdict."""
        cited = _id(inventory, "plugins/cc_a/rules/cited.md", "Cited Rule")
        result = _vet(inventory, [cited, _id(inventory, "plugins/cc_a/rules/always.md", "One")])
        assert result["inbound"] == {cited: ["plugins/cc_a/skills/demo/SKILL.md"]}


class TestCitations:
    """Covers which mentions of ``<file> §<Heading>`` count as citations of a section."""

    @staticmethod
    def _inbound(root: Path, file: str, section: str) -> list[str]:
        """Return the inbound citing files of the section ``section`` in ``file``."""
        inventory = ci.build_inventory(root, ci.ALL_KINDS)
        item = next(i for i in inventory["items"] if i["file"] == file and i["section"] == section)  # type: ignore[attr-defined]
        return item["inbound"]

    @pytest.fixture
    def tree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Build two skills with a same-named entrypoint, a rule, and citations in several written forms."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/target.md", "## Spawn Discipline\n\nbody\n\n## Quiet Section\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/one/SKILL.md", "## Gate Step\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/two/SKILL.md", "## Gate Step\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/one/modes/m.md", "## Mode\n\nsee SKILL.md §Gate Step\n")
        _write(tmp_path, "plugins/cc_a/skills/two/modes/n.md", "## Mode\n\nnothing\n")
        _write(
            tmp_path,
            "plugins/cc_a/skills/two/notes.md",
            "`target.md` §Spawn Discipline and foundry-target.md §Spawn Discipline, also target.md §Quiet Section, §Spawn Discipline\n",
        )
        return tmp_path

    def test_backtick_quoted_file_name_is_a_citation(self, tree: Path) -> None:
        """A citation written ``file.md`` §Heading is matched, as is the delivered ``foundry-`` file name."""
        assert self._inbound(tree, "plugins/cc_a/rules/target.md", "Spawn Discipline") == [
            "plugins/cc_a/skills/two/notes.md"
        ]

    def test_second_heading_of_a_multi_section_citation_counts(self, tree: Path) -> None:
        """``file.md §A, §B`` cites both A and B."""
        assert self._inbound(tree, "plugins/cc_a/rules/target.md", "Quiet Section") == [
            "plugins/cc_a/skills/two/notes.md"
        ]

    def test_shared_entrypoint_name_counts_only_inside_its_own_skill(self, tree: Path) -> None:
        """``SKILL.md §Gate Step`` from skill one cites skill one's entrypoint, never skill two's."""
        assert self._inbound(tree, "plugins/cc_a/skills/one/SKILL.md", "Gate Step") == [
            "plugins/cc_a/skills/one/modes/m.md"
        ]
        assert self._inbound(tree, "plugins/cc_a/skills/two/SKILL.md", "Gate Step") == []

    def test_delivered_name_resolves_to_the_plugin_it_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``a-gate.md §Heading`` cites plugin a's ``gate.md`` and never plugin b's, wherever it is written.

        The setup skill delivers a plugin's rules as ``<plugin>-<name>.md``. Stripping the prefix alone loses which
        plugin was meant, so a rule of the same name in the citing plugin would be mistaken for the target and the real
        target would look uncited.
        """
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/gate.md", "## Shared Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_b/rules/gate.md", "## Shared Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_b/skills/s/SKILL.md", "## Step\n\ncites `a-gate.md` §Shared Heading\n")
        assert self._inbound(tmp_path, "plugins/cc_a/rules/gate.md", "Shared Heading") == [
            "plugins/cc_b/skills/s/SKILL.md"
        ]
        assert self._inbound(tmp_path, "plugins/cc_b/rules/gate.md", "Shared Heading") == []

    def test_relative_path_is_resolved_against_the_citing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``../rules/target.md §Heading`` names the file two directories up from the citing file, not any
        ``target.md``."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/target.md", "## Wanted Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_b/rules/target.md", "## Wanted Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Step\n\nsee ../../rules/target.md §Wanted Heading\n")
        assert self._inbound(tmp_path, "plugins/cc_a/rules/target.md", "Wanted Heading") == [
            "plugins/cc_a/skills/s/SKILL.md"
        ]
        assert self._inbound(tmp_path, "plugins/cc_b/rules/target.md", "Wanted Heading") == []

    def test_plugin_prefixes_that_start_each_other_resolve_by_which_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With plugins ``a`` and ``a-b``, ``a-b-gate.md`` names ``a-b``'s ``gate.md`` because ``a`` has no
        ``b-gate.md``.

        Reading the prefix off the name alone cannot tell the two plugins apart; looking up which candidate files exist
        can, and it never credits a plugin that has no such file.
        """
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/gate.md", "## Shared Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_a-b/rules/gate.md", "## Shared Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_c/skills/s/SKILL.md", "## Step\n\ncites `a-b-gate.md` §Shared Heading\n")
        assert self._inbound(tmp_path, "plugins/cc_a-b/rules/gate.md", "Shared Heading") == [
            "plugins/cc_c/skills/s/SKILL.md"
        ]
        assert self._inbound(tmp_path, "plugins/cc_a/rules/gate.md", "Shared Heading") == []

    def test_a_file_really_named_with_a_plugin_prefix_wins_over_the_delivered_reading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``a-gate.md`` cited from a plugin that owns a file of exactly that name means that file, not a's
        ``gate.md``."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/gate.md", "## Shared Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_b/rules/a-gate.md", "## Shared Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_b/skills/s/SKILL.md", "## Step\n\ncites a-gate.md §Shared Heading\n")
        assert self._inbound(tmp_path, "plugins/cc_b/rules/a-gate.md", "Shared Heading") == [
            "plugins/cc_b/skills/s/SKILL.md"
        ]
        assert self._inbound(tmp_path, "plugins/cc_a/rules/gate.md", "Shared Heading") == []

    def test_dot_slash_path_means_the_citing_files_own_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``./target.md`` from a mode file names the neighbour in its directory, not a same-named rule elsewhere."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/target.md", "## Wanted Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/modes/target.md", "## Wanted Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/modes/m.md", "## Mode\n\nsee ./target.md §Wanted Heading\n")
        assert self._inbound(tmp_path, "plugins/cc_a/skills/s/modes/target.md", "Wanted Heading") == [
            "plugins/cc_a/skills/s/modes/m.md"
        ]
        assert self._inbound(tmp_path, "plugins/cc_a/rules/target.md", "Wanted Heading") == []

    def test_backslash_separators_keep_their_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Windows-style ``rules\\target.md`` is read as ``rules/target.md``, not collapsed to a bare file name."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/target.md", "## Wanted Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/modes/target.md", "## Wanted Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/t/SKILL.md", "## Step\n\nsee rules\\target.md §Wanted Heading\n")
        assert self._inbound(tmp_path, "plugins/cc_a/rules/target.md", "Wanted Heading") == [
            "plugins/cc_a/skills/t/SKILL.md"
        ]
        assert self._inbound(tmp_path, "plugins/cc_a/skills/s/modes/target.md", "Wanted Heading") == []

    def test_explicit_path_that_names_no_file_is_a_miss_not_a_delivered_guess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A written-out ``plugins/cc_b/rules/a-gate.md`` that does not exist credits nothing, not plugin a's
        ``gate.md``."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/gate.md", "## Shared Heading\n\nbody\n")
        _write(
            tmp_path, "plugins/cc_c/skills/s/SKILL.md", "## Step\n\nsee plugins/cc_b/rules/a-gate.md §Shared Heading\n"
        )
        assert self._inbound(tmp_path, "plugins/cc_a/rules/gate.md", "Shared Heading") == []

    def test_dot_slash_neighbour_that_exists_but_is_not_inventoried_credits_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``./target.md`` naming a real neighbour the inventory skips is not rerouted to a rule of the same name."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/target.md", "## Wanted Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/notes/target.md", "## Wanted Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Step\n\nsee ./notes/target.md §Wanted Heading\n")
        assert self._inbound(tmp_path, "plugins/cc_a/rules/target.md", "Wanted Heading") == []

    def test_two_letter_heading_matches_only_exactly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A short heading such as ``CI`` is cited by an equal title, never by a longer title that merely starts with
        it."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/target.md", "## CI\n\nbody\n\n## CI Setup Notes\n\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Step\n\nsee target.md §CI\n")
        assert self._inbound(tmp_path, "plugins/cc_a/rules/target.md", "CI") == ["plugins/cc_a/skills/s/SKILL.md"]
        assert self._inbound(tmp_path, "plugins/cc_a/rules/target.md", "CI Setup Notes") == []

    def test_ambiguous_bare_name_outside_the_citing_tree_credits_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare name found in two other plugins is not guessed onto either of them."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/target.md", "## Wanted Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_b/rules/target.md", "## Wanted Heading\n\nbody\n")
        _write(tmp_path, "plugins/cc_c/skills/s/SKILL.md", "## Step\n\nsee target.md §Wanted Heading\n")
        assert self._inbound(tmp_path, "plugins/cc_a/rules/target.md", "Wanted Heading") == []
        assert self._inbound(tmp_path, "plugins/cc_b/rules/target.md", "Wanted Heading") == []


class TestSelect:
    """Covers scope, floor, cap and batching of the group selection."""

    @pytest.fixture
    def inventory(self, corpus: Path) -> dict[str, object]:
        """Return the mini-corpus inventory."""
        return ci.build_inventory(corpus, ci.ALL_KINDS)

    @staticmethod
    def _groups(chosen: dict[str, object]) -> list[str]:
        """Return the selected group ids."""
        return [g["group"] for g in chosen["selected"]]  # type: ignore[attr-defined]

    def test_single_file_groups_are_left_out_with_a_reason(self, inventory: dict[str, object]) -> None:
        """A group made of one file cannot hold a cross-file cluster, so it is reported, not examined."""
        chosen = ci.select_groups(inventory, "", 0, 50, 4)
        reasons = {g["group"]: g["reason"] for g in chosen["not_examined"]}  # type: ignore[attr-defined]
        assert "single-file" in reasons["lazy:cc_a:skills/other"]
        assert "lazy:cc_a:skills/demo" in self._groups(chosen)

    def test_scope_tokens_are_a_union(self, inventory: dict[str, object]) -> None:
        """Kind, plugin, agent and skill tokens each add the groups they name, as in the audit skill's own grammar."""
        assert all(
            g.startswith("lazy:cc_a:skills/") for g in self._groups(ci.select_groups(inventory, "skills", 0, 50, 4))
        )
        assert self._groups(ci.select_groups(inventory, "demo", 0, 50, 4)) == ["lazy:cc_a:skills/demo"]
        assert "lazy:cc_a:agents" in self._groups(ci.select_groups(inventory, "helper demo", 0, 50, 4))
        assert "lazy:cc_a:skills/demo" in self._groups(ci.select_groups(inventory, "helper demo", 0, 50, 4))
        assert self._groups(ci.select_groups(inventory, "plugins", 0, 50, 4)) == self._groups(
            ci.select_groups(inventory, "", 0, 50, 4)
        )

    def test_unknown_scope_token_is_reported_and_blocks_the_cli(
        self, inventory: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A token that names nothing is listed, and the command exits 1 before any spend instead of widening the
        run."""
        assert ci.select_groups(inventory, "demo zzz", 0, 50, 4)["unmatched_tokens"] == ["zzz"]
        inv = tmp_path / "i.json"
        inv.write_text(json.dumps(inventory), encoding="utf-8")
        assert ci.main(["select", "--inventory", str(inv), "--scope", "zzz"]) == 1
        assert "BLOCKED" in capsys.readouterr().out

    def test_floor_and_cap_leave_groups_unexamined(self, inventory: dict[str, object]) -> None:
        """Groups under the token floor or past the cap are listed with the reason instead of vanishing."""
        capped = ci.select_groups(inventory, "a", 0, 1, 4)
        assert len(capped["selected"]) == 1  # type: ignore[arg-type]
        assert "group cap" in {g["reason"] for g in capped["not_examined"]}  # type: ignore[attr-defined]
        floored = ci.select_groups(inventory, "a", 10_000, 50, 4)
        assert floored["selected"] == []

    def test_mergeable_groups_are_examined_before_larger_lazy_ones(self, corpus: Path) -> None:
        """A small always-loaded group outranks a larger lazy one: only it can be merged, and it costs every session."""
        _write(corpus, "plugins/cc_a/skills/demo/modes/extra.md", "## Extra\n\n" + "x" * 3000 + "\n")
        inventory = ci.build_inventory(corpus, ci.ALL_KINDS)
        chosen = ci.select_groups(inventory, "a", 0, 50, 4)
        assert self._groups(chosen)[0].startswith("always:cc_a:")
        assert "lazy:cc_a:skills/demo" in self._groups(chosen)

    def test_batches_cover_every_selected_group_once(self, inventory: dict[str, object]) -> None:
        """Batches partition the selected groups, with no group repeated and none dropped."""
        chosen = ci.select_groups(inventory, "a", 0, 50, 2)
        flat = [g for batch in chosen["batches"] for g in batch]  # type: ignore[attr-defined]
        assert sorted(flat) == sorted(self._groups(chosen))
        assert len(chosen["batches"]) <= 2  # type: ignore[arg-type]


class TestSettle:
    """Covers the deterministic rules applied to judge decisions and reviews."""

    PROPOSAL = "## Alpha Rule\n\nshort merged text\n"

    @pytest.fixture
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        """Build a corpus with two long always-loaded rules, a skill with a mode file, and their vetted clusters."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        root = tmp_path / "repo"
        _write(root, "plugins/cc_a/rules/a.md", "## Alpha Rule\n\n" + "x" * 300 + "\n")
        _write(root, "plugins/cc_a/rules/b.md", "## Beta Rule\n\n" + "y" * 300 + "\n")
        _write(root, "plugins/cc_a/skills/demo/SKILL.md", "## Steps\n\n" + "s" * 300 + "\n\ncites a.md §Alpha Rule\n")
        _write(root, "plugins/cc_a/skills/demo/modes/m.md", "## Mode\n\n" + "m" * 300 + "\n")
        inventory = ci.build_inventory(root, ci.ALL_KINDS)
        ids = {
            "alpha": _id(inventory, "plugins/cc_a/rules/a.md", "Alpha Rule"),
            "beta": _id(inventory, "plugins/cc_a/rules/b.md", "Beta Rule"),
            "steps": _id(inventory, "plugins/cc_a/skills/demo/SKILL.md", "Steps"),
            "mode": _id(inventory, "plugins/cc_a/skills/demo/modes/m.md", "Mode"),
        }
        clusters = [
            {"id": "ab", "items": [ids["alpha"], ids["beta"]]},
            {"id": "sm", "items": [ids["steps"], ids["mode"]]},
        ]
        proposals = tmp_path / "proposals"
        proposals.mkdir()
        (proposals / "ab.md").write_text(self.PROPOSAL, encoding="utf-8")
        return {"vetted": ci.validate(inventory, clusters, 0), "ids": ids, "proposals": proposals}

    def _merge(self, env: dict[str, object], **overrides: object) -> dict[str, object]:
        """Return a merge-prune judgment for cluster ``ab`` that satisfies every rule, with ``overrides`` applied."""
        ids = env["ids"]
        obligations = [
            {"original": "o", "from": ids[key], "to": "short merged text", "modal_preserved": True}  # type: ignore[index]
            for key in ("alpha", "beta")
        ]
        judged: dict[str, object] = {
            "cluster": "ab",
            "decision": "merge-prune",
            "survivor": ids["alpha"],  # type: ignore[index]
            "repoint": [],
            "obligation_map": obligations,
        }
        return judged | overrides

    def _sharpen(self, env: dict[str, object], edits: object, cluster: str = "sm") -> dict[str, object]:
        """Return a sharpen judgment for the skill cluster with ``edits``."""
        judged: dict[str, object] = {"cluster": cluster, "decision": "sharpen", "reason": "tighten"}
        return judged | {"edits": edits}

    @staticmethod
    def _edit(action: str, file: str, **extra: object) -> dict[str, object]:
        """Return one edit of ``action`` in the skill ``file``."""
        return {"action": action, "file": file, "lines": "3", "before": "", "after": "text"} | extra

    def _run(
        self, env: dict[str, object], judged: dict[str, object], reviews: list[dict[str, object]] | None = None
    ) -> dict[str, object]:
        """Settle ``judged`` against the environment and return its record.

        A review is bound to the exact change it judged, so each given verdict is stamped with the digest of this
        judgment's change unless the test set one itself.
        """
        if reviews:
            first, _ = ci.settle(env["vetted"], [judged], env["proposals"], [])  # type: ignore[arg-type]
            reviews = [{"digest": first["clusters"][0]["digest"], **review} for review in reviews]  # type: ignore[index]
        document, _ = ci.settle(env["vetted"], [judged], env["proposals"], reviews or [])  # type: ignore[arg-type]
        return document["clusters"][0]  # type: ignore[index]

    @staticmethod
    def _cite(
        vetted: dict[str, object], cluster_id: str, member: str, title: str, refs: list[str], line: int = 1
    ) -> None:
        """Make ``refs`` cite the heading ``title`` at ``line`` of ``member`` in a cluster of ``vetted``."""
        cluster = next(c for c in vetted["clusters"] if c["id"] == cluster_id)  # type: ignore[attr-defined]
        file = next(loc["file"] for loc in cluster["locations"] if loc["id"] == member)
        cluster["inbound"] = {member: refs}
        cluster["guarded"] = [{"id": member, "file": file, "title": title, "line": line, "refs": refs}]
        cluster["file_guarded"] = cluster["guarded"]

    SKILL = "plugins/cc_a/skills/demo/SKILL.md"
    MODE = "plugins/cc_a/skills/demo/modes/m.md"

    def test_valid_merge_awaits_review_and_writes_obligations(self, env: dict[str, object]) -> None:
        """A rule-compliant merge stays merge-prune, is priced, and gets its obligation map written beside it."""
        record = self._run(env, self._merge(env))
        assert (record["decision"], record["validation"]) == ("merge-prune", "unvalidated")
        assert record["savings"] > 0
        assert json.loads((env["proposals"] / "ab.obligations.json").read_text(encoding="utf-8"))  # type: ignore[operator]

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            pytest.param({"obligation_map": []}, "no obligation map", id="empty-map"),
            pytest.param(
                {
                    "obligation_map": [
                        {"original": "o", "from": "x", "to": "short merged text", "modal_preserved": False}
                    ]
                },
                "modal",
                id="modal-dropped",
            ),
            pytest.param({"obligation_map": [{"modal_preserved": True}]}, "lacks original", id="map-without-fields"),
            pytest.param(
                {"obligation_map": [{"original": "o", "from": "x", "to": "absent words", "modal_preserved": True}]},
                "not in the proposal",
                id="wording-not-in-proposal",
            ),
            pytest.param(
                {
                    "obligation_map": [
                        {"original": "o", "from": "x", "to": "short merged text", "modal_preserved": True}
                    ]
                },
                "no obligation recorded",
                id="member-uncovered",
            ),
            pytest.param({"survivor": "elsewhere#0"}, "survivor", id="survivor-outside"),
        ],
    )
    def test_merge_breaking_a_rule_is_downgraded(
        self, env: dict[str, object], overrides: dict[str, object], fragment: str
    ) -> None:
        """A merge lacking a complete obligation map or a valid survivor falls back to unreviewable advice."""
        record = self._run(env, self._merge(env, **overrides))
        assert (record["decision"], record["advice_only"]) == ("sharpen", True)
        assert any(fragment in d for d in record["downgrades"])

    def test_missing_or_longer_proposal_is_downgraded(self, env: dict[str, object]) -> None:
        """No proposal file, or one that is not shorter than its members, cannot be a merge."""
        (env["proposals"] / "ab.md").unlink()  # type: ignore[operator]
        assert any("missing" in d for d in self._run(env, self._merge(env))["downgrades"])  # type: ignore[attr-defined]
        (env["proposals"] / "ab.md").write_text("short merged text " + "z" * 3000, encoding="utf-8")  # type: ignore[operator]
        assert any("not shorter" in d for d in self._run(env, self._merge(env))["downgrades"])  # type: ignore[attr-defined]

    def test_lazy_group_merge_is_downgraded(self, env: dict[str, object]) -> None:
        """A skill's entrypoint and mode file may be sharpened together but never merged."""
        (env["proposals"] / "sm.md").write_text(self.PROPOSAL, encoding="utf-8")  # type: ignore[operator]
        judged = self._merge(env, cluster="sm", survivor=env["ids"]["steps"])  # type: ignore[index]
        record = self._run(env, judged)
        assert record["decision"] == "sharpen"
        assert any("not allowed" in d for d in record["downgrades"])

    def test_colon_suffix_rename_of_the_cited_survivor_heading_needs_a_repoint(self, env: dict[str, object]) -> None:
        """``Alpha Rule: old`` renamed ``Alpha Rule: new`` is a different heading, so the citation is stranded.

        Heading matching for citations deliberately drops a title's tail; deciding whether the heading survived must
        compare the whole title, or a rename that only changes the suffix passes unnoticed.
        """
        vetted = json.loads(json.dumps(env["vetted"]))
        cluster = next(c for c in vetted["clusters"] if c["id"] == "ab")
        for guard in cluster["guarded"]:
            if guard["id"] == env["ids"]["alpha"]:  # type: ignore[index]
                guard["title"] = "Alpha Rule: old"
        (env["proposals"] / "ab.md").write_text("## Alpha Rule: new\n\nshort merged text\n", encoding="utf-8")  # type: ignore[operator]
        document, _ = ci.settle(vetted, [self._merge(env)], env["proposals"], [])  # type: ignore[arg-type]
        assert any("renamed" in d for d in document["clusters"][0]["downgrades"])  # type: ignore[index]

    def test_rewrite_that_keeps_the_cited_heading_needs_no_repoint(self, env: dict[str, object]) -> None:
        """A rewrite over the section's lines whose new text still opens with the same heading keeps the citation."""
        vetted = json.loads(json.dumps(env["vetted"]))
        self._cite(vetted, "sm", env["ids"]["steps"], "Steps", ["plugins/cc_a/other.md"])  # type: ignore[index]
        edit = {"action": "rewrite", "file": self.SKILL, "lines": "1-5", "before": "x", "after": "## Steps\n\nnew body"}
        document, _ = ci.settle(vetted, [self._sharpen(env, [edit])], env["proposals"], [])  # type: ignore[arg-type]
        assert document["clusters"][0]["decision"] == "sharpen"  # type: ignore[index]

    def test_renaming_the_cited_survivor_heading_needs_a_repoint(self, env: dict[str, object]) -> None:
        """A merge whose survivor is cited under ``Alpha Rule`` but whose proposal calls it ``Merged`` strands the
        citation.

        The earlier guard exempted the survivor outright; the citation only survives if the heading does, so a proposal
        that renames or drops it needs the citing files under ``repoint`` like any deleted section.
        """
        (env["proposals"] / "ab.md").write_text("## Merged\n\nshort merged text\n", encoding="utf-8")  # type: ignore[operator]
        assert any("renamed" in d for d in self._run(env, self._merge(env))["downgrades"])  # type: ignore[attr-defined]
        repointed = self._merge(env, repoint=[self.SKILL])
        assert self._run(env, repointed)["decision"] == "merge-prune"

    def test_deleting_a_cited_section_needs_a_repoint(self, env: dict[str, object]) -> None:
        """Dropping the section other files cite is downgraded unless the judgment lists those files for re-pointing."""
        survivor = self._merge(env, survivor=env["ids"]["beta"])  # type: ignore[index]
        assert any("cited by" in d for d in self._run(env, survivor)["downgrades"])  # type: ignore[attr-defined]
        repointed = self._merge(env, survivor=env["ids"]["beta"], repoint=[self.SKILL])  # type: ignore[index]
        assert self._run(env, repointed)["decision"] == "merge-prune"

    @pytest.mark.parametrize(
        ("verdict", "decision", "validation"),
        [
            pytest.param("holds", "merge-prune", "holds", id="holds"),
            pytest.param("weakens", "rejected", "weakens", id="weakens-is-rejected-not-relabelled"),
            pytest.param("loses", "keep", "loses", id="loses-keeps-the-text"),
        ],
    )
    def test_review_verdict_decides_the_final_state(
        self, env: dict[str, object], verdict: str, decision: str, validation: str
    ) -> None:
        """The independent verdict, not the judge, settles whether a merge stands, is rejected or reverts to keep."""
        record = self._run(env, self._merge(env), [{"cluster": "ab", "verdict": verdict, "evidence": "checked"}])
        assert (record["decision"], record["validation"]) == (decision, validation)

    def test_most_severe_of_several_verdicts_wins(self, env: dict[str, object]) -> None:
        """A later `holds` never overrides an earlier `weakens` for the same cluster."""
        reviews = [{"cluster": "ab", "verdict": "weakens"}, {"cluster": "ab", "verdict": "holds"}]
        assert self._run(env, self._merge(env), reviews)["decision"] == "rejected"

    @pytest.mark.parametrize(
        ("edits", "validation"),
        [
            pytest.param(
                [{"action": "delete", "file": SKILL, "lines": "3", "before": "x"}], "unvalidated", id="delete"
            ),
            pytest.param(
                [{"action": "pointer", "file": SKILL, "lines": "3", "before": "x"}], "unvalidated", id="pointer"
            ),
            pytest.param([{"action": "move", "file": SKILL, "lines": "3"}], "unvalidated", id="unknown-action"),
            pytest.param([{"action": "Rewrite", "file": SKILL, "lines": "3"}], "unvalidated", id="capitalised-action"),
            pytest.param(
                [{"action": "add", "file": SKILL, "lines": "3", "before": "old text"}],
                "unvalidated",
                id="add-that-replaces",
            ),
            pytest.param(
                [{"action": "add", "file": SKILL, "lines": "3", "after": "new text"}], "not-required", id="pure-add"
            ),
        ],
    )
    def test_only_a_pure_add_skips_review(self, env: dict[str, object], edits: list[object], validation: str) -> None:
        """Every edit shape except an `add` with no replaced text goes to independent review."""
        record = self._run(env, self._sharpen(env, edits))
        assert record["validation"] == validation

    @pytest.mark.parametrize(
        "malformed",
        [
            pytest.param({"edits": ["SECRET_JUDGE_RATIONALE"]}, id="string-edit"),
            pytest.param(
                {"edits": [{"action": "add", "file": "f", "after": {"reason": "SECRET_JUDGE_RATIONALE"}}]},
                id="nested-after",
            ),
            pytest.param({"edits": "not a list"}, id="edits-not-a-list"),
            pytest.param({"repoint": {"reason": "SECRET_JUDGE_RATIONALE"}}, id="repoint-object"),
            pytest.param({"repoint": [{"reason": "SECRET_JUDGE_RATIONALE"}]}, id="repoint-entry-object"),
            pytest.param({"edits": [{"action": "add", "file": "f", "lines": "9" * 5000}]}, id="huge-line-range"),
            pytest.param({"edits": [{"action": "add", "file": "f", "lines": {"x": 1}}]}, id="lines-object"),
        ],
    )
    def test_malformed_judgment_is_rejected_whole_and_leaks_nothing(
        self, env: dict[str, object], malformed: dict[str, object]
    ) -> None:
        """A judgment with a field of the wrong type is rejected as a whole; none of its text reaches a reviewer.

        The judge is a model: an object where a list belongs, a nested rationale or a 5000-digit range used to be copied
        into reviewer inputs, mis-sorted, or abort the whole settlement. One strict schema now turns each into a
        rejected cluster with the reason.
        """
        document, pending = ci.settle(
            env["vetted"], [self._sharpen(env, [self._edit("add", self.SKILL)]) | malformed], env["proposals"], []
        )  # type: ignore[arg-type]
        record = document["clusters"][0]  # type: ignore[index]
        assert record["decision"] == "rejected"
        assert any("malformed judgment" in d for d in record["downgrades"])
        assert pending == []
        assert "SECRET_JUDGE_RATIONALE" not in json.dumps(document) + json.dumps(pending)

    def test_extra_obligation_fields_never_reach_the_written_file(self, env: dict[str, object]) -> None:
        """An obligation entry keeps only its four fields, so a rationale added to it is not written for reviewers."""
        merge = self._merge(env)
        merge["obligation_map"] = [dict(entry, reason="SECRET_JUDGE_RATIONALE") for entry in merge["obligation_map"]]  # type: ignore[attr-defined]
        self._run(env, merge)
        written = (env["proposals"] / "ab.obligations.json").read_text(encoding="utf-8")  # type: ignore[operator]
        assert "SECRET_JUDGE_RATIONALE" not in written
        assert set(json.loads(written)[0]) == {"original", "from", "to", "modal_preserved"}

    def test_survivors_own_obligations_need_a_map_entry(self, env: dict[str, object]) -> None:
        """The survivor's text is replaced by the proposal, so its obligations must be mapped like the others'."""
        ids = env["ids"]
        entry = {"original": "o", "from": ids["beta"], "to": "short merged text", "modal_preserved": True}  # type: ignore[index]
        record = self._run(env, self._merge(env, obligation_map=[entry]))
        assert (record["decision"], record["advice_only"]) == ("sharpen", True)
        assert any("no obligation recorded" in d for d in record["downgrades"])

    def test_review_verdict_counts_even_when_review_was_not_required(self, env: dict[str, object]) -> None:
        """A reviewer who looked at a pure add and found it loses something is not overridden."""
        edits = [{"action": "add", "file": self.SKILL, "lines": "3", "after": "new text"}]
        record = self._run(env, self._sharpen(env, edits), [{"cluster": "sm", "verdict": "loses"}])
        assert (record["decision"], record["validation"]) == ("keep", "loses")

    def test_sharpen_without_edits_is_unreviewable_advice(self, env: dict[str, object]) -> None:
        """A sharpen with no concrete edit is reported unvalidated, is not sent to review, and leaks no reasoning."""
        document, pending = ci.settle(env["vetted"], [self._sharpen(env, [])], env["proposals"], [])  # type: ignore[arg-type]
        record = document["clusters"][0]  # type: ignore[index]
        assert (record["decision"], record["validation"], record["advice_only"]) == ("sharpen", "unvalidated", True)
        assert pending == []

    def test_edit_outside_the_cluster_is_rejected(self, env: dict[str, object]) -> None:
        """An edit adding text to a file that is not a member of the cluster is rejected, not reported as ready."""
        edits = [self._edit("add", "plugins/cc_a/skills/demo/OTHER.md")]
        record = self._run(env, self._sharpen(env, edits))
        assert record["decision"] == "rejected"
        assert any("outside the cluster" in d for d in record["downgrades"])

    @pytest.mark.parametrize(
        ("lines", "fragment"),
        [
            pytest.param("40-50", "outside the cluster's sections", id="past-the-member"),
            pytest.param("2-9", "outside the cluster's sections", id="straddles-the-member-end"),
            pytest.param("x", "no parseable", id="not-a-range"),
            pytest.param("5-2", "inverted", id="inverted-range"),
            pytest.param(None, "no parseable", id="missing-lines"),
        ],
    )
    def test_edit_must_lie_inside_a_member_section(
        self, env: dict[str, object], lines: str | None, fragment: str
    ) -> None:
        """An edit whose line range is missing, malformed or beyond the cluster's sections is rejected."""
        edit = self._edit("add", self.SKILL)
        if lines is None:
            del edit["lines"]
        else:
            edit["lines"] = lines
        record = self._run(env, self._sharpen(env, [edit]))
        assert record["decision"] == "rejected"
        assert any(fragment in d for d in record["downgrades"])

    def test_edit_touching_a_blocked_section_is_rejected(self, env: dict[str, object]) -> None:
        """An add inside a section the inventory blocked (frontmatter here) is rejected, not left unreviewed."""
        vetted = json.loads(json.dumps(env["vetted"]))
        cluster = next(c for c in vetted["clusters"] if c["id"] == "sm")
        cluster["blocked_ranges"] = [{"id": "x#0", "file": self.SKILL, "start_line": 1, "end_line": 3}]
        document, _ = ci.settle(vetted, [self._sharpen(env, [self._edit("add", self.SKILL)])], env["proposals"], [])  # type: ignore[arg-type]
        record = document["clusters"][0]  # type: ignore[index]
        assert record["decision"] == "rejected"
        assert any("blocked section x#0" in d for d in record["downgrades"])

    @pytest.mark.parametrize(
        "side",
        [
            pytest.param(
                [
                    {
                        "action": "rewrite",
                        "file": "plugins/cc_a/rules/a.md",
                        "lines": "1-3",
                        "before": "o",
                        "after": "x" * 3000,
                    }
                ],
                id="growing-edit-inside-a-member",
            ),
            pytest.param(
                [
                    {
                        "action": "rewrite",
                        "file": "plugins/cc_a/rules/a.md",
                        "lines": "400-401",
                        "before": "o",
                        "after": "x",
                    }
                ],
                id="edit-outside-the-members",
            ),
        ],
    )
    def test_merge_carrying_edits_is_downgraded(self, env: dict[str, object], side: list[dict[str, object]]) -> None:
        """A merge-prune's change is its proposal: edits beside it would grow or alter text no saving or review counts.

        The fixture case adds thousands of tokens through a side edit while the proposal alone shows a small saving;
        such a merge must fall back to unreviewable advice instead of being priced as a shortening.
        """
        record = self._run(env, self._merge(env, edits=side))
        assert (record["decision"], record["advice_only"], record["savings"]) == ("sharpen", True, 0)
        assert any("carries no edits" in d for d in record["downgrades"])

    def test_reviewer_edits_carry_only_the_change_fields(self, env: dict[str, object]) -> None:
        """A free-text key inside an edit (a reason, a note) never reaches the reviewer.

        The judge's reasoning is filtered at the top level of the judgment; an edit is also judge-authored JSON, so a
        stray ``reason`` inside it would have been copied through. Only the fields that describe the change are shown.
        """
        edit = self._edit("rewrite", self.SKILL, before="old", reason="SECRET_JUDGE_RATIONALE", note="why")
        _, pending = ci.settle(env["vetted"], [self._sharpen(env, [edit])], env["proposals"], [])  # type: ignore[arg-type]
        assert "SECRET_JUDGE_RATIONALE" not in json.dumps(pending)
        assert set(pending[0]["edits"][0]) == {"file", "lines", "action", "before", "after"}

    def test_merge_review_input_carries_survivor_and_repoint(self, env: dict[str, object]) -> None:
        """The reviewer of a merge sees the proposal, the survivor and the files to re-point, never the judge's
        reasoning."""
        _, pending = ci.settle(
            env["vetted"],  # type: ignore[arg-type]
            [self._merge(env, repoint=[self.SKILL])],
            env["proposals"],  # type: ignore[arg-type]
            [],
        )
        assert (pending[0]["survivor"], pending[0]["repoint"]) == (env["ids"]["alpha"], [self.SKILL])  # type: ignore[index]

    @pytest.mark.parametrize(
        ("edits", "crosses"),
        [
            pytest.param(
                [
                    {
                        "action": "delete",
                        "file": MODE,
                        "lines": "3",
                        "before": "run the validation step before every report",
                    },
                    {
                        "action": "add",
                        "file": SKILL,
                        "lines": "3",
                        "after": "Note: run the validation step before every report.",
                    },
                ],
                True,
                id="exact-move",
            ),
            pytest.param(
                [
                    {
                        "action": "delete",
                        "file": MODE,
                        "lines": "3",
                        "before": "Never force push to the default branch.",
                    },
                    {
                        "action": "add",
                        "file": SKILL,
                        "lines": "3",
                        "after": "Pushes to the default branch never force.",
                    },
                ],
                True,
                id="reworded-move",
            ),
            pytest.param(
                [
                    {
                        "action": "rewrite",
                        "file": SKILL,
                        "lines": "3",
                        "before": "Validate all inputs before execution.",
                        "after": "Check inputs first.",
                    },
                    {
                        "action": "rewrite",
                        "file": MODE,
                        "lines": "3",
                        "before": "Validate all inputs before execution. Log errors.",
                        "after": "Validate all inputs before execution. Report errors.",
                    },
                ],
                False,
                id="shared-substring-independent-rewrites",
            ),
        ],
    )
    def test_net_transfer_between_lazy_files_goes_to_review_flagged(
        self, env: dict[str, object], edits: list[dict[str, object]], crosses: bool
    ) -> None:
        """Text leaving one lazy file and arriving in another is flagged for the reviewer; it is never string-matched.

        Whether an edit set moves text is a semantic call. A lexical test rejected independent rewrites that happened to
        share a sentence and admitted a reworded move, so the script only measures net text per file and the independent
        reviewer, told the flag, decides: ``weakens`` rejects it as the merge it is.
        """
        document, pending = ci.settle(env["vetted"], [self._sharpen(env, edits)], env["proposals"], [])  # type: ignore[arg-type]
        record = document["clusters"][0]  # type: ignore[index]
        assert (record["decision"], record["validation"], record["crosses_files"]) == (
            "sharpen",
            "unvalidated",
            crosses,
        )
        assert pending[0]["crosses_files"] is crosses

    def test_reviewer_weakens_verdict_rejects_a_flagged_move(self, env: dict[str, object]) -> None:
        """A reviewer who calls the flagged move a merge turns the sharpen into a rejection with the quoted
        difference."""
        edits = [
            {
                "action": "delete",
                "file": self.MODE,
                "lines": "3",
                "before": "run the validation step before every report",
            },
            {"action": "add", "file": self.SKILL, "lines": "3", "after": "Note: run the validation step."},
        ]
        record = self._run(env, self._sharpen(env, edits), [{"cluster": "sm", "verdict": "weakens", "evidence": "q"}])
        assert (record["decision"], record["validation"]) == ("rejected", "weakens")

    def test_merge_allowed_group_is_never_flagged(self, env: dict[str, object]) -> None:
        """In an always-loaded group moving text between files is an ordinary merge, so no flag is raised."""
        edits = [
            {"action": "delete", "file": "plugins/cc_a/rules/b.md", "lines": "3", "before": "y" * 40},
            {"action": "add", "file": "plugins/cc_a/rules/a.md", "lines": "3", "after": "y" * 40},
        ]
        document, _ = ci.settle(env["vetted"], [self._sharpen(env, edits, cluster="ab")], env["proposals"], [])  # type: ignore[arg-type]
        assert document["clusters"][0]["crosses_files"] is False  # type: ignore[index]

    def test_sharpen_removing_a_cited_heading_needs_every_citing_file_under_repoint(
        self, env: dict[str, object]
    ) -> None:
        """Deleting the heading line of a section other files cite is rejected unless they are all listed for re-
        pointing.

        The merge path already required this; a sharpen that deletes the section reaches the same dangling citation, so
        the rule belongs to every edit that removes text, and the reviewer is told which files to re-point.
        """
        vetted = json.loads(json.dumps(env["vetted"]))
        self._cite(vetted, "sm", env["ids"]["steps"], "Steps", ["plugins/cc_a/other.md"])  # type: ignore[index]
        edit = {"action": "delete", "file": self.SKILL, "lines": "1-5", "before": "x"}
        rejected, _ = ci.settle(vetted, [self._sharpen(env, [edit])], env["proposals"], [])  # type: ignore[arg-type]
        assert rejected["clusters"][0]["decision"] == "rejected"  # type: ignore[index]
        assert any("repoint" in d for d in rejected["clusters"][0]["downgrades"])  # type: ignore[index]
        judged = self._sharpen(env, [edit]) | {"repoint": ["plugins/cc_a/other.md"]}
        document, pending = ci.settle(vetted, [judged], env["proposals"], [])  # type: ignore[arg-type]
        assert document["clusters"][0]["decision"] == "sharpen"  # type: ignore[index]
        assert pending[0]["repoint"] == ["plugins/cc_a/other.md"]

    def test_rewrite_inside_a_cited_section_does_not_need_repoint(self, env: dict[str, object]) -> None:
        """Only an edit that covers the heading line removes the citation target; body rewrites keep it."""
        vetted = json.loads(json.dumps(env["vetted"]))
        self._cite(vetted, "sm", env["ids"]["steps"], "Steps", ["plugins/cc_a/other.md"])  # type: ignore[index]
        edit = {"action": "rewrite", "file": self.SKILL, "lines": "3-4", "before": "old", "after": "new"}
        document, _ = ci.settle(vetted, [self._sharpen(env, [edit])], env["proposals"], [])  # type: ignore[arg-type]
        assert document["clusters"][0]["decision"] == "sharpen"  # type: ignore[index]

    def test_independent_rewrites_in_two_lazy_files_are_a_sharpen_not_a_move(self, env: dict[str, object]) -> None:
        """Rewording one sentence in each of two files removes different text from each; that is not a relocation.

        Treating every removal in one file plus every addition in another as a move rejected plain independent
        sharpening of a skill's entrypoint and its mode file, which is exactly what a lazy cluster may do.
        """
        edits = [
            self._edit(
                "rewrite", self.SKILL, before="Before execution, validate inputs.", after="Validate inputs first."
            ),
            self._edit(
                "rewrite", self.MODE, before="Prior to reporting, verify outputs.", after="Verify outputs first."
            ),
        ]
        record = self._run(env, self._sharpen(env, edits))
        assert (record["decision"], record["validation"]) == ("sharpen", "unvalidated")
        assert record["downgrades"] == []

    def test_same_text_added_twice_needs_review(self, env: dict[str, object]) -> None:
        """Identical added text in two places is duplication, which is reviewed even though each edit only adds."""
        edits = [self._edit("add", self.SKILL), self._edit("add", self.MODE)]
        assert self._run(env, self._sharpen(env, edits))["validation"] == "unvalidated"

    def test_review_inputs_leave_out_the_judges_reasoning(self, env: dict[str, object]) -> None:
        """The reviewer receives locations and the change, never the judge's shared, distinct or reason text."""
        judged = self._merge(env, reason="secret rationale", shared="s", distinct="d")
        _, pending = ci.settle(env["vetted"], [judged], env["proposals"], [])  # type: ignore[arg-type]
        assert "secret rationale" not in json.dumps(pending)
        assert pending[0]["proposal"].endswith("ab.md")

    def test_settled_record_keeps_the_review_evidence_and_reason(self, env: dict[str, object]) -> None:
        """The report is built from the settled record, so it carries the judge's reason and the reviewer's quote."""
        review = [{"cluster": "ab", "verdict": "weakens", "evidence": "quoted difference"}]
        record = self._run(env, self._merge(env, reason="why"), review)
        assert (record["reason"], record["evidence"]) == ("why", "quoted difference")

    def test_obligation_file_is_removed_when_the_merge_does_not_stand(self, env: dict[str, object]) -> None:
        """A later settle that rejects the merge deletes the obligation map the earlier pass wrote."""
        self._run(env, self._merge(env))
        assert (env["proposals"] / "ab.obligations.json").exists()  # type: ignore[operator]
        self._run(env, self._merge(env), [{"cluster": "ab", "verdict": "weakens"}])
        assert not (env["proposals"] / "ab.obligations.json").exists()  # type: ignore[operator]

    def test_judgments_correspond_one_to_one_with_judged_clusters(self, env: dict[str, object]) -> None:
        """A duplicate judgment is rejected, an unknown one is rejected, and a missing one is recorded as unjudged."""
        judgments = [self._merge(env), self._merge(env), {"cluster": "ghost", "decision": "keep"}]
        document, _ = ci.settle(env["vetted"], judgments, env["proposals"], [])  # type: ignore[arg-type]
        decisions = [(c["cluster"], c["decision"]) for c in document["clusters"]]  # type: ignore[index]
        assert decisions == [("ab", "merge-prune"), ("ab", "rejected"), ("ghost", "rejected"), ("sm", "unjudged")]
        assert document["totals"]["savings"]["always"]["unvalidated"] == self._run(env, self._merge(env))["savings"]  # type: ignore[index]

    def test_judgment_for_a_cluster_that_was_not_to_be_judged_is_rejected(self, env: dict[str, object]) -> None:
        """A judgment for a valid cluster the cap or floor excluded does not bypass the skip."""
        vetted = {"clusters": [dict(c, judge=False, skip_reason="judge cap") for c in env["vetted"]["clusters"]]}  # type: ignore[index]
        document, _ = ci.settle(vetted, [self._merge(env)], env["proposals"], [])
        assert document["clusters"][0]["decision"] == "rejected"  # type: ignore[index]

    def test_unknown_or_rejected_cluster_is_not_settled_as_valid(self, env: dict[str, object]) -> None:
        """A judgment for a cluster the guard did not accept is recorded as rejected."""
        document, _ = ci.settle(env["vetted"], [{"cluster": "ghost", "decision": "keep"}], env["proposals"], [])  # type: ignore[arg-type]
        assert document["clusters"][0]["decision"] == "rejected"  # type: ignore[index]

    def test_valid_cluster_survives_a_rejected_duplicate_id(self, env: dict[str, object]) -> None:
        """When the guard reports `[valid, rejected-duplicate]` for one id, the valid one is the one settled."""
        clusters = env["vetted"]["clusters"]  # type: ignore[index]
        duplicate = dict(clusters[0], ok=False, judge=False)
        document, _ = ci.settle({"clusters": [*clusters, duplicate]}, [self._merge(env)], env["proposals"], [])
        assert document["clusters"][0]["decision"] == "merge-prune"  # type: ignore[index]

    def test_totals_separate_validated_from_unvalidated_savings(self, env: dict[str, object]) -> None:
        """Savings are priced per load class and only a reviewed merge counts as validated."""
        bare, pending = ci.settle(env["vetted"], [self._merge(env)], env["proposals"], [])  # type: ignore[arg-type]
        holds = [{"cluster": "ab", "digest": pending[0]["digest"], "verdict": "holds", "evidence": "checked"}]
        reviewed, _ = ci.settle(env["vetted"], [self._merge(env)], env["proposals"], holds)  # type: ignore[arg-type]
        assert bare["totals"]["savings"]["always"]["validated"] == 0  # type: ignore[index]
        assert reviewed["totals"]["savings"]["always"]["validated"] > 0  # type: ignore[index]

    def test_review_written_for_another_change_is_ignored(self, env: dict[str, object]) -> None:
        """A verdict binds to the exact proposal it judged; the same cluster with new text is unvalidated again.

        Reviews are matched on a digest of the proposal, obligations, repoint and locations. A stale or reused review
        file must never validate text the reviewer did not read.
        """
        _, pending = ci.settle(env["vetted"], [self._merge(env)], env["proposals"], [])  # type: ignore[arg-type]
        holds = [{"cluster": "ab", "digest": pending[0]["digest"], "verdict": "holds", "evidence": "checked"}]
        (env["proposals"] / "ab.md").write_text(
            "## Alpha Rule\n\nshort merged text, may omit checks\n", encoding="utf-8"
        )  # type: ignore[operator]
        document, _ = ci.settle(env["vetted"], [self._merge(env)], env["proposals"], holds)  # type: ignore[arg-type]
        record = document["clusters"][0]  # type: ignore[index]
        assert (record["validation"], any("different change" in d for d in record["downgrades"])) == (
            "unvalidated",
            True,
        )

    @pytest.mark.parametrize(
        "review",
        [
            pytest.param({"cluster": "ab", "verdict": "holds", "evidence": "x"}, id="no-digest"),
            pytest.param({"cluster": "ab", "verdict": "holds", "digest": "0" * 64, "evidence": "x"}, id="wrong-digest"),
            pytest.param({"cluster": "ab", "verdict": "loses", "digest": "stale"}, id="stale-loses"),
        ],
    )
    def test_review_without_the_matching_digest_changes_nothing(
        self, env: dict[str, object], review: dict[str, object]
    ) -> None:
        """A verdict that does not carry this change's digest neither validates nor rejects it."""
        document, _ = ci.settle(env["vetted"], [self._merge(env)], env["proposals"], [review])  # type: ignore[arg-type]
        record = document["clusters"][0]  # type: ignore[index]
        assert (record["decision"], record["validation"]) == ("merge-prune", "unvalidated")

    def test_digest_covers_the_flags_the_reviewer_is_shown(self, env: dict[str, object]) -> None:
        """The same edits in a group that allows merging and one that does not are different reviewer inputs."""
        edits = [{"action": "delete", "file": self.SKILL, "lines": "3", "before": "x" * 40}]
        vet = next(c for c in env["vetted"]["clusters"] if c["id"] == "sm")  # type: ignore[index]
        judged = {"edits": edits, "repoint": []}
        flipped = dict(vet, merge_allowed=True)
        proposal = env["proposals"] / "none.md"  # type: ignore[operator]
        assert ci._change_digest("sharpen", vet, judged, proposal, True) != ci._change_digest(
            "sharpen", flipped, judged, proposal, True
        )
        assert ci._change_digest("sharpen", vet, judged, proposal, True) != ci._change_digest(
            "sharpen", vet, judged, proposal, False
        )

    def test_digest_covers_the_original_text_under_review(self, env: dict[str, object]) -> None:
        """The same proposal over changed originals is a different change, so an old verdict does not carry over."""
        vet = next(c for c in env["vetted"]["clusters"] if c["id"] == "sm")  # type: ignore[index]
        judged = {"edits": [], "repoint": []}
        proposal = env["proposals"] / "none.md"  # type: ignore[operator]
        before = ci._change_digest("sharpen", vet, judged, proposal, False, {"a.md": "never rely"})
        after = ci._change_digest("sharpen", vet, judged, proposal, False, {"a.md": "often rely"})
        assert before != after

    def test_two_different_edit_lists_have_different_digests(self, env: dict[str, object]) -> None:
        """'Must retain access checks' and 'may omit access checks' are different changes with different digests."""
        digests = []
        for after in ("must retain access checks", "may omit access checks"):
            edit = {"action": "rewrite", "file": self.SKILL, "lines": "3", "before": "s" * 30, "after": after}
            digests.append(self._run(env, self._sharpen(env, [edit]))["digest"])
        assert digests[0] != digests[1]

    @pytest.mark.parametrize(
        "original",
        [
            pytest.param(None, id="null"),
            pytest.param(False, id="false"),
            pytest.param([], id="empty-list"),
            pytest.param({}, id="empty-object"),
        ],
    )
    def test_obligation_field_of_the_wrong_type_rejects_the_judgment(
        self, env: dict[str, object], original: object
    ) -> None:
        """A present obligation field that is not text rejects the whole judgment instead of being dropped."""
        entry = {"original": original, "from": env["ids"]["alpha"], "to": "short merged text", "modal_preserved": True}  # type: ignore[index]
        record = self._run(env, self._merge(env, obligation_map=[entry]))
        assert record["decision"] == "rejected"
        assert any("obligation field original" in d for d in record["downgrades"])  # type: ignore[attr-defined]


class TestCli:
    """Covers the command-line contract."""

    def test_unknown_kinds_exit_one(self, corpus: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A kinds list naming nothing valid is refused before any work."""
        assert ci.main(["inventory", "--root", str(corpus), "--kinds", "bogus"]) == 1
        assert "BLOCKED" in capsys.readouterr().out

    def test_missing_inventory_file_exits_one(self, tmp_path: Path) -> None:
        """A validate run pointed at a missing inventory exits 1."""
        clusters = tmp_path / "c.json"
        clusters.write_text("[]", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            ci.main(["validate", "--inventory", str(tmp_path / "none.json"), "--clusters", str(clusters)])
        assert exc.value.code == 1

    def test_wrong_schema_version_exits_one(self, tmp_path: Path) -> None:
        """An inventory of another schema version is refused."""
        inv, clusters = tmp_path / "i.json", tmp_path / "c.json"
        inv.write_text(json.dumps({"schema_version": 99, "items": []}), encoding="utf-8")
        clusters.write_text("[]", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            ci.main(["validate", "--inventory", str(inv), "--clusters", str(clusters)])
        assert exc.value.code == 1

    def test_validate_prints_verdict_per_cluster(
        self, corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A validate run prints one line per cluster with its reasons and exits 0 even when a cluster is not ok."""
        inv, clusters = tmp_path / "i.json", tmp_path / "c.json"
        assert ci.main(["inventory", "--root", str(corpus), "--out", str(inv)]) == 0
        clusters.write_text(json.dumps([{"id": "bad", "items": ["ghost#1", "ghost#2"]}]), encoding="utf-8")
        assert ci.main(["validate", "--inventory", str(inv), "--clusters", str(clusters)]) == 0
        out = capsys.readouterr().out
        assert "bad: NOT ok" in out
        assert "unknown members" in out

    def test_select_and_settle_round_trip_through_files(self, corpus: Path, tmp_path: Path) -> None:
        """The select and settle subcommands read and write the documented files and exit 0."""
        inv, sel, vet, jud, out = (tmp_path / n for n in ("i.json", "s.json", "v.json", "j.json", "o.json"))
        assert ci.main(["inventory", "--root", str(corpus), "--out", str(inv)]) == 0
        assert ci.main(["select", "--inventory", str(inv), "--scope", "a", "--min-tokens", "0", "--out", str(sel)]) == 0
        assert "selected" in json.loads(sel.read_text(encoding="utf-8"))
        cls = tmp_path / "c.json"
        cls.write_text("[]", encoding="utf-8")
        assert ci.main(["validate", "--inventory", str(inv), "--clusters", str(cls), "--out", str(vet)]) == 0
        jud.write_text("[]", encoding="utf-8")
        args = [
            "settle",
            "--inventory",
            str(inv),
            "--vetted",
            str(vet),
            "--judgments",
            str(jud),
            "--proposals-dir",
            str(tmp_path),
            "--out",
            str(out),
        ]
        assert ci.main(args) == 0
        assert json.loads(out.read_text(encoding="utf-8"))["clusters"] == []

    def test_settle_rejects_a_non_report_vetted_file(
        self, corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A vetted file that is not a validate report is refused with exit 1."""
        inv, vet, jud = tmp_path / "i.json", tmp_path / "v.json", tmp_path / "j.json"
        assert ci.main(["inventory", "--root", str(corpus), "--out", str(inv)]) == 0
        vet.write_text("[]", encoding="utf-8")
        jud.write_text("[]", encoding="utf-8")
        argv = ["settle", "--inventory", str(inv), "--vetted", str(vet), "--judgments", str(jud)]
        assert ci.main([*argv, "--proposals-dir", str(tmp_path)]) == 1
        assert "BLOCKED" in capsys.readouterr().out


#: The controlled fixture: relative path -> text of seven rules of a synthetic plugin, written to a temp tree per test
#: so no Markdown instruction file sits in the test tree.
FIXTURE_FILE = Path(__file__).parent / "fixtures" / "consolidate_fixture.json"


class TestControlledFixture:
    """Covers the deterministic half of the controlled fixture used to measure the judge.

    The seven rules and the answer each must get: ``release-sources``, ``changelog-evidence`` and
    ``release-draft-grounding`` are always loaded and paraphrase one idea (they cluster and may merge);
    ``release-sources-python`` repeats ``release-sources`` under a ``**/*.py`` scope (never clusters with it);
    ``force-push-ban`` is an incident-encoded ``never`` (must not be generalised away); ``rebase-policy`` is a nearby
    ``prefer`` (must not be hardened into the ban); ``release-language`` is another decision surface (stays alone).
    """

    @pytest.fixture
    def inventory(self, tmp_path: Path) -> dict[str, object]:
        """Write the fixture tree to a temp dir and inventory it."""
        for rel, text in json.loads(FIXTURE_FILE.read_text(encoding="utf-8")).items():
            _write(tmp_path, rel, text)
        return ci.build_inventory(tmp_path, ("rules",))

    def test_paraphrased_rules_form_a_valid_mergeable_cluster(self, inventory: dict[str, object]) -> None:
        """The three rules that say one thing in different words can be clustered, and the guard allows a merge."""
        files = ("release-sources", "changelog-evidence", "release-draft-grounding")
        ids = [
            _id(inventory, f"plugins/cc_demo/rules/{f}.md", s)
            for f, s in zip(
                files, ("Release Notes Source", "Changelog Evidence", "Release Draft Grounding"), strict=True
            )
        ]
        result = _vet(inventory, ids)
        assert (result["ok"], result["merge_allowed"]) == (True, True)

    def test_same_wording_with_a_narrower_scope_cannot_be_clustered(self, inventory: dict[str, object]) -> None:
        """The path-scoped copy of the release rule has the same words but fires on fewer files: refused."""
        ids = [
            _id(inventory, "plugins/cc_demo/rules/release-sources.md", "Release Notes Source"),
            _id(inventory, "plugins/cc_demo/rules/release-sources-python.md", "Release Notes Source"),
        ]
        result = _vet(inventory, ids)
        assert result["ok"] is False
        assert len(result["mixed_scopes"]) == 2


@pytest.fixture(scope="module")
def real_inventory() -> dict[str, object]:
    """Inventory the real source tree once for the module."""
    return ci.build_inventory(REPO_ROOT, ci.ALL_KINDS)


@pytest.mark.skipif(not (REPO_ROOT / "plugins" / "cc_foundry").is_dir(), reason="needs the source tree")
class TestRealCorpus:
    """Covers the real plugin tree: the byte-identical shared copies must come out blocked."""

    def test_adversarial_loop_copies_are_blocked(self, real_inventory: dict[str, object]) -> None:
        """Every ``adversarial-loop.md`` copy in the Claude plugins is blocked, never a merge candidate."""
        copies = [i for i in real_inventory["items"] if i["file"].endswith("rules/_full/adversarial-loop.md")]  # type: ignore[attr-defined]
        assert copies, "no adversarial-loop.md copies found"
        assert all(any(b.startswith("shared-file") for b in i["blocked"]) for i in copies)

    def test_delta_rule_and_its_full_variant_never_share_a_group(self, real_inventory: dict[str, object]) -> None:
        """``cc_develop`` ships ``quality-gates.md`` and ``quality-gates-delta.md``; setup delivers one, so they stay
        apart."""
        groups = {
            i["file"]: i["group"]
            for i in real_inventory["items"]  # type: ignore[attr-defined]
            if i["file"]
            in ("plugins/cc_develop/rules/quality-gates.md", "plugins/cc_develop/rules/quality-gates-delta.md")
        }
        assert len(groups) == 2
        assert len(set(groups.values())) == 2

    def test_every_skill_and_agent_frontmatter_is_blocked(self, real_inventory: dict[str, object]) -> None:
        """A skill's or agent's frontmatter (name, description, tools) is never a merge candidate."""
        heads = [i for i in real_inventory["items"] if i["kind"] in ("skill", "agent") and i["start_line"] == 1]  # type: ignore[attr-defined]
        assert heads
        assert all(any(b.startswith("frontmatter") for b in i["blocked"]) for i in heads)

    def test_backticked_citation_of_a_shipped_rule_is_found(self, real_inventory: dict[str, object]) -> None:
        """``claude-config.md`` §Agent/Skill Spawn Discipline is cited in the backtick form across the plugin."""
        cited = [
            i
            for i in real_inventory["items"]  # type: ignore[attr-defined]
            if i["file"] == "plugins/cc_foundry/rules/claude-config.md" and i["section"].startswith("Agent/Skill Spawn")
        ]
        assert cited
        assert "plugins/cc_foundry/rules/model-tiering.md" in cited[0]["inbound"]

    def test_example_headings_inside_nested_fences_are_not_sections(self, real_inventory: dict[str, object]) -> None:
        """The distill memory mode shows a template inside a four-backtick block; its headings are not sections."""
        titles = {i["section"] for i in real_inventory["items"] if i["file"].endswith("skills/distill/modes/memory.md")}  # type: ignore[attr-defined]
        assert "[Rule heading]" not in titles


class TestScopeGlobs:
    """Covers reading ``paths:`` so that a character class in a glob is never taken for a YAML flow list."""

    @pytest.mark.parametrize(
        "frontmatter",
        [
            pytest.param('paths: "src/[ab]/**"', id="quoted-scalar"),
            pytest.param("paths: src/[ab]/**", id="bare-scalar"),
            pytest.param("paths:\n  - src/[ab]/**", id="block-item"),
        ],
    )
    def test_character_class_stays_part_of_the_glob(self, frontmatter: str) -> None:
        """``src/[ab]/**`` is one glob in every YAML spelling, never the list ``['ab']``."""
        assert ci.parse_paths(f"---\n{frontmatter}\n---\nbody") == ["src/[ab]/**"]

    def test_rules_scoped_by_different_character_class_globs_are_different_groups(self) -> None:
        """Two rules whose globs differ only outside the brackets must never share a merge group."""
        groups = {
            ci.group_id("cc_a", ci.classify("rules/x.md", f'---\npaths: "{glob}"\n---\nbody')[1])
            for glob in ("src/[ab]/**", "docs/[ab]/**")
        }
        assert len(groups) == 2

    @pytest.mark.parametrize(
        "frontmatter",
        [
            pytest.param("paths: >-\n  src/**", id="folded-scalar"),
            pytest.param("paths: |\n  src/**", id="literal-scalar"),
            pytest.param("paths: &anchor src/**", id="anchor"),
            pytest.param("paths: !!str src/**", id="tag"),
            pytest.param("paths: src/**\npaths: docs/**", id="duplicate-key"),
            pytest.param("paths: [a/**, [b/**]]", id="nested-flow"),
        ],
    )
    def test_yaml_spellings_this_reader_does_not_read_fail_closed(self, frontmatter: str) -> None:
        """A block or folded scalar, anchor, tag, duplicate key or nested list is never guessed at."""
        assert ci.parse_paths(f"---\n{frontmatter}\n---\nbody") == [ci.UNPARSED_PATHS]

    @pytest.mark.parametrize(
        "frontmatter",
        [
            pytest.param("  paths: src/**", id="indented-key"),
            pytest.param("{paths: [src/**]}", id="flow-mapping-root"),
            pytest.param("name: x\nmeta: {a: 1, paths: src/**}", id="key-inside-a-flow-mapping"),
        ],
    )
    def test_a_paths_key_the_reader_did_not_account_for_is_never_read_as_no_scope(self, frontmatter: str) -> None:
        """Valid YAML can put the key where the column-0 reader does not look; that must not mean 'always loaded'."""
        assert ci.parse_paths(f"---\n{frontmatter}\n---\nbody") == [ci.UNPARSED_PATHS]

    @pytest.mark.parametrize(
        "frontmatter",
        [
            pytest.param('"paths": "src/**"', id="quoted-key"),
            pytest.param("? paths\n: src/**", id="explicit-key"),
            pytest.param('"\\u0070aths": src/**', id="escaped-key"),
            pytest.param("description: d\n'paths': src/**", id="single-quoted-key"),
        ],
    )
    def test_frontmatter_outside_the_closed_key_grammar_is_unreadable(self, frontmatter: str) -> None:
        """Only bare ``key:`` lines are read; a key spelled any other way could be a scope, so it is never skipped."""
        assert ci.parse_paths(f"---\n{frontmatter}\n---\nbody") == [ci.UNPARSED_PATHS]

    def test_unreadable_scopes_never_share_a_merge_group(self) -> None:
        """Two rules whose scope could not be read are each their own group, so they are never merged together."""
        groups = {
            ci.group_id("cc_a", ci.classify(f"rules/{name}.md", "---\npaths: >-\n  x\n---\nbody")[1])
            for name in ("a", "b")
        }
        assert len(groups) == 2

    def test_unterminated_flow_list_fails_closed(self) -> None:
        """A flow list that never closes is not guessed at; the rule is not taken for an unscoped one."""
        assert ci.parse_paths("---\npaths: [a/**, b/**\n---\nbody") == [ci.UNPARSED_PATHS]


class TestGuardedHeadings:
    """Covers citations to deep headings: every cited heading must survive a change or be re-pointed."""

    A = "plugins/cc_a/rules/a.md"

    @pytest.fixture
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        """Build two rules, one with a cited level-5 heading, and the vetted cluster over both."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        root = tmp_path / "repo"
        deep = "## Alpha Rule\n\n" + "x" * 300 + "\n\n##### Deep Anchor\n\nmore deep text\n"
        _write(root, self.A, deep)
        _write(root, "plugins/cc_a/rules/b.md", "## Beta Rule\n\n" + "y" * 300 + "\n")
        _write(root, "plugins/cc_a/skills/demo/SKILL.md", "## Steps\n\ncites a.md §Deep Anchor\n")
        inventory = ci.build_inventory(root, ci.ALL_KINDS)
        alpha, beta = _id(inventory, self.A, "Alpha Rule"), _id(inventory, "plugins/cc_a/rules/b.md", "Beta Rule")
        proposals = tmp_path / "proposals"
        proposals.mkdir()
        vetted = ci.validate(inventory, [{"id": "ab", "items": [alpha, beta]}], 0)
        return {
            **{"inventory": inventory, "vetted": vetted, "alpha": alpha, "beta": beta, "proposals": proposals},
            "root": root,
        }

    def _settle(self, env: dict[str, object], judged: dict[str, object]) -> dict[str, object]:
        """Settle one judgment of cluster ``ab`` and return its record."""
        judgments = [{"cluster": "ab"} | judged]
        document, _ = ci.settle(env["vetted"], judgments, env["proposals"], [], env["root"])  # type: ignore[arg-type]
        return document["clusters"][0]  # type: ignore[index]

    def _merge(self, env: dict[str, object], proposal: str) -> dict[str, object]:
        """Write ``proposal`` and return the merge judgment that keeps ``alpha`` as survivor."""
        (env["proposals"] / "ab.md").write_text(proposal, encoding="utf-8")  # type: ignore[operator]
        obligations = [
            {"original": "o", "from": env[key], "to": "short merged text", "modal_preserved": True}
            for key in ("alpha", "beta")
        ]
        return {"decision": "merge-prune", "survivor": env["alpha"], "obligation_map": obligations}

    def test_inventory_records_the_cited_deep_heading_with_its_line(self, env: dict[str, object]) -> None:
        """The level-5 heading is guarded at its own line, with the file that cites it."""
        alpha = next(i for i in env["inventory"]["items"] if i["id"] == env["alpha"])  # type: ignore[index]
        assert [(g["title"], g["line"], g["refs"]) for g in alpha["guarded"]] == [
            ("Deep Anchor", 5, ["plugins/cc_a/skills/demo/SKILL.md"])
        ]

    @pytest.mark.parametrize(
        "after",
        [
            pytest.param("", id="deleted"),
            pytest.param("```\n##### Deep Anchor\n```", id="fenced-copy-is-not-a-heading"),
        ],
    )
    def test_edit_dropping_the_cited_deep_heading_needs_a_repoint(self, env: dict[str, object], after: str) -> None:
        """Deleting or fencing the level-5 heading strands its citation, though the section's own heading stays."""
        edit = {"action": "rewrite", "file": self.A, "lines": "5-7", "before": "##### Deep Anchor", "after": after}
        record = self._settle(env, {"decision": "sharpen", "edits": [edit]})
        assert (record["decision"], any("Deep Anchor" in d for d in record["downgrades"])) == ("rejected", True)

    def test_edit_keeping_the_cited_deep_heading_needs_no_repoint(self, env: dict[str, object]) -> None:
        """A rewrite whose new text still carries the level-5 heading leaves the citation intact."""
        edit = {"action": "rewrite", "file": self.A, "lines": "5-7", "before": "x", "after": "##### Deep Anchor\n\nnew"}
        assert self._settle(env, {"decision": "sharpen", "edits": [edit]})["decision"] == "sharpen"

    def test_merge_dropping_the_cited_deep_heading_needs_a_repoint(self, env: dict[str, object]) -> None:
        """A merge that keeps the survivor's own heading but drops its cited level-5 heading strands the citation."""
        record = self._settle(env, self._merge(env, "## Alpha Rule\n\nshort merged text\n"))
        assert (record["decision"], any("cited by" in d for d in record["downgrades"])) == ("sharpen", True)

    def test_merge_carrying_the_cited_deep_heading_stands(self, env: dict[str, object]) -> None:
        """The same merge with the level-5 heading in its proposal keeps every citation."""
        record = self._settle(env, self._merge(env, "## Alpha Rule\n\nshort merged text\n\n##### Deep Anchor\n"))
        assert record["decision"] == "merge-prune"

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param({"action": "add", "lines": "4", "before": "", "after": "```"}, id="fence-opened-before-it"),
            pytest.param(
                {"action": "rewrite", "lines": "5", "before": "#", "after": "    ##### Deep Anchor"},
                id="indented-into-code",
            ),
        ],
    )
    def test_edit_that_stops_a_heading_being_a_heading_is_caught_by_re_reading_the_file(
        self, env: dict[str, object], edit: dict[str, str]
    ) -> None:
        """The heading line is never deleted, yet the edited file no longer has the heading: an unmatched fence opened
        above it, or four spaces of indent.

        Survival is decided by applying the edits and reading the result with the inventory's own heading reader, so
        this needs no list of ways to hide a heading.
        """
        record = self._settle(env, {"decision": "sharpen", "edits": [{"file": self.A} | edit]})
        assert (record["decision"], any("Deep Anchor" in d for d in record["downgrades"])) == ("rejected", True)

    def test_fence_added_at_the_end_of_a_member_hides_the_next_section_from_outside_the_cluster(
        self, env: dict[str, object]
    ) -> None:
        """The cited heading belongs to a section outside the cluster, but the edit's fence still swallows it.

        Guards cover every cited heading in the member's file, because a structural edit changes how all of it reads.
        """
        root = env["root"]
        extra = root / "plugins/cc_a/rules/c.md"  # type: ignore[operator]
        _write(root, "plugins/cc_a/rules/c.md", "## Editable\n\n" + "e" * 300 + "\n\n## Protected\n\nNever delete.\n")  # type: ignore[arg-type]
        _write(root, "plugins/cc_a/skills/demo/SKILL.md", "## Steps\n\ncites c.md §Protected\n")  # type: ignore[arg-type]
        inventory = ci.build_inventory(root, ci.ALL_KINDS)  # type: ignore[arg-type]
        ids = [_id(inventory, "plugins/cc_a/rules/c.md", "Editable"), env["beta"]]
        vetted = ci.validate(inventory, [{"id": "ab", "items": ids}], 0)
        edit = {"file": "plugins/cc_a/rules/c.md", "action": "add", "lines": "5", "before": "", "after": "```"}
        document, _ = ci.settle(
            vetted, [{"cluster": "ab", "decision": "sharpen", "edits": [edit]}], env["proposals"], [], root
        )  # type: ignore[arg-type]
        assert extra.exists()
        assert (
            document["clusters"][0]["decision"],
            "Protected" in " ".join(document["clusters"][0]["downgrades"]),
        ) == ("rejected", True)  # type: ignore[index]

    def test_overlapping_edits_are_rejected(self, env: dict[str, object]) -> None:
        """Two edits covering a common line cannot both be applied, so neither is trusted."""
        edits = [
            {"action": "rewrite", "file": self.A, "lines": "3-5", "before": "x", "after": "##### Deep Anchor"},
            {"action": "rewrite", "file": self.A, "lines": "5-6", "before": "y", "after": "##### Deep Anchor"},
        ]
        record = self._settle(env, {"decision": "sharpen", "edits": edits})
        assert any("overlap" in d for d in record["downgrades"])


class TestHeadingReader:
    """Covers the one heading reader behind sections, citations and survival after an edit."""

    @pytest.mark.parametrize(
        ("text", "titles"),
        [
            pytest.param("   ## Indented\n\nbody", ["Indented"], id="atx-with-three-spaces"),
            pytest.param("    ## Code\n\nbody", [], id="four-spaces-is-code"),
            pytest.param("Setext Title\n------------\n\nbody", ["Setext Title"], id="setext-level-two"),
            pytest.param("Top\n===\n\nbody", ["Top"], id="setext-level-one"),
            pytest.param("```\n## In Fence\n```\n## Real", ["Real"], id="fenced-heading-ignored"),
            pytest.param("<!--\n## Hidden\n-->\n## Real", ["Real"], id="commented-heading-ignored"),
            pytest.param("<!-- note --> text\n## Real", ["Real"], id="inline-comment-closes"),
            pytest.param("<!--\n```\n-->\n## Real", ["Real"], id="fence-marker-inside-comment"),
            pytest.param("- item\n---\n", [], id="rule-after-list-item"),
            pytest.param("| a |\n|---|\n", [], id="table-delimiter"),
        ],
    )
    def test_headings_are_read_the_commonmark_way(self, text: str, titles: list[str]) -> None:
        """Indent, setext underlines and fences decide what is a heading, for sections and citations alike."""
        assert [title for title, _ in ci._headings(text)] == titles
        assert [t for t, *_ in ci.split_sections(text) if not t.startswith("(")] == titles


class TestSnapshotInputs:
    """Covers what the inventory hashes: every input that decides merge eligibility."""

    def test_shared_digest_changes_with_the_propagation_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Shared-file membership blocks merging but lives in no inventoried file, so it has its own digest."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        before = ci.shared_digest()
        monkeypatch.setattr(ci, "MANIFEST", [{"canonical": "plugins/cc_a/rules/s.md", "copies": []}])
        assert ci.shared_digest() != before

    def test_inventory_records_the_shared_digest(self, corpus: Path) -> None:
        """A freshness diff of the inventory sees the shared-file digest beside the file digests."""
        assert ci.build_inventory(corpus, ci.ALL_KINDS)["shared_digest"] == ci.shared_digest()


def _vetted_cluster(cid: str) -> dict[str, object]:
    """Return a minimal well-formed ``ok`` cluster of a validate report."""
    return {
        **{"id": cid, "ok": True, "judge": True, "merge_allowed": True, "tokens_before": 10, "load_class": "always"},
        **{"items": [], "members": [], "locations": [], "blocked_ranges": [], "inbound": {}, "guarded": []},
        "file_guarded": [],
    }


class TestArtifactBoundaries:
    """Covers hand-edited or stale inventory and validate files: rejected cleanly, never crashing or escaping."""

    @staticmethod
    def _inventory(corpus: Path, tmp_path: Path) -> Path:
        """Write an inventory of ``corpus`` and return its path."""
        inv = tmp_path / "inv.json"
        assert ci.main(["inventory", "--root", str(corpus), "--out", str(inv)]) == 0
        return inv

    @staticmethod
    def _settle_args(inv: Path, tmp_path: Path, vetted: object, judgments: object = ()) -> list[str]:
        """Write a validate report and judgments beside ``inv`` and return the ``settle`` argv."""
        (tmp_path / "v.json").write_text(json.dumps(vetted), encoding="utf-8")
        (tmp_path / "j.json").write_text(json.dumps(list(judgments)), encoding="utf-8")
        proposals = tmp_path / "proposals"
        proposals.mkdir(exist_ok=True)
        return [
            *("settle", "--inventory", str(inv), "--vetted", str(tmp_path / "v.json")),
            *("--judgments", str(tmp_path / "j.json"), "--proposals-dir", str(proposals)),
        ]

    def test_unsafe_cluster_id_is_blocked_and_no_file_outside_the_run_is_touched(
        self, corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An absolute id would make settle delete ``<id>.obligations.json`` outside the proposals directory."""
        victim = tmp_path / "outside" / "target.obligations.json"
        victim.parent.mkdir()
        victim.write_text("keep me", encoding="utf-8")
        inv = self._inventory(corpus, tmp_path)
        vetted = {"clusters": [_vetted_cluster(str(tmp_path / "outside" / "target"))]}
        assert ci.main(self._settle_args(inv, tmp_path, vetted)) == 1
        assert "not a safe name" in capsys.readouterr().out
        assert victim.read_text(encoding="utf-8") == "keep me"

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda d: d.update(items=None), id="items-null"),
            pytest.param(lambda d: d.update(groups=[{}]), id="group-without-fields"),
            pytest.param(lambda d: d.update(items=[None]), id="item-null"),
            pytest.param(lambda d: d["groups"][0].update(group="bad"), id="group-id-without-parts"),
            pytest.param(
                lambda d: d["items"][0]["guarded"].append({"title": "t", "line": 1, "refs": [{}]}), id="refs-not-text"
            ),
            pytest.param(lambda d: d.update(kinds=["everything"]), id="unknown-kind"),
        ],
    )
    def test_malformed_inventory_is_blocked_not_a_crash(
        self, corpus: Path, tmp_path: Path, mutate: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An inventory whose records lack the fields later steps read, or hold them in the wrong type, exits 1."""
        inv = self._inventory(corpus, tmp_path)
        document = json.loads(inv.read_text(encoding="utf-8"))
        mutate(document)  # type: ignore[operator]
        inv.write_text(json.dumps(document), encoding="utf-8")
        for argv in (["select", "--inventory", str(inv), "--scope", "rules"], ["settle", "--inventory", str(inv)]):
            with pytest.raises(SystemExit) as exc:
                ci.main(
                    [
                        *argv,
                        *(["--vetted", "x", "--judgments", "x", "--proposals-dir", "x"] if argv[0] == "settle" else []),
                    ]
                )
            assert exc.value.code in (1, 2)
        assert "BLOCKED" in capsys.readouterr().out

    @pytest.mark.parametrize("clusters", [pytest.param([None], id="null-entry"), pytest.param([{}], id="empty-entry")])
    def test_malformed_validate_report_is_blocked_not_a_crash(
        self, corpus: Path, tmp_path: Path, clusters: list[object], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A validate report holding a null or empty cluster is refused before settlement."""
        inv = self._inventory(corpus, tmp_path)
        assert ci.main(self._settle_args(inv, tmp_path, {"clusters": clusters})) == 1
        assert "BLOCKED" in capsys.readouterr().out

    def test_inventory_that_no_longer_matches_the_tree_is_blocked(
        self, corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Editing an examined file after the inventory was built stops select, validate and settle."""
        inv = self._inventory(corpus, tmp_path)
        (corpus / "plugins/cc_a/rules/always.md").write_text("## One\n\nchanged\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            ci.main(["select", "--inventory", str(inv)])
        assert (exc.value.code, "does not match the tree" in capsys.readouterr().out) == (1, True)

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param("an extra rule appears", id="new-instruction-file"),
            pytest.param("a citing doc changes", id="citation-source"),
        ],
    )
    def test_any_decision_input_change_makes_the_inventory_stale(
        self, corpus: Path, tmp_path: Path, change: str
    ) -> None:
        """A new instruction file or a changed citing document is a stale inventory, not a silent difference."""
        document = ci.build_inventory(corpus, ci.ALL_KINDS)
        assert ci.inventory_mismatch(document) == []
        target = "plugins/cc_a/rules/new.md" if change == "an extra rule appears" else "plugins/cc_a/README.md"
        _write(corpus, target, "## New\n\ntext\n")
        assert ci.inventory_mismatch(document)

    def test_hand_edited_merge_flag_cannot_turn_a_lazy_cluster_into_a_merge(
        self, corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Settlement recomputes eligibility from the inventory; a flipped ``merge_allowed`` is refused."""
        inv = self._inventory(corpus, tmp_path)
        inventory = json.loads(inv.read_text(encoding="utf-8"))
        ids = [
            _id(inventory, "plugins/cc_a/skills/demo/SKILL.md", "Steps"),
            _id(inventory, "plugins/cc_a/skills/demo/modes/extra.md", "Extra"),
        ]
        vetted = ci.validate(inventory, [{"id": "lazy", "items": ids}], 0)
        assert vetted["clusters"][0]["merge_allowed"] is False  # type: ignore[index]
        vetted["clusters"][0]["merge_allowed"] = True  # type: ignore[index]
        assert ci.main(self._settle_args(inv, tmp_path, vetted)) == 1
        assert "differs from validate's result" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda d: d.update(kinds=[{}]), id="kinds-object"),
            pytest.param(lambda d: d.update(kinds=[[]]), id="kinds-list"),
            pytest.param(lambda d: d.update(kinds=[]), id="kinds-empty"),
            pytest.param(lambda d: d.update(root=3), id="root-not-text"),
        ],
    )
    def test_unrebuildable_inventory_is_blocked_cleanly(
        self, corpus: Path, tmp_path: Path, mutate: object, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A kinds list holding objects, or no kinds, or a non-text root, exits 1 instead of raising."""
        inv = self._inventory(corpus, tmp_path)
        document = json.loads(inv.read_text(encoding="utf-8"))
        mutate(document)  # type: ignore[operator]
        inv.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            ci.main(["select", "--inventory", str(inv)])
        assert (exc.value.code, "BLOCKED" in capsys.readouterr().out) == (1, True)

    def test_hand_edited_load_class_in_the_inventory_is_caught_by_the_rebuild(
        self, corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Making a skill's items look always-loaded cannot make them mergeable: the inventory is rebuilt and
        compared."""
        inv = self._inventory(corpus, tmp_path)
        document = json.loads(inv.read_text(encoding="utf-8"))
        for item in document["items"]:
            if item["file"].startswith("plugins/cc_a/skills/demo/"):
                item["group"] = "always:cc_a:"
                item["load"]["hierarchy"] = "always"
        inv.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(SystemExit):
            ci.main(["validate", "--inventory", str(inv), "--clusters", str(tmp_path / "none.json")])
        assert "items differs from a rebuild" in capsys.readouterr().out

    def test_judge_flags_cannot_be_restored_by_hand(
        self, corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The second of two overlapping clusters is not judged; setting its flags back is refused by recomputation."""
        inv = self._inventory(corpus, tmp_path)
        inventory = json.loads(inv.read_text(encoding="utf-8"))
        a, b = (_id(inventory, "plugins/cc_a/rules/always.md", title) for title in ("One", "Two"))
        c = _id(inventory, "plugins/cc_a/rules/cited.md", "Other Rule")
        vetted = ci.validate(inventory, [{"id": "first", "items": [a, b]}, {"id": "second", "items": [b, c]}], 0)
        second = next(c for c in vetted["clusters"] if not c["ok"])  # type: ignore[attr-defined]
        assert second["judge"] is False
        second["ok"] = second["judge"] = True
        assert ci.main(self._settle_args(inv, tmp_path, vetted)) == 1
        assert "differs from validate's result" in capsys.readouterr().out


class TestAdjacentGuards:
    """Covers changes that hide a cited heading outside the cluster by changing how the rest of the file reads."""

    C = "plugins/cc_a/rules/c.md"

    @pytest.fixture
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        """Build ``c.md`` with an editable member followed by a cited section outside the cluster."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        root = tmp_path / "repo"
        _write(root, self.C, "## Member\n\n" + "m" * 300 + "\n\n## Adjacent\n\nNever delete.\n")
        _write(root, "plugins/cc_a/rules/b.md", "## Beta Rule\n\n" + "y" * 300 + "\n")
        _write(root, "plugins/cc_a/skills/demo/SKILL.md", "## Steps\n\ncites c.md §Adjacent\n")
        inventory = ci.build_inventory(root, ci.ALL_KINDS)
        member, beta = _id(inventory, self.C, "Member"), _id(inventory, "plugins/cc_a/rules/b.md", "Beta Rule")
        proposals = tmp_path / "proposals"
        proposals.mkdir()
        vetted = ci.validate(inventory, [{"id": "mb", "items": [member, beta]}], 0)
        return {"root": root, "vetted": vetted, "member": member, "beta": beta, "proposals": proposals}

    def _settle(self, env: dict[str, object], judged: dict[str, object]) -> dict[str, object]:
        """Settle one judgment of cluster ``mb`` against the real files and return its record."""
        judgments = [{"cluster": "mb"} | judged]
        document, _ = ci.settle(env["vetted"], judgments, env["proposals"], [], env["root"])  # type: ignore[arg-type]
        return document["clusters"][0]  # type: ignore[index]

    @pytest.mark.parametrize(
        "after",
        [
            pytest.param("```", id="opening-fence"),
            pytest.param("<!--", id="opening-comment"),
        ],
    )
    def test_add_that_swallows_the_next_section_is_rejected(self, env: dict[str, object], after: str) -> None:
        """A pure add at the end of the member hides the adjacent cited heading, with no repoint and no review."""
        edit = {"file": self.C, "action": "add", "lines": "4", "before": "", "after": after}
        record = self._settle(env, {"decision": "sharpen", "edits": [edit]})
        assert (record["decision"], "Adjacent" in " ".join(record["downgrades"])) == ("rejected", True)

    @pytest.mark.parametrize("closing", ["```", "<!-- unterminated"])
    def test_merge_proposal_that_swallows_the_next_section_is_downgraded(
        self, env: dict[str, object], closing: str
    ) -> None:
        """The proposal replaces the survivor's lines, so an unclosed fence or comment in it hides what follows."""
        (env["proposals"] / "mb.md").write_text(f"## Member\n\nshort merged text\n{closing}\n", encoding="utf-8")  # type: ignore[operator]
        obligations = [
            {"original": "o", "from": env[key], "to": "short merged text", "modal_preserved": True}
            for key in ("member", "beta")
        ]
        judged = {"decision": "merge-prune", "survivor": env["member"], "obligation_map": obligations}
        record = self._settle(env, judged)
        assert (record["decision"], "cited by plugins/cc_a/skills/demo/SKILL.md" in " ".join(record["downgrades"])) == (
            "sharpen",
            True,
        )

    def test_clean_merge_proposal_stands(self, env: dict[str, object]) -> None:
        """The same merge without the unclosed fence leaves the adjacent heading alone."""
        (env["proposals"] / "mb.md").write_text("## Member\n\nshort merged text\n", encoding="utf-8")  # type: ignore[operator]
        obligations = [
            {"original": "o", "from": env[key], "to": "short merged text", "modal_preserved": True}
            for key in ("member", "beta")
        ]
        judged = {"decision": "merge-prune", "survivor": env["member"], "obligation_map": obligations}
        assert self._settle(env, judged)["decision"] == "merge-prune"


class TestCitationLists:
    """Covers one file mention with several headings, several mentions on a line, and quoted headings."""

    @staticmethod
    def _cited(tmp_path: Path, line: str) -> dict[str, list[str]]:
        """Return ``{file: cited heading list}`` for the corpus ``a.md`` / ``b.md`` and one citing ``line``."""
        for name, head in (("a.md", "Alpha"), ("b.md", "Beta")):
            _write(tmp_path, f"plugins/cc_a/rules/{name}", f"## {head}\n\ntext\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", f"## Steps\n\n{line}\n")
        inventory = ci.build_inventory(tmp_path, ci.ALL_KINDS)
        return {
            item["file"].rsplit("/", 1)[-1]: [g["title"] for g in item["guarded"]]
            for item in inventory["items"]  # type: ignore[attr-defined]
            if item["guarded"]
        }

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param("a.md §Alpha, b.md §Beta", {"a.md": ["Alpha"], "b.md": ["Beta"]}, id="two-files-one-line"),
            pytest.param('a.md §"Alpha"', {"a.md": ["Alpha"]}, id="double-quoted"),
            pytest.param("a.md §*Alpha*", {"a.md": ["Alpha"]}, id="emphasis"),
        ],
    )
    def test_each_heading_is_attributed_to_its_own_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str, expected: dict[str, list[str]]
    ) -> None:
        """A heading cited after one file name never lands on the previous file; quotes are not part of a title."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        assert self._cited(tmp_path, line) == expected


class TestWriteFences:
    """Covers the rule that this mode writes only run artifacts, never into the instruction sources."""

    def test_output_path_inside_the_sources_is_blocked(self, corpus: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``inventory --out <a rule file>`` would overwrite a source, so it exits 1 and leaves the file alone."""
        target = corpus / "plugins/cc_a/rules/always.md"
        before = target.read_bytes()
        with pytest.raises(SystemExit) as exc:
            ci.main(["inventory", "--root", str(corpus), "--out", str(target)])
        assert (exc.value.code, target.read_bytes() == before) == (1, True)
        assert "run artifact location" in capsys.readouterr().out

    def test_symlink_into_the_sources_is_blocked(self, corpus: Path, tmp_path: Path) -> None:
        """A run-directory file that is a symlink to a source resolves into the sources and is refused."""
        target = corpus / "plugins/cc_a/rules/always.md"
        link = tmp_path / "out.json"
        link.symlink_to(target)
        before = target.read_bytes()
        with pytest.raises(SystemExit) as exc:
            ci.main(["inventory", "--root", str(corpus), "--out", str(link)])
        assert (exc.value.code, target.read_bytes() == before) == (1, True)

    def test_proposals_directory_inside_the_sources_is_blocked(
        self, corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Obligation files are written beside the proposals, so that directory may not be a source directory."""
        inv = tmp_path / "inv.json"
        assert ci.main(["inventory", "--root", str(corpus), "--out", str(inv)]) == 0
        (tmp_path / "v.json").write_text(
            json.dumps(ci.validate(json.loads(inv.read_text(encoding="utf-8")), [])), encoding="utf-8"
        )
        (tmp_path / "j.json").write_text("[]", encoding="utf-8")
        args = [
            "settle",
            "--inventory",
            str(inv),
            "--vetted",
            str(tmp_path / "v.json"),
            "--judgments",
            str(tmp_path / "j.json"),
        ]
        assert ci.main([*args, "--proposals-dir", str(corpus / "plugins/cc_a/rules")]) == 1
        assert "run artifact location" in capsys.readouterr().out


class TestUnreadableArtifacts:
    """Covers files that are not JSON at all."""

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(b"\xff\xfe\x00binary", id="binary"),
            pytest.param(b"1" * 10000, id="ten-thousand-digit-integer"),
            pytest.param(b"[" * 100000, id="deeply-nested"),
        ],
    )
    def test_non_json_content_is_blocked_not_a_crash(
        self, tmp_path: Path, content: bytes, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Binary data, an absurd integer or runaway nesting exits 1 with a diagnostic."""
        bad = tmp_path / "bad.json"
        bad.write_bytes(content)
        with pytest.raises(SystemExit) as exc:
            ci.main(["select", "--inventory", str(bad)])
        assert (exc.value.code, "BLOCKED" in capsys.readouterr().out) == (1, True)


class TestWriteAllowList:
    """Covers the allow list: run artifacts only, whatever else lies in or outside the tree."""

    @pytest.mark.parametrize(
        "relative",
        [
            pytest.param("AGENTS.md", id="root-agents-file"),
            pytest.param("CLAUDE.md", id="root-claude-file"),
            pytest.param("README.md", id="root-readme"),
        ],
    )
    def test_other_files_of_the_tree_are_not_writable(self, corpus: Path, relative: str) -> None:
        """Only ``.temp``, ``.reports`` and the system temp directory outside the tree are run-artifact places."""
        assert ci._allowed_output(corpus / relative, corpus) is False

    def test_run_artifact_places_are_writable(self, corpus: Path, tmp_path: Path) -> None:
        """``.temp/`` and ``.reports/`` of the tree, and a temp directory outside it, are allowed."""
        assert [
            ci._allowed_output(corpus / ".temp/x.json", corpus),
            ci._allowed_output(corpus / ".reports/audit/x.json", corpus),
            ci._allowed_output(tmp_path / "x.json", corpus),
        ] == [True, True, True]

    def test_symlink_in_the_run_directory_to_another_file_is_not_writable(self, corpus: Path) -> None:
        """A link under ``.temp`` that points at the root AGENTS.md resolves there and is refused."""
        (corpus / ".temp").mkdir()
        (corpus / "AGENTS.md").write_text("x", encoding="utf-8")
        (corpus / ".temp/link.json").symlink_to(corpus / "AGENTS.md")
        assert ci._allowed_output(corpus / ".temp/link.json", corpus) is False


class TestHeadingEdgeCases:
    """Covers heading and citation spellings that earlier readers missed."""

    @pytest.mark.parametrize(
        ("text", "title"),
        [
            pytest.param("## Alpha#", "Alpha#", id="hash-without-space-is-part-of-the-title"),
            pytest.param("## Alpha ##", "Alpha", id="closing-hashes-after-a-space"),
        ],
    )
    def test_closing_hashes_need_a_space_before_them(self, text: str, title: str) -> None:
        """``## Alpha#`` is the title ``Alpha#``; only a run after whitespace is a closing sequence."""
        assert ci._headings(text) == [(title, 1)]

    def test_code_quoted_citation_is_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A heading cited in backticks guards the heading just as the plain spelling does."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/rules/a.md", "## Alpha\n\ntext\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\ncites a.md §`Alpha`\n")
        inventory = ci.build_inventory(tmp_path, ci.ALL_KINDS)
        guarded = [g["title"] for i in inventory["items"] if i["file"].endswith("a.md") for g in i["guarded"]]  # type: ignore[attr-defined]
        assert guarded == ["Alpha"]

    def test_symlink_pointing_outside_the_tree_is_not_inventoried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A source symlinked from outside the tree cannot be read as part of it, so it is listed as excluded."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        root = tmp_path / "repo"
        outside = tmp_path / "elsewhere.md"
        outside.write_text("## Out\n\ntext\n", encoding="utf-8")
        _write(root, "plugins/cc_a/rules/real.md", "## Real\n\ntext\n")
        (root / "plugins/cc_a/rules/linked.md").symlink_to(outside)
        inventory = ci.build_inventory(root, ci.ALL_KINDS)
        assert ({i["file"] for i in inventory["items"]}, inventory["excluded"]) == (  # type: ignore[attr-defined]
            {"plugins/cc_a/rules/real.md"},
            ["plugins/cc_a/rules/linked.md"],
        )


class TestOccurrenceSurvival:
    """Covers equal headings: deleting one occurrence is a loss even though another remains."""

    def test_deleting_one_of_two_equal_headings_is_a_loss(self) -> None:
        """The surviving level-5 ``Anchor`` heading must not vouch for the deleted one."""
        text = "## S\n\n##### Anchor\n\none\n\n##### Anchor\n\ntwo\n"
        guard = {"id": "f#0", "file": "f.md", "title": "Anchor", "line": 3, "refs": ["x.md"]}
        edit = {"file": "f.md", "action": "delete", "lines": "3-5", "before": "x", "after": ""}
        assert ci._lost_headings([guard], [edit], text) == [guard]


class TestSourceRangeTransfer:
    """Covers the move check, which must read the real lines of an edit's range, not the judge's quote."""

    def test_removed_text_comes_from_the_source_range(self) -> None:
        """An edit that quotes nothing still removes the characters of the lines it names."""
        edit = {"action": "rewrite", "lines": "2-2", "before": "", "after": "short"}
        sources = {"f.md": "line one\nthis obligation must never be dropped here\nline three\n"}
        assert ci._removed_chars(edit, "f.md", sources) == len("this obligation must never be dropped here")

    def test_without_the_source_the_judges_quote_is_used(self) -> None:
        """Library callers without a repository root keep the quote-based count."""
        edit = {"action": "delete", "lines": "2", "before": "abc  def", "after": ""}
        assert ci._removed_chars(edit, "f.md", None) == len("abc def")


class TestUnreadableProposal:
    """Covers a proposal file that is not UTF-8 text."""

    def test_binary_proposal_is_a_downgrade_not_a_crash(self, tmp_path: Path) -> None:
        """A proposal holding byte 0xff reads as missing text, so the merge is downgraded."""
        bad = tmp_path / "p.md"
        bad.write_bytes(b"\xff\xfe")
        vet = {"merge_allowed": True, "members": [], "inbound": {}, "guarded": [], "file_guarded": []}
        vet |= {"locations": [], "tokens_before": 10}
        assert ci._read_text(bad) is None
        assert any("not UTF-8" in reason for reason in ci._merge_blockers({"survivor": "x"}, vet, None))


class TestMergeOccurrenceCount:
    """Covers a merge proposal that keeps fewer equal cited headings than the survivor had."""

    @pytest.mark.parametrize(
        ("proposal", "blocked"),
        [
            pytest.param("##### Alpha\n\nkept\n", True, id="one-of-two-kept"),
            pytest.param("##### Alpha\n\none\n\n##### Alpha\n\ntwo\n", False, id="both-kept"),
        ],
    )
    def test_proposal_keeps_every_cited_occurrence(self, proposal: str, blocked: bool) -> None:
        """Two cited ``Alpha`` headings in the survivor: one in the proposal does not vouch for the other."""
        guard = {"id": "f#0", "file": "f.md", "title": "Alpha", "line": 3, "refs": ["x.md"]}
        vet = {"merge_allowed": True, "members": ["f#0"], "inbound": {}, "guarded": [guard, dict(guard, line=7)]}
        vet |= {"file_guarded": [], "locations": [], "tokens_before": 100}
        reasons = ci._merge_blockers({"survivor": "f#0"}, vet, proposal)
        assert any("cited by x.md" in reason for reason in reasons) is blocked


class TestQuotedHeadingLists:
    """Covers a citation that names several code-quoted headings after one file."""

    def test_each_quoted_heading_of_a_list_is_a_mention(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``a.md §`Alpha`, §`Beta``` gives Alpha and Beta, not only the first."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\nsee a.md §`Alpha`, §`Beta` for both\n")
        assert [(m.path, m.heading) for m in ci._mentions(tmp_path)] == [("a.md", "alpha"), ("a.md", "beta")]


class TestHeadingReaderCommonMark:
    """Covers heading and fence spellings the reader must follow as CommonMark does."""

    @pytest.mark.parametrize(
        ("text", "titles"),
        [
            pytest.param(
                "## Target\n\n    ```\n\n## Guarded\nnever delete\n",
                ["Target", "Guarded"],
                id="indented-code-is-not-a-fence",
            ),
            pytest.param(
                "## Target\n\n```a`b\n\n## Guarded\nnever delete\n",
                ["Target", "Guarded"],
                id="backtick-in-info-string-is-not-a-fence",
            ),
            pytest.param(
                "# Target\nGuarded\n---\nnever delete\n", ["Target", "Guarded"], id="setext-right-under-a-heading"
            ),
            pytest.param("first line\nsecond line\n---\n", ["first line second line"], id="multi-line-setext"),
            pytest.param("## Target\n\n```\n## Hidden\n```\n", ["Target"], id="real-fence-still-hides-headings"),
        ],
    )
    def test_headings_found(self, text: str, titles: list[str]) -> None:
        """The reader finds every heading CommonMark does and none inside a real fence."""
        assert [title for title, _ in ci._headings(text)] == titles


class TestWriteReplacesTheName:
    """Covers writes that must never reach through a link standing at the output name."""

    def test_symlink_at_the_output_name_is_replaced(self, tmp_path: Path) -> None:
        """A link planted at the output name leaves the file it points at unchanged."""
        target = tmp_path / "AGENTS.md"
        target.write_text("instructions\n", encoding="utf-8")
        (tmp_path / "out.json").symlink_to(target)
        ci._write_artifact(tmp_path / "out.json", "{}\n")
        assert (target.read_text(encoding="utf-8"), (tmp_path / "out.json").is_symlink()) == ("instructions\n", False)

    def test_hard_link_at_the_output_name_is_replaced(self, tmp_path: Path) -> None:
        """A second name for a source's inode keeps the source content when the output is rewritten."""
        source = tmp_path / "AGENTS.md"
        source.write_text("instructions\n", encoding="utf-8")
        (tmp_path / "out.json").hardlink_to(source)
        ci._write_artifact(tmp_path / "out.json", "{}\n")
        assert (source.read_text(encoding="utf-8"), (tmp_path / "out.json").read_text(encoding="utf-8")) == (
            "instructions\n",
            "{}\n",
        )


class TestRunAnchor:
    """Covers the rule that a run's later steps write only beside the run's own inventory."""

    def test_another_runs_directory_is_refused(self, corpus: Path) -> None:
        """Both runs sit under ``.reports``, but a step of run A may not write into run B."""
        run_a = corpus / ".reports/audit/a/consolidate"
        run_b = corpus / ".reports/audit/b/consolidate"
        assert [
            ci._allowed_output(run_a / "x.json", corpus, run_a),
            ci._allowed_output(run_b / "x.json", corpus, run_a),
        ] == [True, False]


class TestStash:
    """Covers the subcommand that saves the run directory for later shell blocks."""

    def test_writes_the_line_under_a_run_artifact_location(self, corpus: Path, tmp_path: Path) -> None:
        """The text lands in the named file with a trailing newline."""
        target = tmp_path / "state" / "run-dir"
        assert ci.main(["stash", "--root", str(corpus), "--path", str(target), "--text", "RUN"]) == 0
        assert target.read_text(encoding="utf-8") == "RUN\n"

    def test_refuses_an_instruction_file_of_the_tree(self, corpus: Path) -> None:
        """A path naming the tree's AGENTS.md is blocked and the file is not written."""
        (corpus / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
        code = ci.main(["stash", "--root", str(corpus), "--path", str(corpus / "AGENTS.md"), "--text", "x"])
        assert (code, (corpus / "AGENTS.md").read_text(encoding="utf-8")) == (1, "instructions\n")

    def test_refuses_a_planted_symlink_into_the_tree(self, corpus: Path, tmp_path: Path) -> None:
        """A sentinel pre-planted as a link to AGENTS.md resolves into the tree, so it is blocked and nothing is
        written."""
        target = corpus / "AGENTS.md"
        target.write_text("instructions\n", encoding="utf-8")
        sentinel = tmp_path / "state" / "run-dir"
        sentinel.parent.mkdir()
        sentinel.symlink_to(target)
        code = ci.main(["stash", "--root", str(corpus), "--path", str(sentinel), "--text", "RUN"])
        assert (code, target.read_text(encoding="utf-8")) == (1, "instructions\n")


class TestRound13Readers:
    """Covers quoted citation lists, headings that start with punctuation, and unprintable text."""

    @pytest.mark.parametrize(
        ("citation", "headings"),
        [
            pytest.param('a.md §"CI", §"QA"', ["ci", "qa"], id="double-quoted-short-titles"),
            pytest.param("a.md §'CI', §'QA'", ["ci", "qa"], id="single-quoted-short-titles"),
            pytest.param("a.md §*CI*, §*QA*", ["ci", "qa"], id="emphasised-short-titles"),
        ],
    )
    def test_closing_marks_are_not_part_of_the_title(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, citation: str, headings: list[str]
    ) -> None:
        """A quote or emphasis mark closing a cited title before a comma is dropped from the title."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", f"## Steps\n\nsee {citation} here\n")
        assert [m.heading for m in ci._mentions(tmp_path)] == headings

    @pytest.mark.parametrize(
        ("text", "titles"),
        [
            pytest.param("!Alpha\n---\nMust stop.\n", ["!Alpha"], id="bang-title"),
            pytest.param("*Alpha* text\n===\nbody\n", ["*Alpha* text"], id="emphasis-first-title"),
            pytest.param("#hashtag\n---\nbody\n", ["#hashtag"], id="hash-without-space-is-text"),
            pytest.param("- item\n---\nbody\n", [], id="list-item-then-rule-is-no-heading"),
        ],
    )
    def test_setext_paragraph_starts_follow_commonmark(self, text: str, titles: list[str]) -> None:
        """Only a real block starter keeps a line from being the heading text above an underline."""
        assert [title for title, _ in ci._headings(text)] == titles

    def test_printing_an_unencodable_character_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A lone surrogate in a model-written id prints as an escape instead of ending the run."""
        stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="\n")  # Windows would write CRLF by default
        monkeypatch.setattr(ci.sys, "stdout", stream)
        ci._tolerate_any_text()
        print("id-\ud800")
        stream.flush()
        assert stream.buffer.getvalue() == b"id-\\ud800\n"  # type: ignore[attr-defined]

    @pytest.mark.parametrize("field", ["reason", "survivor", "repoint", "edits", "obligation_map"])
    def test_null_field_makes_the_judgment_malformed(self, field: str) -> None:
        """A present field set to null is the wrong type, not an absent one."""
        _, problems = ci.normalize_judgment({"cluster": "c", "decision": "keep", field: None})
        assert problems


class TestHtmlBlocksAndSetextRuns:
    """Covers CommonMark constructs that change which lines are headings."""

    @pytest.mark.parametrize(
        ("text", "titles"),
        [
            pytest.param("Alpha\n---\nBeta\n---\n", ["Alpha", "Beta"], id="two-setext-headings-in-a-row"),
            pytest.param("<pre>\n## CI\n</pre>\n## After\n", ["After"], id="pre-block-hides-its-heading"),
            pytest.param("<div>\n## CI\n\n## Real\n", ["Real"], id="block-tag-runs-to-the-blank-line"),
            pytest.param("<span>\n## CI\n\n## Real\n", ["Real"], id="complete-tag-alone-runs-to-the-blank-line"),
            pytest.param("text <b>x</b>\n## H\n", ["H"], id="inline-html-is-no-block"),
            pytest.param("<pre>one</pre>\n## H\n", ["H"], id="block-closed-on-its-own-line"),
            pytest.param("<!-- note -->\n## H\n", ["H"], id="comment-line-keeps-the-next-heading"),
        ],
    )
    def test_headings_found(self, text: str, titles: list[str]) -> None:
        """A heading counts only where CommonMark makes it one."""
        assert [title for title, _ in ci._headings(text)] == titles

    def test_opening_an_html_block_before_a_cited_heading_loses_it(self) -> None:
        """Adding ``<pre>`` right above ``## CI`` hides the heading, so the edit needs a repoint."""
        guard = {"id": "f#0", "file": "f.md", "title": "CI", "line": 1, "refs": ["x.md"]}
        edit = {"file": "f.md", "action": "add", "lines": "1", "before": "", "after": "<pre>"}
        assert ci._lost_headings([guard], [edit], "## CI\nbody\n") == [guard]

    def test_quoted_title_may_hold_a_file_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``a.md §"CI.md"`` cites a heading called CI.md; the dot-md inside the quotes is not the next file."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", '## Steps\n\nsee a.md §"CI.md" here\n')
        assert [(m.path, m.heading) for m in ci._mentions(tmp_path)] == [("a.md", "ci")]

    def test_next_file_with_its_own_citation_still_splits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``a.md §A, b.md §B`` still gives A to a.md and B to b.md."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\nsee a.md §Alpha, b.md §Beta.\n")
        assert [(m.path, m.heading) for m in ci._mentions(tmp_path)] == [("a.md", "alpha"), ("b.md", "beta")]


def _json_file(path: Path) -> object:
    """Return the parsed JSON a test run wrote."""
    return json.loads(path.read_text(encoding="utf-8"))


class TestCollect:
    """Covers the subcommand that joins the lists parallel agents wrote, in place of a shell redirect."""

    def _run(self, corpus: Path, folder: Path, out: Path) -> int:
        return ci.main(
            ["collect", "--root", str(corpus), "--dir", str(folder), "--glob", "clusters-*.json", "--out", str(out)]
        )

    def test_joins_matching_lists_in_name_order(self, corpus: Path, tmp_path: Path) -> None:
        """Two agent files and one unrelated file give the two lists joined, nothing else."""
        folder = tmp_path / "run"
        folder.mkdir()
        (folder / "clusters-b.json").write_text('[{"b": 2}]', encoding="utf-8")
        (folder / "clusters-a.json").write_text('[{"a": 1}]', encoding="utf-8")
        (folder / "other.json").write_text('[{"c": 3}]', encoding="utf-8")
        assert (self._run(corpus, folder, folder / "clusters.json"), _json_file(folder / "clusters.json")) == (
            0,
            [{"a": 1}, {"b": 2}],
        )

    def test_no_match_gives_an_empty_list(self, corpus: Path, tmp_path: Path) -> None:
        """When every agent timed out the joined list is empty, not an error."""
        folder = tmp_path / "run"
        folder.mkdir()
        assert (self._run(corpus, folder, folder / "clusters.json"), _json_file(folder / "clusters.json")) == (0, [])

    def test_output_outside_the_run_directory_is_refused(self, corpus: Path, tmp_path: Path) -> None:
        """An output named elsewhere, such as the tree's AGENTS.md, is blocked and the file is not touched."""
        folder = tmp_path / "run"
        folder.mkdir()
        (corpus / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
        code = self._run(corpus, folder, corpus / "AGENTS.md")
        assert (code, (corpus / "AGENTS.md").read_text(encoding="utf-8")) == (1, "instructions\n")

    def test_linked_output_name_is_replaced_not_followed(self, corpus: Path, tmp_path: Path) -> None:
        """A hard link planted at the output name leaves the file it shares an inode with unchanged."""
        folder = tmp_path / "run"
        folder.mkdir()
        shared = tmp_path / "shared.md"
        shared.write_text("instructions\n", encoding="utf-8")
        (folder / "clusters.json").hardlink_to(shared)
        self._run(corpus, folder, folder / "clusters.json")
        assert shared.read_text(encoding="utf-8") == "instructions\n"


class TestContainerHeadings:
    """Covers headings inside blockquotes and list items, which are headings in CommonMark."""

    @pytest.mark.parametrize(
        ("text", "titles"),
        [
            pytest.param("## Wrapper\n\n> ##### CI\n> Must run checks.\n", ["Wrapper", "CI"], id="blockquote-heading"),
            pytest.param("## Wrapper\n\n- ##### CI\n  Must run checks.\n", ["Wrapper", "CI"], id="list-item-heading"),
            pytest.param("> - ## Deep\n", ["Deep"], id="nested-containers"),
            pytest.param("> Alpha\n> ---\n", ["Alpha"], id="blockquote-setext"),
            pytest.param("- item\n---\n", [], id="rule-after-a-list-item-is-no-underline"),
            pytest.param("> ```\n> ## Hidden\n> ```\n", [], id="fence-inside-a-blockquote-hides-its-lines"),
        ],
    )
    def test_headings_found(self, text: str, titles: list[str]) -> None:
        """A heading inside a container counts, at any level, and a container fence still hides its lines."""
        assert [title for title, _ in ci._headings(text)] == titles

    def test_cited_container_heading_is_guarded_and_its_deletion_is_a_loss(self) -> None:
        """A cited ``> ##### CI`` inside a section guards that line; deleting it needs a repoint."""
        body = "> ##### CI\n> Must run checks.\n"
        index = {"f.md": [("ci", "x.md")]}
        guards = ci._guarded("f.md", "Wrapper", 1, body, index)
        edit = {"file": "f.md", "action": "delete", "lines": "1-2", "before": "x", "after": ""}
        assert (
            [g["title"] for g in guards],
            ci._lost_headings([dict(g, file="f.md", id="f#0") for g in guards], [edit], body),
        ) == (
            ["CI"],
            [dict(guards[0], file="f.md", id="f#0")],
        )


class TestCitationRunningOnIntoProse:
    """Covers a citation whose text continues after the title (``§CI and b.md §QA``)."""

    @pytest.mark.parametrize(
        ("wanted", "cited", "same"),
        [
            pytest.param("ci", "ci and", True, id="conjunction-after-a-short-title"),
            pytest.param("ci", "ci", True, id="equal"),
            pytest.param("ci setup notes", "ci", False, id="short-citation-does-not-claim-a-longer-heading"),
            pytest.param("ci", "cipher suite", False, id="no-word-boundary-no-match"),
        ],
    )
    def test_same_heading(self, wanted: str, cited: str, same: bool) -> None:
        """A title followed by a space and prose still names the title; a longer heading is not claimed by a short
        one."""
        assert ci._same_heading(wanted, cited) is same


class TestBlockReader:
    """Covers the one CommonMark block reader behind every heading question (containers, fences, lazy lines)."""

    @pytest.mark.parametrize(
        ("text", "titles"),
        [
            pytest.param(
                "## Wrapper\n\n- outer\n\n    ##### CI\n    Must check.\n",
                ["Wrapper", "CI"],
                id="heading-in-a-list-item-continuation",
            ),
            pytest.param("- ```\n  code\n  ```\n\n##### CI\n", ["CI"], id="item-fence-closes-with-its-item"),
            pytest.param("1.\n\n\t##### CI\n", [], id="empty-item-then-blank-is-closed"),
            pytest.param("1.\n  ##### CI\n", ["CI"], id="empty-item-continues-on-the-next-line"),
            pytest.param("> Alpha\n===\n", [], id="underline-after-a-lazy-quote-line-is-text"),
            pytest.param("[ref]: http://x\n===\n", [], id="definition-is-not-a-setext-title"),
            pytest.param("text\n2. ##### CI\n", [], id="ordered-item-not-at-one-cannot-interrupt"),
            pytest.param("- a\n2. ##### CI\n", ["CI"], id="ordered-item-after-an-item-starts-a-list"),
            pytest.param("- a\n    ##### CI\n", ["CI"], id="indented-heading-in-an-item"),
            pytest.param("<pre>\n## Hidden\n</pre>\n## Shown\n", ["Shown"], id="html-block-hides-its-lines"),
            pytest.param("<!--\n## Hidden\n-->\n## Shown\n", ["Shown"], id="comment-block-hides-its-lines"),
            pytest.param("## Title <!-- note -->\n", ["Title"], id="inline-comment-dropped-from-the-title"),
        ],
    )
    def test_headings_found(self, text: str, titles: list[str]) -> None:
        """The reader returns the headings a CommonMark renderer shows, in line order, and no others."""
        assert [title for title, _ in ci._headings(text)] == titles

    def test_deleting_a_cited_list_continuation_heading_is_a_loss(self) -> None:
        """A cited level-5 heading indented under a list item guards its line like any other heading."""
        body = "- outer\n\n    ##### CI\n    Must check.\n"
        index = {"f.md": [("ci", "x.md")]}
        guards = [dict(g, file="f.md", id="f#0") for g in ci._guarded("f.md", "Wrapper", 1, body, index)]
        edit = {"file": "f.md", "action": "delete", "lines": "3-4", "before": "x", "after": ""}
        assert [g["title"] for g in ci._lost_headings(guards, [edit], body)] == ["CI"]

    def test_section_split_ignores_container_headings(self) -> None:
        """A heading inside a container is never a section boundary."""
        text = "## One\n\n- ## Inner\n\n## Two\n"
        assert [title for title, *_ in ci.split_sections(text)] == ["One", "Two"]

    def test_agrees_with_a_reference_parser(self) -> None:
        """Every three-line mix of container, fence, HTML and paragraph lines reads as markdown-it (CommonMark) reads
        it.

        The fragments cover each construct the guard depends on; longer known counterexamples are listed beside them.
        Skipped when markdown-it is not installed.
        """
        markdown_it = pytest.importorskip("markdown_it")
        fragments = [
            "", "##### CI", "    ##### CI", "\t##### CI", "- item", "  ##### CI", "- ```", "  ```", "```", "> quote",
            "> ##### CI", "<pre>", "</pre>", "<!--", "-->", "Setext", "---", "===", "1.", "2. ##### CI",
            "[ref]: http://x", "  Setext in item", "[ref]: <foo", "a\u2028```", "\u00a0", "[a[b]: /url",
        ]  # fmt: skip
        known = [
            "## Wrapper\n\n- outer\n\n    ##### CI\n    Must check.\n",
            "- ```\n  code\n  ```\n\n##### CI\n",
            "1.\n\n\t##### CI\n",
            "   1. num2\n    - ##### CI\n</pre>\n>### CI\n",
            "> Setext in quote\n    ##### CI\n> ---\n",
        ]
        parser = markdown_it.MarkdownIt("commonmark")
        documents = ["\n".join(lines) + "\n" for lines in itertools.product(fragments, repeat=3)] + known
        mismatches = []
        for text in documents:
            tokens = parser.parse(text)
            want = {
                (tok.map[0], " ".join(tokens[pos + 1].content.split()))
                for pos, tok in enumerate(tokens)
                if tok.type == "heading_open" and tokens[pos + 1].content.strip()
            }
            got = {(idx, " ".join(title.split())) for idx, _, title, _ in ci._scan_blocks(ci._lines(text))}
            if got != want:
                mismatches.append(text)
        assert mismatches == []


class TestInlineMarkupTitles:
    """Covers a heading written with emphasis, code ticks or a link, cited by its plain words."""

    @pytest.mark.parametrize(
        ("heading", "cited"),
        [
            pytest.param("**CI**", "CI", id="bold-heading-plain-citation"),
            pytest.param("`CI`", "CI", id="code-heading-plain-citation"),
            pytest.param("CI", "**CI**", id="plain-heading-bold-citation"),
            pytest.param("[CI](http://x)", "CI", id="link-heading"),
            pytest.param("_CI_", "CI", id="underscore-emphasis"),
            pytest.param("<code>CI</code>", "CI", id="html-tags"),
            pytest.param("Q&amp;A", "Q&A", id="html-entity"),
            pytest.param("![logo](x.png) CI", "logo CI", id="image-keeps-its-alt-text"),
            pytest.param("[CI][ref]", "CI", id="reference-link"),
            pytest.param("\uff23\uff29", "CI", id="fullwidth-letters"),
        ],
    )
    def test_equal_after_inline_markup(self, heading: str, cited: str) -> None:
        """Both sides reduce to the same words, so the citation is found and the heading is guarded."""
        assert (ci._normalize_title(heading), ci._full_title(heading)) == (
            ci._normalize_title(cited),
            ci._full_title(cited),
        )

    def test_snake_case_title_keeps_its_underscores(self) -> None:
        """Underscores inside a word are part of the title, not emphasis."""
        assert ci._normalize_title("run_all_checks") == "run_all_checks"

    @pytest.mark.parametrize("heading", [pytest.param("**CI**", id="bold"), pytest.param("`CI`", id="code")])
    def test_deleting_a_marked_up_cited_heading_is_a_loss(self, heading: str) -> None:
        """A cited heading written with markup still guards its section: deleting it needs a repoint."""
        body = f"##### {heading}\nMust check.\n"
        index = {"f.md": [(ci._normalize_title("CI"), "x.md")]}
        guards = [dict(g, file="f.md", id="f#0") for g in ci._guarded("f.md", "Wrapper", 1, body, index)]
        edit = {"file": "f.md", "action": "delete", "lines": "1-2", "before": "x", "after": ""}
        assert [g["title"] for g in ci._lost_headings(guards, [edit], body)] == [heading]


class TestFlowListComments:
    """Covers a flow-list `paths:` whose comment or quoted item holds a closing bracket."""

    @pytest.mark.parametrize(
        ("front", "globs"),
        [
            pytest.param("paths: [src/**] # , docs/**]", ["src/**"], id="comment-holds-a-bracket"),
            pytest.param("paths: ['src/**]', 'docs/**']", ["src/**]", "docs/**"], id="quoted-item-holds-a-bracket"),
            pytest.param("paths: [a, # c\n  b]", ["a", "b"], id="comment-line-inside-a-multi-line-list"),
            pytest.param('paths: ["a#b", c]', ["a#b", "c"], id="hash-inside-quotes-is-text"),
            pytest.param("paths: [a, b]  # note", ["a", "b"], id="trailing-comment"),
        ],
    )
    def test_globs_read(self, front: str, globs: list[str]) -> None:
        """The list ends at the bracket YAML ends it on, and comments never become globs."""
        assert ci.parse_paths(f"---\n{front}\n---\n") == globs

    @pytest.mark.parametrize(
        "front",
        [
            pytest.param("paths: [a]x", id="text-after-the-list"),
            pytest.param("paths: [[a]]", id="nested-list"),
            pytest.param("paths: [a, b", id="unterminated"),
            pytest.param("paths: [a]#c", id="comment-without-a-space"),
        ],
    )
    def test_unreadable_list_is_unparsed(self, front: str) -> None:
        """Anything the reader cannot place stays unparsed rather than guessed."""
        assert ci.parse_paths(f"---\n{front}\n---\n") == [ci.UNPARSED_PATHS]

    def test_bracket_in_comment_does_not_merge_two_scopes(self) -> None:
        """Two rules whose scopes differ only by a bracket in a comment or a quote never share a group."""
        comment = ci.parse_paths("---\npaths: [src/**] # , docs/**]\n---\n")
        quoted = ci.parse_paths("---\npaths: ['src/**]', 'docs/**']\n---\n")
        assert comment != quoted


class TestMarkdownLineAndBlankRules:
    """Covers what Markdown calls a line break, a blank line and a link reference definition."""

    @pytest.mark.parametrize(
        ("text", "titles"),
        [
            pytest.param("[ref]: <foo\n===\nMust run checks.\n", ["[ref]: <foo"], id="unterminated-angle-destination"),
            pytest.param("[ref]: foo(bar\n===\n", ["[ref]: foo(bar"], id="unbalanced-parenthesis"),
            pytest.param('[ref]: <foo> "t"\n===\n', [], id="complete-definition-with-title"),
            pytest.param("paragraph\u2028```\n##### CI\n", ["CI"], id="u2028-is-not-a-line-break"),
            pytest.param("paragraph\x0c```\n##### CI\n", ["CI"], id="form-feed-is-not-a-line-break"),
            pytest.param("a\n\u00a0\n---\n", ["a"], id="no-break-space-line-is-text-not-blank"),
            pytest.param("text\n\u2003##### CI\n", [], id="em-space-is-not-indentation-of-a-heading"),
        ],
    )
    def test_headings_found(self, text: str, titles: list[str]) -> None:
        """The reader draws line and blank boundaries where CommonMark does, not where Python's string methods do."""
        assert [title for title, _ in ci._headings(text)] == titles

    def test_lines_split_only_at_markdown_breaks(self) -> None:
        """``\\n``, ``\\r\\n`` and ``\\r`` end a line; U+2028 and form feed do not."""
        assert ci._lines("a\u2028b\r\nc\rd\x0ce\n") == ["a\u2028b", "c", "d\x0ce"]


class TestCitedTitleWithCodeTicks:
    """Covers a cited heading that holds an inline code span."""

    @pytest.mark.parametrize(
        ("citation", "title"),
        [
            pytest.param("a.md §Run `tests`", "run tests", id="balanced-ticks-in-the-title"),
            pytest.param("a.md §CI `checks`", "ci checks", id="trailing-code-span"),
            pytest.param("`a.md §Run tests`", "run tests", id="citation-wrapped-in-one-code-span"),
            pytest.param("a.md §`Run tests` and more", "run tests", id="title-wrapped-in-ticks"),
        ],
    )
    def test_title_read_whole(self, tmp_path: Path, citation: str, title: str) -> None:
        """A balanced code span stays in the title; a single tick that closes a wrapping span ends it."""
        _write(tmp_path, "plugins/p/rules/b.md", f"## B\n\nSee {citation}.\n")
        assert ci._mentions(tmp_path)[0].heading.startswith(title)

    def test_deleting_a_heading_with_a_code_span_is_a_loss(self) -> None:
        """A cited ``Run `tests``` heading is found by its citation, so deleting it needs a repoint."""
        body = "##### Run `tests`\nMust run.\n"
        index = {"f.md": [(ci._normalize_title("Run `tests`"), "x.md")]}
        guards = [dict(g, file="f.md", id="f#0") for g in ci._guarded("f.md", "Wrapper", 1, body, index)]
        edit = {"file": "f.md", "action": "delete", "lines": "1-2", "before": "x", "after": ""}
        assert [g["title"] for g in ci._lost_headings(guards, [edit], body)] == ["Run `tests`"]


class TestFlowListBlankLines:
    """Covers a blank line inside a multi-line flow list, which YAML folds to a newline, not a space."""

    @pytest.mark.parametrize(
        "front",
        [
            pytest.param('paths: ["src/**\n\n docs/**"]', id="blank-line-in-a-quoted-item"),
            pytest.param("paths: [src/**,\n\n  docs/**]", id="blank-line-between-items"),
        ],
    )
    def test_blank_line_leaves_the_list_unparsed(self, front: str) -> None:
        """A list whose lines include a blank one is not guessed at, so it never groups with a differently spelled
        one."""
        assert ci.parse_paths(f"---\n{front}\n---\n") == [ci.UNPARSED_PATHS]

    def test_continued_list_without_blank_line_is_read(self) -> None:
        """A flow list continued on the next line, with no blank line, is still read."""
        assert ci.parse_paths('---\npaths: ["src/**\n docs/**"]\n---\n') == ["src/** docs/**"]


class TestRoundEighteenRoutes:
    """Covers a definition label with a bracket, a shortcut reference link in a title, and quoted flow items."""

    @pytest.mark.parametrize(
        ("text", "titles"),
        [
            pytest.param("[a[b]: /url\n===\nMust run.\n", ["[a[b]: /url"], id="bracket-in-a-definition-label"),
            pytest.param("[a\\[b]: /url\n===\n", [], id="escaped-bracket-is-a-valid-label"),
            pytest.param("[ ]: /url\n===\n", ["[ ]: /url"], id="blank-label-is-no-definition"),
        ],
    )
    def test_definition_labels(self, text: str, titles: list[str]) -> None:
        """A line is taken as a link reference definition only when CommonMark takes it as one."""
        assert [title for title, _ in ci._headings(text)] == titles

    def test_shortcut_reference_link_title_matches_its_citation(self) -> None:
        """A heading ``[CI]`` (a link only when a definition exists) and the citation ``§CI`` name the same section."""
        body = "##### [CI]\nMust check.\n\n[CI]: /x\n"
        index = {"f.md": [(ci._normalize_title("CI"), "x.md")]}
        guards = [dict(g, file="f.md", id="f#0") for g in ci._guarded("f.md", "Wrapper", 1, body, index)]
        edit = {"file": "f.md", "action": "delete", "lines": "1-2", "before": "x", "after": ""}
        assert [g["title"] for g in ci._lost_headings(guards, [edit], body)] == ["[CI]"]

    @pytest.mark.parametrize(
        ("front", "globs"),
        [
            pytest.param(
                'paths: ["src/\n  #private\n  **"]', ["src/ #private **"], id="hash-inside-a-quoted-continuation"
            ),
            pytest.param('paths: ["src/  **"]', ["src/  **"], id="two-spaces-inside-quotes-are-kept"),
            pytest.param("paths: [a, # c\n  b]", ["a", "b"], id="comment-line-outside-quotes"),
        ],
    )
    def test_quote_state_runs_across_lines(self, front: str, globs: list[str]) -> None:
        """A ``#`` inside a quoted item is text on any line; outside quotes it starts a comment."""
        assert ci.parse_paths(f"---\n{front}\n---\n") == globs

    def test_the_two_spellings_are_not_one_scope(self) -> None:
        """A folded quoted item with a ``#`` and the same words with plain spacing are different globs."""
        folded = ci.parse_paths('---\npaths: ["src/\n  #private\n  **"]\n---\n')
        plain = ci.parse_paths('---\npaths: ["src/  **"]\n---\n')
        assert folded != plain

    @pytest.mark.parametrize(
        "front",
        [
            pytest.param("paths: [\n   'it''s']", id="doubled-quote-escape-in-a-flow-item"),
            pytest.param("paths:\n- xdocs/**\n\n  - x", id="deeper-line-continues-the-item-above"),
            pytest.param("paths:\n  # only a comment", id="comment-line-is-no-glob"),
            pytest.param("paths: true", id="boolean-is-no-glob"),
            pytest.param("paths: ~", id="null-is-no-glob"),
            pytest.param("paths: 123", id="number-is-no-glob"),
            pytest.param("paths: 2026-01-02", id="date-is-no-glob"),
        ],
    )
    def test_value_yaml_reads_otherwise_is_unparsed(self, front: str) -> None:
        """A spelling YAML reads as another type, or as one longer scalar, is never taken as a glob list."""
        assert ci.parse_paths(f"---\n{front}\n---\n") == [ci.UNPARSED_PATHS]

    def test_agrees_with_a_yaml_parser(self) -> None:
        """Whenever ``parse_paths`` reads a value, PyYAML reads the same list from the same frontmatter.

        Every one to three line continuation built from quote, comment, bracket and plain-text pieces; skipped when
        PyYAML is not installed.
        """
        yaml = pytest.importorskip("yaml")
        starts = ["", " [", ' ["a', " 'x", " a", " [a, b]"]
        pieces = ["]", ",", '"src/**"', "'it''s'", "# c", "#x", 'b"', "y'", "- x", "a #b", "a#b", "}", "&and", "~"]
        mismatches = []
        for first in starts:
            for count in (1, 2):
                for combo in itertools.product(pieces, repeat=count):
                    front = "paths:" + first + "\n" + "\n".join("  " + piece for piece in combo)
                    got = ci.parse_paths(f"---\n{front}\n---\n")
                    if got in ([], [ci.UNPARSED_PATHS]):
                        continue
                    try:
                        data = yaml.safe_load(front)
                    except (yaml.YAMLError, ValueError, OverflowError):
                        continue
                    want = data.get("paths") if isinstance(data, dict) else None
                    want = [want] if isinstance(want, str) else want
                    if want != got:
                        mismatches.append(front)
        assert mismatches == []


class TestRoundNineteenRoutes:
    """Covers link reference definitions over several lines, white space in cited titles, and YAML-only white space."""

    @pytest.mark.parametrize(
        ("text", "titles"),
        [
            pytest.param("[a]: /u\x01rl\n===\n", ["[a]: /u\x01rl"], id="control-character-in-a-destination"),
            pytest.param('[a]: /url\n "title"\n===\n', [], id="title-on-the-next-line"),
            pytest.param('[a]: /url\n "title" x\n===\n', ['"title" x'], id="spoiled-title-is-paragraph-text"),
            pytest.param("[a]:\n/url\n===\n", [], id="destination-on-the-next-line"),
            pytest.param("[a]: /url\n===\n---\n", ["==="], id="definition-then-paragraph-then-underline"),
            pytest.param('[a]: /url "t" "u"\n===\n', ['[a]: /url "t" "u"'], id="two-titles-are-no-definition"),
            pytest.param('[a]: /url "multi\nline"\n===\n', [], id="title-over-two-lines"),
            pytest.param('- [a]: /url\n"title"\n    # H\n', ["H"], id="lazy-title-line-keeps-the-item-open"),
            pytest.param("[\u00a0]: /x\n---\n", ["[ ]: /x"], id="label-of-only-no-break-space"),
        ],
    )
    def test_definition_spans(self, text: str, titles: list[str]) -> None:
        """A definition takes the lines CommonMark gives it; what is left over can still be a heading."""
        assert [title for title, _ in ci._headings(text)] == titles

    def test_definitions_agree_with_a_reference_parser(self) -> None:
        """Every three-line mix of definition parts, containers and underlines reads as markdown-it reads it.

        Skipped when markdown-it is not installed.
        """
        markdown_it = pytest.importorskip("markdown_it")
        fragments = [
            "[a]: /url", "[a]:", "/url", '"title"', ' "title"', '"title" x', "'t'", "(t)", "[a]: <b>", '[a]: <b> "t"',
            '"multi', 'line"', "[a]: /u\x01rl", "[a]: /u\u00a0rl", "[ ]: /x", "> [a]: /url", '> "title"', "- [a]: /url",
            "===", "---", "", "-", "Setext", "# H",
        ]  # fmt: skip
        parser = markdown_it.MarkdownIt("commonmark")
        mismatches = []
        for lines in itertools.product(fragments, repeat=3):
            text = "\n".join(lines) + "\n"
            tokens = parser.parse(text)
            want = {
                (tok.map[0], " ".join(tokens[pos + 1].content.split()))
                for pos, tok in enumerate(tokens)
                if tok.type == "heading_open" and tokens[pos + 1].content.strip()
            }
            got = {(idx, " ".join(title.split())) for idx, _, title, _ in ci._scan_blocks(ci._lines(text))}
            if got != want:
                mismatches.append(text)
        assert mismatches == []

    @pytest.mark.parametrize(
        ("heading", "cited"),
        [
            pytest.param("CI Checks", "CI   Checks", id="spaces-in-the-citation"),
            pytest.param("CI   Checks", "CI Checks", id="spaces-in-the-heading"),
            pytest.param("CI\u00a0Checks", "CI Checks", id="no-break-space"),
            pytest.param("CI&nbsp;Checks", "CI\tChecks", id="entity-and-tab"),
            pytest.param("Stra\u00dfe", "STRASSE", id="case-folding"),
        ],
    )
    def test_spacing_and_case_do_not_split_a_title(self, heading: str, cited: str) -> None:
        """A heading and its citation reduce to the same words however the white space and case are spelled."""
        assert ci._normalize_title(heading) == ci._normalize_title(cited)
        assert ci._full_title(heading) == ci._full_title(cited)

    def test_a_spaced_citation_still_guards_its_heading(self) -> None:
        """A citation ``§CI   Checks`` protects the heading ``CI Checks``: deleting it needs a repoint."""
        body = "##### CI Checks\nMust check.\n"
        index = {"f.md": [(ci._normalize_title("CI   Checks"), "x.md")]}
        guards = [dict(g, file="f.md", id="f#0") for g in ci._guarded("f.md", "Wrapper", 1, body, index)]
        edit = {"file": "f.md", "action": "delete", "lines": "1-2", "before": "x", "after": ""}
        assert [g["title"] for g in ci._lost_headings(guards, [edit], body)] == ["CI Checks"]

    @pytest.mark.parametrize(
        ("front", "globs"),
        [
            pytest.param('paths: ["a\u00a0\n b"]', ["a\u00a0 b"], id="no-break-space-at-a-fold"),
            pytest.param('paths: ["a b"]', ["a b"], id="plain-space"),
            pytest.param("paths: a\u00a0b", ["a\u00a0b"], id="no-break-space-inside-a-scalar"),
            pytest.param("paths:\n  - a\u00a0", ["a\u00a0"], id="trailing-no-break-space-in-a-block-item"),
            pytest.param("paths: [a,\u00a0b]", ["a", "\u00a0b"], id="leading-no-break-space-in-a-flow-item"),
        ],
    )
    def test_only_space_and_tab_are_white_space(self, front: str, globs: list[str]) -> None:
        """A no-break space is part of a glob, as YAML reads it; it is never trimmed away."""
        assert ci.parse_paths(f"---\n{front}\n---\n") == globs

    def test_a_no_break_space_makes_a_different_scope(self) -> None:
        """Two values YAML reads as different strings are never one merge scope."""
        folded = ci.parse_paths('---\npaths: ["a\u00a0\n b"]\n---\n')
        plain = ci.parse_paths('---\npaths: ["a b"]\n---\n')
        assert folded != plain

    @pytest.mark.parametrize(
        "char",
        [
            pytest.param("\x0b", id="vertical-tab"),
            pytest.param("\x0c", id="form-feed"),
            pytest.param("\x1c", id="file-separator"),
            pytest.param("\x7f", id="delete"),
            pytest.param("\x85", id="next-line"),
            pytest.param("\u2028", id="line-separator"),
            pytest.param("\u2029", id="paragraph-separator"),
        ],
    )
    def test_characters_yaml_breaks_or_refuses_are_unparsed(self, char: str) -> None:
        """A frontmatter holding a YAML line break other than ``\\n``, or a control character, is not read."""
        assert ci.parse_paths(f"---\npaths: [a{char}b]\n---\n") == [ci.UNPARSED_PATHS]

    def test_agrees_with_a_yaml_parser_on_white_space(self) -> None:
        """Whenever ``parse_paths`` reads a value, PyYAML reads the same list, with tabs and no-break spaces mixed in.

        Skipped when PyYAML is not installed.
        """
        yaml = pytest.importorskip("yaml")
        starts = ["", " [", ' ["a', " 'x", " a", " [a, b]"]
        pieces = [
            "\u00a0",
            "a\u00a0",
            "\ta",
            "a\t",
            "\u2003",
            "'x\u00a0'",
            '"b\u00a0"',
            "\u00a0#c",
            "a:\t",
            "- \u00a0x",
            "]",
        ]
        mismatches = []
        for first in starts:
            for count in (1, 2):
                for combo in itertools.product(pieces, repeat=count):
                    front = "paths:" + first + "\n" + "\n".join("  " + piece for piece in combo)
                    got = ci.parse_paths(f"---\n{front}\n---\n")
                    if got in ([], [ci.UNPARSED_PATHS]):
                        continue
                    try:
                        data = yaml.safe_load(front)
                    except (yaml.YAMLError, ValueError, OverflowError):
                        continue
                    want = data.get("paths") if isinstance(data, dict) else None
                    want = [want] if isinstance(want, str) else want
                    if want != got:
                        mismatches.append(front)
        assert mismatches == []


class TestRoundTwentyRoutes:
    """Covers survivor headings in the whole file, escaped marks in titles, nested YAML items and registered skills."""

    @pytest.mark.parametrize(
        ("proposal", "lost"),
        [
            pytest.param("CI\n==\nMust preserve.\n", {"x.md"}, id="setext-title-folds-in-the-paragraph-above"),
            pytest.param("##### CI\nMust preserve.\n", set(), id="heading-stays-a-heading"),
        ],
    )
    def test_survivor_heading_is_read_in_the_whole_file(self, proposal: str, lost: set[str]) -> None:
        """A proposal valid alone can still fold the heading above it into a new title once spliced into the file."""
        vet = {
            "members": ["f.md#1", "g.md#0"],
            "locations": [
                {"id": "f.md#1", "file": "f.md", "start_line": 3, "end_line": 4},
                {"id": "g.md#0", "file": "g.md", "start_line": 1, "end_line": 2},
            ],
            "file_guarded": [{"id": "f.md#1", "file": "f.md", "title": "CI", "line": 3, "refs": ["x.md"]}],
        }
        sources = {"f.md": "## Prev\nprevious paragraph\n## CI\nMust preserve.\n", "g.md": "## G\nbody\n"}
        assert ci._merge_losses(vet, "f.md#1", proposal, set(), sources) == lost

    @pytest.mark.parametrize(
        ("heading", "cited"),
        [
            pytest.param("C\\*I", "C*I", id="escaped-star"),
            pytest.param("C\\*I", "CI", id="escaped-star-against-no-star"),
            pytest.param("a\\_b\\_", "a_b_", id="escaped-underscores"),
            pytest.param("\\[CI\\]", "CI", id="escaped-brackets"),
            pytest.param("\\<i>", "<i>", id="escaped-angle-bracket"),
        ],
    )
    def test_escaped_and_bare_marks_reduce_alike(self, heading: str, cited: str) -> None:
        """A heading that escapes a mark and a citation that writes it bare name the same section."""
        assert ci._normalize_title(heading) == ci._normalize_title(cited)
        assert ci._full_title(heading) == ci._full_title(cited)

    def test_titles_agree_with_the_text_a_reference_parser_renders(self) -> None:
        """A heading's markup and the plain text markdown-it renders for it reduce to the same words.

        Every one to three part mix of emphasis, code, escapes, entities, links and tags; skipped when markdown-it is
        not installed.
        """
        markdown_it = pytest.importorskip("markdown_it")
        parts = [
            "A", "**B**", "*C*", "_D_", "__E__", "`F`", "G\\*H", "I\\_J", "&amp;", "&nbsp;", "[K](u)", "[L]", "<b>M</b>",
            "N~~O~~", "\\[P\\]", "Q_R", "x&#42;y", "\\#", "a\\\\b", "***Z***", "``c`d``", "&copy;", "\u00a0", "\\<i>",
        ]  # fmt: skip
        links = [
            "<https://example.com>", "<a@b.co>", '[CI](url(a) "t")', "[x](<u v>)", "[y](u 'it')", "![img](p.png)",
            "[z]()", "[w](u (t))", "[v](a(b(c)))", "<mailto:x@y.z>", '[s]("t")', "[r](  u  )", "x(y)", "[e](u\\))",
            '<code title="x > y">N</code>', "<span class='a>b'>O</span>", "<br/>", "<!-- c -->P", "[CI](u&#41;tail)",
            "[q](u&amp;v)", "&#42;R&#42;", "<i>", "a<b", '<a href="u">S</a>',
        ]  # fmt: skip
        parser = markdown_it.MarkdownIt("commonmark")
        mismatches = []
        for count, pool in ((1, parts + links), (2, parts + links), (3, parts), (3, links)):
            for combo in itertools.product(pool, repeat=count):
                heading = " ".join(combo)
                inline = parser.parse("# " + heading + "\n")[1]
                plain = "".join(c.content for c in inline.children if c.type in ("text", "code_inline", "image"))
                if ci._full_title(heading) != ci._full_title(plain):
                    mismatches.append(heading)
        assert mismatches == []

    @pytest.mark.parametrize(
        "front",
        [
            pytest.param("paths:\n  - - a", id="nested-block-sequence"),
            pytest.param("paths: [- a]", id="block-entry-inside-a-flow-list"),
            pytest.param("paths:\n  - ? a", id="explicit-key-entry"),
            pytest.param("paths:\n  - -", id="bare-nested-dash"),
            pytest.param("paths:\n  - : a", id="empty-key-mapping"),
        ],
    )
    def test_items_that_start_like_yaml_structure_are_unparsed(self, front: str) -> None:
        """An item YAML reads as a nested sequence or mapping is never taken as a glob."""
        assert ci.parse_paths(f"---\n{front}\n---\n") == [ci.UNPARSED_PATHS]

    @pytest.mark.parametrize(
        ("front", "globs"),
        [
            pytest.param('paths: ["- a"]', ["- a"], id="quoted-dash-and-space"),
            pytest.param("paths: -a", ["-a"], id="dash-without-space"),
            pytest.param("paths:\n  - a - b", ["a - b"], id="dash-inside-a-scalar"),
        ],
    )
    def test_dash_inside_a_scalar_is_still_a_glob(self, front: str, globs: list[str]) -> None:
        """Only a dash, question mark or colon followed by a space opens structure; elsewhere it is text."""
        assert ci.parse_paths(f"---\n{front}\n---\n") == globs

    def test_a_nested_sequence_is_not_the_scope_of_its_quoted_text(self) -> None:
        """``- - a`` and ``["- a"]`` are different values and never one merge scope."""
        nested = ci.parse_paths("---\npaths:\n  - - a\n---\n")
        quoted = ci.parse_paths('---\npaths: ["- a"]\n---\n')
        assert nested != quoted

    def test_agrees_with_a_yaml_parser_on_indicators(self) -> None:
        """Whenever ``parse_paths`` reads a value, PyYAML reads the same list, with indicators at the item start.

        Skipped when PyYAML is not installed.
        """
        yaml = pytest.importorskip("yaml")
        starts = ["", " [", " a", " [a]"]
        pieces = ["- a", "- - a", "? a", ": a", "-", "?", ":", "-a", "- 'x'", "a - b", "- ", "[- a]", "- ? a", "a: b"]
        mismatches = []
        for first in starts:
            for count in (1, 2):
                for combo in itertools.product(pieces, repeat=count):
                    front = "paths:" + first + "\n" + "\n".join("  " + piece for piece in combo)
                    got = ci.parse_paths(f"---\n{front}\n---\n")
                    if got in ([], [ci.UNPARSED_PATHS]):
                        continue
                    try:
                        data = yaml.safe_load(front)
                    except (yaml.YAMLError, ValueError, OverflowError):
                        continue
                    want = data.get("paths") if isinstance(data, dict) else None
                    want = [want] if isinstance(want, str) else want
                    if want != got:
                        mismatches.append(front)
        assert mismatches == []

    def test_registered_skills_of_another_plugin_are_inventoried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Claude plugin outside ``cc_*`` contributes the skill directory its manifest registers, and only that."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\nrun\n")
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", '{"name": "other", "skills": "./claude-skills/"}')
        _write(tmp_path, "plugins/other/claude-skills/q/SKILL.md", "## Query\n\nask\n")
        _write(tmp_path, "plugins/other/rules/r.md", "## R\n\nnever loaded by Claude\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        by_file = {item["file"]: item for item in items}
        assert sorted(by_file) == ["plugins/cc_a/skills/s/SKILL.md", "plugins/other/claude-skills/q/SKILL.md"]
        other = by_file["plugins/other/claude-skills/q/SKILL.md"]
        assert (other["kind"], other["group"]) == ("skill", "lazy:other:skills/q")

    @pytest.mark.parametrize(
        "registered",
        [
            pytest.param("../cc_a/skills", id="parent-directory"),
            pytest.param("/etc", id="absolute-path"),
            pytest.param("C:/skills", id="drive-path"),
            pytest.param(".", id="plugin-root"),
        ],
    )
    def test_a_manifest_cannot_point_the_inventory_elsewhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registered: str
    ) -> None:
        """A registered skill path that leaves the plugin directory is ignored."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\nrun\n")
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"skills": registered}))
        assert [p.relative_to(tmp_path).as_posix() for p in ci.discover(tmp_path, ci.ALL_KINDS)] == [
            "plugins/cc_a/skills/s/SKILL.md"
        ]

    def test_a_scope_token_names_a_plugin_outside_cc(self) -> None:
        """``other`` selects the groups of the plugin ``other``, not only ``cc_other``."""
        assert ci._token_groups("other", ["lazy:other:skills/q", "lazy:cc_a:skills/s"], {}) == {"lazy:other:skills/q"}

    @pytest.mark.parametrize(
        ("front", "globs"),
        [
            pytest.param("paths: [a,\n#c\n b]", ["a", "b"], id="comment-line-inside-a-flow-list"),
            pytest.param("paths:\n  - x\n#c\n  - ? y", [ci.UNPARSED_PATHS], id="comment-line-then-a-structured-item"),
            pytest.param("paths:\n  - x\n#c\n  - y", [ci.UNPARSED_PATHS], id="comment-line-inside-a-block-list"),
        ],
    )
    def test_a_comment_line_at_column_zero_does_not_end_the_value(self, front: str, globs: list[str]) -> None:
        """A comment line between list items is skipped by YAML; the reader never stops reading the value there."""
        assert ci.parse_paths(f"---\n{front}\n---\n") == globs


class TestRoundTwentyOneRoutes:
    """Covers autolinks and link targets in titles, YAML numbers, additive skill registration and review artifacts."""

    @pytest.mark.parametrize(
        ("heading", "cited"),
        [
            pytest.param("<https://example.com>", "https://example.com", id="autolink"),
            pytest.param("<a@b.co>", "a@b.co", id="email-autolink"),
            pytest.param('[CI](url(a) "t")', "CI", id="target-with-parentheses-and-title"),
            pytest.param("[x](<u v>)", "x", id="angle-destination-with-space"),
            pytest.param("[e](u\\))", "e", id="escaped-parenthesis-in-the-destination"),
            pytest.param("![img](p.png)", "img", id="image"),
        ],
    )
    def test_links_reduce_to_their_visible_text(self, heading: str, cited: str) -> None:
        """A link, image or autolink in a heading and the words a citation uses for it reduce alike."""
        assert ci._normalize_title(heading) == ci._normalize_title(cited)
        assert ci._full_title(heading) == ci._full_title(cited)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("0b10", id="binary"),
            pytest.param("-0x1F", id="signed-hex"),
            pytest.param("+0o7", id="signed-octal"),
            pytest.param("1_000", id="underscored-int"),
            pytest.param("0._", id="float-with-underscore"),
            pytest.param("<<", id="merge-key"),
        ],
    )
    def test_numbers_and_indicators_are_unparsed_unquoted_and_globs_quoted(self, value: str) -> None:
        """A plain scalar YAML reads as a number or indicator is no glob; the quoted string is one."""
        assert ci.parse_paths(f"---\npaths: [{value}]\n---\n") == [ci.UNPARSED_PATHS]
        assert ci.parse_paths(f'---\npaths: ["{value}"]\n---\n') == [value]

    def test_plain_scalars_agree_with_a_yaml_parser(self) -> None:
        """Every short plain scalar of YAML-typing characters is either unread or read as PyYAML reads it.

        Skipped when PyYAML is not installed.
        """
        yaml = pytest.importorskip("yaml")
        alphabet = "0189abexX_.:-+~nulZ <="
        mismatches = []
        for length in (1, 2, 3):
            for chars in itertools.product(alphabet, repeat=length):
                text = "".join(chars)
                front = f"paths: [{text}]"
                got = ci.parse_paths(f"---\n{front}\n---\n")
                if text != text.strip(" ") or "  " in text or got in ([], [ci.UNPARSED_PATHS]):
                    continue
                try:
                    data = yaml.safe_load(front)
                except (yaml.YAMLError, ValueError, OverflowError):
                    continue
                if (data.get("paths") if isinstance(data, dict) else None) != got:
                    mismatches.append(front)
        assert mismatches == []

    @pytest.mark.parametrize(
        "registered",
        [
            pytest.param("./extra/", id="scalar-path"),
            pytest.param(["./extra/"], id="list-of-one-path"),
            pytest.param(["./other/", "./extra/"], id="list-with-a-missing-directory"),
        ],
    )
    def test_registered_skills_add_to_the_default_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registered: object
    ) -> None:
        """A manifest ``skills`` value, a path or a list, adds directories; the default ``skills/`` is still read."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"skills": registered}))
        _write(tmp_path, "plugins/other/skills/a/SKILL.md", "## A\n\nrun\n")
        _write(tmp_path, "plugins/other/extra/b/SKILL.md", "## B\n\nrun\n")
        found = [p.relative_to(tmp_path).as_posix() for p in ci.discover(tmp_path, ci.ALL_KINDS)]
        assert found == ["plugins/other/skills/a/SKILL.md", "plugins/other/extra/b/SKILL.md"]

    def test_a_registered_directory_is_named_skills_in_its_groups(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A skill under a registered directory is grouped like one under ``skills/``."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"skills": ["./extra/"]}))
        _write(tmp_path, "plugins/other/extra/b/SKILL.md", "## B\n\nrun\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [(item["kind"], item["group"]) for item in items] == [("skill", "lazy:other:skills/b")]

    @pytest.mark.parametrize(
        "registered",
        [
            pytest.param("extra", id="no-dot-slash-prefix"),
            pytest.param(["../cc_a/skills"], id="parent-directory-in-a-list"),
            pytest.param([3, None, {"path": "./extra"}], id="not-text"),
        ],
    )
    def test_malformed_registrations_are_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registered: object
    ) -> None:
        """Only a ``./`` path inside the plugin is a registration; anything else leaves the default directory alone."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\nrun\n")
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"skills": registered}))
        _write(tmp_path, "plugins/other/extra/b/SKILL.md", "## B\n\nrun\n")
        assert [p.relative_to(tmp_path).as_posix() for p in ci.discover(tmp_path, ci.ALL_KINDS)] == [
            "plugins/cc_a/skills/s/SKILL.md"
        ]

    @pytest.mark.parametrize(
        ("root_skill", "expected"),
        [
            pytest.param(
                True,
                ["plugins/other/SKILL.md", "plugins/other/b/SKILL.md", "plugins/other/modes/task.md"],
                id="root-folder-holds-skill-md-so-it-is-one-skill-with-its-fragments",
            ),
            pytest.param(False, ["plugins/other/b/SKILL.md"], id="root-folder-holds-skill-folders"),
        ],
    )
    def test_a_plugin_root_registration_names_only_skill_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_skill: bool, expected: list[str]
    ) -> None:
        """Registering ``.`` reads the root skill with its fragments, or the ``SKILL.md`` files one level down."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"skills": ["."]}))
        _write(tmp_path, "plugins/other/b/SKILL.md", "## B\n\nrun\n")
        _write(tmp_path, "plugins/other/README.md", "## Readme\n\nnot a skill\n")
        _write(tmp_path, "plugins/other/docs/notes.md", "## Notes\n\nnot a skill\n")
        _write(tmp_path, "plugins/other/modes/task.md", "## Task\n\nfragment of the root skill\n")
        if root_skill:
            _write(tmp_path, "plugins/other/SKILL.md", "## Root\n\nrun\n")
        found = sorted(p.relative_to(tmp_path).as_posix() for p in ci.discover(tmp_path, ci.ALL_KINDS))
        assert found == expected

    def test_a_plugin_with_only_a_root_skill_is_one_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Claude plugin with ``SKILL.md`` at its root and no skill directory loads as a single skill."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"name": "other"}))
        _write(tmp_path, "plugins/other/SKILL.md", "## Root\n\nrun\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [(item["file"], item["kind"]) for item in items] == [("plugins/other/SKILL.md", "skill")]

    @pytest.mark.parametrize(
        "review",
        [
            pytest.param(
                {"cluster": "c", "digest": "d", "verdict": "holds", "evidence": None}, id="holds-null-evidence"
            ),
            pytest.param(
                {"cluster": "c", "digest": "d", "verdict": "holds", "evidence": "  "}, id="holds-blank-evidence"
            ),
            pytest.param({"cluster": "c", "digest": "d", "verdict": "holds"}, id="holds-without-evidence"),
            pytest.param({"cluster": None, "digest": "d", "verdict": "holds", "evidence": "x"}, id="null-cluster"),
            pytest.param({"cluster": "c", "digest": 3, "verdict": "holds", "evidence": "x"}, id="number-digest"),
        ],
    )
    def test_a_holds_verdict_needs_text_fields_and_evidence(self, review: dict[str, object]) -> None:
        """A review that does not carry its identity and, for ``holds``, the check run is not a verdict."""
        assert ci._pick_verdicts([review]) == {}

    def test_a_negative_verdict_counts_without_evidence(self) -> None:
        """A ``loses`` or ``weakens`` verdict is never dropped for a missing evidence field."""
        review = {"cluster": "c", "digest": "d", "verdict": "loses", "evidence": None}
        assert ci._pick_verdicts([review])["c"]["d"]["verdict"] == "loses"

    @pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
    def test_a_nonstandard_number_is_refused_on_load(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], constant: str
    ) -> None:
        """An artifact holding ``NaN`` or ``Infinity`` is not JSON; it is refused instead of carried into the output."""
        bad = tmp_path / "r.json"
        bad.write_text(f'[{{"cluster": "c", "evidence": {constant}}}]', encoding="utf-8")
        with pytest.raises(SystemExit) as exit_info:
            ci._load_json(str(bad))
        assert (exit_info.value.code, "is not a JSON value" in capsys.readouterr().out) == (1, True)

    def test_emitting_a_nonstandard_number_fails(self, tmp_path: Path) -> None:
        """The writer never produces ``NaN``, whatever reached it."""
        with pytest.raises(ValueError, match="not JSON compliant"):
            ci._emit({"evidence": float("nan")}, str(tmp_path / ".temp" / "o.json"), tmp_path)


class TestRoundTwentyTwoRoutes:
    """Covers HTML tags and entities in titles, quoted citations, registered agents, one-skill folders and floats."""

    @pytest.mark.parametrize(
        ("heading", "cited"),
        [
            pytest.param('<code title="x > y">CI</code>', "CI", id="tag-attribute-holding-an-angle-bracket"),
            pytest.param("<span class='a>b'>CI</span>", "CI", id="single-quoted-attribute-with-angle-bracket"),
            pytest.param("[CI](u&#41;tail)", "CI", id="entity-in-a-link-target"),
            pytest.param("[CI](u&amp;v)", "CI", id="ampersand-entity-in-a-link-target"),
            pytest.param("<!-- note -->CI", "CI", id="inline-comment"),
            pytest.param("CI<br/>", "CI", id="self-closing-tag"),
            pytest.param("a<b", "a<b", id="bare-angle-bracket-is-text"),
        ],
    )
    def test_markup_reduces_to_its_visible_text(self, heading: str, cited: str) -> None:
        """A heading with raw HTML or entities and the words a citation uses for it reduce alike."""
        assert ci._normalize_title(heading) == ci._normalize_title(cited)
        assert ci._full_title(heading) == ci._full_title(cited)

    @pytest.mark.parametrize(
        ("citing", "headings"),
        [
            pytest.param('see x.md §"CI;CD" for it', ["ci;cd"], id="semicolon-in-double-quotes"),
            pytest.param("see x.md §`A|B` for it", ["a|b"], id="bar-in-code-ticks"),
            pytest.param("see x.md §“A;B” now", ["a;b"], id="semicolon-in-curly-quotes"),
            pytest.param("see x.md §Alpha; y.md §Beta", ["alpha", "beta"], id="unquoted-semicolon-still-separates"),
            pytest.param('see x.md §"Alpha", §"Beta;Gamma"', ["alpha", "beta;gamma"], id="quoted-list"),
        ],
    )
    def test_quoted_cited_title_keeps_its_separators(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, citing: str, headings: list[str]
    ) -> None:
        """A title in quotes or code ticks may hold ``;`` and ``|``; outside quotes they still end a citation."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", f"## Steps\n\n{citing}\n")
        assert [m.heading for m in ci._mentions(tmp_path)] == headings

    @pytest.mark.parametrize(
        ("registered", "expected"),
        [
            pytest.param(["./special.md"], ["plugins/other/special.md"], id="list-replaces-the-default-directory"),
            pytest.param("./special.md", ["plugins/other/special.md"], id="scalar-replaces-the-default-directory"),
            pytest.param(["special.md"], [], id="no-dot-slash-prefix-loads-nothing-and-still-replaces"),
            pytest.param(["./missing.md", "./notes.txt"], [], id="missing-and-non-markdown-files-leave-nothing"),
            pytest.param(["../cc_a/agents/x.md"], [], id="parent-directory-loads-nothing-and-still-replaces"),
        ],
    )
    def test_registered_agents_replace_the_default_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registered: object, expected: list[str]
    ) -> None:
        """An agent file named by the manifest is inventoried and the displaced ``agents/`` scan is not.

        The plugin reference says the key replaces the default scan and a path that is not a ``./`` path inside the
        plugin does not load, so an invalid list leaves no agents rather than falling back to the folder.
        """
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"agents": registered}))
        _write(tmp_path, "plugins/other/special.md", "## Special\n\nagent\n")
        _write(tmp_path, "plugins/other/notes.txt", "not markdown\n")
        _write(tmp_path, "plugins/other/agents/old.md", "## Old\n\nold agent\n")
        found = [p.relative_to(tmp_path).as_posix() for p in ci.discover(tmp_path, ("agents",))]
        assert found == expected

    def test_a_registered_agent_is_an_agent_of_its_plugin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registered agent outside ``agents/`` is classified like one inside it, in a plugin outside ``cc_*``."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"agents": ["./sub/special.md"]}))
        _write(tmp_path, "plugins/other/sub/special.md", "## Special\n\nagent\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [(item["kind"], item["group"]) for item in items] == [("agent", "lazy:other:agents")]

    def test_a_folder_holding_skill_md_is_one_skill_with_its_fragments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registered folder with ``SKILL.md`` directly in it is one skill: entrypoint and fragments share a group."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"skills": ["./one"]}))
        _write(tmp_path, "plugins/other/one/SKILL.md", "## One\n\nrun\n")
        _write(tmp_path, "plugins/other/one/modes/task.md", "## Task\n\nrun\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert sorted((item["file"], item["kind"], item["group"]) for item in items) == [
            ("plugins/other/one/SKILL.md", "skill", "lazy:other:skills/one"),
            ("plugins/other/one/modes/task.md", "skill-fragment", "lazy:other:skills/one"),
        ]

    def test_a_plugin_that_is_one_skill_is_grouped_under_its_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plugin with ``SKILL.md`` at its root and no skill directory is a skill named after the plugin."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"name": "other"}))
        _write(tmp_path, "plugins/other/SKILL.md", "## Root\n\nrun\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [(item["kind"], item["group"]) for item in items] == [("skill", "lazy:other:skills/other")]

    @pytest.mark.parametrize("number", ["1e999", "-1e999", "1E400", "2.5e1000"])
    def test_a_number_too_large_for_a_double_is_refused_on_load(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], number: str
    ) -> None:
        """A JSON number that would read as infinity is refused like ``NaN``; a clean diagnostic, no crash later."""
        bad = tmp_path / "r.json"
        bad.write_text(f'[{{"extra": {number}}}]', encoding="utf-8")
        with pytest.raises(SystemExit) as exit_info:
            ci._load_list(str(bad), "judgments")
        assert (exit_info.value.code, "out of range" in capsys.readouterr().out) == (1, True)

    def test_an_ordinary_float_still_loads(self, tmp_path: Path) -> None:
        """Finite fractions and exponents are unaffected by the range check."""
        good = tmp_path / "r.json"
        good.write_text('[{"confidence": 0.85, "scale": 1e3}]', encoding="utf-8")
        assert ci._load_list(str(good), "reviews") == [{"confidence": 0.85, "scale": 1000.0}]


class TestRoundTwentyThreeRoutes:
    """Covers headings tested against the cited text, nested agents, root skills, alternatives and judgment nulls."""

    @pytest.mark.parametrize(
        ("title", "citing"),
        [
            pytest.param("CI §CD", 'see tgt.md §"CI §CD" now', id="section-sign-inside-quotes"),
            pytest.param("CI;CD", "see tgt.md §CI;CD now", id="unquoted-semicolon"),
            pytest.param("CI|CD", "see tgt.md §CI|CD now", id="unquoted-bar"),
            pytest.param("CI;CD", "see tgt.md §`CI;CD`", id="ticked-semicolon"),
        ],
    )
    def test_a_heading_is_guarded_by_text_that_begins_with_its_title(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, title: str, citing: str
    ) -> None:
        """A heading is found in the cited text however its separators are written; no title is cut out of the text."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", f"## Wrap\n\n##### {title}\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", f"## Steps\n\n{citing}\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [g["title"] for item in items for g in item["guarded"]] == [title]

    def test_every_common_citation_form_guards_every_title(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Titles with separators and punctuation, cited exactly in each usual spelling, are all guarded."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        titles = ["CI", "CI;CD", "CI|CD", "CI §CD", "A, B", "A (v2)", "A: B", "Plan — Do", "a/b", "C++", "Q&A"]
        forms = [
            "§{t}", '§"{t}"', "§`{t}`", "§{t} and more", "§{t}.", "§{t}, then", "(§{t})",
            "§“{t}”", "§{t} (see above)",
        ]  # fmt: skip
        missed = []
        for title in titles:
            _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", f"## Wrap\n\n##### {title}\nbody\n")
            for form in forms:
                _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", f"## Steps\n\nsee tgt.md {form.format(t=title)}\n")
                items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
                if not any(g["title"] == title for item in items for g in item["guarded"]):
                    missed.append((title, form))
        assert missed == []

    def test_an_unrelated_citation_guards_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A citation of another heading does not guard this one."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", "## Wrap\n\n##### Cinema\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\nsee tgt.md §CI;CD\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [g["title"] for item in items for g in item["guarded"]] == []

    @pytest.mark.parametrize(
        ("tail", "title", "expected"),
        [
            pytest.param("ci checks pass", "ci", True, id="word-boundary-after-the-title"),
            pytest.param("ci", "ci", True, id="whole-text"),
            pytest.param("ci;cd", "ci", True, id="separator-after-the-title"),
            pytest.param("cinema", "ci", False, id="longer-word"),
            pytest.param("ci", "", False, id="empty-title"),
            pytest.param("x ci", "ci", False, id="title-not-at-the-start"),
        ],
    )
    def test_title_must_begin_the_cited_text_at_a_word_boundary(self, tail: str, title: str, expected: bool) -> None:
        """The text after a section sign names a heading only when it begins with the whole title."""
        assert ci._starts_with_title(tail, title) is expected

    def test_index_entries_without_cited_text_still_work(self) -> None:
        """A two-field index entry, as library callers build it, is read by the title rule alone."""
        index = {"f.md": [("ci", "x.md")]}
        assert ci.inbound_refs("f.md", ["CI"], index) == ["x.md"]

    def test_nested_agents_are_inventoried(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A subfolder of ``agents/`` is part of an agent's name, so its files are agents too."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"name": "other"}))
        _write(tmp_path, "plugins/other/agents/top.md", "## Top\n\nagent\n")
        _write(tmp_path, "plugins/other/agents/team/nested.md", "## Nested\n\nagent\n")
        found = [p.relative_to(tmp_path).as_posix() for p in ci.discover(tmp_path, ("agents",))]
        assert found == ["plugins/other/agents/team/nested.md", "plugins/other/agents/top.md"]

    def test_a_root_skill_brings_its_fragments_but_not_other_components(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A skill kept at the plugin root includes its Markdown fragments and leaves tests, docs and READMEs out."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"name": "other"}))
        _write(tmp_path, "plugins/other/SKILL.md", "## Root\n\nrun\n")
        _write(tmp_path, "plugins/other/modes/task.md", "## Task\n\nfragment\n")
        _write(tmp_path, "plugins/other/reference.md", "## Reference\n\nfragment\n")
        for ignored in ("README.md", "CLAUDE.md", "tests/t.md", "docs/d.md", ".hidden/h.md"):
            _write(tmp_path, f"plugins/other/{ignored}", "## Not\n\nnot a skill\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert sorted((item["file"], item["group"]) for item in items) == [
            ("plugins/other/SKILL.md", "lazy:other:skills/other"),
            ("plugins/other/modes/task.md", "lazy:other:skills/other"),
            ("plugins/other/reference.md", "lazy:other:skills/other"),
        ]

    def test_a_registered_default_directory_holding_skill_md_is_one_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``skills: ["./skills"]`` with ``skills/SKILL.md`` directly in it is one skill with its fragments."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"skills": ["./skills"]}))
        _write(tmp_path, "plugins/other/skills/SKILL.md", "## One\n\nrun\n")
        _write(tmp_path, "plugins/other/skills/modes/task.md", "## Task\n\nrun\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert sorted({item["group"] for item in items}) == ["lazy:other:skills/skills"]

    def test_install_alternatives_with_one_scope_stay_in_separate_groups(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scoped rule and its ``-delta`` variant are delivered one or the other, so they never share a group."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        front = "---\npaths:\n  - src/**\n---\n\n"
        _write(tmp_path, "plugins/cc_demo/rules/a.md", front + "## A\n\nrule\n")
        _write(tmp_path, "plugins/cc_demo/rules/a-delta.md", front + "## A delta\n\nrule\n")
        items = ci.build_inventory(tmp_path, ("rules",))["items"]
        assert {(item["file"], item["group"]) for item in items} == {
            ("plugins/cc_demo/rules/a-delta.md", "scoped:cc_demo:alt:a-delta"),
            ("plugins/cc_demo/rules/a.md", "scoped:cc_demo:alt:a"),
        }

    @pytest.mark.parametrize(
        ("field", "value", "problem"),
        [
            pytest.param("proposal", None, "proposal is not text", id="null-proposal"),
            pytest.param("shared", None, "shared is not text", id="null-shared"),
            pytest.param("distinct", 3, "distinct is not text", id="number-distinct"),
            pytest.param("triggers", [], "triggers is not an object of text", id="list-triggers"),
            pytest.param("triggers", None, "triggers is not an object of text", id="null-triggers"),
            pytest.param("triggers", {"a": 1}, "triggers is not an object of text", id="non-text-trigger"),
        ],
    )
    def test_template_fields_of_the_wrong_type_make_the_judgment_malformed(
        self, field: str, value: object, problem: str
    ) -> None:
        """The judge template's text fields and trigger object are checked like the rest; null is never accepted."""
        _, problems = ci.normalize_judgment({"cluster": "c", "decision": "keep", field: value})
        assert problems == [problem]

    def test_well_formed_template_fields_pass(self) -> None:
        """A judgment that fills the template as written has no problems."""
        raw = {
            "cluster": "c",
            "decision": "keep",
            "shared": "s",
            "distinct": "",
            "proposal": "",
            "triggers": {"a": "t"},
        }
        assert ci.normalize_judgment(raw)[1] == []


class TestRoundTwentyFourRoutes:
    """Covers invalid YAML, wrapped and spaced citations, word-boundary prefixes, child skills and schema types."""

    @pytest.mark.parametrize(
        "front",
        [
            pytest.param("paths: [a,,b]", id="empty-flow-item"),
            pytest.param('paths: ["a" "b"]', id="two-quoted-items-without-a-comma"),
            pytest.param("paths: [a b: c]", id="colon-inside-a-flow-item"),
            pytest.param("paths: [file?.py]", id="question-mark-inside-a-flow-item"),
            pytest.param("paths:\n  ,", id="comma-starts-a-plain-scalar"),
            pytest.param("paths:\n  }", id="brace-starts-a-plain-scalar"),
            pytest.param("paths: \n-a", id="scalar-at-column-zero"),
            pytest.param("paths:\n[ a ]", id="flow-list-at-column-zero"),
            pytest.param("paths: [a, b]\n\t", id="tab-line"),
            pytest.param("paths: a\n   \t", id="spaces-then-a-tab"),
            pytest.param("paths: [a # c\n  b]", id="comment-ends-a-plain-item"),
        ],
    )
    def test_frontmatter_a_yaml_parser_refuses_is_unparsed(self, front: str) -> None:
        """A value PyYAML cannot parse is never read as a list; it gets a group of its own."""
        assert ci.parse_paths(f"---\n{front}\n---\n") == [ci.UNPARSED_PATHS]

    @pytest.mark.parametrize(
        ("front", "globs"),
        [
            pytest.param("paths: [a, b,]", ["a", "b"], id="trailing-comma"),
            pytest.param("paths: [\"a\", 'b']", ["a", "b"], id="mixed-quotes"),
            pytest.param("paths: [a, # c\n  b]", ["a", "b"], id="comment-after-a-comma"),
            pytest.param("paths: [a, b]  # note", ["a", "b"], id="trailing-comment"),
        ],
    )
    def test_ordinary_flow_lists_still_read(self, front: str, globs: list[str]) -> None:
        """Tightening the reader leaves the usual spellings alone."""
        assert ci.parse_paths(f"---\n{front}\n---\n") == globs

    def test_never_reads_what_a_yaml_parser_refuses(self) -> None:
        """Whenever PyYAML rejects a ``paths:`` value, ``parse_paths`` returns it unreadable (or empty), never a list.

        Every one to two line continuation of quote, comma, colon, tab and indicator pieces under a flow-list or block
        start; skipped when PyYAML is not installed.
        """
        yaml = pytest.importorskip("yaml")
        starts = ["", " [", ' ["a', " a", " [a, b]", " [a,"]
        pieces = [
            ",",
            ":",
            "?",
            "\t",
            "a b",
            "'x'",
            '"y"',
            "}",
            "]",
            "- a",
            "*al",
            "!t",
            "#c",
            "a: b",
            "-a",
            "[ a ]",
            "''",
        ]
        accepted = []
        for first in starts:
            for count in (1, 2):
                for combo in itertools.product(pieces, repeat=count):
                    front = "paths:" + first + "\n" + "\n".join("  " + piece for piece in combo)
                    got = ci.parse_paths(f"---\n{front}\n---\n")
                    if got in ([], [ci.UNPARSED_PATHS]):
                        continue
                    try:
                        yaml.safe_load(front)
                    except (yaml.YAMLError, ValueError, OverflowError):
                        accepted.append(front)
        assert accepted == []

    @pytest.mark.parametrize(
        ("heading", "citing"),
        [
            pytest.param("CI CD", "see tgt.md §CI\nCD later", id="title-wrapped-onto-the-next-line"),
            pytest.param("CI;" + "x" * 400, "see tgt.md §CI;" + "x" * 400, id="title-longer-than-the-old-cap"),
        ],
    )
    def test_cited_text_runs_to_the_end_of_the_paragraph(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, heading: str, citing: str
    ) -> None:
        """A long title, or one wrapped across lines, is still found in the text after the section sign."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", f"## Wrap\n\n##### {heading}\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", f"## Steps\n\n{citing}\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [g["title"] for item in items for g in item["guarded"]] == [heading]

    def test_a_blank_line_ends_the_cited_text(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Text after a blank line is another paragraph and cites nothing."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", "## Wrap\n\n##### CI CD\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\nsee tgt.md §CI\n\nCD is unrelated\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [g["title"] for item in items for g in item["guarded"]] == []

    def test_a_file_name_with_spaces_is_resolved(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``my rules.md §CI`` names the file ``my rules.md``; a lead-in word is not part of the name."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/my rules.md", "## Wrap\n\n##### CI\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\nsee my rules.md §CI now\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [g["title"] for item in items for g in item["guarded"]] == ["CI"]

    def test_a_lead_in_word_does_not_hide_the_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``see tgt.md §CI`` still resolves ``tgt.md`` when no file is named ``see tgt.md``."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", "## Wrap\n\n##### CI\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\nplease see tgt.md §CI now\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [g["title"] for item in items for g in item["guarded"]] == ["CI"]

    @pytest.mark.parametrize(
        ("wanted", "cited"),
        [
            pytest.param("phase 4", "phase 4a execution gate", id="citation-names-a-part-inside-the-section"),
            pytest.param("output routing rules", "output routing", id="citation-drops-the-tail-words"),
            pytest.param("output routing", "output routing rules", id="citation-runs-on-by-words"),
            pytest.param("build", "building", id="shared-first-letters"),
        ],
    )
    def test_a_long_prefix_still_names_the_section(self, wanted: str, cited: str) -> None:
        """The prefix rule is loose on purpose: a citation of ``Phase 4a`` protects the section ``Phase 4``.

        A missing guard lets a cited heading vanish unnoticed, while an extra guard only asks for one more repoint, so
        the rule errs towards guarding. Real example: ``prepare.md \u00a7Phase 4a execution gate`` points into the
        heading ``Phase 4: Demo and summary``.
        """
        assert ci._same_heading(wanted, cited) is True

    def test_a_child_skill_of_a_registered_root_brings_its_fragments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``skills: ["."]``, a folder holding ``SKILL.md`` is one skill together with its Markdown fragments."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"skills": ["."]}))
        _write(tmp_path, "plugins/other/b/SKILL.md", "## B\n\nrun\n")
        _write(tmp_path, "plugins/other/b/modes/task.md", "## Task\n\nfragment\n")
        _write(tmp_path, "plugins/other/c/notes.md", "## Notes\n\nnot a skill folder\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert sorted((item["file"], item["group"]) for item in items) == [
            ("plugins/other/b/SKILL.md", "lazy:other:skills/b"),
            ("plugins/other/b/modes/task.md", "lazy:other:skills/b"),
        ]

    @pytest.mark.parametrize(
        "version",
        [
            pytest.param(True, id="boolean"),
            pytest.param({"v": 1}, id="object"),
            pytest.param("1", id="text"),
            pytest.param(2, id="other-number"),
        ],
    )
    def test_schema_version_must_be_the_plain_integer(self, version: object) -> None:
        """A boolean, object, text or other number is not schema version 1, in an inventory or a validate report."""
        assert ci._inventory_problems({"schema_version": version, "root": ".", "kinds": ["rules"]}) != []
        assert ci._vetted_problems({"schema_version": version, "clusters": []}) != []

    def test_the_genuine_schema_version_passes(self) -> None:
        """Version 1 with its sibling fields is accepted by both checks."""
        assert ci._inventory_problems({"schema_version": 1, "root": ".", "kinds": ["rules"]}) == []
        assert ci._vetted_problems({"schema_version": 1, "floor": 1500, "max_judged": 20, "clusters": []}) == []


class TestRoundTwentyFiveRoutes:
    """Covers lists of sections that wrap, invalid YAML beside ``paths:`` and a registered child skill named like a
    folder."""

    @pytest.mark.parametrize(
        ("citing", "expected"),
        [
            pytest.param("see tgt.md §First,\n§Second", ["First", "Second"], id="comma-then-sign-on-the-next-line"),
            pytest.param("see tgt.md §First\n§Second", ["First", "Second"], id="no-comma"),
            pytest.param("see tgt.md §First and\nalso §Second", ["First", "Second"], id="sign-later-in-the-next-line"),
            pytest.param("see tgt.md §First,\n§Second,\n§Third", ["First", "Second", "Third"], id="three-lines"),
            pytest.param("see tgt.md §First;\n§Second", ["First"], id="semicolon-ends-the-list"),
            pytest.param("see tgt.md §First,\n- §Second", ["First"], id="list-item-is-a-new-block"),
            pytest.param("see tgt.md §First,\n> §Second", ["First"], id="quote-is-a-new-block"),
            pytest.param("see tgt.md §First\n\n§Second", ["First"], id="blank-line-ends-the-paragraph"),
            pytest.param("see tgt.md §First,\nother.md §Second", ["First"], id="next-line-cites-another-file"),
        ],
    )
    def test_a_wrapped_list_of_sections_is_one_citation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, citing: str, expected: list[str]
    ) -> None:
        """Section signs on the following lines of the same paragraph are cited like those on the first line."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", "## First\n\nx\n\n## Second\n\ny\n\n## Third\n\nz\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", f"## Steps\n\n{citing}\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert sorted(g["title"] for item in items for g in item["guarded"]) == expected

    @pytest.mark.parametrize(
        "other",
        [
            pytest.param("description: [", id="unclosed-flow-list"),
            pytest.param("description: a: b", id="colon-inside-a-plain-scalar"),
            pytest.param("description: x:", id="trailing-colon"),
            pytest.param('description: "open', id="unclosed-double-quote"),
            pytest.param("description: 'open", id="unclosed-single-quote"),
            pytest.param('description: "bad \\q escape"', id="unknown-escape"),
            pytest.param("description: &a x", id="anchor"),
            pytest.param("description: !tag x", id="tag"),
            pytest.param("description: {a: 1}", id="flow-mapping"),
            pytest.param("description: |\n  deep\n shallow", id="block-scalar-dedent"),
            pytest.param("description: ok\n  more: x", id="mapping-inside-a-plain-scalar"),
            pytest.param("description:\n  - a\n  b", id="list-then-text"),
            pytest.param("description: 'a'\n  extra", id="text-after-a-closed-quote"),
            pytest.param("[oops]", id="flow-line-at-column-zero"),
        ],
    )
    @pytest.mark.parametrize("before", [pytest.param(True, id="before-paths"), pytest.param(False, id="after-paths")])
    def test_invalid_yaml_beside_paths_makes_the_scope_unreadable(self, other: str, before: bool) -> None:
        """A frontmatter block PyYAML refuses is refused whole: its ``paths:`` is not a scope, wherever the bad entry
        is."""
        lines = [other, "paths: src/**"] if before else ["paths: src/**", other]
        assert ci.parse_paths("---\n" + "\n".join(lines) + "\n---\n") == [ci.UNPARSED_PATHS]

    @pytest.mark.parametrize(
        "other",
        [
            pytest.param("description: plain text, with a comma", id="plain"),
            pytest.param('description: "quoted: with a colon"', id="double-quoted"),
            pytest.param("description: 'it''s fine'", id="single-quoted"),
            pytest.param("description: text  # comment", id="trailing-comment"),
            pytest.param("description: >\n  folded\n  text", id="folded-block"),
            pytest.param("description: |-\n  literal\n   deeper\n  back", id="literal-block"),
            pytest.param("description: first\n  second line", id="multi-line-plain"),
            pytest.param("tags:\n- a\n- b", id="block-list-at-column-zero"),
            pytest.param("tags:\n  - a\n  - 'b'", id="indented-block-list"),
            pytest.param("allowed-tools: [Read, Bash]", id="flow-list"),
            pytest.param("empty: []", id="empty-flow-list"),
            pytest.param("nested:\n  key: value", id="flat-mapping"),
            pytest.param("nothing:", id="empty-value"),
        ],
    )
    def test_ordinary_entries_beside_paths_still_read(self, other: str) -> None:
        """Common frontmatter spellings next to ``paths:`` leave the scope readable."""
        assert ci.parse_paths(f"---\n{other}\npaths: src/**\n---\n") == ["src/**"]
        assert ci.parse_paths(f"---\npaths: [a, b]\n{other}\n---\n") == ["a", "b"]

    def test_never_reads_a_block_a_yaml_parser_refuses_beside_paths(self) -> None:
        """Whenever PyYAML rejects a frontmatter holding ``paths:`` and one more entry, ``parse_paths`` is unreadable.

        Every value, one to two continuation lines and both orders; skipped when PyYAML is not installed.
        """
        yaml = pytest.importorskip("yaml")
        values = [
            "x", "a: b", "[", "[a,b]", "[]", "{}", "{a: 1}", '"q"', '"q', "'q'", "'q", "&a x", "*a", "!t x", "|", ">-",
            "# c", "a #c", "a#c", "-", "- x", "? k", "%d", "@x", "`x", "x:", "1", "~", '"\\q"', '"\\x4"', '"\\u00e9"',
            "a, b", "[a, 1]", "|2", '"a" b', "'a' 'b'", "a 'b'", "[a, b] c", "[a] # c", "a ] b", "a, [b]", "'a'#c",
        ]  # fmt: skip
        extras = ["", "  more", "  - i", "  k: v", "- i", " x: y", "  # c", "   deeper", "oops"]
        accepted = []
        for value in values:
            for count in (0, 1, 2):
                for combo in itertools.product(extras, repeat=count):
                    other = "\n".join(["description: " + value, *combo])
                    for front in (other + "\npaths: src/**", "paths: src/**\n" + other, "paths: [a, b]\n" + other):
                        if ci.parse_paths(f"---\n{front}\n---\n") in ([], [ci.UNPARSED_PATHS]):
                            continue
                        try:
                            yaml.safe_load(front)
                        except (yaml.YAMLError, ValueError, OverflowError):
                            accepted.append(front)
        assert accepted == []

    def test_a_registered_child_skill_is_found_whatever_the_folder_is_called(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``skills: ["."]``, a folder named ``docs`` that holds ``SKILL.md`` is a skill with its fragments."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"skills": ["."]}))
        _write(tmp_path, "plugins/other/docs/SKILL.md", "## Docs\n\nrun\n")
        _write(tmp_path, "plugins/other/docs/modes/task.md", "## Task\n\nfragment\n")
        _write(tmp_path, "plugins/other/scripts/notes.md", "## Notes\n\nno SKILL.md here\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert sorted((item["file"], item["group"]) for item in items) == [
            ("plugins/other/docs/SKILL.md", "lazy:other:skills/docs"),
            ("plugins/other/docs/modes/task.md", "lazy:other:skills/docs"),
        ]


class TestRoundTwentySixRoutes:
    """Covers wrapped citations inside blockquotes, command files and a root skill beside child skills."""

    @pytest.mark.parametrize(
        ("citing", "expected"),
        [
            pytest.param("> see tgt.md §First,\n> §Second", ["First", "Second"], id="quoted-wrapped-list"),
            pytest.param("> see tgt.md §First,\n§Second", ["First", "Second"], id="quoted-then-lazy-line"),
            pytest.param("> > see tgt.md §First,\n> > §Second", ["First", "Second"], id="nested-quote"),
            pytest.param("> see tgt.md §First\n>\n> §Second", ["First"], id="quoted-blank-line-ends-it"),
            pytest.param("> see tgt.md §First,\n> - §Second", ["First"], id="quoted-list-item-is-new-block"),
            pytest.param("> see tgt.md §First,\n> > §Second", ["First"], id="deeper-quote-is-another-container"),
            pytest.param("see tgt.md §First,\n> §Second", ["First"], id="unquoted-then-quote"),
        ],
    )
    def test_a_wrapped_list_inside_a_blockquote_is_one_citation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, citing: str, expected: list[str]
    ) -> None:
        """Quote markers at the start of the following lines are container syntax, not a new block."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", "## First\n\nx\n\n## Second\n\ny\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", f"## Steps\n\n{citing}\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert sorted(g["title"] for item in items for g in item["guarded"]) == expected

    def test_a_title_wrapped_inside_a_blockquote_is_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``> tgt.md §CI`` then ``> CD`` cites the heading ``CI CD`` without the quote marker in the title."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", "## Wrap\n\n##### CI CD\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\n> see tgt.md §CI\n> CD later\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [g["title"] for item in items for g in item["guarded"]] == ["CI CD"]

    @pytest.mark.parametrize(
        ("manifest", "files", "expected"),
        [
            pytest.param({}, ["commands/run.md"], ["commands/run.md"], id="default-directory"),
            pytest.param({}, ["commands/sub/deep.md"], ["commands/sub/deep.md"], id="default-subfolder"),
            pytest.param({"commands": "./custom"}, ["custom/run.md"], ["custom/run.md"], id="registered-directory"),
            pytest.param({"commands": ["./a.md", "./b.md"]}, ["a.md", "b.md"], ["a.md", "b.md"], id="listed-files"),
            pytest.param(
                {"commands": {"go": {"source": "./x/go.md"}, "say": {"content": "inline"}}},
                ["x/go.md"],
                ["x/go.md"],
                id="object-map-source-and-inline",
            ),
            pytest.param(
                {"commands": "./custom"},
                ["commands/old.md", "custom/new.md"],
                ["custom/new.md"],
                id="key-replaces-default",
            ),
            pytest.param({"commands": "custom"}, ["custom/run.md"], [], id="path-without-prefix-loads-nothing"),
            pytest.param({}, ["commands/notes.txt"], [], id="other-extensions-are-not-commands"),
        ],
    )
    def test_command_files_are_inventoried_as_lazy_skill_text(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        manifest: dict[str, object],
        files: list[str],
        expected: list[str],
    ) -> None:
        """A command is loaded when invoked, like a skill: it is covered, in one lazy group, and never merged."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"name": "other", **manifest}))
        for name in files:
            _write(tmp_path, f"plugins/other/{name}", "## Run\n\nbody\n")
        items = ci.build_inventory(tmp_path, ("skills",))["items"]
        assert sorted(item["file"].removeprefix("plugins/other/") for item in items) == expected
        assert {item["group"] for item in items} <= {"lazy:other:skills/_commands"}

    def test_commands_are_left_out_when_skills_are_not_asked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the skills kind covers command files."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"name": "other"}))
        _write(tmp_path, "plugins/other/commands/run.md", "## Run\n\nbody\n")
        assert ci.build_inventory(tmp_path, ("rules",))["items"] == []

    def test_a_root_skill_and_its_child_skills_are_all_covered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``skills: ["."]``, a root ``SKILL.md`` does not hide a child folder's ``SKILL.md`` and fragments."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/other/.claude-plugin/plugin.json", json.dumps({"skills": ["."]}))
        _write(tmp_path, "plugins/other/SKILL.md", "## Root\n\nrun\n")
        _write(tmp_path, "plugins/other/docs/SKILL.md", "## Docs\n\nrun\n")
        _write(tmp_path, "plugins/other/docs/modes/extra.md", "## Extra\n\nfragment\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert sorted((item["file"], item["group"]) for item in items) == [
            ("plugins/other/SKILL.md", "lazy:other:skills/other"),
            ("plugins/other/docs/SKILL.md", "lazy:other:skills/docs"),
            ("plugins/other/docs/modes/extra.md", "lazy:other:skills/docs"),
        ]


class TestRoundTwentySevenRoutes:
    """Covers citations wrapped inside any container, read from the block reader's paragraphs."""

    @pytest.mark.parametrize(
        ("citing", "expected"),
        [
            pytest.param("- > see tgt.md §First,\n  > §Second", ["First", "Second"], id="quote-inside-a-list-item"),
            pytest.param("> - see tgt.md §First,\n>   §Second", ["First", "Second"], id="list-item-inside-a-quote"),
            pytest.param("- see tgt.md §First,\n  §Second", ["First", "Second"], id="list-item-continuation"),
            pytest.param("1. see tgt.md §First,\n   §Second", ["First", "Second"], id="numbered-item-continuation"),
            pytest.param("- see tgt.md §First,\n§Second", ["First", "Second"], id="lazy-line-of-a-list-item"),
            pytest.param("- - see tgt.md §First,\n    §Second", ["First", "Second"], id="nested-list-items"),
            pytest.param("> - > see tgt.md §First,\n>   > §Second", ["First", "Second"], id="quote-list-quote"),
            pytest.param("- see tgt.md §First,\n- §Second", ["First"], id="next-item-is-a-new-block"),
            pytest.param("- see tgt.md §First,\n\n  §Second", ["First"], id="blank-line-inside-an-item"),
            pytest.param("- > see tgt.md §First,\n  >\n  > §Second", ["First"], id="blank-quoted-line"),
            pytest.param("- see tgt.md §First,\n  > §Second", ["First"], id="quote-inside-the-item-is-a-new-block"),
        ],
    )
    def test_a_wrapped_list_in_any_container_is_one_citation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, citing: str, expected: list[str]
    ) -> None:
        """The container markers of the following lines are not text: the paragraph decides what is one citation."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", "## First\n\nx\n\n## Second\n\ny\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", f"## Steps\n\n{citing}\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert sorted(g["title"] for item in items for g in item["guarded"]) == expected

    def test_a_title_wrapped_inside_a_list_item_is_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``- tgt.md §CI`` then ``  CD`` cites ``CI CD`` without the item's indent in the title."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", "## Wrap\n\n##### CI CD\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\n- see tgt.md §CI\n  CD later\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [g["title"] for item in items for g in item["guarded"]] == ["CI CD"]

    def test_a_file_name_and_its_section_sign_on_two_quoted_lines_are_one_citation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``> tgt.md`` then ``> §CI`` names the file and the section across the quote marker."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", "## Wrap\n\n##### CI\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\n> see tgt.md\n> §CI\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [g["title"] for item in items for g in item["guarded"]] == ["CI"]

    def test_the_paragraphs_match_a_commonmark_parser(self) -> None:
        """Every paragraph markdown-it reports is one the reader reports, and the reader adds only setext headings.

        All sequences of one to three fragments from a pool of container openers, continuations and breaks; skipped when
        markdown-it-py is not installed.
        """
        markdown_it = pytest.importorskip("markdown_it")
        parser = markdown_it.MarkdownIt("commonmark")
        pool = [
            "text", "- item", "  more", "> quote", "> more", ">", "1. num", "   cont", "- > both", "  > cont", "", "---",
            "# Head", "```", "    code", "[a]: /u", "<div>", "   - nested", "> - li", ">   cont",
        ]  # fmt: skip

        def words(content: str) -> str:
            return "\n".join(" ".join(line.split()) for line in content.split("\n")).strip()

        wrong = []
        for count in (1, 2, 3):
            for combo in itertools.product(pool, repeat=count):
                text = "\n".join(combo) + "\n"
                tokens = parser.parse(text)
                want = {
                    (t.map[0], words(tokens[i + 1].content)) for i, t in enumerate(tokens) if t.type == "paragraph_open"
                }
                heads = {t.map[0] for t in tokens if t.type == "heading_open"}
                found: list[list[tuple[int, str]]] = []
                ci._scan_blocks(ci._lines(text), paragraphs=found)
                got = {(para[0][0], words("\n".join(row for _, row in para))) for para in found}
                if want - got or {p for p in got - want if p[0] not in heads}:
                    wrong.append(text)
        assert wrong == []


class TestRoundTwentyEightRoutes:
    """Covers wrapped file names, container-free code blocks, heading units, long titles and unloadable typed values."""

    def test_a_file_name_wrapped_over_a_line_break_is_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``see my`` then ``rules.md §Target`` names the file ``my rules.md``, as it does on one line."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/my rules.md", "## Target\n\nx\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", "## Steps\n\nsee my\nrules.md §Target\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [g["title"] for item in items for g in item["guarded"]] == ["Target"]

    @pytest.mark.parametrize(
        ("citing", "expected"),
        [
            pytest.param("> ```\n> see tgt.md §First,\n> §Second\n> ```", ["First", "Second"], id="quoted-fence"),
            pytest.param(
                "- ```\n  see tgt.md §First,\n  §Second\n  ```", ["First", "Second"], id="fence-in-a-list-item"
            ),
            pytest.param("```\nsee tgt.md §First,\n§Second\n```", ["First", "Second"], id="plain-fence"),
            pytest.param("> see my\n> rules.md §First", ["First"], id="file-name-wrapped-inside-a-quote"),
            pytest.param("# tgt.md §First\n# §Second", ["First"], id="heading-then-heading"),
            pytest.param("# tgt.md §First\n<!-- c -->\n# §Second", ["First"], id="heading-comment-heading"),
            pytest.param("# tgt.md §First\n[a]: /u\n# §Second", ["First"], id="heading-definition-heading"),
            pytest.param("> # tgt.md §First\n> # §Second", ["First"], id="quoted-headings"),
            pytest.param("tgt.md §First\n===\n# §Second", ["First"], id="setext-heading-then-heading"),
        ],
    )
    def test_a_citation_never_runs_across_leaf_blocks_and_always_across_container_markers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, citing: str, expected: list[str]
    ) -> None:
        """Code inside a container is read without its markers; one heading is never the rest of the one above."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", "## First\n\nx\n\n## Second\n\ny\n")
        _write(tmp_path, "plugins/cc_a/skills/t/my rules.md", "## First\n\nx\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", f"## Steps\n\n{citing}\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert sorted({g["title"] for item in items for g in item["guarded"]}) == expected

    def test_a_title_longer_than_the_default_tail_is_still_guarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A heading of 2,100 characters is found: the cited text is kept as long as the longest heading needs."""
        monkeypatch.setattr(ci, "MANIFEST", [])
        title = "CI;" + "x" * 2100
        _write(tmp_path, "plugins/cc_a/skills/t/tgt.md", f"## Wrap\n\n##### {title}\nbody\n")
        _write(tmp_path, "plugins/cc_a/skills/s/SKILL.md", f"## Steps\n\nsee tgt.md §{title}\n")
        items = ci.build_inventory(tmp_path, ci.ALL_KINDS)["items"]
        assert [g["title"] for item in items for g in item["guarded"]] == [title]

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("2026-99-99", id="impossible-date"),
            pytest.param("2026-01-01", id="date"),
            pytest.param("2026-01-01 10:00:00", id="timestamp"),
            pytest.param("9" * 5000, id="digits-too-long-to-convert"),
            pytest.param("0b_", id="empty-binary"),
            pytest.param("0x_", id="empty-hex"),
            pytest.param("1_000", id="underscored-number"),
            pytest.param("<<", id="merge-key-as-a-value"),
            pytest.param("=", id="value-tag"),
            pytest.param("1e5", id="exponent-without-a-dot"),
        ],
    )
    def test_a_value_yaml_cannot_construct_makes_the_scope_unreadable(self, value: str) -> None:
        """A neighbouring entry whose typed value raises while the document loads leaves ``paths:`` unreadable."""
        assert ci.parse_paths(f"---\npaths: src/**\nother: {value}\n---\n") == [ci.UNPARSED_PATHS]

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("12", id="integer"),
            pytest.param("-3", id="negative-integer"),
            pytest.param("1.5", id="float"),
            pytest.param("true", id="boolean"),
            pytest.param("null", id="null"),
            pytest.param("v1.2.3", id="version-string"),
            pytest.param("2026-weekly", id="date-like-text"),
        ],
    )
    def test_ordinary_typed_values_beside_paths_still_read(self, value: str) -> None:
        """Short numbers, booleans and version strings next to ``paths:`` leave the scope readable."""
        assert ci.parse_paths(f"---\npaths: src/**\nother: {value}\n---\n") == ["src/**"]

    def test_never_reads_a_block_whose_typed_value_fails_to_load(self) -> None:
        """Whenever PyYAML fails to load an entry beside ``paths:``, ``parse_paths`` is unreadable.

        Every one to three characters of number, date, sign, colon, underscore and tag spelling; skipped when PyYAML is
        not installed.
        """
        yaml = pytest.importorskip("yaml")
        alphabet = "0123456789_.-+:eExXbBoO<=~ "
        accepted = []
        for count in (1, 2, 3):
            for combo in itertools.product(alphabet, repeat=count):
                value = "".join(combo)
                front = f"paths: src/**\nother: {value}"
                if ci.parse_paths(f"---\n{front}\n---\n") in ([], [ci.UNPARSED_PATHS]):
                    continue
                try:
                    yaml.safe_load(front)
                except (yaml.YAMLError, ValueError, OverflowError):
                    accepted.append(front)
        assert accepted == []
