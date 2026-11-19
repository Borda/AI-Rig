"""Keep foundry decisions readable before choosing and preserve report follow-up delivery after the answer.

Each test reads skill Markdown as text and checks single-line phrases, so reflowing a paragraph never breaks an
assertion.
"""

import hashlib
import re
from pathlib import Path

import pytest
from _markdown_text import found, missing

_SKILLS = Path(__file__).resolve().parents[2] / "skills"

#: Rendering limits every plain-question gate states: size caps and the ban on tables, fences and previews.
_QUESTION_LIMITS = (
    "6 plain lines",
    "600 characters",
    "1,500",
    "no table",
    "no pipe layout",
    "no code fence",
    "no `preview`",
)
#: Delivery wording of a saved-and-printed gate: printed once as normal Markdown, last reply text before the question.
_PRINT_ONCE = ("printed once", "normal Markdown", "last reply text before")
#: Wording that told the model to route rich content through option previews before the plain-question rewrite.
_OBSOLETE_PREVIEW_ROUTING = (
    "`preview` of every",
    "`preview` of both",
    "every option's `preview`",
    "option previews",
    "Preview cap",
)


def _question_rule(text: str, section: str) -> str:
    """Resolve the named shared rule when a gate references it instead of repeating its limits."""
    if "§Tree summary format — Question text rule" not in section:
        return section
    if "### Tree summary format" not in text.splitlines():
        text = (_SKILLS / "brainstorm/SKILL.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line == "### Tree summary format")
    end = next(index for index in range(start + 1, len(lines)) if lines[index].startswith("### "))
    return next(line for line in lines[start:end] if line.startswith("**Question text rule**:"))


@pytest.mark.parametrize(
    ("relative", "start", "end", "artifact"),
    [
        pytest.param(
            "brainstorm/SKILL.md",
            "3. Call a single `AskUserQuestion`",
            "### Seeding the tree",
            ".reports/brainstorm/<ts>/<slug>-branches.md",
            id="brainstorm-seeding-reaction",
        ),
        pytest.param(
            "brainstorm/SKILL.md",
            "### Tree summary format",
            "### Loop bounds",
            ".reports/brainstorm/<ts>/<slug>-tree.md",
            id="brainstorm-tree-and-seeding",
        ),
        pytest.param(
            "brainstorm/SKILL.md",
            "- After limit:",
            "- **Gate**",
            ".reports/brainstorm/<ts>/<slug>-tree.md",
            id="brainstorm-operation-limit",
        ),
        pytest.param(
            "brainstorm/SKILL.md",
            "## Step 6: Present and gate",
            "**Gate**: do not exit",
            "tree file",
            id="brainstorm-final-tree",
        ),
        pytest.param(
            "brainstorm/modes/breakdown.md",
            "#### Step D1",
            "#### Step D2",
            ".reports/brainstorm/<ts>/<slug>-tree.md",
            id="brainstorm-distillation-tree",
        ),
        pytest.param(
            "brainstorm/modes/breakdown.md",
            "#### Step D3",
            "**Sections**:",
            ".reports/brainstorm/<ts>/<slug>-spec-draft.md",
            id="brainstorm-spec",
        ),
        pytest.param(
            "brainstorm/modes/breakdown.md",
            "#### Step B3",
            "End with",
            ".reports/brainstorm/<ts>/<slug>-plan.md",
            id="brainstorm-plan-both-invocation-variants",
        ),
        pytest.param(
            "create/SKILL.md",
            "**Arc approval + voice**",
            "## Step 4",
            ".temp/create/<slug>-arc.md",
            id="create-arc-and-revision",
        ),
        pytest.param(
            "calibrate/SKILL.md",
            "Build combined benchmark report",
            "## Step 4",
            ".reports/calibrate/<TIMESTAMP>/benchmark-summary.md",
            id="calibrate-benchmark-and-proposals",
        ),
        pytest.param(
            "session/SKILL.md",
            "Build the Unlanded table",
            "When a `session-restore.js`",
            ".temp/session/unlanded.md",
            id="session-unlanded",
        ),
        pytest.param(
            "setup/SKILL.md",
            "Otherwise the report lists",
            "On (a), re-invoke",
            ".temp/setup/cache-purge.md",
            id="setup-cache-purge",
        ),
        pytest.param(
            "distill/modes/executables.md",
            "## Step E3",
            "## Step E4",
            "$RUN_DIR/candidates.md",
            id="distill-executable-candidates",
        ),
        pytest.param(
            "distill/modes/external.md", "**E13:", "**E14:", "$EXT_RUN_DIR/adoption.md", id="distill-external-adoption"
        ),
        pytest.param(
            "distill/modes/memory.md",
            "Call `AskUserQuestion` tool with the (annotated) proposal table",
            "## Step L5",
            "$RUN_DIR/proposals.md",
            id="distill-memory-proposals",
        ),
        pytest.param(
            "distill/modes/prune.md",
            "**P-eager-2**",
            "**P-eager-3**",
            ".temp/distill/prune/scored.md",
            id="distill-eager-scored-items",
        ),
        pytest.param(
            "distill/modes/prune.md",
            "**P2**",
            "Only after user picks",
            ".temp/distill/prune/proposals.md",
            id="distill-prune-proposals",
        ),
    ],
)
def test_rich_decision_content_is_saved_printed_once_and_summarized(
    relative: str, start: str, end: str, artifact: str
) -> None:
    """Each gate saves its rich content, prints it once before asking, and keeps a bounded plain question summary.

    Each section must name its own artifact and delivery. Limits and option placement must be stated locally or resolved
    through its explicit reference to the named shared rule.
    """
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    section = text.split(start, 1)[1].split(end, 1)[0]
    assert missing(section, [artifact, *_PRINT_ONCE, "question text"]) == []
    rule = _question_rule(text, section)
    assert missing(rule, _QUESTION_LIMITS) == []
    assert "recommended option second" in rule or "display recommended (a) second" in rule


#: Bold labels every distill report carries, in the order the rule lists them; the findings label names its form.
_DISTILL_REPORT_LABELS = ("**Summary**", "**Recommendations**", "**Review verdict**", "**Open decisions**")
#: Order wording: printed as the last reply text in the question's own response, never replaced by the saved file.
_DISTILL_REPORT_ORDER = (
    "SKILL.md §Report-first gate",
    "last reply text before",
    "no tool call between",
    "never replaces the print",
)
#: Wording that would move the report behind the answer, the order report-header follow-ups use and distill must not.
_DISTILL_AFTER_ANSWER = ("final reply after the answer", "after the question", "after the answer")
#: Findings labels; a report names exactly one, matching whether it renders a table or a per-item text list.
_FINDINGS_LABELS = ("**Findings table**", "**Findings list**")
#: The only question sites allowed without their own printed report, as named in the distill skill rule.
_DISTILL_EXCLUSIONS = ("`--project` picker", "per-item approval after printed diff", "follow-up within printed gate")
_DISTILL_FILES = (
    "distill/SKILL.md",
    "distill/modes/executables.md",
    "distill/modes/external.md",
    "distill/modes/memory.md",
    "distill/modes/prune.md",
)
_RULES = Path(__file__).resolve().parents[2] / "rules"
_OPTION = re.compile(r"^- \(([a-z])\) label:")
_QUESTION_LINE = re.compile(r'^- question: "[^"]*"')
#: Sentence end: terminal punctuation, optional closing quotes or brackets, then whitespace or the end of the line.
_SENTENCE_END = re.compile(r"[.!?…][\"”’)\]]*(?=\s|$)")
#: New phrasings of a plain-text ask, none of them using the question tool; each must fail in every ask region.
_PLAIN_TEXT_ASKS = (
    "Pause for user input before continuing.",
    "Prompt the user for item numbers.",
    "Seek the maintainer's OK before deleting.",
    "“Keep these files?”",
    "Halt until the maintainer types a number.",
    "Get a thumbs-up from the user before writing.",
    "Hold here for the user's verdict on each draft.",
    "Request a threshold from the user, then extract.",
    "Do not continue until the user says go.",
    "Find out from the human which clusters matter.",
    "Let them weigh in on Group B.",
    "Collect a sign-off from the owner, then apply.",
    "Check with the operator whether drafts may be written.",
    "Stay idle until someone replies with a list.",
    "The user names the files to drop; continue afterwards.",
    "Expect a number from the reviewer before going on.",
    "Gather preferences from the team before extracting.",
    "Await instructions.",
    "Should the scored table be trimmed further?",
    "Type `y` to continue, anything else to stop.",
    "Leave the call on threshold to whoever runs the skill.",
    "Only proceed once a human has looked at the diff and nodded.",
)

#: Fingerprints of reviewed statement sentences inside ask regions (``_fingerprint``). Each was read and judged
#: not to ask the user anything; add one only after the same review, never to silence a failure.
_REVIEWED_STATEMENTS: frozenset[str] = frozenset(
    {
        "06eb764f6244c999",
        "077ba21506f29bf3",
        "095d02300c6cd5ad",
        "0be938286351604f",
        "0cb9bbd5b6f8a7e5",
        "125bf1bb622a2646",
        "15ca7853d427f3de",
        "18e5abc7ea69ebf6",
        "192578b689e715f9",
        "1a6f784431b8e5bd",
        "1bb9e473ccbad02c",
        "23f663630e2f2825",
        "24895e79ed573bb5",
        "24e01425483e3512",
        "251476ec0820d5a9",
        "267c23695b6e8fc2",
        "27ec62c9c58f84eb",
        "28f8dfd84f5d9e36",
        "2a88e39ec5003782",
        "2ce4ee99e712ca81",
        "2ff9344da07a378f",
        "31408564e45860fd",
        "35539e5c084a457c",
        "35896e5f0cca24bc",
        "3704af166335f252",
        "3ee4f79c15916a69",
        "49f488f6d90756f5",
        "4e2136624c5c89a2",
        "511a742f30a20896",
        "5b374263265dc7cf",
        "5c1b66620bd7cc39",
        "5c63ef3cb15eea64",
        "64011d11804cff77",
        "6af4eae3dd1787ec",
        "6b2e537f5f19cdec",
        "6ba58147f51567fc",
        "6bc54f13bea593dc",
        "6e9ba6b5a0b41b65",
        "6eb66d360cf8de2b",
        "7195f23f423b0b96",
        "78b56f0960013932",
        "78bc5266f60515ba",
        "79334a65ccdfd8a0",
        "7ac2698bad2d4789",
        "7dc3b6d0f5e3efe1",
        "82eaf0a4cb446c0f",
        "845942f26235406c",
        "86ed19285e662f88",
        "892cc29dd9c34c4d",
        "894a34c66476e19b",
        "8a1b2bf7b94e42b3",
        "8c1336b60c4ff75c",
        "8d73cf4b7b180920",
        "8e8970efc5b5d60a",
        "937af42de002337b",
        "942361ba525fef22",
        "96ac230e0b0ddb3c",
        "9ae634a6fa861b90",
        "b1169feab5940a55",
        "b1213378d9ba16e1",
        "b283b240c60e2217",
        "b39e12a79eeb4d62",
        "b66958bad75513e0",
        "b8832f72a899e8fd",
        "b95ccdf1d402b17b",
        "bb0b471ba9afbe2d",
        "bd496f140d542b96",
        "bdfdf0ae22571349",
        "becb988ff134842e",
        "bf40762b90fd79d8",
        "c3f4f3e3288e3813",
        "c9240e8d4abccbb6",
        "c977dbe785221d34",
        "ca06b7b873aa5406",
        "cc52fe9872b8177f",
        "cce92802860ab1ce",
        "cef067fb05537e5b",
        "d07f9e4d52c222de",
        "d5abdf9109d9e401",
        "de1d4db10d7f4916",
        "e45d298d580adc03",
        "e727d28a81f12439",
        "e79218435301fd62",
        "eed29eb84a2a4621",
        "f12651c097e9c3b1",
        "fb9e124b5f3cb796",
        "fbce57795d329f60",
        "fec460b8a8d59bad",
        "ffdb8dc79f93bb28",
    }
)

#: Every distill report gate, with its findings label.
_DISTILL_GATES = [
    pytest.param(
        "distill/modes/executables.md", "## Step E3", "## Step E4", "**Findings table**", id="executables-candidates"
    ),
    pytest.param(
        "distill/modes/executables.md",
        "**Adversarial Convergence Loop**",
        "**Commit grouping**",
        "**Findings table**",
        id="executables-convergence-stop",
    ),
    pytest.param("distill/modes/external.md", "**E13:", "**E14:", "**Findings table**", id="external-adoption"),
    pytest.param(
        "distill/modes/memory.md",
        "**L4 gate report**",
        "## Step L5",
        "**Findings table**",
        id="memory-proposals",
    ),
    pytest.param(
        "distill/modes/prune.md", "**P-eager-2**", "**P-eager-3**", "**Findings table**", id="prune-eager-scored"
    ),
    pytest.param(
        "distill/modes/prune.md", "**P2**", "Only after user picks", "**Findings list**", id="prune-proposals"
    ),
]
#: Zero-result runs: analysis found nothing to propose, so the report prints without a question.
_DISTILL_ZERO_RESULTS = [
    pytest.param("distill/modes/executables.md", "No qualifying clusters found:", id="executables-no-candidates"),
    pytest.param("distill/modes/memory.md", "**Scope check**:", id="memory-no-lessons"),
    pytest.param("distill/modes/memory.md", "**L4 zero-result**", id="memory-nothing-to-apply"),
    pytest.param("distill/modes/prune.md", "**P-eager-2 zero-result**", id="prune-eager-all-keep"),
    pytest.param("distill/modes/prune.md", "**P2 zero-result**", id="prune-nothing-to-drop"),
    pytest.param("distill/modes/external.md", "**E13 zero-result**", id="external-nothing-to-port"),
]
#: Regions where distill reports and asks: each gate, each zero-result line and the default Step 5 report.
_ASK_REGIONS = [
    *((param.values[0], param.values[1], param.values[2]) for param in _DISTILL_GATES),
    *((param.values[0], param.values[1], "\n") for param in _DISTILL_ZERO_RESULTS),
    ("distill/SKILL.md", "## Step 5: Report", "## Mode: Memory Pruning — only when first token is `prune`"),
]


def _section(relative: str, start: str, end: str) -> str:
    """Return the text of ``relative`` between the first ``start`` marker and the next ``end`` marker."""
    return (_SKILLS / relative).read_text(encoding="utf-8").split(start, 1)[1].split(end, 1)[0]


def _instruction_lines(relative: str, text: str | None = None) -> list[tuple[int, str]]:
    """Return numbered instruction lines of a distill file, without frontmatter, fences, inputs or the skill rule.

    The rule and the inputs list describe questions to state the contract; no question is asked there.
    """
    if text is None:
        text = (_SKILLS / relative).read_text(encoding="utf-8")
    lines = text.splitlines()
    kept, in_fence, skipped = [], False, False
    body_start = lines.index("---", 1) + 1 if lines and lines[0] == "---" else 0
    for number, line in enumerate(lines[body_start:], start=body_start + 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        opens = line == "<inputs>" or line.startswith("**Report-first gate — every mode**")
        skipped = (skipped or opens) and not (line == "</inputs>" or line.startswith("<!--"))
        if not in_fence and not skipped:
            kept.append((number, line))
    return kept


def _sentences(line: str) -> list[str]:
    """Split ``line`` into sentences; code spans are masked so a dot inside a path never ends one."""
    masked = re.sub(r"`[^`]*`", lambda span: "x" * len(span.group(0)), line)
    pieces, begin = [], 0
    for match in _SENTENCE_END.finditer(masked):
        pieces.append(line[begin : match.end()].strip())
        begin = match.end()
    pieces.append(line[begin:].strip())
    return [piece for piece in pieces if piece]


def _fingerprint(sentence: str) -> str:
    """Return the short, whitespace-insensitive digest under which a reviewed statement sentence is recorded."""
    return hashlib.sha256(" ".join(sentence.split()).encode("utf-8")).hexdigest()[:16]


def _is_tagged(sentence: str) -> bool:
    """Return whether ``sentence`` carries one of the exclusion tags the skill rule lists."""
    return any(f"§Report-first gate exclusion: {name}" in sentence for name in _DISTILL_EXCLUSIONS)


def _region_lines(relative: str, text: str) -> list[tuple[int, str]]:
    """Return the instruction lines of ``text`` that fall inside one of the file's ask regions."""
    line_starts = [0]
    for line in text.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))
    spans = []
    for region_file, start, end in _ASK_REGIONS:
        if region_file == relative and start in text:
            begin = text.index(start)
            spans.append((begin, text.index(end, begin + len(start))))
    return [
        (number, line)
        for number, line in _instruction_lines(relative, text)
        if any(begin <= line_starts[number - 1] < stop for begin, stop in spans)
    ]


