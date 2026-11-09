"""Tests for the evolution report renderer: hostile-input hardening, offline-safe output, bounds and captions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from evolution_render import (
    MAX_MATRIX_FEATURES,
    clean,
    embed_json,
    main,
    md_cell,
    render_markdown,
    render_report,
    render_svg_burnup,
    render_svg_heatmap,
    render_svg_sankey,
    safe_url,
)

LS = chr(0x2028)
PS = chr(0x2029)
HOSTILE_TITLES = (
    "</script><script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "line" + LS + "sep" + PS + "end",
    "a < b & c > d",
    '=HYPERLINK("http://evil.example/","click")',
)
REPO = {"owner": "acme", "repo": "widgets"}
OUTPUT_NAMES = ("report.html", "report.md", "heatmap.svg", "burnup.svg")


def _cell(column_id: str, *, binary: bool, raw: float = 0.9, soft: float = 0.5) -> dict[str, Any]:
    """Build one scored cell."""
    return {
        "column_id": column_id,
        "release": "1.0",
        "raw_score": raw,
        "soft_share": soft,
        "binary": binary,
        "method": "blame",
        "confidence": 0.8,
        "cross_line": False,
    }


def _aggregate(releases: list[str], top: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a small aggregate over ``releases`` with flows, heatmap, burn-up, residue and a same-cycle entry."""
    flow = [
        {"from": PRE, "to": releases[-1], "weight": 2.0, "bucket": "legacy"},
        {"from": releases[0], "to": releases[-1], "weight": 3.5, "bucket": "2-5"},
        {"from": releases[-2], "to": releases[-1], "weight": 1.5, "bucket": "previous"},
        {"from": "same-cycle", "to": releases[-1], "weight": 1.0, "bucket": "same-cycle"},
    ]
    heat = {releases[-1]: {PRE: 2.0, releases[0]: 3.5, releases[-2]: 1.5, "same-cycle": 1.0}}
    burn = [{"release": r, "features": 3 * (i + 1), "fixes": 2 * (i + 1)} for i, r in enumerate(releases)]
    return {
        "releases": releases,
        "flow": flow,
        "heatmap": heat,
        "burnup": burn,
        "top_features": top
        if top is not None
        else [{"column_id": "c1", "title": HOSTILE_TITLES[0], "release": releases[0], "fix_count": 4}],
        "residue": {releases[-1]: {"unlinked": 0.5, "unknown": 0.25}},
        "same_cycle": [{"fix_id": "f1", "column_id": "c1", "release": releases[-1]}],
        "totals": {"fixes": 8, "soft_sum": 8.0, "binary_links": 5},
    }


PRE = "pre-window"


def _hostile_report(**overrides: Any) -> dict[str, Any]:
    """Build a report whose every free-text field carries hostile content."""
    releases = ["1.0", "1.1", "1.2", "2.0"]
    columns = [
        {"column_id": f"c{i}", "kind": "feature", "release": "1.0", "title": title, "url": "javascript:alert(1)"}
        for i, title in enumerate(HOSTILE_TITLES, 1)
    ]
    rows = [
        {
            "fix_id": f"f{i}",
            "fix_release": "2.0",
            "title": title,
            "cells": [_cell("c1", binary=True), _cell("c2", binary=True), _cell("zzz", binary=True)],
        }
        for i, title in enumerate(HOSTILE_TITLES, 1)
    ]
    report = {
        "repo": REPO,
        "manifest": {"scope": HOSTILE_TITLES[1], "threshold": 0.5},
        "rows": rows,
        "columns": columns,
        "aggregate": _aggregate(releases),
        "bugs": {"series": [{"release": "1.1", "open": 4}, {"release": "2.0", "open": 6}]},
        "warnings": list(HOSTILE_TITLES),
    }
    report.update(overrides)
    return report


