#!/usr/bin/env python3
"""Non-interactive Claude Code install probe for the disposable codemap-py candidate.

Feasibility probe, not a CI test: it drives the real ``claude`` CLI against a throwaway
``CLAUDE_CONFIG_DIR`` to prove the built candidate installs and exposes exactly its declared
Claude skill roster. It never touches the user's real ``~/.claude`` — every mutation is scoped
to a fresh temporary config dir that is removed on exit.

Flow (all inside the disposable config dir):

1. Copy the plugin working tree into a DISPOSABLE source checkout outside the repo and build the
   candidate by running THAT COPY's ``build_package.py`` (the copy is ``SOURCE_ROOT``).
2. Write a local marketplace at ``<root>/.claude-plugin/marketplace.json`` whose one entry
   points at the candidate with a ``./``-relative string ``source``.
3. ``claude plugin marketplace add <root>`` then ``claude plugin install codemap-py@<mkt> --scope user``.
4. Statically verify the installed bytes under
   ``<config>/plugins/cache/<mkt>/codemap-py/<version>``: a ``claude-skills/`` directory whose
   set of ``SKILL.md``-bearing skill dirs equals the installed ``package-manifest.json`` Claude
   roster exactly, a ``.claude-plugin/plugin.json`` with the expected name/version, and a
   ``skills`` manifest field that references ``claude-skills`` — never any bundled
   ``codex-skills/`` directory (which may ship in the payload but must not be a Claude skills source).
5. Source-independent runtime proof (``_probe_runtime.runtime_proof``): DELETE the whole disposable
   source tree (copy checkout + candidate + marketplace) BEFORE any execution — the source the
   installed bytes came from is now literally unavailable — then run
   ``doctor``/``index``/``query`` from the installed cache bytes under a scrubbed env, asserting no
   FORBIDDEN path (temp checkout OR developer repo) leaks via env, argv, or installed bytes and the
   interpreter is non-forbidden. Deletion of the developer git checkout itself stays a Phase 6
   release-acceptance cell on a runner that can drop the workspace.

Claude Code exposes config-dir isolation through the ``CLAUDE_CONFIG_DIR`` environment
variable (verified: both ``marketplace add`` and ``install`` honour it and run
non-interactively). If a future CLI drops that isolation, the probe records
``claude-config-isolation-unsupported`` rather than mutating the real config.

Exit codes: 0 = installed and verified; 2 = a named prerequisite is absent (builder not
landed, or the ``claude`` CLI is not installed) — a recorded limitation, not a failure;
1 = the probe ran but installation or verification failed.

The probe writes its JSON result to stdout, and to ``--report <path>`` when given.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Resolved path of this probe script.
_THIS = Path(__file__).resolve()
#: Directory holding the probe and package-builder scripts.
_SCRIPTS = _THIS.parent
#: Root of the repository checkout that contains ``plugins/codemap-py``.
_REPO_ROOT = _SCRIPTS.parents[2]
#: Path of the package builder script the probe runs before installing.
_BUILDER = _SCRIPTS / "build_package.py"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _probe_runtime import (  # noqa: E402  (needs the scripts path insert above)
    RuntimeProof,
    build_from_checkout,
    runtime_proof,
    stage_disposable_source,
    write_real_mode_map,
)

#: Name of the throwaway marketplace the probe registers to install the candidate from.
MKT_NAME = "codemap-py-probe-mkt"
#: Plugin name the probe installs and expects the installed manifest to carry.
PLUGIN_NAME = "codemap-py"

#: Exit code when the install probe passes.
EXIT_OK = 0
#: Exit code when the install probe runs and fails.
EXIT_FAIL = 1
#: Exit code when the probe cannot run because the builder or the ``claude`` CLI is missing.
EXIT_SKIP = 2


def claude_marketplace_manifest(rel_path: str) -> dict:
    """Return the Claude Code local-marketplace manifest for the candidate.

    Claude's plugin ``source`` is a plain ``./``-relative string (unlike Codex's object form).

    Examples:
        >>> m = claude_marketplace_manifest("./plugins/codemap-py")
        >>> m["plugins"][0]["source"]
        './plugins/codemap-py'
        >>> m["plugins"][0]["name"]
        'codemap-py'
    """
    return {
        "name": MKT_NAME,
        "owner": {"name": "codemap-py probe"},
        "plugins": [{"name": PLUGIN_NAME, "description": "codemap-py probe candidate", "source": rel_path}],
    }


def _skills_field_names(skills_field: object) -> list[str]:
    """Normalise a Claude ``skills`` manifest field (str or list) to a list of strings."""
    if isinstance(skills_field, str):
        return [skills_field]
    if isinstance(skills_field, list):
        return [s for s in skills_field if isinstance(s, str)]
    return []


def _package_roster(installed_path: Path) -> list[str] | None:
    """Return the installed ``package-manifest.json`` Claude roster, or ``None`` if absent."""
    manifest_path = installed_path / "package-manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        roster = json.loads(manifest_path.read_text()).get("skills", {}).get("claude")
    except (OSError, json.JSONDecodeError):
        return None
    return sorted(roster) if isinstance(roster, list) else None


@dataclass(frozen=True)
class ClaudeInstallReport:
    """Static verification result for one installed Claude plugin tree.

    Dataclass (not TypedDict): named construction and attribute access;
    converted to a dict by :meth:`to_dict` at the JSON-emission boundary.

    Attributes:
        ok: Whether every check in ``checks`` passed.
        checks: Per-check verdicts keyed by check name.
        issues: Human-readable reasons for each failed check.
        skill_dirs: Installed ``claude-skills/`` dirs holding a ``SKILL.md``.
        roster: Claude roster from the installed ``package-manifest.json``, or ``None`` if absent.
        manifest: Identity fields echoed from ``.claude-plugin/plugin.json``.

    Examples:
        >>> ClaudeInstallReport(ok=False, checks={}, issues=[], skill_dirs=[], roster=None, manifest={}).ok
        False
    """

    ok: bool
    checks: dict[str, bool]
    issues: list[str]
    skill_dirs: list[str]
    roster: list[str] | None
    manifest: dict

    def to_dict(self) -> dict:
        """Return the JSON-envelope projection of this report.

        Returns:
            A dict with ``ok``, ``checks``, ``issues``, ``skill_dirs``, ``roster``, ``manifest``.

        Examples:
            >>> sorted(ClaudeInstallReport(True, {}, [], [], None, {}).to_dict())
            ['checks', 'issues', 'manifest', 'ok', 'roster', 'skill_dirs']
        """
        return {
            "ok": self.ok,
            "checks": self.checks,
            "issues": self.issues,
            "skill_dirs": self.skill_dirs,
            "roster": self.roster,
            "manifest": self.manifest,
        }


def verify_claude_install(installed_path: Path) -> ClaudeInstallReport:
    """Statically check the installed Claude plugin bytes; return a checks/issues report.

    Verifies the manifest identity, that the ``claude-skills/`` skill set (dirs holding a
    ``SKILL.md``, so ``_shared`` is excluded) equals the installed ``package-manifest.json``
    Claude roster exactly, and that no ``skills`` entry sources a bundled ``codex-skills``
    directory.

    Examples:
        >>> verify_claude_install(Path("/nonexistent")).ok
        False
    """
    checks: dict[str, bool] = {}
    issues: list[str] = []

    manifest_path = installed_path / ".claude-plugin" / "plugin.json"
    checks["manifest_present"] = manifest_path.is_file()
    manifest: dict = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"manifest unreadable: {exc}")
    else:
        issues.append("missing .claude-plugin/plugin.json")

    checks["name_matches"] = manifest.get("name") == PLUGIN_NAME
    if not checks["name_matches"]:
        issues.append(f"manifest name {manifest.get('name')!r} != {PLUGIN_NAME!r}")
    checks["version_present"] = bool(manifest.get("version"))
    if not checks["version_present"]:
        issues.append("manifest version missing")

    skills_dir = installed_path / "claude-skills"
    checks["claude_skills_present"] = skills_dir.is_dir()
    skill_dirs = (
        sorted(p.name for p in skills_dir.iterdir() if (p / "SKILL.md").is_file()) if skills_dir.is_dir() else []
    )
    roster = _package_roster(installed_path)
    checks["package_manifest_present"] = roster is not None
    checks["roster_matches_skill_dirs"] = bool(skill_dirs) and roster == skill_dirs
    if not checks["roster_matches_skill_dirs"]:
        issues.append(f"claude skill dirs {skill_dirs} != package-manifest roster {roster}")

    names = _skills_field_names(manifest.get("skills"))
    checks["skills_field_is_claude"] = any("claude-skills" in n for n in names)
    checks["codex_skills_not_registered"] = not any("codex-skills" in n for n in names)
    if not checks["codex_skills_not_registered"]:
        issues.append(f"codex-skills registered as claude skills source: {names!r}")

    return ClaudeInstallReport(
        ok=all(checks.values()),
        checks=checks,
        issues=issues,
        skill_dirs=skill_dirs,
        roster=roster,
        manifest={
            "name": manifest.get("name"),
            "version": manifest.get("version"),
            "skills": manifest.get("skills"),
        },
    )


def _claude_version() -> str | None:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=15, check=False)  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or out.stderr.strip() or None


def _run(cmd: list[str], config_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
        cmd,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _resolve_installed_path(config_dir: Path) -> Path | None:
    """Locate the single installed version dir under the disposable config cache."""
    base = config_dir / "plugins" / "cache" / MKT_NAME / PLUGIN_NAME
    if not base.is_dir():
        return None
    versions = sorted(p for p in base.iterdir() if p.is_dir())
    return versions[0] if len(versions) == 1 else None


def run_probe() -> dict:
    """Drive the full disposable install and return a JSON-able result envelope."""
    claude_version = _claude_version()
    if claude_version is None:
        return {"probe": "claude", "status": "claude-cli-not-present", "claude_version": None}
    if not _BUILDER.is_file():
        return {
            "probe": "claude",
            "status": "builder-not-yet-present",
            "claude_version": claude_version,
            "detail": f"missing {_BUILDER}",
        }

    workdir = Path(tempfile.mkdtemp(prefix="codemap-py-claude-probe-"))
    config_dir = workdir / "claude-config"
    config_dir.mkdir()
    # The disposable source tree (copy checkout + marketplace + candidate) is deleted
    # before runtime; the installed cache under config_dir survives it.
    src_root = workdir / "src"
    mkt_root = src_root / "mkt"
    candidate = mkt_root / "plugins" / PLUGIN_NAME
    candidate.mkdir(parents=True)
    mkt_manifest = mkt_root / ".claude-plugin" / "marketplace.json"
    mkt_manifest.parent.mkdir(parents=True)

    result: dict = {"probe": "claude", "claude_version": claude_version}
    try:
        # Capture the REAL repo's mode map BEFORE the disposable copy exists — the copy's
        # own synthesized index is not authoritative (see _probe_runtime.stage_disposable_source).
        mode_map_path = write_real_mode_map(_REPO_ROOT, src_root / "mode-map.json")
        checkout = stage_disposable_source(_REPO_ROOT, src_root / "checkout")
        built_ok, detail = build_from_checkout(checkout, candidate, mode_map_path)
        if not built_ok:
            return {**result, "status": "builder-failed", "detail": detail}

        mkt_manifest.write_text(json.dumps(claude_marketplace_manifest(f"./plugins/{PLUGIN_NAME}"), indent=2))

        steps = [
            (["claude", "plugin", "marketplace", "add", str(mkt_root)], "marketplace-add-failed"),
            (["claude", "plugin", "install", f"{PLUGIN_NAME}@{MKT_NAME}", "--scope", "user"], "plugin-install-failed"),
        ]
        for cmd, fail_status in steps:
            proc = _run(cmd, config_dir)
            if proc.returncode != 0:
                return {**result, "status": fail_status, "detail": proc.stderr.strip()}

        installed_path = _resolve_installed_path(config_dir)
        if installed_path is None:
            return {
                **result,
                "status": "claude-config-isolation-unsupported",
                "detail": "install reported success but no plugin cache under CLAUDE_CONFIG_DIR",
            }

        verification = verify_claude_install(installed_path)
        runtime = (
            runtime_proof(installed_path, workdir, [src_root], [_REPO_ROOT, src_root])
            if verification.ok
            else RuntimeProof(ok=False, checks={}, detail={"skipped": "static verification failed"})
        )
        verification_payload = {**verification.to_dict(), "runtime_ok": runtime.ok}
        return {
            **result,
            "status": "ok" if verification.ok and runtime.ok else "verification-failed",
            "installed_path": str(installed_path),
            "verification": verification_payload,
            "runtime": runtime.to_dict(),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Non-interactive Claude Code install probe for codemap-py.")
    parser.add_argument("--report", type=Path, help="Write the JSON result to this path in addition to stdout.")
    args = parser.parse_args(argv)

    result = run_probe()
    payload = json.dumps(result, indent=2)
    print(payload)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(payload)

    if result["status"] == "ok":
        return EXIT_OK
    if result["status"] in {"builder-not-yet-present", "claude-cli-not-present"}:
        return EXIT_SKIP
    return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
