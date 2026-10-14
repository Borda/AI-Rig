#!/usr/bin/env python3
"""Validate release evidence and record explicitly requested local demo executions.

## Purpose

Reject passing release communication results whose local evidence receipt cannot prove a bounded release contract.

## Scope

This module validates the release-specific Git range, already-published patch subtraction, claims, attribution, output
bytes, changelog preservation, and demo execution receipt associated with a versioned release result. It deliberately
checks exact local records and source bytes; it does not claim to decide general prose truth or publish a release.

## Usage

``validate_release_evidence(metadata, out_dir, requested_artifacts)`` is called from ``validate-artifacts.py`` after the
common result and gate checks. The caller supplies parsed result metadata, the retained run directory, and selected
deliverable names. Validation only reads local Git and files. The separate, explicit CLI ``python release_evidence.py
record-demo --script demo.py --cwd . --environment 'project environment' --out run`` executes the script with the
selected Python interpreter, retaining output and before/after digests in a new directory. It never installs
dependencies or grants network access; the owning workflow must authorize execution first.

## Outputs

A receipt satisfying every binding returns normally. A missing, stale, incomplete, or internally inconsistent record
raises ``SystemExit`` with a stable ``release-evidence-*`` reason that makes the result ineligible for promotion.

## Failure

Examples include a non-ancestor baseline, unaccounted candidate, incomplete credit or exclusion, path/digest drift, lost
historical changelog bytes, placeholder summary, or a demo receipt whose recorded output cannot be bound. Ambiguous
patch equivalence fails closed rather than being inferred from subjects, titles, or net tree state.

## Used by

``shared/validate-artifacts.py`` invokes this helper for release contract version one. ``skills/release`` documents the
receipt producer contract. ``tests/packaging/test_release_artifacts.py`` uses real temporary Git histories to exercise
the public validator entrypoint.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import html
import json
import os
import re
import string
import subprocess
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from run_gates import terminate_process

#: Pattern for a full git commit hash, either 40 hex characters (SHA-1) or 64 (SHA-256).
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
#: Allowed dispositions for a commit's release-note evidence row.
_DISPOSITIONS = {"included", "already-released", "internal-only", "reverted", "superseded", "merge-only"}
#: Allowed stated bases for excluding an item from the release notes.
_EXCLUSION_BASES = {"legal-restriction", "privacy-request", "verified-duplicate"}


def _fail(reason: str) -> None:
    """Stop validation with a stable release-evidence failure reason."""
    raise SystemExit(f"release-evidence-{reason}")


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one regular evidence file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_completed(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one Git command with UTF-8 decoded output and leave the exit status to the caller."""
    return subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "-c", "i18n.logOutputEncoding=utf-8", *args],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _git(repository: Path, *args: str) -> str:
    """Read Git paths and metadata as UTF-8 and reject a nonzero result."""
    completed = _git_completed(repository, *args)
    if completed.returncode:
        _fail("git:" + args[0])
    return completed.stdout.strip()


def _prefetch_rev_parse(repository: Path, head: object) -> dict[str, str]:
    """Answer the three pinned-head probes with one Git process instead of three.

    ``git rev-parse`` prints one line per argument in argument order, so each probe's line is the answer a separate call
    would give. Any refusal by Git, or an unexpected line count, yields no answers at all: ``_rev_parse`` then asks Git
    one probe at a time, so a missing or unrelated revision still fails at the same step with the same reason instead of
    being attributed to whichever probe the batch happened to reject.
    """
    if not isinstance(head, str) or not _SHA.fullmatch(head):
        return {}
    probes = ("--is-inside-work-tree", "HEAD", f"{head}^{{tree}}")
    completed = _git_completed(repository, "rev-parse", *probes)
    lines = completed.stdout.strip().splitlines()
    if completed.returncode or len(lines) != len(probes):
        return {}
    return dict(zip(probes, lines, strict=True))


def _rev_parse(repository: Path, probe: str, answers: dict[str, str]) -> str:
    """Return one ``git rev-parse`` answer from the prefetched batch, asking Git only when it holds none."""
    return answers[probe] if probe in answers else _git(repository, "rev-parse", probe)


def _is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    """Return whether one verified Git object is reachable from another."""
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    _fail("git:merge-base")


def _patch_id(repository: Path, sha: str) -> str:
    """Return a stable patch ID without decoding or rewriting the patch's file bytes."""
    patch = subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "show", "--format=", "--no-ext-diff", sha],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if patch.returncode:
        _fail("git:show")
    stable = subprocess.run(["git", "patch-id", "--stable"], input=patch.stdout, capture_output=True, check=False)  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
    if stable.returncode or not stable.stdout.strip():
        _fail("patch-id")
    return stable.stdout.split()[0].decode("ascii")


def _blob_sha256(repository: Path, sha: str, path: str) -> str | None:
    """Return one tree blob digest, or ``None`` when the path is absent from that tree."""
    completed = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=repository, capture_output=True, check=False)  # noqa: S603, S607 - argv list, no shell; tool resolved via PATH on purpose
    if completed.returncode == 0:
        return hashlib.sha256(completed.stdout).hexdigest()
    if completed.returncode == 128:
        return None
    _fail("git:show")


