"""Execute producer-supplied native page commands without a redundant working directory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import test_review_prepare as preparation


@pytest.mark.integration
def test_generated_pages_execute_from_unrelated_cwd_with_exact_context(tmp_path: Path) -> None:
    """Remove the mutable cwd coordinate while retaining every actual reader command and source byte."""
    nested = tmp_path / "review inputs café" / "nested folder"
    nested.mkdir(parents=True)
    run = preparation._review_inputs(nested)
    (run / "qa-specialist-evidence.md").write_text(
        "Scope: widget.py at frozen revision.\nQuestion: boundary behavior.\n" + "Boundary checked: café π.\n" * 2500,
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
    call = next(
        item for item in dispatch["calls"] if item["arguments"]["task_name"].startswith("review_qa_specialist_")
    )
    sources = re.findall(r"```javascript\n(.*?)\n```", call["arguments"]["message"], re.DOTALL)
    assert len(sources) >= 9
    unrelated_cwd = tmp_path / "unrelated execution folder"
    unrelated_cwd.mkdir()
    header = (
        f"<!-- codex-review-provenance role={role} run={plan['review_run_id']} "
        f"input={plan['review_input_sha256']} context={entry['context_sha256']} attempt=1 -->"
    ).encode("utf-8")
    first_call = preparation._CONTEXT_READ_CALL(run / "inspection-plan.json", role, 1, sys.executable)
    first_arguments = json.loads(first_call.split("tools.exec_command(", 1)[1].split("); text", 1)[0])
    key = "review-context-" + hashlib.sha256((first_call + "\0" + entry["context_sha256"]).encode()).hexdigest()
    pragma = '// @exec: {"max_output_tokens": 10000}\n'
    delivered = []
    for page, source in enumerate(sources, 1):
        # Compact dispatch stores one immutable command; subsequent frames select only their page.
        if page == 1:
            assert source == (
                pragma
                + f'const args = {json.dumps(first_arguments, ensure_ascii=False)}; store("{key}", args); '
                + "const r = await tools.exec_command(args); text(r.output);"
            )
        else:
            assert source == (
                pragma
                + f'const args = load("{key}"); const r = await tools.exec_command('
                + f'{{...args, cmd: args.cmd + " --page {page}"}}); text(r.output);'
            )
        arguments = {**first_arguments, "cmd": first_arguments["cmd"] + (f" --page {page}" if page != 1 else "")}
        assert "workdir" not in arguments, f"page {page} still transmits a redundant working-directory coordinate"
        # CreateProcess consumes native list2cmdline syntax; POSIX uses the generated shlex quoting.
        command = arguments["cmd"] if os.name == "nt" else shlex.split(arguments["cmd"])
        completed = subprocess.run(command, cwd=unrelated_cwd, capture_output=True, check=False)
        assert completed.returncode == 0, (page, completed.stderr)
        assert completed.stderr == b""
        provenance, position, body = completed.stdout.split(b"\n", 2)
        assert provenance == header
        assert position == f"<!-- codex-review-context-page {page}/{len(sources)} -->".encode("utf-8")
        delivered.append(body)
    assert delivered[3] and delivered[8]
    reconstructed = b"".join(delivered)
    assert reconstructed == context
    assert hashlib.sha256(reconstructed).hexdigest() == entry["context_sha256"]
    assert reconstructed.decode("utf-8").encode("utf-8") == context


def test_legacy_all_page_reader_retains_issued_bytes() -> None:
    """Protect the reader identity used to reconstruct issued cwd-bearing all-page calls."""
    reader = Path(__file__).with_name("fixtures") / "legacy-all-page-context-reader/review_context.py"
    assert hashlib.sha256(reader.read_bytes()).hexdigest() == (
        "c185dc007a261a2d9c0e449e5a888a7a771cd99336ebe8c7085432bc2b0d43c8"
    )