#: Each mode's single list of open-item classes: definition marker, required classes, and the report lines (zero-result
#: and gate) whose **Open decisions** must name the list.
_OPEN_ITEM_LISTS = [
    pytest.param(
        "distill/modes/prune.md",
        "**Prune open items**",
        ("manual-review items", "timed-out projects"),
        ("**P-eager-2 zero-result**", "**P2 zero-result**", "**P-eager-2**: Write the gate report", "**P2**: Build"),
        id="prune",
    ),
    pytest.param(
        "distill/modes/executables.md",
        "**Executables open items**",
        ("held back", "timed-out scan units"),
        (
            "No qualifying clusters found:",
            "Write the gate report (SKILL.md §Report-first gate) to `$RUN_DIR/candidates.md`",
        ),
        id="executables",
    ),
    pytest.param(
        "distill/modes/memory.md",
        "**Memory open items**",
        ("⚠ conflicts", "⚠ CONTRADICTION pairs", "unclassified files", "unresolved targets"),
        ("**Scope check**:", "**L4 zero-result**", "**L4 gate report**"),
        id="memory",
    ),
    pytest.param(
        "distill/modes/external.md",
        "**External open items**",
        ("Discuss rows", "`NEEDS_MORE_INFO` rows"),
        ("**E13 zero-result**", "- **Open decisions** —"),
        id="external",
    ),
]


