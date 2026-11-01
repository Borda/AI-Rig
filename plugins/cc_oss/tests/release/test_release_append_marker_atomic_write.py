"""Atomic-write tests for ``release_append_marker._write_marker_atomic``.

The marker records the last processed release endpoint. Truncating it in place leaves an empty file when the process
dies mid-write, so the write goes through a same-directory temporary file and ``os.replace``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import release_append_marker as ram


class TestWriteMarkerAtomic:
    """_write_marker_atomic: the marker is replaced whole and no temporary file survives."""

    def test_new_marker_holds_sha_and_leaves_no_temp_file(self, tmp_path: Path) -> None:
        """A first write creates the marker with a trailing newline and nothing else in the directory.

        The parent directory does not exist yet and the sha carries stray whitespace; both are normalised, and the
        temporary file used for the swap must be gone afterwards.
        """
        marker = tmp_path / "state" / "marker"
        error = ram._write_marker_atomic(marker, "abc123 ")
        assert (error, marker.read_text(encoding="utf-8"), sorted(p.name for p in marker.parent.iterdir())) == (
            None,
            "abc123\n",
            ["marker"],
        )

    def test_existing_marker_is_replaced(self, tmp_path: Path) -> None:
        """A later write replaces the previous sha instead of appending to it.

        The marker holds exactly one endpoint; leaving the old line behind would make the next read ambiguous.
        """
        marker = tmp_path / "marker"
        marker.write_text("old\n", encoding="utf-8")
        error = ram._write_marker_atomic(marker, "new")
        assert (error, marker.read_text(encoding="utf-8")) == (None, "new\n")

    @mock.patch("release_append_marker.os.replace", autospec=True, side_effect=OSError("swap refused"))
    def test_failed_replace_reports_error_and_keeps_old_marker(self, replace: Any, tmp_path: Path) -> None:
        """When the final replace fails the old marker survives and the temporary file is removed.

        The filesystem refuses the swap step. The caller must get an error message, the previous endpoint must stay
        readable, and no ``.tmp`` file may be left in the state directory.
        """
        marker = tmp_path / "marker"
        marker.write_text("old\n", encoding="utf-8")
        error = ram._write_marker_atomic(marker, "new")
        assert (error, marker.read_text(encoding="utf-8"), sorted(p.name for p in tmp_path.iterdir())) == (
            "release marker write failed: swap refused",
            "old\n",
            ["marker"],
        )