def _tree_entry(repository: Path, sha: str, path: str) -> tuple[str, str, str] | None:
    """Return the exact literal-path mode, type, and object identity, including gitlinks."""
    record = _git(repository, "--literal-pathspecs", "ls-tree", "-z", sha, "--", path)
    if not record:
        return None
    fields = record.split("\t", 1)[0].split()
    if len(fields) != 3:
        _fail("git:ls-tree")
    return fields[0], fields[1], fields[2]


def _changed_paths(repository: Path, sha: str) -> set[str]:
    """Return the exact paths changed by one candidate patch, including root commits."""
    paths = (
        _git(repository, "diff-tree", "--root", "--no-commit-id", "-r", "--name-only", "-z", sha)
        .rstrip("\0")
        .split("\0")
    )
    if not paths or paths == [""]:
        _fail("released-paths")
    return set(paths)


def release_sections(text: str) -> list[tuple[str, int, int, int]]:
    """Locate level-two ATX or Setext sections outside fences with original character offsets."""
    sections = []
    paragraph = []
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        marker = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", content)
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker:
            fence = marker[1]
            paragraph = []
        else:
            heading = re.fullmatch(r" {0,3}##[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*", content)
            if heading:
                sections.append((heading[1], offset, offset + len(line)))
                paragraph = []
            elif re.fullmatch(r" {0,3}(?:-+|=+)[ \t]*", content) and paragraph:
                if content.lstrip().startswith("-"):
                    sections.append(
                        (" ".join(value.strip() for value, _start in paragraph), paragraph[0][1], offset + len(line))
                    )
                paragraph = []
            elif not content.strip() or re.match(
                r"^(?: {4}|\t| {0,3}(?:#{1,6}[ \t]|>|[-*+][ \t]|\d+[.)][ \t]))", content
            ):
                paragraph = []
            else:
                paragraph.append((content, offset))
        offset += len(line)
    return [
        (title, start, body, sections[index + 1][1] if index + 1 < len(sections) else len(text))
        for index, (title, start, body) in enumerate(sections)
    ]


def _nonempty_section(text: str, heading: str) -> bool:
    """Return whether one equivalent level-two Markdown section contains substantive content."""
    declared = release_sections(heading + "\n")
    if len(declared) != 1:
        return False
    matches = [section for section in release_sections(text) if section[0] == declared[0][0]]
    return len(matches) == 1 and bool(text[matches[0][2] : matches[0][3]].strip())


def _credit_lines(text: str) -> list[str]:
    """Read rendered credit lines outside fences without treating Unicode content as line endings."""
    lines = []
    fence = None
    # Markdown normalizes CR/LF and NUL for rendering. Receipt and excerpt bytes stay untouched;
    # other controls and Unicode separators remain data rather than invented paragraph boundaries.
    for line in re.split(r"\r\n?|\n", text.replace("\x00", "\ufffd")):
        marker = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip(" \t"):
                fence = None
        elif marker:
            fence = marker[1]
            lines.append("")
        else:
            lines.append(line)
    return lines


def _credit_closing(text: str, start: int, opener: str, closer: str) -> int | None:
    """Find a balanced label or destination boundary without treating escaped delimiters as syntax."""
    depth = 1
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == opener:
            depth += 1
        elif text[index] == closer:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _credit_link_end(text: str, start: int, *, inline: bool) -> int | None:
    """Read a complete link destination and optional title, rejecting malformed trailing text."""
    index = start
    # Unicode whitespace can be URL data; only Markdown separators split destination/title tokens.
    syntax_whitespace = " \t\r\n"
    while index < len(text) and text[index] in syntax_whitespace:
        index += 1
    if re.search(r"\n[ \t]*\n", text[start:index]):
        return None
    if inline and text[index : index + 1] == ")":
        return index + 1
    if text[index : index + 1] == "<":
        index += 1
        while index < len(text) and text[index] != ">":
            if text[index] == "\\" and index + 1 < len(text) and text[index + 1] in string.punctuation:
                index += 2
            elif text[index] in "<\r\n":
                return None
            else:
                index += 1
        if index == len(text):
            return None
        index += 1
    else:
        destination = index
        depth = 0
        while index < len(text) and text[index] not in syntax_whitespace:
            character = text[index]
            if character == "\\" and index + 1 < len(text) and text[index + 1] in string.punctuation:
                index += 2
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                if depth == 0:
                    break
                depth -= 1
            elif ord(character) < 32 or ord(character) == 127:
                return None
            index += 1
        if index == destination or depth:
            return None
    destination_end = index
    while index < len(text) and text[index] in syntax_whitespace:
        index += 1
    if re.search(r"\n[ \t]*\n", text[destination_end:index]):
        return None
    if inline and text[index : index + 1] == ")":
        return index + 1
    if not inline and index == len(text):
        return index
    if index == destination_end or text[index : index + 1] not in {'"', "'", "("}:
        return None
    opener = text[index]
    closer = ")" if opener == "(" else opener
    title_start = index
    index += 1
    while index < len(text) and text[index] != closer:
        if text[index] == "\\" and index + 1 < len(text) and text[index + 1] in string.punctuation:
            index += 2
        elif opener == "(" and text[index] == "(":
            return None
        else:
            index += 1
    if index == len(text) or re.search(r"\n[ \t]*\n", text[title_start:index]):
        return None
    index += 1
    title_end = index
    while index < len(text) and text[index] in syntax_whitespace:
        index += 1
    if re.search(r"\n[ \t]*\n", text[title_end:index]):
        return None
    if inline and text[index : index + 1] == ")":
        return index + 1
    return index if not inline and index == len(text) else None


