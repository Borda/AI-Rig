#!/usr/bin/env python3
"""Render a release-evolution report as self-contained HTML, Markdown and static SVG charts.

Purpose:
    Turn the scored evolution store (rows, columns, aggregate, manifest, optional open-bug series) into four
    artefacts that need no network and no JavaScript: ``report.html``, ``report.md``, ``heatmap.svg`` and
    ``burnup.svg``. Every chart is inline SVG generated here with the standard library; hover detail is an SVG
    ``<title>`` and matrix row groups are HTML ``<details>``.

Views:
    The *soft* view spreads one unit of weight per fix over the features that explain it (it feeds the Sankey, the
    release heatmap, age buckets and residue). The *binary* view counts a fix for a feature when its score reaches
    the threshold and its strongest signal is blame, changelog text, an LLM verdict or a manual override; symbol,
    file and inherited overlap never link (it feeds the matrix and the most-fixed ranking). The totals differ by
    design; every caption says which view it shows.

Hardening:
    All text goes through ``html.escape`` or XML escaping, control characters and U+2028/U+2029 are replaced,
    links are emitted only under the report repository's own ``https://github.com/<owner>/<repo>/`` prefix, the
    embedded JSON escapes ``<``, ``>``, ``&`` and the line separators, and the page carries a restrictive
    Content-Security-Policy. Markdown cells escape ``|``, markup characters and leading spreadsheet-formula
    characters.

Usage:
    evolution_render.py render --rows FILE --columns FILE --aggregate FILE --manifest FILE [--bugs FILE]
        --repo-json FILE --out-dir DIR [--warnings FILE]

Outputs:
    ``DIR/report.html``, ``DIR/report.md``, ``DIR/heatmap.svg``, ``DIR/burnup.svg``; stdout is a one-line summary.

Exit codes:
    0 success; 2 unreadable or malformed input file.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Final
from xml.sax.saxutils import escape as _xml_escape

from _evolution_common import RESIDUE_COLUMNS, last_by_key, read_jsonl

#: Matrix shows at most this many features; the rest and all pseudo-columns collapse into "other".
MAX_MATRIX_FEATURES: Final = 30
#: Matrix shows at most this many fix rows overall (the full matrix stays in matrix.csv).
MAX_MATRIX_ROWS: Final = 240
#: Heatmap shows at most this many (most recent) releases per axis.
MAX_HEATMAP_RELEASES: Final = 60
#: Sankey draws at most this many bands (heaviest first).
MAX_SANKEY_FLOWS: Final = 120
#: Row cap for ranking and same-cycle tables.
MAX_RANKING_ROWS: Final = 25
MAX_TABLE_ROWS: Final = 100
#: Rows of the embedded data block kept per list.
MAX_EMBED_ROWS: Final = 200

BUCKETS: Final = ("same-cycle", "previous", "2-5", "legacy")
BUCKET_LABELS: Final = {
    "same-cycle": "same cycle",
    "previous": "previous release",
    "2-5": "2-5 releases back",
    "legacy": "legacy (older)",
}
#: Age buckets drawn as Sankey bands, with their categorical colour slot.
BAND_CLASSES: Final = {"previous": "b1", "2-5": "b2", "legacy": "b3"}
PRE_WINDOW: Final = "pre-window"

_SAFE_PART = re.compile(r"^[A-Za-z0-9._-]+$")
_BAD_URL_CHARS = re.compile(r"[\s\"'<>\\\x00-\x1f\x7f]")
#: Characters in these two code points split lines in JavaScript and some Markdown renderers; built with chr() so the
#: source holds no ambiguous literal.
LINE_SEPARATOR: Final = chr(0x2028)
PARAGRAPH_SEPARATOR: Final = chr(0x2029)
_CONTROL = re.compile("[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f" + LINE_SEPARATOR + PARAGRAPH_SEPARATOR + "]")
_MD_SPECIAL = re.compile(r"([\\`*_\[\]()!#~|:.])")
_FORMULA_LEAD = ("=", "+", "-", "@")

# --------------------------------------------------------------------------------------------------------------------
# Theme: colour tokens from the dataviz reference palette (categorical slots 1-3 validate all-pairs in both modes).
# --------------------------------------------------------------------------------------------------------------------

_LIGHT_VARS: Final = (
    "--page:#f9f9f7;--surface:#fcfcfb;--t1:#0b0b0b;--t2:#52514e;--tm:#898781;--grid:#e1e0d9;--axis:#c3c2b7;"
    "--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;"
    "--h1:#cde2fb;--h2:#9ec5f4;--h3:#6da7ec;--h4:#3987e5;--h5:#256abf;--h6:#184f95;--warn:#fab219;"
)
_DARK_VARS: Final = (
    "--page:#0d0d0d;--surface:#1a1a19;--t1:#f0efec;--t2:#c3c2b7;--tm:#898781;--grid:#2c2c2a;--axis:#383835;"
    "--s1:#3987e5;--s2:#d95926;--s3:#199e70;"
    "--h1:#0d366b;--h2:#184f95;--h3:#256abf;--h4:#3987e5;--h5:#6da7ec;--h6:#9ec5f4;--warn:#fab219;"
)

THEME_CSS: Final = (
    f".viz-root{{color-scheme:light;{_LIGHT_VARS}}}\n"
    f'@media (prefers-color-scheme: dark){{:root:where(:not([data-theme="light"])) .viz-root'
    f"{{color-scheme:dark;{_DARK_VARS}}}}}\n"
    f':root[data-theme="dark"] .viz-root{{color-scheme:dark;{_DARK_VARS}}}\n'
)

CHART_CSS: Final = (
    ".surf{fill:var(--surface)}.svgt{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;font-size:11px}"
    ".t1{fill:var(--t1)}.t2{fill:var(--t2)}.tm{fill:var(--tm)}"
    ".grid{stroke:var(--grid);stroke-width:1;fill:none}.axis{stroke:var(--axis);stroke-width:1;fill:none}"
    ".b1{fill:var(--s1)}.b2{fill:var(--s2)}.b3{fill:var(--s3)}"
    ".band{fill-opacity:.55;stroke:var(--surface);stroke-width:.5}.node{fill:var(--tm)}"
    ".h1{fill:var(--h1)}.h2{fill:var(--h2)}.h3{fill:var(--h3)}.h4{fill:var(--h4)}.h5{fill:var(--h5)}.h6{fill:var(--h6)}"
    ".ln1{stroke:var(--s1)}.ln2{stroke:var(--s2)}.ln3{stroke:var(--s3)}"
    ".line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}.dash{stroke-dasharray:6 4}"
    ".dot{stroke:var(--surface);stroke-width:2}.hit{fill:transparent}.ring{fill:none;stroke:var(--t2);stroke-width:1.5}"
    ".hit:hover{fill:var(--grid);fill-opacity:.5}.mx{fill:var(--s1)}\n"
)

PAGE_CSS: Final = (
    "body{margin:0;background:var(--page);color:var(--t1);font:14px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif}"
    "main{max-width:1040px;margin:0 auto;padding:16px}h1{font-size:22px;margin:8px 0}h2{font-size:17px;margin:28px 0 6px}"
    ".card{background:var(--surface);border:1px solid var(--grid);border-radius:8px;padding:12px;margin:8px 0;"
    "overflow-x:auto}.card svg{max-width:100%;height:auto;display:block}"
    ".cap{color:var(--t2);font-size:13px;margin:4px 0 8px}.banner{border:1px solid var(--warn);border-left-width:6px;"
    "border-radius:6px;padding:8px 12px;background:var(--surface);margin:12px 0}"
    "table{border-collapse:collapse;font-size:13px;width:100%}th,td{text-align:left;padding:4px 8px;"
    "border-bottom:1px solid var(--grid)}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}"
    ".stats{display:flex;flex-wrap:wrap;gap:12px}.stat{background:var(--surface);border:1px solid var(--grid);"
    "border-radius:8px;padding:8px 12px;min-width:120px}.stat b{display:block;font-size:20px}"
    "details{margin:4px 0}summary{cursor:pointer;color:var(--t2)}dl{margin:0;font-size:13px;color:var(--t2)}"
    "dt{float:left;clear:left;width:130px}dd{margin:0 0 0 130px}a{color:var(--s1)}\n"
)

CSP: Final = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"


# --------------------------------------------------------------------------------------------------------------------
# Text and link safety
# --------------------------------------------------------------------------------------------------------------------


def clean(value: Any, limit: int = 200) -> str:
    """Return ``value`` as one-line text without control characters, line separators or runs of whitespace.

    Control characters (including U+2028 and U+2029) become spaces, whitespace collapses, and text longer than ``limit``
    characters is cut with an ellipsis.
    """
    text = "" if value is None else str(value)
    text = " ".join(_CONTROL.sub(" ", text).split())
    return text if len(text) <= limit else text[: max(limit - 1, 1)] + "…"


def esc(value: Any, limit: int = 200) -> str:
    """Return ``value`` cleaned and HTML-escaped for element text or a double-quoted attribute."""
    return html.escape(clean(value, limit), quote=True)


def esc_xml(value: Any, limit: int = 200) -> str:
    """Return ``value`` cleaned and XML-escaped for SVG text, ``<title>`` content or attributes."""
    return _xml_escape(clean(value, limit), {'"': "&quot;", "'": "&#x27;"})


def md_cell(value: Any, limit: int = 120) -> str:
    """Return ``value`` safe inside a Markdown table cell.

    Markup characters are backslash-escaped (so ``[x](javascript:...)`` cannot become a link), raw HTML is entity-
    escaped, ``|`` is escaped, and a leading spreadsheet-formula character gets a backslash.
    """
    text = html.escape(clean(value, limit), quote=False)
    text = _MD_SPECIAL.sub(r"\\\1", text)
    return "\\" + text if text.startswith(_FORMULA_LEAD) else text


def repo_prefix(repo: dict[str, Any]) -> str | None:
    """Return ``https://github.com/<owner>/<repo>/`` for a well-formed repo identity, else ``None``."""
    owner = repo.get("owner")
    name = repo.get("repo") or repo.get("name")
    if isinstance(owner, str) and isinstance(name, str) and _SAFE_PART.fullmatch(owner) and _SAFE_PART.fullmatch(name):
        return f"https://github.com/{owner}/{name}/"
    return None


