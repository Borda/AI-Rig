#!/usr/bin/env python
"""commit_action_item.py — commit helper for /oss:resolve Step 8.

Stages the given files and commits them with a caller-supplied or script-built
message.

Three message-source modes (mutually exclusive):

* ``--message-file <path>`` — caller supplies the fully-formed message (used by
  the ``all`` commit path, which assembles a bespoke body).
* ``--build`` plus fields — script assembles the canonical per-item ``each``-mode
  message (subject + ``[resolve No.<id>]`` attribution block + co-author trailers),
  so the ``each``-mode template lives in one place instead of being inlined in
  ``action-item-dispatch.md``.
* ``--build-group`` plus fields — script assembles the ``grouped``-mode message from
  the group's topic label and the per-item commit subjects. Item subjects already
  carry a Conventional Commits ``type(scope):`` prefix, so the topic label is never
  prepended to them (that produced ``tests: test(predict): …`` double prefixes).

Usage:
    commit_action_item.py --message-file <path> --files <file1> [<file2>...]
    commit_action_item.py --build --summary <s> --item-id <id> --author <a> \\
        --pr <n> --comment <text> --challenge <text> [--codex] \\
        --files <file1> [<file2>...]
    commit_action_item.py --build-group --topic <t> --summaries-file <path> \\
        --pr <n> --items "<id> <id>..." [--codex] --files <file1> [<file2>...]

Exit codes:
    0 — commit succeeded
    1 — bad args, message file missing, or commit failed
    3 — staging area empty for the given ``--files`` after add — no commit created. Distinct from 0
        (never a silent no-op success): a caller whose combined-reset design pre-stages other groups'
        files alongside this group's must be able to tell "nothing to commit for these paths" from
        "committed" — treating both as 0 let one group's commit silently absorb every other group's
        staged diff while every later group reported success for doing nothing.
"""

from __future__ import annotations

import argparse
import atexit
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from shutil import which

#: Matches ASCII control characters, including newlines, that must not reach a commit message.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
#: Parses a Conventional Commits subject into type, optional scope, optional ``!`` marker and description.
_CC_SUBJECT_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()]*)\))?(?P<bang>!)?: (?P<desc>\S.*)$")
#: Conventional Commits types accepted as an item subject's own prefix.
_CC_TYPES = frozenset({"feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert"})
#: Grouping labels from the ``domain`` auto mapping that are not themselves valid commit types.
_TOPIC_TYPES = {"tests": "test", "logic": "fix", "misc": "chore", "config": "chore"}
#: Grouped-subject length cap, matching the documented ``grouped`` contract in action-item-dispatch.md.
_GROUP_SUBJECT_MAX = 72
#: Co-author trailer appended to every commit message this script builds.
_CLAUDE_TRAILER = "Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>"
#: Extra co-author trailer added when Codex contributed to the change (``include_codex``).
_CODEX_TRAILER = "Co-authored-by: Codex <codex@openai.com>"


def _sanitize_field(text: str) -> str:
    """Replace control characters — including newlines — with a space, for text bound for a commit message.

    ``build_each_message`` interpolates reviewer- and PR-comment-supplied text directly into a
    permanent git commit authored under an automated bot identity. An embedded ``\\n`` could forge
    an extra trailer line (e.g. a spoofed ``Co-authored-by:``) or corrupt the ``[resolve No.<id>]``
    header this template relies on. Substituting a space (rather than deleting) keeps the quoted
    excerpt legible and preserves character count, so downstream truncation to a fixed width is
    unaffected either way — sanitizing before truncation here is a fidelity choice (keep the first
    72 *visible* chars) not a security requirement, since truncate-then-sanitize is equally safe.

    A control character does not become a line start on its own: only the template's own literal
    ``\\n`` characters create line starts, so replacing rather than deleting is sufficient — no
    structural token can be reconstructed by two now-adjacent words. Not covered: Unicode line
    separators U+0085/U+2028/U+2029 — display-layer hygiene only, since git splits commit-object
    lines on LF, not on these; a deliberate, recorded scope limit, not an oversight.

    Args:
        text: Raw field value.

    Returns:
        *text* with every ASCII control character (0x00-0x1F, 0x7F) replaced by a single space.

    Examples:
        >>> _sanitize_field("line1\\nline2")
        'line1 line2'
        >>> _sanitize_field("octocat")
        'octocat'
    """
    return _CONTROL_CHARS_RE.sub(" ", text)