def _credit_references(text: str) -> set[str]:
    """Resolve complete reference definitions at paragraph boundaries outside fenced examples."""
    lines = _credit_lines(text)
    references = set()
    index = 0
    boundary = True
    while index < len(lines):
        line = lines[index]
        definition = re.match(r"^ {0,3}\[((?:\\[^\r\n]|[^\[\]\\])+)\]:", line) if boundary else None
        if definition and definition[1].strip() and len(definition[1]) <= 999:
            candidate = line
            end = index
            valid_end = None
            while True:
                if _credit_link_end(candidate, definition.end(), inline=False) is not None:
                    valid_end = end
                end += 1
                if end == len(lines) or not lines[end].strip(" \t"):
                    break
                candidate += "\n" + lines[end]
            if valid_end is not None:
                references.add(" ".join(definition[1].split()).casefold())
                boundary = True
                index = valid_end + 1
            else:
                boundary = False
                index += 1
            continue
        boundary = not line.strip(" \t") or bool(re.match(r"^ {0,3}(?:#{1,6}[ \t]|[-=]+[ \t]*$)", line))
        index += 1
    return references


def _credit_link_span(text: str, index: int, references: set[str]) -> tuple[str, int] | None:
    """Recognize one complete supported link candidate before applying the no-nested-links rule."""
    if text[index] != "[" or (index and text[index - 1] == "!"):
        return None
    close = _credit_closing(text, index, "[", "]")
    if close is None:
        return None
    label = text[index + 1 : close]
    key = " ".join(label.split()).casefold()
    end = close
    linked = key in references
    if text[close + 1 : close + 2] == "(":
        destination = _credit_link_end(text, close + 2, inline=True)
        linked = destination is not None
        end = destination - 1 if destination is not None else close
    elif text[close + 1 : close + 2] == "[":
        reference = _credit_closing(text, close + 1, "[", "]")
        reference_key = text[close + 2 : reference] if reference is not None else ""
        linked = reference is not None and " ".join((reference_key or label).split()).casefold() in references
        end = reference if reference is not None else close
    return (label, end) if linked else None


def _credit_label_text(text: str, references: set[str], *, stop_at_description: bool = False) -> str:
    """Read a complete visible label, resolving supported links and balanced emphasis before comparison."""
    output = []
    delimiters = []
    index = 0
    while index < len(text):
        if stop_at_description and re.match(r"(?:[ \t]+[—–-][ \t]+|:[ \t]+)", text[index:]):
            break
        if stop_at_description and text[index : index + 2] == " (":
            annotation = _credit_closing(text, index + 1, "(", ")")
            if annotation is not None:
                handle = _credit_label_text(text[index + 2 : annotation], references)
                tail = text[annotation + 1 :]
                if re.fullmatch(r"@[A-Za-z0-9_-]+", handle) and (
                    not tail.strip() or re.match(r"(?:[ \t]+[—–-][ \t]+|:[ \t]+)", tail)
                ):
                    break
        character = text[index]
        if character == "\\" and index + 1 < len(text) and text[index + 1] in string.punctuation:
            output.append(text[index + 1])
            index += 2
            continue
        link = _credit_link_span(text, index, references)
        if link is not None:
            label, end = link
            inner = 0
            while inner < len(label):
                if label[inner] == "\\":
                    inner += 2
                elif _credit_link_span(label, inner, references) is not None:
                    break
                else:
                    inner += 1
            # An active inner link deactivates its outer link. Leave the outer syntax in the ordinary
            # text stream so its brackets and destination remain part of the complete visible identity.
            if inner >= len(label):
                output.append(_credit_label_text(label, references))
                index = end + 1
                continue
        if character in "*_":
            run = re.match(r"\*+|_+", text[index:])[0]
            end = index + len(run)
            before = text[index - 1] if index else "\n"
            after = text[end] if end < len(text) else "\n"
            before_punctuation = before in string.punctuation or unicodedata.category(before)[0] in "PS"
            after_punctuation = after in string.punctuation or unicodedata.category(after)[0] in "PS"
            left = not after.isspace() and (not after_punctuation or before.isspace() or before_punctuation)
            right = not before.isspace() and (not before_punctuation or after.isspace() or after_punctuation)
            can_open = left and (character == "*" or not right or before_punctuation)
            can_close = right and (character == "*" or not left or after_punctuation)
            delimiters.append((len(output), character, can_open, can_close))
            output.append(run)
            index = end
            continue
        output.append(character)
        index += 1
    # Shared closing runs can close inner emphasis and outer strong emphasis. Pair only flanking runs,
    # preserve every unmatched marker, and forbid crossing pairs or ambiguous multiples of three.
    openers = []
    for position, marker, can_open, can_close in delimiters:
        if can_close:
            while output[position]:
                match = None
                for opener_index in range(len(openers) - 1, -1, -1):
                    opener, opener_marker, opener_closes = openers[opener_index]
                    if opener_marker != marker:
                        continue
                    opening = len(output[opener])
                    closing = len(output[position])
                    if (opener_closes or can_open) and (opening + closing) % 3 == 0 and (opening % 3 or closing % 3):
                        continue
                    match = opener_index
                    break
                if match is None:
                    break
                opener = openers[match][0]
                used = 2 if len(output[opener]) >= 2 and len(output[position]) >= 2 else 1
                output[opener] = output[opener][used:]
                output[position] = output[position][used:]
                del openers[match + 1 :]
                if not output[opener]:
                    openers.pop()
        if can_open and output[position]:
            openers.append((position, marker, can_close))
    return " ".join(html.unescape("".join(output)).split())