def safe_url(url: Any, repo: dict[str, Any]) -> str | None:
    """Return ``url`` only when it lies under the repo's own GitHub URL and holds no unsafe characters."""
    prefix = repo_prefix(repo)
    if not prefix or not isinstance(url, str) or not url.startswith(prefix):
        return None
    if _BAD_URL_CHARS.search(url) or "/../" in url or url.endswith("/.."):
        return None
    return url


def link_or_text(text: Any, url: Any, repo: dict[str, Any], limit: int = 80) -> str:
    """Return escaped ``text``, wrapped in an anchor when ``url`` passes :func:`safe_url`."""
    label = esc(text, limit)
    target = safe_url(url, repo)
    if target is None:
        return label
    return f'<a href="{html.escape(target, quote=True)}" rel="noopener noreferrer">{label}</a>'


def embed_json(obj: Any) -> str:
    """Serialise ``obj`` for a ``<script type="application/json">`` block with HTML-significant characters escaped."""
    text = json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    for raw, repl in (
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("&", "\\u0026"),
        (LINE_SEPARATOR, "\\u2028"),
        (PARAGRAPH_SEPARATOR, "\\u2029"),
    ):
        text = text.replace(raw, repl)
    return text


# --------------------------------------------------------------------------------------------------------------------
# Small numeric and ordering helpers
# --------------------------------------------------------------------------------------------------------------------