def _next_content_line(lines: list[str], index: int) -> str:
    """Return the first non-blank line after ``lines[index]``, or an empty string at the end of the file."""
    return next((line for line in lines[index + 1 :] if line.strip()), "")


def _unrecorded_sentences(relative: str, text: str) -> list[str]:
    """Return ask-region sentences that are neither a legal ask form nor a reviewed, recorded statement.

    Legal ask forms: a sentence naming ``AskUserQuestion``, a sentence carrying an exclusion tag, a ``- (x) label:``
    option line, and the quoted question of a ``- question:`` line whose next non-blank line is an option. Bold-only
    lines are judged as sentences too. Every other sentence must match a recorded fingerprint.
    """
    all_lines = text.splitlines()
    unrecorded = []
    for number, line in _region_lines(relative, text):
        if _OPTION.match(line):
            continue
        question = _QUESTION_LINE.match(line)
        if question and not _OPTION.match(_next_content_line(all_lines, number - 1)):
            unrecorded.append(f"{relative}:{number} question without options: {line[:80]}")
            continue
        for sentence in _sentences(line[question.end() :] if question else line):
            if "AskUserQuestion" in sentence or _is_tagged(sentence):
                continue
            if _fingerprint(sentence) not in _REVIEWED_STATEMENTS:
                unrecorded.append(f"{relative}:{number} {_fingerprint(sentence)} {sentence[:80]}")
    return unrecorded