def _visible_credit_name(text: str, name: str, *, context: str | None = None) -> bool:
    """Compare complete rendered credit labels with the verified identity, never marked substrings."""
    definition = re.compile(r"^ {0,3}\[(.+)\]:")
    references = _credit_references(context if context is not None else text)
    for line in _credit_lines(text):
        if definition.match(line):
            continue
        label = re.sub(r"^[ \t]*[-*+][ \t]+", "", line).strip()
        if _credit_label_text(label, references, stop_at_description=True) == " ".join(name.split()):
            return True
    return False


def _identity_hash(name: str, email: str) -> str:
    """Produce the internal, normalized identity binding for one person."""
    return hashlib.sha256(f"{name}\0{email.casefold()}".encode()).hexdigest()


def _eligible_identities(repository: Path, candidates: set[str]) -> dict[str, str]:
    """Derive every author/coauthor identity before separating human and bot credits."""
    identities: dict[str, str] = {}
    for candidate in candidates:
        record = _git(
            repository, "show", "-s", "--format=%aN%x00%aE%x00%(trailers:key=Co-authored-by,valueonly)", candidate
        )
        author_name, author_email, trailers = record.split("\x00", maxsplit=2)
        if author_name and author_email:
            identities[_identity_hash(author_name, author_email)] = author_name
        for name, email in re.findall(r"(.*?)\s*<([^>]+)>", trailers):
            identities[_identity_hash(name.strip(), email.strip())] = name.strip()
    return identities


def _require_receipt(metadata: dict[str, Any]) -> dict[str, Any]:
    """Extract the supported version-one receipt from result metadata."""
    receipt = metadata.get("release_evidence")
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        _fail("receipt")
    return receipt


def _validate_scope(receipt: dict[str, Any], metadata: dict[str, Any]) -> tuple[Path, set[str], set[str]]:
    """Bind a pinned repository, ancestor range, and complete candidate inventory."""
    raw_repository = receipt.get("repository")
    if not isinstance(raw_repository, str) or not Path(raw_repository).is_absolute():
        _fail("repository")
    repository = Path(raw_repository)
    if not repository.is_dir():
        _fail("repository")
    head = metadata.get("release_head")
    answers = _prefetch_rev_parse(repository, head)
    if _rev_parse(repository, "--is-inside-work-tree", answers) != "true":
        _fail("repository")
    if not isinstance(head, str) or not _SHA.fullmatch(head) or _rev_parse(repository, "HEAD", answers) != head:
        _fail("head")
    final_tree = receipt.get("final_tree")
    if not isinstance(final_tree, str) or _rev_parse(repository, f"{head}^{{tree}}", answers) != final_tree:
        _fail("final-tree")
    baseline = receipt.get("baseline")
    if baseline is not None and (not isinstance(baseline, str) or not _SHA.fullmatch(baseline)):
        _fail("baseline")
    if baseline is not None and not _is_ancestor(repository, baseline, head):
        _fail("baseline-ancestry")
    expected = set(_git(repository, "rev-list", f"{baseline}..{head}" if baseline else head).splitlines())
    rows = receipt.get("candidates")
    if not isinstance(rows, list) or (not rows and expected):
        _fail("candidates")
    recorded: set[str] = set()
    included: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            _fail("candidate-row")
        sha, disposition, reason = row.get("sha"), row.get("disposition"), row.get("reason")
        if not isinstance(sha, str) or sha not in expected or sha in recorded:
            _fail("candidate-set")
        if disposition not in _DISPOSITIONS or not isinstance(reason, str) or not reason.strip():
            _fail("candidate-disposition")
        recorded.add(sha)
        if disposition == "included":
            included.add(sha)
        if disposition == "already-released":
            released_head = receipt.get("released_head")
            released_sha, comparison = row.get("released_sha"), row.get("comparison")
            if (
                not isinstance(released_head, str)
                or not _SHA.fullmatch(released_head)
                or not isinstance(released_sha, str)
                or not _SHA.fullmatch(released_sha)
                or comparison not in {"identity", "stable-patch"}
            ):
                _fail("released-binding")
            if not _is_ancestor(repository, released_sha, released_head):
                _fail("released-reachability")
            if comparison == "identity" and released_sha != sha:
                _fail("released-identity")
            if comparison == "stable-patch" and _patch_id(repository, released_sha) != _patch_id(repository, sha):
                _fail("released-patch")
            published_tree = row.get("published_tree")
            path_rows = row.get("published_paths")
            if (
                not isinstance(published_tree, str)
                or _git(repository, "rev-parse", f"{released_head}^{{tree}}") != published_tree
                or not isinstance(path_rows, list)
            ):
                _fail("released-tree")
            paths: dict[str, str | None] = {}
            for path_row in path_rows:
                if (
                    not isinstance(path_row, dict)
                    or not isinstance(path_row.get("path"), str)
                    or path_row["path"] in paths
                    or (
                        path_row.get("sha256") is not None
                        and (
                            not isinstance(path_row["sha256"], str)
                            or not re.fullmatch(r"[0-9a-f]{64}", path_row["sha256"])
                        )
                    )
                    or (
                        path_row.get("mode") is not None
                        and (not isinstance(path_row["mode"], str) or not re.fullmatch(r"[0-7]{6}", path_row["mode"]))
                    )
                    or (path_row.get("object_type") is not None and path_row["object_type"] not in {"blob", "commit"})
                ):
                    _fail("released-path-row")
                paths[path_row["path"]] = path_row.get("sha256")
            if set(paths) != _changed_paths(repository, sha):
                _fail("released-paths")
            for path, digest in paths.items():
                row = next(item for item in path_rows if item["path"] == path)
                candidate_entry = _tree_entry(repository, sha, path)
                published_entry = _tree_entry(repository, released_head, path)
                if (
                    digest != _blob_sha256(repository, sha, path)
                    or digest != _blob_sha256(repository, released_head, path)
                    or candidate_entry != published_entry
                    or (candidate_entry[:2] if candidate_entry else (None, None))
                    != (row.get("mode"), row.get("object_type"))
                ):
                    _fail("released-tree-path")
    if recorded != expected:
        _fail("candidate-set")
    return repository, expected, included


