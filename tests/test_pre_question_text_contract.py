"""Keep decision content out of reply text written right before an ``AskUserQuestion`` call.

On models that return text written before a tool call as a progress-update thinking block (empty under the default
``display``), anything a skill prints and then follows with a question can vanish: the user sees "Challenger raised 2
blocker(s). How to proceed?" with no blockers, or a question saying "(shown above)" with nothing above it, and decides
blind. Decision content therefore travels in the question itself — its question text, or the ``preview`` of every option
of a single-select question — or, when no tool call follows, in the turn's final reply.

This guard scans every Claude-facing plugin instruction file that mentions ``AskUserQuestion`` for the ordering phrases
that put the content first and the question second: "print X, then invoke ``AskUserQuestion``", "print X,
``AskUserQuestion``: …", "show X, then ask Y", "show X per hit, ask: …", "write all sections inline, then invoke
``AskUserQuestion``", "(shown above)", "in the message that issues …", "print X before the question". A match passes
only when the same sentence places the content in the question (``preview``, question text) or the final reply.

Those phrases are per-line, so a second, structural check covers the same ordering split across steps: a numbered step
or heading that opens with a display verb and a content noun ("5. Print queue as formatted table:", "3. Output ordered
task table:"), followed — later in that step or in the next one — by an ``AskUserQuestion``. It passes when a sentence
in either step names the placement together with that noun ("the queue table as the ``preview`` of every option"), which
keeps a report printed for a follow-up whose previews carry it green. ``report-header`` tables have their own show-once
contract (``test_report_header_show_once_contract.py``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

#: Paths (relative to ``plugins/``, POSIX form) left out of the scan, each with the reason. None today.
_EXCLUDED_PREFIXES: tuple[str, ...] = ()

#: Ordering phrases that print decision content first and ask about it afterwards.
_ORDERING = (
    re.compile(
        r"\b(?:print|show|present|surface|display|emit|render|report|list|output)(?:s|ed)?\b[^.;!?]{0,250}?"
        r"\bthen\b[^.;!?]{0,40}?\bAskUserQuestion\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:print|show|present|display|output)(?:s|ed)?\b[^.;!?]{0,250}?\.\s+Then (?:call|invoke) AskUserQuestion\b",
        re.IGNORECASE,
    ),
    # Comma-joined: "Print the instructions below, `AskUserQuestion`: …", "show X per hit, ask: …".
    re.compile(
        r"\b(?:print|show|present|surface|display|emit|render|list|output)(?:s|ed)?\b[^.;!?]{0,250}?"
        r",\s*(?:then\s+)?(?:AskUserQuestion|ask(?:s|ed)?)\b",
        re.IGNORECASE,
    ),
    # "show X, then ask Y" with no tool name in reach of the first pattern.
    re.compile(
        r"\b(?:print|show|present|surface|display|emit|render|list|output)(?:s|ed)?\b[^.;!?]{0,250}?"
        r"\bthen ask(?:s|ed)?\b",
        re.IGNORECASE,
    ),
    # Content written into the reply "inline", then asked about: "Write all 6 sections inline, then invoke …".
    # Requires "inline" so a file write followed by a question ("write the staged file, then …") stays out.
    re.compile(
        r"\b(?:write|build|assemble|draft)(?:s|ing)?\b[^.;!?]{0,250}?\binline\b[^.;!?]{0,120}?"
        r"\b(?:AskUserQuestion|ask(?:s|ed)?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bin the message that issues\b", re.IGNORECASE),
    re.compile(r"\((?:shown|listed|printed|see|summari[sz]ed) above\)", re.IGNORECASE),
    re.compile(
        r"\b(?:print|show|present|display|emit|state|output)(?:s|ed)?\b[^.;!?]{0,120}?"
        r"\b(?:before|above) (?:the |this )?(?:question|picker|AskUserQuestion|asking)\b",
        re.IGNORECASE,
    ),
)

#: Placement that keeps the content visible: inside the question, or in the final reply after the last tool call.
_PLACEMENT = re.compile(r"preview|question text|in its question|final reply", re.IGNORECASE)

#: How far past a match its own sentence may still name the placement.
_PLACEMENT_WINDOW = 250


def _strip_punctuation(match: re.Match[str]) -> str:
    """Return a code span's content without the punctuation that would end a sentence."""
    return re.sub(r"[.;!?]", "", match.group(1))