def _synthetic_large() -> dict[str, Any]:
    """Build a 500-feature by 3000-fix report over 40 releases."""
    releases = [f"1.{i}" for i in range(40)]
    columns = [
        {"column_id": f"c{i}", "kind": "feature", "release": releases[i % 40], "title": f"feature {i}"}
        for i in range(500)
    ]
    top = [
        {"column_id": f"c{i}", "title": f"feature {i}", "release": releases[i % 40], "fix_count": 500 - i}
        for i in range(500)
    ]
    rows = [
        {
            "fix_id": f"f{i}",
            "fix_release": releases[i % 40],
            "title": f"fix number {i}",
            "cells": [_cell(f"c{i % 500}", binary=True), _cell(f"c{(i * 7) % 500}", binary=True)],
        }
        for i in range(3000)
    ]
    agg = _aggregate(releases, top)
    agg["flow"] = [
        {"from": releases[a], "to": releases[b], "weight": 1.0 + a, "bucket": "previous"}
        for b in range(40)
        for a in range(b)
    ]
    agg["heatmap"] = {releases[b]: {releases[a]: 1.0 + a for a in range(b)} for b in range(40)}
    return {
        "repo": REPO,
        "manifest": {},
        "rows": rows,
        "columns": columns,
        "aggregate": agg,
        "bugs": None,
        "warnings": [],
    }


@pytest.fixture(scope="module")
def hostile_outputs() -> dict[str, str]:
    """Render the hostile report once for the module."""
    return render_report(_hostile_report())


class TestHostileText:
    """Untrusted titles, warnings and links never reach any output raw."""

    @pytest.mark.parametrize(
        "needle",
        [
            pytest.param("<img src=x", id="img-tag"),
            pytest.param("<script>alert", id="inline-script"),
            pytest.param(LS, id="line-separator"),
            pytest.param(PS, id="paragraph-separator"),
            pytest.param('href="javascript', id="javascript-href"),
            pytest.param("a < b", id="raw-less-than"),
        ],
    )
    @pytest.mark.parametrize("name", [pytest.param(n, id=n) for n in OUTPUT_NAMES])
    def test_needle_absent(self, hostile_outputs: dict[str, str], name: str, needle: str) -> None:
        """Hostile fragments never appear raw in any rendered file.

        The report carries script, image-handler, separator, javascript-link and bare angle-bracket text in titles,
        warnings and manifest values; none of those literal forms may survive into HTML, Markdown or SVG.
        """
        assert needle not in hostile_outputs[name]

    def test_single_script_block(self, hostile_outputs: dict[str, str]) -> None:
        """The page holds exactly one script element, the JSON data block, with a single closing tag.

        A title containing a closing script tag would add a second closer if the embed were not escaped.
        """
        html_text = hostile_outputs["report.html"]
        assert (html_text.count("<script"), html_text.count("</script>")) == (1, 1)

    def test_embedded_json_roundtrips(self, hostile_outputs: dict[str, str]) -> None:
        """The embedded data block parses as JSON and keeps the hostile text as inert data.

        Escaping must be lossless: the title with the closing script tag decodes back to its original value.
        """
        match = re.search(
            r'<script type="application/json" id="evolution-data">(.*?)</script>', hostile_outputs["report.html"], re.S
        )
        data = json.loads(match.group(1))
        assert HOSTILE_TITLES[0] in data["warnings"]

    def test_markdown_formula_cell_escaped(self, hostile_outputs: dict[str, str]) -> None:
        """No Markdown table cell starts with a spreadsheet-formula character.

        The formula title is a top feature, so it lands in a table cell and must be prefixed with a backslash.
        """
        report = _hostile_report()
        report["aggregate"]["top_features"] = [
            {"column_id": "c5", "title": HOSTILE_TITLES[4], "release": "1.0", "fix_count": 2}
        ]
        text = render_markdown(report)
        cells = [c.strip() for line in text.splitlines() if line.startswith("|") for c in line.split(" | ")]
        assert [c for c in cells if c.lstrip("| ").startswith("=")] == []

    def test_svg_text_xml_escaped(self, hostile_outputs: dict[str, str]) -> None:
        """Hostile column titles inside SVG tooltips are XML-escaped so the document stays well-formed.

        Both standalone files are parsed as XML; a stray less-than or ampersand would raise.
        """
        import xml.etree.ElementTree as ET

        parsed = [ET.fromstring(hostile_outputs[n]) for n in ("heatmap.svg", "burnup.svg")]  # noqa: S314 - parses this module's own output
        assert len(parsed) == 2


