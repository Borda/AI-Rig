"""Prove root-bootstrap migration preserves shell blocks and refuses ambiguous text."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve().parent / "sys1_migrate.py"
OLD_ROOT = "${CLAUDE_PLUGIN_ROOT:-plugins/cc_demo}"
PROLOGUE = (
    'PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name demo)'
    " || exit $?; export PLUGIN_ROOT"
)


def _document(root: Path, text: str, relative: str = "skills/test/SKILL.md") -> Path:
    """Create an independently installable plugin document fixture."""
    plugin = root / "plugins/cc_demo"
    manifest = plugin / ".claude-plugin/plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('{"name":"demo"}', encoding="utf-8")
    path = plugin / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _run(root: Path, *arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    """Run the real CLI and load its receipt when present."""
    receipt = root / "receipt.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--receipt", str(receipt), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    return result, json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else {}


def test_default_dry_run_preserves_input_and_records_digests(tmp_path: Path) -> None:
    """Report proposed changes without changing Markdown by default."""
    original = f'```bash\npython "{OLD_ROOT}/bin/run.py" --value x  # timeout: 20000\n```\n'
    document = _document(tmp_path, original)
    result, receipt = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert document.read_text(encoding="utf-8") == original
    assert receipt["mode"] == "dry-run"
    assert receipt["totals"]["files_changed"] == 1
    assert receipt["totals"]["blocks_rewritten"] == 1
    assert receipt["files"][0]["before_sha256"] != receipt["files"][0]["after_sha256"]


def test_apply_preserves_metadata_inline_commands_and_timeout(tmp_path: Path) -> None:
    """Insert one bootstrap after leading comments without changing command arguments."""
    document = _document(
        tmp_path,
        "<!-- file: SKILL.md — consumers: test -->\n```bash\n# loads: part.md\n"
        f'if true; then python "{OLD_ROOT}/bin/run.py" --value x; fi  # timeout: 2500\n```\n',
    )
    result, receipt = _run(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert document.read_text(encoding="utf-8") == (
        "<!-- file: SKILL.md — consumers: test -->\n```bash\n# loads: part.md\n"
        f'{PROLOGUE}\nif true; then python "$PLUGIN_ROOT/bin/run.py" --value x; fi  # timeout: 2500\n```\n'
    )
    assert receipt["totals"]["ambiguities"] == 0


def test_heredoc_and_multiline_quote_stay_contiguous(tmp_path: Path) -> None:
    """Place the bootstrap before commands rather than inside multiline data."""
    body = f'cat <<\'DATA\'\nliteral\nDATA\nprintf "%s\\n" "two\nlines"\npython "{OLD_ROOT}/bin/run.py"\n'
    document = _document(tmp_path, f"```bash\n{body}```\n")
    result, _ = _run(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert document.read_text(encoding="utf-8") == f"```bash\n{PROLOGUE}\n{body.replace(OLD_ROOT, '$PLUGIN_ROOT')}```\n"


def test_exact_old_assignment_is_replaced_without_overwriting_provenance(tmp_path: Path) -> None:
    """Remove the obsolete standalone root assignment after adding the resolver."""
    document = _document(
        tmp_path,
        f"```bash\n[ -f settings.json ] || printf '{{}}' > settings.json\nPLUGIN_ROOT=\"{OLD_ROOT}\"\n"
        'python "$PLUGIN_ROOT/bin/run.py"  # timeout: 5000\n```\n',
    )
    result, _ = _run(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert document.read_text(encoding="utf-8") == (
        f"```bash\n{PROLOGUE}\n[ -f settings.json ] || printf '{{}}' > settings.json\n"
        'python "$PLUGIN_ROOT/bin/run.py"  # timeout: 5000\n```\n'
    )


def test_second_apply_is_idempotent(tmp_path: Path) -> None:
    """Leave previously migrated blocks byte-identical on another apply."""
    document = _document(tmp_path, f'```bash\npython "{OLD_ROOT}/bin/run.py"\n```\n')
    first, _ = _run(tmp_path, "--apply")
    migrated = document.read_bytes()
    second, receipt = _run(tmp_path, "--apply")
    assert first.returncode == second.returncode == 0
    assert document.read_bytes() == migrated
    assert receipt["totals"]["files_changed"] == 0


@pytest.mark.parametrize("marker", ["text", "prompt", "python"])
def test_non_shell_fences_are_reported_without_changes(tmp_path: Path, marker: str) -> None:
    """Require human interpretation for prompt, text, and foreign-language fences."""
    original = f'```{marker}\npython "{OLD_ROOT}/bin/run.py"\n```\n'
    document = _document(tmp_path, original)
    result, receipt = _run(tmp_path, "--apply")
    assert result.returncode == 2
    assert document.read_text(encoding="utf-8") == original
    assert receipt["totals"]["ambiguities"] == 1
    assert receipt["ambiguities"][0]["line"] == 2


def test_prose_and_reference_sidecars_are_included(tmp_path: Path) -> None:
    """Normalize prose separately and inventory reference fragments outside skills."""
    document = _document(tmp_path, f"Read `{OLD_ROOT}/README.md`.\n", "references/agent/guide.md")
    result, receipt = _run(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert document.read_text(encoding="utf-8") == "Read `${CLAUDE_PLUGIN_ROOT}/README.md`.\n"
    assert receipt["plugins"]["demo"]["categories"]["reference"]["prose_replacements"] == 1


@pytest.mark.parametrize("relative", ["skills/test/fixtures/negative.md", "skills/test/CHANGELOG.md"])
def test_historical_inputs_are_excluded(tmp_path: Path, relative: str) -> None:
    """Keep historical fixtures and changelogs outside migration scope."""
    original = f'```bash\npython "{OLD_ROOT}/bin/run.py"\n```\n'
    document = _document(tmp_path, original, relative)
    result, receipt = _run(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert document.read_text(encoding="utf-8") == original
    assert receipt["totals"]["files_changed"] == 0


def test_repeated_excludes_preserve_selected_files(tmp_path: Path) -> None:
    """Honor explicit file and glob exclusions before reading documents."""
    original = f'```bash\npython "{OLD_ROOT}/bin/run.py"\n```\n'
    document = _document(tmp_path, original)
    result, receipt = _run(
        tmp_path, "--apply", "--exclude", "plugins/cc_demo/skills/test/SKILL.md", "--exclude", "*/other.md"
    )
    assert result.returncode == 0, result.stderr
    assert document.read_text(encoding="utf-8") == original
    assert receipt["totals"]["files_changed"] == 0


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(f'{PROLOGUE}\n{PROLOGUE}\npython "{OLD_ROOT}/bin/run.py"', id="duplicate-bootstrap"),
        pytest.param(f'PLUGIN_ROOT=/untrusted\npython "{OLD_ROOT}/bin/run.py"', id="overwritten-root"),
        pytest.param('python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_other}/bin/run.py"', id="foreign-plugin"),
        pytest.param(f"cat <<'DATA'\npython \"{OLD_ROOT}/bin/run.py\"\nDATA", id="heredoc-data-operand"),
    ],
)
def test_ambiguous_blocks_abort_apply_atomically(tmp_path: Path, body: str) -> None:
    """Refuse uncertain ownership and embedded shell text before writing any file."""
    document = _document(tmp_path, f"```bash\n{body}\n```\n")
    safe = _document(tmp_path, f'```bash\npython "{OLD_ROOT}/bin/run.py"\n```\n', "skills/test/safe.md")
    original = document.read_bytes()
    safe_original = safe.read_bytes()
    result, receipt = _run(tmp_path, "--apply")
    assert result.returncode == 2
    assert document.read_bytes() == original
    assert safe.read_bytes() == safe_original
    assert receipt["totals"]["ambiguities"] == 1


def test_blockquote_fence_prefixes_remain_intact(tmp_path: Path) -> None:
    """Retain blockquote layout and the original tilde fence delimiters."""
    document = _document(tmp_path, f'> ~~~~bash\n> python "{OLD_ROOT}/bin/run.py"\n> ~~~~\n')
    result, _ = _run(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert (
        document.read_text(encoding="utf-8")
        == f'> ~~~~bash\n> {PROLOGUE}\n> python "$PLUGIN_ROOT/bin/run.py"\n> ~~~~\n'
    )


def test_list_indented_fences_keep_original_indentation(tmp_path: Path) -> None:
    """Parse shell fences nested in list items without reformatting the list."""
    document = _document(tmp_path, f'1. Run:\n\n   ```bash\n   python "{OLD_ROOT}/bin/run.py"\n   ```\n')
    result, receipt = _run(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert document.read_text(encoding="utf-8") == (
        f'1. Run:\n\n   ```bash\n   {PROLOGUE}\n   python "$PLUGIN_ROOT/bin/run.py"\n   ```\n'
    )
    assert receipt["totals"]["blocks_rewritten"] == 1


def test_assignment_timeout_comment_is_preserved(tmp_path: Path) -> None:
    """Retain timeout comments when the obsolete assignment line disappears."""
    document = _document(
        tmp_path, f'```bash\nPLUGIN_ROOT="{OLD_ROOT}"  # timeout: 5000\npython "$PLUGIN_ROOT/bin/run.py"\n```\n'
    )
    result, receipt = _run(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert document.read_text(encoding="utf-8") == (
        f'```bash\n# timeout: 5000\n{PROLOGUE}\npython "$PLUGIN_ROOT/bin/run.py"\n```\n'
    )
    assert receipt["totals"]["root_assignments_replaced"] == 1


def test_duplicate_existing_prologues_are_refused_without_default_tokens(tmp_path: Path) -> None:
    """Validate bootstrap provenance even when token migration already finished."""
    original = f'```bash\n{PROLOGUE}\n{PROLOGUE}\npython "$PLUGIN_ROOT/bin/run.py"\n```\n'
    document = _document(tmp_path, original)
    result, receipt = _run(tmp_path, "--apply")
    assert result.returncode == 2
    assert document.read_text(encoding="utf-8") == original
    assert receipt["totals"]["ambiguities"] == 1


def test_unterminated_fences_are_never_rewritten_as_prose(tmp_path: Path) -> None:
    """Protect incomplete shell examples from accidental prose substitutions."""
    original = f'```bash\npython "{OLD_ROOT}/bin/run.py"\n'
    document = _document(tmp_path, original)
    result, receipt = _run(tmp_path, "--apply")
    assert result.returncode == 2
    assert document.read_text(encoding="utf-8") == original
    assert receipt["ambiguities"][0]["reason"] == "token in unsupported or unterminated fence"


def test_mixed_fence_indentation_is_refused_without_reformatting(tmp_path: Path) -> None:
    """Require an explicit choice when existing list indentation is inconsistent."""
    original = f'   ```bash\n   python "{OLD_ROOT}/bin/run.py"\necho unindented\n   ```\n'
    document = _document(tmp_path, original)
    result, receipt = _run(tmp_path, "--apply")
    assert result.returncode == 2
    assert document.read_text(encoding="utf-8") == original
    assert receipt["ambiguities"][0]["reason"] == "inconsistent fence body prefix"


def test_receipt_cannot_overwrite_an_input_document(tmp_path: Path) -> None:
    """Keep dry-run read-only even when its receipt destination aliases Markdown."""
    original = f'```bash\npython "{OLD_ROOT}/bin/run.py"\n```\n'
    document = _document(tmp_path, original)
    result, _ = _run(tmp_path, "--receipt", str(document))
    assert result.returncode == 2
    assert document.read_text(encoding="utf-8") == original
    assert "receipt aliases input document" in result.stderr


def _symlinks_available() -> bool:
    """Probe file symlink support without assuming host privileges."""
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "target"
        target.write_text("probe", encoding="utf-8")
        try:
            (Path(directory) / "link").symlink_to(target)
        except OSError:
            return False
        return True


@pytest.mark.skipif(not _symlinks_available(), reason="file symlinks unavailable on this host")
def test_symlink_document_cannot_escape_the_selected_plugin(tmp_path: Path) -> None:
    """Reject a document link into another tree before planning any writes."""
    document = _document(tmp_path, "placeholder")
    target = tmp_path / "outside.md"
    original = f'```bash\npython "{OLD_ROOT}/bin/run.py"\n```\n'
    target.write_text(original, encoding="utf-8")
    document.unlink()
    document.symlink_to(target)
    result, _ = _run(tmp_path, "--apply")
    assert result.returncode == 2
    assert target.read_text(encoding="utf-8") == original
    assert "document escapes its plugin" in result.stderr


@pytest.mark.skipif(not _symlinks_available(), reason="file symlinks unavailable on this host")
def test_receipt_symlink_cannot_overwrite_an_input_document(tmp_path: Path) -> None:
    """Resolve the receipt destination before comparing it with selected documents."""
    original = f'```bash\npython "{OLD_ROOT}/bin/run.py"\n```\n'
    document = _document(tmp_path, original)
    receipt_link = tmp_path / "receipt-link.json"
    receipt_link.symlink_to(document)
    result, _ = _run(tmp_path, "--receipt", str(receipt_link))
    assert result.returncode == 2
    assert document.read_text(encoding="utf-8") == original
    assert "receipt aliases input document" in result.stderr


def test_nested_fence_text_remains_one_non_shell_block(tmp_path: Path) -> None:
    """Keep fence-like prompt content inside the outer text fence for review."""
    original = (
        f'```text\n   ```bash\n   python "{OLD_ROOT}/bin/run.py"\n   ```\nStill text: {OLD_ROOT}/bin/run.py\n```\n'
    )
    document = _document(tmp_path, original)
    result, receipt = _run(tmp_path, "--apply")
    assert result.returncode == 2
    assert document.read_text(encoding="utf-8") == original
    assert receipt["totals"]["blocks_affected"] == 1
    assert receipt["totals"]["files_changed"] == 0
    assert receipt["ambiguities"][0]["reason"] == "non-shell or prompt/text fence (text)"


def _write_with_concurrent_change(
    path: Path, data: bytes, write: Callable[[Path, bytes], int], trigger: Path, other: Path
) -> int:
    """Simulate another writer changing the next document after the first write."""
    written = write(path, data)
    if path == trigger:
        write(other, b"concurrent owner change")
    return written


def test_apply_rechecks_each_file_after_prior_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve another owner's edit arriving after the global source preflight."""
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_bytes(b"first before")
    second.write_bytes(b"second before")
    monkeypatch.setattr(sys, "path", sys.path.copy())
    migration = runpy.run_path(str(SCRIPT))
    original_write = Path.write_bytes
    monkeypatch.setattr(
        Path,
        "write_bytes",
        lambda path, data: _write_with_concurrent_change(path, data, original_write, first, second),
    )
    with pytest.raises(ValueError, match="source changed before write"):
        migration["_apply"]([(first, b"first before", b"first after"), (second, b"second before", b"second after")])
    assert first.read_bytes() == b"first after"
    assert second.read_bytes() == b"concurrent owner change"