def _validate_claims(receipt: dict[str, Any], out_dir: Path, included: set[str]) -> None:
    """Bind every included candidate to a nonempty retained-artifact claim excerpt."""
    claims = receipt.get("claims")
    if not isinstance(claims, list):
        _fail("claims")
    claimed: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            _fail("claim-row")
        artifact, excerpt, sha, candidates = (
            claim.get("artifact"),
            claim.get("excerpt"),
            claim.get("sha256"),
            claim.get("candidate_shas"),
        )
        deliverables = (out_dir / "deliverables").resolve()
        path = deliverables / str(artifact)
        if (
            not isinstance(artifact, str)
            or Path(artifact).name != artifact
            or not isinstance(excerpt, str)
            or not excerpt.strip()
            or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or not isinstance(candidates, list)
            or not candidates
            or any(not isinstance(item, str) for item in candidates)
            or len(candidates) != len(set(candidates))
        ):
            _fail("claim-row")
        if (
            not path.is_file()
            or path.resolve().parent != deliverables
            or _sha256(path) != sha
            or excerpt not in path.read_text(encoding="utf-8")
        ):
            _fail("claim-binding")
        if not all(isinstance(item, str) and item in included for item in candidates):
            _fail("claim-candidate")
        claimed.update(candidates)
    if claimed != included:
        _fail("claim-coverage")


def _validate_scope_record(out_dir: Path, receipt: dict[str, Any], head: str, candidates: set[str]) -> None:
    """Require scope prose to visibly retain receipt coordinates rather than headings alone."""
    scope = out_dir / "release-scope.md"
    text = scope.read_text(encoding="utf-8")
    required = {head, receipt["final_tree"], *candidates}
    baseline = receipt.get("baseline")
    if baseline is not None:
        required.add(baseline)
    released_head = receipt.get("released_head")
    if isinstance(released_head, str):
        required.add(released_head)
    if not all(value in text for value in required):
        _fail("scope-record")


def _validate_artifacts(receipt: dict[str, Any], out_dir: Path, requested: set[str]) -> dict[str, Path]:
    """Bind each selected retained artifact to its declared final destination bytes."""
    rows = receipt.get("artifacts")
    if not isinstance(rows, list):
        _fail("artifacts")
    paths: dict[str, Path] = {}
    for row in rows:
        if not isinstance(row, dict):
            _fail("artifact-row")
        name, destination, digest = row.get("name"), row.get("destination"), row.get("sha256")
        if (
            not isinstance(name, str)
            or not isinstance(destination, str)
            or not Path(destination).is_absolute()
            or not isinstance(digest, str)
        ):
            _fail("artifact-row")
        retained = out_dir / "deliverables" / name
        final = Path(destination)
        if (
            name in paths
            or not retained.is_file()
            or not final.is_file()
            or _sha256(retained) != digest
            or _sha256(final) != digest
        ):
            _fail("artifact-binding")
        paths[name] = final
    if set(paths) != requested:
        _fail("artifact-set")
    for name, path in paths.items():
        text = path.read_text(encoding="utf-8")
        if name == "SUMMARY.md":
            if not _nonempty_section(text, "## Overview") or not _nonempty_section(text, "## Benefits"):
                _fail("summary-role")
        if name == "MIGRATION.md" and not (
            _nonempty_section(text, "## Actions") or _nonempty_section(text, "## No migration required")
        ):
            _fail("migration-role")
        if name == "CHANGELOG.md" and not release_sections(text):
            _fail("changelog-role")
    return paths