def _plain(line: str) -> str:
    """Return ``line`` with inline code spans unwrapped and their dots dropped, so code never ends a sentence early.

    Examples:
        >>> _plain("print `` ! Unknown flag(s): `--x`. `` then invoke `AskUserQuestion` with a `preview`")
        'print   Unknown flag(s): --x  then invoke AskUserQuestion with a preview'
    """
    line = re.sub(r"``(.+?)``", _strip_punctuation, line)
    return re.sub(r"`([^`]+)`", _strip_punctuation, line)


def _ordering_hits(text: str) -> list[str]:
    """Return every ordering phrase in ``text`` whose sentence names no in-question or final-reply placement.

    Examples:
        >>> _ordering_hits("Present findings, then invoke `AskUserQuestion` — (a) Revise · (b) Abort.")
        ['Present findings, then invoke AskUserQuestion']
        >>> _ordering_hits("Show the list, then `AskUserQuestion` with it as the `preview` of every option.")
        []
    """
    hits = []
    for line in text.splitlines():
        plain = _plain(line)
        for pattern in _ORDERING:
            for match in pattern.finditer(plain):
                if not _PLACEMENT.search(plain[match.start() : match.end() + _PLACEMENT_WINDOW]):
                    hits.append(match.group(0))
    return hits


#: Line opening a step: a numbered item (up to one nesting level deep) or a heading.
_STEP_START = re.compile(r"^\s{0,6}(?:\d+[.)]\s+\S|#{2,6}\s)")

#: Fenced code delimiter; lines inside a fence belong to the step around them and never open a step.
_FENCE = re.compile(r"^\s*(?:```|~~~)")

#: Opening line of a step that displays a body of content: a display verb first, then a content noun. The verb must open
#: the step — the same verb anywhere in its line also matched "If not found: print `! …`" guards and spawn steps.
_DISPLAY_STEP = re.compile(
    r"^\s*(?:\d+[.)]\s+|#{2,6}\s+(?:Step\s+[\w.-]+\s*[:—-]\s*)?)?(?:\*\*)?"
    r"(?:output|print|show|present|display|render|emit)s?\b[^\n]{0,120}?\b"
    r"(table|summary|list|plan|queue|spec|report|tree|findings|results|diff|draft|proposal|candidates|arc|section"
    r"|options|hypothes[ie]s|issues|items|changes)",
    re.IGNORECASE,
)

#: Sentence boundary inside a step once its lines are joined.
_SENTENCE_END = re.compile(r"[.;!?](?:\s|$)")


def _steps(text: str) -> list[list[str]]:
    """Split ``text`` into steps: each numbered item or heading with the lines that follow it, fences kept whole.

    Examples:
        >>> [len(step) for step in _steps("intro\\n1. Print table:\\n```\\n2. not a step\\n```\\n2. Ask")]
        [1, 4, 1]
    """
    steps: list[list[str]] = []
    current: list[str] = []
    in_fence = False
    for line in text.splitlines():
        opens_step = not in_fence and bool(_STEP_START.match(line))
        if _FENCE.match(line):
            in_fence = not in_fence
        if opens_step and current:
            steps.append(current)
            current = []
        current.append(line)
    if current:
        steps.append(current)
    return steps


def _names_placement(lines: list[str], noun: str) -> bool:
    """Return whether one sentence of ``lines`` places content named ``noun`` in the question or the final reply.

    Examples:
        >>> _names_placement(["6. `AskUserQuestion`, the queue table as the `preview` of every option."], "queue")
        True
        >>> _names_placement(["Carry task 1's invocation in the question text."], "table")
        False
    """
    # Per line: plugin Markdown never hard-wraps prose, so a line break ends a sentence — joining lines first let a step
    # heading's noun and a later paragraph's placement word read as one sentence.
    sentences = [sentence for line in lines for sentence in _SENTENCE_END.split(_plain(line))]
    # Stem, so "Present candidates" pairs with "the candidate table" and "hypotheses" with "hypothesis".
    noun_pattern = re.compile(rf"\b{re.escape(re.sub(r'e?s$', '', noun.lower()))}", re.IGNORECASE)
    return any(_PLACEMENT.search(sentence) and noun_pattern.search(sentence) for sentence in sentences)


