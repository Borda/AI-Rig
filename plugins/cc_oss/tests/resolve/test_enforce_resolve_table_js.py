"""Subprocess tests for ``hooks/enforce-resolve-table.js``.

The hook is a ``PreToolUse`` gate on ``AskUserQuestion`` for the oss:resolve Step 3d item picker. The regressions it
guards: conflict-resolution output in the same turn pushed the findings table out of the reply, and on 5.5-family
models reply text written before a tool call can come back as an empty progress update — either way the user selected
items blind. Contract:

* **Scoped** — only calls carrying the bulk-action question are gated, and only while the run's
  ``resolve-impl-dir-<CSID>`` sentinel names a directory holding ``action-items.jsonl``.
* **Gate ids** — every pending id plus every resolved/addressed id, since closed items are selected only by typing
  their ids, which the bulk question text invites. Done and info rows are never selectable and never required.
* **One table where the user looks first** — a single Markdown table holds every gate id in the call's first
  question: its question text, or the ``preview`` of every option of a single-select question (html-format previews
  are read with their wrapper tags removed). Reply text, option descriptions (rendered as one line), later
  questions, some previews only, and multiSelect previews never count; rows split across fields or tables never add
  up.
* **Denies a blind picker** — no such table; the reason names the ids the best table lacks, where a misplaced table
  sits, and the one placement that passes on retry, and never asks for a reply-text copy.
* **Allows** — the first question's table covers every gate id; non-selection questions; no selectable items.
* **Fails open** — no sentinel, missing or malformed items file, unparsable payload.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "hooks" / "enforce-resolve-table.js"
CSID = "test-session-5678"

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_BULK = {
    "question": "Or choose a bulk action:",
    "header": "Bulk action",
    "multiSelect": False,
    "options": [
        {"label": "+All [req]", "description": "Implement all required items"},
        {"label": "+All [suggest]", "description": "Implement all suggested items"},
        {"label": "ALL (req + suggest)", "description": "Implement all pending items"},
        {"label": "Skip all", "description": "Skip all items, exit"},
    ],
}
_PUSH = {
    "question": "Push after implementing?",
    "header": "Push",
    "options": [{"label": "Push"}, {"label": "Don't push"}],
}

_TABLE = (
    "| # | Type | Severity | Summary |\n"
    "| -- | -- | -- | -- |\n"
    "| 1 | [report] | medium | guard keeps view |\n"
    "| 2 | [gh][suggest] | low | add test |\n"
)
_ITEMS_QUESTION = {
    "question": "Items to implement:",
    "header": "Items",
    "multiSelect": True,
    "options": [
        {"label": "[report] #1: guard keeps view", "description": "foundry:sw-engineer"},
        {"label": "[gh][suggest] #2: add test", "description": "@reviewer"},
    ],
}


def _with_previews(question: dict, preview: str) -> dict:
    """Copy ``question`` with ``preview`` set on every option, as Step 3d puts the table on each bulk option."""
    return {**question, "options": [{**option, "preview": preview} for option in question["options"]]}


def _items(tmp_path: Path, *rows: dict) -> Path:
    """Write action-items.jsonl into an impl dir and point the CSID sentinel at it."""
    impl = tmp_path / "impl"
    impl.mkdir()
    (impl / "action-items.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n"
    )
    (tmp_path / f"resolve-impl-dir-{CSID}").write_text(f"{impl}\n", encoding="utf-8", newline="\n")
    return impl


def _transcript(tmp_path: Path, assistant_text: str) -> Path:
    """Write a transcript with one human turn followed by one assistant reply."""
    path = tmp_path / "transcript.jsonl"
    rows = [
        {"type": "user", "message": {"content": "/oss:resolve 42"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": assistant_text}]}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
    return path


def _run(tmp_path: Path, questions: list[dict], transcript: Path | None) -> str | None:
    """Run the hook and return its denial reason, or None when it allowed the call."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": questions},
        "transcript_path": str(transcript) if transcript else None,
    }
    env = {**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": CSID}
    proc = subprocess.run(
        ["node", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=10, env=env
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    decision = json.loads(out)["hookSpecificOutput"] if out else {}
    return decision.get("permissionDecisionReason") if decision.get("permissionDecision") == "deny" else None


_PENDING = ({"id": 1, "type": "[report]"}, {"id": 2, "type": "[gh][suggest]"}, {"id": 3, "type": "[done]"})


@_skip_node_unavailable
class TestSelectionGate:
    def test_picker_without_table_is_denied(self, tmp_path: Path) -> None:
        """A picker after conflict-resolution chatter, with no table in the reply, is denied and names the ids.

        This is the reported regression: the reply showed only the merge-commit narration before the picker.
        """
        _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [_BULK], _transcript(tmp_path, "Resolved CHANGELOG.md. Committed 8db6a761."))
        assert reason is not None
        assert "1, 2" in reason

    def test_reply_text_table_is_denied_and_the_preview_remedy_passes(self, tmp_path: Path) -> None:
        """A full table in reply text alone is denied; the same table moved into every bulk preview passes on retry.

        On 5.5-family models reply text written before a tool call can render as an empty progress update, so a reply-
        only table may never reach the user. The denial must name the one placement the gate accepts, and that corrected
        call must open the picker — never a second denial.
        """
        _items(tmp_path, *_PENDING)
        transcript = _transcript(tmp_path, "Merged items:\n\n" + _TABLE)
        denied = _run(tmp_path, [_BULK], transcript)
        remedied = _run(tmp_path, [_with_previews(_BULK, _TABLE)], transcript)
        assert denied is not None
        assert "Reply text" in denied
        assert "set the `preview` of every one of its options to the same full ACTION_ITEMS table" in denied
        assert remedied is None

    def test_table_only_in_thinking_is_denied(self, tmp_path: Path) -> None:
        """A table the model wrote only in thinking is denied, and the reason says thinking does not count.

        Observed failure: each picker response held thinking + tool_use and no text block, so the user never saw the
        table. The reason must point at the preview, the one place the table renders with the picker.
        """
        _items(tmp_path, *_PENDING)
        transcript = tmp_path / "transcript.jsonl"
        rows = [
            {"type": "user", "message": {"content": "ask me with questions"}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": _TABLE}]}},
        ]
        transcript.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
        reason = _run(tmp_path, [_BULK], transcript)
        assert reason is not None
        assert "thinking" in reason
        assert "preview" in reason

    def test_ids_mentioned_only_in_prose_are_denied(self, tmp_path: Path) -> None:
        """Item numbers in prose or a count line do not replace the table rows."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_BULK], _transcript(tmp_path, "2 pending items: #1 and #2.")) is not None

    def test_deny_reason_names_the_preview_fix_and_never_asks_for_reply_text(self, tmp_path: Path) -> None:
        """The denial sends the table to the bulk-option preview and never asks for a reply-text copy or a retry.

        A 5.5-family run printed the table three times as reply text; each copy came back as an empty progress update,
        so neither a reprint nor the identical call could succeed. The table is shown once, in the preview, so a denial
        that asked for reply text would also show it twice.
        """
        _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [_BULK], _transcript(tmp_path, "Resolved CHANGELOG.md."))
        assert reason is not None
        assert "put the table in the bulk-option preview" in reason
        assert "print" not in reason.lower()
        assert "identical call" not in reason

    def test_deny_reason_states_exactly_what_the_gate_accepts(self, tmp_path: Path) -> None:
        """The denial names the first-question placement the gate checks and the html-host form of the fix.

        A reason that described a looser rule than the gate checks sent the model to a placement the gate then denied;
        one that omitted the html form left a host that rejects plain-markdown previews looping on denials.
        """
        _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [_BULK], _transcript(tmp_path, "Resolved CHANGELOG.md."))
        assert reason is not None
        assert (
            "The gate passes once one Markdown table holding every pending id sits in the call's first question"
            in reason
        )
        assert "its question text, or the `preview` of every option of that single-select question" in reason
        assert "one option description" not in reason
        assert "make the single-select bulk-action question the call's first question (Q1)" in reason
        assert "html preview hosts: wrap the table in `<pre>` with the table on its own lines" in reason


@_skip_node_unavailable
class TestToolInputTable:
    def test_table_only_in_bulk_previews_is_allowed(self, tmp_path: Path) -> None:
        """A full table carried in the bulk options' preview opens the picker even when the reply text is empty.

        The 5.5-family failure: the transcript held only an empty thinking block plus the tool_use, so the picker's own
        preview was the only copy of the table the user could see.
        """
        _items(tmp_path, *_PENDING)
        transcript = tmp_path / "transcript.jsonl"
        rows = [
            {"type": "user", "message": {"content": "/oss:resolve 42"}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}},
        ]
        transcript.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
        assert _run(tmp_path, [_with_previews(_BULK, _TABLE)], transcript) is None

    def test_denial_never_waits_on_the_transcript(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A picker without a rendered table is denied at once, however long a transcript flush wait is configured.

        Reply text no longer counts, so the hook has no reason to re-read the transcript; a 10 s flush wait set here
        would stall every denial if a transcript poll were still on the path.
        """
        _items(tmp_path, *_PENDING)
        monkeypatch.setenv("CLAUDE_GATE_FLUSH_WAIT_MS", "10000")
        started = time.monotonic()
        reason = _run(tmp_path, [_BULK], _transcript(tmp_path, "no table"))
        assert reason is not None
        assert time.monotonic() - started < 5

    def test_preview_missing_rows_is_denied(self, tmp_path: Path) -> None:
        """A preview table lacking a pending row is denied and names only the missing id.

        The user would still pick without seeing that row, exactly as with a partial reply-text table.
        """
        _items(tmp_path, *_PENDING, {"id": 4, "type": "[report]"})
        reason = _run(tmp_path, [_with_previews(_BULK, _TABLE)], _transcript(tmp_path, "no table"))
        assert reason is not None
        assert "4" in reason
        assert "1, 2" not in reason

    def test_multiselect_preview_is_ignored(self, tmp_path: Path) -> None:
        """A table in a multiSelect question's preview is not rendered for the user, so it never satisfies the gate.

        Only single-select questions render option previews; counting the item checkboxes' preview would be a false pass
        while the user still sees no table.
        """
        _items(tmp_path, *_PENDING)
        questions = [_with_previews(_ITEMS_QUESTION, _TABLE), _BULK]
        assert _run(tmp_path, questions, _transcript(tmp_path, "no table")) is not None

    def test_table_in_question_text_is_allowed(self, tmp_path: Path) -> None:
        """A full table in the bulk question's text also counts as shown.

        Question text renders for every question type and keeps its line breaks, so the user sees the rows there just as
        in a preview.
        """
        _items(tmp_path, *_PENDING)
        bulk = {**_BULK, "question": _BULK["question"] + "\n\n" + _TABLE}
        assert _run(tmp_path, [bulk], _transcript(tmp_path, "no table")) is None

    def test_table_in_an_option_description_is_denied_and_the_preview_retry_passes(self, tmp_path: Path) -> None:
        """A full table in one option description is denied as misplaced, and the preview the denial names passes.

        Claude Code renders each option row on one line, every line break in its description replaced, so the rows
        arrive as one garbled line and the user selects blind; the denial must send the table to the previews.
        """
        _items(tmp_path, *_PENDING)
        described = {**_BULK, "options": [{**_BULK["options"][0], "description": _TABLE}, *_BULK["options"][1:]]}

        denied = _run(tmp_path, [described], _transcript(tmp_path, "no table"))
        remedied = _run(tmp_path, [_with_previews(_BULK, _TABLE)], _transcript(tmp_path, "no table"))

        assert denied is not None
        assert "not where the user sees it before answering" in denied
        assert "It sits in an option `description`, which Claude Code renders as one line" in denied
        assert "or over the cap to the file named in the question text" in denied
        assert remedied is None

    def test_over_cap_table_in_an_option_description_is_denied_and_the_file_retry_passes(self, tmp_path: Path) -> None:
        """A 25-row table in one option description is denied, and the file form for that table passes.

        This is the blind picker the gate let through: the description held every row within its 2000-char cut, but
        rendered as one line. A table that size never fits a preview, so the file form is its passing retry.
        """
        impl = _items(tmp_path, *_pending_items(25))
        described = {
            **_BULK,
            "options": [{**_BULK["options"][0], "description": _long_table(25)}, *_BULK["options"][1:]],
        }

        denied = _run(tmp_path, [described], _transcript(tmp_path, "no table"))
        remedied = _run(tmp_path, [_file_form(impl, _long_table(25))], _transcript(tmp_path, "no table"))

        assert denied is not None
        assert "It sits in an option `description`" in denied
        assert remedied is None

    @pytest.mark.parametrize(
        ("bulk", "reply"),
        [
            pytest.param(
                _with_previews(_BULK, _TABLE),
                "| # | Summary |\n| -- | -- |\n| 4 | late item |\n",
                id="preview-plus-reply",
            ),
            pytest.param(
                {
                    **_BULK,
                    "options": [
                        {**_BULK["options"][0], "preview": _TABLE},
                        {**_BULK["options"][1], "preview": "| # | Summary |\n| -- | -- |\n| 4 | late item |\n"},
                        *_BULK["options"][2:],
                    ],
                },
                "no table",
                id="two-option-previews",
            ),
            pytest.param(
                _with_previews(_BULK, _TABLE + "\n| # | Summary |\n| -- | -- |\n| 4 | late item |\n"),
                "no table",
                id="two-tables-in-one-preview",
            ),
        ],
    )
    def test_rows_split_across_sources_or_tables_are_denied(self, tmp_path: Path, bulk: dict, reply: str) -> None:
        """Rows that only add up across sources, option previews, or separate tables never open the picker.

        The user sees one focused preview, or the reply, at a time — never their union — so a split table still leaves
        some pending row unseen while the user picks.
        """
        _items(tmp_path, *_PENDING, {"id": 4, "type": "[report]"})
        reason = _run(tmp_path, [bulk], _transcript(tmp_path, reply))
        assert reason is not None
        assert "4" in reason


#: A single-select question the picker may carry beside the bulk question; it holds no table.
_COMMIT = {
    "question": "Commit mode for selected items:",
    "header": "Commit",
    "multiSelect": False,
    "options": [{"label": "Each item separately"}, {"label": "All at once"}],
}
#: Calls hiding a full table where the user does not see it before answering, with the misplacement the denial names.
_MISPLACED = [
    pytest.param(
        [
            {
                **_BULK,
                "options": [*_BULK["options"][:2], {**_BULK["options"][2], "preview": _TABLE}, _BULK["options"][3]],
            }
        ],
        "It sits in 1 of 4 option previews",
        id="one-option-preview-only",
    ),
    pytest.param([_COMMIT, _with_previews(_BULK, _TABLE)], "It sits in question 2", id="bulk-question-second"),
]


@_skip_node_unavailable
class TestFirstQuestionPlacement:
    @pytest.mark.parametrize(("questions", "note"), _MISPLACED)
    def test_table_the_user_cannot_see_before_answering_is_denied(
        self, tmp_path: Path, questions: list[dict], note: str
    ) -> None:
        """A full table in only some option previews, or in a later question, is denied and the denial names where.

        The picker opens on question 1 and shows only the focused option's preview, so the user answers before ever
        reaching such a table; a gate that accepted any field let that blind pick through.
        """
        _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, questions, _transcript(tmp_path, "no table"))
        assert reason is not None
        assert note in reason
        assert "not where the user sees it before answering" in reason

    def test_multiselect_bulk_question_keeps_the_misplacement_note(self, tmp_path: Path) -> None:
        """A full table in a multiSelect bulk question's previews is named as misplaced, and single-select then passes.

        multiSelect previews never render, so the call is denied; reading only rendered fields made the reason claim no
        table held the ids at all and dropped the note naming where the table sits and how to move it.
        """
        _items(tmp_path, *_PENDING)
        multiselect = _with_previews({**_BULK, "multiSelect": True}, _TABLE)

        denied = _run(tmp_path, [multiselect], _transcript(tmp_path, "no table"))
        remedied = _run(tmp_path, [{**multiselect, "multiSelect": False}], _transcript(tmp_path, "no table"))

        assert denied is not None
        assert "not where the user sees it before answering" in denied
        assert "It sits in a multiSelect question's previews, which are never rendered" in denied
        assert "no single table in this call shows" not in denied
        assert remedied is None

    def test_question_level_description_table_is_named_as_misplaced(self, tmp_path: Path) -> None:
        """A full table only in the bulk question's question-level description is denied as misplaced, not missing.

        That field is one helper line under the question on form hosts and never delivers; a reason claiming no table
        held the ids, followed by a note saying where the table sits, would contradict itself.
        """
        _items(tmp_path, *_PENDING)

        reason = _run(tmp_path, [{**_BULK, "description": _TABLE}], _transcript(tmp_path, "no table"))

        assert reason is not None
        assert "not where the user sees it before answering" in reason
        assert "It sits in the question-level `description`" in reason
        assert "no single table in this call shows" not in reason

    @pytest.mark.parametrize(("questions", "note"), _MISPLACED)
    def test_call_following_the_denial_remedy_is_allowed(
        self, tmp_path: Path, questions: list[dict], note: str
    ) -> None:
        """The placement the denial names — bulk question first, the table as every option's preview — passes at once.

        A remedy the gate itself rejects loops the model on denials; the corrected call must open the picker on the
        first retry, with the other questions kept after the bulk question.
        """
        _items(tmp_path, *_PENDING)
        denied = _run(tmp_path, questions, _transcript(tmp_path, "no table"))
        others = [question for question in questions if question["question"] != _BULK["question"]]
        remedied = _run(tmp_path, [_with_previews(_BULK, _TABLE), *others], _transcript(tmp_path, "no table"))
        assert denied is not None
        assert "set the `preview` of every one of its options to the same full ACTION_ITEMS table" in denied
        assert remedied is None


#: _TABLE as an html-format host carries it: a tag is required, so the rows arrive wrapped.
_HTML_PREVIEWS = [
    pytest.param("<pre>\n" + _TABLE + "</pre>", id="pre-block"),
    pytest.param("<pre>\n" + _TABLE.replace("add test", "add test &#124; docs") + "</pre>", id="in-cell-entity-pipe"),
    pytest.param("<pre>" + _TABLE + "</pre>", id="pre-inline-first-row"),
    pytest.param('<div class="t"><pre><code>\n' + _TABLE + "</code></pre></div>", id="nested-wrappers"),
    pytest.param("<pre>\n" + _TABLE.replace("|", "&#124;") + "</pre>", id="escaped-pipes"),
]


@_skip_node_unavailable
class TestHtmlPreviewHost:
    @pytest.mark.parametrize("preview", _HTML_PREVIEWS)
    def test_wrapped_table_in_bulk_previews_is_allowed(self, tmp_path: Path, preview: str) -> None:
        """A table an html-format host wraps in tags still opens the picker.

        With ``previewFormat: "html"`` the host rejects a preview holding no html tag; a gate that only read bare
        markdown rows would deny the wrapped table and loop on its own remedy.
        """
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_with_previews(_BULK, preview)], _transcript(tmp_path, "no table")) is None

    def test_in_cell_entity_pipe_never_forges_an_id_cell(self, tmp_path: Path) -> None:
        """An escaped pipe inside a ``<pre>`` cell stays cell content, so it cannot split out a missing id as a cell.

        Decoding ``&#124;`` to a bare ``|`` split the summary ``see &#124; 9 &#124; later`` into a ``9`` cell, and the
        gate counted item 9 as shown although no row for it existed.
        """
        _items(tmp_path, *_PENDING, {"id": 9, "type": "[report]"})
        preview = "<pre>\n" + _TABLE.replace("add test", "see &#124; 9 &#124; later") + "</pre>"
        reason = _run(tmp_path, [_with_previews(_BULK, preview)], _transcript(tmp_path, "no table"))
        assert reason is not None
        assert "item(s) 9." in reason

    def test_html_table_element_is_denied_with_the_pre_remedy(self, tmp_path: Path) -> None:
        """An html ``<table>`` element is not read as rows; the denial names the ``<pre>`` form instead."""
        _items(tmp_path, *_PENDING)
        preview = "<table><tr><th>#</th></tr><tr><td>1</td></tr><tr><td>2</td></tr></table>"
        reason = _run(tmp_path, [_with_previews(_BULK, preview)], _transcript(tmp_path, "no table"))
        assert reason is not None
        assert "wrap the table in `<pre>`" in reason


