#!/usr/bin/env python3
"""Read a frozen code-review context through auditable native child tool calls.

## Purpose

Bind a reviewer's short dispatch instruction to the exact bytes of a previously prepared role card and bounded source
context. The child reads bounded pages in order and receives a provenance header computed from the frozen plan on each
page.

## Scope

This module only reads a plan and its declared context. It does not inspect a repository, change permissions, write
artifacts, or run review code. The parent uses its pure rendering functions to construct and validate the permitted
child tool invocations and expected tool responses.

## Usage

The review prepare step calls ``dispatch_message`` with the frozen inspection plan path, role, attempt, and Python
interpreter. The child copies each exact embedded ``functions.exec`` source in order. Those calls invoke this module
with ``--plan``, ``--role``, ``--attempt``, and a page number under the same interpreter.

## Outputs

Standard output contains exact UTF-8 bytes: the provenance header and page position followed by one context slice,
without locale-dependent encoding or newline translation. The parent validator checks all child calls and returned text
against the same frozen plan and recorded SHA-256 digest.

## Failure

Invalid roles, paths, context digests, or plan fields exit nonzero; no partial context is emitted. The validator
independently rejects missing, altered, repeated, unordered, or additional child tool calls and outputs.

## Used by

The code-review preparation workflow, native inspection children, and ``validate_artifacts.py``. This module's CLI is a
read-only transport for bounded review evidence, not a host-enforced isolation boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

PAGE_BYTES = 6000


def context_pages(context_text: str) -> list[str]:
    """Split UTF-8 context at character boundaries into bounded byte pages."""
    content = context_text.encode("utf-8")
    pages: list[str] = []
    while content:
        end = min(PAGE_BYTES, len(content))
        while end > 0:
            try:
                page = content[:end].decode("utf-8")
            except UnicodeDecodeError:
                end -= 1
                continue
            pages.append(page)
            content = content[end:]
            break
        else:
            raise ValueError("review-context-character-exceeds-page")
    return pages or [""]


def render_read_output(
    context_text: str,
    role: str,
    run_id: str,
    input_sha256: str,
    context_sha256: str,
    attempt: int,
    page: int = 1,
) -> str:
    """Prepend provenance and page position to one bounded context slice."""
    header = (
        f"<!-- codex-review-provenance role={role} run={run_id} "
        f"input={input_sha256} context={context_sha256} attempt={attempt} -->"
    )
    pages = context_pages(context_text)
    if not 1 <= page <= len(pages):
        raise ValueError("review-context-page-invalid")
    if len(pages) == 1:
        return f"{header}\n{pages[0]}"
    output = f"{header}\n<!-- codex-review-context-page {page}/{len(pages)} -->\n{pages[page - 1]}"
    if len(output.encode("utf-8")) > 8192:
        raise ValueError("review-context-page-output-too-large")
    return output


def render_read_call(
    plan_path: Path,
    role: str,
    attempt: int = 1,
    python_executable: str = sys.executable,
    page: int = 1,
    reader_path: Path | None = None,
) -> str:
    """Render one exact native tool call for the selected context page."""
    argv = [
        python_executable,
        str((reader_path or Path(__file__)).resolve()),
        "--plan",
        str(plan_path.resolve()),
        "--role",
        role,
        "--attempt",
        str(attempt),
    ]
    if page != 1:
        argv.extend(["--page", str(page)])
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    args = {"cmd": command, "workdir": str(plan_path.resolve().parent), "max_output_tokens": 10000}
    return (
        '// @exec: {"max_output_tokens": 10000}\n'
        f"const r = await tools.exec_command({json.dumps(args, ensure_ascii=False)}); text(r.output);"
    )


def dispatch_message(
    plan_path: Path,
    role: str,
    attempt: int = 1,
    python_executable: str = sys.executable,
    *,
    provenance_header: bool = True,
    reader_path: Path | None = None,
    _all_page_calls: bool = True,
) -> str:
    """Render exact page calls or the issued recipe for a known historical reader."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    entries = [entry for entry in plan["contexts"] if entry.get("role_id") == role]
    if len(entries) != 1:
        raise ValueError("review-context-role-count")
    context = (plan_path.parent / entries[0]["context_path"]).read_bytes().decode("utf-8")
    count = len(context_pages(context))
    first_call = render_read_call(plan_path, role, attempt, python_executable, reader_path=reader_path)
    later_pages = (
        f"For pages 2 through {count}, copy the same JavaScript source once per page in order. "
        "In each copied source, append ` --page N` to the end of the `cmd` string, replacing N with that page's "
        "actual number. Keep every other byte of the JavaScript source unchanged."
        if count > 1
        else ""
    )
    final_instruction = (
        "all pages. Begin your final answer with the provenance header printed by the reads; do not include the "
        "context body in the final answer."
        if provenance_header
        else "all pages. Return your findings and Reviewer Assessment; provenance is derived from the audited reads. "
        "Do not include the context body in the final answer."
    )
    if _all_page_calls:
        calls = "\n\n".join(
            f"```javascript\n{render_read_call(plan_path, role, attempt, python_executable, page, reader_path)}\n```"
            for page in range(1, count + 1)
        )
        return (
            f"Read all {count} frozen review context pages in order with exactly one functions.exec call per page. "
            "Copy each complete JavaScript block below exactly once, in order, without editing its command or path. "
            "Use no other tools. Read each full output before the next call and review only after "
            f"{final_instruction}\n\n{calls}"
        )
    return (
        f"Read all {count} frozen review context pages in order with exactly one functions.exec call per page, "
        "starting with the exact source below. Use no other tools. Read each full output before the next call and review only after "
        f"{final_instruction}\n{later_pages}\n```javascript\n{first_call}\n```"
    )


def read_context(plan_path: Path, role: str, attempt: int, page: int = 1) -> str:
    """Read one plan-selected page after checking the full context digest."""
    if attempt < 1 or not role or any(char in role for char in "/\\\n\r"):
        raise ValueError("invalid-review-context-identity")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    entries = [entry for entry in plan["contexts"] if entry.get("role_id") == role]
    if len(entries) != 1:
        raise ValueError("review-context-role-count")
    entry = entries[0]
    context_path = (plan_path.parent / entry["context_path"]).resolve()
    if not context_path.is_relative_to(plan_path.parent.resolve()):
        raise ValueError("review-context-outside-plan")
    content = context_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != entry["context_sha256"]:
        raise ValueError("review-context-hash-mismatch")
    return render_read_output(
        content.decode("utf-8"), role, plan["review_run_id"], plan["review_input_sha256"], digest, attempt, page
    )


def main() -> None:
    """Emit one verified frozen context as exact UTF-8 bytes on every host."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--page", type=int, default=1)
    args = parser.parse_args()
    try:
        sys.stdout.buffer.write(read_context(args.plan, args.role, args.attempt, args.page).encode("utf-8"))
    except (KeyError, OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        parser.exit(1, f"review-context-read-failed:{error}\n")


if __name__ == "__main__":
    main()