@dataclass(frozen=True)
class EachMessageFields:
    """Fields for the ``each``-mode per-item commit message.

    Attributes:
        summary: Imperative short subject for the change.
        item_id: Review action-item id.
        author: GitHub handle of the reviewer (without leading ``@`` — attribution only,
            not a live ping).
        pr: Pre-formatted PR reference to embed verbatim — ``#<N>`` when the commit
            lands in the same repo as the PR, or the full PR URL when it lands in a
            different repo (e.g. pushed to a contributor's fork, where bare ``#N``
            would resolve against the fork's own issues, not this PR). Caller resolves
            which form applies (see ``PR_REF`` in ``resolve/SKILL.md`` Step 4).
        comment: Full review comment text (truncated to 72 chars in the body).
        challenge: Challenge-outcome string (e.g. ``evidence=VALID suggestion=VALID resolution=as-suggested``).
        include_codex: Whether to add the OpenAI Codex co-author trailer.
    """

    summary: str
    item_id: str
    author: str
    pr: str
    comment: str
    challenge: str
    include_codex: bool = False


def build_each_message(fields: EachMessageFields) -> str:
    """Build the canonical ``each``-mode per-item commit message.

    Mirrors the heredoc previously inlined in ``action-item-dispatch.md``:
    subject line, ``[resolve No.<id>]`` attribution block
    quoting the first 72 chars of the review comment, the challenge-outcome line,
    then the Claude (and optional Codex) co-author trailers.

    Args:
        fields: Structured message fields (see :class:`EachMessageFields`).

    Returns:
        Full commit message string.

    Examples:
        >>> f = EachMessageFields(
        ...     "Fix typo", "3", "octocat", "#42", "Please fix the typo here",
        ...     "evidence=VALID suggestion=VALID resolution=as-suggested",
        ... )
        >>> msg = build_each_message(f)
        >>> msg.splitlines()[0]
        'Fix typo'
        >>> "[resolve No.3] Review by octocat (PR #42):" in msg
        True
        >>> "Co-authored-by: Codex <codex@openai.com>" in msg
        False
        >>> codex_fields = EachMessageFields("s", "1", "a", "9", "c", "evidence=VALID", True)
        >>> "Co-authored-by: Codex <codex@openai.com>" in build_each_message(codex_fields)
        True
    """
    summary = _sanitize_field(fields.summary)
    item_id = _sanitize_field(fields.item_id)
    author = _sanitize_field(fields.author)
    pr = _sanitize_field(fields.pr)
    challenge = _sanitize_field(fields.challenge)
    quoted = _sanitize_field(fields.comment)[:72]
    codex_trailer = "\nCo-authored-by: Codex <codex@openai.com>" if fields.include_codex else ""
    return (
        f"{summary}\n"
        f"\n"
        f"[resolve No.{item_id}] Review by {author} (PR {pr}):\n"
        f'"{quoted}..."\n'
        f"Challenge: {challenge}\n"
        f"\n---\n"
        f"Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>"
        f"{codex_trailer}"
    )


@dataclass(frozen=True)
class _CCSubject:
    """A commit subject split into its Conventional Commits parts."""

    type: str
    scope: str
    breaking: bool
    desc: str


