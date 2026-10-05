#!/usr/bin/env python3
"""Non-interactive Codex install probe for the disposable codemap-py candidate.

Feasibility probe, not a CI test: it drives the real ``codex`` CLI against a
throwaway ``CODEX_HOME`` to prove the built candidate installs with its declared
``./codex-skills/`` roster. It never touches the user's real ``~/.codex`` — every
mutation is scoped to a fresh temporary home removed on exit.

Flow (all inside the disposable home):

1. Copy the plugin working tree into a DISPOSABLE source checkout outside the repo and build the
   candidate by running THAT COPY's ``build_package.py`` (the copy is ``SOURCE_ROOT``).
2. Write a local marketplace at ``<root>/.agents/plugins/marketplace.json`` whose one
   entry points at the candidate with a ``./``-relative ``source.path``.
3. ``codex plugin marketplace add <root>`` then ``codex plugin add codemap-py@<mkt> --json``.
4. Statically verify the installed bytes: ``.codex-plugin/plugin.json`` has the expected
   name/version, a Codex skill source referencing ``codex-skills`` (never a bundled
   ``claude-skills``), a non-empty installed Codex skill directory, and the packaged
   runtime-scoped ``./hooks/codex-hooks.json`` declaration. The package validator separately
   requires the shipped skill source and its six-skill Claude parity.
5. Source-independent runtime proof (``_probe_runtime.runtime_proof``): DELETE the whole disposable
   source tree (copy checkout + candidate + marketplace) BEFORE any execution — the source the
   installed bytes came from is now literally unavailable — then run
   ``doctor``/``index``/``query`` from the installed cache bytes under a scrubbed env, asserting no
   FORBIDDEN path (temp checkout OR developer repo) leaks via env, argv, or installed bytes and the
   interpreter is non-forbidden. Deletion of the developer git checkout itself stays a Phase 6
   release-acceptance cell on a runner that can drop the workspace.

Exit codes: 0 = installed and verified; 2 = a named prerequisite is absent (the builder
has not landed yet, or the ``codex`` CLI is not installed) — a recorded limitation, not a
failure; 1 = the probe ran but installation or verification failed.

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

_THIS = Path(__file__).resolve()
_SCRIPTS = _THIS.parent
_REPO_ROOT = _SCRIPTS.parents[2]
_BUILDER = _SCRIPTS / "build_package.py"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _probe_runtime import (
    RuntimeProof,
    build_from_checkout,
    runtime_proof,
    stage_disposable_source,
    write_real_mode_map,
)

MKT_NAME = "codemap-py-probe-mkt"
PLUGIN_NAME = "codemap-py"
# Version is asserted only as "present"; the authoritative value lives in the
# tracked .claude-plugin/plugin.json and flows through the builder.

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIP = 2


def codex_marketplace_manifest(rel_path: str) -> dict:
    """Return the Codex local-marketplace manifest for the candidate.

    ``rel_path`` is the candidate location relative to the marketplace root, ``./``-prefixed
    and kept inside the root, as Codex requires. The ``source.source`` discriminator must be
    present — without it Codex registers the marketplace but parses zero plugins.

    Examples:
        >>> m = codex_marketplace_manifest("./plugins/codemap-py")
        >>> m["plugins"][0]["source"]
        {'source': 'local', 'path': './plugins/codemap-py'}
        >>> m["plugins"][0]["name"]
        'codemap-py'
    """
    return {
        "name": MKT_NAME,
        "interface": {"displayName": "codemap-py probe marketplace"},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": {"source": "local", "path": rel_path},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
        ],
    }


def _package_codex_roster(installed_path: Path) -> list | None:
    """Return the installed ``package-manifest.json`` Codex roster, or ``None`` if absent."""
    manifest_path = installed_path / "package-manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        roster = json.loads(manifest_path.read_text()).get("skills", {}).get("codex")
    except (OSError, json.JSONDecodeError):
        return None
    return roster if isinstance(roster, list) else None


@dataclass(frozen=True)
class CodexInstallReport:
    """Static verification result for one installed Codex plugin tree.

    Dataclass (not TypedDict): named construction and attribute access;
    converted to a dict by :meth:`to_dict` at the JSON-emission boundary.

    Unlike its Claude counterpart there is no ``roster`` field: the Codex
    contract compares ``skill_dirs`` against the manifest's declared source,
    and the package-manifest roster is only read to assert it stays empty on
    the undeclared-source fallback.

    Attributes:
        ok: Whether every check in ``checks`` passed.
        checks: Per-check verdicts keyed by check name.
        issues: Human-readable reasons for each failed check.
        skill_dirs: Installed ``codex-skills/`` subdirectory names.
        manifest: Identity fields echoed from ``.codex-plugin/plugin.json``.

    Examples:
        >>> CodexInstallReport(ok=False, checks={}, issues=[], skill_dirs=[], manifest={}).ok
        False
    """

    ok: bool
    checks: dict[str, bool]
    issues: list[str]
    skill_dirs: list[str]
    manifest: dict

    def to_dict(self) -> dict:
        """Return the JSON-envelope projection of this report.

        Returns:
            A dict with ``ok``, ``checks``, ``issues``, ``skill_dirs``, ``manifest``.

        Examples:
            >>> sorted(CodexInstallReport(True, {}, [], [], {}).to_dict())
            ['checks', 'issues', 'manifest', 'ok', 'skill_dirs']
        """
        return {
            "ok": self.ok,
            "checks": self.checks,
            "issues": self.issues,
            "skill_dirs": self.skill_dirs,
            "manifest": self.manifest,
        }


def verify_codex_install(installed_path: Path) -> CodexInstallReport:
    """Statically check the installed Codex plugin bytes; return a checks/issues report.

    Verifies the manifest identity, then checks the skill roster against the
    manifest's own contract. The shipped plugin declares ``./codex-skills/``;
    the probe requires a Codex source rather than ``claude-skills`` and a
    non-empty installed directory. The undeclared-source fallback rejects
    unexpected roster directories and requires an empty package-manifest roster.

    Examples:
        >>> verify_codex_install(Path("/nonexistent")).ok
        False
    """
    checks: dict[str, bool] = {}
    issues: list[str] = []

    manifest_path = installed_path / ".codex-plugin" / "plugin.json"
    checks["manifest_present"] = manifest_path.is_file()
    manifest: dict = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(f"manifest unreadable: {exc}")
    else:
        issues.append("missing .codex-plugin/plugin.json")

    checks["name_matches"] = manifest.get("name") == PLUGIN_NAME
    if not checks["name_matches"]:
        issues.append(f"manifest name {manifest.get('name')!r} != {PLUGIN_NAME!r}")
    checks["version_present"] = bool(manifest.get("version"))
    if not checks["version_present"]:
        issues.append("manifest version missing")
    hooks_field = manifest.get("hooks")
    checks["hooks_field_is_codex"] = hooks_field == "./hooks/codex-hooks.json"
    issues.extend(
        [] if checks["hooks_field_is_codex"] else [f"manifest hooks {hooks_field!r} != './hooks/codex-hooks.json'"]
    )
    checks["hooks_config_present"] = (installed_path / "hooks" / "codex-hooks.json").is_file()
    issues.extend([] if checks["hooks_config_present"] else ["installed hooks/codex-hooks.json missing"])

    skills_field = manifest.get("skills")
    skills_dir = installed_path / "codex-skills"
    skill_dirs = sorted(p.name for p in skills_dir.iterdir() if p.is_dir()) if skills_dir.is_dir() else []
    package_codex_roster = _package_codex_roster(installed_path)
    if skills_field is None:
        # Undeclared-source fallback: no roster directories and an empty package roster.
        checks["no_roster_dirs_when_undeclared"] = not skills_dir.is_dir() and not (installed_path / "skills").is_dir()
        if not checks["no_roster_dirs_when_undeclared"]:
            issues.append("manifest declares no skills, but a roster directory is present")
        checks["package_codex_roster_empty"] = package_codex_roster == []
        if not checks["package_codex_roster_empty"]:
            issues.append(f"package-manifest codex roster must be empty, got {package_codex_roster}")
    else:
        checks["skills_field_is_codex"] = isinstance(skills_field, str) and "codex-skills" in skills_field
        checks["claude_skills_not_registered"] = not (isinstance(skills_field, str) and "claude-skills" in skills_field)
        if not checks["claude_skills_not_registered"]:
            issues.append(f"claude-skills registered as codex skills source: {skills_field!r}")
        checks["declared_roster_nonempty"] = len(skill_dirs) >= 1
        if not checks["declared_roster_nonempty"]:
            issues.append(f"manifest declares {skills_field!r} but no skill dirs found")

    return CodexInstallReport(
        ok=all(checks.values()),
        checks=checks,
        issues=issues,
        skill_dirs=skill_dirs,
        manifest={
            "name": manifest.get("name"),
            "version": manifest.get("version"),
            "skills": skills_field,
            "hooks": hooks_field,
        },
    )


def _codex_version() -> str | None:
    try:
        out = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=15, check=False)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or out.stderr.strip() or None


def _run(cmd: list[str], home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        env={**os.environ, "CODEX_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def run_probe() -> dict:
    """Drive the full disposable install and return a JSON-able result envelope."""
    codex_version = _codex_version()
    if codex_version is None:
        return {"probe": "codex", "status": "codex-cli-not-present", "codex_version": None}
    if not _BUILDER.is_file():
        return {
            "probe": "codex",
            "status": "builder-not-yet-present",
            "codex_version": codex_version,
            "detail": f"missing {_BUILDER}",
        }

    workdir = Path(tempfile.mkdtemp(prefix="codemap-py-codex-probe-"))
    home = workdir / "codex-home"
    home.mkdir()
    # The disposable source tree (copy checkout + marketplace + candidate) is deleted
    # before runtime; the installed cache under home survives it.
    src_root = workdir / "src"
    mkt_root = src_root / "mkt"
    candidate = mkt_root / "plugins" / PLUGIN_NAME
    candidate.mkdir(parents=True)
    mkt_manifest = mkt_root / ".agents" / "plugins" / "marketplace.json"
    mkt_manifest.parent.mkdir(parents=True)

    result: dict = {"probe": "codex", "codex_version": codex_version}
    try:
        # Capture the REAL repo's mode map BEFORE the disposable copy exists — the copy's
        # own synthesized index is not authoritative (see _probe_runtime.stage_disposable_source).
        mode_map_path = write_real_mode_map(_REPO_ROOT, src_root / "mode-map.json")
        checkout = stage_disposable_source(_REPO_ROOT, src_root / "checkout")
        built_ok, detail = build_from_checkout(checkout, candidate, mode_map_path)
        if not built_ok:
            return {**result, "status": "builder-failed", "detail": detail}

        mkt_manifest.write_text(json.dumps(codex_marketplace_manifest(f"./plugins/{PLUGIN_NAME}"), indent=2))

        add = _run(["codex", "plugin", "marketplace", "add", str(mkt_root), "--json"], home)
        if add.returncode != 0:
            return {**result, "status": "marketplace-add-failed", "detail": add.stderr.strip()}
        install = _run(["codex", "plugin", "add", f"{PLUGIN_NAME}@{MKT_NAME}", "--json"], home)
        if install.returncode != 0:
            return {**result, "status": "plugin-add-failed", "detail": install.stderr.strip()}
        installed_path = Path(json.loads(install.stdout)["installedPath"])

        verification = verify_codex_install(installed_path)
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
    parser = argparse.ArgumentParser(description="Non-interactive Codex install probe for codemap-py.")
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
    if result["status"] in {"builder-not-yet-present", "codex-cli-not-present"}:
        return EXIT_SKIP
    return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