def _question_summary_ok(sentence: str) -> bool:
    """Return whether ``sentence`` gives its question text counts, the top recommendation and the report path."""
    masked = re.sub(r"`[^`]*`", lambda span: "CODE→" if "→" in span.group(0) else "CODE", sentence)
    clauses = [re.split(r";|\.\s|\.$", tail)[0] for tail in masked.split("question text carries")[1:]]
    return any("count" in c and "one-line top recommendation" in c and "CODE→" in c for c in clauses)


def _label_positions(text: str) -> list[int]:
    """Return where the five report parts first appear in ``text``, the findings part under either label."""
    findings = min((text.find(label) for label in _FINDINGS_LABELS if label in text), default=-1)
    parts = ["**Summary**", "**Recommendations**", None, "**Review verdict**", "**Open decisions**"]
    return [findings if part is None else text.find(part) for part in parts]


def _gate_body(section: str) -> str:
    """Return a gate section without its zero-result line and bold-only headings, which hold no gate ask."""
    zero_results = tuple(param.values[1] for param in _DISTILL_ZERO_RESULTS)
    return "\n".join(
        line
        for line in section.splitlines()
        if not line.startswith(zero_results) and not re.fullmatch(r"\*\*[^*]+\*\*", line.strip())
    )


def _ask_precedes_open_decisions(section: str) -> bool:
    """Return whether the gate's first ``AskUserQuestion`` sentence comes before its **Open decisions** label."""
    body = _gate_body(section)
    return body.find("AskUserQuestion") < body.find("**Open decisions**")


def _inline_option_overflow(relative: str, text: str) -> list[str]:
    """Return tool or tagged ask sentences that list more than four inline options after the word ``options``."""
    overflow = []
    for number, line in _instruction_lines(relative, text):
        for sentence in _sentences(line):
            if ("AskUserQuestion" in sentence or _is_tagged(sentence)) and " options " in sentence:
                listed = re.findall(r"`[^`]+`", sentence.split(" options ", 1)[1])
                if len(listed) > 4:
                    overflow.append(f"{relative}:{number}")
    return overflow


def _label_overflow(relative: str, text: str) -> list[str]:
    """Return option lines that are the fifth ``- (x) label:`` of one question."""
    oversized, count = [], 0
    for number, line in _instruction_lines(relative, text):
        option = _OPTION.match(line)
        if line.startswith("- question:") or (option and option.group(1) == "a"):
            count = 0
        if option:
            count += 1
            if count == 5:
                oversized.append(f"{relative}:{number}")
    return oversized


def _open_decisions_of(text: str, site: str) -> str:
    """Return the **Open decisions** value on the first line starting with ``site``."""
    line = next(line for line in text.splitlines() if line.startswith(site))
    return line.split("**Open decisions**", 1)[1]


@pytest.mark.parametrize(("relative", "start", "end", "findings"), _DISTILL_GATES)
def test_distill_gate_prints_labelled_report_before_asking(relative: str, start: str, end: str, findings: str) -> None:
    """Each distill report gate names its five parts in gate order and asks only after its **Open decisions** label.

    Checked: the five labels, their order, the print-before-call wording, the absence of after-answer wording, and the
    gate's first ``AskUserQuestion`` sentence placed after **Open decisions** (zero-result lines and headings ignored).
    """
    section = _section(relative, start, end)
    assert missing(section, [*_DISTILL_REPORT_LABELS, findings, *_DISTILL_REPORT_ORDER]) == []
    assert found(section, _DISTILL_AFTER_ANSWER) == []
    positions = _label_positions(section)
    assert positions == sorted(positions), positions
    assert not _ask_precedes_open_decisions(start + section)


