"""Tests for ``bin/mint_finding_ids.py`` and ``bin/merge_action_items.py``.

Both scripts implement the review → resolve handoff: the review mints stable finding ids, resolve folds those findings
into its numbered item list without renumbering. Files are real JSONL under ``tmp_path``; only the external Git Bash
path converter is mocked.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest import mock

import find_review_report as frr
import merge_action_items as mai
import mint_finding_ids as mfi
import pytest

_TAXONOMY = Path(__file__).parents[2] / "skills" / "_shared" / "review-section-taxonomy.md"


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    """Write ``records`` one per line and return ``path``."""
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8", newline="\n")
    return path


def _write_files(root: Path, files: dict[str, str]) -> None:
    """Create each ``relative path -> text`` entry under ``root``, making parent directories."""
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")


def _finding(**overrides: object) -> dict:
    """Return a complete review finding, overridable per test."""
    record = {
        "section": "### Architecture & Quality",
        "severity": "high",
        "title": "Missing None check",
        "full_text": "[high] `load()` dereferences `cfg` without a None check (src/app.py:12)",
        "file": "src/app.py",
        "line": 12,
        "change": "code",
        "author": "foundry:sw-engineer",
        "source_file": "run/foundry--sw-engineer.md",
        "verify_file": "run/verify-1.md",
        "verify_verdict": "CONFIRMED",
    }
    record.update(overrides)
    return record


def _findings_file(tmp_path: Path, records: list[dict]) -> Path:
    """Write minted findings where the review keeps them: a report directory, not the resolve item directory."""
    review_dir = tmp_path / "review"
    review_dir.mkdir(exist_ok=True)
    return _write_jsonl(review_dir / "findings.jsonl", mfi.mint(records))


def _findings_beside_items(tmp_path: Path, records: list[dict] | None = None) -> Path:
    """Write minted findings (default: one ``_finding()``) as ``f.jsonl`` in the same directory as the item list."""
    return _write_jsonl(tmp_path / "f.jsonl", mfi.mint(records if records is not None else [_finding()]))


def _gh(item_id: int, **overrides: object) -> dict:
    """Return a pending GitHub action item, overridable per test."""
    record = {
        "id": item_id,
        "type": "[gh][req]",
        "change": "code",
        "severity": 3,
        "author": "@reviewer",
        "summary": "guard cfg",
        "file": "src/app.py",
        "line": 12,
        "url": f"https://example.invalid/c/{item_id}",
        "full_comment_text": "cfg can be None here",
        "location": "inline",
        "origin": "posted",
    }
    record.update(overrides)
    return record


def _survivor(**overrides: object) -> dict:
    """Return the surviving row of a root-cause merge: the one finding the report prints for the group."""
    return _finding(**{"candidate_id": "cand-survivor", **overrides})


def _merged(**overrides: object) -> dict:
    """Return a confirmed row the consolidator merged into ``_survivor``; the report does not print it on its own."""
    record = {
        "candidate_id": "cand-merged",
        "merged_into": "cand-survivor",
        "title": "Pool capacity leaks",
        "full_text": "[high] `release()` never returns the slot (src/pool.py:40)",
        "file": "src/pool.py",
        "line": 40,
    }
    return _finding(**{**record, **overrides})


def _recheck_argv(items: Path, review_dir: Path, head: str) -> list[str]:
    """Return the ``--recheck-verdicts`` command line for ``items`` against ``review_dir`` at ``head``."""
    return ["--items", str(items), "--recheck-verdicts", "--review-dir", str(review_dir), "--current-head", head]


@pytest.fixture
def review_dir(tmp_path: Path) -> Path:
    """Return a review directory that recorded head ``abc1234``."""
    _write_files(tmp_path, {"review/head-sha.txt": "abc1234\n"})
    return tmp_path / "review"


class TestMint:
    """``mint_finding_ids``: stable, collision-free finding ids."""

    @pytest.mark.parametrize(
        ("section", "slug"),
        [
            pytest.param("### [blocking] Critical (must fix before merge)", "critical", id="blocking-critical"),
            pytest.param("### ⚠ LOW CONFIDENCE — API Design (if applicable)", "api-design", id="low-confidence-prefix"),
            pytest.param("### Architecture & Quality", "architecture-quality", id="ampersand"),
        ],
    )
    def test_section_slug_drops_decoration(self, section: str, slug: str) -> None:
        """Heading marks, confidence prefix, blocking tag and qualifiers never change the slug.

        The slug prefixes every finding id, so decoration that leaked in would rename ids between review runs.
        """
        assert mfi.section_slug(section) == slug

    def test_id_is_stable_under_line_drift_and_path_spelling(self) -> None:
        """The same finding at another line, spelled with Windows separators, keeps its id.

        Ids name findings in the resolution ledger across re-runs, so cosmetic drift in the report must not rename them.
        """
        first = mfi.mint([_finding()])[0]["id"]
        moved = mfi.mint([_finding(line=40, file=".\\src\\app.py", title="  missing none CHECK. ")])[0]["id"]
        assert first == moved
        assert re.fullmatch(r"architecture-quality-[0-9a-f]{8}", first)

    def test_collisions_get_suffixes_in_file_order(self) -> None:
        """Two findings that hash alike get deterministic ``-2`` suffixes.

        Two findings differing only by line hash to one base id; the second must stay addressable on its own.
        """
        out = mfi.mint([_finding(), _finding(line=99)])
        assert out[1]["id"] == out[0]["id"] + "-2"

    def test_collision_suffix_ignores_report_order(self) -> None:
        """Colliding findings keep their ids when the report lists them in another order.

        A resolution ledger names findings by id; if suffixes followed report order, a re-run that reordered two
        colliding findings would attach one finding's outcome to the other.
        """
        first, second = _finding(full_text="cfg may be None"), _finding(line=99, full_text="cfg reloaded twice")
        forward = mfi.mint([dict(first), dict(second)])
        backward = mfi.mint([dict(second, line=140), dict(first, line=3)])
        assert {f["full_text"]: f["id"] for f in forward} == {f["full_text"]: f["id"] for f in backward}

    def test_cli_rejects_incomplete_record(self, tmp_path: Path) -> None:
        """A record without a title blocks instead of minting a weak id.

        An empty title would hash to the same id for unrelated findings, so the CLI exits 1 before writing anything.
        """
        path = _write_jsonl(tmp_path / "findings.jsonl", [_finding(title="")])
        assert mfi.main([str(path)]) == 1

    def test_carry_from_keeps_a_carried_row_id_against_an_outranking_collision(self, tmp_path: Path) -> None:
        """A row equal to a prior row except its id keeps that id; a colliding new row takes the next free suffix.

        Without ``--carry-from`` the new row's text outranks the carried one and takes the base id, re-suffixing a row a
        fill-gaps run must publish verbatim.
        """
        prior_path = _write_jsonl(tmp_path / "prior.jsonl", mfi.mint([_finding(full_text="alpha")]))
        carried = mfi.read_jsonl(prior_path)[0]
        new_path = _write_jsonl(tmp_path / "findings.jsonl", [carried, _finding(full_text="beta", line=40)])
        assert mfi.main([str(new_path), "--carry-from", str(prior_path)]) == 0
        minted = mfi.read_jsonl(new_path)
        assert (minted[0], minted[1]["id"]) == (carried, carried["id"] + "-2")

    def test_without_carry_from_ignores_prior_ids(self, tmp_path: Path) -> None:
        """Without ``--carry-from`` minting ignores prior ids exactly as before, outranking collisions included.

        The carry option must stay opt-in: plain minting of an already-minted file still lets the better-ranked row win.
        """
        carried = mfi.mint([_finding(full_text="alpha")])[0]
        path = _write_jsonl(tmp_path / "findings.jsonl", [carried, _finding(full_text="beta", line=40)])
        assert mfi.main([str(path)]) == 0
        assert [row["id"] for row in mfi.read_jsonl(path)] == [carried["id"] + "-2", carried["id"]]


class TestResolveType:
    """``merge_action_items.resolve_type``: the taxonomy's Severity → Resolve Type table."""

    @pytest.mark.parametrize(
        ("severity", "section", "expected"),
        [
            pytest.param("low", "### [blocking] Critical (must fix before merge)", "[report][req]", id="blocking-low"),
            pytest.param(
                "medium", "### [blocking] Critical (must fix before merge)", "[report][req]", id="blocking-medium"
            ),
            pytest.param("high", "### Test Coverage", "[report][req]", id="high-any-section"),
            pytest.param("medium", "### Performance Concerns", "[report][req]", id="code-medium"),
            pytest.param("medium", "### Test Coverage", "[report][suggest]", id="non-code-medium"),
            pytest.param("low", "### Test Coverage", "[report][suggest]", id="plain-low"),
        ],
    )
    def test_follows_taxonomy_table(self, severity: str, section: str, expected: str) -> None:
        """Each finding maps to the resolve type the taxonomy's Severity → Resolve Type table assigns.

        The review places a `[blocking]`-tagged finding in the blocking bucket at its own severity, so a low-severity
        test gap tagged Block-tier must still reach resolve as required work, not an optional suggestion.
        """
        assert mai.resolve_type({"severity": severity, "section": section}) == expected

    def test_medium_req_sections_match_taxonomy(self) -> None:
        """The script's MEDIUM → [req] sections are exactly the taxonomy's code-related MEDIUM rows plus Critical.

        The taxonomy table is the contract; this reads its MEDIUM row cell by cell so the script cannot drift from it.
        """
        text = _TAXONOMY.read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| MEDIUM |") and "`[req]`" in line)
        names = [part.strip() for part in row.split("|")[2].replace("(code-related)", "").split(",")]
        headers = {
            "Architecture": "### Architecture & Quality",
            "Performance": "### Performance Concerns",
            "API Design": "### API Design (if applicable)",
        }
        expected = {mfi.section_slug(headers[name]) for name in names} | {"critical"}
        assert expected == set(mai.REQ_MEDIUM_SECTIONS)