def _parse_cc_subject(text: str) -> _CCSubject | None:
    """Split a commit subject into Conventional Commits parts, or return None when it has no valid prefix.

    Only the standard type vocabulary counts, so free text that merely contains a colon
    (``note: runs on CPU``) is not mistaken for a typed subject.

    Args:
        text: One commit subject line.

    Returns:
        Parsed parts, or ``None`` when *text* lacks a recognised ``type(scope)!:`` prefix.

    Examples:
        >>> _parse_cc_subject("test(predict): harden routing test")
        _CCSubject(type='test', scope='predict', breaking=False, desc='harden routing test')
        >>> _parse_cc_subject("note: runs on CPU") is None
        True
    """
    match = _CC_SUBJECT_RE.match(text)
    if match is None or match["type"] not in _CC_TYPES:
        return None
    return _CCSubject(match["type"], match["scope"] or "", bool(match["bang"]), match["desc"].strip())


def _fit_words(text: str, width: int) -> str:
    """Shorten text to at most width characters, cutting at a word boundary and marking the cut with an ellipsis.

    Args:
        text: Description text to shorten.
        width: Maximum length of the result, ellipsis included.

    Returns:
        *text* unchanged when it fits, else a word-boundary prefix ending in ``…``.

    Examples:
        >>> _fit_words("gate MPS fallback", 40)
        'gate MPS fallback'
        >>> _fit_words("gate MPS antialias CPU fallback on torch", 20)
        'gate MPS antialias…'
    """
    if len(text) <= width:
        return text
    cut = text[: max(width - 1, 1)]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:") + "…"


def _group_type(topic: str, parsed: list[_CCSubject | None]) -> str:
    """Pick the commit type for a group: the items' shared type, else one derived from the topic label.

    Args:
        topic: Grouping label (``domain`` auto label, file slug, specialist tag, or typed label).
        parsed: Parsed item subjects, ``None`` for an item without a typed subject.

    Returns:
        A Conventional Commits type.

    Examples:
        >>> _group_type("misc", [_parse_cc_subject("docs(x): a")])
        'docs'
        >>> _group_type("logic", [None])
        'fix'
        >>> _group_type("my-module", [None])
        'chore'
    """
    types = {p.type for p in parsed if p is not None}
    if len(types) == 1 and all(p is not None for p in parsed):
        return types.pop()
    if topic in _TOPIC_TYPES:
        return _TOPIC_TYPES[topic]
    return topic if topic in _CC_TYPES else "chore"


def _group_scope(topic: str, parsed: list[_CCSubject | None]) -> str:
    """Pick the commit scope for a group: the items' shared scope, else a topic label that names a module.

    Args:
        topic: Grouping label.
        parsed: Parsed item subjects, ``None`` for an item without a typed subject.

    Returns:
        Scope text, or ``""`` for no scope.

    Examples:
        >>> _group_scope("tests", [_parse_cc_subject("test(predict): a"), _parse_cc_subject("test(predict): b")])
        'predict'
        >>> _group_scope("my-module", [None])
        'my-module'
        >>> _group_scope("tests", [None])
        ''
    """
    scopes = {p.scope for p in parsed if p is not None}
    if len(scopes) == 1 and all(p is not None for p in parsed) and "" not in scopes:
        return scopes.pop()
    return "" if topic in _TOPIC_TYPES or topic in _CC_TYPES else topic