def _cross_step_hits(text: str) -> list[str]:
    """Return each display step followed by a question that carries none of its content, as ``"<step> → <next>"``.

    A display step opens with a display verb and a content noun; the question is an ``AskUserQuestion`` later in that
    step or anywhere in the next one. Text printed there is written before a tool call, so it can vanish as an empty
    progress update while the question is answered blind.

    Examples:
        >>> _cross_step_hits("5. Print queue as table:\\n```\\n1 · a\\n```\\n6. Present gate via `AskUserQuestion`:")
        ['5. Print queue as table: → 6. Present gate via `AskUserQuestion`:']
        >>> _cross_step_hits("5. Print queue as table:\\n6. `AskUserQuestion`, the queue table as every `preview`")
        []
    """
    hits = []
    steps = _steps(text)
    for index, step in enumerate(steps):
        display = _DISPLAY_STEP.match(_plain(step[0]))
        if not display:
            continue
        following = steps[index + 1] if index + 1 < len(steps) else []
        if not any("AskUserQuestion" in _plain(line) for line in step[1:] + following):
            continue
        if _names_placement(step + following, display.group(1)):
            continue
        hits.append(f"{step[0].strip()} → {following[0].strip() if following else '(same step)'}")
    return hits


def _scanned_files() -> list[str]:
    """Plugin instruction files that ask through ``AskUserQuestion``, as POSIX paths under ``plugins/``."""
    files = []
    for path in sorted(PLUGINS_DIR.rglob("*.md")):
        relative = path.relative_to(PLUGINS_DIR).as_posix()
        if "/tests/" in f"/{relative}" or path.name == "CHANGELOG.md" or relative.startswith(_EXCLUDED_PREFIXES):
            continue
        if "AskUserQuestion" in path.read_text(encoding="utf-8"):
            files.append(relative)
    return files


_FILES = _scanned_files()


def test_scan_covers_the_question_heavy_skills() -> None:
    """The scan reaches the skills whose pre-question text this guard was written for — never an empty pass."""
    expected = {
        "cc_develop/skills/fix/SKILL.md",
        "cc_foundry/skills/brainstorm/modes/breakdown.md",
        "cc_research/skills/kaggle/SKILL.md",
        "cc_research/skills/run/modes/team.md",
        "cc_research/skills/_shared/unsupported-flag-protocol.md",
    }

    assert expected <= set(_FILES)


@pytest.mark.parametrize("relative", _FILES)
def test_decision_content_is_carried_by_the_question(relative: str) -> None:
    """No instruction prints decision content as reply text and only then asks about it.

    Each hit is a sentence that would show the user a question whose substance may have vanished as an empty progress
    update; move the content into the question text or every option's ``preview``, or into the final reply when no tool
    call follows.
    """
    hits = _ordering_hits((PLUGINS_DIR / relative).read_text(encoding="utf-8"))

    assert hits == [], f"{relative} prints decision content before the question: {hits}"


@pytest.mark.parametrize("relative", _FILES)
def test_no_step_displays_content_for_a_later_question(relative: str) -> None:
    """No step prints a body of content that the next question then asks about without carrying it.

    The per-line phrases above miss this split: "5. Print queue as formatted table:" with the gate in step 6 put a
    hypothesis queue launching paid implementation agents in front of a question that showed none of it. Carry the
    content as every option's ``preview`` (or the question text), or deliver it in the final reply when nothing is
    asked.
    """
    hits = _cross_step_hits((PLUGINS_DIR / relative).read_text(encoding="utf-8"))

    assert hits == [], f"{relative} displays content in a step before the question that asks about it: {hits}"