def test_ask_moved_before_the_report_fails_the_order_check() -> None:
    """An ``AskUserQuestion`` sentence inserted at the top of a gate, before its report, is caught."""
    section = "## Step E3" + _section("distill/modes/executables.md", "## Step E3", "## Step E4")
    early = "Call `AskUserQuestion` before writing anything; its question text carries counts.\n"
    assert _ask_precedes_open_decisions(early + section)
    assert not _ask_precedes_open_decisions(section)


@pytest.mark.parametrize(("relative", "start", "end", "findings"), _DISTILL_GATES)
def test_distill_findings_label_names_its_rendered_form(relative: str, start: str, end: str, findings: str) -> None:
    """A gate labels its findings by what it renders, so a text list is never announced as a table."""
    section = _section(relative, start, end)
    assert found(section, [label for label in _FINDINGS_LABELS if label != findings]) == []


@pytest.mark.parametrize(("relative", "start", "end", "findings"), _DISTILL_GATES)
def test_distill_gate_menu_offers_only_actionable_options(relative: str, start: str, end: str, findings: str) -> None:
    """Every gate with an option menu states an actionability rule and lists ``Skip`` in every menu.

    Checked: one of the actionability phrases is present and each ``- question:`` block with options includes ``Skip``.
    Whether each stated condition matches the mode's candidate sets is checked per mode by the tests below.
    """
    section = _section(relative, start, end)
    if "- (a) label:" not in section:
        return
    assert found(section, ["Offer only actionable options", "Options always actionable", "offered only when"]) != []
    menus = [block for block in section.split("- question:")[1:] if "- (a) label:" in block]
    assert all("label: `Skip`" in menu for menu in menus)


def test_external_gate_keys_every_decision_on_one_actionable_set() -> None:
    """The external zero-result exit, menu and apply step all use the actionable set, never Group A membership.

    The set excludes Discuss and Skip rows and rows the challenger marked ``DISCARD`` or ``NEEDS_MORE_INFO``.
    """
    text = (_SKILLS / "distill/modes/external.md").read_text(encoding="utf-8")
    definition = next(line for line in text.splitlines() if line.startswith("**Actionable set**"))
    assert (
        missing(definition, ["Adopt or Tweak", "`DISCARD`", "`NEEDS_MORE_INFO`", "never on Group A membership"]) == []
    )
    assert "actionable set empty" in next(line for line in text.splitlines() if line.startswith("**E13 zero-result**"))
    assert "only when the actionable set is non-empty" in text
    assert "apply actionable-set targets only" in text
    assert found(text, ["confirmed Group A", "only when Group A is non-empty", "— Group A empty"]) == []


def test_executables_threshold_follow_up_has_a_way_out() -> None:
    """The eager threshold follow-up offers ``Skip`` beside its thresholds, so no user must pick a threshold."""
    section = _section("distill/modes/executables.md", '- question: "Threshold for bin/ extraction?"', "## Step E4")
    assert "label: `Skip`" in section


@pytest.mark.parametrize("relative", _DISTILL_FILES)
def test_every_distill_question_carries_its_own_summary(relative: str) -> None:
    """Every ask sentence, a tagged follow-up included, gives its question text counts, top recommendation and path.

    Text printed before a tool call can arrive blank on some hosts; every question then still carries the decision.
    Scope pickers and per-item approvals after a printed diff ask about that scope or item, not about a report.
    """
    unsummarised = []
    for number, line in _instruction_lines(relative):
        if re.fullmatch(r"\*\*[^*]+\*\*", line.strip()):
            continue
        for sentence in _sentences(line):
            asks = (
                "AskUserQuestion" in sentence
                or "§Report-first gate exclusion: follow-up within printed gate" in sentence
            )
            exempt = any(f"§Report-first gate exclusion: {name}" in sentence for name in _DISTILL_EXCLUSIONS[:2])
            if asks and not exempt and not _question_summary_ok(sentence):
                unsummarised.append(f"{relative}:{number} {sentence[:80]}")
    assert unsummarised == []


@pytest.mark.parametrize("relative", _DISTILL_FILES)
def test_every_distill_ask_uses_a_legal_form(relative: str) -> None:
    """Inside every ask region each sentence is a legal ask form or a reviewed statement; outside, tool asks are tagged.

    Checked inside ask regions (each gate, each zero-result line, the default Step 5 report): every sentence, bold-only
    lines included, names ``AskUserQuestion``, carries an exclusion tag, is an option line, is the quoted question of a
    ``- question:`` line followed by its options, or matches a reviewed fingerprint; any other added or changed sentence
    fails whatever its wording. Checked outside them: every ``AskUserQuestion`` sentence carries an exclusion tag.
    Declared limit, accepted: plain-text asks outside ask regions, words added inside a sentence that already names the
    tool or a tag, option descriptions and fenced code are not inspected. A recorded fingerprint is trusted, so adding
    one needs the same review as the text.
    """
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    assert _unrecorded_sentences(relative, text) == []
    region = {number for number, _ in _region_lines(relative, text)}
    untagged = [
        f"{relative}:{number}"
        for number, line in _instruction_lines(relative, text)
        if number not in region and not re.fullmatch(r"\*\*[^*]+\*\*", line.strip())
        for sentence in _sentences(line)
        if "AskUserQuestion" in sentence and not _is_tagged(sentence)
    ]
    assert untagged == []