class TestLinks:
    """Links appear only under the report repository's own GitHub URL."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            pytest.param("https://github.com/acme/widgets/pull/3", True, id="own-repo"),
            pytest.param("https://github.com/acme/other/pull/3", False, id="other-repo"),
            pytest.param("https://github.com/acme/widgets-evil/pull/3", False, id="prefix-lookalike"),
            pytest.param("javascript:alert(1)", False, id="javascript"),
            pytest.param("https://github.com/acme/widgets/pull/3 onclick=x", False, id="whitespace"),
            pytest.param("https://github.com/acme/widgets/../../evil", False, id="dotdot"),
            pytest.param(None, False, id="none"),
        ],
    )
    def test_safe_url(self, url: Any, expected: bool) -> None:
        """Only URLs under the repo prefix without unsafe characters are accepted.

        Look-alike repositories, other schemes, embedded whitespace and parent-directory hops are all refused.
        """
        assert (safe_url(url, REPO) is not None) is expected

    def test_own_repo_link_rendered(self) -> None:
        """A feature URL inside the repo becomes an anchor; a hostile one stays plain text.

        The ranking shows one linked and one unlinked feature from the same report.
        """
        report = _hostile_report()
        report["columns"][0]["url"] = "https://github.com/acme/widgets/pull/9"
        report["aggregate"]["top_features"] = [
            {"column_id": "c1", "title": "linked", "release": "1.0", "fix_count": 3},
            {"column_id": "c2", "title": "plain", "release": "1.0", "fix_count": 2},
        ]
        page = render_report(report)["report.html"]
        assert ('href="https://github.com/acme/widgets/pull/9"' in page, page.count("href=")) == (True, 1)

    def test_hostile_repo_identity_disables_links(self) -> None:
        """A repo name with slashes or markup yields no prefix, so no link is ever emitted.

        The identity fails the safe-component check even when the URL looks plausible.
        """
        assert safe_url("https://github.com/a/<b>/x", {"owner": "a", "repo": "<b>"}) is None


class TestOfflineSafe:
    """The page needs no network and no script execution."""

    @pytest.mark.parametrize(
        "pattern",
        [
            pytest.param(r"<script[^>]+src=", id="script-src"),
            pytest.param(r"<link\b", id="link-element"),
            pytest.param(r"<[a-zA-Z][^>]*\son\w+=", id="inline-handler"),
            pytest.param(r"(?:src|href)=\"https?:", id="remote-asset"),
            pytest.param(r"url\(\s*['\"]?https?:", id="css-remote-url"),
            pytest.param(r"@import", id="css-import"),
        ],
    )
    def test_no_external_reference(self, pattern: str) -> None:
        """The HTML contains no external script, stylesheet, handler attribute or remote asset.

        A clean report is rendered and each forbidden pattern is searched for in the page source.
        """
        page = render_report(_hostile_report())["report.html"]
        markup = re.sub(r"<script.*?</script>", "", page, flags=re.S)
        assert re.search(pattern, markup) is None

    def test_csp_meta_present(self) -> None:
        """The page carries a restrictive Content-Security-Policy as defence in depth.

        The policy denies everything by default and allows only inline style.
        """
        page = render_report(_hostile_report())["report.html"]
        assert "default-src &#x27;none&#x27;" in page

    def test_dark_mode_tokens_present(self) -> None:
        """Light and dark themes are CSS custom properties keyed on prefers-color-scheme.

        The page must define dark values both for the media query and for the explicit data-theme toggle.
        """
        page = render_report(_hostile_report())["report.html"]
        assert ("prefers-color-scheme: dark" in page, ':root[data-theme="dark"]' in page) == (True, True)


class TestViewsAndCaptions:
    """Captions name their view and explain why totals differ."""

    @pytest.mark.parametrize(
        "needle",
        [
            pytest.param("Soft view", id="soft-caption"),
            pytest.param("Binary view", id="binary-caption"),
            pytest.param("differ by design", id="totals-differ"),
        ],
    )
    def test_caption_text(self, needle: str) -> None:
        """The page names the soft and binary views and states that totals differ by design.

        Each phrase is a caption fragment that guards against a chart shipping without its view label.
        """
        assert needle in render_report(_hostile_report())["report.html"]

    def test_provisional_banner_when_flagged(self) -> None:
        """The provisional-weights banner shows only when the manifest flags it.

        The same report is rendered with and without the flag.
        """
        flagged = _hostile_report(manifest={"weights_provisional": True})
        pair = (
            "Provisional weights" in render_report(flagged)["report.html"],
            "Provisional weights" in render_report(_hostile_report())["report.html"],
        )
        assert pair == (True, False)

    def test_bugs_line_labelled_approximate(self) -> None:
        """Open bugs render as a dashed line labelled approximate, in the chart and the caption.

        The standalone burn-up SVG names the series and uses a dash pattern.
        """
        svg = render_svg_burnup(_hostile_report()["aggregate"], _hostile_report()["bugs"], standalone=True)
        assert ("open bugs (approximate)" in svg, "dash" in svg) == (True, True)

    def test_no_bugs_no_bug_series(self) -> None:
        """Without a bug series the chart has only features and fixes.

        No label mentions bugs when the input is empty.
        """
        assert "bugs" not in render_svg_burnup(_hostile_report()["aggregate"], None, standalone=True)

    def test_same_cycle_not_drawn_as_band(self) -> None:
        """Same-cycle weight never becomes a Sankey band.

        Only the three cross-release flows are drawn, so there are exactly three band paths.
        """
        svg = render_svg_sankey(_hostile_report()["aggregate"])
        assert svg.count('class="band ') == 3

    def test_markdown_references_charts(self) -> None:
        """The Markdown report embeds the static heatmap and burn-up files by relative name.

        The skill writes both SVGs beside the Markdown, so the references must be exact.
        """
        text = render_markdown(_hostile_report())
        assert ("![heatmap](heatmap.svg)" in text, "![burn-up](burnup.svg)" in text) == (True, True)


@pytest.fixture(scope="module")
def large_outputs() -> dict[str, str]:
    """Render the 500 by 3000 synthetic report once for the module."""
    return render_report(_synthetic_large())


class TestBounds:
    """Output stays bounded for very large inputs."""

    def test_html_size_bound(self, large_outputs: dict[str, str]) -> None:
        """The HTML for 500 features and 3000 fixes stays under 1.5 MB.

        Rows are capped in the matrix and per-fix data is not embedded.
        """
        assert len(large_outputs["report.html"].encode("utf-8")) < 1_500_000

    def test_matrix_capped_with_other(self, large_outputs: dict[str, str]) -> None:
        """The matrix header names exactly 30 feature columns plus one "other" column.

        Header labels are the rotated text elements inside the first matrix card.
        """
        header = large_outputs["report.html"].split('<section id="matrix">', 1)[1].split("</svg>", 1)[0]
        labels = re.findall(r"rotate\(-90", header)
        assert (len(labels), ">other<" in header) == (MAX_MATRIX_FEATURES + 1, True)

    def test_matrix_rows_capped(self, large_outputs: dict[str, str]) -> None:
        """No more than the row cap of fix rows is drawn, and the note points at matrix.csv.

        3000 eligible fixes must collapse to the documented maximum.
        """
        page = large_outputs["report.html"]
        shown = re.search(r"Showing (\d+) of (\d+) fixes", page)
        assert (int(shown.group(1)) <= 240, int(shown.group(2)), "matrix.csv" in page) == (True, 3000, True)

    def test_flows_capped(self) -> None:
        """The Sankey draws at most 120 bands of the 780 available flows.

        Heavier flows win; the rest are counted in the description.
        """
        svg = render_svg_sankey(_synthetic_large()["aggregate"])
        assert svg.count('class="band ') == 120

    def test_svg_standalone_sizes(self, large_outputs: dict[str, str]) -> None:
        """Static SVG files stay small even for 40 releases with 780 heatmap cells.

        Standalone files are what the Markdown report links to, so they are bounded at 600 KB.
        """
        assert max(len(large_outputs[n]) for n in ("heatmap.svg", "burnup.svg")) < 600_000


class TestRendering:
    """Structure of the rendered artefacts for ordinary input."""

    def test_returns_four_files(self) -> None:
        """render_report returns exactly the four named artefacts.

        The skill writes them by key, so the key set is a contract.
        """
        assert sorted(render_report(_hostile_report())) == sorted(OUTPUT_NAMES)

    def test_empty_report_renders(self) -> None:
        """An empty report renders placeholders instead of raising.

        A repository with no releases still produces valid files.
        """
        outputs = render_report(
            {"repo": {}, "manifest": {}, "rows": [], "columns": [], "aggregate": {}, "bugs": None, "warnings": []}
        )
        assert sorted(outputs) == sorted(OUTPUT_NAMES)

    def test_heatmap_triangular(self) -> None:
        """Heatmap cells only appear on or above the diagonal (introduced no later than fixed).

        The fixture's cells all sit in the last column; the legend adds six swatch rects, so cell rects are the rest.
        """
        svg = render_svg_heatmap(_aggregate(["1.0", "1.1", "1.2", "2.0"]))
        assert len(re.findall(r'<rect class="h\d" x="\d+" y="\d+" width="\d+" height="\d+"><title>', svg)) == 4

    def test_standalone_svg_has_theme(self) -> None:
        """Standalone SVG files carry their own dark-mode style so they look right in an img tag.

        The style block includes the prefers-color-scheme media query.
        """
        assert "prefers-color-scheme: dark" in render_svg_heatmap(_aggregate(["1.0", "1.1", "2.0"]), standalone=True)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("a|b", "a\\|b", id="pipe"),
            pytest.param("=SUM(A1)", "\\=SUM\\(A1\\)", id="formula"),
            pytest.param("[x](javascript:1)", "\\[x\\]\\(javascript:1\\)", id="link"),
            pytest.param("<b>", "&lt;b&gt;", id="html"),
            pytest.param("line1\nline2", "line1 line2", id="newline"),
        ],
    )
    def test_md_cell(self, raw: str, expected: str) -> None:
        """Markdown cell escaping neutralises pipes, markup, raw HTML, formulas and newlines.

        Each case is one injection route into a rendered table.
        """
        assert md_cell(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("a" + LS + "b", "a b", id="line-separator"),
            pytest.param("a\x00b", "a b", id="nul"),
            pytest.param("  a \t\n b ", "a b", id="whitespace-runs"),
            pytest.param("x" * 300, "x" * 199 + "…", id="truncate"),
        ],
    )
    def test_clean(self, raw: str, expected: str) -> None:
        """Clean() removes control characters and separators, collapses whitespace and truncates.

        These are the normalisations every output path relies on.
        """
        assert clean(raw) == expected

    def test_embed_json_escapes(self) -> None:
        """embed_json escapes the characters that can end or confuse a script block.

        Less-than, greater-than, ampersand and both line separators must not appear raw.
        """
        out = embed_json({"x": "</script>&" + LS + PS + ">"})
        assert not any(ch in out for ch in "<>&" + LS + PS)


class TestCli:
    """Command-line interface."""

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--help prints usage and exits 0.

        The bin help contract test requires this for every script.
        """
        with pytest.raises(SystemExit) as caught:
            main(["--help"])
        assert caught.value.code == 0

    def test_render_writes_files(self, tmp_path: Path) -> None:
        """The render subcommand reads JSON/JSONL inputs and writes the four files.

        Rows and columns come from JSONL (as in the store) and the others from JSON.
        """
        report = _hostile_report()
        files = {
            "rows.jsonl": "\n".join(json.dumps(r) for r in report["rows"]) + "\n",
            "columns.jsonl": "\n".join(json.dumps(c) for c in report["columns"]) + "\n",
            "aggregate.json": json.dumps(report["aggregate"]),
            "manifest.json": json.dumps(report["manifest"]),
            "repo.json": json.dumps(report["repo"]),
            "bugs.json": json.dumps(report["bugs"]),
        }
        for name, text in files.items():
            (tmp_path / name).write_text(text, encoding="utf-8")
        out = tmp_path / "out"
        code = main(
            [
                "render",
                "--rows",
                str(tmp_path / "rows.jsonl"),
                "--columns",
                str(tmp_path / "columns.jsonl"),
                "--aggregate",
                str(tmp_path / "aggregate.json"),
                "--manifest",
                str(tmp_path / "manifest.json"),
                "--repo-json",
                str(tmp_path / "repo.json"),
                "--bugs",
                str(tmp_path / "bugs.json"),
                "--out-dir",
                str(out),
            ]
        )
        assert (code, sorted(p.name for p in out.iterdir())) == (0, sorted(OUTPUT_NAMES))

    def test_missing_input_exits_two(self, tmp_path: Path) -> None:
        """A missing input file returns exit code 2 instead of a traceback.

        The orchestrator treats 2 as an input failure.
        """
        missing = str(tmp_path / "nope.json")
        code = main(
            [
                "render",
                "--rows",
                missing,
                "--columns",
                missing,
                "--aggregate",
                missing,
                "--manifest",
                missing,
                "--repo-json",
                missing,
                "--out-dir",
                str(tmp_path),
            ]
        )
        assert code == 2
