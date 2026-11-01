"""Execute producer-supplied native page commands without a redundant working directory."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import test_review_prepare as preparation

LEGACY_READER_SHA256 = "c185dc007a261a2d9c0e449e5a888a7a771cd99336ebe8c7085432bc2b0d43c8"
EXEC_PRAGMA = '// @exec: {"max_output_tokens": 10000}\n'


def _javascript_fences(message: str) -> list[str]:
    """Return the body of every ```javascript fenced block in a dispatch message."""
    return [block.split("\n```", 1)[0] for block in message.split("```javascript\n")[1:]]


def _first_page_source(first_arguments: dict[str, Any], key: str) -> str:
    """Return the dispatch frame that stores the immutable command and reads page one."""
    return (
        EXEC_PRAGMA
        + f'const args = {json.dumps(first_arguments, ensure_ascii=False)}; store("{key}", args); '
        + "const r = await tools.exec_command(args); text(r.output);"
    )


def _later_page_source(key: str, page: int) -> str:
    """Return the dispatch frame that loads the stored command and selects only a later page."""
    return (
        EXEC_PRAGMA
        + f'const args = load("{key}"); const r = await tools.exec_command('
        + f'{{...args, cmd: args.cmd + " --page {page}"}}); text(r.output);'
    )


def _execute_pages(
    sources: list[str], first_arguments: dict[str, Any], key: str, header: bytes, cwd: Path
) -> list[bytes]:
    """Run each page's generated command from ``cwd`` and return the page bodies after checking their frames.

    Every frame must match the compact dispatch contract, carry no ``workdir`` coordinate and print the producer
    provenance header followed by its ``page/total`` marker.
    """
    delivered = []
    for page, source in enumerate(sources, 1):
        expected = _first_page_source(first_arguments, key) if page == 1 else _later_page_source(key, page)
        assert source == expected
        arguments = {**first_arguments, "cmd": first_arguments["cmd"] + (f" --page {page}" if page != 1 else "")}
        assert "workdir" not in arguments, f"page {page} still transmits a redundant working-directory coordinate"
        # CreateProcess consumes native list2cmdline syntax; POSIX uses the generated shlex quoting.
        command = arguments["cmd"] if os.name == "nt" else shlex.split(arguments["cmd"])
        completed = subprocess.run(command, cwd=cwd, capture_output=True, check=False)
        assert completed.returncode == 0, (page, completed.stderr)
        assert completed.stderr == b""
        provenance, position, body = completed.stdout.split(b"\n", 2)
        assert provenance == header
        assert position == f"<!-- codex-review-context-page {page}/{len(sources)} -->".encode()
        delivered.append(body)
    return delivered


@pytest.mark.integration
def test_generated_pages_execute_from_unrelated_cwd_with_exact_context(tmp_path: Path) -> None:
    """Generated page commands run from any directory and reproduce every context byte.

    A prepared run has a multi-page qa-specialist context. Each page command is executed through a real subprocess from
    an unrelated folder, which proves the mutable cwd coordinate is gone while no source byte is lost.
    """
    nested = tmp_path / "review inputs café" / "nested folder"
    nested.mkdir(parents=True)
    run = preparation._review_inputs(nested)
    (run / "qa-specialist-evidence.md").write_text(
        "Scope: widget.py at frozen revision.\nQuestion: boundary behavior.\n" + "Boundary checked: café π.\n" * 2000,
        encoding="utf-8",
        newline="\n",
    )
    prepared = preparation._prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    plan = json.loads((run / "inspection-plan.json").read_bytes())
    dispatch = json.loads((run / "dispatch.json").read_bytes())
    role = "qa-specialist"
    entry = next(item for item in plan["contexts"] if item["role_id"] == role)
    context = (run / entry["context_path"]).read_bytes()
    # This case exercises multi-page ordinary dispatch; oversized auto-batches have their own reconstruction test.
    assert len(context) <= 65536
    call = next(
        item for item in dispatch["calls"] if item["arguments"]["task_name"].startswith("review_qa_specialist_")
    )
    sources = _javascript_fences(call["arguments"]["message"])
    assert len(sources) >= 9
    unrelated_cwd = tmp_path / "unrelated execution folder"
    unrelated_cwd.mkdir()
    header = (
        f"<!-- codex-review-provenance role={role} run={plan['review_run_id']} "
        f"input={plan['review_input_sha256']} context={entry['context_sha256']} attempt=1 -->"
    ).encode()
    first_call = preparation._CONTEXT_READ_CALL(run / "inspection-plan.json", role, 1, sys.executable)
    first_arguments = json.loads(first_call.split("tools.exec_command(", 1)[1].split("); text", 1)[0])
    key = "review-context-" + hashlib.sha256((first_call + "\0" + entry["context_sha256"]).encode()).hexdigest()

    delivered = _execute_pages(sources, first_arguments, key, header, unrelated_cwd)

    reconstructed = b"".join(delivered)
    assert delivered[3]
    assert delivered[8]
    assert reconstructed == context
    assert hashlib.sha256(reconstructed).hexdigest() == entry["context_sha256"]
    assert reconstructed.decode("utf-8").encode("utf-8") == context


def test_legacy_all_page_reader_retains_issued_bytes() -> None:
    """The legacy all-page reader fixture keeps the exact bytes issued runs were built against.

    Historical runs reconstruct their cwd-bearing all-page calls from this reader, so any byte change would silently
    break admission of those runs.
    """
    reader = Path(__file__).with_name("fixtures") / "legacy-all-page-context-reader/review_context.py"

    assert hashlib.sha256(reader.read_bytes()).hexdigest() == LEGACY_READER_SHA256