def build_group_subject(topic: str, summaries: list[str]) -> str:
    """Build one Conventional Commits subject for a group of review items, never stacking the topic on a typed subject.

    A single item whose subject is already typed is kept verbatim. Otherwise the type and
    scope come from what the items share, falling back to the topic label; the first item's
    description leads, with ``(+N more)`` for the rest, capped at 72 characters.

    Args:
        topic: Grouping label (``domain`` auto label, file slug, specialist tag, or typed label).
        summaries: Per-item subjects — Phase 2 commit subjects or free-text summaries.

    Returns:
        Subject line.

    Examples:
        >>> build_group_subject("tests", ["test(predict): harden MPS routing test"])
        'test(predict): harden MPS routing test'
        >>> build_group_subject("tests", ["test(predict): harden routing", "test(predict): add case"])
        'test(predict): harden routing (+1 more)'
        >>> build_group_subject("logic", ["gate MPS fallback"])
        'fix: gate MPS fallback'
    """
    cleaned = [s for s in (_sanitize_field(raw).strip() for raw in summaries) if s] or ["resolve review items"]
    parsed = [_parse_cc_subject(s) for s in cleaned]
    if len(cleaned) == 1 and parsed[0] is not None:
        return cleaned[0]
    scope = _group_scope(topic, parsed)
    breaking = "!" if any(p is not None and p.breaking for p in parsed) else ""
    head = f"{_group_type(topic, parsed)}{f'({scope})' if scope else ''}{breaking}: "
    suffix = f" (+{len(cleaned) - 1} more)" if len(cleaned) > 1 else ""
    first_desc = parsed[0].desc if parsed[0] is not None else cleaned[0]
    return head + _fit_words(first_desc, max(_GROUP_SUBJECT_MAX - len(head) - len(suffix), 10)) + suffix


@dataclass(frozen=True)
class GroupMessageFields:
    """Fields for the ``grouped``-mode commit message.

    Attributes:
        topic: Grouping label, already sanitized to ``[a-z0-9-]`` by the caller.
        summaries: Per-item subjects, in group order.
        pr: Pre-formatted PR reference embedded verbatim (same contract as ``EachMessageFields.pr``).
        item_ids: Space-separated item ids that contributed to this commit.
        include_codex: Whether to add the OpenAI Codex co-author trailer.
    """

    topic: str
    summaries: tuple[str, ...]
    pr: str
    item_ids: str
    include_codex: bool = False


def build_group_message(fields: GroupMessageFields) -> str:
    """Build the ``grouped``-mode commit message: typed subject, item list, group marker, trailers.

    The ``[resolve group] PR <ref> — items <ids>`` line is parsed by resolve's straggler gate
    (token-exact match on ids), so its shape must stay fixed.

    Args:
        fields: Structured message fields (see :class:`GroupMessageFields`).

    Returns:
        Full commit message string.

    Examples:
        >>> msg = build_group_message(GroupMessageFields("tests", ("test(x): a", "test(x): b"), "#9", "3 4"))
        >>> msg.splitlines()[0]
        'test(x): a (+1 more)'
        >>> "[resolve group] PR #9 — items 3 4" in msg
        True
    """
    summaries = [s for s in (_sanitize_field(raw).strip() for raw in fields.summaries) if s]
    bullets = "".join(f"- {s}\n" for s in summaries) + "\n" if len(summaries) > 1 else ""
    codex_trailer = f"\n{_CODEX_TRAILER}" if fields.include_codex else ""
    return (
        f"{build_group_subject(fields.topic, list(fields.summaries))}\n"
        f"\n"
        f"{bullets}"
        f"[resolve group] PR {_sanitize_field(fields.pr)} — items {_sanitize_field(fields.item_ids)}\n"
        f"\n---\n"
        f"{_CLAUDE_TRAILER}"
        f"{codex_trailer}"
    )


#: CLI flags that consume exactly one following argument during argument parsing.
_SINGLE_VALUE_FLAGS = frozenset(
    {
        "--message-file",
        "--summary",
        "--item-id",
        "--author",
        "--pr",
        "--comment",
        "--challenge",
        "--topic",
        "--summaries-file",
        "--items",
    }
)
#: CLI flags that select message-building mode, as opposed to committing from ``--message-file``.
_BUILD_MODES = ("--build", "--build-group")