@_skip_node_unavailable
class TestSharedHelperContract:
    def test_sibling_module_exports_every_helper_the_hook_requires(self) -> None:
        """The sibling report-header-table.js exports, as functions, every helper the hook destructures from it.

        The hook fails open on any exception so a hook bug never strands a run; a renamed or dropped export would then
        silently let a blind picker through. Names are read from the hook's own require, so a newly used helper is
        pinned too.
        """
        source = HOOK.read_text(encoding="utf-8")
        match = re.search(r'const \{([^}]*)\} = require\("\./report-header-table\.js"\)', source)
        assert match is not None
        names = [name.strip() for name in match.group(1).split(",") if name.strip()]
        script = "const m = require(process.argv[1]); process.stdout.write(JSON.stringify(process.argv.slice(2).map((n) => typeof m[n])));"
        proc = subprocess.run(
            ["node", "-e", script, str(HOOK.parent / "report-header-table.js"), *names],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0, proc.stderr
        assert {"cappedQuestionDelivers", "fileDelivery", "previewCapNote"} <= set(names)
        assert json.loads(proc.stdout) == ["function"] * len(names)

    @pytest.mark.parametrize(
        ("mirror", "export"),
        [
            pytest.param("QUESTION_TEXT_MAX_CHARS", "TEXT_MAX_CHARS", id="question-text-cut"),
            pytest.param("PREVIEW_WRAP_COLUMNS", "PREVIEW_WRAP_COLUMNS", id="preview-wrap-width"),
        ],
    )
    def test_mirrored_limit_matches_the_sibling_module(self, mirror: str, export: str) -> None:
        """A host limit the hook restates in its denial equals the sibling module's own value.

        The denial names the limit a retry must meet; a stale mirror would state a limit the module no longer applies,
        so a model following it would be denied again.
        """
        match = re.search(rf"const {mirror} = (\d+);", HOOK.read_text(encoding="utf-8"))
        script = "process.stdout.write(String(require(process.argv[1])[process.argv[2]]));"
        proc = subprocess.run(
            ["node", "-e", script, str(HOOK.parent / "report-header-table.js"), export],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert match is not None
        assert proc.returncode == 0, proc.stderr
        assert int(match.group(1)) == int(proc.stdout)


@_skip_node_unavailable
class TestPassThrough:
    def test_non_selection_question_passes(self, tmp_path: Path) -> None:
        """Push and recovery questions are not item pickers and are never gated."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_PUSH], _transcript(tmp_path, "no table")) is None

    def test_no_sentinel_passes(self, tmp_path: Path) -> None:
        """Outside a resolve run (no impl-dir sentinel) the hook stays silent."""
        assert _run(tmp_path, [_BULK], _transcript(tmp_path, "no table")) is None

    def test_no_selectable_items_passes(self, tmp_path: Path) -> None:
        """With only legacy done or info items there is nothing the user can select, so nothing to show."""
        _items(tmp_path, {"id": 1, "type": "[done]"}, {"id": 2, "type": "[gh][info]"})
        assert _run(tmp_path, [_BULK], _transcript(tmp_path, "no table")) is None

    def test_malformed_items_file_passes(self, tmp_path: Path) -> None:
        """A malformed items file fails open; the skill's own merge checks own that failure."""
        impl = _items(tmp_path, *_PENDING)
        (impl / "action-items.jsonl").write_text("not json\n", encoding="utf-8")
        assert _run(tmp_path, [_BULK], _transcript(tmp_path, "no table")) is None


#: Zero pending: only resolved/addressed rows are selectable, by typing their ids; done and info rows never are.
_CLOSED_ONLY = (
    {"id": 1, "type": "[done]"},
    {"id": 2, "type": "[gh][info]"},
    {"id": 3, "type": "[gh][req]", "status": "resolved"},
    {"id": 4, "type": "[gh][suggest]", "status": "addressed"},
)
_CLOSED_TABLE = (
    "| # | Type | Status | Summary |\n"
    "| -- | -- | -- | -- |\n"
    "| 3 | [gh][req] | resolved | rename param |\n"
    "| 4 | [gh][suggest] | addressed | add docstring |\n"
)


@_skip_node_unavailable
class TestClosedOnlyGate:
    def test_closed_only_picker_without_table_is_denied(self, tmp_path: Path) -> None:
        """With nothing pending, the picker is still denied until a table shows every resolved/addressed id.

        The closed-only call is selected only by typing closed ids, so a picker without their table leaves the user
        typing ids blind; done and info rows are never selectable and are not required.
        """
        _items(tmp_path, *_CLOSED_ONLY)
        reason = _run(tmp_path, [_BULK], _transcript(tmp_path, "no table"))
        assert reason is not None
        assert "resolved/addressed item(s) 3, 4" in reason
        assert "one Markdown table holding every resolved/addressed id" in reason

    def test_closed_only_picker_with_table_in_preview_is_allowed(self, tmp_path: Path) -> None:
        """A closed-only picker whose bulk previews carry the closed rows opens without any reply text."""
        _items(tmp_path, *_CLOSED_ONLY)
        assert _run(tmp_path, [_with_previews(_BULK, _CLOSED_TABLE)], _transcript(tmp_path, "")) is None


def _pending_items(rows: int) -> tuple[dict, ...]:
    """``rows`` pending items with ids 1..rows."""
    return tuple({"id": item, "type": "[gh][suggest]"} for item in range(1, rows + 1))


#: Twelve pending items: their table is 14 rendered lines, past the 12-line preview cap however short each row is.
_LONG_PENDING = _pending_items(12)


def _long_table(rows: int = 12) -> str:
    """Item table with ``rows`` rows (ids 1..rows) plus header and separator."""
    body = "".join(f"| {item} | [gh][suggest] | low | fix item {item} |\n" for item in range(1, rows + 1))
    return "| # | Type | Severity | Summary |\n| -- | -- | -- | -- |\n" + body


def _wide_table(rows: int) -> str:
    """Item table with ``rows`` rows of 170+ chars each: 12 physical lines at 10 rows, each row two 86-col rows wide."""
    body = "".join(f"| {item} | [gh][suggest] | low | {'x' * 140} |\n" for item in range(1, rows + 1))
    return "| # | Type | Severity | Summary |\n| -- | -- | -- | -- |\n" + body


def _file_form(impl: Path, table: str | None) -> dict:
    """Build the over-cap shape: full table in the run's table file, path in Q1's text, a summary in every preview.

    ``table=None`` leaves the file unwritten, as a model that named the path but skipped the Write would.
    """
    table_file = impl / "action-items-table.md"
    if table is not None:
        table_file.write_text("### Action Items — PR #42\n\n" + table, encoding="utf-8", newline="\n")
    summary = (
        f"12 pending · [suggest] 12 · severity low 12\nTop: #1 fix item 1 · #2 fix item 2\nFull table: {table_file}"
    )
    question = {**_BULK, "question": f"{_BULK['question']}\n→ Full item table: {table_file}"}
    return _with_previews(question, summary)


@_skip_node_unavailable
class TestPreviewCap:
    @pytest.mark.parametrize(
        ("rows", "denied"),
        [pytest.param(10, False, id="12-lines-fit"), pytest.param(11, True, id="13-lines-over")],
    )
    def test_preview_counts_only_within_the_line_cap(self, tmp_path: Path, rows: int, denied: bool) -> None:
        """A full table in every bulk preview passes at 12 rendered lines and is denied at 13, naming the cap.

        The host clips a preview to the terminal height with no scroll, so rows past the cap are never seen; a gate that
        counted the clipped table let the user pick blind among the hidden rows.
        """
        _items(tmp_path, *_LONG_PENDING[:rows])

        reason = _run(tmp_path, [_with_previews(_BULK, _long_table(rows))], _transcript(tmp_path, "no table"))

        assert (reason is not None) is denied
        assert not denied or "Its option previews exceed the preview cap (option 1: 13 lines" in reason

    def test_preview_over_the_char_cap_is_denied(self, tmp_path: Path) -> None:
        """A full table in a preview of more than 2000 characters is denied: the host hides such a preview outright."""
        _items(tmp_path, *_PENDING)
        table = _TABLE.replace("add test", "add test " + "x" * 2000)

        reason = _run(tmp_path, [_with_previews(_BULK, table)], _transcript(tmp_path, "no table"))

        assert reason is not None
        assert "exceed the preview cap" in reason
        assert "chars" in reason

    @pytest.mark.parametrize("rows", [pytest.param(12, id="12-rows"), pytest.param(25, id="25-rows-context-budget")])
    def test_over_cap_denial_names_the_file_form_and_its_retry_passes(self, tmp_path: Path, rows: int) -> None:
        """An over-cap table in the previews is denied and routed to the run's table file; that retry passes at once.

        A remedy the gate itself rejects loops the model on denials; the reason carries the exact path to write. A
        25-row selection — the ≥19 context-budget table — never fits a preview, so the file is its only passing shape.
        """
        impl = _items(tmp_path, *_pending_items(rows))
        table_file = impl / "action-items-table.md"

        denied = _run(tmp_path, [_with_previews(_BULK, _long_table(rows))], _transcript(tmp_path, "no table"))
        remedied = _run(tmp_path, [_file_form(impl, _long_table(rows))], _transcript(tmp_path, "no table"))

        assert denied is not None
        assert f"first write that full table to `{table_file}` with the Write tool" in denied
        assert f"`→ Full item table: {table_file}` to Q1's question text, within its first 2000 chars" in denied
        assert "(≤2000 chars and ≤12 lines" in denied
        assert remedied is None

    def test_wide_rows_count_as_wrapped_lines_and_the_file_retry_passes(self, tmp_path: Path) -> None:
        """Twelve physical lines of 170-char rows are denied as 22 wrapped lines; the file form for them passes.

        The preview box is 86 columns wide on the reference terminal, so each row wraps onto two rows and the bottom
        half of the table is clipped; counting physical lines let that table through while the user saw only half.
        """
        impl = _items(tmp_path, *_pending_items(10))

        denied = _run(tmp_path, [_with_previews(_BULK, _wide_table(10))], _transcript(tmp_path, "no table"))
        remedied = _run(tmp_path, [_file_form(impl, _wide_table(10))], _transcript(tmp_path, "no table"))

        assert denied is not None
        assert "Its option previews exceed the preview cap (option 1: 22 lines" in denied
        assert "a line over 86 chars counting as one line per 86 chars it spans" in denied
        assert f"first write that full table to `{impl / 'action-items-table.md'}`" in denied
        assert remedied is None

    def test_file_form_in_a_multiselect_first_question_names_multiselect(self, tmp_path: Path) -> None:
        """A file form whose Q1 is multiSelect is denied for that cause, and the single-select retry passes.

        multiSelect previews never render, so the summaries naming the file are unseen; blaming the preview shape sent
        the model to rewrite summaries that were already correct.
        """
        impl = _items(tmp_path, *_LONG_PENDING)
        form = _file_form(impl, _long_table())

        denied = _run(tmp_path, [{**form, "multiSelect": True}], _transcript(tmp_path, "no table"))
        remedied = _run(tmp_path, [form], _transcript(tmp_path, "no table"))

        assert denied is not None
        assert "a multiSelect question whose option previews never render" in denied
        assert "every option preview must be a compact summary" not in denied
        assert remedied is None

    def test_file_named_past_the_shown_question_text_names_the_cut(self, tmp_path: Path) -> None:
        """A file named only past Q1's first 2000 chars is denied for that cause, and the pointer moved up passes.

        The host cuts question text after 2000 chars, so the user never sees that pointer; blaming the preview shape
        left the pointer where it was, and the retry was denied again.
        """
        impl = _items(tmp_path, *_LONG_PENDING)
        form = _file_form(impl, _long_table())
        pushed = {
            **form,
            "question": f"{_BULK['question']}\n{'y' * 2100}\n→ Full item table: {impl / 'action-items-table.md'}",
        }

        denied = _run(tmp_path, [pushed], _transcript(tmp_path, "no table"))
        remedied = _run(tmp_path, [form], _transcript(tmp_path, "no table"))

        assert denied is not None
        assert "is named in Q1's question text only past its first 2000 chars" in denied
        assert "every option preview must be a compact summary" not in denied
        assert remedied is None

    @pytest.mark.parametrize(
        ("table", "expected"),
        [
            pytest.param(None, "does not exist — write the full table there with the Write tool", id="file-missing"),
            pytest.param(_long_table(11), "pending item(s) 12.", id="file-lacks-an-id"),
        ],
    )
    def test_file_form_without_every_id_on_disk_is_denied(
        self, tmp_path: Path, table: str | None, expected: str
    ) -> None:
        """The file form passes only when the named file exists and its table holds every gate id.

        A summary pointing at a file that was never written, or at a table missing a row, leaves the user selecting
        without that row.
        """
        impl = _items(tmp_path, *_LONG_PENDING)

        reason = _run(tmp_path, [_file_form(impl, table)], _transcript(tmp_path, "no table"))

        assert reason is not None
        assert expected in reason

    @pytest.mark.parametrize(
        ("rows", "allowed"),
        [pytest.param(8, False, id="12-line-file-fits-the-cap"), pytest.param(9, True, id="13-line-file-over-the-cap")],
    )
    def test_file_form_passes_only_for_a_table_over_the_cap(self, tmp_path: Path, rows: int, allowed: bool) -> None:
        """The file form passes only when the named file's table is over the preview cap; one that fits is denied.

        A table that fits belongs in every bulk preview, where the user sees it without opening a file. The file holds a
        heading and a blank line before the table, so 8 rows render 12 lines and 9 rows render 13.
        """
        impl = _items(tmp_path, *_pending_items(rows))

        reason = _run(tmp_path, [_file_form(impl, _long_table(rows))], _transcript(tmp_path, "no table"))

        assert (reason is None) is allowed
        assert allowed or "fits the preview cap, so a file named in the question does not deliver it" in reason

    def test_file_form_for_a_table_that_fits_is_denied_and_the_preview_retry_passes(self, tmp_path: Path) -> None:
        """A table that fits, delivered only as a named file, is denied with the preview remedy; that retry passes.

        The summary preview would record a delivery the user never saw in full; the remedy must send the table itself to
        every preview, and the call so corrected must open the picker at once.
        """
        impl = _items(tmp_path, *_pending_items(8))

        denied = _run(tmp_path, [_file_form(impl, _long_table(8))], _transcript(tmp_path, "no table"))
        remedied = _run(tmp_path, [_with_previews(_BULK, _long_table(8))], _transcript(tmp_path, "no table"))

        assert denied is not None
        assert "pass it, complete, as the `preview` of every option instead" in denied
        assert "no single table in this call shows" not in denied
        assert remedied is None

    def test_file_form_naming_an_unreadable_path_keeps_the_missing_file_denial(self, tmp_path: Path) -> None:
        """A table path that cannot be read as a file is denied as missing, exactly as before the size check.

        A directory in the file's place is unreadable on every OS; it must never pass and never be sized as a table.
        """
        impl = _items(tmp_path, *_pending_items(8))
        (impl / "action-items-table.md").mkdir()

        reason = _run(tmp_path, [_file_form(impl, None)], _transcript(tmp_path, "no table"))

        assert reason is not None
        assert "does not exist — write the full table there with the Write tool" in reason

    def test_file_form_with_a_preview_that_is_only_the_path_is_denied(self, tmp_path: Path) -> None:
        """Every preview must be a summary beside the path; a preview holding the path alone tells the user nothing."""
        impl = _items(tmp_path, *_LONG_PENDING)
        call = _file_form(impl, _long_table())
        bare = {
            **call,
            "options": [{**call["options"][0], "preview": str(impl / "action-items-table.md")}, *call["options"][1:]],
        }

        reason = _run(tmp_path, [bare], _transcript(tmp_path, "no table"))

        assert reason is not None
        assert "1 of 4 option previews are not a compact summary within the cap" in reason


#: Pending and closed rows together: the bulk text asks the user to type closed ids, so both kinds are required.
_MIXED = (*_PENDING, {"id": 4, "type": "[gh][req]", "status": "resolved"})


@_skip_node_unavailable
class TestMixedPickerGate:
    def test_table_without_closed_rows_is_denied(self, tmp_path: Path) -> None:
        """A picker whose table shows every pending row but no resolved/addressed row is denied, naming the closed id.

        The bulk question text tells the user to type closed ids; with only the pending rows shown, the user would type
        an id whose row never appeared.
        """
        _items(tmp_path, *_MIXED)

        reason = _run(tmp_path, [_with_previews(_BULK, _TABLE)], _transcript(tmp_path, "no table"))

        assert reason is not None
        assert "pending and resolved/addressed item(s) 4." in reason
        assert "one Markdown table holding every pending and resolved/addressed id" in reason

    def test_table_with_pending_and_closed_rows_is_allowed(self, tmp_path: Path) -> None:
        """A table holding every pending row plus the closed row opens the mixed picker."""
        _items(tmp_path, *_MIXED)
        table = _TABLE + "| 4 | [gh][req] | high | rename param |\n"

        assert _run(tmp_path, [_with_previews(_BULK, table)], _transcript(tmp_path, "no table")) is None