def test_severity_vocabulary_is_shared_by_mint_gate_and_merge() -> None:
    """Mint, the producer gate and the merge accept the same severity words, scored most severe first.

    Three scripts validate severity independently of each other's tests; one shared vocabulary keeps a finding the
    review accepts from being refused by the merge.
    """
    expected_scores = {"critical": 5, "high": 4, "medium": 3, "low": 2, "cosmetic": 1}
    assert (mfi.SEVERITIES, mai.SEVERITY_SCORE) == (frr.FINDING_SEVERITIES, expected_scores)


class TestMerge:
    """``merge_action_items.main``: fold review findings into the numbered item list without renumbering."""

    def test_report_mode_assigns_ids_from_one(self, tmp_path: Path) -> None:
        """With no GitHub items, findings become report items 1..N in findings order.

        This is the plain `oss:review` handoff: no PR comments exist, so every item is a fresh `[report]` item.
        """
        findings = _findings_file(tmp_path, [_finding(), _finding(title="Other", line=3)])
        items = tmp_path / "action-items.jsonl"
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
        rows = mfi.read_jsonl(items)
        assert [r["id"] for r in rows] == [1, 2]
        assert rows[0]["type"] == "[report][req]"
        assert rows[0]["location"] == "report"
        assert rows[0]["finding_id"].startswith("architecture-quality-")
        assert rows[0]["verify_verdict"] == "CONFIRMED"

    def test_exact_match_annotates_github_item_without_verdict(self, tmp_path: Path) -> None:
        """A finding at the same file:line enriches the terse GitHub item; its id never changes.

        The verifier confirmed the review's claim, not the GitHub comment's, so the item must not inherit the verdict.
        """
        items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1), _gh(2, file="src/other.py", line=5)])
        findings = _findings_file(tmp_path, [_finding()])
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
        rows = {r["id"]: r for r in mfi.read_jsonl(items)}
        assert set(rows) == {1, 2}
        assert rows[1]["author"] == "@reviewer + foundry:sw-engineer"
        assert "(also flagged by /review — foundry:sw-engineer)" in rows[1]["summary"]
        assert "[review finding architecture-quality-" in rows[1]["full_comment_text"]
        assert "verify_verdict" not in rows[1]
        assert rows[1]["source_file"] == "run/foundry--sw-engineer.md"
        assert rows[1]["severity"] == 4

    def test_semantic_link_inherits_detail_but_not_verdict(self, tmp_path: Path) -> None:
        """A judged same-target pair at another line must still be re-checked by the challenge phase.

        The orchestrator links a finding to a GitHub item on the same file but another line; the item gains the
        finding's detail while the verdict stays off, so the challenge phase re-verifies it.
        """
        items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, line=30)])
        minted = mfi.mint([_finding()])
        findings = _write_jsonl(tmp_path / "f.jsonl", minted)
        links = _write_jsonl(tmp_path / "links.jsonl", [{"finding_id": minted[0]["id"], "item_id": 1}])
        assert mai.main(["--items", str(items), "--findings", str(findings), "--links", str(links)]) == 0
        (row,) = mfi.read_jsonl(items)
        assert row["finding_id"] == minted[0]["id"]
        assert row["verify_file"] == "run/verify-1.md"
        assert "verify_verdict" not in row

    @pytest.mark.parametrize(
        ("closed_type", "extra"),
        [
            pytest.param("[done][gh][req]", {}, id="done-without-status"),
            pytest.param("[done][gh][req]", {"status": "pending"}, id="done-with-pending-status"),
            pytest.param("[info][gh][req]", {}, id="info-without-status"),
            pytest.param("[info][gh][req]", {"status": "pending"}, id="info-with-pending-status"),
        ],
    )
    def test_closed_type_is_preserved_on_exact_match(self, tmp_path: Path, closed_type: str, extra: dict) -> None:
        """A closed item at the finding's own location keeps its history and the finding is appended as a new item.

        Closed type markers protect history even when request markers and an open status remain on the row.
        """
        history = _gh(1, type=closed_type, line=12, **extra)
        items = _write_jsonl(tmp_path / "action-items.jsonl", [history])
        before = items.read_bytes()
        findings = _findings_file(tmp_path, [_finding()])
        finding_id = mfi.read_jsonl(findings)[0]["id"]

        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0

        history_row, finding_row = mfi.read_jsonl(items)
        assert history_row == history
        assert items.read_bytes().startswith(before)
        assert (finding_row["id"], finding_row["type"], finding_row["finding_id"]) == (2, "[report][req]", finding_id)
        assert finding_row["full_comment_text"] == _finding()["full_text"]

    @pytest.mark.parametrize(
        ("closed_type", "extra"),
        [
            pytest.param("[done][gh][req]", {}, id="done-without-status"),
            pytest.param("[done][gh][req]", {"status": "pending"}, id="done-with-pending-status"),
            pytest.param("[info][gh][req]", {}, id="info-without-status"),
            pytest.param("[info][gh][req]", {"status": "pending"}, id="info-with-pending-status"),
        ],
    )
    def test_closed_type_is_never_offered_or_linked_semantically(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], closed_type: str, extra: dict
    ) -> None:
        """A closed item on the finding's file is not a link candidate, and a forced link still appends the finding.

        The candidate pass must offer nothing and leave the file untouched; an orchestrator link to the closed item is
        then ignored in favour of appending, so closed history is never rewritten.
        """
        history = _gh(1, type=closed_type, line=30, **extra)
        items = _write_jsonl(tmp_path / "action-items.jsonl", [history])
        before = items.read_bytes()
        findings = _findings_file(tmp_path, [_finding()])
        finding_id = mfi.read_jsonl(findings)[0]["id"]
        argv = ["--items", str(items), "--findings", str(findings)]

        assert mai.main([*argv, "--candidates"]) == 0
        assert json.loads(capsys.readouterr().out) == []
        assert items.read_bytes() == before
        links = _write_jsonl(tmp_path / "links.jsonl", [{"finding_id": finding_id, "item_id": 1}])
        assert mai.main([*argv, "--links", str(links)]) == 0

        history_row, finding_row = mfi.read_jsonl(items)
        assert history_row == history
        assert items.read_bytes().startswith(before)
        assert (finding_row["id"], finding_row["type"], finding_row["finding_id"]) == (2, "[report][req]", finding_id)
        assert finding_row["full_comment_text"] == _finding()["full_text"]

    def test_unmatched_findings_append_after_highest_github_id(self, tmp_path: Path) -> None:
        """Report items take ids above the existing maximum, gaps included; the file is only appended to.

        Item ids are looked up by number later in the flow, so a new item must never reuse or renumber an existing one.
        """
        items = _write_jsonl(
            tmp_path / "action-items.jsonl", [_gh(1, file="x.py", full_comment_text="x" * 100), _gh(4, type="[done]")]
        )
        before = items.read_text(encoding="utf-8")
        findings = _findings_beside_items(tmp_path)
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
        after = items.read_text(encoding="utf-8")
        assert after.startswith(before)
        assert [r["id"] for r in mfi.read_jsonl(items)] == [1, 4, 5]

    def test_unmatched_terse_github_item_is_marked_thin(self, tmp_path: Path) -> None:
        """A short GitHub comment no finding covers gets reassessed from scratch.

        A three-letter comment such as "nit" carries too little to act on, so the item is flagged for a fresh read.
        """
        items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, file="z.py", full_comment_text="nit")])
        findings = _findings_beside_items(tmp_path)
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
        assert mfi.read_jsonl(items)[0]["thin"] is True

    @pytest.mark.parametrize(
        ("overrides", "thin"),
        [
            pytest.param(
                {"file": "", "full_comment_text": "Please add a CHANGELOG entry for the new option. " * 2},
                False,
                id="long-request-without-file",
            ),
            pytest.param({"full_comment_text": "why?", "type": "[gh][question]"}, False, id="question"),
            pytest.param({"file": "", "full_comment_text": "fix this"}, True, id="short-without-file"),
        ],
    )
    def test_thin_flag_marks_only_short_requests(self, tmp_path: Path, overrides: dict, thin: bool) -> None:
        """Only a short comment lacks the detail to act on; a clear request without a file location does not.

        Questions are never thin, and a long request stays actionable even with no file, so only short requests flag.
        """
        items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, line=77, **overrides)])
        findings = _findings_file(tmp_path, [_finding()])
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
        assert bool(mfi.read_jsonl(items)[0].get("thin")) is thin

    def test_candidates_lists_same_file_pairs_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Same-file, different-line pairs are offered for judgment without touching the item file.

        ``--candidates`` is the read-only half of the semantic-link protocol; the orchestrator judges the pairs.
        """
        items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, line=30)])
        before = items.read_text(encoding="utf-8")
        findings = _findings_beside_items(tmp_path)
        assert mai.main(["--items", str(items), "--findings", str(findings), "--candidates"]) == 0
        pairs = json.loads(capsys.readouterr().out)
        assert [(p["item_id"], p["finding_line"]) for p in pairs] == [(1, 12)]
        assert items.read_text(encoding="utf-8") == before

    @pytest.mark.parametrize(
        ("items", "message"),
        [
            pytest.param([_gh(1), _gh(1)], "duplicate", id="duplicate-id"),
            pytest.param([_gh(1, id="1")], "non-positive or non-integer", id="string-id"),
        ],
    )
    def test_bad_item_ids_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], items: list[dict], message: str
    ) -> None:
        """Step 8 looks items up by unique integer id, so anything else stops the merge.

        A duplicated or non-integer id would make a later lookup ambiguous or fail, so the merge refuses up front.
        """
        path = _write_jsonl(tmp_path / "action-items.jsonl", items)
        findings = _findings_beside_items(tmp_path)
        assert mai.main(["--items", str(path), "--findings", str(findings)]) == 1
        assert message in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("finding", "link", "message"),
        [
            pytest.param(
                dict(_finding(), id="architecture-quality-1", section=5), None, "field section is int", id="int-section"
            ),
            pytest.param(dict(_finding(), id=["architecture-quality-1"]), None, "field id is list", id="list-id"),
            pytest.param(
                dict(_finding(), id="architecture-quality-1", file=3), None, "field file is int", id="int-file"
            ),
            pytest.param(
                dict(_finding(), id="architecture-quality-1", line=[12]), None, "field line is list", id="list-line"
            ),
            pytest.param(7, None, "finding 1 is not a JSON object", id="row-not-object"),
            pytest.param(
                dict(_finding(), id="architecture-quality-1", merged_into=["cand-survivor"]),
                None,
                "finding 1 field merged_into is list, not text",
                id="list-merged-into",
            ),
            pytest.param(
                dict(_finding(), id="architecture-quality-1"),
                {"finding_id": ["architecture-quality-1"], "item_id": 1},
                "link 1 is not a {finding_id: text, item_id: number} object",
                id="list-link-finding-id",
            ),
        ],
    )
    def test_wrongly_typed_rows_block_instead_of_crashing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], finding: object, link: dict | None, message: str
    ) -> None:
        """A finding row or link whose fields are not the expected JSON types refuses with ``! BLOCKED``.

        A numeric section used to reach the section slug's string operations and a list id the merge's id sets, raising
        ``AttributeError``/``TypeError`` mid-merge (found by the resolve intake fuzz under ``--accept-degraded``).
        """
        items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1)])
        findings = _write_jsonl(tmp_path / "f.jsonl", [finding])
        links = _write_jsonl(tmp_path / "links.jsonl", [link] if link else [])
        assert mai.main(["--items", str(items), "--findings", str(findings), "--links", str(links)]) == 1
        error = capsys.readouterr().err
        assert error.startswith("! BLOCKED — ")
        assert message in error

    @pytest.mark.parametrize(
        ("finding", "message"),
        [
            pytest.param(
                {key: value for key, value in _finding(id="architecture-quality-1").items() if key != "author"},
                "! BLOCKED — finding 1 missing author — run mint_finding_ids.py and complete the record\n",
                id="missing-author",
            ),
            pytest.param(
                dict(_finding(), id="architecture-quality-1", section=5),
                "! BLOCKED — finding 1 field section is int, not text — the review must write it as one JSON object of "
                "text fields\n",
                id="int-section",
            ),
            pytest.param(
                dict(_finding(), id="architecture-quality-1", severity="major"),
                "! BLOCKED — finding 1 has unknown severity 'major' — use one of critical, high, medium, low, cosmetic\n",
                id="unknown-severity",
            ),
        ],
    )
    def test_refusal_wording_matches_the_producer_gate(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], finding: dict, message: str
    ) -> None:
        """The merge refuses a row with exactly the words ``oss:review``'s producer gate uses for that row.

        The required-field list, the type check and the severity vocabulary live in ``find_review_report`` so the review
        can refuse, before delivery, any row this merge would refuse; an unknown severity used to pass the producer gate
        and block only here.
        """
        items = _write_jsonl(tmp_path / "action-items.jsonl", [])
        findings = _write_jsonl(tmp_path / "f.jsonl", [finding])
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 1
        assert capsys.readouterr().err == message
        assert frr.finding_row_problem(finding, 1) == message.removeprefix("! BLOCKED — ").rstrip("\n")

    def test_findings_sharing_an_id_block_instead_of_collapsing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two findings with one id stop the merge before anything is written; neither is folded into the other.

        Regression: the merge keyed findings by id, so the second row's confirmed finding was silently lost while its
        text replaced the first row's detail on the GitHub item at the first row's line.
        """
        items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1)])
        before = items.read_bytes()
        rows = [_finding(id="dup-1"), _finding(id="dup-1", file="src/other.py", line=9, title="Other")]
        findings = _write_jsonl(tmp_path / "f.jsonl", rows)
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 1
        assert capsys.readouterr().err.startswith("! BLOCKED — duplicate finding id dup-1 on findings 1 and 2")
        assert items.read_bytes() == before

    def test_link_to_unknown_item_blocks(self, tmp_path: Path) -> None:
        """A semantic link to an item that does not exist is an orchestrator error, not a silent drop.

        Silently ignoring it would hide a mismatch between the candidate list the orchestrator judged and the file.
        """
        items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1)])
        minted = mfi.mint([_finding(line=5)])
        findings = _write_jsonl(tmp_path / "f.jsonl", minted)
        links = _write_jsonl(tmp_path / "links.jsonl", [{"finding_id": minted[0]["id"], "item_id": 9}])
        assert mai.main(["--items", str(items), "--findings", str(findings), "--links", str(links)]) == 1

    def test_two_findings_on_one_line_never_share_an_item(self, tmp_path: Path) -> None:
        """The worst finding claims the GitHub item; the other becomes its own item with its own evidence.

        Folding both into one item would let one finding's verifier file vouch for the other's claim and would drop the
        first finding's id from the resolution ledger.
        """
        items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1)])
        unverified = _finding(
            section="### Performance Concerns",
            severity="medium",
            title="Slow lookup",
            verify_file="",
            verify_verdict="",
        )
        findings = _findings_file(tmp_path, [unverified, _finding()])
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
        rows = {r["id"]: r for r in mfi.read_jsonl(items)}
        assert rows[1]["finding_id"].startswith("architecture-quality-")
        assert rows[2]["finding_id"].startswith("performance-concerns-")
        assert "verify_verdict" not in rows[2]
        assert "verify_file" not in rows[2]

    def test_github_item_does_not_inherit_codex_eligibility(self, tmp_path: Path) -> None:
        """Codex eligibility describes the review's claim; a GitHub request on the same line may need a specialist.

        An exact-match finding marked ``codex_eligible`` enriches the GitHub item, but that flag must not be copied.
        """
        items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1)])
        findings = _findings_file(tmp_path, [_finding(codex_eligible=True)])
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
        assert "codex_eligible" not in mfi.read_jsonl(items)[0]

    def test_withdrawn_duplicate_link_frees_its_item(self, tmp_path: Path) -> None:
        """A finding linked twice is ignored, and the item it first claimed stays open for another finding's link.

        The orchestrator may emit contradictory links for one finding; the merge drops that finding's links entirely
        instead of letting the first one lock the item.
        """
        path = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, line=30), _gh(2, line=31)])
        findings = _findings_file(tmp_path, [_finding(line=5), _finding(title="Other", line=6)])
        first, other = (f["id"] for f in mfi.read_jsonl(findings))
        rows = [
            {"finding_id": first, "item_id": 1},
            {"finding_id": first, "item_id": 2},
            {"finding_id": other, "item_id": 1},
        ]
        links = _write_jsonl(tmp_path / "links.jsonl", rows)
        assert mai.main(["--items", str(path), "--findings", str(findings), "--links", str(links)]) == 0
        assert mfi.read_jsonl(path)[0]["finding_id"] == other

    @pytest.mark.parametrize(
        "rows",
        [
            pytest.param([_finding(), _finding(title="Other", line=40)], id="plain-rows"),
            pytest.param([_survivor(), _merged()], id="root-cause-merged-rows"),
        ],
    )
    def test_rerun_changes_nothing(self, tmp_path: Path, rows: list[dict]) -> None:
        """A retried merge block (e.g. after compaction) must not duplicate items or annotations.

        A folded row leaves no ``finding_id`` of its own on any item, so a re-run must recognise it through the survivor
        item's ``merged_finding_ids`` instead of appending it as a new item.
        """
        items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1)])
        findings = _findings_file(tmp_path, rows)
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
        first = items.read_text(encoding="utf-8")
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
        assert items.read_text(encoding="utf-8") == first

    def test_findings_parsed_by_resolve_carry_no_verdict(self, tmp_path: Path) -> None:
        """A findings file beside the item list was written by resolve from an old report, so no verifier backs it.

        Resolve parses an older report into ``report-findings.jsonl`` next to the items; it must not claim a verdict.
        """
        items = tmp_path / "action-items.jsonl"
        findings = _write_jsonl(tmp_path / "report-findings.jsonl", mfi.mint([_finding()]))
        assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
        assert "verify_verdict" not in mfi.read_jsonl(items)[0]

    @pytest.mark.parametrize(
        "items",
        [
            pytest.param([_gh(1, line=30, type="[done]")], id="legacy-done-item"),
            pytest.param([_gh(1, line=30, status="resolved")], id="resolved-item"),
            pytest.param([_gh(1, line=30, status="addressed")], id="addressed-item"),
            pytest.param([_gh(1, line=30, finding_id="api-design-00000000")], id="item-already-holds-a-finding"),
        ],
    )
    def test_link_to_unavailable_item_appends_the_finding(self, tmp_path: Path, items: list[dict]) -> None:
        """A semantic link the item cannot take falls back to appending, so the finding stays selectable.

        Closed, resolved, addressed and already-annotated items refuse a link; the finding is appended, never dropped.
        """
        path = _write_jsonl(tmp_path / "action-items.jsonl", items)
        findings = _findings_file(tmp_path, [_finding()])
        finding_id = mfi.read_jsonl(findings)[0]["id"]
        links = _write_jsonl(tmp_path / "links.jsonl", [{"finding_id": finding_id, "item_id": 1}])
        assert mai.main(["--items", str(path), "--findings", str(findings), "--links", str(links)]) == 0
        assert [r.get("finding_id") for r in mfi.read_jsonl(path)][-1] == finding_id
        assert len(mfi.read_jsonl(path)) == 2