def _num(value: Any) -> float:
    """Return ``value`` as a finite float, or 0.0 for anything non-numeric."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _f(value: float, digits: int = 1) -> str:
    """Format ``value`` with fixed decimals, no thousands separator."""
    return f"{value:.{digits}f}"


def _release_order(aggregate: dict[str, Any]) -> dict[str, int]:
    """Map release version to its chronological position in the aggregate."""
    return {str(v): i for i, v in enumerate(aggregate.get("releases") or [])}


def _node_key(node: str, order: dict[str, int]) -> tuple[int, str]:
    """Sort key for a Sankey/heatmap node: pre-window first, known releases in order, unknown last."""
    if node == PRE_WINDOW:
        return (-1, node)
    return (order.get(node, 10**6), node)


def _level(weight: float, top: float) -> int:
    """Quantise ``weight`` into sequential steps 1-6 relative to ``top``."""
    if top <= 0 or weight <= 0:
        return 0
    return max(1, min(6, math.ceil(6 * weight / top)))


def _nice_max(value: float) -> tuple[float, float]:
    """Return ``(axis_max, tick_step)`` giving about four round ticks for ``value``."""
    if value <= 0:
        return 1.0, 1.0
    raw = value / 4
    base = 10 ** math.floor(math.log10(raw))
    step = next((m * base for m in (1, 2, 5, 10) if m * base >= raw), 10 * base)
    return step * math.ceil(value / step), step


def _bug_series(bugs: Any) -> dict[str, float]:
    """Normalise the open-bug input to ``{release: open_count}``; accepts a mapping or a list of row dicts."""
    if isinstance(bugs, dict) and isinstance(bugs.get("series"), (list, dict)):
        bugs = bugs["series"]
    if isinstance(bugs, dict):
        return {str(k): _num(v) for k, v in bugs.items() if not isinstance(v, (dict, list))}
    result: dict[str, float] = {}
    for item in bugs if isinstance(bugs, list) else []:
        if isinstance(item, dict) and "release" in item:
            result[str(item["release"])] = _num(item.get("open", item.get("count", item.get("open_bugs"))))
    return result


# --------------------------------------------------------------------------------------------------------------------
# SVG scaffolding
# --------------------------------------------------------------------------------------------------------------------


def _svg_wrap(body: list[str], width: int, height: int, title: str, desc: str, standalone: bool) -> str:
    """Wrap chart elements in an accessible ``<svg>``; a standalone file also carries its own theme styles."""
    head = '<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" ' if standalone else "<svg "
    size = f'width="{width}" height="{height}" ' if standalone else 'width="100%" '
    parts = [
        f'{head}viewBox="0 0 {width} {height}" {size}role="img" class="svgt">',
        f"<title>{esc_xml(title)}</title><desc>{esc_xml(desc, 400)}</desc>",
    ]
    if standalone:
        parts.append(f"<style>{THEME_CSS}{CHART_CSS}</style>")
    parts.append('<g class="viz-root">' if standalone else "<g>")
    parts.append(f'<rect class="surf" x="0" y="0" width="{width}" height="{height}"/>')
    parts.extend(body)
    parts.append("</g></svg>")
    return "".join(parts) + ("\n" if standalone else "")


def _text(
    x: float, y: float, content: Any, cls: str = "t2", anchor: str = "start", extra: str = "", limit: int = 40
) -> str:
    """One SVG ``<text>`` element with escaped content."""
    return f'<text class="{cls}" x="{_f(x)}" y="{_f(y)}" text-anchor="{anchor}"{extra}>{esc_xml(content, limit)}</text>'


def _empty_chart(title: str, message: str, standalone: bool) -> str:
    """Return a placeholder SVG for a chart with no data."""
    return _svg_wrap([_text(16, 32, message, "t2", limit=120)], 480, 60, title, message, standalone)


def _legend(items: list[tuple[str, str, str]], x: float, y: float) -> list[str]:
    """Row of legend entries ``(css_class, label, kind)``; kind is ``rect`` or ``dash`` or ``line``."""
    out: list[str] = []
    for cls, label, kind in items:
        if kind == "rect":
            out.append(f'<rect class="{cls}" x="{_f(x)}" y="{_f(y - 9)}" width="12" height="12" rx="2"/>')
        else:
            dash = " dash" if kind == "dash" else ""
            out.append(
                f'<line class="line {cls}{dash}" x1="{_f(x)}" y1="{_f(y - 3)}" x2="{_f(x + 18)}" y2="{_f(y - 3)}"/>'
            )
        shift = 16 if kind == "rect" else 24
        out.append(_text(x + shift, y, label, "t1", limit=40))
        x += shift + 8 + 6.2 * min(len(clean(label)), 40)
    return out


# --------------------------------------------------------------------------------------------------------------------
# Sankey (soft view)
# --------------------------------------------------------------------------------------------------------------------


def _sankey_flows(aggregate: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Collapse flows to drawable bands without same-cycle, keeping the heaviest; return ``(flows, omitted)``."""
    merged: dict[tuple[str, str, str], float] = defaultdict(float)
    for flow in aggregate.get("flow") or []:
        src, dst, bucket = str(flow.get("from")), str(flow.get("to")), str(flow.get("bucket"))
        weight = _num(flow.get("weight"))
        if weight > 0 and bucket != "same-cycle" and src != "same-cycle":
            merged[(src, dst, bucket)] += weight
    ranked = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
    kept = [{"from": s, "to": d, "bucket": b, "weight": w} for (s, d, b), w in ranked[:MAX_SANKEY_FLOWS]]
    return kept, max(len(ranked) - len(kept), 0)


def _stack(
    nodes: list[str], totals: dict[str, float], scale: float, top: float, gap: float
) -> dict[str, tuple[float, float]]:
    """Stack nodes top to bottom; return ``{node: (y, height)}``."""
    pos: dict[str, tuple[float, float]] = {}
    y = top
    for node in nodes:
        height = max(totals[node] * scale, 1.0)
        pos[node] = (y, height)
        y += height + gap
    return pos


def _band_offsets(
    flows: list[dict[str, Any]], side: str, other: str, order: dict[str, int], scale: float
) -> dict[int, float]:
    """Offset of each flow inside its ``side`` node, ordering bands by the opposite node to limit crossings."""
    offsets: dict[int, float] = {}
    used: dict[str, float] = defaultdict(float)
    indexed = sorted(
        range(len(flows)), key=lambda i: (_node_key(flows[i][other], order), _node_key(flows[i][side], order))
    )
    for i in indexed:
        node = flows[i][side]
        offsets[i] = used[node]
        used[node] += flows[i]["weight"] * scale
    return offsets


def _band_path(x0: float, y0: float, x1: float, y1: float, thick0: float, thick1: float) -> str:
    """SVG path of one Sankey band between two vertical edges."""
    xm = (x0 + x1) / 2
    return (
        f"M{_f(x0)},{_f(y0)}C{_f(xm)},{_f(y0)} {_f(xm)},{_f(y1)} {_f(x1)},{_f(y1)}"
        f"L{_f(x1)},{_f(y1 + thick1)}C{_f(xm)},{_f(y1 + thick1)} {_f(xm)},{_f(y0 + thick0)} {_f(x0)},{_f(y0 + thick0)}Z"
    )


def render_svg_sankey(aggregate: dict[str, Any], *, standalone: bool = False) -> str:
    """Draw the introducing-release to fixing-release Sankey (soft view); band colour is the age bucket.

    Same-cycle fixes are never drawn as a band (they are listed in a table). At most ``MAX_SANKEY_FLOWS`` bands are
    drawn; the caption chart note reports how many lighter bands were left out.
    """
    title = "Sankey: introducing release to fixing release (soft view)"
    flows, omitted = _sankey_flows(aggregate)
    if not flows:
        return _empty_chart(title, "No cross-release fixes to draw.", standalone)
    order = _release_order(aggregate)
    left = sorted({f["from"] for f in flows}, key=lambda n: _node_key(n, order))
    right = sorted({f["to"] for f in flows}, key=lambda n: _node_key(n, order))
    lt: dict[str, float] = defaultdict(float)
    rt: dict[str, float] = defaultdict(float)
    for f in flows:
        lt[f["from"]] += f["weight"]
        rt[f["to"]] += f["weight"]
    total = sum(lt.values())
    nmax = max(len(left), len(right))
    height, top, gap = max(440, 16 * nmax + 70), 44.0, 4.0
    scale = min((height - top - 16 - gap * (len(c) - 1)) / total for c in (left, right))
    scale = max(scale, 0.01)
    width, node_w, margin = 900, 14, 112
    lpos = _stack(left, lt, scale, top, gap)
    rpos = _stack(right, rt, scale, top, gap)
    xl, xr = margin, width - margin - node_w
    loff = _band_offsets(flows, "from", "to", order, scale)
    roff = _band_offsets(flows, "to", "from", order, scale)
    body = _legend([(BAND_CLASSES[b], BUCKET_LABELS[b], "rect") for b in BAND_CLASSES], 16, 20)
    for i, f in enumerate(flows):
        thick = max(f["weight"] * scale, 0.5)
        y0, y1 = lpos[f["from"]][0] + loff[i], rpos[f["to"]][0] + roff[i]
        tip = f"{f['from']} → {f['to']}: {_f(f['weight'], 2)} fixes (soft), {BUCKET_LABELS[f['bucket']]}"
        path = _band_path(xl + node_w, y0, xr, y1, thick, thick)
        body.append(
            f'<path class="band {BAND_CLASSES.get(f["bucket"], "b3")}" d="{path}"><title>{esc_xml(tip)}</title></path>'
        )
    for pos, x, anchor, dx, totals in ((lpos, xl, "end", -6, lt), (rpos, xr, "start", node_w + 6, rt)):
        for node, (y, h) in pos.items():
            tip = f"{node}: {_f(totals[node], 2)} fixes (soft)"
            body.append(
                f'<rect class="node" x="{x}" y="{_f(y)}" width="{node_w}" height="{_f(h)}"><title>{esc_xml(tip)}</title></rect>'
            )
            if h + gap >= 10:
                body.append(_text(x + dx, y + h / 2 + 4, node, "t1", anchor, limit=16))
    body.append(_text(xl, top - 8, "introduced in", "tm", limit=30))
    body.append(_text(xr + node_w, top - 8, "fixed in", "tm", "end", limit=30))
    note = f"{omitted} lighter flows not drawn." if omitted else "All flows drawn."
    return _svg_wrap(body, width, int(height), title, f"Soft-view fix weight by age bucket. {note}", standalone)


