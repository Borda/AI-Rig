"""Refresh acknowledgements must not follow planted marker symlinks."""

from __future__ import annotations

from pathlib import Path

import pytest
from _host_skips import skip_file_symlink_unavailable
from codemap_py import graph


@skip_file_symlink_unavailable
def test_refresh_publication_preserves_symlink_victim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Publishing an acknowledgement replaces the marker link without modifying its victim."""
    victim = tmp_path / "private.txt"
    victim.write_text("private data")
    marker = tmp_path / "codemap-refresh-fp-project"
    marker.symlink_to(victim)
    monkeypatch.setenv("CODEMAP_REFRESH_RECORD", str(marker))
    monkeypatch.setenv("CODEMAP_REFRESH_FINGERPRINT", "fingerprint")

    graph._mark_refresh_published()

    assert victim.read_text() == "private data"
    assert not marker.is_symlink()
    lines = marker.read_text().splitlines()
    assert lines[0] == "fingerprint"
    assert int(lines[1]) > 0
    assert lines[2] == "published"