@pytest.mark.parametrize(
    "sentence",
    [
        pytest.param("Present findings, then invoke `AskUserQuestion` — (a) Revise.", id="present-then-invoke"),
        pytest.param(
            "Found: print `` ! Unknown flag(s): `--x`. `` then invoke `AskUserQuestion`.", id="print-code-then"
        ),
        pytest.param(
            '- question: "Plan ready. Task 1 requires manual invocation (shown above). What next?"', id="above"
        ),
        pytest.param("Print (annotated) proposal table. Then call `AskUserQuestion` tool.", id="print-dot-then-call"),
        pytest.param("put the Sources block in the message that issues Step 3d's picker", id="message-that-issues"),
        pytest.param("print as plain text before the question: a tip", id="print-before-question"),
        pytest.param(
            "| `unauthorized` | Print the credential instructions below, `AskUserQuestion`: (a) skip · (b) user sets up"
            " token, then re-probe |",
            id="print-comma-tool",
        ),
        pytest.param(
            "**Arc approval + voice** (single AskUserQuestion call): show proposed arc, then ask voice choice — option"
            " (d) redirects to arc adjustment.",
            id="show-then-ask",
        ),
        pytest.param(
            'Collect ambiguous hits, invoke `AskUserQuestion` — show file + 5-line context per hit, ask: "Is this a'
            ' real reference?"',
            id="show-comma-ask",
        ),
        pytest.param(
            "Found → surface existing task to user, ask whether to re-generate plan or skip.", id="surface-comma-ask"
        ),
        pytest.param(
            "Ambiguous (2+ equally close): list them, `AskUserQuestion` to disambiguate.", id="list-comma-tool"
        ),
        pytest.param(
            "Write all 6 sections inline, then invoke a single `AskUserQuestion` for the full spec:", id="write-inline"
        ),
        pytest.param("Output the plan table, then call `AskUserQuestion` with (a) start · (b) stop.", id="output-then"),
    ],
)
def test_ordering_phrases_are_detected(sentence: str) -> None:
    """Each ordering form this guard exists for is detected, so a regression in the patterns cannot pass silently."""
    assert _ordering_hits(sentence)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "5. Print queue as formatted table:\n\n   ```text\n   1 · hypothesis\n   ```\n\n"
            "Before user gate, update `state.json`.\n\n6. Present user gate via `AskUserQuestion`:\n",
            id="numbered-display-then-numbered-gate",
        ),
        pytest.param(
            "3. Output ordered task table:\n\n```markdown\n| # | Task |\n```\n\n#### Step B3: Post-plan prompt\n\n"
            "Carry task 1's invocation in the question text. Call `AskUserQuestion`: (a) Start task 1 now.\n",
            id="display-then-heading-whose-placement-is-for-other-content",
        ),
        pytest.param(
            "4. Show the findings table:\n\n```\n| a |\n```\n\nThen call `AskUserQuestion`: (a) fix · (b) skip\n",
            id="display-and-ask-in-one-step",
        ),
        pytest.param(
            "#### Step 2: Present candidate list\n\nOne line per candidate.\n\n#### Step 3: Pick\n\n"
            "`AskUserQuestion` — which candidate?\n",
            id="heading-display-then-heading-ask",
        ),
    ],
)
def test_cross_step_ordering_is_detected(text: str) -> None:
    """A display step followed by a question that does not carry its content is detected across lines and steps."""
    assert _cross_step_hits(text)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "5. Print report header table, as the `preview` of every option of the follow-up question, never also as"
            " reply text.\n6. Follow-up: `AskUserQuestion` (a) `/develop:fix` · (b) skip\n",
            id="report-then-follow-up-preview-in-display-step",
        ),
        pytest.param(
            "5. Print queue as formatted table:\n\n```text\n1 · a\n```\n\n6. Present user gate via `AskUserQuestion`, the"
            " queue table from step 5 as the `preview` of every option:\n",
            id="placement-named-in-the-asking-step",
        ),
        pytest.param(
            "5. Print the final summary table as the final reply, after the last tool call.\n"
            "6. `AskUserQuestion` only when a required decision is missing.\n",
            id="final-reply-delivery",
        ),
        pytest.param(
            "4. Print the findings table.\n5. Write the report file.\n6. `AskUserQuestion` — (a) fix · (b) skip\n",
            id="question-two-steps-later-is-out-of-scope",
        ),
        pytest.param(
            "3. Build ordered task table — shown only as the `preview` of every option of Step B3's question.\n\n"
            "#### Step B3: Post-plan prompt\n\nCall `AskUserQuestion`.\n",
            id="non-display-verb",
        ),
    ],
)
def test_cross_step_placements_pass(text: str) -> None:
    """Steps whose content the question carries, or that deliver it as the final reply, are not flagged."""
    assert _cross_step_hits(text) == []


@pytest.mark.parametrize(
    "sentence",
    [
        pytest.param(
            "STOP. Invoke `AskUserQuestion` with the findings as the `preview` of every option, not as reply text.",
            id="preview-placement",
        ),
        pytest.param("Found → invoke `AskUserQuestion` with question text `` ! Unknown flag(s): `--x`. ``.", id="text"),
        pytest.param(
            "Update the plan first, then present findings as the final reply, after the last tool call.", id="final"
        ),
        pytest.param(
            "| `unauthorized` | `AskUserQuestion` with the credential instructions below as the `preview` of both"
            " options: (a) skip · (b) user sets up token, then re-probe |",
            id="tool-with-preview",
        ),
        pytest.param(
            "Collect ambiguous hits, invoke `AskUserQuestion` — one question per hit, its file:line + 5-line context as"
            ' the `preview` of both options (real reference · false positive), question text: "Is this a real'
            ' reference?"',
            id="context-as-preview",
        ),
    ],
)
def test_in_question_placements_pass(sentence: str) -> None:
    """Sentences that carry the content in the question or the final reply are not flagged."""
    assert _ordering_hits(sentence) == []
