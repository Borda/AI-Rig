"""Override collision probes obey the same bounded reader as the writer."""

from pathlib import Path

import pytest
from codemap_py import index_paths, rwgate


def test_override_identity_probe_rejects_oversized_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The collision probe cannot load an oversized index before the writer's bounded read."""
    path = tmp_path / "index.json"
    path.write_text('{"scan_root":"' + "x" * 100 + '"}')
    monkeypatch.setattr(rwgate, "_MAX_INDEX_SIZE_BYTES", 32)
    assert index_paths._read_scan_root(path) is None
