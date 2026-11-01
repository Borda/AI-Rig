"""Fixtures shared by the GitHub read setup tests: a verified installed package and the execpolicy allow rule."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def installed_plugin(home: Path, version: str = "1.2.3") -> Path:
    """Create a verified installed package with the two GitHub evidence helpers."""
    root = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / version
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "codex-rig", "version": version}), encoding="utf-8"
    )
    (root / "shared").mkdir()
    (root / "shared" / "github_read.py").write_text('"""Fixture reader."""\n', encoding="utf-8")
    (root / "shared" / "collect_pr.py").write_text('"""Fixture collector."""\n', encoding="utf-8")
    files = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": f"{path.stat().st_mode & 0o777:04o}",
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    reader = next(item for item in files if item["path"] == "shared/github_read.py")
    (root / "package-manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "plugin": "codex-rig",
                "version": version,
                "files": files,
                "skills": [],
                "roles": [],
                "bootstrap": {"protocol": "fixture", "helper": reader["path"], "sha256": reader["sha256"]},
                "generator": {"version": "fixture", "path": reader["path"], "sha256": reader["sha256"]},
            }
        ),
        encoding="utf-8",
    )
    return root


def allow_rule(pattern: list[object], eol: str = "\n") -> bytes:
    """Render one execpolicy allow rule for a launcher/helper command pattern."""
    return f'prefix_rule(pattern={json.dumps(pattern)}, decision="allow"){eol}'.encode()