class TestRecheckVerdicts:
    """``--recheck-verdicts``: a stored verdict survives only while its evidence still holds."""

    @pytest.mark.parametrize(
        ("files", "current_head", "kept"),
        [
            pytest.param(
                {"review/head-sha.txt": "abc1234\n", "verify-1.md": "CONFIRMED"}, "abc1234def", True, id="same-head"
            ),
            pytest.param(
                {"review/head-sha.txt": "abc1234\n", "verify-1.md": "CONFIRMED"}, "fff9999", False, id="head-moved"
            ),
            pytest.param({"verify-1.md": "CONFIRMED"}, "abc1234", False, id="review-head-unknown"),
            pytest.param({"review/head-sha.txt": "abc1234\n"}, "abc1234", False, id="verifier-file-swept"),
        ],
    )
    def test_keeps_verdict_only_when_evidence_still_holds(
        self, tmp_path: Path, files: dict[str, str], current_head: str, kept: bool
    ) -> None:
        """A confirmed verdict skips the existence check only while the head and the verifier's file are unchanged.

        The review recorded its head sha beside its report; a moved head, an unknown head or a swept verifier file each
        mean the confirmation can no longer be trusted, so the verdict is dropped for the challenge phase to redo.
        """
        _write_files(tmp_path, files)
        review = tmp_path / "review"
        review.mkdir(exist_ok=True)
        verify = tmp_path / "verify-1.md"
        items = _write_jsonl(
            tmp_path / "action-items.jsonl", [_gh(1, verify_verdict="CONFIRMED", verify_file=str(verify))]
        )
        assert mai.main(_recheck_argv(items, review, current_head)) == 0
        assert ("verify_verdict" in mfi.read_jsonl(items)[0]) is kept

    @pytest.mark.parametrize(
        ("files", "verdict"),
        [
            pytest.param({"review/verify result.md": "Independent confirmation"}, "CONFIRMED", id="verifier-present"),
            pytest.param({}, None, id="verifier-missing"),
        ],
    )
    @mock.patch("merge_action_items.subprocess.run", autospec=True)
    def test_resolves_git_bash_verifier_path_on_windows(
        self,
        mock_run: mock.MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        review_dir: Path,
        files: dict[str, str],
        verdict: str | None,
    ) -> None:
        """Resolve a JSON evidence pointer before testing the real verifier file, preserving its recorded spelling.

        On Windows the verifier path is stored in Git Bash spelling and converted with ``cygpath``; only that converter
        is mocked, so the existence check still reads the real file. The hostile pointer proves it is never shell-run.
        """
        _write_files(tmp_path, files)
        pointer = "/c/review dir/verify $(touch injected) `payload`.md"
        items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, verify_verdict="CONFIRMED", verify_file=pointer)])
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout=str(review_dir / "verify result.md") + "\n")
        monkeypatch.setattr(mai.sys, "platform", "win32")

        assert mai.main(_recheck_argv(items, review_dir, "abc1234")) == 0

        row = mfi.read_jsonl(items)[0]
        assert (row.get("verify_verdict"), row["verify_file"]) == (verdict, pointer)
        mock_run.assert_called_once_with(
            ["cygpath", "-w", pointer], capture_output=True, text=True, check=True, timeout=5
        )
        assert not (tmp_path / "injected").exists()

    @mock.patch(
        "merge_action_items.subprocess.run",
        autospec=True,
        side_effect=subprocess.CalledProcessError(1, ["cygpath", "-w", "/c/review/verify.md"]),
    )
    def test_blocks_on_verifier_path_conversion_failure(
        self, mock_run: mock.MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_dir: Path
    ) -> None:
        """A failed converter must leave the evidence ledger intact and return a blocking failure.

        If ``cygpath`` fails, treating the pointer as missing would silently drop confirmed verdicts, so the recheck
        stops and the item file is byte-identical.
        """
        items = _write_jsonl(
            tmp_path / "action-items.jsonl", [_gh(1, verify_verdict="CONFIRMED", verify_file="/c/review/verify.md")]
        )
        before = items.read_bytes()
        monkeypatch.setattr(mai.sys, "platform", "win32")

        assert mai.main(_recheck_argv(items, review_dir, "abc1234")) == 1

        assert items.read_bytes() == before
        mock_run.assert_called_once()