def _parse_args(args: list[str]) -> tuple[dict[str, str | bool], list[str], str | None]:
    """Parse CLI args into an options dict plus the ``--files`` list.

    Args:
        args: Raw argument list (``sys.argv[1:]``).

    Returns:
        ``(opts, files, error)`` — ``error`` is ``None`` on success or a message string.

    Examples:
        >>> opts, files, err = _parse_args(["--message-file", "m.txt", "--files", "a.py", "b.py"])
        >>> err is None and files == ["a.py", "b.py"]
        True
        >>> opts["--message-file"]
        'm.txt'
        >>> _parse_args(["--bogus"])[2]
        "commit_action_item: unknown arg '--bogus'"
    """
    opts: dict[str, str | bool] = {}
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--codex":
            opts["--codex"] = True
            i += 1
        elif a in _BUILD_MODES:
            opts[a] = True
            i += 1
        elif a in _SINGLE_VALUE_FLAGS:
            opts[a] = args[i + 1] if i + 1 < len(args) else ""
            i += 2
        elif a == "--files":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                files.append(args[i])
                i += 1
        else:
            return opts, files, f"commit_action_item: unknown arg '{a}'"
    return opts, files, None


def _render_group_message(opts: dict[str, str | bool]) -> tuple[str, str | None]:
    """Render the ``--build-group`` message from parsed options, reading item subjects from the summaries file.

    Args:
        opts: Parsed options dict from :func:`_parse_args`.

    Returns:
        ``(message, error)`` — ``error`` is ``None`` on success.

    Examples:
        >>> _render_group_message({"--topic": "tests"})[1]
        'commit_action_item: --build-group requires --summaries-file'
    """
    summaries_file = str(opts.get("--summaries-file", ""))
    if not summaries_file:
        return "", "commit_action_item: --build-group requires --summaries-file"
    if not Path(summaries_file).is_file():
        return "", f"commit_action_item: summaries file not found: {summaries_file}"
    summaries = tuple(Path(summaries_file).read_text(encoding="utf-8").splitlines())
    fields = GroupMessageFields(
        topic=str(opts.get("--topic", "")),
        summaries=summaries,
        pr=str(opts.get("--pr", "")),
        item_ids=str(opts.get("--items", "")),
        include_codex=bool(opts.get("--codex", False)),
    )
    return build_group_message(fields), None


def _render_message(opts: dict[str, str | bool]) -> tuple[str, str | None]:
    """Render the commit message for whichever build mode the options select.

    Args:
        opts: Parsed options dict from :func:`_parse_args`; exactly one build mode is set.

    Returns:
        ``(message, error)`` — ``error`` is ``None`` on success.

    Examples:
        >>> _render_message({"--build-group": True})[1]
        'commit_action_item: --build-group requires --summaries-file'
    """
    if opts.get("--build-group"):
        return _render_group_message(opts)
    each = EachMessageFields(
        summary=str(opts.get("--summary", "")),
        item_id=str(opts.get("--item-id", "")),
        author=str(opts.get("--author", "")),
        pr=str(opts.get("--pr", "")),
        comment=str(opts.get("--comment", "")),
        challenge=str(opts.get("--challenge", "")),
        include_codex=bool(opts.get("--codex", False)),
    )
    return build_each_message(each), None


def _resolve_message_file(opts: dict[str, str | bool]) -> tuple[str, str | None]:
    """Resolve the commit message file from ``--message-file``, ``--build``, or ``--build-group``.

    In a build mode the message is rendered and written to a NamedTemporaryFile whose
    path is returned (cleaned up at process exit).

    Args:
        opts: Parsed options dict from :func:`_parse_args`.

    Returns:
        ``(message_file_path, error)`` — ``error`` is ``None`` on success.

    Examples:
        No doctest — build paths write a temp file; covered by pytest.
    """
    modes = [m for m in (*_BUILD_MODES, "--message-file") if m in opts]
    if len(modes) > 1:
        return "", f"commit_action_item: pass only one of {', '.join(modes)}, not both"
    if modes and modes[0] in _BUILD_MODES:
        msg, err = _render_message(opts)
        if err is not None:
            return "", err
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", encoding="utf-8", newline="\n", delete=False)
        with handle:
            handle.write(msg)
        path = handle.name
        atexit.register(lambda: Path(path).unlink(missing_ok=True))
        return path, None

    msg_file = str(opts.get("--message-file", ""))
    if not msg_file:
        return "", "commit_action_item: --message-file required"
    if not Path(msg_file).is_file():
        return "", f"commit_action_item: message file not found: {msg_file}"
    return msg_file, None