@pytest.mark.parametrize("phrase", _PLAIN_TEXT_ASKS)
@pytest.mark.parametrize("relative", _DISTILL_FILES)
def test_plain_text_ask_fails_in_every_region_until_tagged(relative: str, phrase: str) -> None:
    """Each plain-text ask inserted into a region (plain or bold) or appended to a region line fails; tagged, it passes.

    Appending covers a second ask added after an already tagged or tool-naming sentence.
    """
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    region = dict(_region_lines(relative, text))
    assert region
    tag = "(§Report-first gate exclusion: per-item approval after printed diff)"
    for number, line in region.items():
        inside_options = _QUESTION_LINE.match(line) or _OPTION.match(lines[number] if number < len(lines) else "")
        if number + 1 in region and not inside_options:
            for variant in (phrase, f"**{phrase}**"):
                inserted = "".join([*lines[:number], f"{variant}\n", *lines[number:]])
                assert _unrecorded_sentences(relative, inserted) != [], (number, variant)
            tagged = "".join([*lines[:number], f"{tag} {phrase}\n", *lines[number:]])
            assert _unrecorded_sentences(relative, tagged) == [], number
        if _OPTION.match(line) or re.fullmatch(r"\*\*[^*]+\*\*", line.strip()):
            continue
        appended = "".join([*lines[: number - 1], f"{line.rstrip()} {phrase}\n", *lines[number:]])
        assert _unrecorded_sentences(relative, appended) != [], number


@pytest.mark.parametrize("relative", [rel for rel in _DISTILL_FILES if rel != "distill/SKILL.md"])
def test_question_line_without_its_options_fails(relative: str) -> None:
    """A bare ``- question:`` line in a gate, with no option lines after it, is not a legal ask; with options it is."""
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    start = next(param.values[1] for param in _DISTILL_GATES if param.values[0] == relative)
    first = next(index for index, line in enumerate(lines) if line.startswith(start)) + 1
    bare = "".join([*lines[:first], '- question: "Which one should win? Reply in chat."\n\n', *lines[first:]])
    legal = "".join(
        [*lines[:first], '- question: "Which one should win?"\n- (a) label: `Skip` — x\n\n', *lines[first:]]
    )
    assert any("question without options" in item for item in _unrecorded_sentences(relative, bare))
    assert _unrecorded_sentences(relative, legal) == []


@pytest.mark.parametrize("relative", _DISTILL_FILES)
def test_every_tagged_distill_ask_goes_through_the_question_tool(relative: str) -> None:
    """A tagged ask (picker, per-item approval, follow-up) names ``AskUserQuestion``, never a plain-text prompt."""
    plain = [
        f"{relative}:{number}"
        for number, line in _instruction_lines(relative)
        for sentence in _sentences(line)
        if _is_tagged(sentence) and "AskUserQuestion" not in sentence
    ]
    assert plain == []


def test_no_distill_option_asks_for_a_chat_reply() -> None:
    """Option descriptions never send the user to a chat reply; item numbers arrive through the question tool."""
    lines = [line for relative in _DISTILL_FILES for _, line in _instruction_lines(relative) if _OPTION.match(line)]
    assert found("\n".join(lines), ["next message", "in chat", "reply with"]) == []
    specific = next(line for line in lines if "label: `Specific items`" in line)
    assert "via the question tool" in specific


def test_convergence_stop_states_a_true_reason_per_stop_path() -> None:
    """The loop stop report names a reason for each empty part that matches the stop path, never a bare ``none``."""
    section = _section("distill/modes/executables.md", "**Adversarial Convergence Loop**", "**Commit grouping**")
    required = ["`none — no open finding` after a clean `W_n == 0`", "`none — no round completed` before `W_0`"]
    assert missing(section, [*required, "`none — nothing awaits the user`"]) == []
    assert "or `none`" not in section


def test_distill_rule_names_every_exclusion_it_allows() -> None:
    """The skill rule lists each exclusion and each legal ask form, so no site invents its own."""
    rule = (_SKILLS / "distill/SKILL.md").read_text(encoding="utf-8")
    required = [*_DISTILL_EXCLUSIONS, "§Report-first gate exclusion: <name>", "Legal ask forms", "`Other` answer"]
    assert missing(rule, required) == []


@pytest.mark.parametrize("relative", _DISTILL_FILES)
def test_distill_question_options_stay_within_cap(relative: str) -> None:
    """No distill question lists more than four options, counting label lines and inline option lists.

    Label lines: a question's options run from its ``(a)`` label to the next ``- question:`` line or ``(a)`` label, so
    blank lines and prose between labels do not reset the count. Inline lists: backticked labels after the word
    ``options`` in a sentence that names ``AskUserQuestion`` or carries an exclusion tag.
    """
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    assert _label_overflow(relative, text) == []
    assert _inline_option_overflow(relative, text) == []


def test_inline_option_list_over_cap_fails() -> None:
    """Five inline options written into the item-number follow-up are caught; its two current options are not."""
    relative = "distill/modes/prune.md"
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    current = "options `Back` (return to the prune choice) and `Skip` (leave memory untouched)"
    assert current in text
    five = "options `Back` (x), `Skip` (y), `All` (z), `None` (w) and `Later` (v)"
    assert _inline_option_overflow(relative, text.replace(current, five)) != []
    assert _inline_option_overflow(relative, text) == []


@pytest.mark.parametrize(("relative", "start"), _DISTILL_ZERO_RESULTS)
def test_distill_zero_result_run_prints_the_report_without_a_question(relative: str, start: str) -> None:
    """A run that finds nothing to propose still prints all five parts in gate order, empty ones stated, no question."""
    line = next(
        line for line in (_SKILLS / relative).read_text(encoding="utf-8").splitlines() if line.startswith(start)
    )
    assert missing(line, [*_DISTILL_REPORT_LABELS, "none — ", "no question"]) == []
    assert found(line, _FINDINGS_LABELS) != []
    assert "AskUserQuestion" not in line
    positions = _label_positions(line)
    assert positions == sorted(positions), positions