def _pr_author_identities(receipt: dict[str, Any], candidates: set[str]) -> dict[str, str]:
    """Collect receipt-recorded PR author identities associated with candidate commits."""
    rows = receipt.get("pr_authors", [])
    if not isinstance(rows, list):
        _fail("pr-authors")
    identities: dict[str, str] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("sha") not in candidates
            or not isinstance(row.get("identity_hash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", row["identity_hash"])
            or not isinstance(row.get("display_name"), str)
            or not row["display_name"].strip()
            or ("is_bot" in row and not isinstance(row["is_bot"], bool))
        ):
            _fail("pr-author-row")
        identities[row["identity_hash"]] = row["display_name"].strip()
    return identities


def _validate_bots(
    receipt: dict[str, Any], eligible: dict[str, str], out_dir: Path, artifacts: dict[str, Path]
) -> set[str]:
    """Bind classified automation identities to one aggregate credit, never human entries."""
    rows = receipt.get("bots", [])
    if not isinstance(rows, list):
        _fail("bot-inventory")
    identities: set[str] = set()
    labels: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            _fail("bot-row")
        identity, name, label = row.get("identity_hash"), row.get("display_name"), row.get("label")
        if (
            not isinstance(identity, str)
            or identity not in eligible
            or identity in identities
            or name != eligible[identity]
            or not isinstance(label, str)
            or not label.strip()
            or any(character in label for character in "\r\n,*")
        ):
            _fail("bot-row")
        proof = row.get("classification_evidence")
        if not isinstance(proof, dict) or not isinstance(proof.get("path"), str):
            _fail("bot-classification-proof")
        proof_path = out_dir / proof["path"]
        if (
            not proof_path.is_file()
            or not proof_path.resolve().is_relative_to(out_dir.resolve())
            or proof.get("sha256") != _sha256(proof_path)
            or not isinstance(proof.get("excerpt"), str)
            or not proof["excerpt"].strip()
            or proof["excerpt"] not in proof_path.read_text(encoding="utf-8")
        ):
            _fail("bot-classification-proof")
        identities.add(identity)
        labels.add(label)
    required = {identity for identity, name in eligible.items() if "[bot]" in name.casefold()}
    required.update(
        row["identity_hash"]
        for row in receipt.get("pr_authors", [])
        if row.get("is_bot") is True and row["identity_hash"] in eligible
    )
    if not required <= identities:
        _fail("bot-coverage")
    # Only the draft owns public contributor credits; audit/demo record them privately.
    target = artifacts.get("DRAFT.md", out_dir / "contributors.md")
    text = target.read_text(encoding="utf-8")
    lines = [re.sub(r"^(\*{1,2}|_{1,2})(.*)\1$", r"\2", line.strip()) for line in re.split(r"\r\n?|\n", text)]
    aggregate_credits = [
        line.removeprefix("Automated contributions:").strip()
        for line in lines
        if line.startswith("Automated contributions:")
    ]
    declared = [label.strip() for label in aggregate_credits[0].split(",")] if len(aggregate_credits) == 1 else []
    if (identities and (len(aggregate_credits) != 1 or len(declared) != len(labels) or set(declared) != labels)) or (
        not identities and aggregate_credits
    ):
        _fail("bot-aggregate-credit")
    if any(
        re.match(r"^[ \t]*[-*+][ \t]+", line) and _visible_credit_name(line, eligible[identity], context=text)
        for identity in identities
        for line in re.split(r"\r\n?|\n", text)
    ):
        _fail("bot-individual-credit")
    return identities


def _validate_contributors(
    receipt: dict[str, Any], repository: Path, out_dir: Path, candidates: set[str], artifacts: dict[str, Path]
) -> None:
    """Reconcile human credit/exclusion and separate aggregate automation accounting."""
    rows = receipt.get("contributors")
    if not isinstance(rows, list):
        _fail("contributors")
    recorded: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("identity_hash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", row["identity_hash"])
            or not isinstance(row.get("display_name"), str)
            or not row["display_name"].strip()
        ):
            _fail("contributor-row")
        identity = row["identity_hash"]
        disposition = row.get("disposition")
        if identity in recorded or disposition not in {"credited", "excluded"}:
            _fail("contributor-row")
        if disposition == "credited" and (
            not isinstance(row.get("credit_excerpt"), str) or not row["credit_excerpt"].strip()
        ):
            _fail("contributor-credit")
        if disposition == "excluded" and (
            row.get("exclusion_basis") not in _EXCLUSION_BASES
            or not isinstance(row.get("exclusion"), str)
            or not row["exclusion"].strip()
        ):
            _fail("contributor-exclusion")
        recorded.add(identity)
    new_candidates = {row["sha"] for row in receipt["candidates"] if row["disposition"] != "already-released"}
    eligible = _eligible_identities(repository, new_candidates)
    for identity, display_name in _pr_author_identities(receipt, candidates).items():
        if not any(
            row["identity_hash"] == identity and row["sha"] in new_candidates for row in receipt.get("pr_authors", [])
        ):
            continue
        if identity in eligible and eligible[identity] != display_name:
            _fail("pr-author-alias")
        eligible[identity] = display_name
    bots = _validate_bots(receipt, eligible, out_dir, artifacts)
    if recorded & bots:
        _fail("bot-human-overlap")
    if recorded != set(eligible) - bots:
        _fail("contributor-coverage")
    public_documents = [path.read_text(encoding="utf-8") for name, path in artifacts.items() if name != "demo.py"]
    public = "\n".join(public_documents)
    exclusions = (out_dir / "contributors.md").read_text(encoding="utf-8")
    credit_documents = public_documents or [exclusions]
    for row in rows:
        if row["display_name"].strip() != eligible[row["identity_hash"]]:
            _fail("contributor-display-name")
        if row["disposition"] == "credited" and not any(
            row["credit_excerpt"] in document
            and _visible_credit_name(row["credit_excerpt"], row["display_name"].strip(), context=document)
            and _visible_credit_name(document, row["display_name"].strip())
            for document in credit_documents
        ):
            _fail("contributor-credit")
        if row["disposition"] == "excluded" and row["exclusion"] not in exclusions:
            _fail("contributor-exclusion")
        if row["disposition"] == "excluded":
            proof = row.get("exclusion_evidence")
            if not isinstance(proof, dict):
                _fail("contributor-exclusion-proof")
            proof_path = out_dir / str(proof.get("path", ""))
            if (
                not proof_path.is_file()
                or not proof_path.resolve().is_relative_to(out_dir.resolve())
                or proof.get("sha256") != _sha256(proof_path)
                or not isinstance(proof.get("excerpt"), str)
                or not proof["excerpt"].strip()
                or proof["excerpt"] not in proof_path.read_text(encoding="utf-8")
            ):
                _fail("contributor-exclusion-proof")
    if re.search(r"[\w.+-]+@[\w.-]+", public):
        _fail("public-email")


