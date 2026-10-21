"""Atomic-write tests for ``release_append_marker._write_marker_atomic``.

The marker records the last processed release endpoint. Truncating it in place leaves an empty file when the process
dies mid-write, so the write goes through a same-directory temporary file and ``os.replace``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import release_append_marker as ram


class TestWriteMarkerAtomic:
    """_write_marker_atomic: the marker is replaced whole and no temporary file survives."""

    def test_new_marker_holds_sha_and_leaves_no_temp_file(self, tmp_path: Path) -> None:
        """A first write creates the marker with a trailing newline and nothing else in the directory."""
        marker = tmp_path / "state" / "marker"
        assert ram._write_marker_atomic(marker, "abc123 ") is None
        assert (marker.read_text(encoding="utf-8"), sorted(p.name for p in marker.parent.iterdir())) == (
            "abc123\n",
            ["marker"],
        )

    def test_existing_marker_is_replaced(self, tmp_path: Path) -> None:
        """A later write replaces the previous sha instead of appending to it."""
        marker = tmp_path / "marker"
        marker.write_text("old\n", encoding="utf-8")
        assert ram._write_marker_atomic(marker, "new") is None
        assert marker.read_text(encoding="utf-8") == "new\n"

    def test_failed_replace_reports_error_and_keeps_old_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the final replace fails the old marker survives and the temporary file is removed."""
        marker = tmp_path / "marker"
        marker.write_text("old\n", encoding="utf-8")

        def failing_replace(_src: object, _dst: object) -> None:
            """Simulate a filesystem refusal at the swap step."""
            raise OSError("swap refused")

        monkeypatch.setattr(ram.os, "replace", failing_replace)
        error = ram._write_marker_atomic(marker, "new")
        assert (error, marker.read_text(encoding="utf-8"), sorted(p.name for p in tmp_path.iterdir())) == (
            "release marker write failed: swap refused",
            "old\n",
            ["marker"],
        )