@pytest.mark.parametrize(
    ("relative", "guard", "gate"),
    [
        pytest.param("distill/modes/executables.md", "No qualifying clusters found:", "## Step E3", id="executables"),
        pytest.param("distill/modes/memory.md", "**L4 zero-result**", "**L4 gate report**", id="memory"),
        pytest.param("distill/modes/prune.md", "**P-eager-2 zero-result**", "**P-eager-2**:", id="prune-eager"),
        pytest.param("distill/modes/prune.md", "**P2 zero-result**", "**P3**:", id="prune"),
        pytest.param(
            "distill/modes/external.md",
            "**E13 zero-result**",
            "Write the gate report (SKILL.md §Report-first gate) to `$EXT_RUN_DIR/adoption.md`",
            id="external",
        ),
    ],
)
def test_distill_zero_result_guard_precedes_its_gate(relative: str, guard: str, gate: str) -> None:
    """Each zero-result guard is read before its gate's report and question, so an empty run never reaches them."""
    lines = (_SKILLS / relative).read_text(encoding="utf-8").splitlines()
    guard_at = next(index for index, line in enumerate(lines) if line.startswith(guard))
    gate_at = next(index for index, line in enumerate(lines) if line.startswith(gate))
    assert guard_at < gate_at


@pytest.mark.parametrize(("relative", "marker", "classes", "sites"), _OPEN_ITEM_LISTS)
def test_each_mode_reports_one_open_item_list_everywhere(
    relative: str, marker: str, classes: tuple[str, ...], sites: tuple[str, ...]
) -> None:
    """Each mode defines one open-item list and every gate and zero-result report names it under **Open decisions**.

    One list keeps the zero-result report and the gate report of a mode from disagreeing about what is still open.
    """
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    definition = next(line for line in text.splitlines() if line.startswith(marker))
    assert missing(definition, list(classes)) == []
    name = marker.strip("*").lower()
    assert [site for site in sites if name not in _open_decisions_of(text, site)] == []


def test_dropping_an_open_item_reference_fails() -> None:
    """A zero-result report whose **Open decisions** stops naming the mode's list is caught."""
    text = (_SKILLS / "distill/modes/prune.md").read_text(encoding="utf-8")
    site = "**P2 zero-result**"
    assert "prune open items" in _open_decisions_of(text, site)
    line = next(line for line in text.splitlines() if line.startswith(site))
    mutated = text.replace(
        line, line.replace("**Open decisions** — prune open items", "**Open decisions** — manual-review items")
    )
    assert "prune open items" not in _open_decisions_of(mutated, site)


def test_prune_zero_result_claims_only_analysed_files_kept() -> None:
    """The prune zero-result never says every memory file was kept when a project may have timed out unanalysed."""
    text = (_SKILLS / "distill/modes/prune.md").read_text(encoding="utf-8")
    assert "`none — every memory file kept`" not in text
    assert "`none — every analysed memory file kept`" in text


def test_memory_proposal_template_is_content_of_the_five_part_report() -> None:
    """The L3 template fills the L4 report's findings and recommendations and carries no competing summary layout."""
    section = _section("distill/modes/memory.md", "## Step L3: Generate proposals", "## Step L4")
    assert "L4 report content" in section
    assert found(section, ["### Summary", "**Summary**", "**Open decisions**", "**Review verdict**"]) == []


def test_executables_gate_asks_threshold_only_after_extraction_is_chosen() -> None:
    """The extraction gate keeps the committed four choices without --eager and never forces an irrelevant answer.

    With --eager the threshold is a follow-up asked only after ``bin/ extraction``, so a ``Prose only`` or ``Skip`` user
    answers exactly one question.
    """
    section = _section("distill/modes/executables.md", "## Step E3", "## Step E4")
    lines = section.splitlines()
    first = lines.index('- question: "Extract candidates to bin/ scripts?"')
    labels = [re.search(r"label: `([^`]+)`", line).group(1) for line in lines[first + 1 : first + 5]]
    assert labels == ["HIGH only", "HIGH + MEDIUM", "Prose only", "Skip"]
    follow_up = next(line for line in lines if line.startswith("User picks `bin/ extraction`"))
    assert missing(follow_up, ["follow-up within printed gate", "`Prose only` or `Skip`: no follow-up"]) == []
    threshold = lines.index('- question: "Threshold for bin/ extraction?"')
    assert lines.index(follow_up) < threshold


def test_distill_report_first_rule_covers_every_mode() -> None:
    """The skill-level rule names every mode, the report labels, the question summary, the cap and the default policy.

    The default mode keeps zero gates: its report is printed and its next steps are plain-text commands.
    """
    lines = (_SKILLS / "distill/SKILL.md").read_text(encoding="utf-8").splitlines()
    start = lines.index(next(line for line in lines if line.startswith("**Report-first gate — every mode**")))
    rule = "\n".join(lines[start : lines.index("## Step 1: Inventory existing agents and skills")])
    required = [
        "default Step 5",
        "`prune`",
        "`memory`",
        "`external`",
        "`executables`",
        *_DISTILL_REPORT_LABELS,
        *_FINDINGS_LABELS,
        "never left out",
        "never substitute",
        "No tool call between print and call",
        "Gate reached without printed report = violation",
        "one-line top recommendation",
        "≤4 single-pick options",
        "only actionable options",
        "Zero-result run",
        "never a question",
        "none — default mode runs no reviewer",
        "authoritative default-mode policy",
    ]
    assert missing(rule, required) == []


def test_distill_default_report_follows_gate_part_order_without_a_question() -> None:
    """The default Step 5 report lists the five parts in the gate order, states empty parts, and never asks."""
    lines = (_SKILLS / "distill/SKILL.md").read_text(encoding="utf-8").splitlines()
    end = lines.index("## Mode: Memory Pruning — only when first token is `prune`")
    step5 = "\n".join(lines[lines.index("## Step 5: Report") : end])
    positions = _label_positions(step5)
    assert -1 not in positions, positions
    assert positions == sorted(positions), positions
    required = ["none — default mode runs no reviewer", "none — <reason>", "Default mode has no gate"]
    assert missing(step5, [*required, "plain-text user-run commands"]) == []
    assert '"none"' not in step5
    assert "AskUserQuestion" not in step5