def _changelog_sections(data: bytes) -> dict[bytes, bytes]:
    """Split canonical level-two version sections without normalizing their bytes."""
    text = data.decode("utf-8")
    sections: dict[bytes, bytes] = {}
    for title, start, _body, end in release_sections(text):
        heading = ("## " + title).encode("utf-8")
        if heading in sections:
            _fail("changelog-duplicate-heading")
        section = text[start:end].encode("utf-8")
        # Reference definitions are validated separately and included in the release excerpt below.
        lines = section.splitlines(keepends=True)
        while lines and (not lines[-1].strip() or re.match(rb"^\[[^]\r\n]+\]:[ \t]+", lines[-1])):
            lines.pop()
        sections[heading] = b"".join(lines)
    return sections


def _validate_changelog(receipt: dict[str, Any], artifacts: dict[str, Path]) -> None:
    """Require exact historical sections and reference definitions to survive changelog editing."""
    changelog = receipt.get("changelog")
    if not isinstance(changelog, dict):
        _fail("changelog")
    if changelog.get("destination") is None:
        if (
            changelog.get("backup") is not None
            or not isinstance(changelog.get("absent_reason"), str)
            or not changelog["absent_reason"].strip()
        ):
            _fail("changelog-absence")
        if "CHANGELOG.md" in artifacts:
            text = artifacts["CHANGELOG.md"].read_text(encoding="utf-8")
            heading = changelog.get("target_heading")
            if not isinstance(heading, str) or not _nonempty_section(text, heading):
                _fail("changelog-target")
        return
    for key in ("backup", "destination"):
        value = changelog.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
            _fail("changelog-path")
    backup, final = Path(changelog["backup"]), Path(changelog["destination"])
    if changelog.get("backup_sha256") != _sha256(backup) or changelog.get("final_sha256") != _sha256(final):
        _fail("changelog-digest")
    old, current = backup.read_bytes(), final.read_bytes()
    heading = changelog.get("target_heading")
    declared = release_sections(heading + "\n") if isinstance(heading, str) else []
    if len(declared) != 1:
        _fail("changelog-target")
    target = ("## " + declared[0][0]).encode("utf-8")
    before, after = _changelog_sections(old), _changelog_sections(current)
    if target not in after or not _nonempty_section(after[target].decode("utf-8"), heading):
        _fail("changelog-target")
    mutable = {target, b"## Unreleased", b"## [Unreleased]"}
    old_detail, new_detail = Counter(), Counter()
    for entries, details in ((before, old_detail), (after, new_detail)):
        for key, section in entries.items():
            if key in mutable:
                text = section.decode("utf-8")
                body = release_sections(text)[0][2]
                details.update(line for line in text[body:].encode("utf-8").splitlines(keepends=True) if line.strip())
    if old_detail - new_detail:
        _fail("changelog-target-detail")
    for key, section in before.items():
        if key not in mutable and (key not in after or section.rstrip(b"\r\n") not in after[key]):
            _fail("changelog-history")
    references = re.findall(rb"(?m)^\[[^]\r\n]+\]:[ \t]+[^\r\n]+", old)
    if any(reference not in current for reference in references):
        _fail("changelog-history")
    if "CHANGELOG.md" in artifacts:
        all_references = re.findall(rb"(?m)^\[[^]\r\n]+\]:[ \t]+[^\r\n]+", current)
        excerpt = after[target].rstrip(b"\r\n") + b"\n"
        all_references = [reference for reference in all_references if reference not in excerpt.splitlines()]
        if all_references:
            excerpt += b"\n" + b"\n".join(all_references) + b"\n"
        if artifacts["CHANGELOG.md"].read_bytes() != excerpt:
            _fail("changelog-excerpt")