def _stage_and_commit(git: str, msg_file: str, files: list[str]) -> int:
    """Stage ``files`` and commit them using ``msg_file`` as the message.

    Args:
        git: Absolute path to the ``git`` executable.
        msg_file: Path to the commit message file.
        files: Pathspec of files to stage and commit.

    Returns:
        Exit code: the git subprocess's own return code; 3 when nothing is
        staged for these files after ``git add``.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    add_proc = subprocess.run([git, "add", "--", *files], check=False)  # noqa: S603
    if add_proc.returncode != 0:
        print(f"commit_action_item: git add failed (exit {add_proc.returncode})", file=sys.stderr)
        return add_proc.returncode

    # Empty staging area for THESE files → nothing to commit. Scoped with `-- *files`, not a bare
    # `git diff --cached --quiet` — an unscoped check reads true (has staged changes) whenever any
    # OTHER path is staged in the index, which a caller collapsing multiple groups into one staged
    # diff via a combined reset does deliberately; scoping is what lets this group's own emptiness
    # be told apart from "some unrelated group's diff happens to still be staged".
    cached_proc = subprocess.run(  # noqa: S603
        [git, "diff", "--cached", "--quiet", "--", *files],
        check=False,
    )
    if cached_proc.returncode == 0:
        print(
            "commit_action_item: staging area empty after add — no commit created",
            file=sys.stderr,
        )
        return 3

    # Pathspec the commit — commits only THESE files' staged state, leaving any other group's
    # files staged untouched for their own subsequent commit. Without `-- *files`, `git commit`
    # commits the entire index: the first of several sequential per-group commits (a combined
    # reset stages every group's diff at once) would silently absorb every later group's changes,
    # and each later group's own commit call would then find nothing staged for its files.
    # NOTE: a partial `git commit -- <paths>` commits the WORKING-TREE content of those paths, not
    # necessarily the staged (index) content — invisible here because every caller's flow leaves
    # index and working tree identical for these files (a soft reset touches only the index). Any
    # future caller that stages a file via a partial `git add -p` (or a hook rewrites it after
    # `add`) would silently commit content the caller never staged.
    result = subprocess.run([git, "commit", "-F", msg_file, "--", *files], check=False)  # noqa: S603
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``commit_action_item.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 1 on bad args or commit failure; 3 on empty stage (no commit created); 0 on success.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    args = list(sys.argv[1:] if argv is None else argv)

    # Honour only ``-h/--help`` via argparse; every other flag flows through the manual
    # _parse_args below, which is already reject-strict (unknown arg → exit 1) with a
    # bespoke "unknown arg '<a>'" message. argparse's native errors would change that
    # exit-1 contract to exit-2 — keep the manual parser as the sole argv authority.
    if args in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="commit_action_item.py",
            description="Commit helper for /oss:resolve Step 8.",
        ).parse_args(["-h"])

    opts, files, err = _parse_args(args)
    if err is not None:
        print(err, file=sys.stderr)
        return 1

    msg_file, err = _resolve_message_file(opts)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    if not files:
        print("commit_action_item: --files requires at least one path", file=sys.stderr)
        return 1

    git = which("git")
    if git is None:
        raise FileNotFoundError("executable not found on PATH: git")

    return _stage_and_commit(git, msg_file, files)


if __name__ == "__main__":
    sys.exit(main())
