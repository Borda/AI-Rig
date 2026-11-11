"""Keep staged append records inside their owning run directory."""

from pathlib import Path

import pytest
from _modules import load_module
from _platform import FILE_SYMLINKS_AVAILABLE

HELPER = load_module("finalize_record_safety", Path(__file__).resolve().parents[3] / "shared/remediation_finalize.py")


def test_external_record_is_rejected_without_copy_or_delete(tmp_path: Path) -> None:
    """An explicit record outside the run remains unread and untouched."""
    run = tmp_path / "run"
    run.mkdir()
    record = tmp_path / "private.txt"
    record.write_text("private", encoding="utf-8")

    with pytest.raises(HELPER.DeriveError, match="append-record-path-invalid"):
        HELPER.append_record(run, "closure-log.md", record)

    assert record.read_text(encoding="utf-8") == "private"
    assert not (run / "closure-log.md").exists()


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="file symlinks unavailable")
def test_symlink_record_is_rejected_without_copy_or_delete(tmp_path: Path) -> None:
    """A staged symlink cannot expose an external file or be removed by append."""
    run = tmp_path / "run"
    run.mkdir()
    private = tmp_path / "private.txt"
    private.write_text("private", encoding="utf-8")
    record = run / "closure-log.md.rec"
    record.symlink_to(private)

    with pytest.raises(HELPER.DeriveError, match="append-record-path-invalid"):
        HELPER.append_record(run, "closure-log.md")

    assert record.is_symlink()
    assert private.read_text(encoding="utf-8") == "private"
    assert not (run / "closure-log.md").exists()