def _validate_demo(receipt: dict[str, Any], artifacts: dict[str, Path]) -> None:
    """Bind a selected demo to a successful helper-produced execution receipt and output."""
    if "demo.py" not in artifacts:
        return
    demo = receipt.get("demo")
    if not isinstance(demo, dict) or demo.get("sha256") != _sha256(artifacts["demo.py"]):
        _fail("demo-receipt")
    command = [demo.get("interpreter"), str(artifacts["demo.py"].resolve())]
    if (
        type(demo.get("exit_code")) is not int
        or demo["exit_code"] != 0
        or demo.get("command") != command
        or not all(
            isinstance(demo.get(key), str) and demo[key]
            for key in ("interpreter", "environment", "cwd", "output", "execution_receipt")
        )
    ):
        _fail("demo-execution")
    output = Path(demo["output"])
    receipt = Path(demo.get("execution_receipt", ""))
    if (
        not output.is_file()
        or not isinstance(demo.get("output_sha256"), str)
        or demo["output_sha256"] != _sha256(output)
        or not receipt.is_file()
        or not isinstance(demo.get("execution_receipt_sha256"), str)
        or demo["execution_receipt_sha256"] != _sha256(receipt)
    ):
        _fail("demo-output")
    try:
        execution = json.loads(receipt.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail("demo-execution-receipt")
    if not isinstance(execution, dict) or execution != {
        "command": demo["command"],
        "cwd": demo["cwd"],
        "environment": demo["environment"],
        "exit_code": 0,
        "interpreter": demo["interpreter"],
        "output_sha256": demo["output_sha256"],
        "script_sha256": demo["sha256"],
        "script_sha256_after": demo["sha256"],
    }:
        _fail("demo-execution-receipt")
    try:
        ast.parse(artifacts["demo.py"].read_text(encoding="utf-8"))
    except SyntaxError:
        _fail("demo-python")


def validate_release_evidence(metadata: dict[str, Any], out_dir: Path, requested_artifacts: set[str]) -> None:
    """Validate the release-specific receipt required for a passing contract-v1 result."""
    receipt = _require_receipt(metadata)
    repository, candidates, included = _validate_scope(receipt, metadata)
    _validate_scope_record(out_dir, receipt, metadata["release_head"], candidates)
    if not requested_artifacts:
        _validate_contributors(receipt, repository, out_dir, candidates, {})
        _validate_changelog(receipt, {})
        return
    artifacts = _validate_artifacts(receipt, out_dir, requested_artifacts)
    _validate_claims(receipt, out_dir, included)
    _validate_contributors(receipt, repository, out_dir, candidates, artifacts)
    _validate_changelog(receipt, artifacts)
    _validate_demo(receipt, artifacts)


def record_demo(
    script: Path, interpreter: Path, cwd: Path, environment: str, out: Path, timeout: int = 600
) -> dict[str, Any]:
    """Execute one explicitly authorized local Python demo and retain digest-bound evidence.

    Output uses a new directory; existing evidence is never overwritten. Execution inherits the caller's environment and
    permissions, with no dependency installation or network grant. A timeout or nonzero exit is retained as failed
    evidence. Validation never calls this execution entrypoint implicitly.
    """
    script, interpreter, cwd, out = script.resolve(), interpreter.absolute(), cwd.resolve(), out.resolve()
    if (
        not script.is_file()
        or not interpreter.is_file()
        or not cwd.is_dir()
        or not environment.strip()
        or not 0 < timeout <= 600
    ):
        _fail("demo-input")
    digest = _sha256(script)
    out.mkdir(parents=True, exist_ok=False)
    output = out / "output.txt"
    command = [str(interpreter), str(script)]
    with output.open("xb") as stream:
        process = subprocess.Popen(  # noqa: S603 - argv list, no shell
            command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, start_new_session=os.name != "nt"
        )
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_process(process, sys.platform)
            exit_code = 124
    execution = {
        "command": command,
        "cwd": str(cwd),
        "environment": environment,
        "exit_code": exit_code,
        "interpreter": str(interpreter),
        "output_sha256": _sha256(output),
        "script_sha256": digest,
        "script_sha256_after": _sha256(script),
    }
    execution_path = out / "execution.json"
    execution_path.write_text(json.dumps(execution, indent=2) + "\n", encoding="utf-8", newline="\n")
    receipt = {
        **{
            key: execution[key]
            for key in ("command", "cwd", "environment", "exit_code", "interpreter", "output_sha256")
        },
        "sha256": digest,
        "output": str(output),
        "execution_receipt": str(execution_path),
        "execution_receipt_sha256": _sha256(execution_path),
    }
    (out / "demo.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n")
    return receipt


def main() -> int:
    """Record a single authorized demo; report failure without retrying or promoting it."""
    parser = argparse.ArgumentParser(description="Record an explicitly authorized local demo execution.")
    parser.add_argument("action", choices=["record-demo"])
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()
    receipt = record_demo(args.script, args.python, args.cwd, args.environment, args.out, args.timeout_seconds)
    print(json.dumps(receipt))
    return 0 if receipt["exit_code"] == 0 and receipt["sha256"] == _sha256(args.script) else 1


if __name__ == "__main__":
    raise SystemExit(main())