def test_output_routing_defers_distill_default_mode_policy_to_the_skill() -> None:
    """The shared follow-up example states no distill default-mode gate and points at the skill as the single source."""
    lines = (_RULES / "_full/quality-gates.md").read_text(encoding="utf-8").splitlines()
    example = next(line for line in lines if line.startswith("- `foundry:distill`"))
    assert missing(example, ["no gate", "§Report-first gate"]) == []
    assert "(a)" not in example


def test_breakdown_gates_reference_the_canonical_question_text_rule() -> None:
    """All three breakdown gates inherit the same limits and option order from the parent skill."""
    text = (_SKILLS / "brainstorm/modes/breakdown.md").read_text(encoding="utf-8")
    references = [
        line for line in text.splitlines() if "apply SKILL.md §Tree summary format — Question text rule" in line
    ]
    assert len(references) == 3
    assert missing(_question_rule(text, references[0]), _QUESTION_LIMITS) == []
    assert "6 plain lines" not in text
    assert "hard cap 1,500" not in text


def test_clarifying_question_example_places_recommendation_second() -> None:
    """The Step 2 example agrees with the rule used by actual question delivery gates."""
    lines = (_SKILLS / "brainstorm/SKILL.md").read_text(encoding="utf-8").splitlines()
    rule = next(line for line in lines if line.startswith("- Always use **multiple-choice**"))
    assert "`a) Option A`, `b) Option B ★ recommended`" in rule
    assert "§Tree summary format — Question text rule" in rule


_FOLLOW_UP_FRAGMENTS = (
    "first question's text",
    "6 plain lines",
    "600 characters",
    "1,500",
    "no table",
    "no pipe layout",
    "no code fence",
    "final reply after the answer",
    "last tool call",
    "no `preview`",
    "never before the question",
)


@pytest.mark.parametrize("relative", ["audit/SKILL.md", "audit/modes/steps-4-5-7.md"])
def test_audit_follow_ups_use_plain_question_summary_and_final_reply(relative: str) -> None:
    """Audit defers the full findings to the final reply while the question text names where they come from.

    The audit gates point at ``summary.jsonl`` for the question summary and keep the Audit Report headline and total.
    """
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    assert missing(text, [*_FOLLOW_UP_FRAGMENTS, "$RUN_DIR/summary.jsonl", "Audit Report", "Total: N"]) == []


def test_profile_follow_up_uses_plain_question_summary_and_final_reply() -> None:
    """Profile defers the full header to the final reply while the question text names the report it comes from.

    The profile gate points at ``report.md`` for the question summary and keeps the Title and Outcome header fields.
    """
    text = (_SKILLS / "profile/SKILL.md").read_text(encoding="utf-8")
    assert missing(text, [*_FOLLOW_UP_FRAGMENTS, "$REPORT_DIR/report.md", "Title", "Outcome"]) == []


def test_rename_question_carries_small_diff_and_saves_long_context() -> None:
    """Ambiguous rename decisions include a small diff inline or a saved, printed long diff before asking.

    The rename-validation mode keeps a short diff in the question text, saves a long one with 5-line context, and
    batches at most four questions per call.
    """
    text = (_SKILLS / "manage/modes/rename-validation.md").read_text(encoding="utf-8")
    section = text.split("Collect ambiguous hits", 1)[1]
    required = [
        "small renaming diff in the question text",
        ".temp/manage/rename-hit-<id>.md",
        "5-line context",
        *_PRINT_ONCE,
        *_QUESTION_LIMITS,
        "Batch max 4 per call",
    ]
    assert missing(section, required) == []


def test_shared_terminal_template_defers_header_to_final_reply() -> None:
    """The shared template preserves raw file formats and delivers rendered headers after report follow-up answers."""
    text = (_SKILLS / "_shared/terminal-summaries.md").read_text(encoding="utf-8")
    required = [
        "first question's text",
        "Title",
        "Outcome",
        "Verdict",
        "final reply after the answer",
        "never before the question",
        "no `preview`",
        "Field | Value",
    ]
    assert missing(text, required) == []


def test_audit_final_step_delivers_full_findings_after_all_tools() -> None:
    """The actual audit terminal step keeps every finding after the follow-up answer and final cleanup calls.

    The step prints the whole Step 7 report once, as the last thing, after the completion marker and cleanup calls.
    """
    text = (_SKILLS / "audit/SKILL.md").read_text(encoding="utf-8")
    section = text.split("**Step 11b — terminal output**", 1)[1].split("**Completion marker**", 1)[0]
    required = [
        "final reply after the answer",
        "after the last tool call including the completion marker and cleanup",
        "full Step 7 Audit Report from `$RUN_DIR/audit-report.md`",
        "exact `Total: N`",
        "every finding's `one_line`",
        "normal Markdown",
        "printed once here, never before the question or in an option",
    ]
    assert missing(section, required) == []


@pytest.mark.parametrize(
    "relative",
    [
        "audit/SKILL.md",
        "audit/modes/steps-4-5-7.md",
        "brainstorm/SKILL.md",
        "brainstorm/modes/breakdown.md",
        "calibrate/SKILL.md",
        "create/SKILL.md",
        "distill/modes/executables.md",
        "distill/modes/external.md",
        "distill/modes/memory.md",
        "distill/modes/prune.md",
        "manage/modes/rename-validation.md",
        "profile/SKILL.md",
        "session/SKILL.md",
        "setup/SKILL.md",
        "_shared/terminal-summaries.md",
    ],
)
def test_no_rewritten_gate_still_routes_rich_content_to_option_previews(relative: str) -> None:
    """Sibling instructions in a rewritten file cannot retain the old identical-preview delivery requirement.

    None of the files rewritten for plain-question delivery may still contain any retired preview-routing phrase.
    """
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    assert found(text, _OBSOLETE_PREVIEW_ROUTING) == []
