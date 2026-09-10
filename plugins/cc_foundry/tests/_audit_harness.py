"""Isolated plugin installation used by every audit-log test.

Audit writing is on by default and targets ``~/.claude/logs/audit``. A suite that runs the real hooks without pinning
``HOME`` writes into the runner's own log directory — and because the hooks swallow every audit failure, a missing
library would be silently absorbed and the suite would pass while testing nothing. Both problems are structural, so the
fix is structural: every test gets a throwaway home and a throwaway plugin root holding the shipped hooks, the shipped
library and a real ``plugin.json``, and states ``RIG_AUDIT`` explicitly.

Deliberately a uniquely-named module rather than ``conftest``: ``testpaths`` spans several trees, each with its own
``conftest.py``, and under ``--import-mode=importlib`` a bare ``conftest`` import resolves to whichever loaded first.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = _TESTS_DIR.parent
HOOKS_DIR = PLUGIN_DIR / "hooks"

#: Hooks and libraries copied into every isolated plugin root.
SHIPPED = (
    Path("blueprint-allow.js"),
    Path("sentinel-read-allow.js"),
    Path("allow-dispatch.js"),
    Path("audit-close.js"),
    Path("lib/audit-log.js"),
)


@dataclass(frozen=True)
class AuditEnv:
    """One isolated plugin root plus the throwaway home its hooks write into."""

    root: Path
    home: Path

    @property
    def audit_dir(self) -> Path:
        """Return the directory the hooks append their records to."""
        return self.home / ".claude" / "logs" / "audit"

    def env(self, **extra: str) -> dict[str, str]:
        """Return the subprocess environment: a pinned home, this plugin root, and nothing inherited that matters."""
        base = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "CLAUDE_PLUGIN_ROOT": str(self.root),
        }
        # Windows CPython aborts at startup without these; see the repo's multi-OS rule.
        for name in ("SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
            if name in os.environ:
                base[name] = os.environ[name]
        base.update(extra)
        return base

    def run(self, hook: str, payload: dict | str, **env_extra: str) -> subprocess.CompletedProcess:
        """Run one hook out of this root and return the completed process, with stdout kept as raw bytes."""
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["node", str(self.root / "hooks" / hook)],
            input=stdin.encode("utf-8"),
            capture_output=True,
            env=self.env(**env_extra),
            timeout=30,
            check=False,
        )

    def write_manifest(self, body: bytes | None) -> None:
        """Install, or remove, the blueprint manifest this root's hooks will resolve."""
        target = self.root / "blueprint-manifest.json"
        target.unlink(missing_ok=True)
        if body is not None:
            target.write_bytes(body)

    def log_files(self) -> list[Path]:
        """Return the audit log files that exist, sorted by name."""
        return sorted(self.audit_dir.glob("*.jsonl")) if self.audit_dir.is_dir() else []

    def rows(self) -> list[dict]:
        """Return every parseable record written so far, across all log files."""
        records = []
        for path in self.log_files():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
        return records

    def created_paths(self) -> set[str]:
        """Return every path created under the throwaway home, relative and POSIX-formatted."""
        return {path.relative_to(self.home).as_posix() for path in self.home.rglob("*")}


def install(base: Path, *, name: str = "foundry", version: str = "0.0.0", manifest: bytes | None = None) -> AuditEnv:
    """Build an isolated plugin root and home under ``base`` and return the handle for running its hooks.

    Args:
        base: A directory the caller owns, normally ``tmp_path``.
        name: Short plugin name written into ``plugin.json``; the hooks derive ``cc_<name>`` from it.
        version: Version written into ``plugin.json``, reported as ``agent_version``.
        manifest: Blueprint manifest bytes, or None to install none.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     install(Path(tmp), name="oss").audit_dir.name
        'audit'
    """
    root = base / "plugin"
    (root / "hooks" / "lib").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    for relative in SHIPPED:
        shutil.copy(HOOKS_DIR / relative, root / "hooks" / relative)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8"
    )
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = AuditEnv(root=root, home=home)
    env.write_manifest(manifest)
    return env