def _report_args(tmp_path: Path, report: Path, *extra: str) -> list[str]:
    """Return the ``--report-file`` command line resolve's Step 3a/3c blocks use."""
    return [
        "--items",
        str(tmp_path / "impl" / "action-items.jsonl"),
        "--report-file",
        str(report),
        "--impl-dir",
        str(tmp_path / "impl"),
        "--persist-findings",
        str(tmp_path / "findings-sentinel"),
        *extra,
    ]


class TestReportFile:
    """``--report-file``: find the findings that belong to a consumed review report."""

    def test_takes_the_findings_beside_the_report(self, tmp_path: Path) -> None:
        """The findings file beside the consumed report is merged and its path is persisted for the later links step.

        This is the review's own ``findings.jsonl``; resolve must not touch the parsed-copy fallback when it exists, and
        the persisted line is what Step 3c's second block reads back.
        """
        findings = _findings_file(tmp_path, [_finding()])
        (tmp_path / "impl").mkdir()

        code = mai.main(_report_args(tmp_path, findings.with_name("review-report.md")))

        assert code == 0
        assert (tmp_path / "findings-sentinel").read_text(encoding="utf-8") == f"{findings.as_posix()}\n"
        assert [item["finding_id"] for item in mai.read_jsonl(tmp_path / "impl" / "action-items.jsonl")] == [
            mfi.mint([_finding()])[0]["id"]
        ]

    def test_mints_the_parsed_copy_when_the_report_has_no_findings_file(self, tmp_path: Path) -> None:
        """An older report without ``findings.jsonl`` falls back to the copy resolve parsed, minting its ids in place.

        The parsed copy carries no ids when the orchestrator writes it, so the merge would have nothing stable to key
        on; the item must also lose any verifier verdict, because no verifier backed a file resolve wrote itself.
        """
        report = tmp_path / "review" / "review-report.md"
        report.parent.mkdir()
        (tmp_path / "impl").mkdir()
        parsed = _write_jsonl(tmp_path / "impl" / "report-findings.jsonl", [_finding()])

        code = mai.main(_report_args(tmp_path, report))

        assert code == 0
        assert (tmp_path / "findings-sentinel").read_text(encoding="utf-8") == f"{parsed.as_posix()}\n"
        assert mai.read_jsonl(parsed)[0]["id"] == mfi.mint([_finding()])[0]["id"]
        assert "verify_verdict" not in mai.read_jsonl(tmp_path / "impl" / "action-items.jsonl")[0]

    def test_blocks_with_the_pinned_line_when_no_findings_exist(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No findings beside the report and no parsed copy stops with the line telling the orchestrator what to write.

        The pinned wording names the exact path to write, so the orchestrator can recover without guessing.
        """
        (tmp_path / "impl").mkdir()

        code = mai.main(_report_args(tmp_path, tmp_path / "review" / "review-report.md"))

        assert code == 1
        expected = (tmp_path / "impl" / "report-findings.jsonl").as_posix()
        assert capsys.readouterr().out.strip() == (
            f"! BLOCKED — no findings.jsonl beside the report; write {expected} from the parsed report first"
        )
        assert not (tmp_path / "findings-sentinel").exists()

    def test_stops_when_minting_the_parsed_copy_fails(self, tmp_path: Path) -> None:
        """A parsed record the minter refuses stops the merge before any item is written or path persisted.

        A half-published merge would leave items without ids and a sentinel pointing at an unusable findings file.
        """
        (tmp_path / "impl").mkdir()
        _write_jsonl(tmp_path / "impl" / "report-findings.jsonl", [{"title": "no section or severity"}])

        code = mai.main(_report_args(tmp_path, tmp_path / "review" / "review-report.md"))

        assert code == 1
        assert not (tmp_path / "impl" / "action-items.jsonl").exists()
        assert not (tmp_path / "findings-sentinel").exists()

    @pytest.mark.parametrize(
        ("accepted", "same"),
        [
            pytest.param("report", True, id="same-path"),
            pytest.param("relative", True, id="relative-spelling-of-the-same-file"),
            pytest.param("other", False, id="another-existing-file"),
            pytest.param("absent", False, id="accepted-file-gone"),
            pytest.param("", False, id="nothing-accepted"),
        ],
    )
    def test_intake_accepted_matches_the_report_by_file_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accepted: str, same: bool
    ) -> None:
        """Gap triage's accepted report admits degraded merging only when it is the very file being consumed.

        The sentinel may hold a relative spelling while the report path arrives absolute, so this is ``test -ef``, not
        string equality; an empty, stale or different file never admits a degraded publication.
        """
        report = tmp_path / "review-report.md"
        _write_files(tmp_path, {"review-report.md": "x", "other.md": "y"})
        monkeypatch.chdir(tmp_path)
        spelled = {
            "report": str(report),
            "relative": "review-report.md",
            "other": str(tmp_path / "other.md"),
            "absent": str(tmp_path / "gone.md"),
            "": "",
        }[accepted]

        assert mai._same_file(spelled, report) is same

    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param(["--findings", "f.jsonl"], id="with-findings"),
            pytest.param([], id="without-impl-dir"),
        ],
    )
    def test_flag_combinations_are_refused(self, tmp_path: Path, extra: list[str]) -> None:
        """``--report-file`` replaces ``--findings`` and needs ``--impl-dir``; anything else is an argument error.

        Passing both sources would leave it ambiguous which findings win; argparse exits 2 instead of guessing.
        """
        argv = ["--items", str(tmp_path / "items.jsonl"), "--report-file", str(tmp_path / "r.md"), *extra]

        with pytest.raises(SystemExit) as raised:
            mai.main(argv)

        assert raised.value.code == 2


class TestFoldedRows:
    """Rows carrying ``merged_into`` fold into their survivor's item instead of becoming items of their own."""

    @pytest.mark.parametrize(
        ("items", "appended"),
        [
            pytest.param([], 1, id="survivor-becomes-report-item"),
            pytest.param([_gh(1)], 0, id="survivor-annotates-github-item"),
        ],
    )
    def test_merged_row_folds_into_the_survivor_item(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], items: list[dict], appended: int
    ) -> None:
        """A ``merged_into`` row adds no action item of its own; the survivor's item records its id and its symptom.

        The review prints one finding for a root-cause merge and keeps each merged candidate's confirmed row only so no
        candidate is discarded. One item per row would dispatch the same fix twice and disagree with the report the user
        reads, whether the survivor becomes a ``[report]`` item or enriches the GitHub item on its line.
        """
        path = _write_jsonl(tmp_path / "action-items.jsonl", items)
        findings = _findings_file(tmp_path, [_survivor(), _merged()])
        survivor_id, merged_id = (row["id"] for row in mfi.read_jsonl(findings))

        assert mai.main(["--items", str(path), "--findings", str(findings)]) == 0

        (row,) = mfi.read_jsonl(path)
        summary = json.loads(capsys.readouterr().out)
        assert (row["finding_id"], row["merged_finding_ids"]) == (survivor_id, [merged_id])
        assert f"[merged finding {merged_id}] {_merged()['full_text']}" in row["full_comment_text"]
        assert (summary["folded"], summary["appended"], summary["fold_orphans"]) == (1, appended, [])

    @pytest.mark.parametrize(
        ("survivor", "merged", "item_type", "severity"),
        [
            pytest.param(
                {"severity": "medium", "section": "### Test Coverage Gaps"},
                {"severity": "high"},
                "[report][req]",
                4,
                id="merged-high-outranks-medium-survivor",
            ),
            pytest.param(
                {"severity": "medium", "section": "### Test Coverage Gaps"},
                {"severity": "medium", "section": "### Architecture & Quality"},
                "[report][req]",
                3,
                id="merged-medium-in-code-section-keeps-req",
            ),
            pytest.param(
                {"severity": "critical"}, {"severity": "low"}, "[report][req]", 5, id="survivor-already-highest"
            ),
            pytest.param(
                {"severity": "low", "section": "### Test Coverage Gaps"},
                {"severity": "cosmetic", "section": "### Cosmetic / Style"},
                "[report][suggest]",
                2,
                id="minor-group-stays-suggest",
            ),
        ],
    )
    def test_folded_item_takes_the_most_severe_routing_of_its_group(
        self, tmp_path: Path, survivor: dict, merged: dict, item_type: str, severity: int
    ) -> None:
        """The survivor's item routes at the highest severity across the survivor and every row merged into it.

        The consolidator may keep a medium as the printed survivor while a merged symptom is high; routing by the
        survivor alone would offer a confirmed high as an optional ``[suggest]``, and the fix could be skipped.
        """
        path = tmp_path / "action-items.jsonl"
        findings = _findings_file(tmp_path, [_survivor(**survivor), _merged(**merged)])

        assert mai.main(["--items", str(path), "--findings", str(findings)]) == 0

        (row,) = mfi.read_jsonl(path)
        assert (row["type"], row["severity"]) == (item_type, severity)

    def test_folded_github_item_takes_the_merged_severity_but_keeps_its_type(self, tmp_path: Path) -> None:
        """A GitHub item that absorbs a survivor rises to the merged rows' severity; its GitHub type never changes.

        The merge never retypes a GitHub item, but its severity orders the resolve table and dispatch, so a merged
        critical symptom must lift it.
        """
        path = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, severity=3)])
        findings = _findings_file(
            tmp_path, [_survivor(severity="medium", section="### Test Coverage Gaps"), _merged(severity="critical")]
        )

        assert mai.main(["--items", str(path), "--findings", str(findings)]) == 0

        (row,) = mfi.read_jsonl(path)
        assert (row["id"], row["type"], row["severity"]) == (1, "[gh][req]", 5)

    @pytest.mark.parametrize(
        "rows",
        [
            pytest.param([_survivor(), _merged(merged_into="cand-absent")], id="survivor-missing"),
            pytest.param([_merged(merged_into="cand-merged")], id="row-names-itself"),
            pytest.param(
                [_survivor(), _survivor(title="Twin", line=50), _merged()], id="two-rows-hold-the-survivor-id"
            ),
            pytest.param([_survivor(merged_into="cand-merged"), _merged()], id="target-is-itself-merged"),
        ],
    )
    def test_unresolvable_merged_into_keeps_the_row_as_its_own_item(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], rows: list[dict]
    ) -> None:
        """A ``merged_into`` naming no single surviving row keeps the confirmed finding as its own item and warns.

        Dropping the row would lose a confirmed finding; folding it into a guessed or ambiguous survivor would attach
        its evidence to the wrong item. The warning names the row so the orchestrator can tell the user.
        """
        path = tmp_path / "action-items.jsonl"
        findings = _findings_file(tmp_path, rows)
        merged_id = mfi.read_jsonl(findings)[-1]["id"]

        assert mai.main(["--items", str(path), "--findings", str(findings)]) == 0

        out_rows = mfi.read_jsonl(path)
        captured = capsys.readouterr()
        assert [row["finding_id"] for row in out_rows] == [row["id"] for row in mfi.read_jsonl(findings)]
        assert not [row for row in out_rows if "merged_finding_ids" in row]
        assert f"⚠ finding {merged_id}: merged_into " in captured.err
        assert merged_id in {orphan["finding_id"] for orphan in json.loads(captured.out)["fold_orphans"]}

    def test_merged_row_whose_survivor_an_earlier_merge_took_stays_its_own_item(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A merged row new to a re-run whose survivor an earlier merge already placed is appended, with a warning.

        Folding it into the existing item would rewrite a row an earlier run already handed to the resolve table, and
        dropping it would lose a confirmed finding, so it becomes its own item.
        """
        path = tmp_path / "action-items.jsonl"
        assert mai.main(["--items", str(path), "--findings", str(_findings_file(tmp_path, [_survivor()]))]) == 0
        findings = _findings_file(tmp_path, [_survivor(), _merged()])
        survivor_id, merged_id = (row["id"] for row in mfi.read_jsonl(findings))
        capsys.readouterr()

        assert mai.main(["--items", str(path), "--findings", str(findings)]) == 0

        assert [row["finding_id"] for row in mfi.read_jsonl(path)] == [survivor_id, merged_id]
        assert f"⚠ finding {merged_id}: merged_into " in capsys.readouterr().err

    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param({}, id="no-key"),
            pytest.param({"merged_into": None}, id="null"),
            pytest.param({"merged_into": ""}, id="empty"),
        ],
    )
    def test_rows_without_merged_into_each_become_an_item(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: dict
    ) -> None:
        """Rows whose ``merged_into`` is absent, null or empty keep one item each, exactly as before folding existed.

        Older reviews never wrote the field and newer ones may write it empty; neither may fold anything.
        """
        path = tmp_path / "action-items.jsonl"
        rows = [
            _finding(candidate_id="cand-a", **extra),
            _finding(candidate_id="cand-b", title="Other", line=3, **extra),
        ]
        findings = _findings_file(tmp_path, rows)

        assert mai.main(["--items", str(path), "--findings", str(findings)]) == 0

        out_rows = mfi.read_jsonl(path)
        captured = capsys.readouterr()
        assert [row["id"] for row in out_rows] == [1, 2]
        assert not [row for row in out_rows if "merged_finding_ids" in row]
        assert (json.loads(captured.out)["folded"], captured.err) == (0, "")

    def test_candidates_never_offer_a_folded_row(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An exact folded location needs no semantic link; it reaches resolve through its survivor's exact match.

        The GitHub item sits on the merged row's own line, so offering a link would ask the orchestrator to judge a pair
        the exact pass already settles.
        """
        path = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, file="src/pool.py", line=40)])
        findings = _findings_file(tmp_path, [_survivor(), _merged()])

        assert mai.main(["--items", str(path), "--findings", str(findings), "--candidates"]) == 0

        assert json.loads(capsys.readouterr().out) == []

    def test_github_item_at_folded_location_dedupes_to_survivor(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A folded symptom's exact location claims one GitHub item for the survivor and retains both findings.

        The GitHub item is on the merged row's line; the survivor takes it and records the merged id, so the comment is
        neither duplicated nor left unannotated.
        """
        path = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, file="src/pool.py", line="40")])
        findings = _findings_file(tmp_path, [_survivor(), _merged()])
        survivor_id, merged_id = (row["id"] for row in mfi.read_jsonl(findings))

        assert mai.main(["--items", str(path), "--findings", str(findings)]) == 0

        (item,) = mfi.read_jsonl(path)
        summary = json.loads(capsys.readouterr().out)
        assert (item["id"], item["finding_id"], item["merged_finding_ids"]) == (1, survivor_id, [merged_id])
        assert item["candidate_id"] == "cand-survivor"
        assert f"[merged finding {merged_id}]" in item["full_comment_text"]
        assert "thin" not in item
        assert (summary["deduped_exact"], summary["appended"], summary["folded"]) == (1, 0, 1)

    def test_candidates_offer_survivor_on_folded_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A nearby GitHub comment on a folded symptom's file can be linked to the survivor.

        The comment is on the merged row's file but another line, so the pair is a semantic candidate for the survivor.
        """
        path = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, file="src/pool.py", line=99)])
        findings = _findings_file(tmp_path, [_survivor(), _merged()])
        survivor_id = mfi.read_jsonl(findings)[0]["id"]
        before = path.read_bytes()

        assert mai.main(["--items", str(path), "--findings", str(findings), "--candidates"]) == 0

        (pair,) = json.loads(capsys.readouterr().out)
        assert (pair["finding_id"], pair["item_id"], pair["finding_line"]) == (survivor_id, 1, 40)
        assert path.read_bytes() == before

    def test_candidates_keep_new_folded_row_when_survivor_already_placed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Candidate generation uses the merge's fresh-row pool, where an already placed survivor cannot absorb a row.

        The survivor already owns item 1 from an earlier run, so the new merged row is offered as its own candidate and
        a link to it deduplicates semantically instead of folding.
        """
        findings = _findings_file(tmp_path, [_survivor(), _merged()])
        survivor_id, merged_id = (row["id"] for row in mfi.read_jsonl(findings))
        path = _write_jsonl(
            tmp_path / "action-items.jsonl", [_gh(1, finding_id=survivor_id), _gh(2, file="src/pool.py", line=99)]
        )

        assert mai.main(["--items", str(path), "--findings", str(findings), "--candidates"]) == 0

        (pair,) = json.loads(capsys.readouterr().out)
        assert (pair["finding_id"], pair["item_id"]) == (merged_id, 2)
        links = _write_jsonl(tmp_path / "links.jsonl", [{"finding_id": merged_id, "item_id": 2}])
        assert mai.main(["--items", str(path), "--findings", str(findings), "--links", str(links)]) == 0
        assert [item["finding_id"] for item in mfi.read_jsonl(path)] == [survivor_id, merged_id]
        assert json.loads(capsys.readouterr().out)["deduped_semantic"] == 1

    def test_link_naming_a_folded_row_is_ignored(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A link that names a merged row is counted as ignored and never makes that row a second item's finding.

        Accepting it would annotate the GitHub item with the merged row while the survivor's item also carries it, so
        one fix would again be dispatched twice.
        """
        path = _write_jsonl(
            tmp_path / "action-items.jsonl", [_gh(1, file="src/pool.py", line=99, full_comment_text="x" * 99)]
        )
        findings = _findings_file(tmp_path, [_survivor(), _merged()])
        merged_id = mfi.read_jsonl(findings)[1]["id"]
        links = _write_jsonl(tmp_path / "links.jsonl", [{"finding_id": merged_id, "item_id": 1}])

        assert mai.main(["--items", str(path), "--findings", str(findings), "--links", str(links)]) == 0

        github_row, survivor_row = mfi.read_jsonl(path)
        summary = json.loads(capsys.readouterr().out)
        assert "finding_id" not in github_row
        assert survivor_row["merged_finding_ids"] == [merged_id]
        assert (summary["links_ignored"], summary["folded"]) == (1, 1)