# --------------------------------------------------------------------------------------------------------------------
# Triangular heatmap (soft view)
# --------------------------------------------------------------------------------------------------------------------


def _heat_cells(aggregate: dict[str, Any], shown: list[str]) -> tuple[dict[tuple[str, str], float], bool]:
    """Sum heatmap weights into ``{(introduced_row, fixed_column): weight}`` for the shown releases.

    The second value says whether older releases were folded into the pre-window row.
    """
    inside = set(shown)
    cells: dict[tuple[str, str], float] = defaultdict(float)
    folded = False
    for fixed, row in (aggregate.get("heatmap") or {}).items():
        if fixed not in inside or not isinstance(row, dict):
            continue
        for src, weight in row.items():
            key = fixed if src == "same-cycle" else str(src)
            if key != PRE_WINDOW and key not in inside:
                key, folded = PRE_WINDOW, True
            cells[(key, fixed)] += _num(weight)
    return cells, folded


def render_svg_heatmap(aggregate: dict[str, Any], *, standalone: bool = False) -> str:
    """Draw the triangular introduced-by-fixed release heatmap (soft view), one-hue sequential colour.

    Rows are the release that introduced the code (oldest first, with a pre-window row on top); columns are the release
    that fixed it. Only the most recent ``MAX_HEATMAP_RELEASES`` releases are drawn.
    """
    title = "Release heatmap: introduced by fixed (soft view)"
    rels = [str(v) for v in aggregate.get("releases") or []][-MAX_HEATMAP_RELEASES:]
    cells, folded = _heat_cells(aggregate, rels)
    top = max(cells.values(), default=0.0)
    if not rels or top <= 0:
        return _empty_chart(title, "No soft fix weight to draw.", standalone)
    n = len(rels)
    cs = max(9, min(26, 640 // n))
    gx, gy = 112, 96
    stride = max(1, math.ceil(11 / cs))
    rows = [PRE_WINDOW, *rels]
    width, height = gx + cs * n + 24, gy + cs * (n + 1) + 56
    body = [_text(gx, 16, "fixed in →", "tm"), _text(8, gy - 8, "introduced in ↓", "tm")]
    for j, rel in enumerate(rels):
        if j % stride == 0:
            x, y = gx + j * cs + cs / 2 + 4, gy - 6
            body.append(_text(x, y, rel, "t2", extra=f' transform="rotate(-90 {_f(x)} {_f(y)})"', limit=14))
    for i, name in enumerate(rows):
        if i % stride == 0:
            body.append(_text(gx - 6, gy + i * cs + cs / 2 + 4, name, "t2", "end", limit=16))
    for (src, fixed), weight in sorted(cells.items()):
        i, j = (0 if src == PRE_WINDOW else rels.index(src) + 1), rels.index(fixed)
        if weight <= 0 or i - 1 > j:
            continue
        tip = f"introduced {src}, fixed {fixed}: {_f(weight, 2)} fixes (soft)"
        body.append(
            f'<rect class="h{_level(weight, top)}" x="{gx + j * cs}" y="{gy + i * cs}" width="{cs - 1}" height="{cs - 1}">'
            f"<title>{esc_xml(tip)}</title></rect>"
        )
    body.append(f'<line class="axis" x1="{gx}" y1="{gy}" x2="{gx + cs * n}" y2="{gy}"/>')
    ly = gy + cs * (n + 1) + 28
    body.append(_text(gx, ly + 11, "fewer", "tm"))
    for k in range(1, 7):
        body.append(f'<rect class="h{k}" x="{gx + 40 + (k - 1) * 20}" y="{ly}" width="18" height="14" rx="2"/>')
    body.append(_text(gx + 40 + 6 * 20 + 6, ly + 11, f"more (max {_f(top, 2)} fixes in one cell)", "tm", limit=60))
    if folded:
        body.append(
            _text(gx, ly + 30, "pre-window row also holds releases older than the window shown.", "tm", limit=90)
        )
    desc = f"Soft-view fix weight per introduced and fixed release over {n} releases."
    return _svg_wrap(body, width, height + (16 if folded else 0), title, desc, standalone)


# --------------------------------------------------------------------------------------------------------------------
# Burn-up
# --------------------------------------------------------------------------------------------------------------------


def _spread_labels(labels: list[tuple[float, str, str]], gap: float = 13.0) -> list[tuple[float, str, str]]:
    """Push end-of-line labels apart vertically so none overlap; input and output are ``(y, text, class)``."""
    placed: list[tuple[float, str, str]] = []
    last = -1e9
    for y, text, cls in sorted(labels, key=lambda item: item[0]):
        y = max(y, last + gap)
        placed.append((y, text, cls))
        last = y
    return placed


def render_svg_burnup(aggregate: dict[str, Any], bugs: Any = None, *, standalone: bool = False) -> str:
    """Draw cumulative features and fixes per release, plus open bugs as a dashed approximate line when given.

    All series share one axis (counts). Lines are 2 px, markers appear for up to 30 releases, an invisible hit column
    per release carries the hover tooltip, and each line is also labelled directly at its end.
    """
    title = "Burn-up: cumulative features and fixes"
    points = [p for p in aggregate.get("burnup") or [] if isinstance(p, dict)]
    if not points:
        return _empty_chart(title, "No release burn-up data.", standalone)
    series = [
        ("features", "ln1", "solid", {str(p.get("release")): _num(p.get("features")) for p in points}),
        ("fixes", "ln2", "solid", {str(p.get("release")): _num(p.get("fixes")) for p in points}),
    ]
    bug_map = _bug_series(bugs)
    rels = [str(p.get("release")) for p in points]
    if any(r in bug_map for r in rels):
        series.append(("open bugs (approximate)", "ln3", "dash", {r: bug_map[r] for r in rels if r in bug_map}))
    top, step = _nice_max(max(v for *_, data in series for v in data.values()))
    width, height, ml, mr, mt, mb = 900, 400, 56, 220, 48, 76
    pw, ph, n = width - ml - mr, height - mt - mb, len(rels)

    def sx(i: int) -> float:
        return ml + (pw / 2 if n == 1 else i * pw / (n - 1))

    def sy(v: float) -> float:
        return mt + ph - ph * v / top

    body = _legend([(cls, label, kind if kind == "dash" else "line") for label, cls, kind, _ in series], ml, 22)
    tick = 0.0
    while tick <= top + 1e-9:
        body.append(f'<line class="grid" x1="{ml}" y1="{_f(sy(tick))}" x2="{ml + pw}" y2="{_f(sy(tick))}"/>')
        body.append(_text(ml - 6, sy(tick) + 4, f"{tick:g}", "tm", "end"))
        tick += step
    stride = max(1, math.ceil(n / 24))
    for i, rel in enumerate(rels):
        if i % stride == 0:
            x, y = sx(i) + 4, mt + ph + 14
            body.append(_text(x, y, rel, "t2", "end", f' transform="rotate(-45 {_f(x)} {_f(y)})"', 14))
    ends: list[tuple[float, str, str]] = []
    for label, cls, kind, data in series:
        pts = [(sx(i), sy(data[r])) for i, r in enumerate(rels) if r in data]
        dash = " dash" if kind == "dash" else ""
        body.append(f'<polyline class="line {cls}{dash}" points="{" ".join(f"{_f(x)},{_f(y)}" for x, y in pts)}"/>')
        if n <= 30:
            body.extend(f'<circle class="dot b{cls[-1]}" cx="{_f(x)}" cy="{_f(y)}" r="4"/>' for x, y in pts)
        last = rels[max(i for i, r in enumerate(rels) if r in data)]
        ends.append((sy(data[last]) + 4, f"{label} {data[last]:g}", "t1"))
    body.extend(_text(ml + pw + 8, y, text, cls, limit=34) for y, text, cls in _spread_labels(ends))
    colw = pw / max(n - 1, 1) if n > 1 else pw
    for i, rel in enumerate(rels):
        tip = "; ".join([rel, *(f"{lab} {data[rel]:g}" for lab, _, _, data in series if rel in data)])
        body.append(
            f'<rect class="hit" x="{_f(sx(i) - colw / 2)}" y="{mt}" width="{_f(colw)}" height="{ph}">'
            f"<title>{esc_xml(tip)}</title></rect>"
        )
    desc = "Cumulative features and fixes per release" + (
        "; open bugs are an approximation." if len(series) > 2 else "."
    )
    return _svg_wrap(body, width, height, title, desc, standalone)


# --------------------------------------------------------------------------------------------------------------------
# Feature x fix matrix (binary view)
# --------------------------------------------------------------------------------------------------------------------

_MX_LABEL_W: Final = 260
_MX_CELL: Final = 14


def _top_features(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the up-to-``MAX_MATRIX_FEATURES`` highest binary-count features from the aggregate."""
    items = [t for t in report["aggregate"].get("top_features") or [] if isinstance(t, dict) and "column_id" in t]
    items.sort(key=lambda t: (-_num(t.get("fix_count")), str(t.get("column_id"))))
    return items[:MAX_MATRIX_FEATURES]


def _row_hits(row: dict[str, Any], col_index: dict[str, int]) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """Split a row's binary cells into top-column hits and "other" binary hits."""
    hits: dict[int, dict[str, Any]] = {}
    other: list[dict[str, Any]] = []
    for cell in row.get("cells") or []:
        if not isinstance(cell, dict) or not cell.get("binary"):
            continue
        idx = col_index.get(str(cell.get("column_id")))
        if idx is None:
            other.append(cell)
        else:
            hits[idx] = cell
    return hits, other


def _select_matrix_rows(
    report: dict[str, Any], col_index: dict[str, int]
) -> tuple[list[tuple[str, list[dict[str, Any]]]], int]:
    """Pick matrix rows grouped by fix release, capped overall; return ``(groups, total_eligible)``."""
    order = _release_order(report["aggregate"])
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    eligible = 0
    for row in sorted((r for r in report["rows"] if isinstance(r, dict)), key=lambda r: str(r.get("fix_id"))):
        hits, other = _row_hits(row, col_index)
        if hits or other:
            eligible += 1
            groups[str(row.get("fix_release"))].append(row)
    per_group = max(3, MAX_MATRIX_ROWS // max(len(groups), 1))
    ordered = sorted(groups, key=lambda r: (order.get(r, 10**6), r))
    picked = [(rel, groups[rel][:per_group]) for rel in ordered]
    return picked, eligible


def _matrix_header(cols: list[dict[str, Any]], width: int, repo_titles: dict[str, str]) -> str:
    """Column-label header SVG shared by every release group."""
    height = 150
    body = []
    for j, col in enumerate(cols):
        x, y = _MX_LABEL_W + j * _MX_CELL + _MX_CELL / 2 + 4, height - 6
        label = repo_titles.get(str(col["column_id"]), col["column_id"])
        body.append(_text(x, y, label, "t2", extra=f' transform="rotate(-90 {_f(x)} {_f(y)})"', limit=24))
    x, y = _MX_LABEL_W + len(cols) * _MX_CELL + _MX_CELL / 2 + 4, height - 6
    body.append(_text(x, y, "other", "tm", extra=f' transform="rotate(-90 {_f(x)} {_f(y)})"'))
    body.append(_text(4, height - 6, "fix ↓ / feature →", "tm"))
    return _svg_wrap(body, width, height, "Matrix column labels", "Feature columns of the binary matrix.", False)


def _matrix_group(rows: list[dict[str, Any]], cols: list[dict[str, Any]], col_index: dict[str, int], width: int) -> str:
    """One release group of matrix rows as an SVG: filled cell = binary link; title carries raw and soft scores."""
    rh = _MX_CELL + 1
    ncols = len(cols)
    body: list[str] = []
    for j in range(ncols + 2):
        x = _MX_LABEL_W + j * _MX_CELL
        body.append(f'<line class="grid" x1="{x}" y1="0" x2="{x}" y2="{rh * len(rows)}"/>')
    for i, row in enumerate(rows):
        y = i * rh
        hits, other = _row_hits(row, col_index)
        name = row.get("title") or row.get("text") or row.get("fix_id")
        body.append(f"<g><title>{esc_xml(name, 160)}</title>{_text(4, y + 11, name, 't1', limit=42)}</g>")
        for j, cell in sorted(hits.items()):
            body.append(
                _matrix_cell(_MX_LABEL_W + j * _MX_CELL, y, [cell], name, cols[j].get("title") or cols[j]["column_id"])
            )
        if other:
            body.append(_matrix_cell(_MX_LABEL_W + ncols * _MX_CELL, y, other, name, "other features"))
    return _svg_wrap(body, width, rh * len(rows) + 2, "Matrix rows", "Binary feature links for one fix release.", False)


def _matrix_cell(x: int, y: int, cells: list[dict[str, Any]], fix: Any, feature: Any) -> str:
    """One filled matrix cell; its tooltip lists raw and soft scores of the cells it stands for."""
    raw = max(_num(c.get("raw_score")) for c in cells)
    soft = sum(_num(c.get("soft_share")) for c in cells)
    methods = ", ".join(sorted({clean(c.get("method"), 20) for c in cells}))
    tip = f"{clean(fix, 80)} / {clean(feature, 60)}: raw {_f(raw, 2)}, soft {_f(soft, 2)} ({methods}), binary yes"
    return (
        f'<rect class="mx" x="{x + 1}" y="{y + 1}" width="{_MX_CELL - 2}" height="{_MX_CELL - 2}" rx="2">'
        f"<title>{esc_xml(tip, 300)}</title></rect>"
    )


def render_matrix(report: dict[str, Any]) -> str:
    """Return the HTML fragment of the binary feature-by-fix matrix, rows grouped by fix release in ``<details>``.

    Shows the top ``MAX_MATRIX_FEATURES`` features by binary fix count plus one "other" column for the remaining
    features; at most ``MAX_MATRIX_ROWS`` fix rows are drawn, the rest stay in ``matrix.csv``.
    """
    cols = _top_features(report)
    col_index = {str(c["column_id"]): j for j, c in enumerate(cols)}
    titles = {
        str(c.get("column_id")): clean(c.get("title") or c.get("column_id"), 28)
        for c in report["columns"]
        if isinstance(c, dict)
    }
    groups, eligible = _select_matrix_rows(report, col_index)
    drawn = sum(len(rows) for _, rows in groups)
    if not groups:
        return "<p>No binary feature links to show.</p>"
    width = _MX_LABEL_W + (len(cols) + 2) * _MX_CELL + 8
    parts = [f'<div class="card">{_matrix_header(cols, width, titles)}']
    for rel, rows in groups:
        total = sum(1 for r in report["rows"] if isinstance(r, dict) and str(r.get("fix_release")) == rel)
        parts.append(
            f"<details><summary>{esc(rel, 40)}: {total} fixes, {len(rows)} shown</summary>"
            f"{_matrix_group(rows, cols, col_index, width)}</details>"
        )
    parts.append("</div>")
    note = f"Showing {drawn} of {eligible} fixes with a binary link; the full matrix is in matrix.csv."
    parts.append(f'<p class="cap">{esc(note)}</p>')
    return "".join(parts)


# --------------------------------------------------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------------------------------------------------


def _html_table(headers: list[tuple[str, bool]], rows: list[list[str]]) -> str:
    """Build an HTML table; ``headers`` are ``(text, numeric)`` and cells are already escaped."""
    head = "".join(f'<th class="{"n" if num else ""}">{esc(text)}</th>' for text, num in headers)
    body = "".join(
        "<tr>"
        + "".join(f'<td class="{"n" if headers[i][1] else ""}">{cell}</td>' for i, cell in enumerate(row))
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _ranking_html(report: dict[str, Any]) -> str:
    """Most-fixed features ranking (binary view) with inline bars."""
    items = _top_features(report)[:MAX_RANKING_ROWS]
    if not items:
        return "<p>No features with binary fix links.</p>"
    top = max(_num(t.get("fix_count")) for t in items) or 1.0
    urls = {str(c.get("column_id")): c.get("url") for c in report["columns"] if isinstance(c, dict)}
    rows = []
    for rank, item in enumerate(items, 1):
        count = _num(item.get("fix_count"))
        bar = f'<svg viewBox="0 0 100 8" width="100" height="8" role="img"><rect class="mx" width="{_f(100 * count / top)}" height="8" rx="2"/></svg>'
        link = link_or_text(
            item.get("title") or item.get("column_id"),
            item.get("url") or urls.get(str(item.get("column_id"))),
            report["repo"],
        )
        rows.append([str(rank), link, esc(item.get("release"), 30), f"{count:g}", bar])
    return _html_table(
        [("#", True), ("Feature", False), ("Introduced", False), ("Fixes (binary)", True), ("", False)], rows
    )


def _lookup_titles(report: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(fix_id -> title, column_id -> title)`` maps from the rows and columns."""
    fixes = {
        str(r.get("fix_id")): clean(r.get("title") or r.get("text") or r.get("fix_id"))
        for r in report["rows"]
        if isinstance(r, dict)
    }
    cols = {
        str(c.get("column_id")): clean(c.get("title") or c.get("column_id"))
        for c in report["columns"]
        if isinstance(c, dict)
    }
    return fixes, cols


def _same_cycle_html(report: dict[str, Any]) -> str:
    """Same-cycle table: fixes whose feature shipped in the same release."""
    items = [s for s in report["aggregate"].get("same_cycle") or [] if isinstance(s, dict)]
    if not items:
        return "<p>No same-cycle fixes.</p>"
    fixes, cols = _lookup_titles(report)
    rows = [
        [
            esc(fixes.get(str(s.get("fix_id")), s.get("fix_id")), 80),
            esc(cols.get(str(s.get("column_id")), s.get("column_id")), 80),
            esc(s.get("release"), 30),
        ]
        for s in items[:MAX_TABLE_ROWS]
    ]
    more = f'<p class="cap">{len(items) - MAX_TABLE_ROWS} more not shown.</p>' if len(items) > MAX_TABLE_ROWS else ""
    return _html_table([("Fix", False), ("Feature", False), ("Release", False)], rows) + more


def _residue_columns(aggregate: dict[str, Any]) -> list[str]:
    """Residue bucket names present in the aggregate, standard ones first."""
    seen = {b for row in (aggregate.get("residue") or {}).values() if isinstance(row, dict) for b in row}
    return [b for b in RESIDUE_COLUMNS if b in seen] + sorted(seen - set(RESIDUE_COLUMNS))


def _residue_rows(aggregate: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    """Return ``(bucket_names, rows)`` where each row is ``[release, weights..., total]`` as plain strings."""
    buckets = _residue_columns(aggregate)
    order = _release_order(aggregate)
    residue = aggregate.get("residue") or {}
    rows = []
    for rel in sorted(residue, key=lambda r: (order.get(str(r), 10**6), str(r))):
        data = residue[rel] if isinstance(residue[rel], dict) else {}
        weights = [_num(data.get(b)) for b in buckets]
        rows.append([str(rel), *[_f(w, 2) for w in weights], _f(sum(weights), 2)])
    return buckets, rows


def _residue_html(report: dict[str, Any]) -> str:
    """Residue table: soft weight that no feature could explain, per fixing release."""
    buckets, rows = _residue_rows(report["aggregate"])
    if not rows:
        return "<p>No residue.</p>"
    headers = [("Release", False), *[(b, True) for b in buckets], ("Total", True)]
    return _html_table(headers, [[esc(r[0], 30), *r[1:]] for r in rows])


def _age_rows(aggregate: dict[str, Any]) -> list[list[str]]:
    """Return fixes per fixing release by age bucket (soft view) as ``[release, buckets..., total]`` strings."""
    sums: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for flow in aggregate.get("flow") or []:
        bucket = "same-cycle" if flow.get("from") == "same-cycle" else str(flow.get("bucket"))
        sums[str(flow.get("to"))][bucket] += _num(flow.get("weight"))
    order = _release_order(aggregate)
    rows = []
    for rel in sorted(sums, key=lambda r: (order.get(r, 10**6), r)):
        vals = [sums[rel].get(b, 0.0) for b in BUCKETS]
        rows.append([rel, *[_f(v, 1) for v in vals], _f(sum(vals), 1)])
    return rows


def _age_html(report: dict[str, Any]) -> str:
    """Return fixes per release by age bucket as an HTML table."""
    rows = _age_rows(report["aggregate"])
    if not rows:
        return ""
    headers = [("Fixed in", False), *[(BUCKET_LABELS[b], True) for b in BUCKETS], ("Total", True)]
    return _html_table(headers, [[esc(r[0], 30), *r[1:]] for r in rows])


# --------------------------------------------------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------------------------------------------------

_VIEW_NOTE: Final = (
    "Soft and binary totals differ by design: in the soft view each fix carries one unit of weight shared across the "
    "features that explain it, so soft totals add up to the fix count; in the binary view a fix counts for every "
    "feature whose score reaches the threshold, so one fix can count for several features or for none."
)


def _stats_html(report: dict[str, Any]) -> str:
    """Headline numbers as stat tiles."""
    totals = report["aggregate"].get("totals") or {}
    tiles = [
        ("Fixes", f"{_num(totals.get('fixes')):g}"),
        ("Soft weight total", _f(_num(totals.get("soft_sum")), 1)),
        ("Binary links", f"{_num(totals.get('binary_links')):g}"),
        ("Releases", str(len(report["aggregate"].get("releases") or []))),
    ]
    return (
        '<div class="stats">' + "".join(f'<div class="stat"><b>{esc(v)}</b>{esc(k)}</div>' for k, v in tiles) + "</div>"
    )


def _manifest_html(manifest: dict[str, Any]) -> str:
    """Small definition list of the manifest facts worth showing."""
    items = []
    for key in ("scope", "threshold", "plugin_version", "label_set", "weights"):
        if key in manifest:
            value = manifest[key]
            text = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
            items.append(f"<dt>{esc(key)}</dt><dd>{esc(text, 160)}</dd>")
    return f"<dl>{''.join(items)}</dl>" if items else ""


def _embedded_data(report: dict[str, Any]) -> dict[str, Any]:
    """Bounded data block embedded in the page (no per-fix rows; they live in the store and matrix.csv)."""
    agg = dict(report["aggregate"])
    agg["top_features"] = list(agg.get("top_features") or [])[:MAX_EMBED_ROWS]
    agg["same_cycle"] = list(agg.get("same_cycle") or [])[:MAX_EMBED_ROWS]
    return {"repo": report["repo"], "manifest": report["manifest"], "aggregate": agg, "warnings": report["warnings"]}


def _chart_section(title: str, anchor: str, caption: str, svg: str, extra: str = "") -> str:
    """One titled chart section: heading, caption naming the view, the SVG card and optional extras."""
    return f'<section id="{anchor}"><h2>{esc(title)}</h2><p class="cap">{esc(caption, 700)}</p><div class="card">{svg}</div>{extra}</section>'


def _heat_table_html(aggregate: dict[str, Any]) -> str:
    """Table view of the heatmap (accessible alternative), capped."""
    rels = [str(v) for v in aggregate.get("releases") or []][-MAX_HEATMAP_RELEASES:]
    cells, _ = _heat_cells(aggregate, rels)
    rows = [
        [esc(s, 30), esc(f, 30), _f(w, 2)]
        for (s, f), w in sorted(cells.items(), key=lambda kv: -kv[1])[:MAX_TABLE_ROWS]
        if w > 0
    ]
    table = (
        _html_table([("Introduced", False), ("Fixed", False), ("Soft weight", True)], rows)
        if rows
        else "<p>No cells.</p>"
    )
    return f"<details><summary>Table view</summary>{table}</details>"


def _burn_table_html(aggregate: dict[str, Any], bugs: Any) -> str:
    """Table view of the burn-up series."""
    bug_map = _bug_series(bugs)
    rows = [
        [
            esc(p.get("release"), 30),
            f"{_num(p.get('features')):g}",
            f"{_num(p.get('fixes')):g}",
            f"{bug_map[str(p.get('release'))]:g}" if str(p.get("release")) in bug_map else "-",
        ]
        for p in aggregate.get("burnup") or []
        if isinstance(p, dict)
    ]
    table = _html_table([("Release", False), ("Features", True), ("Fixes", True), ("Open bugs (approx.)", True)], rows)
    return f"<details><summary>Table view</summary>{table}</details>"


def render_html(report: dict[str, Any]) -> str:
    """Render the self-contained HTML report: no external script, stylesheet or font, and no inline handlers."""
    repo, manifest, agg, bugs = report["repo"], report["manifest"], report["aggregate"], report.get("bugs")
    name = (
        f"{clean(repo.get('owner'), 40)}/{clean(repo.get('repo') or repo.get('name'), 60)}".strip("/") or "repository"
    )
    sections = [f"<h1>Release evolution: {esc(name, 120)}</h1>", _manifest_html(manifest), _stats_html(report)]
    if manifest.get("weights_provisional"):
        sections.insert(
            1,
            '<div class="banner" role="note"><b>Provisional weights.</b> The scoring weights have not passed the real-repository gate; treat scores as indicative.</div>',
        )
    warnings = [w for w in report["warnings"] if w]
    if warnings:
        sections.append(
            '<section id="warnings"><h2>Warnings</h2><ul>'
            + "".join(f"<li>{esc(w, 300)}</li>" for w in warnings)
            + "</ul></section>"
        )
    sections.append(f'<p class="cap">{esc(_VIEW_NOTE, 600)}</p>')
    sections.append(
        _chart_section(
            "Sankey: where fixed code came from",
            "sankey",
            "Soft view. Band width is the soft fix weight from the introducing release (left) to the fixing release (right); colour is the age bucket. Same-cycle fixes are not drawn as bands; see the same-cycle table.",
            render_svg_sankey(agg),
        )
    )
    sections.append(
        _chart_section(
            "Release heatmap",
            "heatmap",
            "Soft view. Each cell is the soft fix weight for code introduced in the row release and fixed in the column release. Darker means more weight.",
            render_svg_heatmap(agg),
            _heat_table_html(agg),
        )
    )
    burn_cap = "Count view (neither soft nor binary): cumulative features and fixes per release as plain entry counts."
    if _bug_series(bugs):
        burn_cap += (
            " The dashed open-bugs line is approximate: it is rebuilt from GitHub issue labels and release dates."
        )
    sections.append(
        _chart_section("Burn-up", "burnup", burn_cap, render_svg_burnup(agg, bugs), _burn_table_html(agg, bugs))
    )
    sections.append(
        '<section id="matrix"><h2>Feature by fix matrix</h2><p class="cap">Binary view. A filled cell means the fix counts for that feature (score at or above the threshold); hover shows raw and soft scores. Rows are grouped by fixing release. Soft totals elsewhere on this page differ by design.</p>'
        + render_matrix(report)
        + "</section>"
    )
    sections.append(
        f'<section id="ranking"><h2>Most-fixed features</h2><p class="cap">Binary view: number of fixes counted for each feature.</p>{_ranking_html(report)}</section>'
    )
    sections.append(
        f'<section id="age"><h2>Fixes per release by age bucket</h2><p class="cap">Soft view: weight of fixes by how many releases earlier the code was introduced.</p>{_age_html(report)}</section>'
    )
    sections.append(
        f'<section id="same-cycle"><h2>Same-cycle fixes</h2><p class="cap">Fixes landed in the same release as the feature they repair; the Sankey omits them.</p>{_same_cycle_html(report)}</section>'
    )
    sections.append(
        f'<section id="residue"><h2>Residue</h2><p class="cap">Soft view: weight no feature could be blamed for, by reason.</p>{_residue_html(report)}</section>'
    )
    data = f'<script type="application/json" id="evolution-data">{embed_json(_embedded_data(report))}</script>'
    head = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{html.escape(CSP, quote=True)}">'
        f"<title>Release evolution: {esc(name, 120)}</title><style>{THEME_CSS}{CHART_CSS}{PAGE_CSS}</style></head>"
    )
    return f'{head}<body class="viz-root"><main>{"".join(sections)}</main>{data}</body></html>\n'


# --------------------------------------------------------------------------------------------------------------------
# Markdown report
# --------------------------------------------------------------------------------------------------------------------


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Pipe table; every cell must already be escaped."""
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def render_markdown(report: dict[str, Any]) -> str:
    """Render the Markdown summary: view-labelled tables plus references to the static heatmap and burn-up SVGs."""
    agg, manifest = report["aggregate"], report["manifest"]
    totals = agg.get("totals") or {}
    lines = [
        "## Summary",
        "",
        f"- Fixes: {_num(totals.get('fixes')):g}; soft weight total {_f(_num(totals.get('soft_sum')), 1)}; "
        f"binary links {_num(totals.get('binary_links')):g}; releases {len(agg.get('releases') or [])}.",
        f"- {md_cell(_VIEW_NOTE, 600)}",
    ]
    if manifest.get("weights_provisional"):
        lines.append("- **Provisional weights:** scores are indicative until the real-repository gate passes.")
    lines += [f"- Warning: {md_cell(w, 300)}" for w in report["warnings"] if w]
    top = _top_features(report)[:MAX_RANKING_ROWS]
    lines += ["", "### Most-fixed features (binary view)", ""]
    if top:
        rows = [
            [
                str(i),
                md_cell(t.get("title") or t.get("column_id"), 80),
                md_cell(t.get("release"), 30),
                f"{_num(t.get('fix_count')):g}",
            ]
            for i, t in enumerate(top, 1)
        ]
        lines.append(_md_table(["#", "Feature", "Introduced", "Fixes"], rows))
    else:
        lines.append("No features with binary fix links.")
    lines += ["", "### Fixes per release by age bucket (soft view)", ""]
    age = _age_rows(agg)
    lines.append(
        _md_table(
            ["Fixed in", *[BUCKET_LABELS[b] for b in BUCKETS], "Total"], [[md_cell(r[0], 30), *r[1:]] for r in age]
        )
        if age
        else "No age data."
    )
    lines += ["", "### Residue per release (soft view)", ""]
    buckets, residue = _residue_rows(agg)
    lines.append(
        _md_table(
            ["Release", *[md_cell(b, 30) for b in buckets], "Total"], [[md_cell(r[0], 30), *r[1:]] for r in residue]
        )
        if residue
        else "No residue."
    )
    lines += [
        "",
        "### Charts",
        "",
        "Soft view release heatmap (introduced by fixed):",
        "",
        "![heatmap](heatmap.svg)",
        "",
    ]
    lines += [
        "Cumulative features and fixes per release (open bugs, when shown, are approximate):",
        "",
        "![burn-up](burnup.svg)",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------------------------
# Orchestration and CLI
# --------------------------------------------------------------------------------------------------------------------


def _normalise(report: dict[str, Any]) -> dict[str, Any]:
    """Fill missing report keys with empty values so renderers can index them directly."""
    return {
        "repo": report.get("repo") if isinstance(report.get("repo"), dict) else {},
        "manifest": report.get("manifest") if isinstance(report.get("manifest"), dict) else {},
        "rows": list(report.get("rows") or []),
        "columns": list(report.get("columns") or []),
        "aggregate": report.get("aggregate") if isinstance(report.get("aggregate"), dict) else {},
        "bugs": report.get("bugs"),
        "warnings": [clean(w, 300) for w in report.get("warnings") or []],
    }


def render_report(report: dict[str, Any]) -> dict[str, str]:
    """Render every artefact; return ``{"report.html", "report.md", "heatmap.svg", "burnup.svg"}`` as text.

    Args:
        report: ``{"repo", "manifest", "rows", "columns", "aggregate", "bugs" | None, "warnings"}``.
    """
    rep = _normalise(report)
    return {
        "report.html": render_html(rep),
        "report.md": render_markdown(rep),
        "heatmap.svg": render_svg_heatmap(rep["aggregate"], standalone=True),
        "burnup.svg": render_svg_burnup(rep["aggregate"], rep["bugs"], standalone=True),
    }


def _load(path: Path, key: str | None = None) -> Any:
    """Load a JSON document, or a JSONL file (last record per ``key`` wins); raise ``ValueError`` when unreadable."""
    try:
        if path.suffix == ".jsonl":
            records, _ = read_jsonl(path)
            return list(last_by_key(records, key).values()) if key else records
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for the ``render`` subcommand."""
    parser = argparse.ArgumentParser(description="Render the evolution report as HTML, Markdown and SVG.")
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render", help="write report.html, report.md, heatmap.svg and burnup.svg")
    # Literal flag names: the SKILL/docstring flag-drift hook reads add_argument calls verbatim.
    render.add_argument("--rows", required=True, type=Path, help="scored rows JSONL")
    render.add_argument("--columns", required=True, type=Path, help="column records JSONL")
    render.add_argument("--aggregate", required=True, type=Path, help="aggregate JSON")
    render.add_argument("--manifest", required=True, type=Path, help="store manifest JSON")
    render.add_argument("--repo-json", required=True, type=Path, help="repo identity JSON {host, owner, repo}")
    render.add_argument("--out-dir", required=True, type=Path, help="directory for the four report files")
    render.add_argument("--bugs", type=Path, default=None, help="open-bug series JSON (optional)")
    render.add_argument("--warnings", type=Path, default=None, help="JSON list of warning strings (optional)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        report = {
            "repo": _load(args.repo_json),
            "manifest": _load(args.manifest),
            "rows": _load(args.rows, "fix_id"),
            "columns": _load(args.columns, "column_id"),
            "aggregate": _load(args.aggregate),
            "bugs": _load(args.bugs) if args.bugs else None,
            "warnings": _load(args.warnings) if args.warnings else [],
        }
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    outputs = render_report(report)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in outputs.items():
        with (args.out_dir / name).open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    print(f"rendered {len(outputs)} files to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
